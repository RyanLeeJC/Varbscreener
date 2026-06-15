"""
Grid slow-drift pause — per-ticker disable on sustained move vs grid band.

Rule (defaults from ``strategy/gridstrat.py``):
  - **Upward:** 4h Binance 5m close-to-close drift ≥ ``GRID_DRIFT_PAUSE_BAND_MULT × band%``
    (default 2× band over 4h — e.g. 2.5% band → +5% in 4h).
  - On trigger: cancel pending limits → reduce-only flatten → sticky pause (no auto-resume).
  - ``GRID_DRIFT_PAUSE_CLEAR`` removes entries (comma tickers or ``ALL``).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from grid_limits_reconcile import _fetch_pending_limit_rows
from pending_limit_cancel import cancel_limit_rows
from portfolio_manager_pairs import _instrument_label, _position_qty, _positions_list
from strategy.gridstrat import (
    ENV_GRID_DRIFT_PAUSE_CLEAR,
    drift_return_fraction,
    grid_band_pct_for_asset,
    grid_drift_pause_band_mult,
    grid_drift_pause_default_state_path,
    grid_drift_pause_direction,
    grid_drift_pause_enabled,
    grid_drift_pause_lookback_hours,
    drift_pause_threshold_fraction,
    should_pause_for_drift,
)

DEFAULT_BINANCE_WORKERS: int = 8
ENV_BINANCE_WORKERS: str = "GRID_DRIFT_PAUSE_BINANCE_WORKERS"

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BARS_PER_HOUR_5M: int = 12


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def _binance_futures_symbol(ticker: str) -> str:
    return f"{str(ticker).strip().upper()}USDT"


def _binance_request_proxies() -> Optional[Dict[str, str]]:
    u = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if not u:
        return None
    return {"http": u, "https": u}


def _binance_klines_get(*, symbol: str, interval: str, limit: int) -> List[Any]:
    import requests

    resp = requests.get(
        BINANCE_FAPI_KLINES,
        params={"symbol": symbol, "interval": str(interval), "limit": int(limit)},
        timeout=20,
        proxies=_binance_request_proxies(),
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows if isinstance(rows, list) else []


def fetch_binance_5m_closes(ticker: str, *, limit: int) -> List[float]:
    sym = _binance_futures_symbol(ticker)
    try:
        rows = _binance_klines_get(symbol=sym, interval="5m", limit=int(limit))
    except Exception:
        return []
    out: List[float] = []
    for row in rows:
        try:
            px = float(row[4])
            if px > 0:
                out.append(px)
        except (IndexError, TypeError, ValueError):
            continue
    return out


def kline_limit_for_lookback_hours(hours: float) -> int:
    n_bars = max(1, int(round(float(hours) * float(BARS_PER_HOUR_5M))))
    return n_bars + 1


def fetch_binance_drift_fraction(ticker: str, *, lookback_hours: float) -> Optional[float]:
    closes = fetch_binance_5m_closes(ticker, limit=kline_limit_for_lookback_hours(lookback_hours))
    if len(closes) < 2:
        return None
    return drift_return_fraction(start_close=closes[0], end_close=closes[-1])


def fetch_binance_drift_fractions_parallel(
    tickers: Sequence[str],
    *,
    lookback_hours: float,
    workers: int = DEFAULT_BINANCE_WORKERS,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    uniq = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    if not uniq:
        return out

    def _one(sym: str) -> tuple[str, Optional[float]]:
        return sym, fetch_binance_drift_fraction(sym, lookback_hours=float(lookback_hours))

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(_one, t): t for t in uniq}
        for fut in as_completed(futs):
            sym, drift = fut.result()
            if drift is not None:
                out[sym] = float(drift)
    return out


def default_state_path(varibot_dir: str) -> str:
    return grid_drift_pause_default_state_path(varibot_dir)


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"paused": {}}
    if not isinstance(data, dict):
        return {"paused": {}}
    data.setdefault("paused", {})
    return data


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def paused_ticker_set(state: Dict[str, Any]) -> Set[str]:
    paused = state.get("paused")
    if not isinstance(paused, dict):
        return set()
    return {str(k).strip().upper() for k in paused if str(k).strip()}


def apply_pause_clear(state: Dict[str, Any]) -> Set[str]:
    raw = (os.environ.get(ENV_GRID_DRIFT_PAUSE_CLEAR) or "").strip()
    if not raw:
        return set()
    os.environ.pop(ENV_GRID_DRIFT_PAUSE_CLEAR, None)
    paused = state.get("paused")
    if not isinstance(paused, dict):
        return set()
    if raw.upper() == "ALL":
        cleared = set(paused.keys())
        state["paused"] = {}
        return {str(k).strip().upper() for k in cleared if str(k).strip()}
    want = {p.strip().upper() for p in raw.split(",") if p.strip()}
    cleared = {k for k in paused if str(k).strip().upper() in want}
    for k in cleared:
        paused.pop(k, None)
    return {str(k).strip().upper() for k in cleared if str(k).strip()}


def cancel_ticker_limits(ep: Any, *, ticker: str, log: Callable[[str], None], live: bool) -> None:
    rows = _fetch_pending_limit_rows(ep, asset=str(ticker).strip().upper())
    if not rows:
        log(f"grid_drift_pause[{ticker}]: no pending limits to cancel")
        return
    if not live:
        log(f"grid_drift_pause[{ticker}]: dry-run — would cancel {len(rows)} limit(s)")
        return
    ok, err = cancel_limit_rows(ep, rows, log=lambda m: log(f"grid_drift_pause[{ticker}]: {m}"))
    log(f"grid_drift_pause[{ticker}]: canceled limits ok={ok} err={err}")


def _fmt_pct_frac(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _position_qty_for_ticker(positions_raw: Any, ticker: str) -> Optional[float]:
    sym = str(ticker).strip().upper()
    for p in _positions_list(positions_raw):
        if _instrument_label(p).strip().upper() != sym:
            continue
        qty = _position_qty(p)
        if qty is not None and abs(float(qty)) > 1e-12:
            return float(qty)
    return None


def run_grid_drift_pause_cycle(
    ep: Any,
    positions_raw: Any,
    *,
    cycle_index: int,
    grid_tickers: Set[str],
    varibot_dir: str,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
) -> Set[str]:
    """Evaluate slow-drift rule; cancel limits, flatten, sticky-pause offenders."""
    if not grid_drift_pause_enabled():
        return set()

    lookback_h = grid_drift_pause_lookback_hours()
    band_mult = grid_drift_pause_band_mult()
    direction = grid_drift_pause_direction()

    if int(cycle_index) == 1 or int(cycle_index) % 60 == 0:
        log(
            f"grid_drift_pause: policy {direction} — "
            f"{lookback_h:g}h drift ≥ {band_mult:g}×band% → cancel, flatten, pause"
        )

    path = default_state_path(varibot_dir)
    state = load_state(path)
    cleared = apply_pause_clear(state)
    if cleared:
        log(f"grid_drift_pause: cleared pause for {', '.join(sorted(cleared))}")
        save_state(path, state)

    paused = paused_ticker_set(state)
    syms = {str(s).strip().upper() for s in grid_tickers if str(s).strip()}
    if not syms:
        return paused

    workers = max(1, _env_int(ENV_BINANCE_WORKERS, DEFAULT_BINANCE_WORKERS))
    try:
        drifts = fetch_binance_drift_fractions_parallel(
            sorted(syms),
            lookback_hours=float(lookback_h),
            workers=workers,
        )
    except Exception as e:
        log(f"grid_drift_pause: Binance drift fetch failed ({type(e).__name__}: {e})")
        return paused

    newly: Set[str] = set()
    for sym in sorted(syms):
        if sym in paused:
            continue
        drift = drifts.get(sym)
        if drift is None:
            continue
        band = float(grid_band_pct_for_asset(sym))
        thresh = drift_pause_threshold_fraction(band_pct=band, band_mult=band_mult)
        if not should_pause_for_drift(
            float(drift),
            band_pct=band,
            band_mult=band_mult,
            direction=direction,
        ):
            continue
        log(
            f"grid_drift_pause[{sym}]: PAUSE drift_{lookback_h:g}h="
            f"{_fmt_pct_frac(drift)} thresh={thresh * 100:.2f}% "
            f"(band={band:g}%×{band_mult:g}, dir={direction}) "
            f"({'LIVE' if live and not dry_run else 'dry-run'})"
        )
        cancel_ticker_limits(ep, ticker=sym, log=log, live=bool(live and not dry_run))

        qty = _position_qty_for_ticker(positions_raw, sym)
        if qty is not None:
            qty_abs = abs(float(qty))
            close_side = "sell" if float(qty) > 0 else "buy"
            if live and not dry_run:
                try:
                    close_position(sym, qty_abs, close_side)
                except Exception as e:
                    log(f"grid_drift_pause[{sym}]: flatten failed ({type(e).__name__}: {e})")
            else:
                log(
                    f"grid_drift_pause[{sym}]: dry-run — would flatten {close_side} "
                    f"qty={qty_abs:g}"
                )

        if not isinstance(state.get("paused"), dict):
            state["paused"] = {}
        state["paused"][sym] = {
            "since_cycle": int(cycle_index),
            "since_ts": time.time(),
            "drift_frac": float(drift),
            "band_pct": band,
            "band_mult": float(band_mult),
            "lookback_hours": float(lookback_h),
            "direction": direction,
            "threshold_frac": float(thresh),
            "flattened": bool(qty is not None),
        }
        paused.add(sym)
        newly.add(sym)
        time.sleep(0.25)

    if newly:
        save_state(path, state)
        log(f"grid_drift_pause: paused {', '.join(sorted(newly))}")
    elif paused:
        log(f"grid_drift_pause: still paused {', '.join(sorted(paused))}")

    return paused
