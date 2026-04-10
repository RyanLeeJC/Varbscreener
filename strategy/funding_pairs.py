from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

STRATEGY_NAME: str = "funding_pairs"

# Pairing rules / constants
MAX_PAIR_COUNT: int = 10
# % of (portfolio_value_usd × leverage) allocated to each pair (long+short legs combined).
# Each leg receives half: per_order_usd = (pv×lev×FUNDING_PAIR_MAX_IM_PCT/100) / 2.
# See im_target_pct_for_funding_pairs_multimarket() for mapping to multimarketorder's --im-target-pct.
FUNDING_PAIR_MAX_IM_PCT: float = 7.0
TOP_N_BY_VOL_24H: int = 80
MAX_ABS_24H_MOVE_PCT: float = 10.0
MAX_ABS_ANN_FUNDINGRATE_PCT: float = 50.0
MIN_ABS_FUNDINGRATE_DIFF_PCT: float = 5.0
PAIR_MAX_AGE_S: float = 3.0 * 3600.0
PAIR_TP_UPNL_PCT: float = 2.0

# Copied from strategy/revert_median.py (and also present in other strategy modules).
TICKER_BLACKLIST: frozenset[str] = frozenset({"BERA","XPL", "ETC", "PAXG", "XAUT", "RIVER", "EDGE", "BASED", "VVV", "IP", "STO", "TRADOOR", "FIO", "ENJ", "MAGMA", "ARIA", "FARTCOIN"})


def _repo_root_from_here() -> str:
    # This file lives in: <repo_root>/strategy/funding_pairs.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def default_state_json_path() -> str:
    return os.path.join(os.path.dirname(__file__), "funding_pairs_state.json")


def im_target_pct_for_funding_pairs_multimarket(n_long: int, n_short: int) -> float:
    """
    multimarketorder computes:
      per_order_usd = (portfolio_value_usd × leverage × im_target_pct / 100) / (n_long + n_short)

    For funding pairs we want each pair to use FUNDING_PAIR_MAX_IM_PCT of max notional (pv×lev),
    split evenly across the two legs:
      per_leg_usd = (portfolio_value_usd × leverage × FUNDING_PAIR_MAX_IM_PCT / 100) / 2

    Solving for im_target_pct:
      im_target_pct = (n_long + n_short) × FUNDING_PAIR_MAX_IM_PCT / 2
    (e.g. 5 pairs → 10 orders → 75 when FUNDING_PAIR_MAX_IM_PCT=15, so each leg is 7.5% of max IM.)
    """
    n = int(n_long) + int(n_short)
    if n <= 0:
        raise ValueError("im_target_pct_for_funding_pairs_multimarket requires at least one order")
    return float(n) * float(FUNDING_PAIR_MAX_IM_PCT) / 2.0


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


def _parse_pct_field(v: Any) -> Optional[float]:
    """
    Parses values like "-1.23%" or 7.0016 into "percentage points".
    Returns None if missing/unparseable.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _load_listing_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("listings"), list):
        return [x for x in payload["listings"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("Unexpected listingtable payload shape (expected dict with 'listings' or a list).")


@dataclass(frozen=True)
class ListingRow:
    ticker: str
    vol_24h: float
    ann_fundingrate_pct: float
    chg_24h_pct: float


def _to_row(d: Dict[str, Any]) -> Optional[ListingRow]:
    t = _as_str(d.get("vari_ticker") or d.get("ticker") or d.get("symbol"))
    if not t:
        return None
    # listingtable payloads have evolved; be permissive in accepted field names.
    vol = _as_float(
        d.get("vol_24h")
        if "vol_24h" in d
        else (
            d.get("volume_24h")
            or d.get("vol_24h_usd")
            or d.get("volume_24h_usd")
            or d.get("total_volume")
            or d.get("vol")
        )
    )
    if vol is None or float(vol) <= 0:
        return None
    afr = _parse_pct_field(
        d.get("ann_fundingrate")
        or d.get("ann_fundingrate_pct")
        or d.get("annualized_funding_rate")
        or d.get("ann_funding_rate")
        or d.get("funding_rate_annualized")
    )
    if afr is None:
        return None
    chg = _parse_pct_field(
        d.get("price_change_24h_pct")
        or d.get("price_change_percentage_24h")
        or d.get("price_change_percentage_24h_in_currency")
        or d.get("price_change_24h")
        or d.get("chg_24h_pct")
    )
    if chg is None:
        return None
    return ListingRow(
        ticker=str(t).upper(),
        vol_24h=float(vol),
        ann_fundingrate_pct=float(afr),
        chg_24h_pct=float(chg),
    )


def _eligible_universe(
    listing_rows: Sequence[Dict[str, Any]],
    *,
    exclude: Set[str],
    top_n_by_vol: int,
) -> List[ListingRow]:
    items: List[ListingRow] = []
    for d in listing_rows:
        r = _to_row(d)
        if r is None:
            continue
        if r.ticker in exclude:
            continue
        if abs(float(r.chg_24h_pct)) > float(MAX_ABS_24H_MOVE_PCT):
            continue
        if abs(float(r.ann_fundingrate_pct)) > float(MAX_ABS_ANN_FUNDINGRATE_PCT):
            continue
        items.append(r)

    items.sort(key=lambda x: (x.vol_24h, x.ticker), reverse=True)
    # Take top N by volume after filters (user spec).
    return items[: int(top_n_by_vol)]


@dataclass(frozen=True)
class Pair:
    long: str
    short: str
    long_afr: float
    short_afr: float

    @property
    def afr_diff(self) -> float:
        return abs(float(self.short_afr) - float(self.long_afr))


def _build_pairs(
    universe: Sequence[ListingRow],
    *,
    target_pairs: int,
    disallow: Set[str],
) -> List[Pair]:
    """
    Build up to target_pairs disjoint (no repeated tickers) pairs:
      - long: positive ann_fundingrate
      - short: negative ann_fundingrate
      - |afr_short - afr_long| >= MIN_ABS_FUNDINGRATE_DIFF_PCT
    """
    longs = [r for r in universe if float(r.ann_fundingrate_pct) > 0 and r.ticker not in disallow]
    shorts = [r for r in universe if float(r.ann_fundingrate_pct) < 0 and r.ticker not in disallow]

    # Greedy: prefer larger funding diff first, with volume as tie-breaker.
    candidates: List[Tuple[float, float, str, str, float, float]] = []
    for l in longs:
        for s in shorts:
            if l.ticker == s.ticker:
                continue
            diff = abs(float(s.ann_fundingrate_pct) - float(l.ann_fundingrate_pct))
            if diff < float(MIN_ABS_FUNDINGRATE_DIFF_PCT):
                continue
            score = diff
            vol_score = min(float(l.vol_24h), float(s.vol_24h))
            candidates.append((score, vol_score, l.ticker, s.ticker, float(l.ann_fundingrate_pct), float(s.ann_fundingrate_pct)))
    candidates.sort(reverse=True)

    used: Set[str] = set()
    out: List[Pair] = []
    for _score, _vol, l_t, s_t, l_afr, s_afr in candidates:
        if len(out) >= int(target_pairs):
            break
        if l_t in used or s_t in used:
            continue
        if l_t in disallow or s_t in disallow:
            continue
        used.add(l_t)
        used.add(s_t)
        out.append(Pair(long=l_t, short=s_t, long_afr=l_afr, short_afr=s_afr))
    return out


def _read_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: str, d: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _now() -> float:
    return float(time.time())


def _state_pairs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    pairs = d.get("pairs")
    if isinstance(pairs, list):
        return [x for x in pairs if isinstance(x, dict)]
    return []


def _tickers_in_state_pairs(d: Dict[str, Any]) -> Set[str]:
    used: Set[str] = set()
    for p in _state_pairs(d):
        for k in ("long", "short"):
            v = p.get(k)
            if isinstance(v, str) and v.strip():
                used.add(v.strip().upper())
    return used


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,  # unused for now (kept for interface compatibility)
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """
    Strategy loader interface used by Varibot when flat.

    IMPORTANT: Varibot's default orchestrator only calls strategy selection when *flat*.
    This module also maintains a state file (pair slots + opened timestamps) so a future
    pair-manager loop can rotate/TP-close individual pairs.
    """
    state_path = default_state_json_path()
    state = _read_state(state_path)

    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)

    exclude: Set[str] = set(TICKER_BLACKLIST)
    # Always exclude BTC/ETH for this strategy (keeps behavior aligned with your existing strategies).
    exclude |= {"BTC", "ETH"}

    universe = _eligible_universe(rows, exclude=exclude, top_n_by_vol=int(top_n) if top_n is not None else TOP_N_BY_VOL_24H)

    # When flat, seed up to MAX_PAIR_COUNT disjoint pairs (may be fewer if the universe is thin).
    disallow: Set[str] = set(exclude)
    new_pairs = _build_pairs(universe, target_pairs=MAX_PAIR_COUNT, disallow=disallow)
    if not new_pairs:
        now0 = _now()
        state_empty: Dict[str, Any] = {
            "strategy": STRATEGY_NAME,
            "written_at_unix": now0,
            "pair_max_age_s": PAIR_MAX_AGE_S,
            "pair_tp_upnl_pct": PAIR_TP_UPNL_PCT,
            "min_abs_fundingrate_diff_pct": MIN_ABS_FUNDINGRATE_DIFF_PCT,
            "filters": {
                "top_n_by_vol_24h": int(top_n) if top_n is not None else TOP_N_BY_VOL_24H,
                "max_abs_24h_move_pct": MAX_ABS_24H_MOVE_PCT,
                "max_abs_ann_fundingrate_pct": MAX_ABS_ANN_FUNDINGRATE_PCT,
                "funding_pair_max_im_pct": FUNDING_PAIR_MAX_IM_PCT,
            },
            "pairs": [],
        }
        _write_state(state_path, state_empty)
        meta_empty: Dict[str, Any] = {
            "strategy": STRATEGY_NAME,
            "listing_json": os.path.abspath(str(listing_json)),
            "marketstate_json": os.path.abspath(str(marketstate_json)) if marketstate_json else None,
            "pair_count": 0,
            "max_pair_count": MAX_PAIR_COUNT,
            "reason": "no_eligible_pairs",
            "universe_top_n_by_vol": int(top_n) if top_n is not None else TOP_N_BY_VOL_24H,
            "state_json": os.path.abspath(state_path),
            "funding_pair_max_im_pct": FUNDING_PAIR_MAX_IM_PCT,
            "note": (
                "No funding-opposite pairs met filters; relax MIN_ABS_FUNDINGRATE_DIFF_PCT / volume filters or wait."
            ),
        }
        return [], [], meta_empty

    opened = _now()
    state_out: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "written_at_unix": opened,
        "pair_max_age_s": PAIR_MAX_AGE_S,
        "pair_tp_upnl_pct": PAIR_TP_UPNL_PCT,
        "min_abs_fundingrate_diff_pct": MIN_ABS_FUNDINGRATE_DIFF_PCT,
        "filters": {
            "top_n_by_vol_24h": int(top_n) if top_n is not None else TOP_N_BY_VOL_24H,
            "max_abs_24h_move_pct": MAX_ABS_24H_MOVE_PCT,
            "max_abs_ann_fundingrate_pct": MAX_ABS_ANN_FUNDINGRATE_PCT,
            "funding_pair_max_im_pct": FUNDING_PAIR_MAX_IM_PCT,
        },
        "pairs": [
            {
                "slot": i + 1,
                "long": p.long,
                "short": p.short,
                "long_afr": p.long_afr,
                "short_afr": p.short_afr,
                "afr_diff": p.afr_diff,
                "opened_unix": opened,
                "closed_unix": None,
                "last_seen": {"combined_upnl_usd": None, "combined_notional_usd": None, "combined_upnl_pct": None},
            }
            for i, p in enumerate(new_pairs)
        ],
    }
    _write_state(state_path, state_out)

    longs = [p.long for p in new_pairs]
    shorts = [p.short for p in new_pairs]
    n_long = len(longs)
    n_short = len(shorts)
    meta: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "listing_json": os.path.abspath(str(listing_json)),
        "marketstate_json": os.path.abspath(str(marketstate_json)) if marketstate_json else None,
        "pair_count": len(new_pairs),
        "max_pair_count": MAX_PAIR_COUNT,
        "universe_top_n_by_vol": int(top_n) if top_n is not None else TOP_N_BY_VOL_24H,
        "filters": {
            "abs_price_change_24h_pct_lte": MAX_ABS_24H_MOVE_PCT,
            "abs_ann_fundingrate_lte": MAX_ABS_ANN_FUNDINGRATE_PCT,
            "min_abs_ann_fundingrate_diff": MIN_ABS_FUNDINGRATE_DIFF_PCT,
            "funding_pair_max_im_pct": FUNDING_PAIR_MAX_IM_PCT,
        },
        "pairs": [{"slot": i + 1, "long": p.long, "short": p.short, "afr_diff": p.afr_diff} for i, p in enumerate(new_pairs)],
        "state_json": os.path.abspath(state_path),
        "long_count": n_long,
        "short_count": n_short,
        "im_target_pct_for_multimarket": im_target_pct_for_funding_pairs_multimarket(n_long, n_short),
        "note": (
            f"This strategy seeds up to {MAX_PAIR_COUNT} funding-opposite pairs when flat "
            f"(each pair uses {FUNDING_PAIR_MAX_IM_PCT:g}% of max IM (pv×leverage), split across two legs). "
            "Pair-level TP/rotation uses the funding_pairs manager when positions exist."
        ),
    }
    return longs, shorts, meta


def _pos_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _inst_sym(p: Dict[str, Any]) -> str:
    # Match Varibot/positions._instrument_label: omni often nests instrument under position_info.
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
    for k in ("underlying", "symbol", "asset"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return "UNKNOWN"


def _first_float(p: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for k in keys:
        if k not in p:
            continue
        v = p.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _nested_float(p: Dict[str, Any], path2: Sequence[str]) -> Optional[float]:
    cur: Any = p
    for k in path2:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    try:
        return float(cur)
    except Exception:
        return None


def _metrics_by_underlying_from_positions(positions_raw: Any) -> Dict[str, Dict[str, float]]:
    """
    Map upper underlying -> {upnl_usd, notional_usd}. Skips rows missing uPNL or notional in the API payload.
    """
    by_sym: Dict[str, Dict[str, float]] = {}
    for p in _pos_list(positions_raw):
        sym = _inst_sym(p)
        upnl = _first_float(p, ["unrealized_pnl", "u_pnl", "upnl", "unrealizedPnl"])
        value = _first_float(p, ["value", "position_value", "notional", "notional_value", "usd_value"])
        if value is None:
            value = _nested_float(p, ["position_info", "value"])
        if upnl is None or value is None:
            continue
        by_sym[sym] = {"upnl_usd": float(upnl), "notional_usd": abs(float(value))}
    return by_sym


def pair_interval_status(
    *,
    positions_raw: Any,
    state_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    One row per slot in funding_pairs state with combined uPnL / notional / % (for terminal visibility).
    Includes rows when a leg is missing from GET /api/positions (partial data).
    """
    path = state_json_path or default_state_json_path()
    state = _read_state(path)
    pairs = _state_pairs(state)
    by_sym = _metrics_by_underlying_from_positions(positions_raw)
    now = _now()
    rows: List[Dict[str, Any]] = []
    if not pairs:
        return {
            "state_json": os.path.abspath(path),
            "pair_tp_upnl_pct": float(PAIR_TP_UPNL_PCT),
            "pair_max_age_s": float(PAIR_MAX_AGE_S),
            "rows": [],
            "computed_at_unix": now,
            "reason": "no_pairs_in_state",
        }

    for p in pairs:
        long_t = _as_str(p.get("long"))
        short_t = _as_str(p.get("short"))
        slot = p.get("slot")
        opened = _as_float(p.get("opened_unix"))
        age_s = (now - float(opened)) if opened is not None else None

        if not long_t or not short_t:
            rows.append(
                {
                    "slot": slot,
                    "long": long_t or "?",
                    "short": short_t or "?",
                    "combined_upnl_usd": None,
                    "combined_notional_usd": None,
                    "combined_upnl_pct": None,
                    "age_s": age_s,
                    "would_close_tp": False,
                    "would_close_age": False,
                    "missing_legs": "incomplete_state",
                }
            )
            continue

        lu, su = long_t.upper(), short_t.upper()
        l = by_sym.get(lu)
        s = by_sym.get(su)
        miss: List[str] = []
        if not l:
            miss.append("long")
        if not s:
            miss.append("short")

        if miss:
            rows.append(
                {
                    "slot": slot,
                    "long": lu,
                    "short": su,
                    "combined_upnl_usd": None,
                    "combined_notional_usd": None,
                    "combined_upnl_pct": None,
                    "age_s": age_s,
                    "would_close_tp": False,
                    "would_close_age": False,
                    "missing_legs": ",".join(miss),
                }
            )
            continue

        combined_upnl = float(l["upnl_usd"]) + float(s["upnl_usd"])
        combined_notional = float(l["notional_usd"]) + float(s["notional_usd"])
        upnl_pct = None if combined_notional <= 0 else (combined_upnl / combined_notional) * 100.0
        tp_hit = (upnl_pct is not None) and (float(upnl_pct) >= float(PAIR_TP_UPNL_PCT))
        age_hit = (age_s is not None) and (float(age_s) >= float(PAIR_MAX_AGE_S))
        rows.append(
            {
                "slot": slot,
                "long": lu,
                "short": su,
                "combined_upnl_usd": combined_upnl,
                "combined_notional_usd": combined_notional,
                "combined_upnl_pct": upnl_pct,
                "age_s": age_s,
                "would_close_tp": bool(tp_hit),
                "would_close_age": bool(age_hit),
                "missing_legs": None,
            }
        )

    return {
        "state_json": os.path.abspath(path),
        "pair_tp_upnl_pct": float(PAIR_TP_UPNL_PCT),
        "pair_max_age_s": float(PAIR_MAX_AGE_S),
        "rows": rows,
        "computed_at_unix": now,
    }


def format_pair_interval_log_lines(snapshot: Dict[str, Any]) -> List[str]:
    """Human-readable lines for Varibot _log (table + threshold reminder)."""
    lines: List[str] = []
    tp_pct = float(snapshot.get("pair_tp_upnl_pct") or PAIR_TP_UPNL_PCT)
    max_age = float(snapshot.get("pair_max_age_s") or PAIR_MAX_AGE_S)
    rows_in = snapshot.get("rows")
    reason = snapshot.get("reason")
    lines.append(
        f"funding_pairs manager: per-pair uPnL (close if >={tp_pct:g}% of pair notional OR age >={max_age/3600:g}h)"
    )
    if reason == "no_pairs_in_state":
        lines.append("  (no rows: funding_pairs_state.json has no pair slots)")
        return lines
    if not isinstance(rows_in, list) or not rows_in:
        lines.append("  (no pair rows)")
        return lines

    def fmt_usd(v: Optional[float]) -> str:
        if v is None:
            return "-"
        sign = "+" if v >= 0 else ""
        return f"{sign}${v:.2f}"

    def fmt_pct(v: Optional[float]) -> str:
        if v is None:
            return "-"
        sign = "+" if v >= 0 else ""
        return f"({sign}{v:.2f}%)"

    def fmt_age(age: Any) -> str:
        if age is None:
            return "-"
        try:
            sec = float(age)
        except Exception:
            return "-"
        if sec < 3600:
            return f"{int(sec // 60)}m"
        return f"{sec / 3600:.1f}h"

    table: List[List[str]] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        slot = r.get("slot")
        pair_no = f"#{slot}" if slot is not None else "#?"
        legs = f"Long {r.get('long')}/ Short {r.get('short')}"
        cu = r.get("combined_upnl_usd")
        cp = r.get("combined_upnl_pct")
        miss = r.get("missing_legs")
        if miss:
            if miss == "incomplete_state":
                mid = "[state: missing long/short]"
            else:
                mid = f"[no position row for leg(s): {miss}]"
        else:
            mid = (
                f"{fmt_usd(None if cu is None else float(cu))} {fmt_pct(None if cp is None else float(cp))}"
            ).strip()
        flags: List[str] = []
        if r.get("would_close_tp"):
            flags.append("TP")
        if r.get("would_close_age"):
            flags.append("MAX_AGE")
        flag_s = f" [{'/'.join(flags)}]" if flags else ""
        age_s = fmt_age(r.get("age_s"))
        table.append([pair_no, legs, mid, f"age={age_s}{flag_s}"])

    cols = ["Pair", "Long / Short", "Pair uPnL (notional %)", "Age / flags"]
    widths = [len(c) for c in cols]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(parts: List[str]) -> str:
        return "  ".join(parts[i].ljust(widths[i]) for i in range(len(parts)))

    lines.append(line(cols))
    lines.append(line(["-" * w for w in widths]))
    for row in table:
        lines.append(line(row))
    return lines


def desired_actions_from_positions(
    *,
    positions_raw: Any,
    state_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes which pair slots should be closed based on:
      - combined uPNL% >= PAIR_TP_UPNL_PCT
      - age >= PAIR_MAX_AGE_S

    This does NOT place orders; it returns a plan so an orchestrator script can:
      - close reduce-only on the two legs
      - open a replacement pair for that slot
      - update state.json accordingly
    """
    path = state_json_path or default_state_json_path()
    state = _read_state(path)
    pairs = _state_pairs(state)
    if not pairs:
        return {"state_json": os.path.abspath(path), "pairs_to_close": [], "reason": "no_pairs_in_state"}

    by_sym = _metrics_by_underlying_from_positions(positions_raw)

    now = _now()
    pairs_to_close: List[Dict[str, Any]] = []
    for p in pairs:
        long_t = _as_str(p.get("long"))
        short_t = _as_str(p.get("short"))
        if not long_t or not short_t:
            continue
        opened = _as_float(p.get("opened_unix"))
        age_s = (now - float(opened)) if opened is not None else None

        l = by_sym.get(long_t.upper())
        s = by_sym.get(short_t.upper())
        if not l or not s:
            continue
        combined_upnl = float(l["upnl_usd"]) + float(s["upnl_usd"])
        combined_notional = float(l["notional_usd"]) + float(s["notional_usd"])
        upnl_pct = None if combined_notional <= 0 else (combined_upnl / combined_notional) * 100.0

        tp_hit = (upnl_pct is not None) and (float(upnl_pct) >= float(PAIR_TP_UPNL_PCT))
        age_hit = (age_s is not None) and (float(age_s) >= float(PAIR_MAX_AGE_S))
        if tp_hit or age_hit:
            pairs_to_close.append(
                {
                    "slot": p.get("slot"),
                    "long": long_t.upper(),
                    "short": short_t.upper(),
                    "combined_upnl_usd": combined_upnl,
                    "combined_notional_usd": combined_notional,
                    "combined_upnl_pct": upnl_pct,
                    "age_s": age_s,
                    "close_reason": ("tp" if tp_hit else "max_age"),
                }
            )

    return {"state_json": os.path.abspath(path), "pairs_to_close": pairs_to_close, "computed_at_unix": now}


def default_listingtable_json_path() -> str:
    return os.path.join(_repo_root_from_here(), "Vari Listings", "listingtabledata.json")


def pick_replacement_pair(
    *,
    listing_json: str,
    state_json_path: Optional[str] = None,
    top_n_by_vol: Optional[int] = None,
    extra_disallow: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Pick a single replacement pair (long/short) for the existing funding_pairs state.

    This is intended for an orchestrator (Varibot) to:
      - close a specific slot's two legs
      - open a fresh pair to refill that slot

    It does NOT mutate the state file.
    """
    st_path = os.path.abspath(state_json_path or default_state_json_path())
    state = _read_state(st_path)
    disallow: Set[str] = set(_tickers_in_state_pairs(state))
    disallow |= set(TICKER_BLACKLIST)
    disallow |= {"BTC", "ETH"}
    if extra_disallow:
        disallow |= {str(x).strip().upper() for x in extra_disallow if str(x).strip()}

    with open(str(listing_json), "r", encoding="utf-8") as f:
        payload = json.load(f)
    rows = _load_listing_rows(payload)

    universe = _eligible_universe(
        rows,
        exclude=disallow,
        top_n_by_vol=int(top_n_by_vol) if top_n_by_vol is not None else TOP_N_BY_VOL_24H,
    )
    pairs = _build_pairs(universe, target_pairs=1, disallow=disallow)
    if not pairs:
        raise ValueError(f"{STRATEGY_NAME}: no eligible replacement pair found (top={len(universe)} by vol).")
    p = pairs[0]
    return {
        "strategy": STRATEGY_NAME,
        "long": p.long,
        "short": p.short,
        "long_afr": p.long_afr,
        "short_afr": p.short_afr,
        "afr_diff": p.afr_diff,
        "universe_top_n_by_vol": int(top_n_by_vol) if top_n_by_vol is not None else TOP_N_BY_VOL_24H,
        "state_json": st_path,
    }


def replace_state_pair_slot(
    *,
    state_json_path: Optional[str],
    slot: int,
    new_long: str,
    new_short: str,
    opened_unix: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Update the funding_pairs state file in-place, replacing the pair for `slot`.
    Returns the updated state dict.
    """
    st_path = os.path.abspath(state_json_path or default_state_json_path())
    state = _read_state(st_path)
    pairs = _state_pairs(state)
    if not pairs:
        raise ValueError(f"{STRATEGY_NAME}: state has no pairs at {st_path}")

    now = _now()
    opened = float(opened_unix) if opened_unix is not None else now
    updated = False
    for p in pairs:
        try:
            s = int(p.get("slot"))
        except Exception:
            continue
        if s != int(slot):
            continue
        p["long"] = str(new_long).strip().upper()
        p["short"] = str(new_short).strip().upper()
        p["opened_unix"] = opened
        p["closed_unix"] = None
        p["last_seen"] = {"combined_upnl_usd": None, "combined_notional_usd": None, "combined_upnl_pct": None}
        updated = True
        break
    if not updated:
        raise ValueError(f"{STRATEGY_NAME}: slot {slot} not found in state at {st_path}")

    state["pairs"] = pairs
    state["written_at_unix"] = now
    _write_state(st_path, state)
    return state


def paper_run(
    *,
    listing_json: Optional[str] = None,
    top_n_by_vol: Optional[int] = None,
    positions_json: Optional[str] = None,
    state_json: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Paper-run helper:
      - seeds up to MAX_PAIR_COUNT pairs from listingtabledata.json (writes/overwrites state JSON)
      - optionally evaluates pair TP / max-age close signals from a saved positions snapshot

    This function does not place orders.
    """
    listing_path = os.path.abspath(listing_json or default_listingtable_json_path())
    longs, shorts, meta = pick_tickers(
        listing_json=listing_path,
        marketstate_json=None,
        top_n=int(top_n_by_vol) if top_n_by_vol is not None else None,
    )

    out: Dict[str, Any] = {"seed": {"long": longs, "short": shorts, "meta": meta}}
    st_path = os.path.abspath(state_json or str(meta.get("state_json") or default_state_json_path()))

    if positions_json:
        with open(os.path.abspath(str(positions_json)), "r", encoding="utf-8") as f:
            positions_raw = json.load(f)
        out["close_plan"] = desired_actions_from_positions(positions_raw=positions_raw, state_json_path=st_path)
    return out


def _cli() -> int:
    ap = argparse.ArgumentParser(
        description=f"Paper-run: seed up to {MAX_PAIR_COUNT} funding-opposite pairs (no orders unless --live)."
    )
    ap.add_argument(
        "--listing-json",
        default=default_listingtable_json_path(),
        help="Path to Vari Listings/listingtabledata.json (default: repo path).",
    )
    ap.add_argument(
        "--top-n-by-vol",
        type=int,
        default=None,
        help=f"Override top-N-by-vol universe size (default {TOP_N_BY_VOL_24H}).",
    )
    ap.add_argument(
        "--positions-json",
        default=None,
        help="Optional saved GET /api/positions JSON to evaluate TP/max-age close signals.",
    )
    ap.add_argument(
        "--state-json",
        default=None,
        help="Optional state JSON path (default: strategy/funding_pairs_state.json).",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="If set, invoke Varibot/multimarketorder.py with --live using the seeded pairs.",
    )
    ap.add_argument(
        "--usd",
        type=float,
        default=None,
        help="Optional fixed USD per order passed to multimarketorder.py (default: use IM-target sizing).",
    )
    ap.add_argument(
        "--im-target-pct",
        type=float,
        default=None,
        dest="im_target_pct",
        help=(
            "Optional override for multimarketorder --im-target-pct (ignored if --usd is set). "
            "If omitted, uses funding-pairs sizing: each pair uses FUNDING_PAIR_MAX_IM_PCT of (pv×lev), "
            "split across two legs (see im_target_pct_for_funding_pairs_multimarket)."
        ),
    )
    ap.add_argument(
        "--multi-script",
        default="multimarketorder.py",
        help="Varibot multi-order script to invoke when using --live (default: multimarketorder.py).",
    )
    ap.add_argument("--print-json", action="store_true", help="Print machine-readable JSON output.")
    args = ap.parse_args()

    out = paper_run(
        listing_json=str(args.listing_json),
        top_n_by_vol=args.top_n_by_vol,
        positions_json=args.positions_json,
        state_json=args.state_json,
    )

    seed = out.get("seed") if isinstance(out, dict) else {}
    longs = seed.get("long") if isinstance(seed, dict) else None
    shorts = seed.get("short") if isinstance(seed, dict) else None
    if not isinstance(longs, list):
        longs = []
    if not isinstance(shorts, list):
        shorts = []

    # Default remains paper/dry-run; only place orders when --live is set.
    if args.live:
        if not longs and not shorts:
            out["multimarket_invocation"] = {"skipped": True, "reason": "no_pairs"}
        else:
            repo_root = _repo_root_from_here()
            varibot_dir = os.path.join(repo_root, "Varibot")
            script_path = os.path.join(varibot_dir, str(args.multi_script))
            if not os.path.isfile(script_path):
                raise FileNotFoundError(f"Missing multi-order script: {script_path}")
            cmd: List[str] = [
                sys.executable,
                "-u",
                script_path,
                "--long",
                ",".join([str(x).strip().upper() for x in longs if str(x).strip()]),
                "--short",
                ",".join([str(x).strip().upper() for x in shorts if str(x).strip()]),
                "--live",
            ]
            if args.usd is not None:
                cmd += ["--usd", str(float(args.usd))]
            elif args.im_target_pct is not None:
                cmd += ["--im-target-pct", str(float(args.im_target_pct))]
            else:
                cmd += [
                    "--im-target-pct",
                    str(im_target_pct_for_funding_pairs_multimarket(len(longs), len(shorts))),
                ]
            rc = int(subprocess.call(cmd, cwd=varibot_dir))
            out["multimarket_invocation"] = {"cmd": cmd, "cwd": varibot_dir, "exit_code": rc}

    if args.print_json:
        print(json.dumps(out, indent=2, default=str))
    else:
        meta = seed.get("meta") if isinstance(seed, dict) else {}
        pairs = meta.get("pairs") if isinstance(meta, dict) else None
        print(f"Strategy: {STRATEGY_NAME} ({'live' if args.live else 'paper'})")
        if isinstance(pairs, list):
            for p in pairs:
                if not isinstance(p, dict):
                    continue
                ad = p.get("afr_diff")
                ad_s = f"{float(ad):.2f}%" if ad is not None else "-"
                print(f"Slot {p.get('slot')}: LONG {p.get('long')} / SHORT {p.get('short')} (diff={ad_s})")
        else:
            print(f"Longs: {', '.join(longs)}")
            print(f"Shorts: {', '.join(shorts)}")
        if isinstance(out.get("close_plan"), dict):
            plan = out["close_plan"]
            n = len(plan.get("pairs_to_close") or [])
            print(f"Pairs to close (from positions snapshot): {n}")
        if isinstance(out.get("multimarket_invocation"), dict):
            inv = out["multimarket_invocation"]
            print(f"multimarket exit_code: {inv.get('exit_code')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

