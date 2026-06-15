"""Unit tests for grid slow-drift pause (gridstrat + grid_drift_pause)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_VARIBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from grid_drift_pause import (
    apply_pause_clear,
    fetch_binance_drift_fraction,
    kline_limit_for_lookback_hours,
    run_grid_drift_pause_cycle,
)
from strategy.gridstrat import (
    drift_pause_threshold_pct,
    should_pause_for_drift,
)


class TestGridDriftPauseLogic(unittest.TestCase):
    def test_threshold_default_y2(self) -> None:
        self.assertAlmostEqual(drift_pause_threshold_pct(band_pct=2.5, band_mult=2.0), 5.0)

    def test_pause_up_at_threshold(self) -> None:
        self.assertTrue(
            should_pause_for_drift(0.05, band_pct=2.5, band_mult=2.0, direction="up")
        )
        self.assertFalse(
            should_pause_for_drift(0.04, band_pct=2.5, band_mult=2.0, direction="up")
        )

    def test_pause_down_only(self) -> None:
        self.assertTrue(
            should_pause_for_drift(-0.05, band_pct=2.5, band_mult=2.0, direction="down")
        )
        self.assertFalse(
            should_pause_for_drift(0.05, band_pct=2.5, band_mult=2.0, direction="down")
        )

    def test_kline_limit_4h(self) -> None:
        self.assertEqual(kline_limit_for_lookback_hours(4.0), 49)


class TestGridDriftPauseCycle(unittest.TestCase):
    @patch("grid_drift_pause.fetch_binance_5m_closes", return_value=[100.0, 105.0])
    def test_fetch_drift_fraction(self, _mock: object) -> None:
        drift = fetch_binance_drift_fraction("AAVE", lookback_hours=4.0)
        self.assertAlmostEqual(float(drift or 0), 0.05)

    @patch("grid_drift_pause.grid_band_pct_for_asset", return_value=2.5)
    @patch("grid_drift_pause.cancel_ticker_limits")
    @patch(
        "grid_drift_pause.fetch_binance_drift_fractions_parallel",
        return_value={"AAVE": 0.06, "BTC": 0.01},
    )
    def test_run_cycle_pauses_offender(self, _drift: object, _cancel: object, _band: object) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.environ["GRID_DRIFT_PAUSE_STATE"] = ".test_grid_drift_pause.json"
            os.environ.pop("GRID_DRIFT_PAUSE_CLEAR", None)
            ep = MagicMock()
            logs: list[str] = []
            closed: list[tuple[str, float, str]] = []

            def _close(sym: str, qty: float, side: str) -> None:
                closed.append((sym, qty, side))

            positions = [{"underlying": "AAVE", "qty": "-10.5"}]
            paused = run_grid_drift_pause_cycle(
                ep,
                positions,
                cycle_index=1,
                grid_tickers={"AAVE", "BTC"},
                varibot_dir=td,
                live=False,
                dry_run=True,
                log=logs.append,
                close_position=_close,
            )
            self.assertIn("AAVE", paused)
            self.assertNotIn("BTC", paused)
            self.assertTrue(any("grid_drift_pause[AAVE]: PAUSE" in ln for ln in logs))
            self.assertTrue(any("would flatten buy" in ln for ln in logs))
            self.assertEqual(closed, [])

    def test_position_qty_for_ticker(self) -> None:
        from grid_drift_pause import _position_qty_for_ticker

        raw = [{"underlying": "AAVE", "qty": "-3"}]
        self.assertAlmostEqual(_position_qty_for_ticker(raw, "AAVE") or 0, -3.0)
        self.assertIsNone(_position_qty_for_ticker(raw, "BTC"))

    def test_apply_pause_clear_all(self) -> None:
        state = {"paused": {"AAVE": {}, "BTC": {}}}
        os.environ["GRID_DRIFT_PAUSE_CLEAR"] = "ALL"
        cleared = apply_pause_clear(state)
        self.assertEqual(cleared, {"AAVE", "BTC"})
        self.assertEqual(state["paused"], {})


if __name__ == "__main__":
    unittest.main()
