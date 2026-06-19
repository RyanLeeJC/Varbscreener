"""Unit tests for paginated pending order fetch (no network)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

_VARIBOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from pending_orders import fetch_pending_order_rows_paginated  # noqa: E402


def _pending_page(rows: list, *, offset: int, limit: int, has_more: bool) -> dict:
    pag = None
    if has_more:
        pag = {"next_page": {"offset": offset + limit}}
    return {"result": rows, "pagination": pag}


class TestFetchPendingPaginated(unittest.TestCase):
    def test_follows_next_page_until_short_page(self) -> None:
        ep = MagicMock()
        ep.get_orders_v2_query.side_effect = [
            _pending_page(
                [
                    {"rfq_id": "a", "order_type": "limit", "status": "pending"},
                    {"rfq_id": "b", "order_type": "limit", "status": "pending"},
                ],
                offset=0,
                limit=2,
                has_more=True,
            ),
            _pending_page(
                [{"rfq_id": "c", "order_type": "limit", "status": "pending"}],
                offset=2,
                limit=2,
                has_more=False,
            ),
        ]
        rows, hit_cap = fetch_pending_order_rows_paginated(
            ep, page_limit=2, max_pages=10
        )
        self.assertEqual(len(rows), 3)
        self.assertFalse(hit_cap)
        self.assertEqual(ep.get_orders_v2_query.call_count, 2)
        second_params = ep.get_orders_v2_query.call_args_list[1][0][0]
        self.assertEqual(second_params["offset"], "2")

    def test_hit_cap_when_max_pages_exhausted(self) -> None:
        ep = MagicMock()
        ep.get_orders_v2_query.return_value = _pending_page(
            [{"rfq_id": "x", "order_type": "limit", "status": "pending"}],
            offset=0,
            limit=1,
            has_more=True,
        )
        rows, hit_cap = fetch_pending_order_rows_paginated(
            ep, page_limit=1, max_pages=1
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(hit_cap)


if __name__ == "__main__":
    unittest.main()
