from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


STRATEGY_NAME: str = "oi_invert"

TRADE_THESIS: str = (
    "Regime-gated OI skew strategy. "
    "First, read BTC/ETH 24h % change (and OI short/long skew). "
    "Only run when BTC/ETH are moving strongly and shorts are building (Pump Now → Dump Next). "
    "Then, inside a liquid universe (ranked by 24h volume with min OI/volume filters), "
    "short outperformers with low OI_short/long, and long outperformers with high OI_short/long (to balance)."
)

# When running this strategy standalone, also write a human-readable table to strategy/strategy_output.txt.
WRITE_STRATEGY_OUTPUT_TXT: bool = True

# When running standalone, also write a universe debug table (top-N by volume) to a separate file.
WRITE_UNIVERSE_DEBUG_TXT: bool = True

# --- Defaults (tune as you like) ---

# How many tickers to consider by 24h volume.
DEFAULT_TOP_N_BY_VOLUME: int = 100

# Exclude list (unioned with TICKER_BLACKLIST).
DEFAULT_EXCLUDE_CSV: str = "BTC,ETH"

# Skip tickers with 24h volume below this USD amount; None disables.
DEFAULT_MIN_VOL_24H_USD: Optional[float] = 30_000.0

# Skip tickers with total open interest (OI_long + OI_short) below this USD amount; None disables.
DEFAULT_MIN_OI_USD: Optional[float] = 50_000.0

# OI dominance ratio thresholds:
# - short when OI_long / OI_short >= this
# - long when OI_short / OI_long >= this
DEFAULT_OI_DOMINANCE_RATIO: float = 1.20

# Optional cap: exclude extreme skew prints (often tiny OI or data quirks).
# Applied as: ratio must be <= DEFAULT_MAX_OI_DOMINANCE_RATIO as well.
DEFAULT_MAX_OI_DOMINANCE_RATIO: float = 5.0

# Momentum thresholds (percentage points, not decimals).
# Used symmetrically:
# - short if 24h >= +X and 7d >= +A
# - long  if 24h <= -X and 7d <= -A
DEFAULT_MIN_24H_PCT: float = 0.0
DEFAULT_MIN_7D_PCT: float = 0.0

USE_BTC_ADJUST: bool = True  # (kept for backward compatibility; unused by current strategy logic)

# --- New strategy gates / bands (keeps the existing universe settings above) ---

# Only proceed when max(|BTC 24h|, |ETH 24h|) exceeds this.
DEFAULT_BTC_ETH_ABS_24H_TRIGGER_PCT: float = 4.0

# Pump Now -> Dump Next regime confirmation:
# If max(BTC 24h, ETH 24h) > trigger AND max(BTC OI_short/long, ETH OI_short/long) > trigger.
DEFAULT_PUMP_NOW_DUMP_NEXT_MIN_OI_SHORT_LONG: float = 1.2

# Candidate bands on OI_short/long for outperformers.
DEFAULT_SHORT_OI_SHORT_LONG_MIN: float = 0.2
DEFAULT_SHORT_OI_SHORT_LONG_MAX: float = 0.75
DEFAULT_LONG_OI_SHORT_LONG_MIN: float = 3.0
DEFAULT_LONG_OI_SHORT_LONG_MAX: float = 10.0

TICKER_BLACKLIST: frozenset[str] = frozenset(
    {
        "XPL",
        "ETC",
        "PAXG",
        "XAUT",
        "RIVER",
        "EDGE",
        "BASED",
        "VVV",
        "IP",
        "ZEC",
    }
)


def _repo_root_from_here() -> str:
    # This file lives in: <repo_root>/strategy/oi_invert.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def default_listingtable_json_path() -> str:
    return os.path.join(_repo_root_from_here(), "Vari Listings", "listingtabledata.json")


def default_universe_debug_txt_path() -> str:
    return os.path.join(os.path.dirname(__file__), "oi_invert_universe.txt")


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _parse_pct(v: Any) -> Optional[float]:
    s = _as_str(v)
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _split_csv(s: Optional[str]) -> List[str]:
    if not s:
        return []
    parts: List[str] = []
    for p in str(s).replace(";", ",").split(","):
        t = p.strip()
        if t:
            parts.append(t)
    return parts


def _load_listing_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("listings"), list):
        listings = payload.get("listings")
        return [x for x in listings if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("Unexpected listingtable payload shape (expected dict with 'listings' or a list).")


def _get_row_by_symbol(rows: Sequence[Dict[str, Any]], sym_u: str) -> Optional[Dict[str, Any]]:
    target = (sym_u or "").strip().upper()
    if not target:
        return None
    return next((x for x in rows if str(x.get("vari_ticker") or "").strip().upper() == target), None)


def _get_btc_eth_changes(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Returns BTC/ETH 24h and 7d percentage-point changes (floats), when available.
    Keys:
      btc_24h_chg_pct, btc_7d_chg_pct, eth_24h_chg_pct, eth_7d_chg_pct
    """
    out: Dict[str, Optional[float]] = {
        "btc_24h_chg_pct": None,
        "btc_7d_chg_pct": None,
        "eth_24h_chg_pct": None,
        "eth_7d_chg_pct": None,
    }
    btc = _get_row_by_symbol(rows, "BTC")
    eth = _get_row_by_symbol(rows, "ETH")
    if isinstance(btc, dict):
        out["btc_24h_chg_pct"] = _parse_pct(
            btc.get("price_change_24h_pct") or btc.get("price_change_24h") or btc.get("chg_24h_pct")
        )
        out["btc_7d_chg_pct"] = _parse_pct(btc.get("price_change_7d_pct") or btc.get("price_change_7d") or btc.get("chg_7d_pct"))
    if isinstance(eth, dict):
        out["eth_24h_chg_pct"] = _parse_pct(
            eth.get("price_change_24h_pct") or eth.get("price_change_24h") or eth.get("chg_24h_pct")
        )
        out["eth_7d_chg_pct"] = _parse_pct(eth.get("price_change_7d_pct") or eth.get("price_change_7d") or eth.get("chg_7d_pct"))
    return out


def _get_btc_eth_oi_short_long(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Returns BTC/ETH OI_short/long ratios (floats), when available.
    Keys:
      btc_oi_short_long, eth_oi_short_long
    """
    out: Dict[str, Optional[float]] = {"btc_oi_short_long": None, "eth_oi_short_long": None}
    btc = _get_row_by_symbol(rows, "BTC")
    eth = _get_row_by_symbol(rows, "ETH")
    if isinstance(btc, dict):
        oi_l = _as_float(btc.get("OI_long") if "OI_long" in btc else btc.get("oi_long"))
        oi_s = _as_float(btc.get("OI_short") if "OI_short" in btc else btc.get("oi_short"))
        out["btc_oi_short_long"] = _ratio(oi_s, oi_l)
    if isinstance(eth, dict):
        oi_l = _as_float(eth.get("OI_long") if "OI_long" in eth else eth.get("oi_long"))
        oi_s = _as_float(eth.get("OI_short") if "OI_short" in eth else eth.get("oi_short"))
        out["eth_oi_short_long"] = _ratio(oi_s, oi_l)
    return out


def _get_btc_eth_oi_long_short(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Returns BTC/ETH OI_long/short ratios (floats), when available.
    Keys:
      btc_oi_long_short, eth_oi_long_short
    """
    out: Dict[str, Optional[float]] = {"btc_oi_long_short": None, "eth_oi_long_short": None}
    btc = _get_row_by_symbol(rows, "BTC")
    eth = _get_row_by_symbol(rows, "ETH")
    if isinstance(btc, dict):
        oi_l = _as_float(btc.get("OI_long") if "OI_long" in btc else btc.get("oi_long"))
        oi_s = _as_float(btc.get("OI_short") if "OI_short" in btc else btc.get("oi_short"))
        out["btc_oi_long_short"] = _ratio(oi_l, oi_s)
    if isinstance(eth, dict):
        oi_l = _as_float(eth.get("OI_long") if "OI_long" in eth else eth.get("oi_long"))
        oi_s = _as_float(eth.get("OI_short") if "OI_short" in eth else eth.get("oi_short"))
        out["eth_oi_long_short"] = _ratio(oi_l, oi_s)
    return out


@dataclass(frozen=True)
class ListingRow:
    ticker: str
    vol_24h: float
    oi_long: float
    oi_short: float
    chg_24h_pct: float
    chg_7d_pct: float

    @property
    def oi_total(self) -> float:
        return float(self.oi_long) + float(self.oi_short)

    @property
    def oi_long_over_short(self) -> Optional[float]:
        if float(self.oi_short) <= 0:
            return None
        return float(self.oi_long) / float(self.oi_short)

    @property
    def oi_short_over_long(self) -> Optional[float]:
        if float(self.oi_long) <= 0:
            return None
        return float(self.oi_short) / float(self.oi_long)


def _to_listing_row(d: Dict[str, Any]) -> Optional[ListingRow]:
    ticker = _as_str(d.get("vari_ticker") or d.get("ticker") or d.get("symbol"))
    if not ticker:
        return None

    vol_24h = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
    oi_long = _as_float(d.get("OI_long") if "OI_long" in d else d.get("oi_long"))
    oi_short = _as_float(d.get("OI_short") if "OI_short" in d else d.get("oi_short"))
    chg_24h = _parse_pct(d.get("price_change_24h_pct") or d.get("price_change_24h") or d.get("chg_24h_pct"))
    chg_7d = _parse_pct(d.get("price_change_7d_pct") or d.get("price_change_7d") or d.get("chg_7d_pct"))

    if vol_24h is None or oi_long is None or oi_short is None or chg_24h is None or chg_7d is None:
        return None
    if float(vol_24h) <= 0:
        return None
    if float(oi_long) < 0 or float(oi_short) < 0:
        return None

    return ListingRow(
        ticker=ticker.upper(),
        vol_24h=float(vol_24h),
        oi_long=float(oi_long),
        oi_short=float(oi_short),
        chg_24h_pct=float(chg_24h),
        chg_7d_pct=float(chg_7d),
    )


def _fmt(v: Optional[float], *, decimals: int = 4) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{decimals}f}"
    except Exception:
        return "-"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f}%"
    except Exception:
        return "-"


def _fmt_intish(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        x = float(v)
    except Exception:
        return "-"
    return f"{x:,.0f}"


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    if float(b) == 0.0:
        return None
    return float(a) / float(b)


def write_universe_debug_txt(
    *,
    listing_json: str,
    out_path: str,
    top_n: int,
    exclude: Set[str],
    min_vol_24h_usd: Optional[float],
    min_oi_usd: Optional[float],
) -> None:
    """
    Writes the top-N by 24h volume (post exclude/min_vol) with:
    Symbol | volume | 24hChg% | 7dChg% | OI_long | OI_short | OI_long/OI_short | OI_short/OI_long

    Includes rows even if some fields are missing; missing values show as '-'.
    """
    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)

    items: List[Dict[str, Any]] = []
    for d in rows:
        sym = _as_str(d.get("vari_ticker") or d.get("ticker") or d.get("symbol"))
        if not sym:
            continue
        sym_u = sym.strip().upper()
        if sym_u in exclude:
            continue

        vol = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
        if vol is None:
            continue  # cannot rank without a volume value
        if min_vol_24h_usd is not None and float(vol) < float(min_vol_24h_usd):
            continue

        chg24 = _parse_pct(d.get("price_change_24h_pct") or d.get("price_change_24h") or d.get("chg_24h_pct"))
        chg7d = _parse_pct(d.get("price_change_7d_pct") or d.get("price_change_7d") or d.get("chg_7d_pct"))
        oi_long = _as_float(d.get("OI_long") if "OI_long" in d else d.get("oi_long"))
        oi_short = _as_float(d.get("OI_short") if "OI_short" in d else d.get("oi_short"))
        oi_total = (float(oi_long) + float(oi_short)) if (oi_long is not None and oi_short is not None) else None
        if min_oi_usd is not None:
            if oi_total is None or float(oi_total) < float(min_oi_usd):
                continue

        items.append(
            {
                "Symbol": sym_u,
                "vol_24h": float(vol),
                "24hChg%": chg24,
                "7dChg%": chg7d,
                "OI": oi_total,
                "OI_long": oi_long,
                "OI_short": oi_short,
                "OI_long/short": _ratio(oi_long, oi_short),
                "OI_short/long": _ratio(oi_short, oi_long),
            }
        )

    items.sort(key=lambda x: (float(x.get("vol_24h") or 0.0), str(x.get("Symbol") or "")), reverse=True)

    # Always show BTC/ETH as the top 1/2 rows (if present),
    # even if they are excluded by the strategy universe.
    def _mk_row_for_symbol(sym_u: str) -> Optional[Dict[str, Any]]:
        d2 = next((x for x in rows if str(x.get("vari_ticker") or "").strip().upper() == sym_u), None)
        if not isinstance(d2, dict):
            return None
        vol2 = _as_float(d2.get("vol_24h") if "vol_24h" in d2 else d2.get("volume_24h"))
        if vol2 is None:
            return None
        chg24_2 = _parse_pct(
            d2.get("price_change_24h_pct") or d2.get("price_change_24h") or d2.get("chg_24h_pct")
        )
        chg7d_2 = _parse_pct(d2.get("price_change_7d_pct") or d2.get("price_change_7d") or d2.get("chg_7d_pct"))
        oi_long2 = _as_float(d2.get("OI_long") if "OI_long" in d2 else d2.get("oi_long"))
        oi_short2 = _as_float(d2.get("OI_short") if "OI_short" in d2 else d2.get("oi_short"))
        oi_total2 = (float(oi_long2) + float(oi_short2)) if (oi_long2 is not None and oi_short2 is not None) else None
        return {
            "Symbol": sym_u,
            "vol_24h": float(vol2),
            "24hChg%": chg24_2,
            "7dChg%": chg7d_2,
            "OI": oi_total2,
            "OI_long": oi_long2,
            "OI_short": oi_short2,
            "OI_long/short": _ratio(oi_long2, oi_short2),
            "OI_short/long": _ratio(oi_short2, oi_long2),
        }

    pinned: List[Dict[str, Any]] = []
    for sym_u in ("BTC", "ETH"):
        r2 = _mk_row_for_symbol(sym_u)
        if r2 is not None:
            pinned.append(r2)

    pinned_syms = {p.get("Symbol") for p in pinned}
    items = [x for x in items if x.get("Symbol") not in pinned_syms]

    # Keep at most top_n from the ranked list, then prepend pinned rows.
    items = pinned + items[: int(top_n)]

    cols = [
        "Symbol",
        "vol_24h",
        "24hChg%",
        "7dChg%",
        "OI",
        "OI_long",
        "OI_short",
        "OI_long/short",
        "OI_short/long",
    ]

    def cell(col: str, row: Dict[str, Any]) -> str:
        v = row.get(col)
        if col == "vol_24h":
            return _fmt_intish(v if isinstance(v, (int, float)) else None)
        if col in ("24hChg%", "7dChg%"):
            return _fmt_pct(v if isinstance(v, (int, float)) else None)
        if col in ("OI_long", "OI_short"):
            return _fmt_intish(v if isinstance(v, (int, float)) else None)
        if col == "OI":
            return _fmt_intish(v if isinstance(v, (int, float)) else None)
        if col in ("OI_long/short", "OI_short/long"):
            return _fmt(v if isinstance(v, (int, float)) else None, decimals=4)
        return str(v) if v is not None else "-"

    widths: Dict[str, int] = {c: len(c) for c in cols}
    for r in items:
        for c in cols:
            widths[c] = max(widths[c], len(cell(c, r)))

    def line(parts: List[str]) -> str:
        out_parts: List[str] = []
        for c, s in zip(cols, parts):
            if c == "Symbol":
                out_parts.append(s.ljust(widths[c]))
            else:
                out_parts.append(s.rjust(widths[c]))
        return "  ".join(out_parts)

    body: List[str] = []
    body.append(f"Strategy: {STRATEGY_NAME} universe debug (Top {int(top_n)} by Volume)")
    body.append(f"listing_json: {os.path.abspath(str(listing_json))}")
    body.append(f"exclude: {', '.join(sorted(exclude)) if exclude else '-'}")
    body.append(f"min_vol_24h_usd: {min_vol_24h_usd if min_vol_24h_usd is not None else '-'}")
    body.append(f"min_oi_usd: {min_oi_usd if min_oi_usd is not None else '-'}")
    body.append("")
    body.append(line(cols))
    body.append(line(["-" * widths[c] for c in cols]))
    for r in items:
        body.append(line([cell(c, r) for c in cols]))

    with open(str(out_path), "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


def _select_universe_by_volume(
    rows: Sequence[Dict[str, Any]],
    *,
    top_n: int,
    exclude: Set[str],
    min_vol_24h_usd: Optional[float],
    min_oi_usd: Optional[float],
) -> List[ListingRow]:
    parsed: List[ListingRow] = []
    for d in rows:
        r = _to_listing_row(d)
        if r is None:
            continue
        if r.ticker in exclude:
            continue
        if min_vol_24h_usd is not None and float(r.vol_24h) < float(min_vol_24h_usd):
            continue
        if min_oi_usd is not None and float(r.oi_total) < float(min_oi_usd):
            continue
        parsed.append(r)

    parsed.sort(key=lambda r: (r.vol_24h, r.ticker), reverse=True)
    # Be resilient to sparse fields (some listings may not include OI_long/OI_short or 7d change).
    # Strategy should still run and just operate on the subset of rows that have the required fields.
    return parsed[: int(top_n)]


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,  # unused (kept for interface compatibility)
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Strategy interface used by `strategy/strategies.py` loader.

    Returns: (longs, shorts, meta)
    """
    eff_top_n = int(top_n) if top_n is not None else int(DEFAULT_TOP_N_BY_VOLUME)
    exclude_set = {s.strip().upper() for s in _split_csv(DEFAULT_EXCLUDE_CSV) if s and str(s).strip()}
    exclude_set |= set(TICKER_BLACKLIST)

    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)
    btc_eth = _get_btc_eth_changes(rows)
    btc_eth_oi = _get_btc_eth_oi_short_long(rows)
    btc_eth_oi_ls = _get_btc_eth_oi_long_short(rows)

    # --- Regime gate (BTC/ETH) ---
    btc24 = btc_eth.get("btc_24h_chg_pct")
    eth24 = btc_eth.get("eth_24h_chg_pct")
    btc7 = btc_eth.get("btc_7d_chg_pct")
    eth7 = btc_eth.get("eth_7d_chg_pct")
    vals24 = [v for v in [btc24, eth24] if v is not None]
    max_abs_24h = max((abs(float(v)) for v in vals24), default=None)
    max_24h = max((float(v) for v in vals24), default=None)
    btc_oi_sl = btc_eth_oi.get("btc_oi_short_long")
    eth_oi_sl = btc_eth_oi.get("eth_oi_short_long")
    vals_sl = [v for v in [btc_oi_sl, eth_oi_sl] if v is not None]
    max_oi_short_long = max((float(v) for v in vals_sl), default=None)

    btc_oi_ls = btc_eth_oi_ls.get("btc_oi_long_short")
    eth_oi_ls = btc_eth_oi_ls.get("eth_oi_long_short")
    vals_ls = [v for v in [btc_oi_ls, eth_oi_ls] if v is not None]
    max_oi_long_short = max((float(v) for v in vals_ls), default=None)

    proceed = (max_abs_24h is not None) and (float(max_abs_24h) > float(DEFAULT_BTC_ETH_ABS_24H_TRIGGER_PCT))
    pump_now_dump_next = (
        (max_24h is not None)
        and (float(max_24h) > float(DEFAULT_BTC_ETH_ABS_24H_TRIGGER_PCT))
        and (max_oi_short_long is not None)
        and (float(max_oi_short_long) > float(DEFAULT_PUMP_NOW_DUMP_NEXT_MIN_OI_SHORT_LONG))
    )

    min_24h = min((float(v) for v in vals24), default=None)
    dump_now_pump_next = (
        (min_24h is not None)
        and (float(min_24h) < -float(DEFAULT_BTC_ETH_ABS_24H_TRIGGER_PCT))
        and (max_oi_long_short is not None)
        and (float(max_oi_long_short) > float(DEFAULT_PUMP_NOW_DUMP_NEXT_MIN_OI_SHORT_LONG))
    )

    universe = _select_universe_by_volume(
        rows,
        top_n=eff_top_n,
        exclude=exclude_set,
        min_vol_24h_usd=DEFAULT_MIN_VOL_24H_USD,
        min_oi_usd=DEFAULT_MIN_OI_USD,
    )

    longs: List[str] = []
    shorts: List[str] = []

    outperform_thr = float(max_24h) if max_24h is not None else None
    underperform_thr = float(min_24h) if min_24h is not None else None

    if proceed and pump_now_dump_next and outperform_thr is not None:
        for r in universe:
            if float(r.chg_24h_pct) <= float(outperform_thr):
                continue
            oi_sl = r.oi_short_over_long
            if oi_sl is None:
                continue

            # Short: outperform BTC/ETH max 24h, with low OI_short/long band.
            if float(DEFAULT_SHORT_OI_SHORT_LONG_MIN) < float(oi_sl) < float(DEFAULT_SHORT_OI_SHORT_LONG_MAX):
                shorts.append(r.ticker)
                continue

            # Long (to balance): outperform BTC/ETH max 24h, with high OI_short/long band.
            if float(DEFAULT_LONG_OI_SHORT_LONG_MIN) < float(oi_sl) < float(DEFAULT_LONG_OI_SHORT_LONG_MAX):
                longs.append(r.ticker)
                continue

        # Balance longs to shorts (cap longs to number of shorts).
        if len(longs) > len(shorts):
            longs = longs[: len(shorts)]

    elif proceed and dump_now_pump_next and underperform_thr is not None and outperform_thr is not None:
        for r in universe:
            oi_ls = r.oi_long_over_short
            if oi_ls is None:
                continue

            # Long: underperform min(BTC,ETH 24h), with low OI_long/short band.
            if float(r.chg_24h_pct) < float(underperform_thr) and float(DEFAULT_SHORT_OI_SHORT_LONG_MIN) < float(oi_ls) < float(DEFAULT_SHORT_OI_SHORT_LONG_MAX):
                longs.append(r.ticker)
                continue

            # Short (to balance): outperform max(BTC,ETH 24h), with high OI_long/short band.
            if float(r.chg_24h_pct) > float(outperform_thr) and float(DEFAULT_LONG_OI_SHORT_LONG_MIN) < float(oi_ls) < float(DEFAULT_LONG_OI_SHORT_LONG_MAX):
                shorts.append(r.ticker)
                continue

        # Balance shorts to longs (cap shorts to number of longs).
        if len(shorts) > len(longs):
            shorts = shorts[: len(longs)]

    meta: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "listing_json": os.path.abspath(str(listing_json)),
        "marketstate_json": os.path.abspath(str(marketstate_json)) if marketstate_json else None,
        "top_n": eff_top_n,
        "universe_count": len(universe),
        **btc_eth,
        **btc_eth_oi,
        **btc_eth_oi_ls,
        "btc_eth_abs_24h_trigger_pct": DEFAULT_BTC_ETH_ABS_24H_TRIGGER_PCT,
        "pump_now_dump_next_min_oi_short_long": DEFAULT_PUMP_NOW_DUMP_NEXT_MIN_OI_SHORT_LONG,
        "short_oi_short_long_band": [DEFAULT_SHORT_OI_SHORT_LONG_MIN, DEFAULT_SHORT_OI_SHORT_LONG_MAX],
        "long_oi_short_long_band": [DEFAULT_LONG_OI_SHORT_LONG_MIN, DEFAULT_LONG_OI_SHORT_LONG_MAX],
        "btc_eth_max_abs_24h": max_abs_24h,
        "btc_eth_max_24h": max_24h,
        "btc_eth_min_24h": min_24h,
        "btc_eth_max_oi_short_long": max_oi_short_long,
        "btc_eth_max_oi_long_short": max_oi_long_short,
        "proceed_gate": bool(proceed),
        "pump_now_dump_next": bool(pump_now_dump_next),
        "dump_now_pump_next": bool(dump_now_pump_next),
        "outperform_threshold_24h": outperform_thr,
        "underperform_threshold_24h": underperform_thr,
        "rank_by": "vol_24h",
        "exclude": DEFAULT_EXCLUDE_CSV,
        "min_vol_24h_usd": DEFAULT_MIN_VOL_24H_USD,
        "min_oi_usd": DEFAULT_MIN_OI_USD,
        "oi_dominance_ratio": DEFAULT_OI_DOMINANCE_RATIO,
        "max_oi_dominance_ratio": DEFAULT_MAX_OI_DOMINANCE_RATIO,
        "long_count": len(longs),
        "short_count": len(shorts),
    }
    return longs, shorts, meta


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Strategy: OI invert (OI crowding + recent momentum).")
    ap.add_argument(
        "--json-path",
        default=os.getenv("LISTINGTABLE_JSON", default_listingtable_json_path()),
        help="Path to listingtabledata.json (default: LISTINGTABLE_JSON env or repo default).",
    )
    ap.add_argument(
        "--universe-txt-path",
        default=default_universe_debug_txt_path(),
        help="Path to write top-N by volume debug table (default: strategy/oi_invert_universe.txt).",
    )
    ap.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N_BY_VOLUME,
        help=f"Universe size by 24h volume after exclusions (default {DEFAULT_TOP_N_BY_VOLUME}).",
    )
    ap.add_argument(
        "--exclude",
        default=DEFAULT_EXCLUDE_CSV,
        help=f"Comma-separated tickers to exclude (default {DEFAULT_EXCLUDE_CSV}).",
    )
    ap.add_argument(
        "--min-vol-24h",
        type=float,
        default=DEFAULT_MIN_VOL_24H_USD if DEFAULT_MIN_VOL_24H_USD is not None else -1.0,
        help=f"Skip listings with vol_24h below this USD amount (default {DEFAULT_MIN_VOL_24H_USD}); use negative to disable.",
    )
    ap.add_argument(
        "--min-oi",
        type=float,
        default=DEFAULT_MIN_OI_USD if DEFAULT_MIN_OI_USD is not None else -1.0,
        help=f"Skip listings with total OI (long+short) below this USD amount (default {DEFAULT_MIN_OI_USD}); use negative to disable.",
    )
    ap.add_argument(
        "--oi-ratio",
        type=float,
        default=DEFAULT_OI_DOMINANCE_RATIO,
        help=f"OI dominance ratio threshold (default {DEFAULT_OI_DOMINANCE_RATIO}).",
    )
    ap.add_argument(
        "--max-oi-ratio",
        type=float,
        default=DEFAULT_MAX_OI_DOMINANCE_RATIO,
        help=f"Cap OI dominance ratio at this value (default {DEFAULT_MAX_OI_DOMINANCE_RATIO}).",
    )
    ap.add_argument("--x24", type=float, default=DEFAULT_MIN_24H_PCT, help="24h threshold X (pct points).")
    ap.add_argument("--a7d", type=float, default=DEFAULT_MIN_7D_PCT, help="7d threshold A (pct points).")
    ap.add_argument(
        "--btc-adjust",
        action="store_true",
        help="Enable BTC-adjusted momentum thresholds (see module docstring near USE_BTC_ADJUST).",
    )
    ap.add_argument("--print-json", action="store_true", help="Print machine-readable JSON output.")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    global DEFAULT_TOP_N_BY_VOLUME, DEFAULT_EXCLUDE_CSV, DEFAULT_MIN_VOL_24H_USD
    global DEFAULT_MIN_OI_USD
    global DEFAULT_OI_DOMINANCE_RATIO, DEFAULT_MIN_24H_PCT, DEFAULT_MIN_7D_PCT
    global DEFAULT_MAX_OI_DOMINANCE_RATIO
    global USE_BTC_ADJUST

    DEFAULT_TOP_N_BY_VOLUME = int(args.top_n)
    DEFAULT_EXCLUDE_CSV = str(args.exclude)
    min_vol = float(args.min_vol_24h)
    DEFAULT_MIN_VOL_24H_USD = None if min_vol < 0 else min_vol
    min_oi = float(args.min_oi)
    DEFAULT_MIN_OI_USD = None if min_oi < 0 else min_oi
    DEFAULT_OI_DOMINANCE_RATIO = float(args.oi_ratio)
    DEFAULT_MAX_OI_DOMINANCE_RATIO = float(args.max_oi_ratio)

    DEFAULT_MIN_24H_PCT = float(args.x24)
    DEFAULT_MIN_7D_PCT = float(args.a7d)
    USE_BTC_ADJUST = bool(args.btc_adjust)

    exclude_set = {s.strip().upper() for s in _split_csv(DEFAULT_EXCLUDE_CSV) if s and str(s).strip()}
    exclude_set |= set(TICKER_BLACKLIST)

    if WRITE_UNIVERSE_DEBUG_TXT and not os.getenv("RAILWAY_ENVIRONMENT"):
        try:
            write_universe_debug_txt(
                listing_json=str(args.json_path),
                out_path=str(args.universe_txt_path),
                top_n=int(DEFAULT_TOP_N_BY_VOLUME),
                exclude=exclude_set,
                min_vol_24h_usd=DEFAULT_MIN_VOL_24H_USD,
                min_oi_usd=DEFAULT_MIN_OI_USD,
            )
        except OSError:
            pass

    longs, shorts, meta = pick_tickers(listing_json=str(args.json_path), marketstate_json=None, top_n=None)
    if args.print_json:
        out: Dict[str, Any] = {**meta, "long": longs, "short": shorts}
        print(json.dumps(out, indent=2))
        return 0

    print(f"Strategy: {STRATEGY_NAME} (top_n={meta.get('top_n')}, exclude={meta.get('exclude')})")
    print(f"Universe ranked by 24h volume; min_vol_24h_usd={meta.get('min_vol_24h_usd')}")
    if meta.get("btc_24h_chg_pct") is not None or meta.get("eth_24h_chg_pct") is not None:
        b24 = meta.get("btc_24h_chg_pct")
        b7 = meta.get("btc_7d_chg_pct")
        e24 = meta.get("eth_24h_chg_pct")
        e7 = meta.get("eth_7d_chg_pct")
        b24s = f"{float(b24):.2f}%" if b24 is not None else "-"
        b7s = f"{float(b7):.2f}%" if b7 is not None else "-"
        e24s = f"{float(e24):.2f}%" if e24 is not None else "-"
        e7s = f"{float(e7):.2f}%" if e7 is not None else "-"
        print(f"BTC 24h={b24s} 7d={b7s} | ETH 24h={e24s} 7d={e7s}")
    trig = meta.get("btc_eth_abs_24h_trigger_pct")
    min_sl = meta.get("pump_now_dump_next_min_oi_short_long")
    max_abs = meta.get("btc_eth_max_abs_24h")
    max_24 = meta.get("btc_eth_max_24h")
    min_24 = meta.get("btc_eth_min_24h")
    max_oi_sl = meta.get("btc_eth_max_oi_short_long")
    max_oi_ls = meta.get("btc_eth_max_oi_long_short")
    proceed = meta.get("proceed_gate")
    pndn = meta.get("pump_now_dump_next")
    dnpn = meta.get("dump_now_pump_next")
    outperf = meta.get("outperform_threshold_24h")
    underperf = meta.get("underperform_threshold_24h")
    sb = meta.get("short_oi_short_long_band")
    lb = meta.get("long_oi_short_long_band")

    def _fmt2(v: Any) -> str:
        if v is None:
            return "-"
        try:
            return f"{float(v):.2f}"
        except Exception:
            return str(v)

    print(f"Gate: proceed if max(|BTC 24h|, |ETH 24h|) > {_fmt2(trig)} (now={_fmt2(max_abs)}).")
    print(
        f"Regime: Pump Now → Dump Next if max(BTC/ETH 24h) > {_fmt2(trig)} (now={_fmt2(max_24)}) and "
        f"max(BTC/ETH OI_short/long) > {_fmt2(min_sl)} (now={_fmt2(max_oi_sl)})."
    )
    print(
        f"Regime: Dump Now → Pump Next if min(BTC/ETH 24h) < -{_fmt2(trig)} (now={_fmt2(min_24)}) and "
        f"max(BTC/ETH OI_long/short) > {_fmt2(min_sl)} (now={_fmt2(max_oi_ls)})."
    )
    print(
        f"Status: proceed={proceed} pump_now_dump_next={pndn} dump_now_pump_next={dnpn} "
        f"outperform_threshold_24h={_fmt2(outperf)} underperform_threshold_24h={_fmt2(underperf)}"
    )
    print(f"Signals: short outperformers with {sb[0]} < OI_short/long < {sb[1]}")
    print(f"        long  outperformers with {lb[0]} < OI_short/long < {lb[1]} (capped to shorts count)")
    print(f"Long [{len(longs)}]: {', '.join(longs) if longs else '(none)'}")
    print(f"Short [{len(shorts)}]: {', '.join(shorts) if shorts else '(none)'}")

    if WRITE_STRATEGY_OUTPUT_TXT and not os.getenv("RAILWAY_ENVIRONMENT"):
        try:
            # Reuse the shared writer so the output matches other strategies.
            try:
                from strategy.strategies import write_strategy_output_txt as _write_shared_output  # type: ignore
            except ModuleNotFoundError:
                # When executed as a file path (python /abs/path/strategy/oi_invert.py),
                # the repo root isn't on sys.path, so `import strategy` fails.
                repo_root = _repo_root_from_here()
                if repo_root not in sys.path:
                    sys.path.insert(0, repo_root)
                from strategy.strategies import write_strategy_output_txt as _write_shared_output  # type: ignore

            _write_shared_output(
                strategy_key=STRATEGY_NAME,
                meta=meta,
                listing_json=str(args.json_path),
                longs=longs,
                shorts=shorts,
            )
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

