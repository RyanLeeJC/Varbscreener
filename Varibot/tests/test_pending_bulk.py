"""Unit tests for bulk pending limit bucketing (no network)."""

from __future__ import annotations

import os
import sys
import unittest

_VARIBOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from pending_orders import (  # noqa: E402
    bulk_pending_fetch_enabled,
    pending_limit_keys_by_asset_from_rows,
)


def _row(*, underlying: str, side: str, price: str) -> dict:
    return {
        "instrument": {"underlying": underlying},
        "side": side,
        "order_type": "limit",
        "status": "pending",
        "limit_price": price,
    }


class TestPendingBulk(unittest.TestCase):
    def test_buckets_by_underlying(self) -> None:
        rows = [
            _row(underlying="ETH", side="buy", price="2100.0"),
            _row(underlying="ETH", side="sell", price="2110.0"),
            _row(underlying="XAU", side="buy", price="4500.0"),
            _row(underlying="BTC", side="buy", price="90000.0"),
        ]
        by = pending_limit_keys_by_asset_from_rows(rows, ["ETH", "XAU"])
        self.assertEqual(by["ETH"], {("buy", "2100.00"), ("sell", "2110.00")})
        self.assertEqual(by["XAU"], {("buy", "4500.00")})
        self.assertNotIn("BTC", by)

    def test_empty_assets_get_empty_sets(self) -> None:
        by = pending_limit_keys_by_asset_from_rows([], ["CL", "HYPE"])
        self.assertEqual(by, {"CL": set(), "HYPE": set()})

    def test_bulk_enabled_by_default(self) -> None:
        old = os.environ.pop("VARIBOT_PENDING_BULK", None)
        try:
            self.assertTrue(bulk_pending_fetch_enabled())
        finally:
            if old is not None:
                os.environ["VARIBOT_PENDING_BULK"] = old

    def test_bulk_disabled_env(self) -> None:
        os.environ["VARIBOT_PENDING_BULK"] = "0"
        try:
            self.assertFalse(bulk_pending_fetch_enabled())
        finally:
            os.environ.pop("VARIBOT_PENDING_BULK", None)


if __name__ == "__main__":
    unittest.main()
