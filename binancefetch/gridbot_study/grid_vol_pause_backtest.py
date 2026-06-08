#!/usr/bin/env python3
"""Rolling-window paired-grid backtest with production vol-pause logic.

Tickers: ``GRID_TRADING_TICKERS`` from strategy/gridstrat.py (+ BTC/ETH market gate).
Data: gridbot_study_01-07JUN.sqlite (Binance USDT-M 5m).

Usage (repo root):
  python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py
  python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py --hours 24
  python3 binancefetch/gridbot_study/grid_vol_pause_backtest.py --hours 12 --json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.gridstrat import GRID_TRADING_TICKERS  # noqa: E402
from strategy.gridstrat_rearm import (  # noqa: E402
    PairedGridNumericConfig,
    derive_sim_ladder_params,
    ensure_bracket_rungs_around_mark,
    init_paired_state,
    paired_totals,
    step_mark_pair_sequential,
)

STUDY_DIR = Path(__file__).resolve().parent
DB_PATH = STUDY_DIR / "gridbot_study_01-07JUN.sqlite"
OUT_JSON = STUDY_DIR / "grid_vol_pause_backtest_last.json"

SGT = timezone(timedelta(hours=8))

# Paired grid sim (matches prior studies)
GRID_NUM = 8
INVESTMENT_USD = 25.0
LEVERAGE = 33.0
GRID_RESET = False

# Production vol-pause defaults (Varibot/grid_vol_pause.py) on 5m bars
MARKET_LB = 12  # 60 min
TICKER_LB = 6  # 30 min
VOL_LB = 36
VOL_MED = 72
MARKET_RET = -0.02
MARKET_PUMP = 0.02
TICKER_BAR_RET = -0.012
TICKER_BAR_PUMP = 0.012
TICKER_CUM_MULT = 1.6
VOL_RATIO_PAUSE = 1.3
RESUME_CYCLES = 18
MIN_PAUSE_CYCLES = 60
RESUME_MARKET_RET = -0.005
RESUME_MARKET_PUMP = 0.005
RESUME_TICKER_MULT = 0.4
RESUME_VOL_RATIO = 1.3
CYCLES_PER_BAR = 5
REQUIRE_BOTH = True


@dataclass(frozen=True)
class SimResult:
    ticker: str
    band_pct: float
    baseline_pnl: float
    vol_pnl: float
    pnl_delta: float
    baseline_dd: float
    vol_dd: float
    pauses: int
    price_chg_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "band_pct": self.band_pct,
            "baseline_pnl": round(self.baseline_pnl, 2),
            "vol_pnl": round(self.vol_pnl, 2),
            "pnl_delta": round(self.pnl_delta, 2),
            "baseline_dd": round(self.baseline_dd, 2),
            "vol_dd": round(self.vol_dd, 2),
            "pauses": self.pauses,
            "price_chg_pct": round(self.price_chg_pct, 2),
        }


def ticker_cum_thresholds(band_pct: float, mult: float) -> Tuple[float, float]:
    frac = (band_pct / 100.0) * mult
    return (-frac, +frac)


def load_series(conn: sqlite3.Connection, underlying: str) -> Dict[int, float]:
    rows = conn.execute(
        "SELECT open_time_ms, close FROM klines_5m WHERE underlying=? ORDER BY open_time_ms",
        (underlying,),
    ).fetchall()
    return {int(r[0]): float(r[1]) for r in rows}


def log_returns(closes: Sequence[float]) -> List[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        p0, p1 = closes[i - 1], closes[i]
        out.append(math.log(p1 / p0) if p0 > 0 and p1 > 0 else 0.0)
    return out


def cum_return(closes: Sequence[float], i: int, lb: int) -> float:
    if i < lb or closes[i - lb] <= 0:
        return 0.0
    return closes[i] / closes[i - lb] - 1.0


def rolling_std(xs: Sequence[float], i: int, lb: int) -> float:
    if i < lb:
        return 0.0
    window = xs[i - lb + 1 : i + 1]
    if len(window) < 2:
        return 0.0
    m = sum(window) / len(window)
    return math.sqrt(max(0.0, sum((x - m) ** 2 for x in window) / (len(window) - 1)))


def median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def precompute_vol_ratio(lr: Sequence[float]) -> List[float]:
    vol = [rolling_std(lr, i, VOL_LB) for i in range(len(lr))]
    vol_med = [0.0] * len(lr)
    for i in range(len(lr)):
        if i >= VOL_MED:
            vol_med[i] = median(vol[i - VOL_MED + 1 : i + 1])
    return [(vol[i] / vol_med[i] if vol_med[i] > 0 else 0.0) for i in range(len(lr))]


def market_stressed(btc_c: Sequence[float], eth_c: Sequence[float], i: int) -> bool:
    b = cum_return(btc_c, i, MARKET_LB)
    e = cum_return(eth_c, i, MARKET_LB)
    return (
        b <= MARKET_RET or b >= MARKET_PUMP
        or e <= MARKET_RET or e >= MARKET_PUMP
    )


def ticker_stressed(
    t_c: Sequence[float], vol_ratio: Sequence[float], i: int, band_pct: float
) -> bool:
    cum = cum_return(t_c, i, TICKER_LB)
    bar = t_c[i] / t_c[i - 1] - 1.0 if i > 0 and t_c[i - 1] > 0 else 0.0
    dump_t, pump_t = ticker_cum_thresholds(band_pct, TICKER_CUM_MULT)
    return (
        cum <= dump_t
        or cum >= pump_t
        or bar <= TICKER_BAR_RET
        or bar >= TICKER_BAR_PUMP
        or vol_ratio[i] >= VOL_RATIO_PAUSE
    )


def should_pause(market: bool, ticker: bool) -> bool:
    return market and ticker if REQUIRE_BOTH else market or ticker


def should_resume(
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    t_c: Sequence[float],
    vol_ratio: Sequence[float],
    i: int,
    band_pct: float,
    calm_cycles: int,
) -> bool:
    if calm_cycles < RESUME_CYCLES:
        return False
    b = cum_return(btc_c, i, MARKET_LB)
    e = cum_return(eth_c, i, MARKET_LB)
    if b <= RESUME_MARKET_RET or b >= RESUME_MARKET_PUMP:
        return False
    if e <= RESUME_MARKET_RET or e >= RESUME_MARKET_PUMP:
        return False
    cum = cum_return(t_c, i, TICKER_LB)
    rd, rp = ticker_cum_thresholds(band_pct, RESUME_TICKER_MULT)
    if cum <= rd or cum >= rp:
        return False
    bar = t_c[i] / t_c[i - 1] - 1.0 if i > 0 and t_c[i - 1] > 0 else 0.0
    if bar <= TICKER_BAR_RET * 0.5 or bar >= TICKER_BAR_PUMP * 0.5:
        return False
    if vol_ratio[i] > RESUME_VOL_RATIO:
        return False
    return True


def reinit_ladder(ticker: str, band_pct: float, mark: float, tick: int) -> Dict[str, Any]:
    anchor = float(mark)
    band = band_pct / 100.0
    lo, hi = anchor * (1.0 - band), anchor * (1.0 + band)
    pcfg = PairedGridNumericConfig(
        grid_num=GRID_NUM,
        investment_usd=INVESTMENT_USD,
        leverage=LEVERAGE,
        mark=anchor,
        grid_reset=GRID_RESET,
    )
    params = derive_sim_ladder_params(anchor=anchor, lower=lo, upper=hi, cfg=pcfg)
    params["asset"] = ticker
    st = init_paired_state(params=params, tick=tick)
    st["last_mark"] = anchor
    return st


def cancel_open(state: Dict[str, Any], tick: int) -> None:
    for o in state.get("orders") or []:
        if o.get("status") == "open":
            o["status"] = "cancelled"
            o["cancelled_at_tick"] = tick


def simulate(
    ticker: str,
    band_pct: float,
    t_c: Sequence[float],
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    vol_ratio: Sequence[float],
    sim_start_i: int,
    *,
    use_vol_pause: bool,
) -> Dict[str, float]:
    state = reinit_ladder(ticker, band_pct, t_c[sim_start_i], 0)
    paused = False
    calm_cycles = 0
    pause_held_cycles = 0
    pause_count = 0
    peak = 0.0
    max_dd = 0.0

    for i in range(sim_start_i + 1, len(t_c)):
        p_prev, p_now = float(t_c[i - 1]), float(t_c[i])
        if use_vol_pause:
            m = market_stressed(btc_c, eth_c, i)
            t = ticker_stressed(t_c, vol_ratio, i, band_pct)
            stressed = should_pause(m, t)
            if not paused:
                if stressed:
                    paused = True
                    calm_cycles = 0
                    pause_held_cycles = 0
                    cancel_open(state, i - sim_start_i)
                    pause_count += 1
            else:
                pause_held_cycles += CYCLES_PER_BAR
                calm_cycles = 0 if stressed else calm_cycles + CYCLES_PER_BAR
                if pause_held_cycles >= MIN_PAUSE_CYCLES and should_resume(
                    btc_c, eth_c, t_c, vol_ratio, i, band_pct, calm_cycles
                ):
                    paused = False
                    calm_cycles = pause_held_cycles = 0
                    inv = float(state["inventory"])
                    cost = float(state["inventory_cost"])
                    rpnl = float(state["realized_pnl"])
                    vol = float(state["volume_usd"])
                    state = reinit_ladder(ticker, band_pct, p_now, i - sim_start_i)
                    state.update(
                        inventory=inv,
                        inventory_cost=cost,
                        realized_pnl=rpnl,
                        volume_usd=vol,
                    )

        if not paused:
            step_mark_pair_sequential(
                state, p_prev=p_prev, p_now=p_now, grid_reset=GRID_RESET
            )
            ensure_bracket_rungs_around_mark(state, mark=p_now)

        _, _, total = paired_totals(state, mark=p_now)
        peak = max(peak, total)
        max_dd = max(max_dd, peak - total)

    _, _, total = paired_totals(state, mark=float(t_c[-1]))
    price_start = float(t_c[sim_start_i])
    price_chg = (float(t_c[-1]) / price_start - 1.0) * 100.0 if price_start > 0 else 0.0
    return {
        "pnl": total,
        "dd": max_dd,
        "pauses": float(pause_count),
        "price_chg_pct": price_chg,
    }


def run_backtest(hours: float, db_path: Path = DB_PATH) -> Dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Missing DB: {db_path} — run fetch_gridbot_study.py first")

    conn = sqlite3.connect(db_path)
    try:
        btc_map = load_series(conn, "BTC")
        eth_map = load_series(conn, "ETH")
        if not btc_map:
            raise ValueError("No BTC rows in DB")

        all_times = sorted(btc_map.keys())
        end_ms = all_times[-1]
        start_ms = end_ms - int(hours * 3600 * 1000)
        warmup_bars = max(MARKET_LB, TICKER_LB, VOL_LB + VOL_MED) + 2

        sim_start_i = next(i for i, t in enumerate(all_times) if t >= start_ms)
        warmup_start_i = max(0, sim_start_i - warmup_bars)
        window_times = all_times[warmup_start_i:]
        rel_sim_start = sim_start_i - warmup_start_i

        btc_c = [btc_map[t] for t in window_times]
        eth_c = [eth_map[t] for t in window_times]

        results: List[SimResult] = []
        for ticker, band_pct in GRID_TRADING_TICKERS.items():
            tmap = load_series(conn, ticker)
            t_c = [tmap.get(t, float("nan")) for t in window_times]
            if any(math.isnan(x) for x in t_c):
                continue
            vol_ratio = precompute_vol_ratio(log_returns(t_c))
            base = simulate(
                ticker, band_pct, t_c, btc_c, eth_c, vol_ratio, rel_sim_start,
                use_vol_pause=False,
            )
            prod = simulate(
                ticker, band_pct, t_c, btc_c, eth_c, vol_ratio, rel_sim_start,
                use_vol_pause=True,
            )
            results.append(
                SimResult(
                    ticker=ticker,
                    band_pct=band_pct,
                    baseline_pnl=base["pnl"],
                    vol_pnl=prod["pnl"],
                    pnl_delta=prod["pnl"] - base["pnl"],
                    baseline_dd=base["dd"],
                    vol_dd=prod["dd"],
                    pauses=int(prod["pauses"]),
                    price_chg_pct=prod["price_chg_pct"],
                )
            )
    finally:
        conn.close()

    base_total = sum(r.baseline_pnl for r in results)
    vol_total = sum(r.vol_pnl for r in results)
    start_sgt = datetime.fromtimestamp(start_ms / 1000, SGT)
    end_sgt = datetime.fromtimestamp(end_ms / 1000, SGT)

    return {
        "hours": hours,
        "window_start_sgt": start_sgt.isoformat(),
        "window_end_sgt": end_sgt.isoformat(),
        "bars_in_window": len(window_times) - rel_sim_start - 1,
        "warmup_bars": rel_sim_start,
        "tickers": [r.to_dict() for r in sorted(results, key=lambda x: x.vol_pnl, reverse=True)],
        "totals": {
            "baseline_pnl": round(base_total, 2),
            "vol_pnl": round(vol_total, 2),
            "pnl_delta": round(vol_total - base_total, 2),
        },
        "params": {
            "grid_num": GRID_NUM,
            "investment_usd": INVESTMENT_USD,
            "leverage": LEVERAGE,
            "require_both": REQUIRE_BOTH,
            "market_1h": f"±{abs(MARKET_RET)*100:.0f}%",
            "ticker_cum": f"band×{TICKER_CUM_MULT}",
            "vol_ratio_pause": VOL_RATIO_PAUSE,
            "min_pause_min": MIN_PAUSE_CYCLES,
        },
    }


def print_report(payload: Dict[str, Any]) -> None:
    print(
        f"Window: {payload['window_start_sgt'][:16].replace('T', ' ')} → "
        f"{payload['window_end_sgt'][:16].replace('T', ' ')} SGT"
    )
    print(f"Bars: {payload['bars_in_window']} (5m) | warmup: {payload['warmup_bars']}")
    p = payload["params"]
    print(
        f"Grid: {p['grid_num']} rungs, ${p['investment_usd']:.0f} × {p['leverage']:.0f}x | "
        f"AND gate, BTC/ETH {p['market_1h']}, ticker {p['ticker_cum']}, "
        f"vol≥{p['vol_ratio_pause']}, min_hold {p['min_pause_min']}m"
    )
    print()
    print(f"{'Ticker':<8} {'Band':>4} {'Price%':>7} {'Base$':>8} {'Vol$':>8} {'Δ$':>7} {'Pauses':>6}")
    print("-" * 58)
    for r in payload["tickers"]:
        print(
            f"{r['ticker']:<8} {r['band_pct']:>4.1f} {r['price_chg_pct']:>+6.1f}% "
            f"{r['baseline_pnl']:>8.2f} {r['vol_pnl']:>8.2f} {r['pnl_delta']:>+7.2f} "
            f"{r['pauses']:>6}"
        )
    t = payload["totals"]
    print("-" * 58)
    print(
        f"{'TOTAL':<8} {'':>4} {'':>7} {t['baseline_pnl']:>8.2f} "
        f"{t['vol_pnl']:>8.2f} {t['pnl_delta']:>+7.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Grid vol-pause rolling backtest")
    ap.add_argument("--hours", type=float, default=12.0, help="Lookback hours (default 12)")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="SQLite path")
    ap.add_argument("--json", action="store_true", help="Write JSON to grid_vol_pause_backtest_last.json")
    ap.add_argument("--quiet", action="store_true", help="JSON only, no table")
    args = ap.parse_args()

    payload = run_backtest(args.hours, db_path=args.db)
    payload["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()

    if not args.quiet:
        print_report(payload)

    if args.json:
        OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"\nWrote {OUT_JSON}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
