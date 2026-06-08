#!/usr/bin/env python3
"""48h band hyperparam for 32-ticker cohort — vol-pause only (production logic).

Bands: 1.5%, 2%, 2.5%, 3%, 3.5%. Picks best band per ticker, then top 15 by PnL.

Usage (repo root):
  python3 binancefetch/gridbot_study/gridbot_48h_hyperparam.py
  python3 binancefetch/gridbot_study/gridbot_48h_hyperparam.py --top 15 --json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

STUDY_DIR = Path(__file__).resolve().parent
ROOT = STUDY_DIR.parents[1]
for p in (str(ROOT), str(STUDY_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from grid_vol_pause_backtest import (  # noqa: E402
    load_series,
    log_returns,
    precompute_vol_ratio,
    simulate,
)
DB_PATH = STUDY_DIR / "32_tickers_48h5m.sqlite"
OUT_JSON = STUDY_DIR / "gridbot_48h_hyperparam.json"
OUT_MD = STUDY_DIR / "gridbot_48h_study.md"

BAND_CANDIDATES = [1.5, 2.0, 2.5, 3.0, 3.5]
WARMUP_BARS = 110  # max(MARKET_LB, TICKER_LB, VOL_LB+VOL_MED)+2 from backtest


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def list_underlyings(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT underlying FROM klines_5m ORDER BY underlying"
    ).fetchall()
    return [str(r[0]) for r in rows]


def run_hyperparam(db_path: Path = DB_PATH, *, top_n: int = 15) -> Dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Missing DB: {db_path} — run fetch_32tickers_48h5m.py first")

    conn = sqlite3.connect(db_path)
    try:
        btc_map = load_series(conn, "BTC")
        eth_map = load_series(conn, "ETH")
        if not btc_map or not eth_map:
            raise ValueError("BTC/ETH gate data required in DB")

        all_times = sorted(btc_map.keys())
        window_times = all_times
        if len(window_times) < WARMUP_BARS + 10:
            raise ValueError(f"Insufficient bars: {len(window_times)} (need ~{WARMUP_BARS + 10})")

        sim_start_i = min(WARMUP_BARS, len(window_times) - 2)
        btc_c = [btc_map[t] for t in window_times]
        eth_c = [eth_map[t] for t in window_times]

        study_tickers = [
            t for t in list_underlyings(conn) if t not in ("BTC", "ETH")
        ]

        per_ticker: List[Dict[str, Any]] = []
        for ticker in sorted(study_tickers):
            tmap = load_series(conn, ticker)
            t_c = [tmap.get(t, float("nan")) for t in window_times]
            if any(math.isnan(x) for x in t_c):
                continue

            vol_ratio = precompute_vol_ratio(log_returns(t_c))
            best_band = None
            best_pnl = -1e18
            best_dd = 0.0
            best_pauses = 0
            band_rows: List[Dict[str, Any]] = []

            for band in BAND_CANDIDATES:
                res = simulate(
                    ticker,
                    band,
                    t_c,
                    btc_c,
                    eth_c,
                    vol_ratio,
                    sim_start_i,
                    use_vol_pause=True,
                )
                band_rows.append(
                    {
                        "band_pct": band,
                        "pnl": round(res["pnl"], 2),
                        "dd": round(res["dd"], 2),
                        "pauses": int(res["pauses"]),
                        "price_chg_pct": round(res["price_chg_pct"], 2),
                    }
                )
                if res["pnl"] > best_pnl:
                    best_pnl = res["pnl"]
                    best_band = band
                    best_dd = res["dd"]
                    best_pauses = int(res["pauses"])

            per_ticker.append(
                {
                    "ticker": ticker,
                    "best_band_pct": best_band,
                    "best_pnl": round(best_pnl, 2),
                    "best_dd": round(best_dd, 2),
                    "pauses": best_pauses,
                    "bands": band_rows,
                }
            )

        per_ticker.sort(key=lambda x: x["best_pnl"], reverse=True)
        picks = per_ticker[:top_n]
        total_pnl = sum(p["best_pnl"] for p in picks)

        start_ms = int(window_times[sim_start_i])
        end_ms = int(window_times[-1])
        return {
            "db": str(db_path),
            "bands_tested": BAND_CANDIDATES,
            "sim_bars": len(window_times) - sim_start_i - 1,
            "warmup_bars": sim_start_i,
            "window_start_ms": start_ms,
            "window_end_ms": end_ms,
            "tickers_tested": len(per_ticker),
            "top_n": top_n,
            "top_picks": picks,
            "top_total_pnl": round(total_pnl, 2),
            "all_results": per_ticker,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "meta": {
                "start_sgt": meta_get(conn, "start_sgt"),
                "end_sgt": meta_get(conn, "end_sgt"),
                "tickers_skipped": meta_get(conn, "tickers_skipped"),
            },
        }
    finally:
        conn.close()


def write_markdown(payload: Dict[str, Any]) -> None:
    meta = payload.get("meta") or {}
    lines = [
        "# Gridbot 48h study — lighter defi/l1/l2 top-half cohort",
        "",
        f"Window: {meta.get('start_sgt', '?')} → {meta.get('end_sgt', '?')} SGT",
        f"Data: `{DB_PATH.name}` (Binance USDT-M 5m, last 48h)",
        f"Logic: production vol-pause (`grid_vol_pause_backtest.py`), bands {BAND_CANDIDATES}",
        "",
        f"**Top {payload['top_n']} by best-band PnL** (total ${payload['top_total_pnl']:.2f})",
        "",
        "| # | Ticker | Band% | PnL | DD | Pauses |",
        "|---|--------|-------|-----|-----|--------|",
    ]
    for i, p in enumerate(payload["top_picks"], 1):
        lines.append(
            f"| {i} | {p['ticker']} | {p['best_band_pct']} | "
            f"${p['best_pnl']:.2f} | ${p['best_dd']:.2f} | {p['pauses']} |"
        )
    lines.extend([
        "",
        "## Live `GRID_TRADING_TICKERS` snippet",
        "",
        "```python",
        "GRID_TRADING_TICKERS = {",
    ])
    for p in payload["top_picks"]:
        lines.append(f'    "{p["ticker"]}": {p["best_band_pct"]},')
    lines.extend(["}", "```", ""])
    if meta.get("tickers_skipped"):
        lines.append(f"Skipped (no Binance perp): {meta['tickers_skipped']}")
        lines.append("")
    lines.extend([
        "## Regenerate",
        "",
        "```bash",
        "python3 binancefetch/gridbot_study/fetch_32tickers_48h5m.py",
        "python3 binancefetch/gridbot_study/gridbot_48h_hyperparam.py",
        "```",
        "",
    ])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def print_report(payload: Dict[str, Any]) -> None:
    meta = payload.get("meta") or {}
    print(f"Window: {meta.get('start_sgt', '?')} → {meta.get('end_sgt', '?')} SGT")
    print(f"Tickers tested: {payload['tickers_tested']} | sim bars: {payload['sim_bars']}")
    print(f"Bands: {BAND_CANDIDATES}")
    print()
    print(f"{'#':>2} {'Ticker':<8} {'Band':>5} {'PnL':>9} {'DD':>9} {'Pauses':>6}")
    print("-" * 44)
    for i, p in enumerate(payload["top_picks"], 1):
        print(
            f"{i:>2} {p['ticker']:<8} {p['best_band_pct']:>5.1f} "
            f"{p['best_pnl']:>9.2f} {p['best_dd']:>9.2f} {p['pauses']:>6}"
        )
    print("-" * 44)
    print(f"{'':>2} {'TOTAL':<8} {'':>5} {payload['top_total_pnl']:>9.2f}")
    if meta.get("tickers_skipped"):
        print(f"\nSkipped: {meta['tickers_skipped']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="48h band hyperparam (vol-pause)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    payload = run_hyperparam(args.db, top_n=args.top)
    write_markdown(payload)

    if not args.quiet:
        print_report(payload)

    if args.json:
        OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"\nWrote {OUT_JSON.name}")

    if not args.quiet:
        print(f"Wrote {OUT_MD.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
