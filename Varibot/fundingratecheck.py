#!/usr/bin/env python3
"""Compare equity/stock funding rates across Vari, Ondo Perps, Hyperliquid xyz, and Lighter.

Default tickers: HL xyz names that also list on Vari and/or Ondo Perps (27 symbols).
Use --triple-only for the 9 names on all three venues (Vari, Ondo, HL).

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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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

# Ondo per-market max leverage — https://docs.ondoperps.xyz/leverage.md
ONDO_MAX_LEV_20: frozenset[str] = frozenset({"US100", "US500", "XAG", "XAU", "WTI", "QQQ", "AAPL"})
ONDO_MAX_LEV_10: frozenset[str] = frozenset(
    {
        "DRAM",
        "AMD",
        "AMZN",
        "COIN",
        "CRCL",
        "GOOGL",
        "HOOD",
        "INTC",
        "META",
        "MSFT",
        "MSTR",
        "NFLX",
        "NVDA",
        "ORCL",
        "PLTR",
        "TSLA",
    }
)

# Vari max leverage from Omni UI (TradFi / Equities, snapshot 2026-06-19). All 20×.
# Symbols not listed are not on Vari; screener omits Vari max lev for those rows.
VARI_MAX_LEV: Dict[str, int] = {
    "AAOI": 20,
    "AMD": 20,
    "ANTHROPIC": 20,
    "ARM": 20,
    "CBRS": 20,
    "COIN": 20,
    "CRCL": 20,
    "DRAM": 20,
    "INTC": 20,
    "LITE": 20,
    "MRVL": 20,
    "MSTR": 20,
    "MU": 20,
    "NBIS": 20,
    "NVDA": 20,
    "OPENAI": 20,
    "QCOM": 20,
    "QNTX": 20,
    "RKLB": 20,
    "SNDK": 20,
    "SPCX": 20,
    "TSLA": 20,
    "TSM": 20,
}

LIGHTER_ORDER_BOOK_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails"

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
ONDO_CONTRACTS_URL = "https://api.ondoperps.xyz/v1/perps/contracts"
ONDO_FUNDING_RATES_URL = "https://api.ondoperps.xyz/v1/perps/funding_rates"

LIGHTER_FUNDING_RATES_URL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"
LIGHTER_FUNDING_INTERVAL_H = 8.0
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart"
YAHOO_UA = "Mozilla/5.0"


class _FetchTimer:
    """Collect per-step durations for screener fetch breakdown."""

    def __init__(self) -> None:
        self.steps: List[tuple[str, float]] = []

    def run(self, label: str, fn: Any) -> Any:
        t0 = time.perf_counter()
        out = fn()
        self.steps.append((label, time.perf_counter() - t0))
        return out

    def emit(self, *, file: Any = None) -> None:
        out = file or sys.stderr
        if not self.steps:
            return
        total = sum(secs for _, secs in self.steps)
        width = max(len(label) for label, _ in self.steps)
        print("\nScreener fetch timing:", file=out)
        for label, secs in self.steps:
            pct = (secs / total * 100.0) if total else 0.0
            print(f"  {label:<{width}}  {secs:6.2f}s  ({pct:4.0f}%)", file=out)
        print(f"  {'TOTAL':<{width}}  {total:6.2f}s", file=out)


@dataclass(frozen=True)
class VenueFunding:
    pct_8h: Optional[float]
    pct_24h: Optional[float]
    pct_ann: Optional[float]


@dataclass(frozen=True)
class OndoFunding:
    """Ondo ``/v1/perps/contracts``: ``fundingRate`` (Last) and ``nextFundingRate`` (Next)."""

    current: VenueFunding
    next: VenueFunding


@dataclass(frozen=True)
class FundingRow:
    ticker: str
    vari: VenueFunding
    ondo: OndoFunding
    hl: VenueFunding
    lighter: VenueFunding


def _funding_from_hourly_decimal(rate: float) -> VenueFunding:
    """Hourly funding as decimal fraction (e.g. 0.0000063 → 0.00063%/h). Used by HL and Ondo history."""
    hourly_pct = rate * 100.0
    return VenueFunding(
        pct_8h=hourly_pct * 8.0,
        pct_24h=hourly_pct * 24.0,
        pct_ann=hourly_pct * 24.0 * 365.0,
    )


def _funding_from_interval_decimal(rate: float, interval_h: float) -> VenueFunding:
    """Per-interval rate as decimal fraction; scale to 8h / 24h / Ann from interval length."""
    interval_pct = rate * 100.0
    hourly_pct = interval_pct / max(interval_h, 1e-9)
    return VenueFunding(
        pct_8h=hourly_pct * 8.0,
        pct_24h=hourly_pct * 24.0,
        pct_ann=hourly_pct * 24.0 * 365.0,
    )


def _funding_from_lighter_rate(rate: float) -> VenueFunding:
    """
    Lighter ``GET /api/v1/funding-rates`` (``exchange: lighter``) ``rate`` is per 8h period
    as a decimal fraction — same shape as Ondo/HL when mapped to 8h %.
    """
    return _funding_from_interval_decimal(rate, LIGHTER_FUNDING_INTERVAL_H)


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


def _empty_ondo_funding() -> OndoFunding:
    empty = _empty_funding()
    return OndoFunding(current=empty, next=empty)


def _funding_from_ondo_hourly_raw(raw: Any) -> Optional[VenueFunding]:
    if raw is None:
        return None
    try:
        return _funding_from_hourly_decimal(float(raw))
    except (TypeError, ValueError):
        return None


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


def _fetch_ondo_mark_prices_result() -> Dict[str, Any]:
    resp = requests.get(ONDO_MARK_PRICES_URL, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise TypeError("Unexpected Ondo mark_prices response")
    return result


def _fetch_ondo_contracts() -> List[Dict[str, Any]]:
    resp = requests.get(ONDO_CONTRACTS_URL, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    rows = body.get("result") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise TypeError("Unexpected Ondo contracts response")
    return [row for row in rows if isinstance(row, dict)]


def _ondo_equity_contract_rows() -> List[Dict[str, Any]]:
    """Enabled equity/ETF contracts (excludes commodities/indices)."""
    out: List[Dict[str, Any]] = []
    for row in _fetch_ondo_contracts():
        if row.get("disabled"):
            continue
        base = str(row.get("baseCurrency", "")).strip().upper()
        if not base or base in ONDO_COMMODITY_INDEX:
            continue
        out.append(row)
    return out


def _fetch_ondo_enabled_bases() -> frozenset[str]:
    return frozenset(str(row.get("baseCurrency", "")).strip().upper() for row in _ondo_equity_contract_rows())


def fetch_ondo_market_bases() -> Dict[str, str]:
    """base ticker → market id (e.g. INTC → INTC-USD.P); enabled contracts only."""
    out: Dict[str, str] = {}
    for row in _ondo_equity_contract_rows():
        base = str(row.get("baseCurrency", "")).strip().upper()
        market_id = str(row.get("market", "")).strip()
        if base and market_id:
            out[base] = market_id
    return out


def fetch_ondo_prices(tickers: Set[str]) -> Dict[str, float]:
    """base ticker → Ondo mark price (USD); enabled contracts only."""
    enabled = _fetch_ondo_enabled_bases()
    out: Dict[str, float] = {}
    for row in _fetch_ondo_mark_prices_result().values():
        if not isinstance(row, dict):
            continue
        pair = row.get("pair") if isinstance(row.get("pair"), dict) else {}
        base = str(pair.get("base", "")).strip().upper()
        if not base or base in ONDO_COMMODITY_INDEX or base not in enabled or base not in tickers:
            continue
        raw = row.get("markPrice", row.get("price"))
        try:
            out[base] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def fetch_ondo_funding(tickers: Set[str]) -> Dict[str, OndoFunding]:
    """
    Ondo ``GET /v1/perps/contracts`` — both rate fields in one response:

    - ``fundingRate`` — Last (settled / current interval baseline)
    - ``nextFundingRate`` — Next (in-progress estimate; same as ``funding_rates.rate``)
    """
    out: Dict[str, OndoFunding] = {}
    for row in _ondo_equity_contract_rows():
        base = str(row.get("baseCurrency", "")).strip().upper()
        if not base or base not in tickers:
            continue
        current = _funding_from_ondo_hourly_raw(row.get("fundingRate"))
        nxt = _funding_from_ondo_hourly_raw(row.get("nextFundingRate"))
        if current is None and nxt is None:
            continue
        out[base] = OndoFunding(
            current=current or _empty_funding(),
            next=nxt or _empty_funding(),
        )
    return out


def ondo_max_lev(ticker: str) -> int:
    sym = str(ticker).strip().upper()
    if sym in ONDO_MAX_LEV_20:
        return 20
    if sym in ONDO_MAX_LEV_10:
        return 10
    return 10


def _leverage_from_lighter_imf(imf: Any) -> Optional[int]:
    try:
        bps = int(imf)
    except (TypeError, ValueError):
        return None
    if bps <= 0:
        return None
    return 10_000 // bps


def fetch_lighter_max_lev(tickers: Set[str]) -> Dict[str, int]:
    resp = requests.get(LIGHTER_ORDER_BOOK_URL, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise TypeError("Unexpected Lighter orderBookDetails response")
    books = body.get("order_book_details")
    if not isinstance(books, list):
        raise TypeError("Unexpected Lighter order_book_details shape")

    out: Dict[str, int] = {}
    for row in books:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym or sym not in tickers or sym in out:
            continue
        lev = _leverage_from_lighter_imf(row.get("min_initial_margin_fraction"))
        if lev is not None:
            out[sym] = lev
    return out


def _fetch_vari_max_lev_live(tickers: Set[str]) -> Dict[str, int]:
    """Live ``set_leverage`` probe per ticker (slow; maintainer refresh only)."""
    try:
        ep = _vari_client()
    except Exception:
        return {}

    out: Dict[str, int] = {}
    for sym in sorted(tickers):
        try:
            res = ep.set_leverage(asset=sym, leverage=20)
            out[sym] = int(res.max)
        except Exception:
            continue
    return out


def _print_vari_max_lev_dict(levs: Dict[str, int]) -> None:
    print("\n# Paste into fundingratecheck.py VARI_MAX_LEV:", file=sys.stderr)
    for sym in sorted(levs):
        print(f'    "{sym}": {levs[sym]},', file=sys.stderr)


def fetch_vari_max_lev(tickers: Set[str], *, force_refresh: bool = False) -> Dict[str, int]:
    if force_refresh:
        live = _fetch_vari_max_lev_live(tickers)
        if live:
            _print_vari_max_lev_dict(live)
        return live
    return {sym: VARI_MAX_LEV[sym] for sym in tickers if sym in VARI_MAX_LEV}


def fetch_max_lev_maps(tickers: Set[str], *, refresh_vari_max_lev: bool = False) -> Dict[str, Dict[str, int]]:
    ondo_markets = fetch_ondo_market_bases()
    hl_markets = fetch_hl_xyz_markets(tickers)
    lighter = fetch_lighter_max_lev(tickers)
    vari = fetch_vari_max_lev(tickers, force_refresh=refresh_vari_max_lev)

    ondo = {sym: ondo_max_lev(sym) for sym in tickers if sym in ondo_markets}
    hl = {sym: ctx.max_lev for sym, ctx in hl_markets.items() if ctx.max_lev is not None}
    return {"ondo": ondo, "hl": hl, "lighter": lighter, "vari": vari}


@dataclass(frozen=True)
class HlMarketCtx:
    funding: VenueFunding
    open_interest_usd: float
    max_lev: Optional[int] = None


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
        max_lev: Optional[int] = None
        if meta.get("maxLeverage") is not None:
            try:
                max_lev = int(meta["maxLeverage"])
            except (TypeError, ValueError):
                max_lev = None
        out[sym] = HlMarketCtx(
            funding=_funding_from_hourly_decimal(float(ctx["funding"])),
            open_interest_usd=oi,
            max_lev=max_lev,
        )
    return out


def fetch_hl_xyz_funding(tickers: Set[str]) -> Dict[str, VenueFunding]:
    return {sym: ctx.funding for sym, ctx in fetch_hl_xyz_markets(tickers).items()}


def fetch_lighter_funding(tickers: Set[str]) -> Dict[str, VenueFunding]:
    """
    Lighter public funding rates — filter ``exchange == "lighter"`` from bulk endpoint.

    Docs: https://github.com/elliottech/lighter-python (``FundingApi.funding_rates``).
    """
    resp = requests.get(LIGHTER_FUNDING_RATES_URL, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise TypeError("Unexpected Lighter funding-rates response")
    rows = body.get("funding_rates")
    if not isinstance(rows, list):
        raise TypeError("Unexpected Lighter funding_rates shape")

    out: Dict[str, VenueFunding] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("exchange", "")).lower() != "lighter":
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym or sym not in tickers or sym in out:
            continue
        try:
            out[sym] = _funding_from_lighter_rate(float(row["rate"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


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


def _yahoo_5d_bars(ticker: str) -> Optional[List[tuple[float, float, float]]]:
    """Last 5 daily bars as (high, low, close) from Yahoo chart API."""
    resp = requests.get(
        f"{YAHOO_CHART_URL}/{ticker}",
        params={"range": "5d", "interval": "1d"},
        headers={"User-Agent": YAHOO_UA},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        return None
    results = body.get("chart", {}).get("result")
    if not results:
        return None
    q = results[0].get("indicators", {}).get("quote", [{}])[0]
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    bars: List[tuple[float, float, float]] = []
    for h, l, c in zip(highs, lows, closes):
        if h is None or l is None or c is None:
            continue
        bars.append((float(h), float(l), float(c)))
    return bars or None


def _atr5d_pct(bars: Sequence[tuple[float, float, float]], ref_price: float) -> Optional[float]:
    if len(bars) < 1 or ref_price <= 0:
        return None
    trs: List[float] = []
    for i, (h, l, _c) in enumerate(bars):
        if i == 0:
            tr = h - l
        else:
            prev_c = bars[i - 1][2]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return (sum(trs) / len(trs)) / ref_price * 100.0


def _lev_from_pct(pct: Optional[float]) -> Optional[int]:
    if pct is None or pct <= 0:
        return None
    return max(1, int(100.0 / pct))


def compute_price_technicals(
    bars: Sequence[tuple[float, float, float]],
    *,
    ref_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not bars:
        return None
    hi5d = max(b[0] for b in bars)
    lo5d = min(b[1] for b in bars)
    price = ref_price if ref_price is not None and ref_price > 0 else bars[-1][2]
    if price <= 0:
        return None
    range_pct = (hi5d - lo5d) / price * 100.0
    half_range_pct = range_pct / 2.0
    atr5d_pct = _atr5d_pct(bars, price)
    return {
        "hi5d": round(hi5d, 4),
        "lo5d": round(lo5d, 4),
        "rangePct": round(range_pct, 2),
        "halfRangePct": round(half_range_pct, 2),
        "atr5dPct": round(atr5d_pct, 2) if atr5d_pct is not None else None,
        "levSafe": _lev_from_pct(half_range_pct),
        "levRisk": _lev_from_pct(atr5d_pct),
    }


def _fetch_yahoo_technicals_one(args: tuple[str, Optional[float]]) -> tuple[str, Optional[Dict[str, Any]]]:
    sym, ref_price = args
    try:
        bars = _yahoo_5d_bars(sym)
        if not bars:
            return sym, None
        return sym, compute_price_technicals(bars, ref_price=ref_price)
    except Exception:
        return sym, None


def fetch_yahoo_technicals(
    tickers: Set[str],
    *,
    ref_prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict[str, Any]]:
    prices = ref_prices or {}
    out: Dict[str, Dict[str, Any]] = {}
    if not tickers:
        return out
    jobs = [(sym, prices.get(sym)) for sym in sorted(tickers)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, tech in pool.map(_fetch_yahoo_technicals_one, jobs):
            if tech is not None:
                out[sym] = tech
    return out


def _venue_payload(
    v: VenueFunding,
    side: Optional[str],
    *,
    max_lev: Optional[int] = None,
    next_funding: Optional[VenueFunding] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "pct8h": v.pct_8h,
        "pct24h": v.pct_24h,
        "pctAnn": v.pct_ann,
        "side": side,
    }
    if max_lev is not None:
        payload["maxLev"] = int(max_lev)
    if next_funding is not None:
        payload["nextPct8h"] = next_funding.pct_8h
        payload["nextPct24h"] = next_funding.pct_24h
        payload["nextPctAnn"] = next_funding.pct_ann
    return payload


def build_screener_payload(
    rows: Sequence[FundingRow],
    *,
    ondo_prices: Dict[str, float],
    max_lev_maps: Optional[Dict[str, Dict[str, int]]] = None,
    technicals: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from datetime import datetime, timezone

    screener_rows: List[Dict[str, Any]] = []
    for row in rows:
        rates = {
            "ondo": row.ondo.current.pct_8h,
            "hl": row.hl.pct_8h,
            "lighter": row.lighter.pct_8h,
            "vari": row.vari.pct_8h,
        }
        present_vals = [v for v in rates.values() if v is not None]
        max_arb = (max(present_vals) - min(present_vals)) if len(present_vals) >= 2 else None
        sides = _arb_sides(rates)
        lev = max_lev_maps or {}
        tech = technicals or {}
        sym = row.ticker
        row_payload: Dict[str, Any] = {
                "symbol": sym,
                "price": ondo_prices.get(sym),
                "maxArb": max_arb,
                "venues": {
                    "ondo": _venue_payload(
                        row.ondo.current,
                        sides["ondo"],
                        max_lev=lev.get("ondo", {}).get(sym),
                        next_funding=row.ondo.next,
                    ),
                    "hl": _venue_payload(
                        row.hl,
                        sides["hl"],
                        max_lev=lev.get("hl", {}).get(sym),
                    ),
                    "lighter": _venue_payload(
                        row.lighter,
                        sides["lighter"],
                        max_lev=lev.get("lighter", {}).get(sym),
                    ),
                    "vari": _venue_payload(
                        row.vari,
                        sides["vari"],
                        max_lev=lev.get("vari", {}).get(sym),
                    ),
                },
            }
        if sym in tech:
            row_payload["technicals"] = tech[sym]
        screener_rows.append(row_payload)

    screener_rows.sort(
        key=lambda r: (r["maxArb"] is None, -(r["maxArb"] or 0.0), r["symbol"]),
    )
    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "rows": screener_rows,
    }


def fetch_screener_payload(
    tickers: Optional[Sequence[str]] = None,
    *,
    refresh_vari_max_lev: bool = False,
    timer: Optional[_FetchTimer] = None,
) -> Dict[str, Any]:
    """Build full screener JSON payload (funding, prices, leverage, Yahoo technicals)."""
    if tickers:
        tick_list = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    else:
        tick_list = list(DEFAULT_OVERLAP_TICKERS)
    tick_set = set(tick_list)

    def timed(label: str, fn: Any) -> Any:
        if timer is not None:
            return timer.run(label, fn)
        return fn()

    vari = timed("Vari funding", lambda: fetch_vari_equity_funding(tick_set))
    ondo = timed("Ondo funding", lambda: fetch_ondo_funding(tick_set))
    hl = timed("HL xyz funding", lambda: fetch_hl_xyz_funding(tick_set))
    lighter = timed("Lighter funding", lambda: fetch_lighter_funding(tick_set))

    rows: List[FundingRow] = []
    for sym in tick_list:
        rows.append(
            FundingRow(
                ticker=sym,
                vari=vari[sym] if sym in vari else _empty_funding(),
                ondo=ondo[sym] if sym in ondo else _empty_ondo_funding(),
                hl=hl[sym] if sym in hl else _empty_funding(),
                lighter=lighter[sym] if sym in lighter else _empty_funding(),
            )
        )

    ondo_prices = timed("Ondo prices", lambda: fetch_ondo_prices(tick_set))

    ondo_markets = timed("Max lev · Ondo markets", lambda: fetch_ondo_market_bases())
    hl_markets = timed("Max lev · HL xyz", lambda: fetch_hl_xyz_markets(tick_set))
    lighter_lev = timed("Max lev · Lighter", lambda: fetch_lighter_max_lev(tick_set))
    vari_lev = timed(
        "Max lev · Vari",
        lambda: fetch_vari_max_lev(tick_set, force_refresh=refresh_vari_max_lev),
    )
    max_lev_maps = {
        "ondo": {sym: ondo_max_lev(sym) for sym in tick_set if sym in ondo_markets},
        "hl": {sym: ctx.max_lev for sym, ctx in hl_markets.items() if ctx.max_lev is not None},
        "lighter": lighter_lev,
        "vari": vari_lev,
    }

    yahoo_technicals = timed(
        "Yahoo technicals",
        lambda: fetch_yahoo_technicals(tick_set, ref_prices=ondo_prices),
    )
    return timed(
        "Build payload",
        lambda: build_screener_payload(
            rows,
            ondo_prices=ondo_prices,
            max_lev_maps=max_lev_maps,
            technicals=yahoo_technicals,
        ),
    )


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
    lighter = fetch_lighter_funding(tick_set)

    rows: List[FundingRow] = []
    for sym in sorted(tick_set):
        rows.append(
            FundingRow(
                ticker=sym,
                vari=vari[sym] if sym in vari else _empty_funding(),
                ondo=ondo[sym] if sym in ondo else _empty_ondo_funding(),
                hl=hl[sym] if sym in hl else _empty_funding(),
                lighter=lighter[sym] if sym in lighter else _empty_funding(),
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
        "L_8h",
        "L_24h",
        "L_Ann",
    )
    print("\t".join(headers))
    for row in rows:
        print(
            "\t".join(
                [
                    row.ticker,
                    *_venue_cells(row.vari),
                    *_venue_cells(row.ondo.current),
                    *_venue_cells(row.hl),
                    *_venue_cells(row.lighter),
                ]
            )
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Equity funding rate check: Vari, Ondo Perps, HL xyz, Lighter.",
    )
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
    ap.add_argument(
        "--refresh-vari-max-lev",
        action="store_true",
        help="Re-fetch Vari max leverage via API and print dict to update VARI_MAX_LEV.",
    )
    args = ap.parse_args(argv)

    tickers = _resolve_tickers(args=args)

    if args.write_screener_data:
        timer = _FetchTimer()
        payload = fetch_screener_payload(
            tickers,
            refresh_vari_max_lev=args.refresh_vari_max_lev,
            timer=timer,
        )
        timer.run(
            "Write data.js",
            lambda: write_screener_data_js(args.write_screener_data, payload),
        )
        timer.emit()
        print(f"Wrote screener data → {args.write_screener_data}", file=sys.stderr)
        return 0

    rows = build_rows(tickers)

    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    else:
        print_table(rows)
        print(
            f"\n{len(tickers)} tickers | "
            f"Vari={sum(1 for r in rows if r.vari.pct_8h is not None)} | "
            f"Ondo={sum(1 for r in rows if r.ondo.current.pct_8h is not None)} | "
            f"HL={sum(1 for r in rows if r.hl.pct_8h is not None)} | "
            f"Lighter={sum(1 for r in rows if r.lighter.pct_8h is not None)} | "
            f"rates in %",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
