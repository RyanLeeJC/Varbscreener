#!/usr/bin/env python3
"""BZ/CL spread mean-reversion backtest → BZCL_sim.data.js for BZCL_sim.html.

Regenerates embedded data from sibling JSON kline files:
  BZUSDT_5m_last30d.json, CLUSDT_5m_last30d.json

Usage (from repo root):
  python3 binancefetch/BZCL_arb/bzcl_backtest.py
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT = {
    "lookback": 288,  # 1 day @ 5m
    "entry_z": 2.5,
    "exit_z": 0.0,
    "notional_per_leg": 10_000.0,
}


def load_funding(path: Path) -> list[dict]:
    with open(path) as f:
        payload = json.load(f)
    return [
        {"t": r["funding_time_ms"], "rate": r["funding_rate"]}
        for r in payload["records"]
    ]


def load_bars(path: Path) -> dict[int, dict]:
    with open(path) as f:
        payload = json.load(f)
    return {b["open_time_ms"]: b for b in payload["bars"]}


def merge(bz_map: dict, cl_map: dict) -> list[dict]:
    times = sorted(set(bz_map) & set(cl_map))
    rows = []
    for t in times:
        bz = bz_map[t]
        cl = cl_map[t]
        ratio = bz["close"] / cl["close"]
        rows.append(
            {
                "t": t,
                "iso": bz["time"],
                "bz": bz["close"],
                "cl": cl["close"],
                "ratio": round(ratio, 6),
                "log_spread": round(math.log(ratio), 8),
            }
        )
    return rows


def zscore_at(log_spreads: list[float], i: int, lookback: int) -> float:
    window = log_spreads[i - lookback : i]
    m = statistics.mean(window)
    s = statistics.stdev(window)
    if s <= 0:
        return 0.0
    return (log_spreads[i] - m) / s


def run_backtest(
    rows: list[dict],
    *,
    lookback: int = DEFAULT["lookback"],
    entry_z: float = DEFAULT["entry_z"],
    exit_z: float = DEFAULT["exit_z"],
    notional: float = DEFAULT["notional_per_leg"],
) -> dict:
    log_spreads = [r["log_spread"] for r in rows]
    n = len(rows)
    position = 0  # +1 long BZ / short CL, -1 short BZ / long CL
    bz_entry = cl_entry = 0.0
    rpnl = 0.0
    peak = 0.0
    max_dd = 0.0
    trades: list[dict] = []
    events: list[dict] = []
    equity: list[dict] = []
    z_series: list[dict] = []

    for i in range(n):
        z = zscore_at(log_spreads, i, lookback) if i >= lookback else None
        if z is not None:
            z_series.append({"t": rows[i]["t"], "z": round(z, 4)})

        if i < lookback:
            continue

        bc, cc = rows[i]["bz"], rows[i]["cl"]
        upnl = 0.0
        if position == 1:
            upnl = notional * (bc / bz_entry - 1) - notional * (cc / cl_entry - 1)
        elif position == -1:
            upnl = -notional * (bc / bz_entry - 1) + notional * (cc / cl_entry - 1)

        eq = rpnl + upnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        equity.append({"t": rows[i]["t"], "eq": round(eq, 2), "pos": position})

        if position == 0:
            if z >= entry_z:
                position = -1
                bz_entry, cl_entry = bc, cc
                events.append(
                    {
                        "t": rows[i]["t"],
                        "iso": rows[i]["iso"],
                        "type": "enter",
                        "side": "short_bz_long_cl",
                        "z": round(z, 3),
                        "bz": bc,
                        "cl": cc,
                    }
                )
            elif z <= -entry_z:
                position = 1
                bz_entry, cl_entry = bc, cc
                events.append(
                    {
                        "t": rows[i]["t"],
                        "iso": rows[i]["iso"],
                        "type": "enter",
                        "side": "long_bz_short_cl",
                        "z": round(z, 3),
                        "bz": bc,
                        "cl": cc,
                    }
                )
        else:
            exit_sig = (position == 1 and z >= -exit_z) or (position == -1 and z <= exit_z)
            if exit_sig:
                if position == 1:
                    trade_pnl = notional * (bc / bz_entry - 1) - notional * (cc / cl_entry - 1)
                else:
                    trade_pnl = -notional * (bc / bz_entry - 1) + notional * (cc / cl_entry - 1)
                rpnl += trade_pnl
                trades.append(
                    {
                        "entry_t": events[-1]["t"] if events else rows[i]["t"],
                        "exit_t": rows[i]["t"],
                        "side": "long_bz_short_cl" if position == 1 else "short_bz_long_cl",
                        "pnl": round(trade_pnl, 2),
                        "z_entry": events[-1]["z"] if events else 0,
                        "z_exit": round(z, 3),
                    }
                )
                events.append(
                    {
                        "t": rows[i]["t"],
                        "iso": rows[i]["iso"],
                        "type": "exit",
                        "pnl": round(trade_pnl, 2),
                        "z": round(z, 3),
                    }
                )
                position = 0

    if position != 0:
        bc, cc = rows[-1]["bz"], rows[-1]["cl"]
        if position == 1:
            trade_pnl = notional * (bc / bz_entry - 1) - notional * (cc / cl_entry - 1)
        else:
            trade_pnl = -notional * (bc / bz_entry - 1) + notional * (cc / cl_entry - 1)
        rpnl += trade_pnl
        trades.append(
            {
                "entry_t": events[-1]["t"] if events else rows[-1]["t"],
                "exit_t": rows[-1]["t"],
                "side": "long_bz_short_cl" if position == 1 else "short_bz_long_cl",
                "pnl": round(trade_pnl, 2),
                "z_entry": events[-1]["z"] if events else 0,
                "z_exit": round(zscore_at(log_spreads, n - 1, lookback), 3),
                "forced": True,
            }
        )

    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "params": {
            "lookback": lookback,
            "entry_z": entry_z,
            "exit_z": exit_z,
            "notional_per_leg": notional,
        },
        "summary": {
            "total_pnl": round(rpnl, 2),
            "trades": len(trades),
            "win_rate": round(wins / len(trades), 4) if trades else 0,
            "max_drawdown": round(max_dd, 2),
            "avg_trade": round(statistics.mean(t["pnl"] for t in trades), 2) if trades else 0,
        },
        "z_series": z_series,
        "equity": equity,
        "trades": trades,
        "events": events,
    }


def main() -> None:
    bz_path = ROOT / "BZUSDT_5m_last30d.json"
    cl_path = ROOT / "CLUSDT_5m_last30d.json"
    bz_map = load_bars(bz_path)
    cl_map = load_bars(cl_path)
    rows = merge(bz_map, cl_map)

    bt = run_backtest(rows)
    payload = {
        "meta": {
            "bz_symbol": "BZUSDT",
            "cl_symbol": "CLUSDT",
            "interval": "5m",
            "bar_count": len(rows),
            "start": rows[0]["iso"],
            "end": rows[-1]["iso"],
            "strategy": "log(BZ/CL) z-score mean reversion",
        },
        "defaults": DEFAULT,
        "funding": {
            "interval_hours": 8,
            "bz": load_funding(ROOT / "BZUSDT_funding_last30d.json"),
            "cl": load_funding(ROOT / "CLUSDT_funding_last30d.json"),
        },
        "bars": rows,
        "backtest": bt,
    }

    out = ROOT / "BZCL_sim.data.js"
    body = json.dumps(payload, separators=(",", ":"))
    out.write_text(f"// Generated by bzcl_backtest.py — open BZCL_sim.html directly\nwindow.__BZCL_ARB_DATA__ = {body};\n")
    s = bt["summary"]
    print(f"Wrote {out} ({len(body):,} bytes)")
    print(
        f"Backtest: PnL ${s['total_pnl']:,.2f} · {s['trades']} trades · "
        f"win {s['win_rate']*100:.1f}% · max DD ${s['max_drawdown']:,.2f}"
    )


if __name__ == "__main__":
    main()
