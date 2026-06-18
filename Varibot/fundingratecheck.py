#!/usr/bin/env python3
"""Compare equity/stock funding rates across Vari, Ondo Perps, and Hyperliquid xyz.

Default tickers: HL xyz names that also list on Vari and/or Ondo Perps (27 symbols).
Use --triple-only for the 9 names on all three venues.

Examples:
  python3 fundingratecheck.py
  python3 fundingratecheck.py --triple-only
  python3 fundingratecheck.py --tickers INTC,NVDA,TSLA
  python3 fundingratecheck.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import requests

# HL xyz tickers that also list on Vari and/or Ondo Perps (excludes HL-only names).
DEFAULT_OVERLAP_TICKERS: tuple[str, ...] = (
    "AAPL",
    "AMD",
    "AMZN",
    "ARM",
    "CBRS",
    "COIN",
    "CRCL",
    "DRAM",
    "GOOGL",
    "HOOD",
    "INTC",
    "LITE",
    "META",
    "MRVL",
    "MSFT",
    "MSTR",
    "MU",
    "NBIS",
    "NFLX",
    "NVDA",
    "ORCL",
    "PLTR",
    "RKLB",
    "SNDK",
    "SPCX",
    "TSLA",
    "TSM",
)

# HL xyz ∩ Vari ∩ Ondo Perps
TRIPLE_OVERLAP_TICKERS: frozenset[str] = frozenset(
    {"AMD", "COIN", "CRCL", "INTC", "MSTR", "MU", "NVDA", "SPCX", "TSLA"}
)

ONDO_COMMODITY_INDEX: frozenset[str] = frozenset({"XAU", "XAG", "WTI", "US100", "US500"})

HL_XYZ_NON_STOCK: frozenset[str] = frozenset(
    {
        "GOLD",
        "SILVER",
        "CL",
        "COPPER",
        "NATGAS",
        "URANIUM",
        "ALUMINIUM",
        "PLATINUM",
        "PALLADIUM",
        "BRENTOIL",
        "WHEAT",
        "CORN",
        "TTF",
        "JPY",
        "EUR",
        "GBP",
        "KRW",
        "XYZ100",
        "SP500",
        "JP225",
        "KR200",
        "NIFTY",
        "IBOV",
        "DXY",
        "VIX",
        "VOL",
    }
)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
ONDO_MARK_PRICES_URL = "https://api.ondoperps.xyz/v1/perps/mark_prices"
ONDO_FUNDING_HISTORY_URL = "https://api.ondoperps.xyz/v1/perps/funding_rate_history"


@dataclass(frozen=True)
class VenueFunding:
    pct_8h: Optional[float]
    pct_24h: Optional[float]
    pct_ann: Optional[float]


@dataclass(frozen=True)
class FundingRow:
    ticker: str
    vari: VenueFunding
    ondo: VenueFunding
    hl: VenueFunding


def _funding_from_hourly_decimal(rate: float) -> VenueFunding:
    """Hourly funding as decimal fraction (e.g. 0.0000063 → 0.00063%/h). Used by HL and Ondo history."""
    hourly_pct = rate * 100.0
    return VenueFunding(
        pct_8h=hourly_pct * 8.0,
        pct_24h=hourly_pct * 24.0,
        pct_ann=hourly_pct * 24.0 * 365.0,
    )


def _funding_from_vari_rate(rate: float, interval_h: float) -> VenueFunding:
    """
    Vari ``supported_assets`` ``funding_rate`` is **annualized APR** (Omni UI "Ann. Funding").

    API stores APR as a decimal fraction: ``0.2123`` → **21.23%** Ann. Funding.
    Derive per-interval / 24h from the funding interval (equity RWA is usually 8h).
    """
    pct_ann = rate * 100.0
    periods_per_year = (24.0 / max(interval_h, 1e-9)) * 365.0
    pct_per_interval = pct_ann / periods_per_year
    pct_24h = pct_ann / 365.0
    return VenueFunding(
        pct_8h=pct_per_interval,
        pct_24h=pct_24h,
        pct_ann=pct_ann,
    )


def _empty_funding() -> VenueFunding:
    return VenueFunding(pct_8h=None, pct_24h=None, pct_ann=None)


def _parse_tickers(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]


def _resolve_tickers(*, args: argparse.Namespace) -> List[str]:
    if args.tickers:
        return sorted(set(_parse_tickers(args.tickers)))
    if args.triple_only:
        return sorted(TRIPLE_OVERLAP_TICKERS)
    if args.all_overlap:
        return list(DEFAULT_OVERLAP_TICKERS)
    return list(DEFAULT_OVERLAP_TICKERS)


def _vari_client():
    from dotenv import load_dotenv

    from variationalbot.config import load_config
    from variationalbot.vari import VariAuth, VariClient, VariEndpoints

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(env_path)
    cfg = load_config(env_path=env_path)
    client = VariClient(
        base_url=cfg.base_url,
        auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
    )
    return VariEndpoints(client)


def fetch_vari_equity_funding(tickers: Set[str]) -> Dict[str, VenueFunding]:
    ep = _vari_client()
    bulk = ep.get_supported_assets()
    out: Dict[str, VenueFunding] = {}
    for sym, rows in bulk.items():
        sym_u = str(sym).strip().upper()
        if sym_u not in tickers:
            continue
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            continue
        row = rows[0]
        if str(row.get("asset_class", "")).lower() != "equity":
            continue
        try:
            rate_pct = float(row["funding_rate"])
        except (KeyError, TypeError, ValueError):
            continue
        interval_s = int(row.get("funding_interval_s") or 3600)
        interval_h = max(interval_s / 3600.0, 1e-9)
        out[sym_u] = _funding_from_vari_rate(rate_pct, interval_h)
    return out


def fetch_ondo_market_bases() -> Dict[str, str]:
    """base ticker → market id (e.g. INTC → INTC-USD.P)."""
    resp = requests.get(ONDO_MARK_PRICES_URL, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise TypeError("Unexpected Ondo mark_prices response")
    out: Dict[str, str] = {}
    for market_id, row in result.items():
        if not isinstance(row, dict):
            continue
        pair = row.get("pair") if isinstance(row.get("pair"), dict) else {}
        base = str(pair.get("base", "")).strip().upper()
        if not base or base in ONDO_COMMODITY_INDEX:
            continue
        out[base] = str(market_id)
    return out


def _fetch_ondo_funding_one(market_id: str) -> VenueFunding:
    """
    Ondo UI "Last" = most recent settled rate from funding_rate_history.

    ``fundingRate`` is the hourly rate as a decimal fraction (×100 → %/hour).
    Do not use ``GET /v1/perps/funding_rates`` ``rate`` — that is the current-
    interval estimate ("Next" in the UI).
    """
    resp = requests.get(
        ONDO_FUNDING_HISTORY_URL,
        params={"market": market_id, "limit": 1},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or not body.get("success"):
        raise RuntimeError(f"Ondo funding_rate_history failed for {market_id}: {body!r}")
    rows = body.get("result")
    if not isinstance(rows, list) or not rows:
        return _empty_funding()
    row = rows[0]
    if not isinstance(row, dict):
        raise TypeError(f"Unexpected Ondo funding history row for {market_id}")
    return _funding_from_hourly_decimal(float(row["fundingRate"]))


def fetch_ondo_funding(tickers: Set[str]) -> Dict[str, VenueFunding]:
    markets = fetch_ondo_market_bases()
    targets = {sym: markets[sym] for sym in tickers if sym in markets}
    out: Dict[str, VenueFunding] = {}
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_ondo_funding_one, mid): sym for sym, mid in targets.items()}
        for fut in as_completed(futures):
            sym = futures[fut]
            out[sym] = fut.result()
    return out


@dataclass(frozen=True)
class HlMarketCtx:
    funding: VenueFunding
    open_interest_usd: float


def fetch_hl_xyz_markets(tickers: Set[str]) -> Dict[str, HlMarketCtx]:
    resp = requests.post(
        HL_INFO_URL,
        json={"type": "metaAndAssetCtxs", "dex": "xyz"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or len(payload) < 2:
        raise TypeError("Unexpected Hyperliquid metaAndAssetCtxs response")
    universe = payload[0].get("universe") if isinstance(payload[0], dict) else None
    ctxs = payload[1]
    if not isinstance(universe, list) or not isinstance(ctxs, list):
        raise TypeError("Unexpected Hyperliquid universe/context shape")

    out: Dict[str, HlMarketCtx] = {}
    for meta, ctx in zip(universe, ctxs):
        if not isinstance(meta, dict) or not isinstance(ctx, dict):
            continue
        if meta.get("isDelisted"):
            continue
        sym = str(meta.get("name", "")).replace("xyz:", "").strip().upper()
        if not sym or sym in HL_XYZ_NON_STOCK or sym not in tickers:
            continue
        try:
            oi = float(ctx.get("openInterest") or 0.0)
        except (TypeError, ValueError):
            oi = 0.0
        out[sym] = HlMarketCtx(
            funding=_funding_from_hourly_decimal(float(ctx["funding"])),
            open_interest_usd=oi,
        )
    return out


def fetch_hl_xyz_funding(tickers: Set[str]) -> Dict[str, VenueFunding]:
    return {sym: ctx.funding for sym, ctx in fetch_hl_xyz_markets(tickers).items()}


def _arb_sides(rates: Dict[str, Optional[float]]) -> Dict[str, Optional[str]]:
    """SELL highest 8h funding, BUY lowest — delta-neutral max-funding capture."""
    present = {k: v for k, v in rates.items() if v is not None}
    if len(present) < 2:
        return {k: None for k in rates}
    hi = max(present, key=present.get)  # type: ignore[arg-type]
    lo = min(present, key=present.get)  # type: ignore[arg-type]
    out = {k: None for k in rates}
    out[hi] = "sell"
    out[lo] = "buy"
    return out


def _venue_payload(v: VenueFunding, side: Optional[str]) -> Dict[str, Any]:
    return {
        "pct8h": v.pct_8h,
        "pct24h": v.pct_24h,
        "pctAnn": v.pct_ann,
        "side": side,
    }


def build_screener_payload(rows: Sequence[FundingRow], *, hl_ctx: Dict[str, HlMarketCtx]) -> Dict[str, Any]:
    from datetime import datetime, timezone

    oi_sorted = sorted(
        ((sym, ctx.open_interest_usd) for sym, ctx in hl_ctx.items()),
        key=lambda x: x[1],
        reverse=True,
    )
    oi_rank = {sym: idx + 1 for idx, (sym, _) in enumerate(oi_sorted)}

    screener_rows: List[Dict[str, Any]] = []
    for row in rows:
        rates = {
            "ondo": row.ondo.pct_8h,
            "hl": row.hl.pct_8h,
            "vari": row.vari.pct_8h,
        }
        present_vals = [v for v in rates.values() if v is not None]
        max_arb = (max(present_vals) - min(present_vals)) if len(present_vals) >= 2 else None
        sides = _arb_sides(rates)
        screener_rows.append(
            {
                "symbol": row.ticker,
                "oiRank": oi_rank.get(row.ticker),
                "maxArb": max_arb,
                "venues": {
                    "ondo": _venue_payload(row.ondo, sides["ondo"]),
                    "hl": _venue_payload(row.hl, sides["hl"]),
                    "vari": _venue_payload(row.vari, sides["vari"]),
                },
            }
        )

    screener_rows.sort(
        key=lambda r: (r["maxArb"] is None, -(r["maxArb"] or 0.0), r["symbol"]),
    )
    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "rows": screener_rows,
    }


def write_screener_data_js(path: str, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2)
    text = f"window.__FUNDING_SCREENER__ = {body};\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def build_rows(tickers: Sequence[str]) -> List[FundingRow]:
    tick_set = {t.upper() for t in tickers}
    vari = fetch_vari_equity_funding(tick_set)
    ondo = fetch_ondo_funding(tick_set)
    hl = fetch_hl_xyz_funding(tick_set)

    rows: List[FundingRow] = []
    for sym in sorted(tick_set):
        rows.append(
            FundingRow(
                ticker=sym,
                vari=vari[sym] if sym in vari else _empty_funding(),
                ondo=ondo[sym] if sym in ondo else _empty_funding(),
                hl=hl[sym] if sym in hl else _empty_funding(),
            )
        )
    return rows


def _fmt_pct(v: Optional[float], *, decimals: int = 4) -> str:
    if v is None:
        return "-"
    return f"{v:.{decimals}f}%"


def _venue_cells(v: VenueFunding) -> List[str]:
    return [
        _fmt_pct(v.pct_8h, decimals=5),
        _fmt_pct(v.pct_24h, decimals=4),
        _fmt_pct(v.pct_ann, decimals=2),
    ]


def print_table(rows: Iterable[FundingRow]) -> None:
    headers = (
        "Ticker",
        "V_8h",
        "V_24h",
        "V_Ann",
        "O_8h",
        "O_24h",
        "O_Ann",
        "H_8h",
        "H_24h",
        "H_Ann",
    )
    print("\t".join(headers))
    for row in rows:
        print("\t".join([row.ticker, *_venue_cells(row.vari), *_venue_cells(row.ondo), *_venue_cells(row.hl)]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Equity funding rate check: Vari, Ondo Perps, HL xyz.")
    ap.add_argument(
        "--tickers",
        help="Comma-separated tickers (default: 27-name HL∩(Vari∪Ondo) overlap list).",
    )
    ap.add_argument(
        "--triple-only",
        action="store_true",
        help="Only the 9 tickers listed on all three venues.",
    )
    ap.add_argument(
        "--all-overlap",
        action="store_true",
        help="Same as default: full 27-name overlap list.",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a TSV table.")
    ap.add_argument(
        "--write-screener-data",
        metavar="PATH",
        help="Write funding_screener.data.js payload (for funding_screener.html).",
    )
    args = ap.parse_args(argv)

    tickers = _resolve_tickers(args=args)
    tick_set = {t.upper() for t in tickers}
    rows = build_rows(tickers)

    if args.write_screener_data:
        hl_ctx = fetch_hl_xyz_markets(tick_set)
        payload = build_screener_payload(rows, hl_ctx=hl_ctx)
        write_screener_data_js(args.write_screener_data, payload)
        print(f"Wrote screener data → {args.write_screener_data}", file=sys.stderr)

    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    elif not args.write_screener_data:
        print_table(rows)
        print(
            f"\n{len(tickers)} tickers | "
            f"Vari={sum(1 for r in rows if r.vari.pct_8h is not None)} | "
            f"Ondo={sum(1 for r in rows if r.ondo.pct_8h is not None)} | "
            f"HL={sum(1 for r in rows if r.hl.pct_8h is not None)} | "
            f"rates in %",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
