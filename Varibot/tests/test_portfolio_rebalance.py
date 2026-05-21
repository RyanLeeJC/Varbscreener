"""Unit tests for portfolio_rebalance.plan_portfolio_rebalance."""

from __future__ import annotations

import unittest

from portfolio_rebalance import (
    IM_TARGET,
    LivePosition,
    grid_rung_usd_notional,
    plan_portfolio_rebalance,
    plan_position_trims,
    round_to_nearest,
)


def _pos(ticker: str, side: str, qty: float, mark: float) -> LivePosition:
    return LivePosition(ticker=ticker, side=side, quantity=qty, mark_price=mark)


class TestPlanPortfolioRebalance(unittest.TestCase):
    def test_trigger_below_skips(self) -> None:
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.34,
            positions=[_pos("ETH", "long", 1.0, 2000.0)],
            margin_trigger=0.35,
        )
        self.assertIsNone(plan)

    def test_odd_n_drops_smallest_notional(self) -> None:
        positions = [
            _pos("AAA", "long", 1.0, 100.0),
            _pos("BBB", "short", 1.0, 200.0),
            _pos("CCC", "long", 1.0, 300.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
            im_target=0.20,
            round_to=10.0,
            min_order_usd=5.0,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.dropped_ticker, "AAA")
        self.assertEqual(plan.n_eff, 2)
        self.assertEqual(set(plan.working_tickers), {"BBB", "CCC"})

    def test_odd_n_tiebreak_alphabetical_drop(self) -> None:
        positions = [
            _pos("ZZZ", "long", 1.0, 50.0),
            _pos("AAA", "short", 1.0, 50.0),
            _pos("MMM", "long", 1.0, 50.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.dropped_ticker, "AAA")

    def test_even_n_no_drop(self) -> None:
        positions = [
            _pos("ETH", "long", 1.0, 100.0),
            _pos("BTC", "short", 1.0, 100.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIsNone(plan.dropped_ticker)
        self.assertEqual(plan.n_eff, 2)

    def test_already_at_target_no_orders(self) -> None:
        target = round_to_nearest(1000.0 * 50.0 * IM_TARGET / 2, 10.0)
        qty = target / 100.0
        positions = [
            _pos("ETH", "long", qty, 100.0),
            _pos("BTC", "short", qty, 100.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
            min_order_usd=5.0,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.orders), 0)

    def test_soft_sort_assigns_biggest_longs_and_shorts(self) -> None:
        positions = [
            _pos("L1", "long", 10.0, 100.0),
            _pos("L2", "long", 8.0, 100.0),
            _pos("S1", "short", 9.0, 100.0),
            _pos("S2", "short", 7.0, 100.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
            min_order_usd=0.01,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        by_ticker = {o.ticker: o for o in plan.orders}
        self.assertEqual(by_ticker["L1"].assigned_side, "long")
        self.assertEqual(by_ticker["L2"].assigned_side, "long")
        self.assertEqual(by_ticker["S1"].assigned_side, "short")
        self.assertEqual(by_ticker["S2"].assigned_side, "short")
        self.assertFalse(by_ticker["L1"].flip)
        self.assertFalse(by_ticker["S1"].flip)

    def test_flip_when_imbalanced_sides(self) -> None:
        positions = [
            _pos("A", "long", 10.0, 100.0),
            _pos("B", "long", 9.0, 100.0),
            _pos("C", "long", 8.0, 100.0),
            _pos("D", "short", 1.0, 100.0),
        ]
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
            min_order_usd=0.01,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        flipped = [o for o in plan.orders if o.flip]
        self.assertTrue(len(flipped) >= 1)
        for o in plan.orders:
            if o.ticker == "D":
                self.assertEqual(o.assigned_side, "short")
            if o.ticker == "A":
                self.assertEqual(o.assigned_side, "long")

    def test_worked_example_target_710(self) -> None:
        marks = {
            "ONDO": 0.5,
            "LDO": 1.0,
            "SUI": 2.0,
            "ETH": 2000.0,
            "BTC": 60000.0,
            "SOL": 80.0,
            "XRP": 0.5,
            "DOGE": 0.1,
            "LINK": 9.0,
            "AVAX": 9.0,
            "NEAR": 1.5,
            "HYPE": 45.0,
            "XMR": 380.0,
            "ENA": 0.5,
            "AAVE": 100.0,
        }
        positions = []
        for i, (sym, mk) in enumerate(marks.items()):
            side = "long" if i < 8 else "short"
            qty = (25.0 + i * 10.0) / mk
            positions.append(_pos(sym, side, qty, mk))

        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=positions,
            margin_trigger=0.35,
            im_target=0.20,
            round_to=10.0,
            min_order_usd=5.0,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.n_eff, 14)
        self.assertEqual(plan.target_notional, 710.0)
        self.assertIsNotNone(plan.dropped_ticker)
        longs = [o for o in plan.orders if o.assigned_side == "long"]
        shorts = [o for o in plan.orders if o.assigned_side == "short"]
        self.assertEqual(len(longs), 7)
        self.assertEqual(len(shorts), 7)
        self.assertGreater(plan.total_volume_usd, 0.0)

    def test_delta_qty_signs(self) -> None:
        plan = plan_portfolio_rebalance(
            portfolio_value=1000.0,
            max_leverage=50.0,
            current_im_usage=0.40,
            positions=[
                _pos("ETH", "long", 2.0, 100.0),
                _pos("BTC", "short", 2.0, 100.0),
            ],
            margin_trigger=0.35,
            min_order_usd=0.01,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        eth = next(o for o in plan.orders if o.ticker == "ETH")
        if eth.delta_qty > 0:
            self.assertEqual(eth.order_side, "buy")
        else:
            self.assertEqual(eth.order_side, "sell")


class TestPlanPositionTrims(unittest.TestCase):
    def test_rung_usd_default_200(self) -> None:
        self.assertEqual(grid_rung_usd_notional(), 200.0)

    def test_trims_when_over_threshold(self) -> None:
        # 15 × $200 = $3000; $3500 long → sell 50% of qty
        trims = plan_position_trims(
            [_pos("ETH", "long", 1.75, 2000.0)],
            trim_multiple=15.0,
            trim_fraction=0.5,
            rung_usd=200.0,
            min_order_usd=5.0,
        )
        self.assertEqual(len(trims), 1)
        t = trims[0]
        self.assertEqual(t.ticker, "ETH")
        self.assertEqual(t.order_side, "sell")
        self.assertAlmostEqual(t.order_quantity, 0.875)
        self.assertAlmostEqual(t.order_notional, 1750.0)

    def test_short_trims_with_buy(self) -> None:
        trims = plan_position_trims(
            [_pos("BTC", "short", 0.1, 60000.0)],
            trim_multiple=15.0,
            trim_fraction=0.5,
            rung_usd=200.0,
            min_order_usd=5.0,
        )
        self.assertEqual(len(trims), 1)
        self.assertEqual(trims[0].order_side, "buy")
        self.assertAlmostEqual(trims[0].order_quantity, 0.05)

    def test_at_threshold_no_trim(self) -> None:
        trims = plan_position_trims(
            [_pos("ETH", "long", 1.5, 2000.0)],
            trim_multiple=15.0,
            trim_fraction=0.5,
            rung_usd=200.0,
            min_order_usd=5.0,
        )
        self.assertEqual(trims, [])

    def test_disabled_when_multiple_zero(self) -> None:
        trims = plan_position_trims(
            [_pos("ETH", "long", 10.0, 1000.0)],
            trim_multiple=0.0,
            trim_fraction=0.5,
            rung_usd=200.0,
        )
        self.assertEqual(trims, [])


if __name__ == "__main__":
    unittest.main()
