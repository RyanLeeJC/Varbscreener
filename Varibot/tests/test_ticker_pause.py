"""Unit tests for ticker_pause pain trigger."""

from __future__ import annotations

import unittest

from ticker_pause import PositionPnL, pain_triggered, evaluate_pain_candidates


class TestTickerPausePain(unittest.TestCase):
    def test_trigger_combined_below_five_pct_value(self) -> None:
        # threshold = -5% × $3756 ≈ -$187.81; combined must be below that
        pos = PositionPnL(ticker="NEAR", qty=1564.0, upnl_usd=-177.39, rpnl_usd=-15.0, value_usd=3756.18)
        self.assertTrue(pain_triggered(pos, pnl_frac=0.05, min_value_usd=50.0))

    def test_near_minus_177_alone_below_five_pct(self) -> None:
        """uPnL only −$177 on ~$3756 value is ~4.7% — does not trip 5% rule."""
        pos = PositionPnL(ticker="NEAR", qty=1564.0, upnl_usd=-177.39, rpnl_usd=0.0, value_usd=3756.18)
        self.assertFalse(pain_triggered(pos, pnl_frac=0.05, min_value_usd=50.0))

    def test_no_trigger_above_threshold(self) -> None:
        pos = PositionPnL(ticker="ONDO", qty=100.0, upnl_usd=-100.0, rpnl_usd=50.0, value_usd=4000.0)
        # combined -50, threshold -200
        self.assertFalse(pain_triggered(pos, pnl_frac=0.05, min_value_usd=50.0))

    def test_rpnl_counts_toward_trigger(self) -> None:
        pos = PositionPnL(ticker="JUP", qty=100.0, upnl_usd=-80.0, rpnl_usd=-80.0, value_usd=2000.0)
        # combined -160, threshold -100
        self.assertTrue(pain_triggered(pos, pnl_frac=0.05, min_value_usd=50.0))

    def test_skips_small_value(self) -> None:
        pos = PositionPnL(ticker="X", qty=1.0, upnl_usd=-100.0, rpnl_usd=0.0, value_usd=10.0)
        self.assertFalse(pain_triggered(pos, pnl_frac=0.05, min_value_usd=50.0))

    def test_evaluate_filters_grid_tickers(self) -> None:
        raw = [
            {
                "instrument": {"underlying": "NEAR"},
                "qty": 100.0,
                "value": 4000.0,
                "unrealized_pnl": -300.0,
                "realized_pnl": -50.0,
            },
            {
                "instrument": {"underlying": "ONDO"},
                "qty": 100.0,
                "value": 4000.0,
                "unrealized_pnl": -10.0,
                "realized_pnl": 0.0,
            },
        ]
        hits = evaluate_pain_candidates(
            raw,
            grid_tickers={"NEAR"},
            pnl_frac=0.05,
            min_value_usd=50.0,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].ticker, "NEAR")


if __name__ == "__main__":
    unittest.main()
