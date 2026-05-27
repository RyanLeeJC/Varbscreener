"""Tests for grid limit reconcile helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from grid_limits_reconcile import _drift_cancel_enabled
from strategy.gridstrat import breach_reanchors_on_breach, gridstrat_flat_rebalance_enabled
from strategy.gridstrat_remnant import (
    compute_venue_actions,
    expanded_band_tolerance,
    half_band_fraction,
    infer_ladder_from_remnants,
)
from variationalbot.vari.endpoints import grid_limit_price_key, instrument_query_param


class TestInstrumentQueryParam(unittest.TestCase):
    def test_crypto_has_four_segment_filter(self) -> None:
        self.assertEqual(instrument_query_param("ETH"), "P-ETH-USDC-3600")

    def test_rwa_omits_filter(self) -> None:
        self.assertIsNone(instrument_query_param("XAU"))
        self.assertIsNone(instrument_query_param("COPPER"))


class TestBreachDefault(unittest.TestCase):
    def test_breach_reset_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRID_REARM_ON_BREACH", None)
            self.assertFalse(breach_reanchors_on_breach())


class TestFlatRebalanceDefault(unittest.TestCase):
    def test_flat_rebalance_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRIDSTRAT_FLAT_REBALANCE", None)
            self.assertFalse(gridstrat_flat_rebalance_enabled())


class TestDriftCancelDefaults(unittest.TestCase):
    def test_drift_cancel_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VARIBOT_GRID_LIMITS_DRIFT_CANCEL", None)
            self.assertFalse(_drift_cancel_enabled())


class TestExpandedBandTolerance(unittest.TestCase):
    def test_band_scales_with_grid_num(self) -> None:
        self.assertAlmostEqual(expanded_band_tolerance(0.03, 10), 0.035454545454545456)
        self.assertAlmostEqual(expanded_band_tolerance(0.03, 20), 0.032857142857142856)

    def test_half_band_prefers_grid_band_pct(self) -> None:
        self.assertAlmostEqual(
            half_band_fraction(grid_band_pct=3.0, lower=1.846, upper=1.961),
            0.03,
        )
        # Fallback: half-band from pinned bounds ratio (not exactly 3% unless symmetric).
        fb = half_band_fraction(grid_band_pct=None, lower=0.94, upper=1.06)
        self.assertGreater(fb, 0.05)


class TestRemnantUsesConfiguredBand(unittest.TestCase):
    def test_farthest_sell_out_when_mark_drifts(self) -> None:
        """At mark 1.795, sell 1.859 is outside ±3.545% when grid_band_pct=3."""
        pending = {
            ("buy", grid_limit_price_key(1.783)),
            ("buy", grid_limit_price_key(1.773)),
            ("buy", grid_limit_price_key(1.762)),
            ("buy", grid_limit_price_key(1.751)),
            ("sell", grid_limit_price_key(1.816)),
            ("sell", grid_limit_price_key(1.827)),
            ("sell", grid_limit_price_key(1.837)),
            ("sell", grid_limit_price_key(1.848)),
            ("sell", grid_limit_price_key(1.859)),
        }
        result = infer_ladder_from_remnants(
            mark=1.795,
            venue_pending_keys=pending,
            configured_spacing=0.01083,
            lower=1.846,
            upper=1.961,
            grid_num=10,
            grid_band_pct=3.0,
        )
        self.assertEqual(len(result.inband_sells), 4)
        self.assertNotIn(1.859, result.inband_sells)
        cancel, post = compute_venue_actions(
            result=result, venue_pending_keys=pending, mark=1.795
        )
        # With depth-only cancels, 1.859 isn't canceled unless keep depth is exceeded.
        # Outward-only refill: next rung above 1.848 exceeds expanded band at this mark → skip.
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertEqual(len(sell_posts), 0)


class TestRemnantProtectedWindow(unittest.TestCase):
    def test_outside_window_orphans_cancelled(self) -> None:
        pending = {
            ("buy", grid_limit_price_key(0.9)),
            ("buy", grid_limit_price_key(0.8)),
            ("sell", grid_limit_price_key(1.1)),
            ("sell", grid_limit_price_key(1.2)),
            ("sell", grid_limit_price_key(2.5)),  # far orphan
        }
        result = infer_ladder_from_remnants(
            mark=1.0,
            venue_pending_keys=pending,
            configured_spacing=0.1,
            lower=0.5,
            upper=1.5,
            grid_num=10,
            nearest_n=5,
        )
        cancel, post = compute_venue_actions(
            result=result, venue_pending_keys=pending, mark=1.0
        )
        # Depth cancels only: ensure far orphan is canceled when depth is small.
        with patch.dict(os.environ, {"VARIBOT_GRID_LIMITS_KEEP_DEPTH": "1"}, clear=False):
            cancel, _ = compute_venue_actions(
                result=result, venue_pending_keys=pending, mark=1.0
            )
            self.assertIn(("sell", grid_limit_price_key(2.5)), cancel)


class TestRemnantProximityHug(unittest.TestCase):
    def test_posts_intermediate_rungs_toward_mark(self) -> None:
        # One in-band sell short of window N; refill posts one rung (outward or toward mark).
        pending = {
            ("sell", grid_limit_price_key(101.0)),
            ("sell", grid_limit_price_key(102.0)),
            ("sell", grid_limit_price_key(103.0)),
            ("sell", grid_limit_price_key(104.0)),
            ("buy", grid_limit_price_key(99.0)),
            ("buy", grid_limit_price_key(98.0)),
            ("buy", grid_limit_price_key(97.0)),
            ("buy", grid_limit_price_key(96.0)),
            ("buy", grid_limit_price_key(95.0)),
        }
        result = infer_ladder_from_remnants(
            mark=100.0,
            venue_pending_keys=pending,
            configured_spacing=1.0,
            lower=50.0,
            upper=150.0,
            grid_num=10,
            nearest_n=5,
            grid_band_pct=20.0,
        )
        cancel, post = compute_venue_actions(result=result, venue_pending_keys=pending, mark=100.0)
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertEqual(1, len(sell_posts))
        self.assertTrue(abs(sell_posts[0] - 105.0) < 1e-6)  # outward from 104

    def test_sufficient_window_skips_proximity_hug(self) -> None:
        # Rungs inside expanded ±20% band around mark=100 (avoid far 110+ sells outside win_upper).
        pending = {
            ("sell", grid_limit_price_key(101.0)),
            ("sell", grid_limit_price_key(102.0)),
            ("sell", grid_limit_price_key(103.0)),
            ("sell", grid_limit_price_key(104.0)),
            ("sell", grid_limit_price_key(105.0)),
            ("buy", grid_limit_price_key(99.0)),
            ("buy", grid_limit_price_key(98.0)),
            ("buy", grid_limit_price_key(97.0)),
            ("buy", grid_limit_price_key(96.0)),
            ("buy", grid_limit_price_key(95.0)),
        }
        result = infer_ladder_from_remnants(
            mark=100.0,
            venue_pending_keys=pending,
            configured_spacing=1.0,
            lower=50.0,
            upper=150.0,
            grid_num=10,
            nearest_n=5,
            grid_band_pct=20.0,
        )
        self.assertTrue(result.sufficient)
        _, post = compute_venue_actions(result=result, venue_pending_keys=pending, mark=100.0)
        self.assertEqual([], [px for side, px in post if side == "sell"])

    def test_at_most_one_post_per_missing_inband_rung(self) -> None:
        # 4 in-band sells, N=5 → at most 1 sell post this cycle (no gap + count double-up).
        pending = {
            ("sell", grid_limit_price_key(110.0)),
            ("sell", grid_limit_price_key(111.0)),
            ("sell", grid_limit_price_key(112.0)),
            ("sell", grid_limit_price_key(113.0)),
            ("buy", grid_limit_price_key(90.0)),
            ("buy", grid_limit_price_key(89.0)),
            ("buy", grid_limit_price_key(88.0)),
            ("buy", grid_limit_price_key(87.0)),
            ("buy", grid_limit_price_key(86.0)),
        }
        result = infer_ladder_from_remnants(
            mark=100.0,
            venue_pending_keys=pending,
            configured_spacing=1.0,
            lower=50.0,
            upper=150.0,
            grid_num=10,
            nearest_n=5,
            grid_band_pct=10.0,
        )
        _, post = compute_venue_actions(result=result, venue_pending_keys=pending, mark=100.0)
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertLessEqual(len(sell_posts), 1)


if __name__ == "__main__":
    unittest.main()
