#!/usr/bin/env python3
"""NEAR paired-grid sim with BTC/ETH + ticker volatility pause rules.

Uses gridbot_study_01-07JUN.sqlite (Binance USDT-M 5m).
Outputs NEAR_vol_pause_sim.data.js for sibling HTML.

Usage (repo root):
  python3 binancefetch/gridbot_study/near_vol_pause_sim.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
OUT_DATA = STUDY_DIR / "NEAR_vol_pause_sim.data.js"
OUT_HTML = STUDY_DIR / "NEAR_vol_pause_sim.html"

TICKER = "NEAR"
BAND_PCT = 2.5  # GRID_TRADING_TICKERS
GRID_NUM = 8
INVESTMENT_USD = 25.0
LEVERAGE = 33.0
GRID_RESET = False  # halt on breach (production default)


@dataclass(frozen=True)
class PauseParams:
    name: str
    # Market (BTC+ETH): cumulative log-return over lookback must be below threshold to stress.
    market_lb: int = 12  # bars (12×5m = 1h)
    market_ret_thresh: float = -0.015  # -1.5% over lookback (either BTC or ETH)
    # Ticker: cumulative OR single-bar pierce.
    ticker_lb: int = 6
    ticker_cum_thresh: float = -0.03  # -3% over 30m
    ticker_bar_thresh: float = -0.012  # -1.2% single 5m bar
    # Vol: rolling std of 1-bar log returns vs trailing median.
    vol_lb: int = 36  # 3h
    vol_median_lb: int = 288  # 24h reference
    vol_ratio_pause: float = 2.0  # pause when vol > ratio × median vol
    # Resume: hysteresis.
    resume_bars: int = 12  # 1h calm
    resume_market_ret: float = -0.005  # market cum ret must be above this
    resume_ticker_ret: float = -0.01
    resume_vol_ratio: float = 1.3
    require_market_and_ticker: bool = True  # both stressed to pause


def load_closes(conn: sqlite3.Connection, underlying: str) -> Tuple[List[int], List[float]]:
    rows = conn.execute(
        """
        SELECT open_time_ms, close FROM klines_5m
        WHERE underlying = ? ORDER BY open_time_ms
        """,
        (underlying,),
    ).fetchall()
    times = [int(r[0]) for r in rows]
    closes = [float(r[1]) for r in rows]
    return times, closes


def log_returns(closes: Sequence[float]) -> List[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out.append(math.log(closes[i] / closes[i - 1]))
        else:
            out.append(0.0)
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
    var = sum((x - m) ** 2 for x in window) / (len(window) - 1)
    return math.sqrt(max(0.0, var))


def median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def cancel_open_orders(state: Dict[str, Any], tick: int) -> None:
    for o in state.get("orders") or []:
        if o.get("status") == "open":
            o["status"] = "cancelled"
            o["cancelled_at_tick"] = tick


def reinit_ladder_at_mark(state: Dict[str, Any], mark: float, *, tick: int) -> Dict[str, Any]:
    anchor = float(mark)
    band = BAND_PCT / 100.0
    lo = anchor * (1.0 - band)
    hi = anchor * (1.0 + band)
    pcfg = PairedGridNumericConfig(
        grid_num=GRID_NUM,
        investment_usd=INVESTMENT_USD,
        leverage=LEVERAGE,
        mark=float(mark),
        grid_reset=GRID_RESET,
    )
    params = derive_sim_ladder_params(anchor=anchor, lower=lo, upper=hi, cfg=pcfg)
    params["asset"] = TICKER
    st = init_paired_state(params=params, tick=tick)
    st["last_mark"] = float(mark)
    return st


def market_stressed(
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    i: int,
    pp: PauseParams,
) -> bool:
    b = cum_return(btc_c, i, pp.market_lb)
    e = cum_return(eth_c, i, pp.market_lb)
    return b <= pp.market_ret_thresh or e <= pp.market_ret_thresh


def ticker_stressed(
    near_c: Sequence[float],
    near_lr: Sequence[float],
    near_vol: Sequence[float],
    near_vol_med: Sequence[float],
    i: int,
    pp: PauseParams,
) -> bool:
    bar_ret = near_c[i] / near_c[i - 1] - 1.0 if i > 0 and near_c[i - 1] > 0 else 0.0
    cum = cum_return(near_c, i, pp.ticker_lb)
    vol_hi = (
        near_vol_med[i] > 0
        and near_vol[i] > pp.vol_ratio_pause * near_vol_med[i]
    )
    pierce = bar_ret <= pp.ticker_bar_thresh or cum <= pp.ticker_cum_thresh
    return pierce or vol_hi


def should_pause(
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    near_c: Sequence[float],
    near_lr: Sequence[float],
    near_vol: Sequence[float],
    near_vol_med: Sequence[float],
    i: int,
    pp: PauseParams,
) -> bool:
    m = market_stressed(btc_c, eth_c, i, pp)
    t = ticker_stressed(near_c, near_lr, near_vol, near_vol_med, i, pp)
    if pp.require_market_and_ticker:
        return m and t
    return m or t


def should_resume(
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    near_c: Sequence[float],
    near_lr: Sequence[float],
    near_vol: Sequence[float],
    near_vol_med: Sequence[float],
    i: int,
    pp: PauseParams,
    calm_streak: int,
) -> bool:
    if calm_streak < pp.resume_bars:
        return False
    b = cum_return(btc_c, i, pp.market_lb)
    e = cum_return(eth_c, i, pp.market_lb)
    n = cum_return(near_c, i, pp.ticker_lb)
    vol_ok = (
        near_vol_med[i] <= 0
        or near_vol[i] <= pp.resume_vol_ratio * near_vol_med[i]
    )
    market_ok = b > pp.resume_market_ret and e > pp.resume_market_ret
    ticker_ok = n > pp.resume_ticker_ret
    bar_ret = near_c[i] / near_c[i - 1] - 1.0 if i > 0 and near_c[i - 1] > 0 else 0.0
    no_pierce = bar_ret > pp.ticker_bar_thresh * 0.5
    return market_ok and ticker_ok and vol_ok and no_pierce


def precompute_vol_features(near_lr: Sequence[float], lb: int, med_lb: int) -> Tuple[List[float], List[float]]:
    vol = [0.0] * len(near_lr)
    vol_med = [0.0] * len(near_lr)
    for i in range(len(near_lr)):
        vol[i] = rolling_std(near_lr, i, lb)
        if i >= med_lb:
            vol_med[i] = median(vol[i - med_lb + 1 : i + 1])
    return vol, vol_med


def simulate_grid(
    times: Sequence[int],
    near_c: Sequence[float],
    btc_c: Sequence[float],
    eth_c: Sequence[float],
    near_lr: Sequence[float],
    near_vol: Sequence[float],
    near_vol_med: Sequence[float],
    pp: Optional[PauseParams] = None,
) -> Dict[str, Any]:
    mark0 = float(near_c[0])
    state = reinit_ladder_at_mark({}, mark0, tick=0)

    paused = False
    calm_streak = 0
    pause_events: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []

    max_inv_notional = 0.0
    max_dd = 0.0
    peak_equity = 0.0
    buy_fills = 0
    sell_fills = 0

    for i in range(1, len(near_c)):
        p_prev = float(near_c[i - 1])
        p_now = float(near_c[i])
        tick = i

        if pp is not None:
            if not paused:
                if should_pause(btc_c, eth_c, near_c, near_lr, near_vol, near_vol_med, i, pp):
                    paused = True
                    calm_streak = 0
                    cancel_open_orders(state, tick)
                    pause_events.append(
                        {
                            "type": "pause",
                            "i": i,
                            "time_ms": times[i],
                            "time": ms_to_iso(times[i]),
                            "price": p_now,
                            "btc_ret": cum_return(btc_c, i, pp.market_lb),
                            "eth_ret": cum_return(eth_c, i, pp.market_lb),
                            "near_ret": cum_return(near_c, i, pp.ticker_lb),
                            "near_vol": near_vol[i],
                            "near_vol_med": near_vol_med[i],
                        }
                    )
            else:
                if not should_pause(btc_c, eth_c, near_c, near_lr, near_vol, near_vol_med, i, pp):
                    calm_streak += 1
                else:
                    calm_streak = 0
                if should_resume(btc_c, eth_c, near_c, near_lr, near_vol, near_vol_med, i, pp, calm_streak):
                    paused = False
                    calm_streak = 0
                    inv_keep = float(state.get("inventory") or 0.0)
                    cost_keep = float(state.get("inventory_cost") or 0.0)
                    realized_keep = float(state.get("realized_pnl") or 0.0)
                    vol_keep = float(state.get("volume_usd") or 0.0)
                    state = reinit_ladder_at_mark(state, p_now, tick=tick)
                    state["inventory"] = inv_keep
                    state["inventory_cost"] = cost_keep
                    state["realized_pnl"] = realized_keep
                    state["volume_usd"] = vol_keep
                    pause_events.append(
                        {
                            "type": "resume",
                            "i": i,
                            "time_ms": times[i],
                            "time": ms_to_iso(times[i]),
                            "price": p_now,
                        }
                    )

        inv_before = float(state.get("inventory") or 0.0)

        if not paused:
            fills_before = sum(1 for o in state.get("orders") or [] if o.get("status") == "filled")
            step_mark_pair_sequential(
                state,
                p_prev=p_prev,
                p_now=p_now,
                grid_reset=GRID_RESET,
            )
            ensure_bracket_rungs_around_mark(state, mark=p_now)
            fills_after = sum(1 for o in state.get("orders") or [] if o.get("status") == "filled")
            new_fills = fills_after - fills_before
            if new_fills > 0:
                # crude: direction from inventory change
                inv_after = float(state.get("inventory") or 0.0)
                if inv_after > inv_before:
                    buy_fills += 1
                elif inv_after < inv_before:
                    sell_fills += 1

        inv = float(state.get("inventory") or 0.0)
        r, u, total = paired_totals(state, mark=p_now)
        inv_n = abs(inv) * p_now
        max_inv_notional = max(max_inv_notional, inv_n)
        peak_equity = max(peak_equity, total)
        dd = peak_equity - total
        max_dd = max(max_dd, dd)

        if i % 12 == 0 or i == len(near_c) - 1:
            series.append(
                {
                    "time_ms": times[i],
                    "time": ms_to_iso(times[i]),
                    "price": p_now,
                    "inventory": inv,
                    "inv_notional": inv_n,
                    "realized": r,
                    "unrealized": u,
                    "total_pnl": total,
                    "paused": paused,
                }
            )

    r, u, total = paired_totals(state, mark=float(near_c[-1]))
    return {
        "params": None if pp is None else pp.__dict__,
        "final_pnl": total,
        "realized_pnl": r,
        "unrealized_pnl": u,
        "max_drawdown": max_dd,
        "max_inv_notional": max_inv_notional,
        "final_inventory": float(state.get("inventory") or 0.0),
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "pause_count": sum(1 for e in pause_events if e["type"] == "pause"),
        "pause_events": pause_events,
        "series": series,
    }


def score_result(res: Dict[str, Any], baseline_dd: float) -> float:
    """Higher is better: PnL minus penalty for drawdown vs baseline."""
    dd_improve = baseline_dd - float(res["max_drawdown"])
    pnl = float(res["final_pnl"])
    return pnl + 2.0 * dd_improve - 0.1 * float(res["pause_count"])


def sweep(
    times: List[int],
    near_c: List[float],
    btc_c: List[float],
    eth_c: List[float],
    near_lr: List[float],
    near_vol: List[float],
    near_vol_med: List[float],
    baseline_dd: float,
) -> List[Dict[str, Any]]:
    candidates: List[PauseParams] = []

    for m_ret in (-0.01, -0.015, -0.02, -0.025):
        for t_cum in (-0.02, -0.03, -0.04, -0.05):
            for t_bar in (-0.008, -0.012, -0.016):
                for vol_r in (1.8, 2.0, 2.5):
                    for resume_b in (6, 12, 18):
                        for req_both in (True, False):
                            candidates.append(
                                PauseParams(
                                    name="",
                                    market_ret_thresh=m_ret,
                                    ticker_cum_thresh=t_cum,
                                    ticker_bar_thresh=t_bar,
                                    vol_ratio_pause=vol_r,
                                    resume_bars=resume_b,
                                    require_market_and_ticker=req_both,
                                )
                            )

    results: List[Dict[str, Any]] = []
    for pp in candidates:
        res = simulate_grid(
            times, near_c, btc_c, eth_c, near_lr, near_vol, near_vol_med, pp=pp
        )
        res["score"] = score_result(res, baseline_dd)
        res["name"] = (
            f"m{pp.market_ret_thresh:.1%}_t{pp.ticker_cum_thresh:.1%}_b{pp.ticker_bar_thresh:.1%}_"
            f"v{pp.vol_ratio_pause}x_rb{pp.resume_bars}_"
            f"{'both' if pp.require_market_and_ticker else 'either'}"
        )
        pp_dict = dict(pp.__dict__)
        pp_dict["name"] = res["name"]
        res["params"] = pp_dict
        results.append(res)

    results.sort(key=lambda r: float(r["score"]), reverse=True)
    return results


def dump_window_stats(
    times: List[int],
    near_c: List[float],
    btc_c: List[float],
    eth_c: List[float],
) -> Dict[str, Any]:
    """Quantiles during the Jun 4–6 dump-ish window for intuition."""
    # index range where BTC dropped sharply: find min BTC in period
    dump_idxs = []
    for i in range(len(btc_c)):
        if cum_return(btc_c, i, 12) <= -0.02 or cum_return(near_c, i, 6) <= -0.03:
            dump_idxs.append(i)
    samples = {
        "btc_ret_1h": [cum_return(btc_c, i, 12) for i in dump_idxs],
        "eth_ret_1h": [cum_return(eth_c, i, 12) for i in dump_idxs],
        "near_ret_30m": [cum_return(near_c, i, 6) for i in dump_idxs],
        "near_bar": [
            near_c[i] / near_c[i - 1] - 1.0
            for i in dump_idxs
            if i > 0 and near_c[i - 1] > 0
        ],
    }
    out: Dict[str, Any] = {"stress_bar_count": len(dump_idxs)}
    for k, vals in samples.items():
        if not vals:
            out[k] = {}
            continue
        s = sorted(vals)
        out[k] = {
            "min": s[0],
            "p10": s[max(0, len(s) // 10)],
            "median": s[len(s) // 2],
            "p90": s[min(len(s) - 1, 9 * len(s) // 10)],
        }
    return out


def write_html() -> None:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEAR Grid Vol Pause Study</title>
<script src="NEAR_vol_pause_sim.data.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root { --bg:#0c1014; --panel:#151b22; --text:#d8dee6; --dim:#8b949e; --accent:#58a6ff; --bad:#f85149; --good:#3fb950; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); padding:16px; }
  h1 { font-size:1.2rem; margin:0 0 8px; }
  .sub { color:var(--dim); font-size:.85rem; margin-bottom:16px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .card { background:var(--panel); border-radius:8px; padding:12px; }
  .card h2 { font-size:.95rem; margin:0 0 8px; color:var(--accent); }
  .stat { display:flex; justify-content:space-between; font-size:.85rem; margin:4px 0; }
  #chart { height:360px; }
  #pnl-chart { height:220px; }
  table { width:100%; border-collapse:collapse; font-size:.75rem; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; }
  .footnote { color:var(--dim); font-size:.75rem; margin-top:12px; }
</style>
</head>
<body>
<h1>NEAR grid — volatility pause study (1–7 Jun 2026)</h1>
<p class="sub">Baseline paired grid vs BTC/ETH + NEAR stress pause. Double-click this file.</p>
<div class="grid">
  <div class="card"><h2>Baseline (no pause)</h2><div id="baseline-stats"></div></div>
  <div class="card"><h2>Recommended pause</h2><div id="best-stats"></div></div>
</div>
<div class="card" style="margin-top:12px"><h2>NEAR price + pause windows</h2><div id="chart"></div></div>
<div class="card" style="margin-top:12px"><h2>Equity curve</h2><div id="pnl-chart"></div></div>
<div class="card" style="margin-top:12px"><h2>Top parameter sets</h2><div id="sweep-table"></div></div>
<p class="footnote">Data: binancefetch/gridbot_study/gridbot_study_01-07JUN.sqlite</p>
<script>
const D = window.__NEAR_VOL_PAUSE_DATA__;
function fmtUsd(x){ return (x>=0?'+':'')+x.toFixed(2); }
function statCard(el, s, label){
  el.innerHTML = `
    <div class="stat"><span>Final PnL</span><span style="color:${s.final_pnl>=0?'var(--good)':'var(--bad)'}">${fmtUsd(s.final_pnl)}</span></div>
    <div class="stat"><span>Max drawdown</span><span>${fmtUsd(-s.max_drawdown)}</span></div>
    <div class="stat"><span>Max inv notional</span><span>$${s.max_inv_notional.toFixed(0)}</span></div>
    <div class="stat"><span>Final inventory</span><span>${s.final_inventory.toFixed(2)} NEAR</span></div>
    <div class="stat"><span>Buy / sell fills</span><span>${s.buy_fills} / ${s.sell_fills}</span></div>
    <div class="stat"><span>Pause events</span><span>${s.pause_count}</span></div>
    <div class="stat"><span>Label</span><span>${label}</span></div>`;
}
statCard(document.getElementById('baseline-stats'), D.baseline, 'no pause');
statCard(document.getElementById('best-stats'), D.recommended, D.recommended.params.name);

const priceData = D.near_prices.map(p=>({time:p.time_ms/1000, value:p.close}));
const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout:{background:{color:'#151b22'}, textColor:'#8b949e'},
  grid:{vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'}},
  rightPriceScale:{borderColor:'#30363d'}, timeScale:{borderColor:'#30363d'},
});
const line = chart.addLineSeries({ color:'#58a6ff', lineWidth:2 });
line.setData(priceData);
D.recommended.pause_events.forEach(e=>{
  if(e.type!=='pause') return;
  line.setMarkers([...(line.markers||[]), {
    time: e.time_ms/1000, position:'belowBar', color:'#f85149', shape:'arrowDown', text:'pause'
  }]);
});

const pnlChart = LightweightCharts.createChart(document.getElementById('pnl-chart'), {
  layout:{background:{color:'#151b22'}, textColor:'#8b949e'},
  grid:{vertLines:{color:'#21262d'}, horzLines:{color:'#21262d'}},
  rightPriceScale:{borderColor:'#30363d'}, timeScale:{borderColor:'#30363d'},
});
const bS = pnlChart.addLineSeries({ color:'#8b949e', lineWidth:1, title:'baseline' });
const rS = pnlChart.addLineSeries({ color:'#3fb950', lineWidth:2, title:'paused' });
bS.setData(D.baseline.series.map(p=>({time:p.time_ms/1000, value:p.total_pnl})));
rS.setData(D.recommended.series.map(p=>({time:p.time_ms/1000, value:p.total_pnl})));

const rows = D.sweep_top.slice(0, 12);
document.getElementById('sweep-table').innerHTML = '<table><tr><th>Rank</th><th>Params</th><th>PnL</th><th>MaxDD</th><th>Pauses</th></tr>' +
  rows.map((r,i)=>`<tr><td>${i+1}</td><td>${r.params.name}</td><td>${fmtUsd(r.final_pnl)}</td><td>${fmtUsd(-r.max_drawdown)}</td><td>${r.pause_count}</td></tr>`).join('') + '</table>';
</script>
</body>
</html>"""
    OUT_HTML.write_text(html, encoding="utf-8")


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        times, near_c = load_closes(conn, "NEAR")
        _, btc_c = load_closes(conn, "BTC")
        _, eth_c = load_closes(conn, "ETH")
    finally:
        conn.close()

    near_lr = log_returns(near_c)
    near_vol, near_vol_med = precompute_vol_features(near_lr, lb=36, med_lb=288)

    baseline = simulate_grid(
        times, near_c, btc_c, eth_c, near_lr, near_vol, near_vol_med, pp=None
    )
    baseline_dd = float(baseline["max_drawdown"])

    sweep_results = sweep(
        times, near_c, btc_c, eth_c, near_lr, near_vol, near_vol_med, baseline_dd
    )
    best = sweep_results[0]

    # Recommended = top sweep score (aggressive OR trigger); production variant documented in payload.
    recommended_pp = PauseParams(
        name="recommended",
        market_ret_thresh=float(best["params"]["market_ret_thresh"]),
        ticker_cum_thresh=float(best["params"]["ticker_cum_thresh"]),
        ticker_bar_thresh=float(best["params"]["ticker_bar_thresh"]),
        vol_ratio_pause=float(best["params"]["vol_ratio_pause"]),
        resume_bars=int(best["params"]["resume_bars"]),
        require_market_and_ticker=bool(best["params"]["require_market_and_ticker"]),
    )
    recommended = simulate_grid(
        times, near_c, btc_c, eth_c, near_lr, near_vol, near_vol_med, pp=recommended_pp
    )
    recommended["params"] = {**recommended_pp.__dict__, "name": "recommended"}

    prod_pp = PauseParams(
        name="production_conservative",
        market_ret_thresh=-0.02,
        ticker_cum_thresh=-0.04,
        ticker_bar_thresh=-0.015,
        vol_ratio_pause=2.0,
        resume_bars=12,
        require_market_and_ticker=True,
    )
    production = simulate_grid(
        times, near_c, btc_c, eth_c, near_lr, near_vol, near_vol_med, pp=prod_pp
    )
    production["params"] = {**prod_pp.__dict__}

    stress_stats = dump_window_stats(times, near_c, btc_c, eth_c)

    near_prices = [
        {"time_ms": times[i], "time": ms_to_iso(times[i]), "close": near_c[i]}
        for i in range(0, len(times), 3)
    ]

    payload = {
        "ticker": TICKER,
        "grid": {
            "band_pct": BAND_PCT,
            "grid_num": GRID_NUM,
            "investment_usd": INVESTMENT_USD,
            "leverage": LEVERAGE,
        },
        "stress_stats": stress_stats,
        "baseline": {k: baseline[k] for k in baseline if k != "pause_events"},
        "recommended": recommended,
        "production_conservative": {k: production[k] for k in production if k not in ("pause_events", "series")},
        "best_sweep": {k: best[k] for k in best if k not in ("pause_events", "series")},
        "sweep_top": [
            {k: r[k] for k in ("name", "final_pnl", "max_drawdown", "pause_count", "score", "params")}
            for r in sweep_results[:20]
        ],
        "near_prices": near_prices,
        "suggested_env_recommended": {
            "GRID_VOL_PAUSE_ENABLED": "1",
            "GRID_VOL_PAUSE_MARKET_LB": "12",
            "GRID_VOL_PAUSE_MARKET_RET": "-0.01",
            "GRID_VOL_PAUSE_TICKER_LB": "6",
            "GRID_VOL_PAUSE_TICKER_CUM_RET": "-0.03",
            "GRID_VOL_PAUSE_TICKER_BAR_RET": "-0.012",
            "GRID_VOL_PAUSE_VOL_LB": "36",
            "GRID_VOL_PAUSE_VOL_RATIO": "1.8",
            "GRID_VOL_PAUSE_RESUME_BARS": "18",
            "GRID_VOL_PAUSE_RESUME_MARKET_RET": "-0.005",
            "GRID_VOL_PAUSE_RESUME_TICKER_RET": "-0.01",
            "GRID_VOL_PAUSE_RESUME_VOL_RATIO": "1.3",
            "GRID_VOL_PAUSE_REQUIRE_BOTH": "0",
        },
        "suggested_env_production": {
            "GRID_VOL_PAUSE_ENABLED": "1",
            "GRID_VOL_PAUSE_MARKET_RET": "-0.02",
            "GRID_VOL_PAUSE_TICKER_CUM_RET": "-0.04",
            "GRID_VOL_PAUSE_TICKER_BAR_RET": "-0.015",
            "GRID_VOL_PAUSE_VOL_RATIO": "2.0",
            "GRID_VOL_PAUSE_RESUME_BARS": "12",
            "GRID_VOL_PAUSE_REQUIRE_BOTH": "1",
        },
    }

    OUT_DATA.write_text(
        "window.__NEAR_VOL_PAUSE_DATA__ = "
        + json.dumps(payload, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    write_html()

    print("=== NEAR grid vol-pause study (1-7 Jun 2026) ===")
    print(f"Grid: {GRID_NUM} rungs, ±{BAND_PCT}% band, ${INVESTMENT_USD} × {LEVERAGE}x")
    print()
    print("Baseline (no pause):")
    print(f"  final PnL: ${baseline['final_pnl']:.2f}  max DD: ${baseline['max_drawdown']:.2f}")
    print(f"  max inv notional: ${baseline['max_inv_notional']:.0f}  inventory: {baseline['final_inventory']:.2f} NEAR")
    print(f"  buy/sell fills: {baseline['buy_fills']}/{baseline['sell_fills']}")
    print()
    print("Recommended pause (best sweep):")
    print(f"  final PnL: ${recommended['final_pnl']:.2f}  max DD: ${recommended['max_drawdown']:.2f}")
    print(f"  max inv notional: ${recommended['max_inv_notional']:.0f}  pauses: {recommended['pause_count']}")
    print()
    print("Production conservative (BTC+NEAR both stressed, fewer pauses):")
    print(f"  final PnL: ${production['final_pnl']:.2f}  max DD: ${production['max_drawdown']:.2f}  pauses: {production['pause_count']}")
    for ev in recommended["pause_events"]:
        print(f"    {ev['type']} @ {ev['time']} price={ev.get('price', 0):.4f}")
    print()
    print("Stress-window quantiles (bars with BTC 1h ≤-2% or NEAR 30m ≤-3%):")
    for k, v in stress_stats.items():
        print(f"  {k}: {v}")
    print()
    print("Top sweep result:", best.get("params", {}).get("name", "?"))
    print(f"  PnL ${best['final_pnl']:.2f}  DD ${best['max_drawdown']:.2f}  score {best['score']:.2f}")
    print()
    print(f"Wrote {OUT_DATA.name} + {OUT_HTML.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
