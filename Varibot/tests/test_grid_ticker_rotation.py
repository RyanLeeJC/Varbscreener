"""Unit tests for grid ticker rotation swap planning and roster file."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_VARIBOT = os.path.join(_REPO, "Varibot")
for p in (_REPO, _VARIBOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from grid_ticker_rotation import (  # noqa: E402
    FileRotationStore,
    build_swap_plan,
)
from strategy.gridstrat import (  # noqa: E402
    ENV_GRID_TRADING_ROSTER_PATH,
    grid_trading_ticker_band_pcts,
    grid_trading_ticker_band_pcts_from_static,
    load_grid_trading_roster,
    save_grid_trading_roster,
)


class TestBuildSwapPlan(unittest.TestCase):
    def test_fill_empty_slots_before_eviction(self) -> None:
        current = {"ENA": 3.0, "XLM": 2.0}
        hyper = [
            {"ticker": "HYPE", "best_band_pct": 2.5, "best_pnl": 50.0},
            {"ticker": "SOL", "best_band_pct": 2.0, "best_pnl": 40.0},
        ]
        plan = build_swap_plan(
            hyperparam_results=hyper,
            current_roster=current,
            live_pnl={"ENA": 10.0, "XLM": 5.0},
            roster_sz=4,
            max_swaps=10,
        )
        self.assertEqual(len(plan["roster_after"]), 4)
        self.assertIn("HYPE", plan["roster_after"])
        self.assertIn("SOL", plan["roster_after"])
        self.assertEqual(plan["remove"], [])

    def test_evict_bottom_by_live_pnl(self) -> None:
        current = {f"T{i}": 2.5 for i in range(4)}
        hyper = [
            {"ticker": "NEW1", "best_band_pct": 3.0, "best_pnl": 100.0},
            {"ticker": "NEW2", "best_band_pct": 2.0, "best_pnl": 90.0},
        ]
        live_pnl = {"T0": -50.0, "T1": -40.0, "T2": 10.0, "T3": 20.0}
        plan = build_swap_plan(
            hyperparam_results=hyper,
            current_roster=current,
            live_pnl=live_pnl,
            roster_sz=4,
            max_swaps=2,
        )
        self.assertIn("T0", plan["remove"])
        self.assertIn("T1", plan["remove"])
        self.assertIn("NEW1", plan["roster_after"])
        self.assertIn("NEW2", plan["roster_after"])

    def test_skips_paused_for_eviction(self) -> None:
        current = {"BAD": 2.5, "GOOD": 2.5}
        hyper = [{"ticker": "NEW", "best_band_pct": 2.0, "best_pnl": 50.0}]
        plan = build_swap_plan(
            hyperparam_results=hyper,
            current_roster=current,
            live_pnl={"BAD": -100.0, "GOOD": 0.0},
            roster_sz=2,
            max_swaps=1,
            paused_tickers={"BAD"},
        )
        self.assertNotIn("BAD", plan["remove"])
        self.assertIn("GOOD", plan["remove"])
        self.assertIn("NEW", plan["roster_after"])


class TestRosterFile(unittest.TestCase):
    def test_roster_overrides_static(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "roster.json")
            save_grid_trading_roster({"DOGE": 1.5, "ETH": 2.0}, path=path)
            old_env = os.environ.pop("GRID_TRADING_TICKERS", None)
            old_path = os.environ.get(ENV_GRID_TRADING_ROSTER_PATH)
            os.environ[ENV_GRID_TRADING_ROSTER_PATH] = path
            try:
                tickers = grid_trading_ticker_band_pcts()
                self.assertEqual(tickers, {"DOGE": 1.5, "ETH": 2.0})
                doc = load_grid_trading_roster(path=path)
                assert doc is not None
                self.assertEqual(doc["tickers"]["DOGE"], 1.5)
            finally:
                if old_env is not None:
                    os.environ["GRID_TRADING_TICKERS"] = old_env
                elif "GRID_TRADING_TICKERS" in os.environ:
                    del os.environ["GRID_TRADING_TICKERS"]
                if old_path is not None:
                    os.environ[ENV_GRID_TRADING_ROSTER_PATH] = old_path
                else:
                    os.environ.pop(ENV_GRID_TRADING_ROSTER_PATH, None)

    def test_env_override_beats_roster_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "roster.json")
            save_grid_trading_roster({"DOGE": 1.5}, path=path)
            os.environ[ENV_GRID_TRADING_ROSTER_PATH] = path
            os.environ["GRID_TRADING_TICKERS"] = "BTC:2"
            try:
                tickers = grid_trading_ticker_band_pcts()
                self.assertEqual(tickers, {"BTC": 2.0})
            finally:
                os.environ.pop(ENV_GRID_TRADING_ROSTER_PATH, None)
                os.environ.pop("GRID_TRADING_TICKERS", None)


class TestFileRotationStore(unittest.TestCase):
    def test_pending_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = FileRotationStore(path=os.path.join(td, "pending.json"))
            doc = {"schema": 1, "remove": ["A"], "add": [], "roster_after": {"B": 2.0}}
            store.write_pending(doc)
            self.assertTrue(store.has_pending())
            loaded = store.load_pending()
            assert loaded is not None
            self.assertEqual(loaded["remove"], ["A"])
            store.mark_applied(loaded)
            reapplied = store.load_pending()
            assert reapplied is not None
            self.assertIsNotNone(reapplied.get("applied_at"))


if __name__ == "__main__":
    unittest.main()
