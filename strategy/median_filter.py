from __future__ import annotations

import argparse
import json
import os
import sys
import statistics
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

MedianMode = Literal["directional", "sideways"]

# Strategy module identifier (used by varibot strategy loader).
STRATEGY_NAME: str = "median_filter"

# Trade thesis (printed to strategy/strategy_output.txt when run via the strategy loader).
TRADE_THESIS: str = (
    "Core thesis: Use the market regime to choose between momentum vs mean‑reversion. "
    "Build a liquid universe (ranked by OI/market cap/volume), then split it by the median 24h % change. "
    "In Directional mode, long outperformers and short underperformers (momentum). "
    "In Sideways mode, long underperformers and short outperformers (fade/mean‑revert)."
)

# How to rank the universe before splitting.
# - "OI": open interest (default behavior)
# - "market_cap": CoinGecko market cap (from listingtabledata.json)
# - "vol_24h": 24h volume (from listingtabledata.json / Variational volume_24h)
RankBy = Literal["OI", "market_cap", "vol_24h"]
DEFAULT_RANK_BY: RankBy = "OI"

# Default universe size by OI (split is half long / half short via median of 24h %).
_DEFAULT_TOP_N: int = 40

# Default exclude list for this strategy (unioned with `TICKER_BLACKLIST`).
DEFAULT_EXCLUDE_CSV: str = "BTC,ETH"

# Skip tickers with 24h volume below this USD amount; None disables the filter.
DEFAULT_MIN_VOL_24H: Optional[float] = 30_000.0

# Skip names with extremely one-sided OI (|long-short|/OI); None disables the filter.
DEFAULT_MAX_OI_SKEW: Optional[float] = 0.95

# Always dropped when building the top-N-by-OI universe (unioned with --exclude).
TICKER_BLACKLIST: frozenset[str] = frozenset(
    {
        "XPL","ETC","PAXG","XAUT","RIVER","EDGE","BASED","VVV","IP","H"
    }
)


def _repo_root_from_here() -> str:
    # This file lives in: <repo_root>/strategy/median_filter.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def default_listingtable_json_path() -> str:
    return os.path.join(_repo_root_from_here(), "Vari Listings", "listingtabledata.json")


def default_marketstate_json_path() -> str:
    return os.path.join(_repo_root_from_here(), "Vari Listings", "marketstate.json")


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
    """
    Parses values like "1.23%" or "-0.47%" into percentage points as float.
    Returns None when parsing fails.
    """
    s = _as_str(v)
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


@dataclass(frozen=True)
class ListingRow:
    ticker: str
    oi: float
    chg_24h_pct: float
    market_cap: Optional[float]
    vol_24h: Optional[float]


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
    if oi is None:
        return None
    market_cap = _as_float(d.get("market_cap"))
    vol_24h = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
    chg = _parse_pct(d.get("price_change_24h_pct") or d.get("price_change_24h") or d.get("chg_24h_pct"))
    if chg is None:
        return None
    return ListingRow(
        ticker=ticker.upper(),
        oi=float(oi),
        chg_24h_pct=float(chg),
        market_cap=market_cap,
        vol_24h=vol_24h,
    )


def select_top_n_by_oi(
    rows: Sequence[Dict[str, Any]],
    *,
    n: int,
    exclude: Set[str],
    max_oi_skew: Optional[float] = DEFAULT_MAX_OI_SKEW,
    min_vol_24h: Optional[float] = DEFAULT_MIN_VOL_24H,
    rank_by: RankBy = DEFAULT_RANK_BY,
) -> List[ListingRow]:
    parsed: List[ListingRow] = []
    for d in rows:
        if min_vol_24h is not None:
            vol = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
            # If volume is missing/unparseable, skip (safer than including illiquid names).
            if vol is None or float(vol) < float(min_vol_24h):
                continue
        if max_oi_skew is not None:
            skew = _as_float(d.get("OI Skew"))
            if skew is not None and skew > max_oi_skew:
                continue
        r = _to_listing_row(d)
        if r is None:
            continue
        if r.ticker in exclude:
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
    if len(parsed) < n:
        raise ValueError(
            f"Not enough valid rows to select top {n} by {rank_by} (got {len(parsed)} after parsing/excludes)."
        )
    return parsed[:n]


@dataclass(frozen=True)
class MedianSplitResult:
    universe: List[ListingRow]
    median_24h_chg_pct: float
    outperformers: List[str]
    underperformers: List[str]


def median_split_by_24h_change(
    universe: Sequence[ListingRow],
    *,
    group_size: int = 50,
) -> MedianSplitResult:
    if len(universe) != group_size * 2:
        raise ValueError(f"Universe must be exactly {group_size*2} rows (got {len(universe)}).")

    chgs = [r.chg_24h_pct for r in universe]
    median_val = float(statistics.median(chgs))

    # Deterministic 50/50: sort by 24h change, then OI, then ticker.
    ordered = sorted(universe, key=lambda r: (r.chg_24h_pct, -r.oi, r.ticker))
    under = [r.ticker for r in ordered[:group_size]]
    over = [r.ticker for r in ordered[-group_size:]]

    return MedianSplitResult(
        universe=list(universe),
        median_24h_chg_pct=median_val,
        outperformers=over,
        underperformers=under,
    )


def get_median_groups_from_listingtable_json(
    *,
    json_path: str,
    top_n: int = _DEFAULT_TOP_N,
    exclude: Optional[Iterable[str]] = None,
    max_oi_skew: Optional[float] = DEFAULT_MAX_OI_SKEW,
    min_vol_24h: Optional[float] = DEFAULT_MIN_VOL_24H,
    rank_by: RankBy = DEFAULT_RANK_BY,
) -> MedianSplitResult:
    exclude_set = {s.strip().upper() for s in (exclude or []) if s and str(s).strip()}
    exclude_set |= TICKER_BLACKLIST
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)
    universe = select_top_n_by_oi(
        rows,
        n=top_n,
        exclude=exclude_set,
        max_oi_skew=max_oi_skew,
        min_vol_24h=min_vol_24h,
        rank_by=rank_by,
    )
    return median_split_by_24h_change(universe, group_size=top_n // 2)


def read_24h_market_regime_from_marketstate_json(marketstate_json_path: str) -> str:
    """
    Reads Vari Listings/marketstate.json (output of marketstate.py).
    Expects payload['market_state']['24h_market_regime'], e.g. 'Sideways Now' / 'Directional Now'
    (also accepts 'Sideways Next' / 'Directional Next' style labels — substring match in regime_to_median_mode).
    """
    with open(marketstate_json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    ms = payload.get("market_state") if isinstance(payload, dict) else None
    if not isinstance(ms, dict):
        raise ValueError(f"market_state missing or not an object in {marketstate_json_path!r}")
    regime = ms.get("24h_market_regime")
    s = _as_str(regime)
    if not s:
        raise ValueError(f"market_state.24h_market_regime missing/empty in {marketstate_json_path!r}")
    return s


def regime_to_median_mode(regime: str) -> MedianMode:
    """
    Maps marketstate *current* regime (… Now) to the median split style used for *expected next* regime.

    - Sideways Now -> expect Directional Next -> momentum split: long top half 24h, short bottom (mode directional).
    - Directional Now -> expect Sideways Next -> fade split: long bottom half, short top (mode sideways).

    Labels with "Next" in the name are treated the same by substring ("sideways" / "directional").
    """
    lo = regime.strip().lower()
    if "directional" in lo:
        return "sideways"
    if "sideways" in lo:
        return "directional"
    raise ValueError(
        f"Unrecognized 24h_market_regime {regime!r}: expected a label containing 'Directional' or 'Sideways' "
        f"(e.g. from marketstate.py: 'Directional Now', 'Sideways Now')."
    )


def format_regime_now_to_next(regime: str) -> str:
    """Human line: '<regime> -> <expected Next>' for terminal output."""
    lo = regime.strip().lower()
    if "directional" in lo:
        return f"{regime.strip()} -> Sideways Next"
    if "sideways" in lo:
        return f"{regime.strip()} -> Directional Next"
    return f"{regime.strip()} -> ?"


def long_short_for_mode(res: MedianSplitResult, mode: MedianMode) -> Tuple[List[str], List[str]]:
    if mode == "directional":
        return res.outperformers, res.underperformers
    return res.underperformers, res.outperformers


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Strategy interface used by `varibot.py`.

    Returns: (longs, shorts, meta)
    """
    eff_top_n = int(top_n) if top_n is not None else int(_DEFAULT_TOP_N)
    res = get_median_groups_from_listingtable_json(
        json_path=str(listing_json),
        top_n=eff_top_n,
        exclude=_split_csv(DEFAULT_EXCLUDE_CSV),
        max_oi_skew=DEFAULT_MAX_OI_SKEW,
        min_vol_24h=DEFAULT_MIN_VOL_24H,
        rank_by=DEFAULT_RANK_BY,
    )

    regime: Optional[str] = None
    mode: MedianMode = "directional"
    if marketstate_json and os.path.isfile(str(marketstate_json)):
        try:
            regime = read_24h_market_regime_from_marketstate_json(str(marketstate_json))
            mode = regime_to_median_mode(regime)
        except Exception:
            # If marketstate is missing/unreadable, fall back to directional.
            regime = None
            mode = "directional"

    longs, shorts = long_short_for_mode(res, mode)
    meta: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "listing_json": os.path.abspath(str(listing_json)),
        "marketstate_json": os.path.abspath(str(marketstate_json))
        if marketstate_json
        else None,
        "mode": mode,
        "24h_market_regime": regime,
        "median_24h_chg_pct": res.median_24h_chg_pct,
        "top_n": eff_top_n,
        "rank_by": DEFAULT_RANK_BY,
        "exclude": DEFAULT_EXCLUDE_CSV,
        "max_oi_skew": DEFAULT_MAX_OI_SKEW,
        "min_vol_24h": DEFAULT_MIN_VOL_24H,
        "long_count": len(longs),
        "short_count": len(shorts),
    }
    return longs, shorts, meta


def _chg_pct_by_ticker(res: MedianSplitResult) -> Dict[str, float]:
    return {r.ticker.upper(): float(r.chg_24h_pct) for r in res.universe}


def _group_table_lines(title: str, tickers: Sequence[str], chg: Dict[str, float]) -> List[str]:
    lines: List[str] = [f"{title} [{len(tickers)}]:", ""]
    if not tickers:
        lines.append("(none)")
        return lines
    col_a, col_b = "Symbol", "24hChg%"
    rows = [(t, f"{chg.get(t, float('nan')):.2f}") for t in tickers]
    w0 = max(len(col_a), max((len(r[0]) for r in rows), default=0))
    w1 = max(len(col_b), max((len(r[1]) for r in rows), default=0))
    lines.append(f"{col_a:<{w0}}  {col_b:>{w1}}")
    lines.append(f"{'-' * w0}  {'-' * w1}")
    for t, p in rows:
        lines.append(f"{t:<{w0}}  {p:>{w1}}")
    return lines


def write_median_filter_output_txt(
    path: str,
    *,
    res: MedianSplitResult,
    longs: List[str],
    shorts: List[str],
    long_title: str,
    short_title: str,
    header_lines: List[str],
) -> None:
    chg = _chg_pct_by_ticker(res)
    body: List[str] = []
    body.extend(header_lines)
    body.append("")
    body.extend(_group_table_lines(long_title, longs, chg))
    body.append("")
    body.extend(_group_table_lines(short_title, shorts, chg))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


def _split_csv(s: Optional[str]) -> List[str]:
    if not s:
        return []
    parts: List[str] = []
    for p in str(s).replace(";", ",").split(","):
        t = p.strip()
        if t:
            parts.append(t)
    return parts


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Median split: top N by OI (ex BTC/ETH, blacklist, optional OI Skew cap), "
        "split universe by 24h % change. "
        "Explicit --mode: directional = long top half / short bottom; sideways = long bottom / short top. "
        "Default --mode auto reads marketstate.json: Sideways Now -> directional split (for Directional Next); "
        "Directional Now -> sideways split (for Sideways Next)."
    )
    ap.add_argument(
        "--json-path",
        default=os.getenv("LISTINGTABLE_JSON", default_listingtable_json_path()),
        help="Path to listingtabledata.json (default: LISTINGTABLE_JSON env or repo default).",
    )
    ap.add_argument(
        "--marketstate-path",
        default=os.getenv("MARKETSTATE_JSON", default_marketstate_json_path()),
        help="Path to marketstate.json for --mode auto (default: MARKETSTATE_JSON env or repo default).",
    )
    ap.add_argument(
        "--mode",
        choices=("auto", "directional", "sideways"),
        default="auto",
        help="auto: map regime Now -> split for expected Next (Sideways Now->directional, Directional Now->sideways); "
        "directional/sideways: force split style (see description).",
    )
    ap.add_argument(
        "--top-n",
        type=int,
        default=_DEFAULT_TOP_N,
        help=f"Universe size by OI after exclusions (default {_DEFAULT_TOP_N}).",
    )
    ap.add_argument(
        "--exclude",
        default=DEFAULT_EXCLUDE_CSV,
        help="Comma-separated tickers to exclude (default BTC,ETH).",
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
    exclude = _split_csv(args.exclude)
    all_excluded = sorted(
        {e.strip().upper() for e in exclude if e and str(e).strip()} | set(TICKER_BLACKLIST)
    )
    max_skew: Optional[float] = float(args.max_oi_skew)
    if max_skew is not None and max_skew < 0:
        max_skew = None
    min_vol_24h: Optional[float] = float(args.min_vol_24h)
    if min_vol_24h is not None and min_vol_24h < 0:
        min_vol_24h = None

    market_regime: Optional[str] = None
    median_mode: MedianMode
    marketstate_abspath: Optional[str] = None

    if args.mode == "auto":
        ms_path = os.path.abspath(str(args.marketstate_path))
        marketstate_abspath = ms_path
        if not os.path.isfile(ms_path):
            print(
                f"error: --mode auto requires {ms_path} (run Vari Listings/marketstate.py first, "
                f"or pass --mode directional / --mode sideways).",
                file=sys.stderr,
            )
            return 2
        try:
            market_regime = read_24h_market_regime_from_marketstate_json(ms_path)
            median_mode = regime_to_median_mode(market_regime)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"error: failed to read market regime from {ms_path}: {e}", file=sys.stderr)
            return 2
    else:
        median_mode = args.mode  # type: ignore[assignment]

    res = get_median_groups_from_listingtable_json(
        json_path=str(args.json_path),
        top_n=int(args.top_n),
        exclude=exclude,
        max_oi_skew=max_skew,
        min_vol_24h=min_vol_24h,
    )
    longs, shorts = long_short_for_mode(res, median_mode)

    if args.print_json:
        out: Dict[str, Any] = {
            "json_path": os.path.abspath(str(args.json_path)),
            "top_n": int(args.top_n),
            "exclude": [e.strip().upper() for e in exclude],
            "blacklist": sorted(TICKER_BLACKLIST),
            "excluded_effective": all_excluded,
            "max_oi_skew": max_skew,
            "median_24h_chg_pct": res.median_24h_chg_pct,
            "median_mode": median_mode,
            "long": longs,
            "short": shorts,
        }
        if market_regime is not None:
            out["24h_market_regime"] = market_regime
        if marketstate_abspath is not None:
            out["marketstate_json"] = marketstate_abspath
        print(json.dumps(out, indent=2))
        return 0

    skew_note = f"; OI Skew ≤ {max_skew}" if max_skew is not None else "; OI Skew filter off"
    print(
        f"Filtering: Top {int(args.top_n)} by OI (excluded: {', '.join(all_excluded)}){skew_note}"
    )
    print(f"Median 24hChg%: {res.median_24h_chg_pct:.2f}%")
    if market_regime is not None:
        print(f"Current Market Regime: {format_regime_now_to_next(market_regime)}")
    print()

    if median_mode == "directional":
        long_title = "Long top half median 24hChg%"
        short_title = "Short btm half median 24hChg%"
    else:
        long_title = "Long btm half median 24hChg%"
        short_title = "Short top half median 24hChg%"

    print(f"{long_title} [{len(longs)}]:")
    print(",".join(longs))
    print()
    print(f"{short_title} [{len(shorts)}]:")
    print(",".join(shorts))

    out_path = os.path.join(_repo_root_from_here(), "Varibot", "median_filter_output.txt")
    file_header: List[str] = [
        f"Written at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"Filtering: Top {int(args.top_n)} by OI (excluded: {', '.join(all_excluded)}){skew_note}",
        f"Median 24hChg%: {res.median_24h_chg_pct:.2f}%",
    ]
    if market_regime is not None:
        file_header.append(f"Current Market Regime: {format_regime_now_to_next(market_regime)}")
    try:
        write_median_filter_output_txt(
            out_path,
            res=res,
            longs=longs,
            shorts=shorts,
            long_title=long_title,
            short_title=short_title,
            header_lines=file_header,
        )
    except OSError as e:
        print(f"warning: could not write {out_path}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
