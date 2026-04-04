"""
Fetch BTC and ETH market stats from CoinGecko only (no Vari API) and write marketstate.json.
Pattern matches listingtable.py CoinGecko usage.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

COINGECKO_BASE_URL: str = "https://api.coingecko.com/api/v3"
COINGECKO_COINS_MARKETS_PATH: str = "/coins/markets"
VS_CURRENCY: str = "usd"
PRICE_CHANGE_WINDOWS: str = "1h,24h,7d"
COINGECKO_MARKET_CAP_ORDER: str = "market_cap_desc"
COINGECKO_MARKETS_PER_PAGE: int = 100
COINGECKO_API_KEY_ENV: str = "COINGECKO_API_KEY"
COINGECKO_MIN_SECONDS_BETWEEN_CALLS: float = 2.0

OUTPUT_JSON_FILENAME: str = "marketstate.json"

# Max(|BTC 24h%|, |ETH 24h%|) above this (percentage points) => Directional Now; else Sideways Now.
CHG_24H_PCT_LIMIT: float = 5.0

# Fixed universe: CoinGecko ids, output order BTC then ETH.
COINGECKO_IDS: List[str] = ["bitcoin", "ethereum"]


def format_fetched_at_sgt_compact(when: Optional[datetime] = None) -> str:
    """e.g. '10:29am 4 Apr 2026 SGT' (same shape as listingtable.json fetched_at)."""
    dt = when if when is not None else datetime.now(ZoneInfo("Asia/Singapore"))
    h12 = dt.hour % 12 or 12
    ampm = dt.strftime("%p").lower()
    return f"{h12}:{dt.minute:02d}{ampm} {dt.day} {dt.strftime('%b')} {dt.year} SGT"


def get_coingecko_headers() -> Dict[str, str]:
    api_key = os.getenv(COINGECKO_API_KEY_ENV)
    headers: Dict[str, str] = {}
    if api_key:
        headers["x-cg-pro-api-key"] = api_key
    return headers


def _request_json_with_retries(
    url: str,
    params: Dict[str, Any],
    headers: Dict[str, str],
    retries: int = 6,
) -> Any:
    debug = os.getenv("COINGECKO_RETRY_DEBUG", "").strip().lower() in ("1", "true", "yes", "y", "on")
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            last_status = resp.status_code
            if debug:
                retry_after_hdr = resp.headers.get("Retry-After")
                rl_headers = {
                    k: v
                    for k, v in resp.headers.items()
                    if isinstance(k, str) and k.lower().startswith("x-ratelimit")
                }
                rl_part = f", x-ratelimit={rl_headers}" if rl_headers else ""
                print(
                    f"[CoinGecko resp] status={resp.status_code}, retry-after={retry_after_hdr}{rl_part}",
                    file=sys.stderr,
                )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait_s = float(retry_after)
                    except Exception:
                        wait_s = 5.0 + attempt * 2.0
                else:
                    wait_s = 5.0 + attempt * 2.0
                if debug:
                    print(
                        f"[CoinGecko retry] 429 rate limited (attempt {attempt+1}/{retries}); "
                        f"sleeping {max(wait_s, COINGECKO_MIN_SECONDS_BETWEEN_CALLS):.2f}s",
                        file=sys.stderr,
                    )
                time.sleep(max(wait_s, COINGECKO_MIN_SECONDS_BETWEEN_CALLS))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            wait_s = max(COINGECKO_MIN_SECONDS_BETWEEN_CALLS, 1.0 + attempt * 1.0)
            if debug:
                print(
                    f"[CoinGecko retry] error {type(e).__name__} (attempt {attempt+1}/{retries}); "
                    f"sleeping {wait_s:.2f}s; msg={str(e)[:180]}",
                    file=sys.stderr,
                )
            time.sleep(wait_s)
    if last_exc is not None:
        raise RuntimeError(
            f"CoinGecko request failed after {retries} retries"
            f" (last_status={last_status}, params={params})"
        ) from last_exc
    raise RuntimeError(
        f"CoinGecko request failed after {retries} retries"
        f" (last_status={last_status}, params={params})"
    )


def fetch_coingecko_markets_for_ids(ids: List[str]) -> List[Dict[str, Any]]:
    headers = get_coingecko_headers()
    normalized = sorted({i.strip().lower() for i in ids if i and i.strip()})
    params: Dict[str, Any] = {
        "vs_currency": VS_CURRENCY,
        "ids": ",".join(normalized),
        "order": COINGECKO_MARKET_CAP_ORDER,
        "per_page": COINGECKO_MARKETS_PER_PAGE,
        "price_change_percentage": PRICE_CHANGE_WINDOWS,
    }
    coins = _request_json_with_retries(
        url=f"{COINGECKO_BASE_URL}{COINGECKO_COINS_MARKETS_PATH}",
        params=params,
        headers=headers,
    )
    return list(coins or [])


def _get_price_change_pct(coin: Dict[str, Any], window: str) -> Any:
    key = f"price_change_percentage_{window}_in_currency"
    return coin.get(key)


def _fmt_pct(value: Any) -> Optional[str]:
    if value is None:
        return None
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


def build_payload() -> Dict[str, Any]:
    coins = fetch_coingecko_markets_for_ids(COINGECKO_IDS)
    by_id = {(c.get("id") or "").lower(): c for c in coins if isinstance(c, dict)}

    listings: List[Dict[str, Any]] = []
    for cid in COINGECKO_IDS:
        coin = by_id.get(cid.lower())
        if not coin:
            listings.append(
                {
                    "coingecko_id": cid,
                    "market_cap": None,
                    "price_change_1h_pct": None,
                    "price_change_24h_pct": None,
                    "price_change_7d_pct": None,
                }
            )
            continue
        listings.append(
            {
                "coingecko_id": coin.get("id"),
                "market_cap": _market_cap_int(coin.get("market_cap")),
                "price_change_1h_pct": _fmt_pct(_get_price_change_pct(coin, "1h")),
                "price_change_24h_pct": _fmt_pct(_get_price_change_pct(coin, "24h")),
                "price_change_7d_pct": _fmt_pct(_get_price_change_pct(coin, "7d")),
            }
        )

    by_row = {
        str(r.get("coingecko_id") or "").lower(): r
        for r in listings
        if isinstance(r, dict) and r.get("coingecko_id")
    }
    btc_24 = (by_row.get("bitcoin") or {}).get("price_change_24h_pct")
    eth_24 = (by_row.get("ethereum") or {}).get("price_change_24h_pct")
    market_state = _btc_eth_market_state(btc_24, eth_24)

    dt_sgt = datetime.now(ZoneInfo("Asia/Singapore"))
    return {
        "fetched_at": format_fetched_at_sgt_compact(dt_sgt),
        "fetched_at_unix": float(dt_sgt.timestamp()),
        "market_state": market_state,
        "listings": listings,
    }


def main() -> None:
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_JSON_FILENAME)
    payload = build_payload()
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
        f"24hChg% BTC {_fmt_signed_pct_terminal(btc_24)} ETH {_fmt_signed_pct_terminal(eth_24)} | {regime}"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
