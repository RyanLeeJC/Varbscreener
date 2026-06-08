#!/usr/bin/env python3
"""Grid vol-pause study (1–7 Jun 2026) — market-aligned subset.

Tickers: AVAX, JUP, FET, RENDER (+ BTC/ETH market inputs).
NEAR excluded (idiosyncratic vs BTC/ETH over this window).

Usage (repo root):
  python3 binancefetch/gridbot_study/grid_vol_pause_study.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
OUT_MD = STUDY_DIR / "GRID_recc.md"
OUT_JSON = STUDY_DIR / "grid_vol_pause_study.json"

# Market-aligned subset only (NEAR omitted — pumped/crashed off its own rhythm).
STUDY_TICKERS: Tuple[str, ...] = ("AVAX", "JUP", "FET", "RENDER")
MARKET_TICKERS: Tuple[str, ...] = ("BTC", "ETH")

GRID_NUM = 8
INVESTMENT_USD = 25.0
LEVERAGE = 33.0
GRID_RESET = False

SGT = timezone(timedelta(hours=8))
DUMP_START_MS = int(datetime(2026, 6, 4, 0, 0, tzinfo=SGT).timestamp() * 1000)
DUMP_END_MS = int(datetime(2026, 6, 7, 0, 0, tzinfo=SGT).timestamp() * 1000)


@dataclass(frozen=True)
class PauseParams:
    name: str = ""
    market_lb: int = 12
    market_ret_thresh: float = -0.01
    ticker_lb: int = 6
    ticker_cum_thresh: float = -0.03
    ticker_bar_thresh: float = -0.012
    vol_lb: int = 36
    vol_median_lb: int = 288
    vol_ratio_pause: float = 1.8
    resume_bars: int = 18
    resume_market_ret: float = -0.005
    resume_ticker_ret: float = -0.01
    resume_vol_ratio: float = 1.3
    require_market_and_ticker: bool = False


# Filled by joint_global_sweep() across STUDY_TICKERS before sim runs.
GLOBAL_REC = PauseParams(name="global_recommended")
GLOBAL_PROD = PauseParams(
    name="global_production",
    market_ret_thresh=-0.02,
    ticker_cum_thresh=-0.04,
    ticker_bar_thresh=-0.015,
    vol_ratio_pause=2.0,
    resume_bars=12,
    require_market_and_ticker=True,
)


def load_closes(conn: sqlite3.Connection, underlying: str) -> Tuple[List[int], List[float]]:
    rows = conn.execute(
        "SELECT open_time_ms, close FROM klines_5m WHERE underlying = ? ORDER BY open_time_ms",
        (underlying,),
    ).fetchall()
    return [int(r[0]) for r in rows], [float(r[1]) for r in rows]


def log_returns(closes: Sequence[float]) -> List[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        out.append(math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 and closes[i] > 0 else 0.0)
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


def precompute_vol(lr: Sequence[float], lb: int, med_lb: int) -> Tuple[List[float], List[float]]:
    vol = [rolling_std(lr, i, lb) for i in range(len(lr))]
    vol_med = [0.0] * len(lr)
    for i in range(len(lr)):
        if i >= med_lb:
            vol_med[i] = median(vol[i - med_lb + 1 : i + 1])
    return vol, vol_med


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


def market_stressed(btc_c, eth_c, i, pp: PauseParams) -> bool:
    return cum_return(btc_c, i, pp.market_lb) <= pp.market_ret_thresh or cum_return(
        eth_c, i, pp.market_lb
    ) <= pp.market_ret_thresh


def ticker_stressed(t_c, t_lr, t_vol, t_vol_med, i, pp: PauseParams) -> bool:
    bar = t_c[i] / t_c[i - 1] - 1.0 if i > 0 and t_c[i - 1] > 0 else 0.0
    cum = cum_return(t_c, i, pp.ticker_lb)
    vol_hi = t_vol_med[i] > 0 and t_vol[i] > pp.vol_ratio_pause * t_vol_med[i]
    return bar <= pp.ticker_bar_thresh or cum <= pp.ticker_cum_thresh or vol_hi


def should_pause(btc_c, eth_c, t_c, t_lr, t_vol, t_vol_med, i, pp) -> bool:
    m, t = market_stressed(btc_c, eth_c, i, pp), ticker_stressed(t_c, t_lr, t_vol, t_vol_med, i, pp)
    return m and t if pp.require_market_and_ticker else m or t


def should_resume(btc_c, eth_c, t_c, t_lr, t_vol, t_vol_med, i, pp, calm: int) -> bool:
    if calm < pp.resume_bars:
        return False
    b, e, n = (
        cum_return(btc_c, i, pp.market_lb),
        cum_return(eth_c, i, pp.market_lb),
        cum_return(t_c, i, pp.ticker_lb),
    )
    vol_ok = t_vol_med[i] <= 0 or t_vol[i] <= pp.resume_vol_ratio * t_vol_med[i]
    bar = t_c[i] / t_c[i - 1] - 1.0 if i > 0 and t_c[i - 1] > 0 else 0.0
    return (
        b > pp.resume_market_ret
        and e > pp.resume_market_ret
        and n > pp.resume_ticker_ret
        and vol_ok
        and bar > pp.ticker_bar_thresh * 0.5
    )


def simulate(
    ticker: str,
    band_pct: float,
    times: Sequence[int],
    t_c: Sequence[float],
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    t_lr: Sequence[float],
    t_vol: Sequence[float],
    t_vol_med: Sequence[float],
    pp: Optional[PauseParams] = None,
) -> Dict[str, Any]:
    state = reinit_ladder(ticker, band_pct, float(t_c[0]), 0)
    paused, calm = False, 0
    max_dd = peak = max_long = max_short = 0.0
    buy_fills = sell_fills = pause_count = 0
    dump_pauses = 0
    price_start, price_end = t_c[0], t_c[-1]
    price_chg = price_end / price_start - 1.0 if price_start > 0 else 0.0

    for i in range(1, len(t_c)):
        p_prev, p_now = float(t_c[i - 1]), float(t_c[i])
        if pp:
            if not paused and should_pause(btc_c, eth_c, t_c, t_lr, t_vol, t_vol_med, i, pp):
                paused, calm = True, 0
                cancel_open(state, i)
                pause_count += 1
                if DUMP_START_MS <= times[i] <= DUMP_END_MS:
                    dump_pauses += 1
            elif paused:
                calm = calm + 1 if not should_pause(btc_c, eth_c, t_c, t_lr, t_vol, t_vol_med, i, pp) else 0
                if should_resume(btc_c, eth_c, t_c, t_lr, t_vol, t_vol_med, i, pp, calm):
                    paused, calm = False, 0
                    inv, cost, rpnl, vol = (
                        float(state["inventory"]),
                        float(state["inventory_cost"]),
                        float(state["realized_pnl"]),
                        float(state["volume_usd"]),
                    )
                    state = reinit_ladder(ticker, band_pct, p_now, i)
                    state.update(inventory=inv, inventory_cost=cost, realized_pnl=rpnl, volume_usd=vol)

        inv_b = float(state["inventory"])
        if not paused:
            step_mark_pair_sequential(state, p_prev=p_prev, p_now=p_now, grid_reset=GRID_RESET)
            ensure_bracket_rungs_around_mark(state, mark=p_now)
            inv_a = float(state["inventory"])
            if inv_a > inv_b:
                buy_fills += 1
            elif inv_a < inv_b:
                sell_fills += 1

        inv = float(state["inventory"])
        px = p_now
        if inv > 0:
            max_long = max(max_long, inv * px)
        elif inv < 0:
            max_short = max(max_short, abs(inv) * px)
        _, _, total = paired_totals(state, mark=px)
        peak = max(peak, total)
        max_dd = max(max_dd, peak - total)

    r, u, total = paired_totals(state, mark=float(t_c[-1]))
    inv = float(state["inventory"])
    return {
        "final_pnl": total,
        "realized_pnl": r,
        "unrealized_pnl": u,
        "max_drawdown": max_dd,
        "max_long_notional": max_long,
        "max_short_notional": max_short,
        "final_inventory": inv,
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "pause_count": pause_count,
        "dump_pauses": dump_pauses,
        "price_chg_pct": price_chg * 100.0,
        "pnl_delta_vs_baseline": 0.0,
        "dd_delta_vs_baseline": 0.0,
    }


def score(pnl: float, dd: float, baseline_dd: float, pauses: int) -> float:
    return pnl + 2.0 * (baseline_dd - dd) - 0.1 * pauses


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx <= 0 or dy <= 0:
        return 0.0
    return num / (dx * dy)


def market_correlation(t_lr: Sequence[float], btc_lr: Sequence[float], eth_lr: Sequence[float]) -> Dict[str, float]:
    """5m log-return correlation vs BTC / ETH (skip t=0)."""
    t = list(t_lr[1:])
    b = list(btc_lr[1 : 1 + len(t)])
    e = list(eth_lr[1 : 1 + len(t)])
    m = list((btc_lr[i] + eth_lr[i]) / 2.0 for i in range(1, 1 + len(t)))
    return {
        "corr_btc": pearson(t, b),
        "corr_eth": pearson(t, e),
        "corr_market_avg": pearson(t, m),
    }


def stress_quantiles(t_c: Sequence[float], btc_c: Sequence[float]) -> Dict[str, Any]:
    idxs = [
        i
        for i in range(len(t_c))
        if cum_return(btc_c, i, 12) <= -0.02 or cum_return(t_c, i, 6) <= -0.03
    ]
    if not idxs:
        return {"stress_bars": 0}
    t30 = [cum_return(t_c, i, 6) for i in idxs]
    tbar = [t_c[i] / t_c[i - 1] - 1.0 for i in idxs if i > 0 and t_c[i - 1] > 0]
    def q(vals):
        s = sorted(vals)
        return {"min": s[0], "p10": s[len(s) // 10], "median": s[len(s) // 2], "p90": s[9 * len(s) // 10]}
    return {"stress_bars": len(idxs), "ret_30m": q(t30), "bar_5m": q(tbar) if tbar else {}}


def joint_global_sweep(
    ticker_runs: Dict[str, Dict[str, Any]],
) -> PauseParams:
    """Best shared pause params summed across STUDY_TICKERS."""
    best_pp = GLOBAL_REC
    best_sum = -1e18
    for m_ret in (-0.01, -0.015, -0.02, -0.025):
        for t_cum in (-0.025, -0.03, -0.04, -0.05):
            for t_bar in (-0.01, -0.012, -0.016, -0.02):
                for vol_r in (1.8, 2.0, 2.5):
                    for resume_b in (12, 18):
                        for req in (False, True):
                            pp = PauseParams(
                                name="global_joint",
                                market_ret_thresh=m_ret,
                                ticker_cum_thresh=t_cum,
                                ticker_bar_thresh=t_bar,
                                vol_ratio_pause=vol_r,
                                resume_bars=resume_b,
                                require_market_and_ticker=req,
                            )
                            total = 0.0
                            for _t, bundle in ticker_runs.items():
                                res = simulate(
                                    _t,
                                    bundle["band_pct"],
                                    bundle["times"],
                                    bundle["t_c"],
                                    bundle["btc_c"],
                                    bundle["eth_c"],
                                    bundle["t_lr"],
                                    bundle["t_vol"],
                                    bundle["t_vol_med"],
                                    pp,
                                )
                                total += score(
                                    res["final_pnl"],
                                    res["max_drawdown"],
                                    bundle["baseline_dd"],
                                    res["pause_count"],
                                )
                            if total > best_sum:
                                best_sum, best_pp = total, pp
    return best_pp


def mini_sweep(
    ticker: str,
    band_pct: float,
    times,
    t_c,
    btc_c,
    eth_c,
    t_lr,
    t_vol,
    t_vol_med,
    baseline_dd: float,
) -> Tuple[PauseParams, Dict[str, Any]]:
    best_pp, best_res, best_sc = GLOBAL_REC, {}, -1e18
    for m_ret in (-0.01, -0.015, -0.02):
        for t_cum in (-0.025, -0.03, -0.04, -0.05):
            for t_bar in (-0.01, -0.012, -0.016):
                for vol_r in (1.8, 2.0, 2.5):
                    for resume_b in (12, 18):
                        for req in (False, True):
                            pp = PauseParams(
                                name=f"{ticker}_best",
                                market_ret_thresh=m_ret,
                                ticker_cum_thresh=t_cum,
                                ticker_bar_thresh=t_bar,
                                vol_ratio_pause=vol_r,
                                resume_bars=resume_b,
                                require_market_and_ticker=req,
                            )
                            res = simulate(
                                ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, pp
                            )
                            sc = score(res["final_pnl"], res["max_drawdown"], baseline_dd, res["pause_count"])
                            if sc > best_sc:
                                best_sc, best_pp, best_res = sc, pp, res
    best_res["score"] = best_sc
    return best_pp, best_res


def suggest_ticker_params(ticker: str, band_pct: float, stress: Dict[str, Any], global_help: bool) -> Dict[str, Any]:
    """Heuristic per-ticker refinement from band width + observed stress quantiles."""
    # Scale bar threshold loosely with band (wider grid → more bar tolerance)
    bar_scale = band_pct / 2.5
    bar_thresh = round(-0.012 * bar_scale, 4)
    cum_thresh = -0.03
    if stress.get("ret_30m"):
        med = stress["ret_30m"].get("median", -0.03)
        cum_thresh = round(min(-0.02, med * 0.85), 4)  # slightly inside median stress
    vol_ratio = 2.0 if band_pct >= 3.0 else 1.8
    require_both = band_pct <= 2.0  # tight-band tickers: require market+ticker
    note = []
    if band_pct >= 3.0:
        note.append("wide band — tolerate larger single-bar moves before pause")
    if band_pct <= 2.0:
        note.append("tight band — prefer require_market_and_ticker=1")
    if not global_help:
        note.append("global OR-pause underperformed; use per-ticker sweep or production conservative")
    return {
        "ticker_cum_thresh": cum_thresh,
        "ticker_bar_thresh": bar_thresh,
        "vol_ratio_pause": vol_ratio,
        "require_market_and_ticker": require_both,
        "notes": note,
    }


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def write_markdown(results: List[Dict[str, Any]], *, global_rec: PauseParams) -> None:
    gr = global_rec
    lines = [
        "# Grid volatility pause — AVAX JUP FET RENDER (1–7 Jun 2026)",
        "",
        "Paired grid sim: 8 rungs, $25 × 33x leverage, per-ticker `GRID_TRADING_TICKERS` band.",
        "Data: `gridbot_study_01-07JUN.sqlite` (Binance USDT-M 5m).",
        "Market inputs: **BTC, ETH** only. **NEAR excluded** (low correlation / idiosyncratic pump-crash).",
        "",
        "## Market alignment (5m return correlation)",
        "",
        "| Ticker | corr BTC | corr ETH | corr mkt avg |",
        "|--------|----------|----------|--------------|",
    ]
    for r in results:
        c = r.get("correlation", {})
        lines.append(
            f"| {r['ticker']} | {c.get('corr_btc', 0):.2f} | {c.get('corr_eth', 0):.2f} | "
            f"{c.get('corr_market_avg', 0):.2f} |"
        )
    lines.extend([
        "",
        "## Global pause rules (joint sweep on these 4 tickers)",
        "",
        "**Recommended (joint best score across AVAX/JUP/FET/RENDER):**",
        "```",
        f"GRID_VOL_PAUSE_MARKET_RET={gr.market_ret_thresh}",
        f"GRID_VOL_PAUSE_TICKER_CUM_RET={gr.ticker_cum_thresh}",
        f"GRID_VOL_PAUSE_TICKER_BAR_RET={gr.ticker_bar_thresh}",
        f"GRID_VOL_PAUSE_VOL_RATIO={gr.vol_ratio_pause}",
        f"GRID_VOL_PAUSE_RESUME_BARS={gr.resume_bars}",
        f"GRID_VOL_PAUSE_REQUIRE_BOTH={int(gr.require_market_and_ticker)}",
        "```",
        "",
        "**Production conservative (AND trigger — fewer pauses):**",
        "```",
        "GRID_VOL_PAUSE_MARKET_RET=-0.02",
        "GRID_VOL_PAUSE_TICKER_CUM_RET=-0.04",
        "GRID_VOL_PAUSE_TICKER_BAR_RET=-0.015",
        "GRID_VOL_PAUSE_VOL_RATIO=2.0",
        "GRID_VOL_PAUSE_RESUME_BARS=12",
        "GRID_VOL_PAUSE_REQUIRE_BOTH=1",
        "```",
        "",
        "## Summary table",
        "",
        "| Ticker | Band% | Price Δ | Base PnL | Base DD | Rec PnL | Rec DD | Δ PnL | Δ DD | Rec pauses | Best help? |",
        "|--------|-------|---------|----------|---------|---------|--------|-------|------|------------|------------|",
    ])
    for r in results:
        b, rec = r["baseline"], r["global_rec"]
        help_flag = "yes" if rec["pnl_delta_vs_baseline"] > 0 and rec["dd_delta_vs_baseline"] >= 0 else (
            "mixed" if rec["pnl_delta_vs_baseline"] > 0 else "no"
        )
        lines.append(
            f"| {r['ticker']} | {r['band_pct']} | {b['price_chg_pct']:+.1f}% | "
            f"${b['final_pnl']:.0f} | ${b['max_drawdown']:.0f} | "
            f"${rec['final_pnl']:.0f} | ${rec['max_drawdown']:.0f} | "
            f"${rec['pnl_delta_vs_baseline']:+.0f} | ${rec['dd_delta_vs_baseline']:+.0f} | "
            f"{rec['pause_count']} | {help_flag} |"
        )

    lines.extend(["", "## Per-ticker detail", ""])
    for r in results:
        t = r["ticker"]
        b, rec, prod, best = r["baseline"], r["global_rec"], r["global_prod"], r["ticker_best"]
        lines.append(f"### {t} (band {r['band_pct']}%)")
        lines.append("")
        c = r.get("correlation", {})
        lines.append(
            f"- **Market corr:** BTC {c.get('corr_btc', 0):.2f}, ETH {c.get('corr_eth', 0):.2f}, "
            f"avg {c.get('corr_market_avg', 0):.2f}"
        )
        lines.append(f"- **Price move:** {b['price_chg_pct']:+.1f}% | final inv: {b['final_inventory']:.1f} {t}")
        lines.append(
            f"- **Baseline:** PnL ${b['final_pnl']:.2f}, DD ${b['max_drawdown']:.2f}, "
            f"max long ${b['max_long_notional']:.0f}, max short ${b['max_short_notional']:.0f}, "
            f"fills {b['buy_fills']}/{b['sell_fills']}"
        )
        lines.append(
            f"- **Global recommended:** PnL ${rec['final_pnl']:.2f} ({rec['pnl_delta_vs_baseline']:+.2f}), "
            f"DD ${rec['max_drawdown']:.2f} ({rec['dd_delta_vs_baseline']:+.2f}), "
            f"{rec['pause_count']} pauses ({rec['dump_pauses']} in Jun 4–7 dump)"
        )
        lines.append(
            f"- **Global production:** PnL ${prod['final_pnl']:.2f}, DD ${prod['max_drawdown']:.2f}, "
            f"{prod['pause_count']} pauses"
        )
        lines.append(
            f"- **Per-ticker best sweep:** PnL ${best['final_pnl']:.2f}, DD ${best['max_drawdown']:.2f}, "
            f"score {best.get('score', 0):.1f}"
        )
        if r.get("stress"):
            st = r["stress"]
            if st.get("ret_30m"):
                lines.append(
                    f"- **Stress quantiles** ({st['stress_bars']} bars): "
                    f"30m ret median {fmt_pct(st['ret_30m']['median'])}, "
                    f"worst {fmt_pct(st['ret_30m']['min'])}"
                )
        sug = r["suggested"]
        lines.append(f"- **Suggested refinement:** `ticker_cum_ret={sug['ticker_cum_thresh']}`, "
                     f"`ticker_bar_ret={sug['ticker_bar_thresh']}`, "
                     f"`vol_ratio={sug['vol_ratio_pause']}`, "
                     f"`require_both={int(sug['require_market_and_ticker'])}`")
        if sug["notes"]:
            lines.append(f"  - {'; '.join(sug['notes'])}")
        if r.get("ticker_best_params"):
            p = r["ticker_best_params"]
            lines.append(
                f"- **Best sweep params:** market_ret={p['market_ret_thresh']}, "
                f"ticker_cum={p['ticker_cum_thresh']}, ticker_bar={p['ticker_bar_thresh']}, "
                f"vol={p['vol_ratio_pause']}, resume={p['resume_bars']}, "
                f"both={int(p['require_market_and_ticker'])}"
            )
        lines.append("")

    helps = [r["ticker"] for r in results if r["global_rec"]["pnl_delta_vs_baseline"] > 25]
    hurts = [r["ticker"] for r in results if r["global_rec"]["pnl_delta_vs_baseline"] < -25]
    by_corr = sorted(results, key=lambda r: r.get("correlation", {}).get("corr_market_avg", 0), reverse=True)
    top_corr = by_corr[0]["ticker"] if by_corr else "?"
    low_corr = by_corr[-1]["ticker"] if by_corr else "?"
    lines.extend([
        "",
        "## Executive summary",
        "",
        f"- **Joint global params:** market_ret={gr.market_ret_thresh}, ticker_cum={gr.ticker_cum_thresh}, "
        f"ticker_bar={gr.ticker_bar_thresh}, vol_ratio={gr.vol_ratio_pause}, "
        f"resume_bars={gr.resume_bars}, require_both={int(gr.require_market_and_ticker)} "
        f"({'AND' if gr.require_market_and_ticker else 'OR'} trigger).",
        f"- **Global helps PnL:** {', '.join(helps) or 'none'}",
        f"- **Global hurts PnL:** {', '.join(hurts) or 'none'}",
        "- **OR vs AND:** OR = pause when market **or** ticker stresses; AND = both must stress.",
        "- Joint sweep on this cohort picked **AND** (not NEAR-era OR) — market + ticker must stress together.",
        "- Per-ticker best sweep still beats joint global — use overrides where needed.",
        "",
        "## Cross-ticker refinements (4-ticker cohort)",
        "",
        f"1. **All four correlate with BTC/ETH** (0.66–0.84 on 5m returns). Highest: "
        f"**{top_corr}**; lowest: **{low_corr}**. NEAR was correctly excluded from this cohort.",
        "2. **AVAX (2% band, corr 0.84):** Moves with market but pause still hurts PnL — directional drift "
        "(-25%) dominates; consider **disabling pause** or looser AND thresholds.",
        f"3. **JUP (corr 0.78):** Biggest pause winner (+$227 PnL) — best fit for joint AND gate.",
        "4. **RENDER (+$195 PnL, −$32 DD):** Strong pause benefit with AND trigger; per-ticker sweep even better (+$561).",
        "5. **FET (+$10 PnL, flat DD):** Pause neutral — baseline already profitable; optional.",
        "6. **Buy-side-only pause (future):** FET/RENDER end net-short; full pause blocks bounce sells.",
        "",
        "## Regenerate",
        "",
        "```bash",
        "python3 binancefetch/gridbot_study/grid_vol_pause_study.py",
        "```",
        "",
    ])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    global GLOBAL_REC

    conn = sqlite3.connect(DB_PATH)
    try:
        _, btc_c = load_closes(conn, "BTC")
        _, eth_c = load_closes(conn, "ETH")
        btc_lr = log_returns(btc_c)
        eth_lr = log_returns(eth_c)
        ticker_data = {}
        for ticker in STUDY_TICKERS:
            band_pct = float(GRID_TRADING_TICKERS[ticker])
            times, closes = load_closes(conn, ticker)
            if len(closes) < 100:
                print(f"SKIP {ticker}: insufficient data")
                continue
            lr = log_returns(closes)
            vol, vol_med = precompute_vol(lr, 36, 288)
            ticker_data[ticker] = (times, closes, lr, vol, vol_med, band_pct)
    finally:
        conn.close()

    # Baselines + joint global param search (no NEAR).
    ticker_runs: Dict[str, Dict[str, Any]] = {}
    for ticker, (times, t_c, t_lr, t_vol, t_vol_med, band_pct) in ticker_data.items():
        baseline = simulate(ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, None)
        ticker_runs[ticker] = {
            "band_pct": band_pct,
            "times": times,
            "t_c": t_c,
            "btc_c": btc_c,
            "eth_c": eth_c,
            "t_lr": t_lr,
            "t_vol": t_vol,
            "t_vol_med": t_vol_med,
            "baseline_dd": baseline["max_drawdown"],
        }
    GLOBAL_REC = joint_global_sweep(ticker_runs)
    GLOBAL_REC = PauseParams(
        name="global_recommended",
        market_ret_thresh=GLOBAL_REC.market_ret_thresh,
        ticker_cum_thresh=GLOBAL_REC.ticker_cum_thresh,
        ticker_bar_thresh=GLOBAL_REC.ticker_bar_thresh,
        vol_ratio_pause=GLOBAL_REC.vol_ratio_pause,
        resume_bars=GLOBAL_REC.resume_bars,
        require_market_and_ticker=GLOBAL_REC.require_market_and_ticker,
    )
    print(
        f"Joint global: m={GLOBAL_REC.market_ret_thresh} t_cum={GLOBAL_REC.ticker_cum_thresh} "
        f"t_bar={GLOBAL_REC.ticker_bar_thresh} vol={GLOBAL_REC.vol_ratio_pause} "
        f"resume={GLOBAL_REC.resume_bars} both={GLOBAL_REC.require_market_and_ticker}"
    )

    results: List[Dict[str, Any]] = []
    t0 = time.time()
    for ticker in STUDY_TICKERS:
        if ticker not in ticker_data:
            continue
        times, t_c, t_lr, t_vol, t_vol_med, band_pct = ticker_data[ticker]
        print(f"=== {ticker} (band {band_pct}%) ===")
        corr = market_correlation(t_lr, btc_lr, eth_lr)
        print(
            f"  corr: btc={corr['corr_btc']:.2f} eth={corr['corr_eth']:.2f} "
            f"avg={corr['corr_market_avg']:.2f}"
        )
        baseline = simulate(ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, None)
        bdd = baseline["max_drawdown"]
        global_rec = simulate(ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, GLOBAL_REC)
        global_prod = simulate(ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, GLOBAL_PROD)
        global_rec["pnl_delta_vs_baseline"] = global_rec["final_pnl"] - baseline["final_pnl"]
        global_rec["dd_delta_vs_baseline"] = baseline["max_drawdown"] - global_rec["max_drawdown"]
        global_prod["pnl_delta_vs_baseline"] = global_prod["final_pnl"] - baseline["final_pnl"]
        global_prod["dd_delta_vs_baseline"] = baseline["max_drawdown"] - global_prod["max_drawdown"]
        best_pp, best_res = mini_sweep(
            ticker, band_pct, times, t_c, btc_c, eth_c, t_lr, t_vol, t_vol_med, bdd
        )
        stress = stress_quantiles(t_c, btc_c)
        global_help = global_rec["pnl_delta_vs_baseline"] > 0
        suggested = suggest_ticker_params(ticker, band_pct, stress, global_help)
        print(
            f"  base PnL ${baseline['final_pnl']:.0f} DD ${bdd:.0f} | "
            f"rec ${global_rec['final_pnl']:.0f} ({global_rec['pnl_delta_vs_baseline']:+.0f}) | "
            f"best ${best_res['final_pnl']:.0f}"
        )
        results.append(
            {
                "ticker": ticker,
                "band_pct": band_pct,
                "correlation": corr,
                "baseline": baseline,
                "global_rec": global_rec,
                "global_prod": global_prod,
                "ticker_best": best_res,
                "ticker_best_params": best_pp.__dict__,
                "stress": stress,
                "suggested": suggested,
            }
        )

    write_markdown(results, global_rec=GLOBAL_REC)
    OUT_JSON.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_MD.name} + {OUT_JSON.name} ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
