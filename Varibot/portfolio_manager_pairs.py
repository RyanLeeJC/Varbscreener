"""
Portfolio-manager helpers for ``varibot.py`` (invert_extreme / near_median paths).

This module was missing from the repo snapshot; implementations follow the call sites
and log strings in ``varibot.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from positions import _instrument_label

# Defaults referenced in varibot comments / argparse help.
PAIR_TP_THRESHOLD_PCT_DEFAULT: float = 5.0
LEG_TP_THRESHOLD_PCT_DEFAULT: float = 5.0
LEG_SL_THRESHOLD_PCT_DEFAULT: float = 10.0


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _position_qty(p: Dict[str, Any]) -> Optional[float]:
    for k in ("qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"):
        if k not in p:
            continue
        try:
            return float(p[k])
        except (TypeError, ValueError):
            continue
    pi = p.get("position_info")
    if isinstance(pi, dict) and "qty" in pi:
        try:
            return float(pi["qty"])
        except (TypeError, ValueError):
            pass
    return None


def _first_float(d: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k not in d:
            continue
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


@dataclass
class PositionRow:
    ticker: str
    side: str  # "L" (long) or "S" (short)
    upnl_pct: float
    upnl_usd: float
    value_usd: float


@dataclass
class CloseLeg:
    side: str
    ticker: str
    upnl_pct: float
    upnl_usd: float
    value_usd: float
    reason: str


@dataclass(frozen=True)
class PairCandidate:
    long_ticker: str
    short_ticker: str


def positions_to_rows(positions_raw: Any) -> List[PositionRow]:
    rows: List[PositionRow] = []
    for p in _positions_list(positions_raw):
        sym = _instrument_label(p).strip().upper()
        q = _position_qty(p)
        if not sym or q is None or abs(float(q)) <= 1e-12:
            continue
        side = "L" if float(q) > 0 else "S"
        upnl_usd = _first_float(
            p,
            (
                "unrealized_pnl",
                "unrealizedPnl",
                "u_pnl",
                "upnl",
                "unrealized_pnl_usd",
                "pnl",
            ),
        )
        if upnl_usd is None and isinstance(p.get("position_info"), dict):
            upnl_usd = _first_float(
                p["position_info"],
                ("unrealized_pnl", "unrealizedPnl", "u_pnl", "upnl"),
            )
        if upnl_usd is None:
            upnl_usd = 0.0

        value_usd = _first_float(
            p,
            ("value", "position_value", "notional", "notional_value", "usd_value", "positionValue"),
        )
        if value_usd is None and isinstance(p.get("position_info"), dict):
            value_usd = _first_float(p["position_info"], ("value", "notional"))
        if value_usd is None or float(value_usd) <= 1e-12:
            value_usd = max(abs(float(q)) * 1.0, 1e-12)

        upnl_pct = (float(upnl_usd) / float(value_usd)) * 100.0
        rows.append(
            PositionRow(
                ticker=sym,
                side=side,
                upnl_pct=float(upnl_pct),
                upnl_usd=float(upnl_usd),
                value_usd=float(value_usd),
            )
        )
    return rows


def select_legs_to_close(
    *,
    rows: Sequence[PositionRow],
    tp_pct: float,
    sl_pct: float,
) -> List[CloseLeg]:
    out: List[CloseLeg] = []
    for r in rows:
        if r.upnl_pct >= float(tp_pct):
            out.append(
                CloseLeg(
                    side=r.side,
                    ticker=r.ticker,
                    upnl_pct=r.upnl_pct,
                    upnl_usd=r.upnl_usd,
                    value_usd=r.value_usd,
                    reason="take_profit",
                )
            )
        elif r.upnl_pct <= -float(sl_pct):
            out.append(
                CloseLeg(
                    side=r.side,
                    ticker=r.ticker,
                    upnl_pct=r.upnl_pct,
                    upnl_usd=r.upnl_usd,
                    value_usd=r.value_usd,
                    reason="stop_loss",
                )
            )
    return out


def filter_replacements_one_side(
    *,
    candidates: Sequence[str],
    disallow: Set[str],
    need: int,
) -> List[str]:
    out: List[str] = []
    if int(need) <= 0:
        return out
    disallow_u = {str(x).strip().upper() for x in disallow if str(x).strip()}
    for c in candidates:
        sym = str(c).strip().upper()
        if not sym or sym in disallow_u:
            continue
        out.append(sym)
        if len(out) >= int(need):
            break
    return out


def filter_replacements(
    *,
    longs: Sequence[str],
    shorts: Sequence[str],
    disallow: Set[str],
    need_each_side: int,
) -> Tuple[List[str], List[str]]:
    n = int(need_each_side)
    return (
        filter_replacements_one_side(candidates=longs, disallow=disallow, need=n),
        filter_replacements_one_side(candidates=shorts, disallow=disallow, need=n),
    )


def scan_best_winner_opposite_pair(*_args: Any, **_kwargs: Any) -> None:
    """Reserved for near_median pairing logic (not wired in current varibot)."""
    raise NotImplementedError("scan_best_winner_opposite_pair is not implemented in this repo snapshot.")


def select_pairs_greedy_grid(*_args: Any, **_kwargs: Any) -> List[PairCandidate]:
    """Reserved for near_median pairing logic (not wired in current varibot)."""
    return []
