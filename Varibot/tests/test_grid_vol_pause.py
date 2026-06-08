"""Unit tests for grid_vol_pause cycle-start macro detector."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_VARIBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from grid_vol_pause import (
    VolPauseConfig,
    _append_history,
    _market_stressed,
    _should_pause,
    _ticker_stressed,
    compute_realized_vol_ratio,
    cum_return,
    evaluate_ticker,
    load_config,
    run_grid_vol_pause_cycle,
    ticker_cum_thresholds,
)


def _cfg(**kw) -> VolPauseConfig:
    base = dict(
        enabled=True,
        market_ret=-0.02,
        market_pump_ret=0.02,
        ticker_cum_band_mult=1.6,
        ticker_bar_ret=-0.012,
        ticker_bar_pump_ret=0.012,
        vol_ratio_pause=1.3,
        resume_cycles=18,
        resume_market_ret=-0.005,
        resume_market_pump_ret=0.005,
        resume_ticker_band_mult=0.4,
        resume_vol_ratio=1.3,
        require_both=True,
        market_lb_cycles=60,
        ticker_lb_cycles=30,
        ticker_bar_lb_cycles=5,
        vol_lb_bars_5m=36,
        vol_median_bars_5m=72,
        binance_workers=8,
        min_pause_cycles=60,
    )
    base.update(kw)
    return VolPauseConfig(**base)


class TestGridVolPauseLogic(unittest.TestCase):
    def test_cum_return(self) -> None:
        self.assertAlmostEqual(cum_return(98.0, 100.0), -0.02)

    def test_ticker_cum_thresholds_from_band(self) -> None:
        dump, pump = ticker_cum_thresholds(band_pct=2.5, mult=1.6)
        self.assertAlmostEqual(dump, -0.04)
        self.assertAlmostEqual(pump, +0.04)

    def test_realized_vol_ratio_computes_with_study_window(self) -> None:
        closes = [100.0 + math.sin(i / 5.0) * 0.5 for i in range(350)]
        ratio = compute_realized_vol_ratio(closes, vol_lb=36, vol_median_lb=100)
        self.assertIsNotNone(ratio)
        assert ratio is not None
        self.assertTrue(math.isfinite(ratio))
        self.assertGreater(ratio, 0.0)

    def test_realized_vol_ratio_flat_is_near_one(self) -> None:
        closes = [100.0 + i * 0.001 for i in range(350)]
        ratio = compute_realized_vol_ratio(closes, vol_lb=36, vol_median_lb=100)
        self.assertIsNotNone(ratio)
        assert ratio is not None
        self.assertLess(ratio, 1.2)

    def test_ticker_vol_stress(self) -> None:
        cfg = _cfg()
        dump_t, pump_t = ticker_cum_thresholds(band_pct=2.5, mult=1.6)
        stress, d, p = _ticker_stressed(
            cfg,
            cum_ret=0.0,
            bar_ret=0.0,
            vol_ratio=2.5,
            cum_dump_thresh=dump_t,
            cum_pump_thresh=pump_t,
        )
        self.assertTrue(stress)
        self.assertTrue(d)
        self.assertTrue(p)

    def test_and_requires_both(self) -> None:
        cfg = _cfg()
        m_stress, _, _ = _market_stressed(cfg, btc_ret=-0.03, eth_ret=0.0)
        dump_t, pump_t = ticker_cum_thresholds(band_pct=2.5, mult=1.6)
        t_stress, _, _ = _ticker_stressed(
            cfg, cum_ret=-0.05, bar_ret=0.0, vol_ratio=None,
            cum_dump_thresh=dump_t, cum_pump_thresh=pump_t,
        )
        self.assertTrue(_should_pause(cfg, market_stress=m_stress, ticker_stress=t_stress))
        self.assertFalse(_should_pause(cfg, market_stress=m_stress, ticker_stress=False))
        self.assertFalse(_should_pause(cfg, market_stress=False, ticker_stress=t_stress))

    def test_vol_alone_does_not_pause_without_market(self) -> None:
        cfg = _cfg(require_both=True)
        dump_t, pump_t = ticker_cum_thresholds(band_pct=2.5, mult=1.6)
        vol_only, _, _ = _ticker_stressed(
            cfg, cum_ret=0.0, bar_ret=0.0, vol_ratio=2.0,
            cum_dump_thresh=dump_t, cum_pump_thresh=pump_t,
        )
        self.assertTrue(vol_only)
        self.assertFalse(_should_pause(cfg, market_stress=False, ticker_stress=vol_only))

    def test_market_alone_does_not_pause_without_ticker(self) -> None:
        cfg = _cfg(require_both=True)
        m_stress, _, _ = _market_stressed(cfg, btc_ret=-0.03, eth_ret=0.0)
        self.assertFalse(_should_pause(cfg, market_stress=m_stress, ticker_stress=False))

    def test_history_1h_return_at_cycle_60(self) -> None:
        cfg = _cfg(require_both=False)
        state = {"paused": {}, "calm_cycles": {}, "history": []}
        for c in range(1, 61):
            _append_history(state, cycle_index=c, prices={"BTC": 100.0, "ETH": 100.0, "JUP": 100.0})
        _append_history(state, cycle_index=61, prices={"BTC": 97.0, "ETH": 100.0, "JUP": 95.0})
        pause, _, dbg = evaluate_ticker(
            cfg,
            state,
            cycle_index=61,
            ticker="JUP",
            prices={"BTC": 97.0, "ETH": 100.0, "JUP": 95.0},
            band_pct=2.5,
            btc_ret=-0.03,
            eth_ret=0.0,
            ticker_cum=-0.05,
            binance_bar_5m={},
            vol_ratio=1.0,
        )
        self.assertTrue(pause)

    @patch("grid_vol_pause.fetch_binance_vol_ratios_parallel", return_value={"JUP": 1.0})
    @patch("grid_vol_pause.fetch_binance_futures_prices", return_value={"BTC": 97.0, "ETH": 100.0, "JUP": 95.0})
    @patch("grid_vol_pause.fetch_binance_kline_returns_parallel")
    @patch("grid_vol_pause.cancel_ticker_limits")
    def test_early_cycle_uses_binance(
        self,
        mock_cancel: unittest.mock.MagicMock,
        mock_klines: unittest.mock.MagicMock,
        mock_prices: unittest.mock.MagicMock,
        mock_vol: unittest.mock.MagicMock,
    ) -> None:
        def _klines(tickers, *, limit, workers):
            out = {}
            for t in tickers:
                if limit >= 61:
                    out[t] = -0.03 if t == "BTC" else 0.0
                elif limit >= 31:
                    out[t] = -0.05
                else:
                    out[t] = -0.02
            return out

        mock_klines.side_effect = _klines
        env = {
            "GRID_VOL_PAUSE_ENABLED": "1",
            "GRID_VOL_PAUSE_MARKET_RET": "-0.02",
            "GRID_VOL_PAUSE_TICKER_CUM_BAND_MULT": "1.6",
            "GRID_VOL_PAUSE_REQUIRE_BOTH": "0",
            "GRID_VOL_PAUSE_STATE": ".test_grid_vol_pause.json",
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, env, clear=False):
                paused = run_grid_vol_pause_cycle(
                    object(),
                    cycle_index=5,
                    grid_tickers={"JUP"},
                    varibot_dir=td,
                    live=False,
                    dry_run=True,
                    log=lambda _m: None,
                )
            mock_vol.assert_called_once()
            self.assertIn("JUP", paused)
            mock_cancel.assert_called_once()


class TestGridVolPauseConfig(unittest.TestCase):
    def test_defaults_match_recc(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertAlmostEqual(cfg.market_ret, -0.02)
        self.assertAlmostEqual(cfg.ticker_cum_band_mult, 1.6)
        self.assertEqual(cfg.vol_lb_bars_5m, 36)
        self.assertEqual(cfg.vol_median_bars_5m, 72)
        self.assertEqual(cfg.min_pause_cycles, 60)
        self.assertTrue(cfg.require_both)
        self.assertAlmostEqual(cfg.vol_ratio_pause, 1.3)


if __name__ == "__main__":
    unittest.main()
