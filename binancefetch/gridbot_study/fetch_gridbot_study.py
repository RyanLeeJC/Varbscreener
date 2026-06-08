#!/usr/bin/env python3
"""Fetch Binance USDT-M 5m klines into gridbot_study SQLite.

Default window (SGT): 2026-06-01 00:00 → latest closed 5m bar.
Re-runs append from the last stored bar per symbol (INSERT OR REPLACE).

Usage (from repo root):
  python3 binancefetch/gridbot_study/fetch_gridbot_study.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from binancefetch.binancefetch import (  # noqa: E402
    BINANCE_FUTURES_KLINES,
    fetch_klines_window,
    kline_to_bar,
)
from strategy.gridstrat import GRID_TRADING_TICKERS  # noqa: E402

STUDY_DIR = Path(__file__).resolve().parent
DB_PATH = STUDY_DIR / "gridbot_study_01-07JUN.sqlite"

SGT = timezone(timedelta(hours=8))
START_SGT = datetime(2026, 6, 1, 0, 0, 0, tzinfo=SGT)
START_MS = int(START_SGT.timestamp() * 1000)

INTERVAL = "5m"
INTERVAL_MS = 5 * 60 * 1000
DATA_HOST = BINANCE_FUTURES_KLINES

GRID_TICKERS = list(GRID_TRADING_TICKERS.keys())
EXTRA_TICKERS = ["BTC", "ETH"]
ALL_TICKERS = GRID_TICKERS + EXTRA_TICKERS


def last_closed_5m_open_sgt(now: datetime | None = None) -> datetime:
    """Open time (SGT) of the most recently closed 5m candle."""
    now = now or datetime.now(SGT)
    minute_floor = (now.minute // 5) * 5
    current_open = now.replace(minute=minute_floor, second=0, microsecond=0)
    return current_open - timedelta(minutes=5)


def end_ms(now: datetime | None = None) -> int:
    return int(last_closed_5m_open_sgt(now).timestamp() * 1000)


def init_db(conn: sqlite3.Connection, *, end_sgt: datetime, end_ms_val: int) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS klines_5m (
            symbol TEXT NOT NULL,
            underlying TEXT NOT NULL,
            open_time_ms INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            close_time_ms INTEGER NOT NULL,
            quote_volume REAL NOT NULL,
            trades INTEGER NOT NULL,
            PRIMARY KEY (symbol, open_time_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_klines_5m_underlying_time
            ON klines_5m (underlying, open_time_ms);
        """
    )
    meta = {
        "interval": INTERVAL,
        "data_host": "futures",
        "start_sgt": START_SGT.isoformat(),
        "end_sgt": end_sgt.isoformat(),
        "start_utc": START_SGT.astimezone(timezone.utc).isoformat(),
        "end_utc": end_sgt.astimezone(timezone.utc).isoformat(),
        "start_ms": str(START_MS),
        "end_ms": str(end_ms_val),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "tickers": ",".join(ALL_TICKERS),
    }
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        list(meta.items()),
    )
    conn.commit()


def store_bars(conn: sqlite3.Connection, *, underlying: str, symbol: str, bars: list[dict]) -> int:
    rows = [
        (
            symbol,
            underlying,
            int(b["open_time_ms"]),
            float(b["open"]),
            float(b["high"]),
            float(b["low"]),
            float(b["close"]),
            float(b["volume"]),
            int(b["close_time_ms"]),
            float(b["quote_volume"]),
            int(b["trades"]),
        )
        for b in bars
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO klines_5m (
            symbol, underlying, open_time_ms, open, high, low, close,
            volume, close_time_ms, quote_volume, trades
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def last_stored_ms(conn: sqlite3.Connection, underlying: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(open_time_ms) FROM klines_5m WHERE underlying = ?",
        (underlying,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def fetch_symbol(
    conn: sqlite3.Connection,
    underlying: str,
    *,
    end_ms_val: int,
    sleep_s: float = 0.1,
) -> int:
    symbol = f"{underlying}USDT"
    last_ms = last_stored_ms(conn, underlying)
    start_ms = (last_ms + INTERVAL_MS) if last_ms is not None else START_MS
    if start_ms > end_ms_val:
        print(f"{underlying}: up to date (last {last_ms})")
        return 0
    raw = fetch_klines_window(
        symbol,
        INTERVAL,
        start_ms,
        end_ms_val,
        base_url=DATA_HOST,
        sleep_s=sleep_s,
    )
    bars = [kline_to_bar(k) for k in raw]
    bars = [b for b in bars if start_ms <= int(b["open_time_ms"]) <= end_ms_val]
    n = store_bars(conn, underlying=underlying, symbol=symbol, bars=bars)
    if bars:
        print(f"{underlying}: +{n} bars ({bars[0]['time']} → {bars[-1]['time']})")
    else:
        print(f"{underlying}: 0 new bars")
    return n


def main() -> int:
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    end_sgt = last_closed_5m_open_sgt()
    end_ms_val = end_ms()
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn, end_sgt=end_sgt, end_ms_val=end_ms_val)
        print(f"Window end (SGT): {end_sgt.isoformat()}")
        total = 0
        for i, t in enumerate(ALL_TICKERS):
            total += fetch_symbol(conn, t, end_ms_val=end_ms_val)
            if i < len(ALL_TICKERS) - 1:
                time.sleep(0.15)
        print(f"Wrote {total} new rows → {DB_PATH}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
