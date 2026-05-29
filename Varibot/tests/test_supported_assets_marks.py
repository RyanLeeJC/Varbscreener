"""Unit tests for supported_assets mark parsing (no network)."""

from __future__ import annotations

import os
import sys
import unittest

_VARIBOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

import varibot as vb  # noqa: E402


class TestSupportedAssetsMarks(unittest.TestCase):
    def test_price_preferred_over_index(self) -> None:
        entry = [{"asset": "ETH", "index_price": "2100.5", "price": "2101.0"}]
        self.assertEqual(vb.mark_price_from_supported_asset_entry(entry), 2101.0)

    def test_price_only(self) -> None:
        entry = [{"asset": "CL", "price": "99.5"}]
        self.assertEqual(vb.mark_price_from_supported_asset_entry(entry), 99.5)

    def test_index_price_fallback(self) -> None:
        entry = [{"asset": "XPD", "index_price": "1376.0"}]
        self.assertEqual(vb.mark_price_from_supported_asset_entry(entry), 1376.0)

    def test_use_bulk_default(self) -> None:
        old = os.environ.pop("VARIBOT_MARKS_SOURCE", None)
        try:
            self.assertTrue(vb._use_bulk_supported_assets_marks())
        finally:
            if old is not None:
                os.environ["VARIBOT_MARKS_SOURCE"] = old

    def test_indicative_mode(self) -> None:
        os.environ["VARIBOT_MARKS_SOURCE"] = "indicative"
        try:
            self.assertFalse(vb._use_bulk_supported_assets_marks())
        finally:
            os.environ.pop("VARIBOT_MARKS_SOURCE", None)


if __name__ == "__main__":
    unittest.main()
