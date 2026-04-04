"""
Derive BTC/ETH regime stats from listingtabledata.json (no extra CoinGecko call).

Run listingtable.py first so listingtabledata.json is fresh; then marketstate.py writes
marketstate.json with the same shape as before (for median_filter / varibot).

Optional: MARKETSTATE_LISTING_JSON env overrides the input path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

OUTPUT_JSON_FILENAME: str = "marketstate.json"
DEFAULT_LISTING_JSON: str = "listingtabledata.json"

# Max(|BTC 24h%|, |ETH 24h%|) above this (percentage points) => Directional Now; else Sideways Now.
CHG_24H_PCT_LIMIT: float = 5.0

COINGECKO_IDS_ORDER: List[str] = ["bitcoin", "ethereum"]


def format_fetched_at_sgt_compact(when: Optional[datetime] = None) -> str:
    """e.g. '10:29am 4 Apr 2026 SGT' (same shape as listingtabledata.json fetched_at)."""
    dt = when if when is not None else datetime.now(ZoneInfo("Asia/Singapore"))
    h12 = dt.hour % 12 or 12
    ampm = dt.strftime("%p").lower()
    return f"{h12}:{dt.minute:02d}{ampm} {dt.day} {dt.strftime('%b')} {dt.year} SGT"


def _fmt_pct(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return None


def _fmt_signed_pct_terminal(pct_str: Optional[str]) -> str:
    """e.g. '0.36%' -> '+0.36%', '-0.12%' unchanged sign semantics."""
    if not pct_str:
        return "n/a"
    s = str(pct_str).replace("%", "").strip()
    try:
        return f"{float(s):+.2f}%"
    except Exception:
        return str(pct_str)


def _pct_str_to_float(pct_str: Optional[str]) -> Optional[float]:
    if not pct_str:
        return None
    s = str(pct_str).replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _btc_eth_market_state(btc_24: Optional[str], eth_24: Optional[str]) -> Dict[str, Any]:
    """Regime from max(|BTC 24h%|, |ETH 24h%|) vs CHG_24H_PCT_LIMIT (strict > limit => Directional)."""
    vals = [
        abs(x)
        for x in (_pct_str_to_float(btc_24), _pct_str_to_float(eth_24))
        if x is not None
    ]
    max_abs = max(vals) if vals else 0.0
    regime = "Directional Now" if max_abs > CHG_24H_PCT_LIMIT else "Sideways Now"
    return {
        "24h_market_regime": regime,
        "24hChg_pct_limit": CHG_24H_PCT_LIMIT,
        "max_abs_24h_pct_btc_eth": round(max_abs, 2) if vals else 0.0,
    }


def _market_cap_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except Exception:
        return None


def _load_listing_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("listings"), list):
        return [x for x in payload["listings"] if isinstance(x, dict)]
    return []


def _find_row_by_coingecko_id(rows: List[Dict[str, Any]], coingecko_id: str) -> Optional[Dict[str, Any]]:
    want = coingecko_id.strip().lower()
    for r in rows:
        cid = str(r.get("coingecko_id") or "").strip().lower()
        if cid == want:
            return r
    return None


def _find_row_by_var_ticker(rows: List[Dict[str, Any]], ticker: str) -> Optional[Dict[str, Any]]:
    want = ticker.strip().upper()
    for r in rows:
        t = str(r.get("vari_ticker") or r.get("ticker") or "").strip().upper()
        if t == want:
            return r
    return None


def _listing_row_to_output(r: Optional[Dict[str, Any]], *, coingecko_id: str) -> Dict[str, Any]:
    if not r:
        return {
            "coingecko_id": coingecko_id,
            "market_cap": None,
            "price_change_1h_pct": None,
            "price_change_24h_pct": None,
            "price_change_7d_pct": None,
        }
    return {
        "coingecko_id": str(r.get("coingecko_id") or coingecko_id),
        "market_cap": _market_cap_int(r.get("market_cap")),
        "price_change_1h_pct": _fmt_pct(r.get("price_change_1h_pct")),
        "price_change_24h_pct": _fmt_pct(r.get("price_change_24h_pct")),
        "price_change_7d_pct": _fmt_pct(r.get("price_change_7d_pct")),
    }


def default_listing_json_path() -> str:
    env = os.getenv("MARKETSTATE_LISTING_JSON", "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_LISTING_JSON)


def build_payload(*, listing_json_path: str) -> Dict[str, Any]:
    if not os.path.isfile(listing_json_path):
        raise FileNotFoundError(f"listing table JSON not found: {listing_json_path}")

    with open(listing_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = _load_listing_rows(data)
    listingtable_fetched_at: Optional[str] = None
    if isinstance(data, dict):
        v = data.get("fetched_at")
        if isinstance(v, str) and v.strip():
            listingtable_fetched_at = v.strip()

    btc_src = _find_row_by_coingecko_id(rows, "bitcoin") or _find_row_by_var_ticker(rows, "BTC")
    eth_src = _find_row_by_coingecko_id(rows, "ethereum") or _find_row_by_var_ticker(rows, "ETH")

    listings: List[Dict[str, Any]] = []
    for cid in COINGECKO_IDS_ORDER:
        src = btc_src if cid == "bitcoin" else eth_src
        listings.append(_listing_row_to_output(src, coingecko_id=cid))

    by_row = {
        str(r.get("coingecko_id") or "").lower(): r
        for r in listings
        if isinstance(r, dict) and r.get("coingecko_id")
    }
    btc_24 = (by_row.get("bitcoin") or {}).get("price_change_24h_pct")
    eth_24 = (by_row.get("ethereum") or {}).get("price_change_24h_pct")
    market_state = _btc_eth_market_state(btc_24, eth_24)

    dt_sgt = datetime.now(ZoneInfo("Asia/Singapore"))
    payload: Dict[str, Any] = {
        "fetched_at": format_fetched_at_sgt_compact(dt_sgt),
        "fetched_at_unix": float(dt_sgt.timestamp()),
        "data_source": "listingtabledata.json",
        "listingtable_json": os.path.abspath(listing_json_path),
        "market_state": market_state,
        "listings": listings,
    }
    if listingtable_fetched_at is not None:
        payload["listingtable_fetched_at"] = listingtable_fetched_at
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Write marketstate.json from BTC/ETH rows in listingtabledata.json (no CoinGecko)."
    )
    ap.add_argument(
        "--listing-json",
        default=default_listing_json_path(),
        help=f"Path to listingtabledata.json (default: beside this script or MARKETSTATE_LISTING_JSON).",
    )
    args = ap.parse_args()

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_JSON_FILENAME)
    try:
        payload = build_payload(listing_json_path=str(args.listing_json))
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from e
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in listing file: {e}", file=sys.stderr)
        raise SystemExit(2) from e

    listings = payload.get("listings") if isinstance(payload.get("listings"), list) else []
    by_id = {
        str(r.get("coingecko_id") or "").lower(): r
        for r in listings
        if isinstance(r, dict) and r.get("coingecko_id")
    }
    btc_24 = (by_id.get("bitcoin") or {}).get("price_change_24h_pct")
    eth_24 = (by_id.get("ethereum") or {}).get("price_change_24h_pct")
    ms = payload.get("market_state") if isinstance(payload.get("market_state"), dict) else {}
    regime = str(ms.get("24h_market_regime") or "Sideways Now")
    print(
        f"24hChg% BTC {_fmt_signed_pct_terminal(btc_24)} ETH {_fmt_signed_pct_terminal(eth_24)} | {regime} "
        f"(from listingtable)"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
