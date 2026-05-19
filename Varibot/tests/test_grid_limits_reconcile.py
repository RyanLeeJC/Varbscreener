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
    _state_open_limit_keys,
)
from strategy.gridstrat import gridstrat_flat_rebalance_enabled


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
