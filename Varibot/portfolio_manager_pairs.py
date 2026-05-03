from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

Side = Literal["L", "S"]

PAIR_TP_THRESHOLD_PCT_DEFAULT: float = 1.0


@dataclass(frozen=True)
class PositionRow:
    ticker: str
    side: Side
    qty: float
    value_usd: float
    upnl_usd: float


def _first_float(d: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k not in d:
            continue
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _nested_float(d: Dict[str, Any], path: Sequence[str]) -> Optional[float]:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    try:
        return float(cur)
    except Exception:
        return None


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _instrument_label(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    if isinstance(inst, str) and inst.strip():
        return inst.strip().upper()

    pos_info = p.get("position_info")
    if isinstance(pos_info, dict):
        inst2 = pos_info.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()

    for k in ("instrument_name", "instrument_id", "instrumentId", "symbol"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()

    underlying = p.get("underlying")
    if isinstance(underlying, str) and underlying.strip():
        return underlying.strip().upper()
    return "UNKNOWN"


def _position_qty(p: Dict[str, Any]) -> Optional[float]:
    q = _first_float(p, ("qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"))
    if q is not None:
        return q
    return _nested_float(p, ("position_info", "qty"))


def _position_value_usd_abs(p: Dict[str, Any]) -> Optional[float]:
    v = _first_float(p, ("value", "position_value", "notional", "notional_value", "usd_value"))
    if v is None:
        v = _nested_float(p, ("position_info", "value"))
        if v is None:
            v = _nested_float(p, ("position_info", "position_value"))
    if v is None:
        return None
    try:
        return abs(float(v))
    except Exception:
        return None


def _position_upnl_usd(p: Dict[str, Any]) -> Optional[float]:
    v = _first_float(p, ("unrealized_pnl", "u_pnl", "upnl", "unrealizedPnl"))
    if v is not None:
        return v
    return _nested_float(p, ("position_info", "unrealized_pnl"))


def positions_to_rows(positions_raw: Any) -> List[PositionRow]:
    """
    Best-effort parse of /api/positions response into normalized rows.
    Only includes rows with a non-zero qty, and with both value_usd and upnl_usd present.
    """
    out: List[PositionRow] = []
    for p in _positions_list(positions_raw):
        sym = _instrument_label(p).strip().upper()
        if not sym or sym == "UNKNOWN":
            continue
        qty = _position_qty(p)
        if qty is None or abs(float(qty)) <= 1e-12:
            continue
        value = _position_value_usd_abs(p)
        upnl = _position_upnl_usd(p)
        if value is None or upnl is None:
            continue
        side: Side = "L" if float(qty) > 0 else "S"
        out.append(
            PositionRow(
                ticker=sym,
                side=side,
                qty=float(qty),
                value_usd=float(value),
                upnl_usd=float(upnl),
            )
        )
    return out


@dataclass(frozen=True)
class PairCandidate:
    long_ticker: str
    short_ticker: str
    combined_upnl_usd: float
    combined_value_usd: float
    combined_upnl_pct: float


def _pair_upnl_pct(*, upnl_a: float, value_a: float, upnl_b: float, value_b: float) -> Optional[float]:
    denom = float(value_a) + float(value_b)
    if denom <= 0:
        return None
    return (float(upnl_a) + float(upnl_b)) / denom * 100.0


def select_pairs_greedy_grid(
    *,
    rows: Sequence[PositionRow],
    threshold_pct: float,
    disallow: Optional[Set[str]] = None,
) -> List[PairCandidate]:
    """
    Select (long, short) pairs to close using an array/grid precompute + greedy matching.

    Eligibility:
    - winners: rows with upnl_usd > 0
    - match each winner with an opposite-side row (can be negative) such that combined uPnL% clears threshold
    - ticker not in disallow (if provided)

    Greedy rule:
    - iterate winners by best uPnL (desc), regardless of side
    - for each winner, scan the opposite side by worst uPnL (asc) and pick the first that meets threshold
    - remove both from eligibility and continue (maximize number of pairs for churn)
    """
    dis = {t.strip().upper() for t in (disallow or set()) if isinstance(t, str)}

    eligible = [r for r in rows if r.ticker not in dis]
    winners = [r for r in eligible if float(r.upnl_usd) > 0]
    if not winners:
        return []

    winners.sort(key=lambda r: float(r.upnl_usd), reverse=True)

    longs_all = [r for r in eligible if r.side == "L"]
    shorts_all = [r for r in eligible if r.side == "S"]
    if not longs_all or not shorts_all:
        return []

    # For matching we always scan the opposite side by worst uPnL first.
    longs_all.sort(key=lambda r: float(r.upnl_usd))   # worst first
    shorts_all.sort(key=lambda r: float(r.upnl_usd))  # worst first

    # Index by ticker to enforce non-overlapping pairs.
    used: Set[str] = set()
    out: List[PairCandidate] = []

    for w in winners:
        if w.ticker in used:
            continue
        opp = shorts_all if w.side == "L" else longs_all
        pick: Optional[PositionRow] = None
        pick_pct: Optional[float] = None

        for cand in opp:
            if cand.ticker in used:
                continue
            p = _pair_upnl_pct(upnl_a=w.upnl_usd, value_a=w.value_usd, upnl_b=cand.upnl_usd, value_b=cand.value_usd)
            if p is None:
                continue
            if float(p) >= float(threshold_pct):
                pick = cand
                pick_pct = float(p)
                break

        if pick is None or pick_pct is None:
            continue

        used.add(w.ticker)
        used.add(pick.ticker)

        if w.side == "L":
            long_t, short_t = w.ticker, pick.ticker
        else:
            long_t, short_t = pick.ticker, w.ticker

        combined_upnl = float(w.upnl_usd) + float(pick.upnl_usd)
        combined_value = float(w.value_usd) + float(pick.value_usd)
        out.append(
            PairCandidate(
                long_ticker=long_t,
                short_ticker=short_t,
                combined_upnl_usd=combined_upnl,
                combined_value_usd=combined_value,
                combined_upnl_pct=float(pick_pct),
            )
        )
    return out


def scan_best_winner_opposite_pair(
    *,
    rows: Sequence[PositionRow],
    disallow: Optional[Set[str]] = None,
) -> Optional[PairCandidate]:
    """
    Among every winner×opposite-side pairing (same pools as ``select_pairs_greedy_grid``),
    return the pair with the highest combined uPnL%, regardless of threshold.

    Used when no pair clears the threshold: shows how close the book was to an exit.
    Returns None if there is no positive-uPnL leg or no both-side liquidity to pair.
    """
    dis = {t.strip().upper() for t in (disallow or set()) if isinstance(t, str)}
    eligible = [r for r in rows if r.ticker not in dis]
    winners = [r for r in eligible if float(r.upnl_usd) > 0]
    if not winners:
        return None

    longs_all = [r for r in eligible if r.side == "L"]
    shorts_all = [r for r in eligible if r.side == "S"]
    if not longs_all or not shorts_all:
        return None

    best: Optional[PairCandidate] = None
    for w in winners:
        opp_rows = shorts_all if w.side == "L" else longs_all
        for cand in opp_rows:
            p = _pair_upnl_pct(
                upnl_a=w.upnl_usd,
                value_a=w.value_usd,
                upnl_b=cand.upnl_usd,
                value_b=cand.value_usd,
            )
            if p is None:
                continue
            pf = float(p)
            comb_u = float(w.upnl_usd) + float(cand.upnl_usd)
            comb_v = float(w.value_usd) + float(cand.value_usd)
            if w.side == "L":
                long_t, short_t = w.ticker, cand.ticker
            else:
                long_t, short_t = cand.ticker, w.ticker

            if best is None:
                best = PairCandidate(
                    long_ticker=long_t,
                    short_ticker=short_t,
                    combined_upnl_usd=comb_u,
                    combined_value_usd=comb_v,
                    combined_upnl_pct=pf,
                )
                continue
            if pf > float(best.combined_upnl_pct) + 1e-12:
                best = PairCandidate(
                    long_ticker=long_t,
                    short_ticker=short_t,
                    combined_upnl_usd=comb_u,
                    combined_value_usd=comb_v,
                    combined_upnl_pct=pf,
                )
            elif abs(pf - float(best.combined_upnl_pct)) <= 1e-12 and comb_u > float(best.combined_upnl_usd):
                best = PairCandidate(
                    long_ticker=long_t,
                    short_ticker=short_t,
                    combined_upnl_usd=comb_u,
                    combined_value_usd=comb_v,
                    combined_upnl_pct=pf,
                )
    return best


def filter_replacements(
    *,
    longs: Sequence[str],
    shorts: Sequence[str],
    disallow: Set[str],
    need_each_side: int,
) -> Tuple[List[str], List[str]]:
    dis = {t.strip().upper() for t in disallow if isinstance(t, str)}
    out_l: List[str] = []
    out_s: List[str] = []
    for t in longs:
        sym = str(t).strip().upper()
        if not sym or sym in dis:
            continue
        if sym not in out_l:
            out_l.append(sym)
        if len(out_l) >= int(need_each_side):
            break
    for t in shorts:
        sym = str(t).strip().upper()
        if not sym or sym in dis:
            continue
        if sym not in out_s:
            out_s.append(sym)
        if len(out_s) >= int(need_each_side):
            break
    return out_l, out_s


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Select PM close pairs from a mock positions table or raw API JSON.")
    ap.add_argument(
        "--threshold-pct",
        type=float,
        default=PAIR_TP_THRESHOLD_PCT_DEFAULT,
        help=f"Combined uPnL% threshold vs combined value (default {PAIR_TP_THRESHOLD_PCT_DEFAULT:g}).",
    )
    ap.add_argument("--input", required=True, help="Path to JSON file: either raw /api/positions or rows list.")
    args = ap.parse_args()

    with open(str(args.input), "r", encoding="utf-8") as f:
        payload = json.load(f)

    rows: List[PositionRow] = []
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "ticker" in payload[0]:
        for r in payload:
            rows.append(
                PositionRow(
                    ticker=str(r.get("ticker") or "").strip().upper(),
                    side=("L" if str(r.get("side") or "").strip().upper() in ("L", "LONG") else "S"),
                    qty=float(r.get("qty") or 0.0),
                    value_usd=float(r.get("value_usd") or r.get("value") or 0.0),
                    upnl_usd=float(r.get("upnl_usd") or r.get("upnl") or 0.0),
                )
            )
    else:
        rows = positions_to_rows(payload)

    pairs = select_pairs_greedy_grid(rows=rows, threshold_pct=float(args.threshold_pct))
    out = [
        {
            "long": p.long_ticker,
            "short": p.short_ticker,
            "combined_upnl_usd": p.combined_upnl_usd,
            "combined_value_usd": p.combined_value_usd,
            "combined_upnl_pct": p.combined_upnl_pct,
        }
        for p in pairs
    ]
    print(json.dumps({"pairs": out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

