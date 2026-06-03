"""Unit tests for book_hedge.plan_book_hedge."""

from __future__ import annotations

import unittest

from book_hedge import (
    compute_book_net,
    compute_hedge_net,
    plan_book_hedge,
)
from portfolio_rebalance import LivePosition


def _pos(ticker: str, side: str, qty: float, mark: float) -> LivePosition:
    return LivePosition(
        ticker=ticker,
        side=side,
        quantity=qty,
        mark_price=mark,
        upnl_usd=None,
    )


class TestBookHedgePlan(unittest.TestCase):
    def test_below_trigger_skips(self) -> None:
        # port 3330, 3x = 9990; book net 5000 long; no hedge legs open
        positions = [
            _pos("XPL", "long", 1000.0, 5.0),
        ]
        plan = plan_book_hedge(
            portfolio_value_usd=3330.0,
            positions=positions,
            port_mult=3.0,
            adjust_usd=1000.0,
            mark_by_ticker={"BTC": 67000.0, "ETH": 1850.0, "SOL": 140.0},
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.action, "skip")
        self.assertEqual(len(plan.legs), 0)

    def test_trigger_opens_equal_short_legs(self) -> None:
        # longs 13k shorts 1k -> book 12k; port 3.33k -> trigger ~9.99k
        positions = [
            _pos("XPL", "long", 10000.0, 1.0),  # 10k
            _pos("FET", "long", 3000.0, 1.0),  # 3k
            _pos("PENGU", "short", 1000.0, 1.0),  # -1k
        ]
        plan = plan_book_hedge(
            portfolio_value_usd=3330.0,
            positions=positions,
            port_mult=3.0,
            adjust_usd=1000.0,
            mark_by_ticker={"BTC": 67000.0, "ETH": 1850.0, "SOL": 140.0},
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.action, "open_adjust")
        self.assertAlmostEqual(plan.book_net_usd, 12000.0)
        self.assertAlmostEqual(plan.hedge_target_usd, -12000.0)
        self.assertEqual(len(plan.legs), 3)
        for leg in plan.legs:
            self.assertEqual(leg.order_side, "sell")
            self.assertAlmostEqual(leg.target_signed_notional, -4000.0, places=0)

    def test_hold_when_gap_under_adjust_usd(self) -> None:
        positions = [
            _pos("XPL", "long", 12000.0, 1.0),
            _pos("BTC", "short", 11500.0 / 67000.0, 67000.0),
        ]
        plan = plan_book_hedge(
            portfolio_value_usd=3000.0,
            positions=positions,
            port_mult=3.0,
            adjust_usd=1000.0,
            mark_by_ticker={"BTC": 67000.0, "ETH": 1850.0, "SOL": 140.0},
        )
        assert plan is not None
        self.assertEqual(plan.action, "hold")
        self.assertEqual(len(plan.legs), 0)

    def test_adjust_when_gap_over_1000(self) -> None:
        positions = [
            _pos("XPL", "long", 14000.0, 1.0),
            _pos("BTC", "short", 12000.0 / 67000.0, 67000.0),
        ]
        plan = plan_book_hedge(
            portfolio_value_usd=3000.0,
            positions=positions,
            port_mult=3.0,
            adjust_usd=1000.0,
            mark_by_ticker={"BTC": 67000.0, "ETH": 1850.0, "SOL": 140.0},
        )
        assert plan is not None
        self.assertEqual(plan.action, "open_adjust")
        self.assertGreater(len(plan.legs), 0)

    def test_close_hedge_when_book_below_trigger(self) -> None:
        positions = [
            _pos("XPL", "long", 2000.0, 1.0),
            _pos("BTC", "short", 0.05, 67000.0),
            _pos("ETH", "short", 1.0, 1850.0),
        ]
        plan = plan_book_hedge(
            portfolio_value_usd=3330.0,
            positions=positions,
            port_mult=3.0,
            mark_by_ticker={"BTC": 67000.0, "ETH": 1850.0, "SOL": 140.0},
        )
        assert plan is not None
        self.assertEqual(plan.action, "close_all")
        tickers = {leg.ticker for leg in plan.legs}
        self.assertIn("BTC", tickers)
        self.assertIn("ETH", tickers)

    def test_book_excludes_hedge_tickers(self) -> None:
        positions = [
            _pos("BTC", "long", 1.0, 67000.0),
            _pos("XPL", "long", 1.0, 100.0),
        ]
        self.assertAlmostEqual(compute_book_net(positions), 100.0)
        self.assertAlmostEqual(compute_hedge_net(positions), 67000.0)


if __name__ == "__main__":
    unittest.main()
