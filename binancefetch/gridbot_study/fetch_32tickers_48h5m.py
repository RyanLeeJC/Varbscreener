#!/usr/bin/env python3
"""Fetch last 48h Binance USDT-M 5m klines for the 32-ticker cohort (+ BTC/ETH gate).

Usage (repo root):
  python3 binancefetch/gridbot_study/fetch_32tickers_48h5m.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from binancefetch.binancefetch import (  # noqa: E402
    BINANCE_FUTURES_KLINES,
    fetch_klines_window,
    kline_to_bar,
)

STUDY_DIR = Path(__file__).resolve().parent
DB_PATH = STUDY_DIR / "32_tickers_48h5m.sqlite"

SGT = timezone(timedelta(hours=8))
INTERVAL = "5m"
INTERVAL_MS = 5 * 60 * 1000
HOURS = 48
DATA_HOST = BINANCE_FUTURES_KLINES

# Top-half lighter defi/l1/l2 cohort (vol-ranked, blacklist+BTC/ETH scrubbed).
STUDY_TICKERS = [
    "HYPE", "SOL", "NEAR", "BNB", "ADA", "VVV", "LIGHTER", "JTO", "ENA", "AVAX",
    "ONDO", "SUI", "LINK", "MON", "ICP", "LTC", "XLM", "ASTER", "TRX", "BCH",
    "AAVE", "MEGA", "CRV", "AERO", "OP", "PENDLE", "VIRTUAL", "DOT", "SYRUP",
    "TIA", "FIL", "HBAR",
]
GATE_TICKERS = ["BTC", "ETH"]
ALL_TICKERS = STUDY_TICKERS + GATE_TICKERS


def last_closed_5m_open_sgt(now: datetime | None = None) -> datetime:
    now = now or datetime.now(SGT)
    minute_floor = (now.minute // 5) * 5
    current_open = now.replace(minute=minute_floor, second=0, microsecond=0)
    return current_open - timedelta(minutes=5)


def init_db(conn: sqlite3.Connection, *, start_sgt: datetime, end_sgt: datetime, start_ms: int, end_ms_val: int) -> None:
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
        "hours": str(HOURS),
        "start_sgt": start_sgt.isoformat(),
        "end_sgt": end_sgt.isoformat(),
        "start_utc": start_sgt.astimezone(timezone.utc).isoformat(),
        "end_utc": end_sgt.astimezone(timezone.utc).isoformat(),
        "start_ms": str(start_ms),
        "end_ms": str(end_ms_val),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "tickers_requested": ",".join(STUDY_TICKERS),
        "gate_tickers": ",".join(GATE_TICKERS),
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


def fetch_symbol(conn: sqlite3.Connection, underlying: str, *, start_ms: int, end_ms_val: int) -> int:
    symbol = f"{underlying}USDT"
    try:
        raw = fetch_klines_window(
            symbol,
            INTERVAL,
            start_ms,
            end_ms_val,
            base_url=DATA_HOST,
            sleep_s=0.08,
        )
    except requests.HTTPError as e:
        print(f"{underlying}: SKIP — Binance {e.response.status_code if e.response is not None else e}")
        return -1
    except Exception as e:
        print(f"{underlying}: SKIP — {type(e).__name__}: {e}")
        return -1

    bars = [kline_to_bar(k) for k in raw]
    bars = [b for b in bars if start_ms <= int(b["open_time_ms"]) <= end_ms_val]
    if not bars:
        print(f"{underlying}: SKIP — 0 bars")
        return -1
    n = store_bars(conn, underlying=underlying, symbol=symbol, bars=bars)
    print(f"{underlying}: {n} bars ({bars[0]['time']} → {bars[-1]['time']})")
    return n


def main() -> int:
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    end_sgt = last_closed_5m_open_sgt()
    end_ms_val = int(end_sgt.timestamp() * 1000)
    start_sgt = end_sgt - timedelta(hours=HOURS)
    start_ms = int(start_sgt.timestamp() * 1000)

    conn = sqlite3.connect(DB_PATH)
    fetched: list[str] = []
    skipped: list[str] = []
    try:
        init_db(conn, start_sgt=start_sgt, end_sgt=end_sgt, start_ms=start_ms, end_ms_val=end_ms_val)
        print(f"Window (SGT): {start_sgt.isoformat()} → {end_sgt.isoformat()}")
        for i, t in enumerate(ALL_TICKERS):
            n = fetch_symbol(conn, t, start_ms=start_ms, end_ms_val=end_ms_val)
            if n >= 0:
                fetched.append(t)
            else:
                skipped.append(t)
            if i < len(ALL_TICKERS) - 1:
                time.sleep(0.12)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("tickers_fetched", ",".join(fetched)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("tickers_skipped", ",".join(skipped)),
        )
        conn.commit()
        print(f"\nFetched {len(fetched)} / {len(ALL_TICKERS)} → {DB_PATH}")
        if skipped:
            print(f"Skipped: {', '.join(skipped)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
