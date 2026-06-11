"""
Per-ticker grid pause when loss thresholds breach.

Triggers (any hit → pause):
  - **Pain:** ``uPnL + rPnL < -(VARIBOT_TICKER_PAUSE_PNL_FRAC × |position value|)``
  - **uPnL vs rung:** ``uPnL < -(VARIBOT_TICKER_PAUSE_UPNL_RUNG_MULT × grid_rung_usd)``

On trigger (live): cancel pending limits → reduce-only flatten → record pause (skip grid).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from grid_limits_reconcile import _fetch_pending_limit_rows
from pending_limit_cancel import cancel_limit_rows
from portfolio_manager_pairs import (
    _first_float,
    _instrument_label,
    _position_qty,
    _positions_list,
)
from variationalbot.vari.endpoints import VariEndpoints

ENV_ENABLED = "VARIBOT_TICKER_PAUSE_ENABLED"
ENV_PNL_FRAC = "VARIBOT_TICKER_PAUSE_PNL_FRAC"
ENV_UPNL_RUNG_MULT = "VARIBOT_TICKER_PAUSE_UPNL_RUNG_MULT"
ENV_MIN_VALUE_USD = "VARIBOT_TICKER_PAUSE_MIN_VALUE_USD"
ENV_STATE_FILE = "VARIBOT_TICKER_PAUSE_STATE"
ENV_CLEAR = "VARIBOT_TICKER_PAUSE_CLEAR"

DEFAULT_ENABLED: bool = True
DEFAULT_PNL_FRAC: float = 0.05
DEFAULT_UPNL_RUNG_MULT: float = 0.5
DEFAULT_MIN_VALUE_USD: float = 50.0
DEFAULT_STATE_NAME: str = ".varibot_ticker_pause.json"

_UPNL_KEYS = ("unrealized_pnl", "unrealizedPnl", "u_pnl", "upnl", "unrealized_pnl_usd", "pnl")
_RPNL_KEYS = ("realized_pnl", "realizedPnl", "r_pnl", "rpnl", "realizedPnl")
_VALUE_KEYS = ("value", "position_value", "notional", "notional_value", "usd_value", "positionValue")


@dataclass(frozen=True)
class PositionPnL:
    ticker: str
    qty: float
    upnl_usd: float
    rpnl_usd: float
    value_usd: float

    @property
    def combined_pnl_usd(self) -> float:
        return float(self.upnl_usd) + float(self.rpnl_usd)

    @property
    def pain_threshold_usd(self) -> float:
        return -float(DEFAULT_PNL_FRAC) * abs(float(self.value_usd))


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def ticker_pause_enabled() -> bool:
    return _env_bool(ENV_ENABLED, DEFAULT_ENABLED)


def ticker_pause_pnl_frac() -> float:
    return max(0.0, _env_float(ENV_PNL_FRAC, DEFAULT_PNL_FRAC))


def ticker_pause_upnl_rung_mult() -> float:
    """uPnL loss threshold as multiple of per-ticker grid rung USD; <= 0 disables."""
    return max(0.0, _env_float(ENV_UPNL_RUNG_MULT, DEFAULT_UPNL_RUNG_MULT))


def ticker_pause_min_value_usd() -> float:
    return max(0.0, _env_float(ENV_MIN_VALUE_USD, DEFAULT_MIN_VALUE_USD))


def default_state_path(varibot_dir: str) -> str:
    name = (os.environ.get(ENV_STATE_FILE) or "").strip() or DEFAULT_STATE_NAME
    return os.path.join(str(varibot_dir), name)


def _parse_position_pnl(p: Dict[str, Any]) -> Optional[PositionPnL]:
    sym = _instrument_label(p).strip().upper()
    qty = _position_qty(p)
    if not sym or qty is None or abs(float(qty)) <= 1e-12:
        return None

    upnl = _first_float(p, _UPNL_KEYS)
    if upnl is None and isinstance(p.get("position_info"), dict):
        upnl = _first_float(p["position_info"], _UPNL_KEYS)
    rpnl = _first_float(p, _RPNL_KEYS)
    if rpnl is None and isinstance(p.get("position_info"), dict):
        rpnl = _first_float(p["position_info"], _RPNL_KEYS)

    value = _first_float(p, _VALUE_KEYS)
    if value is None and isinstance(p.get("position_info"), dict):
        value = _first_float(p["position_info"], ("value", "notional", "position_value"))
    if value is None or float(value) <= 1e-12:
        mark = _first_float(p, ("mark", "mark_price", "markPrice", "mark_px"))
        if mark is None:
            pi = p.get("price_info")
            if isinstance(pi, dict):
                mark = _first_float(pi, ("price",))
        if mark is not None and float(mark) > 0:
            value = abs(float(qty)) * float(mark)
        else:
            value = abs(float(qty))

    return PositionPnL(
        ticker=sym,
        qty=float(qty),
        upnl_usd=float(upnl or 0.0),
        rpnl_usd=float(rpnl or 0.0),
        value_usd=abs(float(value)),
    )


def pain_triggered(
    pos: PositionPnL,
    *,
    pnl_frac: float,
    min_value_usd: float,
) -> bool:
    v = abs(float(pos.value_usd))
    if v < float(min_value_usd):
        return False
    threshold = -float(pnl_frac) * v
    return float(pos.combined_pnl_usd) < float(threshold)


def evaluate_pain_candidates(
    positions_raw: Any,
    *,
    grid_tickers: Set[str],
    pnl_frac: float,
    min_value_usd: float,
) -> List[PositionPnL]:
    out: List[PositionPnL] = []
    for p in _positions_list(positions_raw):
        row = _parse_position_pnl(p)
        if row is None:
            continue
        if row.ticker not in grid_tickers:
            continue
        if pain_triggered(row, pnl_frac=pnl_frac, min_value_usd=min_value_usd):
            out.append(row)
    out.sort(key=lambda r: r.combined_pnl_usd)
    return out


def upnl_rung_triggered(
    pos: PositionPnL,
    *,
    rung_mult: float,
    rung_usd: float,
) -> bool:
    if float(rung_mult) <= 0 or float(rung_usd) <= 0:
        return False
    threshold = -float(rung_mult) * float(rung_usd)
    return float(pos.upnl_usd) < float(threshold)


def evaluate_upnl_rung_candidates(
    positions_raw: Any,
    *,
    grid_tickers: Set[str],
    rung_mult: float,
    rung_usd_for_ticker: Callable[[str], float],
) -> List[PositionPnL]:
    out: List[PositionPnL] = []
    if float(rung_mult) <= 0:
        return out
    for p in _positions_list(positions_raw):
        row = _parse_position_pnl(p)
        if row is None:
            continue
        if row.ticker not in grid_tickers:
            continue
        rung = float(rung_usd_for_ticker(row.ticker))
        if upnl_rung_triggered(row, rung_mult=float(rung_mult), rung_usd=rung):
            out.append(row)
    out.sort(key=lambda r: r.upnl_usd)
    return out


@dataclass(frozen=True)
class PauseCandidate:
    pos: PositionPnL
    reason: str
    threshold_usd: float
    rung_usd: Optional[float] = None
    rung_mult: Optional[float] = None
    pnl_frac: Optional[float] = None


def collect_pause_candidates(
    positions_raw: Any,
    *,
    grid_tickers: Set[str],
    pnl_frac: float,
    min_value_usd: float,
    upnl_rung_mult: float,
    rung_usd_for_ticker: Callable[[str], float],
) -> List[PauseCandidate]:
    seen: Set[str] = set()
    out: List[PauseCandidate] = []

    for pos in evaluate_pain_candidates(
        positions_raw,
        grid_tickers=grid_tickers,
        pnl_frac=pnl_frac,
        min_value_usd=min_value_usd,
    ):
        if pos.ticker in seen:
            continue
        seen.add(pos.ticker)
        out.append(
            PauseCandidate(
                pos=pos,
                reason="pnl_vs_value",
                threshold_usd=-float(pnl_frac) * abs(float(pos.value_usd)),
                pnl_frac=float(pnl_frac),
            )
        )

    for pos in evaluate_upnl_rung_candidates(
        positions_raw,
        grid_tickers=grid_tickers,
        rung_mult=upnl_rung_mult,
        rung_usd_for_ticker=rung_usd_for_ticker,
    ):
        if pos.ticker in seen:
            continue
        seen.add(pos.ticker)
        rung = float(rung_usd_for_ticker(pos.ticker))
        out.append(
            PauseCandidate(
                pos=pos,
                reason="upnl_vs_rung",
                threshold_usd=-float(upnl_rung_mult) * rung,
                rung_usd=rung,
                rung_mult=float(upnl_rung_mult),
            )
        )

    out.sort(key=lambda c: c.pos.combined_pnl_usd)
    return out


def load_pause_state(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"paused": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"paused": {}}
    if not isinstance(raw, dict):
        return {"paused": {}}
    paused = raw.get("paused")
    if not isinstance(paused, dict):
        raw["paused"] = {}
    return raw


def save_pause_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def paused_ticker_set(state: Dict[str, Any]) -> Set[str]:
    paused = state.get("paused")
    if not isinstance(paused, dict):
        return set()
    return {str(k).strip().upper() for k in paused if str(k).strip()}


def apply_pause_clear(state: Dict[str, Any]) -> Set[str]:
    """Remove tickers listed in ``VARIBOT_TICKER_PAUSE_CLEAR`` (comma-separated, or ``all``)."""
    raw = (os.environ.get(ENV_CLEAR) or "").strip()
    if not raw:
        return set()
    parts = {p.strip().upper() for p in raw.split(",") if p.strip()}
    if "ALL" in parts:
        cleared = paused_ticker_set(state)
        state["paused"] = {}
        return cleared
    paused = state.get("paused")
    if not isinstance(paused, dict):
        return set()
    cleared: Set[str] = set()
    for sym in parts:
        if sym in paused:
            del paused[sym]
            cleared.add(sym)
    return cleared


def record_pause(
    state: Dict[str, Any],
    *,
    ticker: str,
    candidate: PauseCandidate,
) -> None:
    paused = state.setdefault("paused", {})
    if not isinstance(paused, dict):
        paused = {}
        state["paused"] = paused
    sym = str(ticker).strip().upper()
    pos = candidate.pos
    entry: Dict[str, Any] = {
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(candidate.reason),
        "upnl_usd": float(pos.upnl_usd),
        "rpnl_usd": float(pos.rpnl_usd),
        "combined_pnl_usd": float(pos.combined_pnl_usd),
        "value_usd": float(pos.value_usd),
        "threshold_usd": float(candidate.threshold_usd),
    }
    if candidate.pnl_frac is not None:
        entry["pnl_frac"] = float(candidate.pnl_frac)
    if candidate.rung_usd is not None:
        entry["rung_usd"] = float(candidate.rung_usd)
    if candidate.rung_mult is not None:
        entry["rung_mult"] = float(candidate.rung_mult)
    paused[sym] = entry


def cancel_ticker_limits(
    ep: VariEndpoints,
    *,
    ticker: str,
    log: Callable[[str], None],
    live: bool,
) -> Tuple[int, int]:
    rows = _fetch_pending_limit_rows(ep, asset=str(ticker).strip().upper())
    if not rows:
        log(f"ticker_pause[{ticker}]: no pending limits to cancel")
        return 0, 0
    if not live:
        log(f"ticker_pause[{ticker}]: dry-run — would cancel {len(rows)} limit(s)")
        return 0, 0
    ok, err = cancel_limit_rows(ep, rows, log=lambda m: log(f"ticker_pause[{ticker}]: {m}"))
    log(f"ticker_pause[{ticker}]: canceled limits ok={ok} err={err}")
    return ok, err


def run_ticker_pain_cycle(
    ep: VariEndpoints,
    positions_raw: Any,
    *,
    grid_tickers: Set[str],
    varibot_dir: str,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
) -> Set[str]:
    """
    Evaluate pain rule; on hit cancel limits, flatten, pause.

    ``close_position(sym, qty_abs, close_side)`` is provided by varibot (live flatten).
    Returns the full paused ticker set (including newly paused).
    """
    if not ticker_pause_enabled():
        return set()

    path = default_state_path(varibot_dir)
    state = load_pause_state(path)
    cleared = apply_pause_clear(state)
    if cleared:
        log(f"ticker_pause: cleared pause for {', '.join(sorted(cleared))}")
        save_pause_state(path, state)

    from portfolio_rebalance import grid_rung_usd_for_ticker

    pnl_frac = ticker_pause_pnl_frac()
    upnl_rung_mult = ticker_pause_upnl_rung_mult()
    min_val = ticker_pause_min_value_usd()
    candidates = collect_pause_candidates(
        positions_raw,
        grid_tickers=grid_tickers,
        pnl_frac=pnl_frac,
        min_value_usd=min_val,
        upnl_rung_mult=upnl_rung_mult,
        rung_usd_for_ticker=grid_rung_usd_for_ticker,
    )

    paused = paused_ticker_set(state)
    newly: Set[str] = set()

    for cand in candidates:
        pos = cand.pos
        sym = pos.ticker
        if cand.reason == "upnl_vs_rung":
            log(
                f"ticker_pause[{sym}]: trigger upnl_vs_rung uPnL=${pos.upnl_usd:.2f} "
                f"rung=${cand.rung_usd:.2f}×{cand.rung_mult:g} "
                f"threshold=${cand.threshold_usd:.2f} "
                f"({'LIVE' if live and not dry_run else 'dry-run'})"
            )
        else:
            log(
                f"ticker_pause[{sym}]: trigger pnl_vs_value uPnL+rPnL=${pos.combined_pnl_usd:.2f} "
                f"(u=${pos.upnl_usd:.2f} r=${pos.rpnl_usd:.2f}) "
                f"value=${pos.value_usd:.2f} threshold=${cand.threshold_usd:.2f} "
                f"({'LIVE' if live and not dry_run else 'dry-run'})"
            )

        cancel_ticker_limits(ep, ticker=sym, log=log, live=bool(live and not dry_run))

        qty_abs = abs(float(pos.qty))
        if qty_abs > 1e-12:
            close_side = "sell" if float(pos.qty) > 0 else "buy"
            if live and not dry_run:
                try:
                    close_position(sym, qty_abs, close_side)
                except Exception as e:
                    log(f"ticker_pause[{sym}]: flatten failed ({type(e).__name__}: {e})")
            else:
                log(
                    f"ticker_pause[{sym}]: dry-run — would flatten {close_side} "
                    f"qty={qty_abs:g}"
                )

        record_pause(state, ticker=sym, candidate=cand)
        paused.add(sym)
        newly.add(sym)
        time.sleep(0.25)

    if newly:
        save_pause_state(path, state)
        log(f"ticker_pause: paused {', '.join(sorted(newly))}")
    elif paused:
        save_pause_state(path, state)

    return paused
