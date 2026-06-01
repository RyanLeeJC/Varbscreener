"""Tests for paired-limit venue pending snapshot / cleared-fill sync."""

from __future__ import annotations

import unittest

from strategy.gridstrat_rearm import (
    _fill_open_order_and_rearm,
    _new_order,
    apply_venue_cleared_limits_as_fills,
    format_fill_qty,
    record_venue_pending_snapshot,
)

try:
    from variationalbot.vari.endpoints import limit_price_key
except ImportError:
    from Varibot.variationalbot.vari.endpoints import limit_price_key  # type: ignore


class TestVenueClearedFillSync(unittest.TestCase):
    def _minimal_state(self) -> dict:
        tick = 0
        orders = [
            _new_order("o0", 99.0, "buy", "initial", None, tick),
            _new_order("o1", 101.0, "sell", "initial", None, tick),
        ]
        return {
            "schema_version": 3,
            "grid_num": 10,
            "orders": orders,
            "next_id": 2,
            "tick": tick,
            "current_anchor": 100.0,
            "current_grid_lower": 95.0,
            "current_grid_upper": 105.0,
            "spacing": 1.0,
            "qty_per_grid": 1.0,
            "inventory": 0.0,
            "inventory_cost": 0.0,
            "realized_pnl": 0.0,
            "volume_usd": 0.0,
            "reset_count": 0,
        }

    def test_never_posted_sell_not_treated_as_filled(self) -> None:
        state = self._minimal_state()
        orders = state["orders"]
        sell = next(o for o in orders if o["side"] == "sell" and o["status"] == "open")
        pending = {
            limit_price_key(str(o["side"]), float(o["level"]))
            for o in orders
            if o["side"] == "buy" and o["status"] == "open"
        }
        # Simulate prior cycle: only buys were on venue.
        record_venue_pending_snapshot(state, pending_keys=pending)
        logs = apply_venue_cleared_limits_as_fills(state, pending_keys=pending)
        self.assertEqual(logs, [])
        self.assertEqual(sell["status"], "open")

    def test_cleared_sell_fills_when_it_was_on_venue_last_cycle(self) -> None:
        state = self._minimal_state()
        orders = state["orders"]
        sell = next(o for o in orders if o["side"] == "sell" and o["status"] == "open")
        sell_key = limit_price_key("sell", float(sell["level"]))
        pending_with_sell = {
            limit_price_key(str(o["side"]), float(o["level"]))
            for o in orders
            if o["status"] == "open"
        }
        record_venue_pending_snapshot(state, pending_keys=pending_with_sell)
        pending_after_fill = pending_with_sell - {sell_key}
        logs = apply_venue_cleared_limits_as_fills(
            state, pending_keys=pending_after_fill
        )
        self.assertTrue(any("venue sync SELL filled" in ln for ln in logs))
        self.assertEqual(sell["status"], "filled")

    def test_empty_last_snapshot_skips_all_clears(self) -> None:
        state = self._minimal_state()
        pending = {("buy", "99.00")}
        logs = apply_venue_cleared_limits_as_fills(state, pending_keys=pending)
        self.assertEqual(logs, [])
        open_n = sum(1 for o in state["orders"] if o["status"] == "open")
        self.assertGreater(open_n, 0)

    def test_empty_pending_with_last_snapshot_fills_cleared(self) -> None:
        state = self._minimal_state()
        orders = state["orders"]
        buy = next(o for o in orders if o["side"] == "buy" and o["status"] == "open")
        pending_with_buy = {
            limit_price_key(str(o["side"]), float(o["level"]))
            for o in orders
            if o["status"] == "open"
        }
        record_venue_pending_snapshot(state, pending_keys=pending_with_buy)
        logs = apply_venue_cleared_limits_as_fills(state, pending_keys=set())
        self.assertTrue(any("venue sync BUY filled" in ln for ln in logs))
        self.assertEqual(buy["status"], "filled")

    def test_format_fill_qty_uses_asset_not_btc(self) -> None:
        state = self._minimal_state()
        state["asset"] = "JTO"
        state["qty_per_grid"] = 740.585
        self.assertEqual(format_fill_qty(state, 740.585), "qty 740.585 JTO")
        self.assertNotIn("BTC", format_fill_qty(state, 740.585))

    def test_fill_log_includes_ticker(self) -> None:
        state = self._minimal_state()
        state["asset"] = "JTO"
        sell = next(o for o in state["orders"] if o["side"] == "sell")
        logs: list[str] = []
        _fill_open_order_and_rearm(state, sell, tick=1, logs=logs)
        self.assertTrue(any("qty 1 JTO" in ln and "SELL filled" in ln for ln in logs))
        self.assertFalse(any("BTC" in ln for ln in logs))


if __name__ == "__main__":
    unittest.main()
