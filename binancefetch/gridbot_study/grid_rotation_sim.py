"""Shared 48h kline fetch + band hyperparam for grid ticker rotation."""

from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

from binancefetch.binancefetch import (
    BINANCE_FUTURES_KLINES,
    fetch_klines_window,
    kline_to_bar,
)
from grid_vol_pause_backtest import (
    load_series,
    log_returns,
    precompute_vol_ratio,
    simulate,
)

SGT = timezone(timedelta(hours=8))
INTERVAL = "5m"
DATA_HOST = BINANCE_FUTURES_KLINES
GATE_TICKERS = ["BTC", "ETH"]
BAND_CANDIDATES = [1.5, 2.0, 2.5, 3.0, 3.5]
WARMUP_BARS = 110
DEFAULT_HOURS = 48


def last_closed_5m_open_sgt(now: datetime | None = None) -> datetime:
    now = now or datetime.now(SGT)
    minute_floor = (now.minute // 5) * 5
    current_open = now.replace(minute=minute_floor, second=0, microsecond=0)
    return current_open - timedelta(minutes=5)


def init_klines_db(conn: sqlite3.Connection) -> None:
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


def _store_bars(conn: sqlite3.Connection, *, underlying: str, symbol: str, bars: list[dict]) -> int:
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


def _fetch_symbol(
    conn: sqlite3.Connection,
    underlying: str,
    *,
    start_ms: int,
    end_ms_val: int,
    sleep_s: float = 0.08,
) -> bool:
    symbol = f"{underlying}USDT"
    try:
        raw = fetch_klines_window(
            symbol,
            INTERVAL,
            start_ms,
            end_ms_val,
            base_url=DATA_HOST,
            sleep_s=sleep_s,
        )
    except requests.HTTPError:
        return False
    except Exception:
        return False

    bars = [kline_to_bar(k) for k in raw]
    bars = [b for b in bars if start_ms <= int(b["open_time_ms"]) <= end_ms_val]
    if not bars:
        return False
    _store_bars(conn, underlying=underlying, symbol=symbol, bars=bars)
    return True


def fetch_klines_48h5m(
    tickers: Sequence[str],
    *,
    db_path: Path,
    hours: float = DEFAULT_HOURS,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Fetch Binance USDT-M 5m klines for *tickers* + BTC/ETH gate into sqlite."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    end_sgt = last_closed_5m_open_sgt()
    end_ms_val = int(end_sgt.timestamp() * 1000)
    start_sgt = end_sgt - timedelta(hours=float(hours))
    start_ms = int(start_sgt.timestamp() * 1000)

    study = [str(t).strip().upper() for t in tickers if str(t).strip()]
    all_syms = list(dict.fromkeys(study + GATE_TICKERS))

    conn = sqlite3.connect(db_path)
    fetched: List[str] = []
    skipped: List[str] = []
    try:
        init_klines_db(conn)
        conn.execute("DELETE FROM klines_5m")
        conn.execute("DELETE FROM meta")
        meta = {
            "interval": INTERVAL,
            "hours": str(hours),
            "start_sgt": start_sgt.isoformat(),
            "end_sgt": end_sgt.isoformat(),
            "start_ms": str(start_ms),
            "end_ms": str(end_ms_val),
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "tickers_requested": ",".join(study),
        }
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        conn.commit()

        for i, sym in enumerate(all_syms):
            ok = _fetch_symbol(conn, sym, start_ms=start_ms, end_ms_val=end_ms_val)
            if ok:
                fetched.append(sym)
            else:
                skipped.append(sym)
            if progress and (i == 0 or (i + 1) % 10 == 0 or i + 1 == len(all_syms)):
                progress(f"klines {i + 1}/{len(all_syms)} — {sym} {'ok' if ok else 'skip'}")
            if i < len(all_syms) - 1:
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
    finally:
        conn.close()

    return {
        "db": str(db_path),
        "fetched": fetched,
        "skipped": skipped,
        "start_sgt": start_sgt.isoformat(),
        "end_sgt": end_sgt.isoformat(),
    }


def run_band_hyperparam(
    db_path: Path,
    *,
    tickers: Sequence[str] | None = None,
    bands: Sequence[float] | None = None,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """Return per-ticker best band by sim PnL (vol-pause on)."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Missing DB: {db_path}")

    band_list = list(bands or BAND_CANDIDATES)
    conn = sqlite3.connect(db_path)
    try:
        btc_map = load_series(conn, "BTC")
        eth_map = load_series(conn, "ETH")
        if not btc_map or not eth_map:
            raise ValueError("BTC/ETH gate data required in DB")

        all_times = sorted(btc_map.keys())
        if len(all_times) < WARMUP_BARS + 10:
            raise ValueError(f"Insufficient bars: {len(all_times)}")

        sim_start_i = min(WARMUP_BARS, len(all_times) - 2)
        window_times = all_times
        btc_c = [btc_map[t] for t in window_times]
        eth_c = [eth_map[t] for t in window_times]

        if tickers is None:
            rows = conn.execute(
                "SELECT DISTINCT underlying FROM klines_5m ORDER BY underlying"
            ).fetchall()
            study = [str(r[0]) for r in rows if str(r[0]) not in GATE_TICKERS]
        else:
            study = [str(t).strip().upper() for t in tickers if str(t).strip().upper() not in GATE_TICKERS]

        per_ticker: List[Dict[str, Any]] = []
        study_sorted = sorted(study)
        for ti, ticker in enumerate(study_sorted):
            if progress and (ti == 0 or (ti + 1) % 10 == 0 or ti + 1 == len(study_sorted)):
                progress(f"hyperparam {ti + 1}/{len(study_sorted)} — {ticker}")
            tmap = load_series(conn, ticker)
            t_c = [tmap.get(t, float("nan")) for t in window_times]
            if any(math.isnan(x) for x in t_c):
                continue

            vol_ratio = precompute_vol_ratio(log_returns(t_c))
            best_band = None
            best_pnl = -1e18
            best_dd = 0.0
            best_pauses = 0
            best_volume = 0.0
            best_bps = 0.0

            for band in band_list:
                res = simulate(
                    ticker,
                    float(band),
                    t_c,
                    btc_c,
                    eth_c,
                    vol_ratio,
                    sim_start_i,
                    use_vol_pause=True,
                )
                if res["pnl"] > best_pnl:
                    best_pnl = float(res["pnl"])
                    best_band = float(band)
                    best_dd = float(res["dd"])
                    best_pauses = int(res["pauses"])
                    best_volume = float(res.get("volume_usd") or 0.0)
                    best_bps = float(res.get("bps") or 0.0)

            if best_band is None:
                continue
            per_ticker.append(
                {
                    "ticker": ticker,
                    "best_band_pct": best_band,
                    "best_pnl": round(best_pnl, 2),
                    "best_dd": round(best_dd, 2),
                    "pauses": best_pauses,
                    "volume_usd": round(best_volume, 2),
                    "bps": round(best_bps, 2),
                }
            )

        per_ticker.sort(key=lambda x: x["best_pnl"], reverse=True)
        return per_ticker
    finally:
        conn.close()
