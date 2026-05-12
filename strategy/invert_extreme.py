from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

STRATEGY_NAME: str = "invert_extreme"

# --- Strategy settings (edit here) ---

# Trade thesis (printed to strategy/strategy_output.txt when run via the strategy loader).
#
# NOTE: Forked from near_median.py for naming/logging; selection is invert_extreme (see pick_tickers).
TRADE_THESIS: str = (
    "Within a rank window on listing data (default: by market cap, excluding BTC/ETH), after liquidity "
    "filters, keep only names whose absolute 7d change is strictly between a floor and ceiling "
    "(default: >10% and <50%). Then short names with positive 1h and positive 24h change; long names with "
    "negative 1h and negative 24h change. 7d is not a third sign gate for direction: among rows that pass "
    "the band, candidates are ordered by |7d| (then |24h|, |1h|) and the list is capped; long and short "
    "counts need not be equal after the cap."
)

# When running this strategy standalone, also write a human-readable table to Varibot/strategy_output.txt.
# Terminal output remains unchanged.
WRITE_STRATEGY_OUTPUT_TXT: bool = True

# How to rank the universe before splitting.
# - "OI": open interest (default behavior)
# - "market_cap": CoinGecko market cap (from listingtabledata.json)
# - "vol_24h": 24h volume (from listingtabledata.json / Variational volume_24h)
RankBy = Literal["OI", "market_cap", "vol_24h"]
DEFAULT_RANK_BY: RankBy = "market_cap"

# Universe size by rank metric (must be even; split is half long / half short).
# You can set either:
# - "60"    → take Top 60 by the rank metric
# - "21-60" → take ranks 21..60 by the rank metric (inclusive)
# Note: the final count must be even (for 50/50 long/short split).
DEFAULT_TOP_SPEC: str = "120"

# Max tickers returned from pick_tickers (even total before 50/50 split). When the filtered rank window is larger,
# names farthest from cross-sectional median 24h change are dropped first (see _near_median_subset).
DEFAULT_MAX_TICKER_ENTRIES: int = 60

# Default exclude list (unioned with `TICKER_BLACKLIST`).
DEFAULT_EXCLUDE_CSV: str = "BTC,ETH"

# Skip names with extremely one-sided OI (|long-short|/OI); None disables the filter.
DEFAULT_MAX_OI_SKEW: Optional[float] = None

# Skip tickers with 24h volume below this USD amount; None disables the filter.
DEFAULT_MIN_VOL_24H: Optional[float] = 30000

# Skip tickers with open interest below this (same units as listingtabledata.json "OI" field); None disables.
DEFAULT_MIN_OI: Optional[float] = 30000

# Require absolute 7d change strictly greater than this percent (e.g. 10 => keep only |7d| > 10).
# Set negative to disable.
DEFAULT_MIN_ABS_CHG_7D_PCT: float = 10.0

# Require absolute 7d change strictly less than this percent (e.g. 50 => drop if |7d| >= 50).
# Set negative to disable.
DEFAULT_MAX_ABS_CHG_7D_PCT: float = 50.0

# Always dropped when building the top-N-by-OI universe.
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
        "PI",
        "NIGHT",
        "SKY",
        "MORPHO",
        "OKB",
        "TRUMP",
        "RAIN",
        "TON",
        "CC",
        "XMR",
        "STABLE",
        "KITE",
        "M",
        "H",
        "JST",
        "BNB",
        "CRO",
        "LTC",
        "XDC",
    }
)


def _repo_root_from_here() -> str:
    # This file lives in: <repo_root>/strategy/invert_extreme.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def default_listingtable_json_path() -> str:
    return os.path.join(_repo_root_from_here(), "Vari Listings", "listingtabledata.json")


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


def _abs_7d_pct_passes_band(chg_7d_pct: float, *, min_abs: Optional[float], max_abs: Optional[float]) -> bool:
    """True if |7d%| is in the open band (min_abs, max_abs) when bounds are set (strict > min, strict < max)."""
    a = abs(float(chg_7d_pct))
    if min_abs is not None and a <= float(min_abs):
        return False
    if max_abs is not None and a >= float(max_abs):
        return False
    return True


def _parse_top_spec(spec: str) -> Tuple[int, int]:
    """
    Returns (start_rank, end_rank), 1-indexed and inclusive.
    Examples:
      "40" -> (1, 40)
      "21-40" -> (21, 40)
    """
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("top spec is empty")
    if "-" not in raw:
        n = int(raw)
        if n <= 0:
            raise ValueError("top spec must be positive")
        return (1, n)
    a, b = raw.split("-", 1)
    lo = int(a.strip())
    hi = int(b.strip())
    if lo <= 0 or hi <= 0:
        raise ValueError("top spec ranks must be positive")
    if hi < lo:
        raise ValueError("top spec range must be XX-YY with YY >= XX")
    return (lo, hi)


def _normalize_even_count(start_rank: int, end_rank: int) -> Tuple[int, int, bool]:
    """
    Ensures the selected rank window contains an even number of rows by dropping the last rank (Option A).
    Returns (start_rank, end_rank, adjusted).
    """
    s = int(start_rank)
    e = int(end_rank)
    if e < s:
        raise ValueError("invalid top spec (end < start)")
    count = e - s + 1
    if count % 2 == 0:
        return (s, e, False)
    # Option A: drop the last one.
    if e == s:
        raise ValueError("top spec selects 1 row; need an even count for long/short split")
    return (s, e - 1, True)


def _slice_ranks(items: Sequence["ListingRow"], start_rank: int, end_rank: int) -> List["ListingRow"]:
    # ranks are 1-indexed inclusive
    start_i = max(0, int(start_rank) - 1)
    end_i = max(0, int(end_rank))
    return list(items[start_i:end_i])


def _default_strategy_output_txt_path() -> str:
    return os.path.join(_repo_root_from_here(), "Varibot", "strategy_output.txt")


def _chg_pct_by_ticker(universe: Sequence["ListingRow"]) -> Dict[str, float]:
    return {r.ticker.upper(): float(r.chg_24h_pct) for r in universe}


def _table_lines(title: str, tickers: Sequence[str], chg: Dict[str, float]) -> List[str]:
    lines: List[str] = [f"{title} [{len(tickers)}]:", ""]
    if not tickers:
        lines.append("(none)")
        return lines
    col_a, col_b = "Symbol", "24hChg%"
    # Sort best -> worst by 24hChg% for the file output table.
    ordered = sorted(
        [str(t).upper() for t in tickers],
        key=lambda t: (chg.get(str(t).upper(), float("-inf")), str(t).upper()),
        reverse=True,
    )
    rows = [(t, f"{chg.get(t, float('nan')):.2f}%") for t in ordered]
    w0 = max(len(col_a), max((len(r[0]) for r in rows), default=0))
    w1 = max(len(col_b), max((len(r[1]) for r in rows), default=0))
    lines.append(f"{col_a:<{w0}}  {col_b:>{w1}}")
    lines.append(f"{'-' * w0}  {'-' * w1}")
    for t, p in rows:
        lines.append(f"{t:<{w0}}  {p:>{w1}}")
    return lines


def write_strategy_output_txt(
    path: str,
    *,
    meta: Dict[str, Any],
    universe: Sequence["ListingRow"],
    longs: Sequence[str],
    shorts: Sequence[str],
) -> None:
    chg = _chg_pct_by_ticker(universe)
    header: List[str] = []
    header.append(f"Strategy: {meta.get('strategy')} (top_n={meta.get('top_n')}, exclude={meta.get('exclude')})")
    if meta.get("rank_by"):
        header.append(f"RankBy: {meta.get('rank_by')}")

    body: List[str] = []
    body.extend(header)
    body.append("")
    body.extend(_table_lines("Long", [str(x).upper() for x in longs], chg))
    body.append("")
    body.extend(_table_lines("Short", [str(x).upper() for x in shorts], chg))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


@dataclass(frozen=True)
class ListingRow:
    ticker: str
    oi: float
    chg_1h_pct: float
    chg_24h_pct: float
    chg_7d_pct: float
    market_cap: Optional[float]
    vol_24h: Optional[float]
    oi_skew: Optional[float]


def _load_listing_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("listings"), list):
        listings = payload.get("listings")
        return [x for x in listings if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("Unexpected listingtable payload shape (expected dict with 'listings' or a list).")


def _to_listing_row(d: Dict[str, Any]) -> Optional[ListingRow]:
    ticker = _as_str(d.get("vari_ticker") or d.get("ticker") or d.get("symbol"))
    if not ticker:
        return None
    oi = _as_float(d.get("OI") if "OI" in d else d.get("open_interest"))
    # For rank_by != OI we still want to keep the row even if OI is missing;
    # OI is used as a tie-breaker / metadata downstream.
    oi_val = float(oi) if oi is not None else 0.0
    market_cap = _as_float(d.get("market_cap"))
    vol_24h = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
    oi_skew = _as_float(d.get("OI Skew"))
    chg_1h = _parse_pct(d.get("price_change_1h_pct") or d.get("price_change_1h") or d.get("chg_1h_pct"))
    chg_24h = _parse_pct(d.get("price_change_24h_pct") or d.get("price_change_24h") or d.get("chg_24h_pct"))
    chg_7d = _parse_pct(d.get("price_change_7d_pct") or d.get("price_change_7d") or d.get("chg_7d_pct"))
    if chg_1h is None or chg_24h is None or chg_7d is None:
        return None
    return ListingRow(
        ticker=ticker.upper(),
        oi=oi_val,
        chg_1h_pct=float(chg_1h),
        chg_24h_pct=float(chg_24h),
        chg_7d_pct=float(chg_7d),
        market_cap=market_cap,
        vol_24h=vol_24h,
        oi_skew=oi_skew,
    )


def _ranked_universe(
    rows: Sequence[Dict[str, Any]],
    *,
    rank_by: RankBy,
) -> List[ListingRow]:
    parsed: List[ListingRow] = []
    for d in rows:
        r = _to_listing_row(d)
        if r is None:
            continue
        parsed.append(r)

    def sort_key(r: ListingRow) -> Tuple[float, str]:
        if rank_by == "market_cap":
            v = float(r.market_cap) if r.market_cap is not None else 0.0
        elif rank_by == "vol_24h":
            v = float(r.vol_24h) if r.vol_24h is not None else 0.0
        else:
            v = float(r.oi)
        return (v, r.ticker)

    parsed.sort(key=sort_key, reverse=True)
    return parsed


def _pick_invert_extreme(
    universe: Sequence[ListingRow],
    *,
    max_total_entries: int,
) -> Tuple[List[str], List[str]]:
    """
    Selection per user spec (caller must already restrict |7d%| band, liquidity, rank window, etc.):
      - shorts: up 1h and up 24h; priority: most positive 7d first
      - longs:  down 1h and down 24h; priority: most negative 7d first
    No pairing requirement: fill up to max_total_entries across both buckets (can be lopsided).
    Final inclusion priority is global by abs(7d%), after bucket criteria are satisfied.
    """
    cap = int(max_total_entries)
    if cap <= 0:
        return [], []

    shorts_all = [r for r in universe if float(r.chg_1h_pct) > 0.0 and float(r.chg_24h_pct) > 0.0]
    longs_all = [r for r in universe if float(r.chg_1h_pct) < 0.0 and float(r.chg_24h_pct) < 0.0]

    # Bucket ranking (kept for tie-break stability inside a bucket).
    shorts_ranked = sorted(
        shorts_all,
        key=lambda r: (float(r.chg_7d_pct), float(r.chg_24h_pct), float(r.chg_1h_pct), r.ticker),
        reverse=True,
    )
    longs_ranked = sorted(
        longs_all,
        key=lambda r: (float(r.chg_7d_pct), float(r.chg_24h_pct), float(r.chg_1h_pct), r.ticker),
    )

    # Global inclusion priority: abs(7d%) desc (ties: abs(24h), abs(1h), then ticker).
    # We preserve side labels by building a combined list first, then splitting after truncation.
    combined: List[Tuple[str, ListingRow]] = []
    combined.extend([("S", r) for r in shorts_ranked])
    combined.extend([("L", r) for r in longs_ranked])
    combined.sort(
        key=lambda x: (abs(float(x[1].chg_7d_pct)), abs(float(x[1].chg_24h_pct)), abs(float(x[1].chg_1h_pct)), x[1].ticker),
        reverse=True,
    )
    picked = combined[:cap]

    longs: List[str] = []
    shorts: List[str] = []
    for side, r in picked:
        if side == "L":
            longs.append(r.ticker)
        else:
            shorts.append(r.ticker)
    return longs, shorts


def _selected_sign_check(
    universe: Sequence[ListingRow],
    *,
    longs: Sequence[str],
    shorts: Sequence[str],
) -> Dict[str, Any]:
    """
    Verify the selection invariants:
      - all shorts are up 1h & up 24h
      - all longs are down 1h & down 24h
    (No pairing / count-balance invariant is enforced.)
    """
    idx: Dict[str, ListingRow] = {r.ticker.upper(): r for r in universe}

    bad_shorts: List[Dict[str, Any]] = []
    for s in shorts:
        r = idx.get(str(s).strip().upper())
        if r is None:
            bad_shorts.append({"ticker": s, "reason": "missing_from_universe"})
            continue
        if not (float(r.chg_1h_pct) > 0.0 and float(r.chg_24h_pct) > 0.0):
            bad_shorts.append(
                {
                    "ticker": r.ticker,
                    "chg_1h_pct": float(r.chg_1h_pct),
                    "chg_24h_pct": float(r.chg_24h_pct),
                    "chg_7d_pct": float(r.chg_7d_pct),
                }
            )

    bad_longs: List[Dict[str, Any]] = []
    for s in longs:
        r = idx.get(str(s).strip().upper())
        if r is None:
            bad_longs.append({"ticker": s, "reason": "missing_from_universe"})
            continue
        if not (float(r.chg_1h_pct) < 0.0 and float(r.chg_24h_pct) < 0.0):
            bad_longs.append(
                {
                    "ticker": r.ticker,
                    "chg_1h_pct": float(r.chg_1h_pct),
                    "chg_24h_pct": float(r.chg_24h_pct),
                    "chg_7d_pct": float(r.chg_7d_pct),
                }
            )

    return {
        "ok": (len(bad_shorts) == 0 and len(bad_longs) == 0),
        "bad_shorts": bad_shorts[:20],
        "bad_longs": bad_longs[:20],
        "bad_shorts_n": len(bad_shorts),
        "bad_longs_n": len(bad_longs),
    }


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,  # unused for now (kept for interface compatibility)
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Rank window comes from DEFAULT_TOP_SPEC only (and CLI overrides of module-level settings).
    The orchestrator's numeric `top_n` is ignored — it used to shrink the rank window (e.g. to 60)
    and the portfolio manager's replacement pool.
    """
    _ = top_n

    exclude_set = {s.strip().upper() for s in _split_csv(DEFAULT_EXCLUDE_CSV) if s and str(s).strip()}
    exclude_set |= set(TICKER_BLACKLIST)

    min_abs_7d: Optional[float] = float(DEFAULT_MIN_ABS_CHG_7D_PCT)
    if min_abs_7d < 0:
        min_abs_7d = None

    max_abs_7d: Optional[float] = float(DEFAULT_MAX_ABS_CHG_7D_PCT)
    if max_abs_7d < 0:
        max_abs_7d = None

    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)
    start_rank, end_rank = _parse_top_spec(DEFAULT_TOP_SPEC)
    start_rank, end_rank, adjusted = _normalize_even_count(start_rank, end_rank)

    ranked = _ranked_universe(rows, rank_by=DEFAULT_RANK_BY)
    if len(ranked) < int(end_rank):
        raise ValueError(
            f"Not enough rows to reach rank {end_rank} by {DEFAULT_RANK_BY} "
            f"(got {len(ranked)} total rows after basic parsing)."
        )
    universe = _slice_ranks(ranked, start_rank, end_rank)

    # Apply filters AFTER slicing by rank (this matches: 'take OI ranks 60-100, then filter further').
    filtered: List[ListingRow] = []
    for r in universe:
        if r.ticker in exclude_set:
            continue
        if not _abs_7d_pct_passes_band(r.chg_7d_pct, min_abs=min_abs_7d, max_abs=max_abs_7d):
            continue
        if DEFAULT_MIN_VOL_24H is not None:
            if r.vol_24h is None or float(r.vol_24h) < float(DEFAULT_MIN_VOL_24H):
                continue
        if DEFAULT_MIN_OI is not None:
            if float(r.oi) < float(DEFAULT_MIN_OI):
                continue
        if DEFAULT_MAX_OI_SKEW is not None:
            if r.oi_skew is not None and float(r.oi_skew) > float(DEFAULT_MAX_OI_SKEW):
                continue
        filtered.append(r)

    # Ensure even count for paired long/short basket by dropping the last element (Option A) if needed.
    filtered_adjusted = False
    if len(filtered) % 2 != 0:
        filtered = filtered[:-1]
        filtered_adjusted = True
    if len(filtered) < 2:
        raise ValueError(
            f"Rank window {start_rank}-{end_rank} produced {len(filtered)} rows after filters; "
            f"need at least 2 to split."
        )

    universe = filtered

    longs, shorts = _pick_invert_extreme(universe, max_total_entries=int(DEFAULT_MAX_TICKER_ENTRIES))
    sign_check = _selected_sign_check(universe, longs=longs, shorts=shorts)

    meta = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "listing_json": os.path.abspath(str(listing_json)),
        "marketstate_json": os.path.abspath(str(marketstate_json)) if marketstate_json else None,
        "mode": "invert_extreme",
        "pick_mode": "invert_extreme",
        "max_ticker_entries": int(DEFAULT_MAX_TICKER_ENTRIES),
        "top_spec": (f"{start_rank}-{end_rank}" if start_rank != 1 else str(end_rank)),
        "top_n": len(universe),
        "top_spec_adjusted": bool(adjusted),
        "post_filter_adjusted": bool(filtered_adjusted),
        "rank_by": DEFAULT_RANK_BY,
        "exclude": DEFAULT_EXCLUDE_CSV,
        "max_oi_skew": DEFAULT_MAX_OI_SKEW,
        "min_vol_24h": DEFAULT_MIN_VOL_24H,
        "min_oi": DEFAULT_MIN_OI,
        "min_abs_chg_7d_pct": min_abs_7d,
        "max_abs_chg_7d_pct": max_abs_7d,
        "long_count": len(longs),
        "short_count": len(shorts),
        "orchestrator_top_n_ignored": True,
        "selection": {
            "abs_7d_band": "|7d%| strictly between min and max when set (applied before 1h/24h bucketing and rank)",
            "short_filter": "chg_1h_pct>0 AND chg_24h_pct>0",
            "long_filter": "chg_1h_pct<0 AND chg_24h_pct<0",
            "rank": "global abs(7d) desc after bucket filters (ties: abs(24h), abs(1h)); bucket ordering uses 7d/24h/1h",
            "sign_check": sign_check,
        },
    }
    return longs, shorts, meta


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Strategy: invert_extreme (short winners vs long losers by 1h/24h/7d signs)."
    )
    ap.add_argument(
        "--json-path",
        default=os.getenv("LISTINGTABLE_JSON", default_listingtable_json_path()),
        help="Path to listingtabledata.json (default: LISTINGTABLE_JSON env or repo default).",
    )
    ap.add_argument(
        "--top",
        dest="top_spec",
        default=DEFAULT_TOP_SPEC,
        help=f"Top ranks spec (default {DEFAULT_TOP_SPEC!r}). Example: '40' or '21-40'. Selected count must be even.",
    )
    ap.add_argument(
        "--max-ticker-entries",
        type=int,
        default=DEFAULT_MAX_TICKER_ENTRIES,
        help=(
            f"Max tickers returned (default {DEFAULT_MAX_TICKER_ENTRIES}). "
            f"If the filter set is larger, names farthest from median 24h change are dropped; odd values use cap−1."
        ),
    )
    ap.add_argument(
        "--exclude",
        default=DEFAULT_EXCLUDE_CSV,
        help=f"Comma-separated tickers to exclude (default {DEFAULT_EXCLUDE_CSV}).",
    )
    ap.add_argument(
        "--max-oi-skew",
        type=float,
        default=DEFAULT_MAX_OI_SKEW if DEFAULT_MAX_OI_SKEW is not None else -1.0,
        help=f"Skip listings with OI Skew above this (default {DEFAULT_MAX_OI_SKEW}); use negative to disable.",
    )
    ap.add_argument(
        "--min-vol-24h",
        type=float,
        default=DEFAULT_MIN_VOL_24H if DEFAULT_MIN_VOL_24H is not None else -1.0,
        help=f"Skip listings with vol_24h below this USD amount (default {DEFAULT_MIN_VOL_24H}); use negative to disable.",
    )
    ap.add_argument(
        "--min-oi",
        type=float,
        default=DEFAULT_MIN_OI if DEFAULT_MIN_OI is not None else -1.0,
        help=f"Skip listings with OI below this (JSON key \"OI\"; default {DEFAULT_MIN_OI}); use negative to disable.",
    )
    ap.add_argument(
        "--min-abs-7d-pct",
        type=float,
        default=float(DEFAULT_MIN_ABS_CHG_7D_PCT),
        help=(
            f"Skip tickers with abs(7d change %) less than or equal to this (default {DEFAULT_MIN_ABS_CHG_7D_PCT:g}, "
            f"i.e. keep |7d| > this); use negative to disable."
        ),
    )
    ap.add_argument(
        "--max-abs-7d-pct",
        type=float,
        default=float(DEFAULT_MAX_ABS_CHG_7D_PCT),
        help=(
            f"Skip tickers with abs(7d change %) greater than or equal to this (default {DEFAULT_MAX_ABS_CHG_7D_PCT:g}, "
            f"i.e. keep |7d| < this); use negative to disable."
        ),
    )
    ap.add_argument("--print-json", action="store_true", help="Print machine-readable JSON output.")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    max_skew: Optional[float] = float(args.max_oi_skew)
    if max_skew is not None and max_skew < 0:
        max_skew = None
    min_vol_24h: Optional[float] = float(args.min_vol_24h)
    if min_vol_24h is not None and min_vol_24h < 0:
        min_vol_24h = None
    min_oi: Optional[float] = float(args.min_oi)
    if min_oi is not None and min_oi < 0:
        min_oi = None

    min_abs_7d: Optional[float] = float(args.min_abs_7d_pct)
    if min_abs_7d is not None and min_abs_7d < 0:
        min_abs_7d = None

    max_abs_7d: Optional[float] = float(args.max_abs_7d_pct)
    if max_abs_7d is not None and max_abs_7d < 0:
        max_abs_7d = None

    # Apply CLI overrides to module-level behavior for this invocation only.
    global DEFAULT_TOP_SPEC, DEFAULT_EXCLUDE_CSV, DEFAULT_MAX_OI_SKEW, DEFAULT_MIN_VOL_24H, DEFAULT_MIN_OI, DEFAULT_MAX_TICKER_ENTRIES, DEFAULT_MIN_ABS_CHG_7D_PCT, DEFAULT_MAX_ABS_CHG_7D_PCT
    DEFAULT_TOP_SPEC = str(args.top_spec)
    DEFAULT_EXCLUDE_CSV = str(args.exclude)
    DEFAULT_MAX_OI_SKEW = max_skew
    DEFAULT_MIN_VOL_24H = min_vol_24h
    DEFAULT_MIN_OI = min_oi
    DEFAULT_MAX_TICKER_ENTRIES = int(args.max_ticker_entries)
    DEFAULT_MIN_ABS_CHG_7D_PCT = float(min_abs_7d) if min_abs_7d is not None else -1.0
    DEFAULT_MAX_ABS_CHG_7D_PCT = float(max_abs_7d) if max_abs_7d is not None else -1.0

    longs, shorts, meta = pick_tickers(listing_json=str(args.json_path), marketstate_json=None)
    if args.print_json:
        out: Dict[str, Any] = {**meta, "long": longs, "short": shorts}
        print(json.dumps(out, indent=2))
        return 0

    print(f"Strategy: {STRATEGY_NAME} (top_n={meta.get('top_n')}, exclude={DEFAULT_EXCLUDE_CSV})")
    print(f"Long [{len(longs)}]: {', '.join(longs)}")
    print(f"Short [{len(shorts)}]: {', '.join(shorts)}")

    if WRITE_STRATEGY_OUTPUT_TXT and not os.getenv("RAILWAY_ENVIRONMENT"):
        try:
            # Recompute the selected universe for table output (keeps file output aligned with the chosen settings).
            with open(str(args.json_path), "r", encoding="utf-8") as f:
                payload = json.load(f)
            rows = _load_listing_rows(payload)
            exclude_set = {s.strip().upper() for s in _split_csv(DEFAULT_EXCLUDE_CSV) if s and str(s).strip()}
            exclude_set |= set(TICKER_BLACKLIST)
            start_rank, end_rank = _parse_top_spec(DEFAULT_TOP_SPEC)
            start_rank, end_rank, _ = _normalize_even_count(start_rank, end_rank)
            ranked2 = _ranked_universe(rows, rank_by=DEFAULT_RANK_BY)
            universe2 = _slice_ranks(ranked2, start_rank, end_rank)
            filtered2: List[ListingRow] = []
            min_a: Optional[float] = float(DEFAULT_MIN_ABS_CHG_7D_PCT)
            if min_a < 0:
                min_a = None
            max_a: Optional[float] = float(DEFAULT_MAX_ABS_CHG_7D_PCT)
            if max_a < 0:
                max_a = None
            for r in universe2:
                if r.ticker in exclude_set:
                    continue
                if not _abs_7d_pct_passes_band(r.chg_7d_pct, min_abs=min_a, max_abs=max_a):
                    continue
                if DEFAULT_MIN_VOL_24H is not None:
                    if r.vol_24h is None or float(r.vol_24h) < float(DEFAULT_MIN_VOL_24H):
                        continue
                if DEFAULT_MIN_OI is not None:
                    if float(r.oi) < float(DEFAULT_MIN_OI):
                        continue
                if DEFAULT_MAX_OI_SKEW is not None:
                    if r.oi_skew is not None and float(r.oi_skew) > float(DEFAULT_MAX_OI_SKEW):
                        continue
                filtered2.append(r)
            if len(filtered2) % 2 != 0:
                filtered2 = filtered2[:-1]
            universe2 = filtered2
            write_strategy_output_txt(
                _default_strategy_output_txt_path(),
                meta=meta,
                universe=universe2,
                longs=longs,
                shorts=shorts,
            )
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

