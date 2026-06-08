"""
Grid volatility pause — cycle-start check (dump + pump).

Production rule (backtest winner on 1–7 Jun): **AND gate**
  - Market: BTC or ETH 1h return ±2% (Binance 1m×61, or history after 60 cycles)
  - Ticker: 30m return ±(grid_band% × 1.6), ~5m bar, and/or vol_ratio ≥ 1.3
  - Pause only when **both** market and ticker stress (GRID_VOL_PAUSE_REQUIRE_BOTH=1)
  - vol_ratio: Binance 5m realized σ (36-bar / 72-bar median) — live from cycle 1
  - Min hold: 1h after pause before resume (GRID_VOL_PAUSE_MIN_PAUSE_HOURS)

Do not set REQUIRE_BOTH=0 unless intentionally testing ticker-only (hurts PnL in study).
"""

from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from grid_limits_reconcile import _fetch_pending_limit_rows
from pending_limit_cancel import cancel_limit_rows

ENV_ENABLED = "GRID_VOL_PAUSE_ENABLED"
ENV_MARKET_RET = "GRID_VOL_PAUSE_MARKET_RET"
ENV_MARKET_PUMP_RET = "GRID_VOL_PAUSE_MARKET_PUMP_RET"
ENV_TICKER_CUM_BAND_MULT = "GRID_VOL_PAUSE_TICKER_CUM_BAND_MULT"
ENV_TICKER_BAR_RET = "GRID_VOL_PAUSE_TICKER_BAR_RET"
ENV_TICKER_BAR_PUMP_RET = "GRID_VOL_PAUSE_TICKER_BAR_PUMP_RET"
ENV_VOL_RATIO = "GRID_VOL_PAUSE_VOL_RATIO"
ENV_RESUME_CYCLES = "GRID_VOL_PAUSE_RESUME_BARS"
ENV_RESUME_MARKET_RET = "GRID_VOL_PAUSE_RESUME_MARKET_RET"
ENV_RESUME_MARKET_PUMP_RET = "GRID_VOL_PAUSE_RESUME_MARKET_PUMP_RET"
ENV_RESUME_TICKER_BAND_MULT = "GRID_VOL_PAUSE_RESUME_TICKER_BAND_MULT"
ENV_RESUME_VOL_RATIO = "GRID_VOL_PAUSE_RESUME_VOL_RATIO"
ENV_REQUIRE_BOTH = "GRID_VOL_PAUSE_REQUIRE_BOTH"
ENV_MARKET_LB_CYCLES = "GRID_VOL_PAUSE_MARKET_LB_CYCLES"
ENV_TICKER_LB_CYCLES = "GRID_VOL_PAUSE_TICKER_LB_CYCLES"
ENV_TICKER_BAR_LB_CYCLES = "GRID_VOL_PAUSE_TICKER_BAR_LB_CYCLES"
ENV_VOL_LB_BARS_5M = "GRID_VOL_PAUSE_VOL_LB_BARS_5M"
ENV_VOL_MEDIAN_BARS_5M = "GRID_VOL_PAUSE_VOL_MEDIAN_BARS_5M"
# Legacy 1m-cycle env names (÷5 → 5m bars) when new vars unset
ENV_VOL_LB_CYCLES = "GRID_VOL_PAUSE_VOL_LB_CYCLES"
ENV_VOL_MEDIAN_LB_CYCLES = "GRID_VOL_PAUSE_VOL_MEDIAN_LB_CYCLES"
ENV_STATE_FILE = "GRID_VOL_PAUSE_STATE"
ENV_BINANCE_WORKERS = "GRID_VOL_PAUSE_BINANCE_WORKERS"
ENV_MIN_PAUSE_CYCLES = "GRID_VOL_PAUSE_MIN_PAUSE_CYCLES"
ENV_MIN_PAUSE_HOURS = "GRID_VOL_PAUSE_MIN_PAUSE_HOURS"

DEFAULT_ENABLED: bool = True
DEFAULT_STATE_NAME: str = ".grid_vol_pause.json"
DEFAULT_HISTORY_MAX: int = 2000

DEFAULT_MARKET_RET: float = -0.02
DEFAULT_MARKET_PUMP_RET: float = 0.02
# 1.6× band → 2.5% grid (JUP) ≈ 4% threshold (matches GRID_recc blanket -4%)
DEFAULT_TICKER_CUM_BAND_MULT: float = 1.6
DEFAULT_TICKER_BAR_RET: float = -0.012
DEFAULT_TICKER_BAR_PUMP_RET: float = 0.012
DEFAULT_VOL_RATIO: float = 1.3
DEFAULT_RESUME_CYCLES: int = 18
DEFAULT_RESUME_MARKET_RET: float = -0.005
DEFAULT_RESUME_MARKET_PUMP_RET: float = 0.005
DEFAULT_RESUME_TICKER_BAND_MULT: float = 0.4
DEFAULT_RESUME_VOL_RATIO: float = 1.3
DEFAULT_REQUIRE_BOTH: bool = True

DEFAULT_MARKET_LB_CYCLES: int = 60
DEFAULT_TICKER_LB_CYCLES: int = 30
DEFAULT_TICKER_BAR_LB_CYCLES: int = 5
# 5m bars: 36 ≈ 3h σ window, 72 ≈ 6h median baseline
DEFAULT_VOL_LB_BARS_5M: int = 36
DEFAULT_VOL_MEDIAN_BARS_5M: int = 72
DEFAULT_BINANCE_WORKERS: int = 8
DEFAULT_MIN_PAUSE_CYCLES: int = 60  # 1h at 1-min Varibot cycles

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FAPI_TICKER_PRICE = "https://fapi.binance.com/fapi/v1/ticker/price"
# limit=N spans (N-1) minutes between first and last close
BINANCE_MARKET_KLINES_LIMIT: int = 61
BINANCE_TICKER_CUM_KLINES_LIMIT: int = 31
MS_PER_CYCLE: int = 60_000


@dataclass(frozen=True)
class VolPauseConfig:
    enabled: bool
    market_ret: float
    market_pump_ret: float
    ticker_cum_band_mult: float
    ticker_bar_ret: float
    ticker_bar_pump_ret: float
    vol_ratio_pause: float
    resume_cycles: int
    resume_market_ret: float
    resume_market_pump_ret: float
    resume_ticker_band_mult: float
    resume_vol_ratio: float
    require_both: bool
    market_lb_cycles: int
    ticker_lb_cycles: int
    ticker_bar_lb_cycles: int
    vol_lb_bars_5m: int
    vol_median_bars_5m: int
    binance_workers: int
    min_pause_cycles: int


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


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def _vol_lb_bars_5m_from_env() -> int:
    if (os.environ.get(ENV_VOL_LB_BARS_5M) or "").strip():
        return max(2, _env_int(ENV_VOL_LB_BARS_5M, DEFAULT_VOL_LB_BARS_5M))
    if (os.environ.get(ENV_VOL_LB_CYCLES) or "").strip():
        return max(2, _env_int(ENV_VOL_LB_CYCLES, 180) // 5)
    return DEFAULT_VOL_LB_BARS_5M


def _min_pause_cycles_from_env() -> int:
    if (os.environ.get(ENV_MIN_PAUSE_CYCLES) or "").strip():
        return max(1, _env_int(ENV_MIN_PAUSE_CYCLES, DEFAULT_MIN_PAUSE_CYCLES))
    hours_raw = (os.environ.get(ENV_MIN_PAUSE_HOURS) or "").strip()
    if hours_raw:
        try:
            hours = float(hours_raw)
            return max(1, int(math.ceil(hours * 60.0)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_PAUSE_CYCLES


def _vol_median_bars_5m_from_env() -> int:
    if (os.environ.get(ENV_VOL_MEDIAN_BARS_5M) or "").strip():
        return max(2, _env_int(ENV_VOL_MEDIAN_BARS_5M, DEFAULT_VOL_MEDIAN_BARS_5M))
    if (os.environ.get(ENV_VOL_MEDIAN_LB_CYCLES) or "").strip():
        return max(2, _env_int(ENV_VOL_MEDIAN_LB_CYCLES, 1440) // 5)
    return DEFAULT_VOL_MEDIAN_BARS_5M


def load_config() -> VolPauseConfig:
    return VolPauseConfig(
        enabled=_env_bool(ENV_ENABLED, DEFAULT_ENABLED),
        market_ret=_env_float(ENV_MARKET_RET, DEFAULT_MARKET_RET),
        market_pump_ret=_env_float(ENV_MARKET_PUMP_RET, DEFAULT_MARKET_PUMP_RET),
        ticker_cum_band_mult=_env_float(ENV_TICKER_CUM_BAND_MULT, DEFAULT_TICKER_CUM_BAND_MULT),
        ticker_bar_ret=_env_float(ENV_TICKER_BAR_RET, DEFAULT_TICKER_BAR_RET),
        ticker_bar_pump_ret=_env_float(ENV_TICKER_BAR_PUMP_RET, DEFAULT_TICKER_BAR_PUMP_RET),
        vol_ratio_pause=_env_float(ENV_VOL_RATIO, DEFAULT_VOL_RATIO),
        resume_cycles=_env_int(ENV_RESUME_CYCLES, DEFAULT_RESUME_CYCLES),
        resume_market_ret=_env_float(ENV_RESUME_MARKET_RET, DEFAULT_RESUME_MARKET_RET),
        resume_market_pump_ret=_env_float(ENV_RESUME_MARKET_PUMP_RET, DEFAULT_RESUME_MARKET_PUMP_RET),
        resume_ticker_band_mult=_env_float(ENV_RESUME_TICKER_BAND_MULT, DEFAULT_RESUME_TICKER_BAND_MULT),
        resume_vol_ratio=_env_float(ENV_RESUME_VOL_RATIO, DEFAULT_RESUME_VOL_RATIO),
        require_both=_env_bool(ENV_REQUIRE_BOTH, DEFAULT_REQUIRE_BOTH),
        market_lb_cycles=max(1, _env_int(ENV_MARKET_LB_CYCLES, DEFAULT_MARKET_LB_CYCLES)),
        ticker_lb_cycles=max(1, _env_int(ENV_TICKER_LB_CYCLES, DEFAULT_TICKER_LB_CYCLES)),
        ticker_bar_lb_cycles=max(1, _env_int(ENV_TICKER_BAR_LB_CYCLES, DEFAULT_TICKER_BAR_LB_CYCLES)),
        vol_lb_bars_5m=_vol_lb_bars_5m_from_env(),
        vol_median_bars_5m=_vol_median_bars_5m_from_env(),
        binance_workers=max(1, _env_int(ENV_BINANCE_WORKERS, DEFAULT_BINANCE_WORKERS)),
        min_pause_cycles=_min_pause_cycles_from_env(),
    )


def binance_vol_klines_limit(cfg: VolPauseConfig) -> int:
    """5m bars needed for realized-vol ratio (window + median baseline)."""
    return int(cfg.vol_lb_bars_5m) + int(cfg.vol_median_bars_5m) + 2


def ticker_cum_thresholds(*, band_pct: float, mult: float) -> Tuple[float, float]:
    """Dump/pump fractions from grid band % × multiplier (e.g. 2.5% × 1.6 → ±4%)."""
    frac = (float(band_pct) / 100.0) * float(mult)
    return (-frac, +frac)


def default_state_path(varibot_dir: str) -> str:
    name = (os.environ.get(ENV_STATE_FILE) or DEFAULT_STATE_NAME).strip() or DEFAULT_STATE_NAME
    return os.path.join(varibot_dir, name)


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"paused": {}, "calm_cycles": {}, "history": []}
    if not isinstance(data, dict):
        return {"paused": {}, "calm_cycles": {}, "history": []}
    data.setdefault("paused", {})
    data.setdefault("calm_cycles", {})
    data.setdefault("history", [])
    return data


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def paused_ticker_set(state: Dict[str, Any]) -> Set[str]:
    paused = state.get("paused")
    if not isinstance(paused, dict):
        return set()
    return {str(k).strip().upper() for k in paused if str(k).strip()}


def _binance_futures_symbol(ticker: str) -> str:
    return f"{str(ticker).strip().upper()}USDT"


def _binance_klines_get(*, symbol: str, interval: str, limit: int) -> List[Any]:
    import requests

    resp = requests.get(
        BINANCE_FAPI_KLINES,
        params={"symbol": symbol, "interval": str(interval), "limit": int(limit)},
        timeout=20,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows if isinstance(rows, list) else []


def fetch_binance_klines_closes(
    ticker: str,
    *,
    interval: str,
    limit: int,
) -> List[float]:
    sym = _binance_futures_symbol(ticker)
    try:
        rows = _binance_klines_get(symbol=sym, interval=interval, limit=limit)
    except Exception:
        return []
    out: List[float] = []
    for row in rows:
        try:
            px = float(row[4])
            if px > 0 and math.isfinite(px):
                out.append(px)
        except (IndexError, TypeError, ValueError):
            continue
    return out


def kline_limit_for_cycles(lb_cycles: int) -> int:
    return int(lb_cycles) + 1


def fetch_binance_kline_return(ticker: str, *, limit: int) -> Optional[float]:
    """Return from Binance 1m klines: close[-1]/close[0] - 1."""
    sym = _binance_futures_symbol(ticker)
    try:
        rows = _binance_klines_get(symbol=sym, interval="1m", limit=limit)
    except Exception:
        return None
    if len(rows) < 2:
        return None
    try:
        then_close = float(rows[0][4])
        now_close = float(rows[-1][4])
    except (IndexError, TypeError, ValueError):
        return None
    return cum_return(now_close, then_close)


def fetch_binance_kline_returns_parallel(
    tickers: Sequence[str],
    *,
    limit: int,
    workers: int = DEFAULT_BINANCE_WORKERS,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    uniq = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    if not uniq:
        return out

    def _one(t: str) -> Tuple[str, Optional[float]]:
        return t, fetch_binance_kline_return(t, limit=limit)

    with ThreadPoolExecutor(max_workers=int(workers)) as ex:
        futs = {ex.submit(_one, t): t for t in uniq}
        for fut in as_completed(futs):
            t, ret = fut.result()
            if ret is not None and math.isfinite(ret):
                out[t] = float(ret)
    return out


def fetch_binance_futures_prices(tickers: Sequence[str]) -> Dict[str, float]:
    """Current USDT-M mark prices; one API call filtered locally."""
    import requests

    want = {_binance_futures_symbol(t) for t in tickers if str(t).strip()}
    if not want:
        return {}
    try:
        resp = requests.get(BINANCE_FAPI_TICKER_PRICE, timeout=20)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    rev = {str(t).strip().upper(): _binance_futures_symbol(t) for t in tickers if str(t).strip()}
    sym_to_ticker = {v: k for k, v in rev.items()}
    out: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        if sym not in want:
            continue
        ticker = sym_to_ticker.get(sym)
        if not ticker:
            continue
        try:
            px = float(row.get("price"))
            if px > 0 and math.isfinite(px):
                out[ticker] = px
        except (TypeError, ValueError):
            pass
    return out


def cum_return(now: float, then: float) -> float:
    if then <= 0 or now <= 0 or not math.isfinite(now) or not math.isfinite(then):
        return 0.0
    return float(now) / float(then) - 1.0


def _history_rows(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    hist = state.get("history")
    if not isinstance(hist, list):
        return []
    return [r for r in hist if isinstance(r, dict)]


def _price_at_cycle(state: Dict[str, Any], *, cycle_index: int, symbol: str) -> Optional[float]:
    sym = str(symbol).strip().upper()
    for row in reversed(_history_rows(state)):
        if int(row.get("cycle") or 0) != int(cycle_index):
            continue
        prices = row.get("prices")
        if isinstance(prices, dict) and sym in prices:
            try:
                v = float(prices[sym])
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None
    return None


def _return_over_cycles(
    state: Dict[str, Any],
    *,
    cycle_index: int,
    symbol: str,
    lb_cycles: int,
) -> Optional[float]:
    """Return over lb_cycles from recorded cycle history."""
    sym = str(symbol).strip().upper()
    now_row = _price_at_cycle(state, cycle_index=cycle_index, symbol=sym)
    if now_row is None:
        return None
    if int(cycle_index) < int(lb_cycles):
        return None
    then = _price_at_cycle(state, cycle_index=int(cycle_index) - int(lb_cycles), symbol=sym)
    if then is None:
        return None
    return cum_return(now_row, then)


def _log_returns_study(closes: Sequence[float]) -> List[float]:
    """Match grid_vol_pause_study: leading 0 then log returns."""
    out: List[float] = [0.0]
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out.append(math.log(float(closes[i]) / float(closes[i - 1])))
        else:
            out.append(0.0)
    return out


def _rolling_std_at(xs: Sequence[float], i: int, lb: int) -> float:
    if i < int(lb) - 1:
        return 0.0
    window = list(xs[i - int(lb) + 1 : i + 1])
    if len(window) < 2:
        return 0.0
    m = sum(window) / len(window)
    var = sum((x - m) ** 2 for x in window) / (len(window) - 1)
    return math.sqrt(max(0.0, var))


def compute_realized_vol_ratio(
    closes: Sequence[float],
    *,
    vol_lb: int,
    vol_median_lb: int,
) -> Optional[float]:
    """
    Realized vol spike vs baseline (study-aligned).

    vol_now = σ(log returns) over last ``vol_lb`` 5m bars;
    vol_med = median of that σ over last ``vol_median_lb`` bars.
    """
    if len(closes) < int(vol_lb) + 2:
        return None
    lr = _log_returns_study(closes)
    vols = [_rolling_std_at(lr, i, int(vol_lb)) for i in range(len(lr))]
    i = len(lr) - 1
    if i < int(vol_lb) - 1:
        return None
    vol_now = float(vols[i])
    start = max(0, i - int(vol_median_lb) + 1)
    vol_med = _median(vols[start : i + 1])
    if vol_med <= 0:
        return None
    return vol_now / vol_med


def fetch_binance_vol_ratios_parallel(
    tickers: Sequence[str],
    *,
    vol_lb: int,
    vol_median_lb: int,
    workers: int = DEFAULT_BINANCE_WORKERS,
) -> Dict[str, float]:
    """Per-ticker vol_ratio from Binance 5m klines — works from cycle 1."""
    limit = int(vol_lb) + int(vol_median_lb) + 2
    out: Dict[str, float] = {}
    uniq = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    if not uniq:
        return out

    def _one(t: str) -> Tuple[str, Optional[float]]:
        closes = fetch_binance_klines_closes(t, interval="5m", limit=limit)
        ratio = compute_realized_vol_ratio(closes, vol_lb=vol_lb, vol_median_lb=vol_median_lb)
        return t, ratio

    with ThreadPoolExecutor(max_workers=int(workers)) as ex:
        futs = {ex.submit(_one, t): t for t in uniq}
        for fut in as_completed(futs):
            t, ratio = fut.result()
            if ratio is not None and math.isfinite(ratio):
                out[t] = float(ratio)
    return out


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _market_stressed(
    cfg: VolPauseConfig,
    *,
    btc_ret: Optional[float],
    eth_ret: Optional[float],
) -> Tuple[bool, bool, bool]:
    dump = False
    pump = False
    if btc_ret is not None:
        dump = dump or float(btc_ret) <= float(cfg.market_ret)
        pump = pump or float(btc_ret) >= float(cfg.market_pump_ret)
    if eth_ret is not None:
        dump = dump or float(eth_ret) <= float(cfg.market_ret)
        pump = pump or float(eth_ret) >= float(cfg.market_pump_ret)
    return dump or pump, dump, pump


def _ticker_stressed(
    cfg: VolPauseConfig,
    *,
    cum_ret: Optional[float],
    bar_ret: Optional[float],
    vol_ratio: Optional[float],
    cum_dump_thresh: float,
    cum_pump_thresh: float,
) -> Tuple[bool, bool, bool]:
    dump = False
    pump = False
    if cum_ret is not None:
        dump = dump or float(cum_ret) <= float(cum_dump_thresh)
        pump = pump or float(cum_ret) >= float(cum_pump_thresh)
    if bar_ret is not None:
        dump = dump or float(bar_ret) <= float(cfg.ticker_bar_ret)
        pump = pump or float(bar_ret) >= float(cfg.ticker_bar_pump_ret)
    if vol_ratio is not None and float(vol_ratio) >= float(cfg.vol_ratio_pause):
        dump = True
        pump = True
    return dump or pump, dump, pump


def _should_pause(
    cfg: VolPauseConfig,
    *,
    market_stress: bool,
    ticker_stress: bool,
) -> bool:
    if cfg.require_both:
        return bool(market_stress and ticker_stress)
    return bool(market_stress or ticker_stress)


def _should_resume(
    cfg: VolPauseConfig,
    *,
    calm_cycles: int,
    btc_ret: Optional[float],
    eth_ret: Optional[float],
    ticker_cum: Optional[float],
    bar_ret: Optional[float],
    vol_ratio: Optional[float],
    band_pct: float,
) -> bool:
    if int(calm_cycles) < int(cfg.resume_cycles):
        return False
    if btc_ret is not None:
        if float(btc_ret) <= float(cfg.resume_market_ret) or float(btc_ret) >= float(cfg.resume_market_pump_ret):
            return False
    if eth_ret is not None:
        if float(eth_ret) <= float(cfg.resume_market_ret) or float(eth_ret) >= float(cfg.resume_market_pump_ret):
            return False
    if ticker_cum is not None:
        resume_dump, resume_pump = ticker_cum_thresholds(
            band_pct=float(band_pct),
            mult=float(cfg.resume_ticker_band_mult),
        )
        if float(ticker_cum) <= float(resume_dump) or float(ticker_cum) >= float(resume_pump):
            return False
    if bar_ret is not None:
        half_bar = float(cfg.ticker_bar_ret) * 0.5
        half_pump = float(cfg.ticker_bar_pump_ret) * 0.5
        if float(bar_ret) <= half_bar or float(bar_ret) >= half_pump:
            return False
    if vol_ratio is not None and float(vol_ratio) > float(cfg.resume_vol_ratio):
        return False
    return True


def _append_history(
    state: Dict[str, Any],
    *,
    cycle_index: int,
    prices: Dict[str, float],
    max_rows: int = DEFAULT_HISTORY_MAX,
) -> None:
    hist = _history_rows(state)
    hist.append({"cycle": int(cycle_index), "ts": time.time(), "prices": dict(prices)})
    if len(hist) > int(max_rows):
        hist = hist[-int(max_rows) :]
    state["history"] = hist


def cancel_ticker_limits(ep: Any, *, ticker: str, log: Callable[[str], None], live: bool) -> None:
    rows = _fetch_pending_limit_rows(ep, asset=str(ticker).strip().upper())
    if not rows:
        log(f"grid_vol_pause[{ticker}]: no pending limits to cancel")
        return
    if not live:
        log(f"grid_vol_pause[{ticker}]: dry-run — would cancel {len(rows)} limit(s)")
        return
    ok, err = cancel_limit_rows(ep, rows, log=lambda m: log(f"grid_vol_pause[{ticker}]: {m}"))
    log(f"grid_vol_pause[{ticker}]: canceled limits ok={ok} err={err}")


def _resolve_market_returns(
    cfg: VolPauseConfig,
    state: Dict[str, Any],
    *,
    cycle_index: int,
    binance_market: Dict[str, float],
) -> Tuple[Optional[float], Optional[float]]:
    btc = eth = None
    if int(cycle_index) >= int(cfg.market_lb_cycles):
        btc = _return_over_cycles(state, cycle_index=cycle_index, symbol="BTC", lb_cycles=cfg.market_lb_cycles)
        eth = _return_over_cycles(state, cycle_index=cycle_index, symbol="ETH", lb_cycles=cfg.market_lb_cycles)
    if btc is None:
        btc = binance_market.get("BTC")
    if eth is None:
        eth = binance_market.get("ETH")
    return btc, eth


def _resolve_ticker_cum_return(
    cfg: VolPauseConfig,
    state: Dict[str, Any],
    *,
    cycle_index: int,
    ticker: str,
    binance_ticker_30m: Dict[str, float],
) -> Optional[float]:
    sym = str(ticker).strip().upper()
    if int(cycle_index) >= int(cfg.ticker_lb_cycles):
        hist = _return_over_cycles(
            state, cycle_index=cycle_index, symbol=sym, lb_cycles=cfg.ticker_lb_cycles
        )
        if hist is not None:
            return hist
    return binance_ticker_30m.get(sym)


def evaluate_ticker(
    cfg: VolPauseConfig,
    state: Dict[str, Any],
    *,
    cycle_index: int,
    ticker: str,
    prices: Dict[str, float],
    band_pct: float,
    btc_ret: Optional[float],
    eth_ret: Optional[float],
    ticker_cum: Optional[float],
    binance_bar_5m: Dict[str, float],
    vol_ratio: Optional[float],
) -> Tuple[bool, bool, Dict[str, Any]]:
    """Returns (should_pause, should_resume, debug_info)."""
    sym = str(ticker).strip().upper()
    cum_dump, cum_pump = ticker_cum_thresholds(band_pct=float(band_pct), mult=float(cfg.ticker_cum_band_mult))

    bar_ret: Optional[float] = None
    if int(cycle_index) >= int(cfg.ticker_bar_lb_cycles):
        now_px = prices.get(sym) or _price_at_cycle(state, cycle_index=cycle_index, symbol=sym)
        then_px = _price_at_cycle(
            state,
            cycle_index=int(cycle_index) - int(cfg.ticker_bar_lb_cycles),
            symbol=sym,
        )
        if now_px is not None and then_px is not None:
            bar_ret = cum_return(now_px, then_px)
    else:
        bar_ret = binance_bar_5m.get(sym)

    m_stress, m_dump, m_pump = _market_stressed(cfg, btc_ret=btc_ret, eth_ret=eth_ret)
    t_stress, t_dump, t_pump = _ticker_stressed(
        cfg,
        cum_ret=ticker_cum,
        bar_ret=bar_ret,
        vol_ratio=vol_ratio,
        cum_dump_thresh=cum_dump,
        cum_pump_thresh=cum_pump,
    )
    pause = _should_pause(cfg, market_stress=m_stress, ticker_stress=t_stress)
    calm = int((state.get("calm_cycles") or {}).get(sym) or 0)
    resume = _should_resume(
        cfg,
        calm_cycles=calm,
        btc_ret=btc_ret,
        eth_ret=eth_ret,
        ticker_cum=ticker_cum,
        bar_ret=bar_ret,
        vol_ratio=vol_ratio,
        band_pct=float(band_pct),
    )
    dbg = {
        "btc_ret": btc_ret,
        "eth_ret": eth_ret,
        "ticker_cum": ticker_cum,
        "ticker_cum_dump_thresh": cum_dump,
        "ticker_cum_pump_thresh": cum_pump,
        "band_pct": float(band_pct),
        "ticker_bar": bar_ret,
        "vol_ratio": vol_ratio,
        "market_dump": m_dump,
        "market_pump": m_pump,
        "ticker_dump": t_dump,
        "ticker_pump": t_pump,
        "calm_cycles": calm,
    }
    return pause, resume, dbg


def _load_band_pcts(grid_tickers: Set[str]) -> Dict[str, float]:
    try:
        from strategy.gridstrat import grid_trading_ticker_band_pcts

        bands = grid_trading_ticker_band_pcts()
    except Exception:
        bands = {}
    out: Dict[str, float] = {}
    for sym in grid_tickers:
        s = str(sym).strip().upper()
        if not s:
            continue
        try:
            out[s] = float(bands.get(s, 2.5))
        except (TypeError, ValueError):
            out[s] = 2.5
    return out


def run_grid_vol_pause_cycle(
    ep: Any,
    *,
    cycle_index: int,
    grid_tickers: Set[str],
    varibot_dir: str,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    price_by_symbol: Optional[Dict[str, float]] = None,
) -> Set[str]:
    """
    Cycle-start volatility pause (dump + pump). Records prices, updates pause state.
    """
    cfg = load_config()
    if not cfg.enabled:
        return set()
    if not cfg.require_both:
        log(
            "grid_vol_pause: WARNING — REQUIRE_BOTH=0 (OR gate). "
            "Production uses AND (market BTC/ETH + ticker incl vol_ratio)."
        )
    elif int(cycle_index) == 1 or int(cycle_index) % 60 == 0:
        log(
            "grid_vol_pause: policy AND — market BTC/ETH 1h±2% + ticker "
            f"(band×{cfg.ticker_cum_band_mult} 30m, vol_ratio≥{cfg.vol_ratio_pause}, "
            f"min_hold={cfg.min_pause_cycles}m)"
        )

    path = default_state_path(varibot_dir)
    state = load_state(path)
    paused = paused_ticker_set(state)
    calm_map: Dict[str, int] = dict(state.get("calm_cycles") or {})

    syms = {str(s).strip().upper() for s in grid_tickers if str(s).strip()}
    band_pcts = _load_band_pcts(syms)
    fetch_syms = sorted({"BTC", "ETH"} | syms)

    prices: Dict[str, float] = {}
    if price_by_symbol:
        for k, v in price_by_symbol.items():
            try:
                fv = float(v)
                if fv > 0 and math.isfinite(fv):
                    prices[str(k).strip().upper()] = fv
            except (TypeError, ValueError):
                pass
    missing = [s for s in fetch_syms if s not in prices]
    if missing:
        try:
            prices.update(fetch_binance_futures_prices(missing))
        except Exception as e:
            log(f"grid_vol_pause: Binance price fetch failed ({type(e).__name__}: {e})")

    need_binance_market = int(cycle_index) < int(cfg.market_lb_cycles)
    need_binance_ticker_cum = int(cycle_index) < int(cfg.ticker_lb_cycles)
    need_binance_bar = int(cycle_index) < int(cfg.ticker_bar_lb_cycles)

    binance_market: Dict[str, float] = {}
    binance_ticker_30m: Dict[str, float] = {}
    binance_bar_5m: Dict[str, float] = {}

    try:
        if need_binance_market:
            binance_market = fetch_binance_kline_returns_parallel(
                ["BTC", "ETH"],
                limit=BINANCE_MARKET_KLINES_LIMIT,
                workers=cfg.binance_workers,
            )
        if need_binance_ticker_cum:
            binance_ticker_30m = fetch_binance_kline_returns_parallel(
                sorted(syms),
                limit=BINANCE_TICKER_CUM_KLINES_LIMIT,
                workers=cfg.binance_workers,
            )
        if need_binance_bar:
            bar_limit = kline_limit_for_cycles(cfg.ticker_bar_lb_cycles)
            binance_bar_5m = fetch_binance_kline_returns_parallel(
                sorted(syms),
                limit=bar_limit,
                workers=cfg.binance_workers,
            )
    except Exception as e:
        log(f"grid_vol_pause: Binance klines fetch failed ({type(e).__name__}: {e})")

    binance_vol_ratio: Dict[str, float] = {}
    try:
        binance_vol_ratio = fetch_binance_vol_ratios_parallel(
            sorted(syms),
            vol_lb=cfg.vol_lb_bars_5m,
            vol_median_lb=cfg.vol_median_bars_5m,
            workers=cfg.binance_workers,
        )
    except Exception as e:
        log(f"grid_vol_pause: Binance vol fetch failed ({type(e).__name__}: {e})")

    _append_history(state, cycle_index=int(cycle_index), prices=prices)

    btc_ret, eth_ret = _resolve_market_returns(
        cfg, state, cycle_index=int(cycle_index), binance_market=binance_market
    )

    newly: Set[str] = set()
    resumed: Set[str] = set()

    for sym in sorted(syms):
        try:
            ticker_cum = _resolve_ticker_cum_return(
                cfg,
                state,
                cycle_index=int(cycle_index),
                ticker=sym,
                binance_ticker_30m=binance_ticker_30m,
            )
            pause_now, resume_now, dbg = evaluate_ticker(
                cfg,
                state,
                cycle_index=int(cycle_index),
                ticker=sym,
                prices=prices,
                band_pct=float(band_pcts.get(sym, 2.5)),
                btc_ret=btc_ret,
                eth_ret=eth_ret,
                ticker_cum=ticker_cum,
                binance_bar_5m=binance_bar_5m,
                vol_ratio=binance_vol_ratio.get(sym),
            )
        except Exception as e:
            log(f"grid_vol_pause[{sym}]: evaluate failed ({type(e).__name__}: {e})")
            continue

        if sym in paused:
            if pause_now:
                calm_map[sym] = 0
            else:
                calm_map[sym] = int(calm_map.get(sym) or 0) + 1
                paused_entry = (state.get("paused") or {}).get(sym) or {}
                since_cycle = int(paused_entry.get("since_cycle") or cycle_index)
                cycles_held = int(cycle_index) - since_cycle
                cooldown_ok = cycles_held >= int(cfg.min_pause_cycles)
                if resume_now and cooldown_ok:
                    state.get("paused", {}).pop(sym, None)
                    calm_map[sym] = 0
                    resumed.add(sym)
                    log(
                        f"grid_vol_pause[{sym}]: RESUME after {cfg.resume_cycles} calm cycle(s) "
                        f"and {cycles_held}m hold (min {cfg.min_pause_cycles})"
                    )
            continue

        if pause_now:
            side = "dump"
            if dbg.get("market_pump") or dbg.get("ticker_pump"):
                side = "pump" if not (dbg.get("market_dump") or dbg.get("ticker_dump")) else "both"
            log(
                f"grid_vol_pause[{sym}]: PAUSE ({side}) "
                f"btc_1h={_fmt_ret(dbg.get('btc_ret'))} eth_1h={_fmt_ret(dbg.get('eth_ret'))} "
                f"ticker_30m={_fmt_ret(dbg.get('ticker_cum'))} "
                f"thresh={_fmt_ret(dbg.get('ticker_cum_dump_thresh'))}/"
                f"{_fmt_ret(dbg.get('ticker_cum_pump_thresh'))} "
                f"(band={dbg.get('band_pct')}%×{cfg.ticker_cum_band_mult}) "
                f"bar={_fmt_ret(dbg.get('ticker_bar'))} "
                f"vol_ratio={_fmt_ratio(dbg.get('vol_ratio'))} "
                f"({'AND' if cfg.require_both else 'OR'})"
            )
            cancel_ticker_limits(ep, ticker=sym, log=log, live=bool(live and not dry_run))
            if not isinstance(state.get("paused"), dict):
                state["paused"] = {}
            state["paused"][sym] = {
                "since_cycle": int(cycle_index),
                "reason": side,
                "debug": dbg,
            }
            calm_map[sym] = 0
            paused.add(sym)
            newly.add(sym)
        else:
            calm_map[sym] = 0

    state["calm_cycles"] = calm_map
    save_state(path, state)

    if newly:
        log(f"grid_vol_pause: paused {', '.join(sorted(newly))}")
    if resumed:
        log(f"grid_vol_pause: resumed {', '.join(sorted(resumed))}")
    if paused and not newly and not resumed:
        log(f"grid_vol_pause: still paused {', '.join(sorted(paused))}")

    return paused


def _fmt_ret(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_ratio(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}x"
    except (TypeError, ValueError):
        return "n/a"
