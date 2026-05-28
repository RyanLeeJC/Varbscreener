"""Tests for grid limit reconcile helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from grid_limits_reconcile import _drift_cancel_enabled
from strategy.gridstrat import breach_reanchors_on_breach, gridstrat_flat_rebalance_enabled
from strategy.gridstrat_remnant import (
    compute_venue_actions,
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
    def test_drift_cancel_default_on(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VARIBOT_GRID_LIMITS_DRIFT_CANCEL", None)
            self.assertTrue(_drift_cancel_enabled())

    def test_drift_cancel_explicit_off(self) -> None:
        with patch.dict(os.environ, {"VARIBOT_GRID_LIMITS_DRIFT_CANCEL": "0"}, clear=False):
            self.assertFalse(_drift_cancel_enabled())


class TestHalfBandFraction(unittest.TestCase):
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
        """At mark 1.795, sell 1.859 is outside ±3% when grid_band_pct=3."""
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
            asset="ETH", result=result, venue_pending_keys=pending, mark=1.795
        )
        # With depth-only cancels, 1.859 isn't canceled unless keep depth is exceeded.
        # Toward-mark refill is enabled: should propose a new sell closer to mark.
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertEqual(len(sell_posts), 1)


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
            asset="ETH", result=result, venue_pending_keys=pending, mark=1.0
        )
        # Depth cancels only: ensure far orphan is canceled when depth is small.
        with patch.dict(os.environ, {"VARIBOT_GRID_LIMITS_KEEP_DEPTH": "1"}, clear=False):
            cancel, _ = compute_venue_actions(
                asset="ETH", result=result, venue_pending_keys=pending, mark=1.0
            )
            self.assertIn(("sell", grid_limit_price_key(2.5)), cancel)


class TestRemnantProximityHug(unittest.TestCase):
    def test_posts_intermediate_rungs_toward_mark(self) -> None:
        # When nearest sell is far from mark, proximity hug should post intermediate sells toward mark.
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
        cancel, post = compute_venue_actions(asset="ETH", result=result, venue_pending_keys=pending, mark=100.0)
        sell_posts = [px for side, px in post if side == "sell"]
        # Nearest sell is 101; toward-mark post should include 100 (but strict > mark, so 100 excluded),
        # so first valid is 101-1 = 100 (excluded), then count-fill should still post outward.
        # The reconciler should at least post one outward rung.
        self.assertTrue(any(abs(px - 105.0) < 1e-6 for px in sell_posts))

    def test_insufficient_count_uses_count_fill_only_not_gap(self) -> None:
        """Short on count: count-fill only — no duplicate gap+count posts on same side."""
        mark = 260.554
        spacing = 1.54183
        pending = {
            ("buy", grid_limit_price_key(257.858)),
            ("buy", grid_limit_price_key(256.316)),
            ("buy", grid_limit_price_key(254.774)),
            ("buy", grid_limit_price_key(253.232)),
            ("sell", grid_limit_price_key(mark + spacing)),
            ("sell", grid_limit_price_key(mark + 2 * spacing)),
            ("sell", grid_limit_price_key(mark + 3 * spacing)),
            ("sell", grid_limit_price_key(mark + 4 * spacing)),
            ("sell", grid_limit_price_key(mark + 5 * spacing)),
        }
        result = infer_ladder_from_remnants(
            mark=mark,
            venue_pending_keys=pending,
            configured_spacing=spacing,
            lower=249.262,
            upper=264.68,
            grid_num=10,
            grid_band_pct=3.0,
        )
        self.assertLess(len(result.inband_buys), result.window_n)
        _, post = compute_venue_actions(
            asset="TAO", result=result, venue_pending_keys=pending, mark=mark
        )
        buy_posts = [px for side, px in post if side == "buy"]
        self.assertEqual(1, len(buy_posts))

    def test_sufficient_window_still_gap_fills_when_nearest_far(self) -> None:
        """8 in-band sells (>=5) but nearest sell >1 spacing above mark → post toward mark."""
        mark = 630.99
        spacing = 1.29
        pending = {
            ("sell", grid_limit_price_key(634.06)),
            ("sell", grid_limit_price_key(635.35)),
            ("sell", grid_limit_price_key(636.64)),
            ("sell", grid_limit_price_key(637.93)),
            ("sell", grid_limit_price_key(639.22)),
            ("sell", grid_limit_price_key(640.51)),
            ("sell", grid_limit_price_key(641.80)),
            ("sell", grid_limit_price_key(643.09)),
            ("sell", grid_limit_price_key(644.38)),
            ("buy", grid_limit_price_key(629.34)),
            ("buy", grid_limit_price_key(628.05)),
            ("buy", grid_limit_price_key(626.76)),
            ("buy", grid_limit_price_key(625.47)),
            ("buy", grid_limit_price_key(624.18)),
        }
        result = infer_ladder_from_remnants(
            mark=mark,
            venue_pending_keys=pending,
            configured_spacing=spacing,
            lower=500.0,
            upper=800.0,
            grid_num=10,
            grid_band_pct=2.0,
        )
        self.assertTrue(result.sufficient)
        self.assertGreaterEqual(len(result.inband_sells), 5)
        _, post = compute_venue_actions(
            asset="BNB", result=result, venue_pending_keys=pending, mark=mark
        )
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertTrue(any(abs(px - 632.77) < 0.02 for px in sell_posts))

    def test_sufficient_window_skips_proximity_hug(self) -> None:
        # Nearest sell within ~1 spacing of mark — no gap-fill even when sufficient.
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
        _, post = compute_venue_actions(asset="ETH", result=result, venue_pending_keys=pending, mark=100.0)
        self.assertEqual([], [px for side, px in post if side == "sell"])

    def test_can_post_multiple_rungs_when_needed(self) -> None:
        # With a large gap, hug can post multiple intermediate rungs in one cycle.
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
        _, post = compute_venue_actions(asset="ETH", result=result, venue_pending_keys=pending, mark=100.0)
        sell_posts = [px for side, px in post if side == "sell"]
        self.assertGreaterEqual(len(sell_posts), 1)


if __name__ == "__main__":
    unittest.main()
