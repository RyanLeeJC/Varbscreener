from __future__ import annotations

import argparse
import json
import os
import statistics
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

STRATEGY_NAME: str = "revert_near_median"

# --- Strategy settings (edit here) ---

# Trade thesis (printed to strategy/strategy_output.txt when run via the strategy loader).
TRADE_THESIS: str = (
    "Core thesis: In a market-cap-ranked universe (excluding BTC/ETH), focus on tickers whose 24h return is closest "
    "to the cross-sectional median (low-volatility/low-dispersion names), then apply a mean-reversion split: "
    "long the under-median names and short the over-median names."
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
# - "40"    → take Top 40 by the rank metric
# - "21-40" → take ranks 21..40 by the rank metric (inclusive)
# Note: the final count must be even (for 50/50 long/short split).
DEFAULT_TOP_SPEC: str = "40"

# Number of tickers to trade (total, before 50/50 split). Must be even.
DEFAULT_MAX_TICKER_ENTRIES: int = 20

# Default exclude list (unioned with `TICKER_BLACKLIST`).
DEFAULT_EXCLUDE_CSV: str = "BTC,ETH"

# Skip names with extremely one-sided OI (|long-short|/OI); None disables the filter.
DEFAULT_MAX_OI_SKEW: Optional[float] = None

# Skip tickers with 24h volume below this USD amount; None disables the filter.
DEFAULT_MIN_VOL_24H: Optional[float] = None

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
        "JST",
        "BNB",
        "CRO",
        "LTC",
    }
)

# Backtest alignment defaults (from baseline_revert_near_median_* json):
# - mcap_rank_max: 20
# - max_ticker_entries: 10
# - pick_mode: near_median
# - revert: true


def _repo_root_from_here() -> str:
    # This file lives in: <repo_root>/strategy/revert_median.py
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
    header.append(f"Median 24hChg%: {float(meta.get('median_24h_chg_pct')):.2f}%")
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
    chg_24h_pct: float
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
    chg = _parse_pct(d.get("price_change_24h_pct") or d.get("price_change_24h") or d.get("chg_24h_pct"))
    if chg is None:
        return None
    return ListingRow(
        ticker=ticker.upper(),
        oi=oi_val,
        chg_24h_pct=float(chg),
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


def _revert_split_by_24h_change(universe: Sequence[ListingRow]) -> Tuple[float, List[str], List[str]]:
    if len(universe) % 2 != 0:
        raise ValueError(f"Universe size must be even (got {len(universe)}).")
    group_size = len(universe) // 2
    chgs = [r.chg_24h_pct for r in universe]
    median_val = float(statistics.median(chgs))

    # Mean-reversion: long underperformers, short outperformers.
    ordered = sorted(universe, key=lambda r: (r.chg_24h_pct, -r.oi, r.ticker))
    longs = [r.ticker for r in ordered[:group_size]]
    shorts = [r.ticker for r in ordered[-group_size:]]
    return median_val, longs, shorts


def _near_median_subset(universe: Sequence[ListingRow], *, k: int) -> List[ListingRow]:
    """
    Pick the k tickers closest to the cross-sectional median 24h change.
    This matches the "pick_mode: near_median" behavior from backtests.
    """
    kk = int(k)
    if kk <= 0:
        raise ValueError("near_median subset size must be positive")
    if kk % 2 != 0:
        raise ValueError("near_median subset size must be even for long/short split")
    if len(universe) < kk:
        raise ValueError(f"Universe too small for near_median pick: need {kk}, got {len(universe)}")
    median_val = float(statistics.median([r.chg_24h_pct for r in universe]))
    ordered = sorted(universe, key=lambda r: (abs(float(r.chg_24h_pct) - median_val), -r.oi, r.ticker))
    return list(ordered[:kk])


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,  # unused for now (kept for interface compatibility)
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    exclude_set = {s.strip().upper() for s in _split_csv(DEFAULT_EXCLUDE_CSV) if s and str(s).strip()}
    exclude_set |= set(TICKER_BLACKLIST)

    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)
    if top_n is not None:
        start_rank, end_rank = (1, int(top_n))
    else:
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
        if DEFAULT_MIN_VOL_24H is not None:
            if r.vol_24h is None or float(r.vol_24h) < float(DEFAULT_MIN_VOL_24H):
                continue
        if DEFAULT_MAX_OI_SKEW is not None:
            if r.oi_skew is not None and float(r.oi_skew) > float(DEFAULT_MAX_OI_SKEW):
                continue
        filtered.append(r)

    # Ensure even count for 50/50 split by dropping the last element (Option A) if needed.
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

    picked = _near_median_subset(universe, k=int(DEFAULT_MAX_TICKER_ENTRIES))
    median_24h_chg_pct, longs, shorts = _revert_split_by_24h_change(picked)

    meta: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "listing_json": os.path.abspath(str(listing_json)),
        "marketstate_json": os.path.abspath(str(marketstate_json)) if marketstate_json else None,
        "mode": "revert_near_median",
        "pick_mode": "near_median",
        "max_ticker_entries": int(DEFAULT_MAX_TICKER_ENTRIES),
        "median_24h_chg_pct": median_24h_chg_pct,
        "top_spec": (f"{start_rank}-{end_rank}" if start_rank != 1 else str(end_rank)),
        "top_n": len(universe),
        "top_spec_adjusted": bool(adjusted),
        "post_filter_adjusted": bool(filtered_adjusted),
        "rank_by": DEFAULT_RANK_BY,
        "exclude": DEFAULT_EXCLUDE_CSV,
        "max_oi_skew": DEFAULT_MAX_OI_SKEW,
        "min_vol_24h": DEFAULT_MIN_VOL_24H,
        "long_count": len(longs),
        "short_count": len(shorts),
    }
    return longs, shorts, meta


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Strategy: revert near median (pick near-median 24h movers, then mean-reversion split)."
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
        help=f"Total tickers to trade (default {DEFAULT_MAX_TICKER_ENTRIES}); must be even.",
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

    # Apply CLI overrides to module-level behavior for this invocation only.
    global DEFAULT_TOP_SPEC, DEFAULT_EXCLUDE_CSV, DEFAULT_MAX_OI_SKEW, DEFAULT_MIN_VOL_24H, DEFAULT_MAX_TICKER_ENTRIES
    DEFAULT_TOP_SPEC = str(args.top_spec)
    DEFAULT_EXCLUDE_CSV = str(args.exclude)
    DEFAULT_MAX_OI_SKEW = max_skew
    DEFAULT_MIN_VOL_24H = min_vol_24h
    DEFAULT_MAX_TICKER_ENTRIES = int(args.max_ticker_entries)

    longs, shorts, meta = pick_tickers(listing_json=str(args.json_path), marketstate_json=None)
    if args.print_json:
        out: Dict[str, Any] = {**meta, "long": longs, "short": shorts}
        print(json.dumps(out, indent=2))
        return 0

    print(f"Strategy: {STRATEGY_NAME} (top_n={meta.get('top_n')}, exclude={DEFAULT_EXCLUDE_CSV})")
    print(f"Median 24hChg%: {float(meta['median_24h_chg_pct']):.2f}%")
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
            for r in universe2:
                if r.ticker in exclude_set:
                    continue
                if DEFAULT_MIN_VOL_24H is not None:
                    if r.vol_24h is None or float(r.vol_24h) < float(DEFAULT_MIN_VOL_24H):
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

