"""Tests for paired-grid drift reconcile (refill-first, narrow cancel)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from grid_limits_reconcile import (
    _drift_cancel_enabled,
    _drift_cancel_orphan_keys,
    _skip_order_history_on_flat_map,
    _state_open_limit_keys,
)
from strategy.gridstrat import gridstrat_flat_rebalance_enabled
from variationalbot.vari.endpoints import instrument_query_param


class TestInstrumentQueryParam(unittest.TestCase):
    def test_crypto_has_four_segment_filter(self) -> None:
        self.assertEqual(instrument_query_param("ETH"), "P-ETH-USDC-3600")

    def test_rwa_omits_filter(self) -> None:
        self.assertIsNone(instrument_query_param("XAU"))
        self.assertIsNone(instrument_query_param("COPPER"))


class TestSkipOrderHistoryOnFlatMap(unittest.TestCase):
    def test_skips_when_flat_and_no_pending(self) -> None:
        self.assertTrue(
            _skip_order_history_on_flat_map(has_positions=False, pending_keys=set())
        )

    def test_fetches_when_flat_but_has_pending(self) -> None:
        self.assertFalse(
            _skip_order_history_on_flat_map(
                has_positions=False, pending_keys={("buy", "100.0")}
            )
        )

    def test_fetches_when_positioned(self) -> None:
        self.assertFalse(
            _skip_order_history_on_flat_map(has_positions=True, pending_keys=set())
        )


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


class TestDriftCancelOrphanKeys(unittest.TestCase):
    def test_protects_sim_open_book(self) -> None:
        state = {
            "schema_version": 4,
            "assets": {
                "ETH": {
                    "orders": [
                        {"side": "sell", "level": 2119.27, "status": "open"},
                        {"side": "buy", "level": 2098.20, "status": "open"},
                    ],
                },
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(state, f)
            path = f.name
        try:
            with patch("grid_limits_reconcile._default_state_path", return_value=path):
                sim_keys = _state_open_limit_keys("ETH")
                orphans = {("sell", "2119.27"), ("buy", "2098.20"), ("sell", "2999.00")}
                cancel = _drift_cancel_orphan_keys(
                    asset="ETH",
                    pending_orphans=orphans,
                    ameta={},
                )
                self.assertEqual(cancel, {("sell", "2999.00")})
                self.assertIn(("sell", "2119.27"), sim_keys)
        finally:
            os.unlink(path)

    def test_flat_rebalance_cancels_all_orphans(self) -> None:
        orphans = {("sell", "2119.27"), ("buy", "2098.20")}
        cancel = _drift_cancel_orphan_keys(
            asset="ETH",
            pending_orphans=orphans,
            ameta={"grid_flat_inventory_rebalance": True},
        )
        self.assertEqual(cancel, orphans)


if __name__ == "__main__":
    unittest.main()
