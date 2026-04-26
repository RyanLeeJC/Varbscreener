#!/usr/bin/env python3
"""
Baseline median long/short backtest (cross-sectional median of 24h returns).

Data source: hourly OHLC bars from SQLite table `ohlc_bars` (timeframe '1h').
All returns and PnL are computed open-to-open using `ohlc_bars.open`.

Strategy:
- For each trading day, align each coin to the entry wall time and compute its 24h return.
- Compute the median 24h return across eligible tickers (universe depends on --mcap-rank;
  exclude BTC and ETH from the median calculation).
- Longs: tickers with 24h return > median
- Shorts: tickers with 24h return < median
- Equal number of longs and shorts: N = min(len(longs), len(shorts)), optionally capped so 2N
  <= --max-ticker-entries.

Notional sizing:
- Default split book (--notional-total, default $10k/day): each position = total / (2N).
- With --ctrlpossize yes: if a traded day has fewer than 10 legs (2N < 10), halve sizing for that day.
- With --skipsmallqty yes: if total legs (2N) would be < 6, skip the day.

Writes JSON suitable for vari_0_generate_dashboard_html.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sqlite3
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

BTC_ID = "bitcoin"
ETH_ID = "ethereum"
TIMEFRAME = "1h"
HOUR_MS = 3_600_000

# Flags shared with other backtests
CTRPOSSIZE_TICKER_THRESHOLD = 10
SKIPSMALLQTY_MIN_LEGS = 6


def parse_time_of_day(text: str) -> Tuple[int, int]:
    s = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        raise ValueError(f"Unrecognized time: {text!r} (try 9am, 9:00, 21:00)")
    h = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = m.group(3)
    if mer == "am":
        if h == 12:
            h = 0
        elif not 0 <= h <= 11:
            raise ValueError(f"Invalid hour for am: {text!r}")
    elif mer == "pm":
        if h == 12:
            h = 12
        elif 1 <= h <= 11:
            h += 12
        else:
            raise ValueError(f"Invalid hour for pm: {text!r}")
    else:
        if not 0 <= h <= 23:
            raise ValueError(f"Hour out of range: {text!r}")
    if not 0 <= minute <= 59:
        raise ValueError(f"Minute out of range: {text!r}")
    return h, minute


def daterange_inclusive(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def align_ts_at_or_after(sorted_ts: Sequence[int], target_ms: int) -> Optional[int]:
    i = bisect_left(sorted_ts, target_ms)
    if i >= len(sorted_ts):
        return None
    return sorted_ts[i]


def last_ts_at_or_before(sorted_ts: Sequence[int], target_ms: int) -> Optional[int]:
    i = bisect_right(sorted_ts, target_ms) - 1
    if i < 0:
        return None
    return sorted_ts[i]


def price_at_exact(series: Dict[int, float], ts: int) -> Optional[float]:
    p = series.get(ts)
    if p is None or p <= 0:
        return None
    return float(p)


def pct_change_at_entry(
    sorted_ts: List[int],
    series: Dict[int, float],
    entry_ts: int,
    hours_back: int,
) -> Optional[float]:
    anchor_ms = entry_ts - hours_back * HOUR_MS
    ts0 = last_ts_at_or_before(sorted_ts, anchor_ms)
    if ts0 is None:
        return None
    p0 = price_at_exact(series, ts0)
    p1 = price_at_exact(series, entry_ts)
    if p0 is None or p1 is None:
        return None
    return p1 / p0 - 1.0


@dataclass
class TradeLeg:
    date: str
    coin_id: str
    side: str  # "long" | "short"
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    ret_fraction: float
    pnl_usd: float


@dataclass
class DayResult:
    date: str
    skipped: bool
    skip_reason: str
    target_entry_ms: int
    target_exit_ms: int
    entry_ts: Optional[int]
    exit_ts: Optional[int]
    median_24h: Optional[float]
    n_cand_for_median: int
    n_long_cand: int
    n_short_cand: int
    n_positions: int
    portfolio_pnl_usd: float
    legs: List[TradeLeg] = field(default_factory=list)


@dataclass
class SessionResult:
    """One interval session (may be skipped)."""

    entry_local_date: str  # local date of session entry (YYYY-MM-DD)
    skipped: bool
    skip_reason: str
    target_entry_wall_ms: int
    target_exit_wall_ms: int
    entry_ts: Optional[int]
    exit_ts: Optional[int]
    median_24h: Optional[float]
    n_cand_for_median: int
    n_long_cand: int
    n_short_cand: int
    n_positions: int
    portfolio_pnl_usd: float
    legs: List[TradeLeg] = field(default_factory=list)


def _no_trade_reason_line(dr: DayResult) -> Optional[str]:
    if not dr.skipped:
        return None
    sr = dr.skip_reason
    if sr == "missing_bars_align":
        return "Missing hourly bar at entry/exit wall"
    if sr == "missing_24h_signal":
        return "Missing 24h return for median computation"
    if sr == "no_balanced_pairs":
        return (
            f"No balanced book: {dr.n_long_cand} long cand., {dr.n_short_cand} short cand."
        )
    if sr == "skip_small_qty":
        return (
            f"Skipped: fewer than {SKIPSMALLQTY_MIN_LEGS} total tickers "
            f"(had {dr.n_positions}; need >= {SKIPSMALLQTY_MIN_LEGS})"
        )
    if sr:
        return sr.replace("_", " ")
    return "No trade"


def _no_session_reason_line(sr: SessionResult) -> Optional[str]:
    if not sr.skipped:
        return None
    r = sr.skip_reason
    if r == "missing_bars_align":
        return "Missing hourly bar at entry/exit wall"
    if r == "missing_24h_signal":
        return "Missing 24h return for median computation"
    if r == "no_balanced_pairs":
        return (
            f"No balanced book: {sr.n_long_cand} long cand., {sr.n_short_cand} short cand."
        )
    if r == "skip_small_qty":
        return (
            f"Skipped: fewer than {SKIPSMALLQTY_MIN_LEGS} total tickers "
            f"(had {sr.n_positions}; need >= {SKIPSMALLQTY_MIN_LEGS})"
        )
    if r:
        return r.replace("_", " ")
    return "No trade"


def load_series_open(
    conn: sqlite3.Connection,
    coin_ids: Sequence[str],
    ts_min: int,
    ts_max: int,
) -> Tuple[Dict[str, List[int]], Dict[str, Dict[int, float]]]:
    placeholders = ",".join("?" * len(coin_ids))
    q = f"""
        SELECT coin_id, ts, open
        FROM ohlc_bars
        WHERE timeframe = '{TIMEFRAME}'
          AND ts BETWEEN ? AND ?
          AND coin_id IN ({placeholders})
          AND open IS NOT NULL AND open > 0
        ORDER BY coin_id, ts ASC
    """
    rows = conn.execute(q, (ts_min, ts_max, *coin_ids)).fetchall()
    ts_lists: Dict[str, List[int]] = {}
    series: Dict[str, Dict[int, float]] = defaultdict(dict)
    for coin_id, ts, o in rows:
        ts_i = int(ts)
        series[str(coin_id)][ts_i] = float(o)
    for cid in coin_ids:
        ts_lists[str(cid)] = sorted(series[str(cid)].keys())
    return ts_lists, dict(series)


def mcap_top_coin_ids_at_entry_ts(
    conn: sqlite3.Connection,
    entry_ts_ms: int,
    max_rank: int,
) -> frozenset[str]:
    # Ranking only: market cap lives in market_metrics, not OHLC.
    q = """
    WITH lb AS (
      SELECT coin_id, MAX(ts) AS mts
      FROM market_metrics
      WHERE timeframe = ?
        AND ts <= ?
        AND market_cap IS NOT NULL
        AND market_cap > 0
      GROUP BY coin_id
    ),
    cap AS (
      SELECT m.coin_id,
             ROW_NUMBER() OVER (ORDER BY m.market_cap DESC) AS rk
      FROM market_metrics m
      INNER JOIN lb ON m.coin_id = lb.coin_id AND m.ts = lb.mts
      WHERE m.timeframe = ?
        AND m.market_cap IS NOT NULL
        AND m.market_cap > 0
    )
    SELECT coin_id FROM cap WHERE rk <= ?
    """
    try:
        rows = conn.execute(
            q,
            (TIMEFRAME, int(entry_ts_ms), TIMEFRAME, int(max_rank)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"market_metrics mcap rank query failed ({exc}). SQLite 3.25+ is required."
        ) from exc
    return frozenset(str(r[0]) for r in rows)


def resolve_blacklist_coin_ids(
    conn: sqlite3.Connection, raw: Any
) -> frozenset[str]:
    """
    Resolve a blacklist string into coin_ids.

    Accepts a comma/space-separated list of tokens. Each token may be:
    - a symbol (ticker), matched against coins.symbol case-insensitively
    - a coin_id (coins.coin_id)
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple)):
        toks = [str(t).strip() for t in raw if str(t).strip()]
    else:
        toks = [t.strip() for t in re.split(r"[,\s]+", str(raw)) if t.strip()]
    if not toks:
        return frozenset()

    out: set[str] = set()
    for tok in toks:
        t = tok.strip()
        if not t:
            continue
        # coin_id direct hit
        rows = conn.execute(
            "SELECT coin_id FROM coins WHERE coin_id = ?",
            (t,),
        ).fetchall()
        if rows:
            out.update(str(r[0]) for r in rows)
            continue
        # symbol match (may map to multiple coin_ids)
        rows = conn.execute(
            "SELECT coin_id FROM coins WHERE UPPER(symbol) = ?",
            (t.upper(),),
        ).fetchall()
        out.update(str(r[0]) for r in rows)

    # Always keep benchmarks available (even if user lists them)
    out.discard(BTC_ID)
    out.discard(ETH_ID)
    return frozenset(out)


def mcap_top_alt_coin_ids_at_entry_ts(
    conn: sqlite3.Connection,
    entry_ts_ms: int,
    *,
    alt_count: int,
    blacklist_ids: frozenset[str],
) -> frozenset[str]:
    """
    Return the top `alt_count` coin_ids by market cap at entry_ts_ms, excluding BTC/ETH and blacklist.

    This provides the "backfill" behavior: if a blacklisted ticker would otherwise be in the top N,
    we keep scanning down market-cap ranks until we have `alt_count` eligible alts (or run out).
    """
    q = """
    WITH lb AS (
      SELECT coin_id, MAX(ts) AS mts
      FROM market_metrics
      WHERE timeframe = ?
        AND ts <= ?
        AND market_cap IS NOT NULL
        AND market_cap > 0
      GROUP BY coin_id
    )
    SELECT m.coin_id
    FROM market_metrics m
    INNER JOIN lb ON m.coin_id = lb.coin_id AND m.ts = lb.mts
    WHERE m.timeframe = ?
      AND m.market_cap IS NOT NULL
      AND m.market_cap > 0
    ORDER BY m.market_cap DESC
    LIMIT 5000
    """
    try:
        rows = conn.execute(
            q,
            (TIMEFRAME, int(entry_ts_ms), TIMEFRAME),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"market_metrics mcap query failed ({exc}). SQLite 3.25+ is required."
        ) from exc

    allow: List[str] = []
    seen: set[str] = set()
    for (cid_raw,) in rows:
        cid = str(cid_raw)
        if cid in seen:
            continue
        seen.add(cid)
        if cid in (BTC_ID, ETH_ID):
            continue
        if cid in blacklist_ids:
            continue
        allow.append(cid)
        if len(allow) >= int(alt_count):
            break
    return frozenset(allow)


def load_coin_id_to_symbol_upper(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute("SELECT coin_id, symbol FROM coins").fetchall()
    out: Dict[str, str] = {}
    for cid, sym in rows:
        if not cid or sym is None:
            continue
        s = str(sym).strip().upper()
        if s:
            out[str(cid)] = s
    return out


def load_vari_ticker_coin_ids(conn: sqlite3.Connection) -> frozenset[str]:
    try:
        rows = conn.execute("SELECT coin_id FROM vari_tickers").fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"Missing table vari_tickers ({exc}). "
            "Create it with varidatabase/schema.sql and populate it "
            "(e.g. INSERT INTO vari_tickers SELECT coin_id FROM coins)."
        ) from exc
    return frozenset(str(r[0]) for r in rows if r and r[0])


def ny_wall_to_utc_ms(d: date, hour: int, minute: int, tz: ZoneInfo) -> int:
    dt = datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _local_date_from_ms(ms: int, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(ms / 1000.0, tz=tz).date()


def max_drawdown_usd(equity_points: Sequence[float]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for x in equity_points:
        peak = max(peak, x)
        max_dd = min(max_dd, x - peak)
    return max_dd  # negative or zero


def run_backtest(
    conn: sqlite3.Connection,
    *,
    start_date: date,
    end_date: date,
    entry_h: int,
    entry_m: int,
    exit_h: int,
    exit_m: int,
    interval_hours: Optional[float] = None,
    tz_name: str,
    notional_total_usd: float,
    mcap_rank_max: Optional[int] = None,
    max_ticker_entries: Optional[int] = None,
    ctrlpossize: bool = False,
    skipsmallqty: bool = False,
    pick_mode: str = "extreme",
    revert: bool = False,
    blacklist: Any = None,
    vari_tickers_only: bool = False,
) -> Tuple[dict, List[DayResult]]:
    tz = ZoneInfo(tz_name)

    # Load bounds: include 24h lookback before first entry + after last exit
    ts_start_load = ny_wall_to_utc_ms(start_date, entry_h, entry_m, tz) - 48 * HOUR_MS
    if interval_hours is not None:
        interval_ms = int(round(float(interval_hours) * 3600 * 1000))
        if interval_ms <= 0:
            raise ValueError("--interval must be positive")
        ts_end_load = ny_wall_to_utc_ms(end_date, 23, 59, tz) + interval_ms + 48 * HOUR_MS
    else:
        ts_end_load = ny_wall_to_utc_ms(end_date, exit_h, exit_m, tz) + 48 * HOUR_MS

    all_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT coin_id FROM ohlc_bars WHERE timeframe = ?",
            (TIMEFRAME,),
        ).fetchall()
    ]
    if vari_tickers_only:
        allow = load_vari_ticker_coin_ids(conn)
        all_ids = [cid for cid in all_ids if cid in allow or cid in (BTC_ID, ETH_ID)]
    if BTC_ID not in all_ids or ETH_ID not in all_ids:
        raise SystemExit("Database must include bitcoin and ethereum hourly OHLC bars.")

    sym_by_id = load_coin_id_to_symbol_upper(conn)
    blacklist_ids = resolve_blacklist_coin_ids(conn, blacklist)

    def coin_label(cid: str) -> str:
        return sym_by_id.get(cid, cid)

    ts_lists, series = load_series_open(conn, all_ids, ts_start_load, ts_end_load)
    btc_ts = ts_lists[BTC_ID]
    universe_all = [
        c for c in all_ids if c not in (BTC_ID, ETH_ID) and c not in blacklist_ids
    ]

    day_results: List[DayResult] = []

    # Interval mode: chain sessions every `interval_hours` hours, anchored at --trade-entry.
    # Example: --interval 12 --trade-entry 9am creates 9am→9pm→9am→... across the date range.
    if interval_hours is not None:
        interval_ms = int(round(float(interval_hours) * 3600 * 1000))
        entry_wall_ms = ny_wall_to_utc_ms(start_date, entry_h, entry_m, tz)
        sessions: List[SessionResult] = []

        while True:
            entry_ld = _local_date_from_ms(entry_wall_ms, tz)
            if entry_ld < start_date:
                entry_wall_ms += interval_ms
                continue
            if entry_ld > end_date:
                break

            target_entry_ms = entry_wall_ms
            target_exit_ms = entry_wall_ms + interval_ms
            entry_date_str = entry_ld.isoformat()

            entry_ts_btc = align_ts_at_or_after(btc_ts, target_entry_ms)
            exit_ts = (
                align_ts_at_or_after(btc_ts, target_exit_ms) if entry_ts_btc else None
            )
            if entry_ts_btc is None or exit_ts is None or exit_ts <= entry_ts_btc:
                sessions.append(
                    SessionResult(
                        entry_local_date=entry_date_str,
                        skipped=True,
                        skip_reason="missing_bars_align",
                        target_entry_wall_ms=target_entry_ms,
                        target_exit_wall_ms=target_exit_ms,
                        entry_ts=entry_ts_btc,
                        exit_ts=exit_ts,
                        median_24h=None,
                        n_cand_for_median=0,
                        n_long_cand=0,
                        n_short_cand=0,
                        n_positions=0,
                        portfolio_pnl_usd=0.0,
                        legs=[],
                    )
                )
                entry_wall_ms = target_exit_ms
                continue

            mcap_allow: Optional[frozenset[str]] = None
            if mcap_rank_max is not None:
                mcap_allow = mcap_top_alt_coin_ids_at_entry_ts(
                    conn,
                    int(entry_ts_btc),
                    alt_count=int(mcap_rank_max),
                    blacklist_ids=blacklist_ids,
                )

            cand: List[Tuple[str, float]] = []
            for cid in universe_all:
                if mcap_allow is not None and cid not in mcap_allow:
                    continue
                sl = ts_lists.get(cid) or []
                if not sl:
                    continue
                ts_e = align_ts_at_or_after(sl, target_entry_ms)
                if ts_e is None:
                    continue
                chg24 = pct_change_at_entry(sl, series[cid], ts_e, 24)
                if chg24 is None:
                    continue
                cand.append((cid, chg24))

            if not cand:
                sessions.append(
                    SessionResult(
                        entry_local_date=entry_date_str,
                        skipped=True,
                        skip_reason="missing_24h_signal",
                        target_entry_wall_ms=target_entry_ms,
                        target_exit_wall_ms=target_exit_ms,
                        entry_ts=entry_ts_btc,
                        exit_ts=exit_ts,
                        median_24h=None,
                        n_cand_for_median=0,
                        n_long_cand=0,
                        n_short_cand=0,
                        n_positions=0,
                        portfolio_pnl_usd=0.0,
                        legs=[],
                    )
                )
                entry_wall_ms = target_exit_ms
                continue

            med = float(statistics.median([v for _, v in cand]))
            longs = [(cid, v) for cid, v in cand if v > med]
            shorts = [(cid, v) for cid, v in cand if v < med]
            if pick_mode not in ("extreme", "near_median"):
                raise ValueError(
                    f"pick_mode must be 'extreme' or 'near_median', got {pick_mode!r}"
                )
            if pick_mode == "near_median":
                longs.sort(key=lambda t: (t[1] - med))  # closest above median first
                shorts.sort(key=lambda t: (med - t[1]))  # closest below median first
            else:
                longs.sort(key=lambda t: t[1], reverse=True)
                shorts.sort(key=lambda t: t[1])

            n = min(len(longs), len(shorts))
            if max_ticker_entries is not None:
                max_pairs = int(max_ticker_entries) // 2
                n = min(n, max_pairs)

            if n == 0:
                sessions.append(
                    SessionResult(
                        entry_local_date=entry_date_str,
                        skipped=True,
                        skip_reason="no_balanced_pairs",
                        target_entry_wall_ms=target_entry_ms,
                        target_exit_wall_ms=target_exit_ms,
                        entry_ts=entry_ts_btc,
                        exit_ts=exit_ts,
                        median_24h=med,
                        n_cand_for_median=len(cand),
                        n_long_cand=len(longs),
                        n_short_cand=len(shorts),
                        n_positions=0,
                        portfolio_pnl_usd=0.0,
                        legs=[],
                    )
                )
                entry_wall_ms = target_exit_ms
                continue

            total_legs = 2 * n
            if skipsmallqty and total_legs < SKIPSMALLQTY_MIN_LEGS:
                sessions.append(
                    SessionResult(
                        entry_local_date=entry_date_str,
                        skipped=True,
                        skip_reason="skip_small_qty",
                        target_entry_wall_ms=target_entry_ms,
                        target_exit_wall_ms=target_exit_ms,
                        entry_ts=entry_ts_btc,
                        exit_ts=exit_ts,
                        median_24h=med,
                        n_cand_for_median=len(cand),
                        n_long_cand=len(longs),
                        n_short_cand=len(shorts),
                        n_positions=total_legs,
                        portfolio_pnl_usd=0.0,
                        legs=[],
                    )
                )
                entry_wall_ms = target_exit_ms
                continue

            effective_book = float(notional_total_usd)
            if ctrlpossize and total_legs < CTRPOSSIZE_TICKER_THRESHOLD:
                effective_book *= 0.5
            leg_notional = effective_book / float(total_legs)

            picks_over = longs[:n]
            picks_under = shorts[:n]
            if revert:
                # Mean-revert: long underperformers, short overperformers.
                picks_long = picks_under
                picks_short = picks_over
            else:
                picks_long = picks_over
                picks_short = picks_under
            legs: List[TradeLeg] = []
            sess_pnl = 0.0

            for cid, _ in picks_long:
                sl = ts_lists[cid]
                ts_e = align_ts_at_or_after(sl, target_entry_ms)
                ts_x = align_ts_at_or_after(sl, target_exit_ms)
                if ts_e is None or ts_x is None or ts_x <= ts_e:
                    continue
                pe = price_at_exact(series[cid], ts_e)
                px = price_at_exact(series[cid], ts_x)
                if pe is None or px is None:
                    continue
                r_long = px / pe - 1.0
                pnl = leg_notional * r_long
                legs.append(
                    TradeLeg(
                        entry_date_str, cid, "long", ts_e, ts_x, pe, px, r_long, pnl
                    )
                )
                sess_pnl += pnl

            for cid, _ in picks_short:
                sl = ts_lists[cid]
                ts_e = align_ts_at_or_after(sl, target_entry_ms)
                ts_x = align_ts_at_or_after(sl, target_exit_ms)
                if ts_e is None or ts_x is None or ts_x <= ts_e:
                    continue
                pe = price_at_exact(series[cid], ts_e)
                px = price_at_exact(series[cid], ts_x)
                if pe is None or px is None:
                    continue
                r_long = px / pe - 1.0
                r_short = -r_long
                pnl = leg_notional * r_short
                legs.append(
                    TradeLeg(
                        entry_date_str,
                        cid,
                        "short",
                        ts_e,
                        ts_x,
                        pe,
                        px,
                        r_short,
                        pnl,
                    )
                )
                sess_pnl += pnl

            if len(legs) != total_legs:
                sessions.append(
                    SessionResult(
                        entry_local_date=entry_date_str,
                        skipped=True,
                        skip_reason="missing_bars_align",
                        target_entry_wall_ms=target_entry_ms,
                        target_exit_wall_ms=target_exit_ms,
                        entry_ts=entry_ts_btc,
                        exit_ts=exit_ts,
                        median_24h=med,
                        n_cand_for_median=len(cand),
                        n_long_cand=len(longs),
                        n_short_cand=len(shorts),
                        n_positions=len(legs),
                        portfolio_pnl_usd=0.0,
                        legs=[],
                    )
                )
                entry_wall_ms = target_exit_ms
                continue

            sessions.append(
                SessionResult(
                    entry_local_date=entry_date_str,
                    skipped=False,
                    skip_reason="",
                    target_entry_wall_ms=target_entry_ms,
                    target_exit_wall_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=med,
                    n_cand_for_median=len(cand),
                    n_long_cand=len(longs),
                    n_short_cand=len(shorts),
                    n_positions=len(legs),
                    portfolio_pnl_usd=sess_pnl,
                    legs=legs,
                )
            )

            entry_wall_ms = target_exit_ms

        # ---- Build dashboard JSON (daily aggregation by entry_local_date) ----
        by_day_pnl: Dict[str, float] = defaultdict(float)
        by_day_traded: Dict[str, bool] = defaultdict(bool)
        all_trades: List[TradeLeg] = []
        for sr in sessions:
            by_day_pnl[sr.entry_local_date] += sr.portfolio_pnl_usd
            if not sr.skipped and sr.n_positions > 0:
                by_day_traded[sr.entry_local_date] = True
                all_trades.extend(sr.legs)

        daily_rows: List[dict] = []
        for ld in sorted(by_day_pnl.keys()):
            pnl = round(by_day_pnl[ld], 2)
            if by_day_traded[ld]:
                daily_rows.append({"date": ld, "pnl": pnl})

        equity_cum = 0.0
        equity_rows: List[dict] = []
        for d in daterange_inclusive(start_date, end_date):
            ds = d.isoformat()
            pnl_day = by_day_pnl.get(ds, 0.0)
            equity_cum += pnl_day
            skipped_chart = not by_day_traded.get(ds)
            equity_rows.append(
                {"date": ds, "equity": round(equity_cum, 2), "skipped": skipped_chart}
            )

        daily_overview: List[dict] = []
        for sr in sessions:
            traded = (not sr.skipped) and sr.n_positions > 0
            n_side = sr.n_positions // 2 if traded else 0
            dt = datetime.fromtimestamp(sr.target_entry_wall_ms / 1000.0, tz=tz)
            dt_x = datetime.fromtimestamp(sr.target_exit_wall_ms / 1000.0, tz=tz)
            tz_short = tz.key.split("/")[-1]
            session_label = (
                f"{dt.day}/{dt.month}/{dt.year} {dt.strftime('%H:%M')} {tz_short}"
            )
            exit_label = f"{dt_x.day}/{dt_x.month}/{dt_x.year} {dt_x.strftime('%H:%M')} {tz_short}"
            daily_overview.append(
                {
                    "date": sr.entry_local_date,
                    "session_label": session_label,
                    "session_key": str(sr.entry_ts or sr.target_entry_wall_ms),
                    "entry_label": session_label,
                    "exit_label": exit_label,
                    "traded": traded,
                    "longs": n_side,
                    "shorts": n_side,
                    "pnl_usd": round(sr.portfolio_pnl_usd, 2) if traded else None,
                    "reason": None if traded else _no_session_reason_line(sr),
                }
            )

        monthly: Dict[str, float] = defaultdict(float)
        for row in daily_rows:
            ym = row["date"][:7]
            monthly[ym] += row["pnl"]
        monthly_out = {k: round(v, 2) for k, v in sorted(monthly.items())}

        coin_trades: Dict[str, int] = defaultdict(int)
        coin_pnl: Dict[str, float] = defaultdict(float)
        for t in all_trades:
            coin_trades[t.coin_id] += 1
            coin_pnl[t.coin_id] += t.pnl_usd

        traded_coins = [(c, p) for c, p in coin_pnl.items() if coin_trades[c] > 0]
        traded_coins.sort(key=lambda kv: kv[1], reverse=True)
        top = [
            {
                "coin_id": c,
                "coin": coin_label(c),
                "pnl": round(p, 2),
                "trades": coin_trades[c],
            }
            for c, p in traded_coins[:10]
        ]
        worst_coins = sorted(traded_coins, key=lambda kv: kv[1])[:10]
        worst = [
            {
                "coin_id": c,
                "coin": coin_label(c),
                "pnl": round(p, 2),
                "trades": coin_trades[c],
            }
            for c, p in worst_coins
        ]

        tz_short = tz.key.split("/")[-1]

        def _fmt_wall_label(ms: int) -> str:
            dt = datetime.fromtimestamp(ms / 1000.0, tz=tz)
            return f"{dt.day}/{dt.month}/{dt.year} {dt.strftime('%H:%M')} {tz_short}"

        trades_payload = sorted(
            (
                {
                    "date": t.date,
                    "session_key": str(t.entry_ts),
                    "coin_id": t.coin_id,
                    "coin": coin_label(t.coin_id),
                    "side": t.side,
                    "entry_label": _fmt_wall_label(t.entry_ts),
                    "exit_label": _fmt_wall_label(t.exit_ts),
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "ret": round(t.ret_fraction * 100.0, 4),
                    "pnl": round(t.pnl_usd, 2),
                }
                for t in all_trades
            ),
            key=lambda x: abs(x["pnl"]),
            reverse=True,
        )

        calendar_days = (end_date - start_date).days + 1
        active_days = len(daily_rows)
        total_pnl = equity_cum
        return_on_notional = (
            (total_pnl / float(notional_total_usd)) if notional_total_usd else 0.0
        )

        wins = sum(1 for r in daily_rows if r["pnl"] > 0)
        losses = sum(1 for r in daily_rows if r["pnl"] < 0)
        win_rate = wins / active_days if active_days else 0.0
        daily_pnls = [r["pnl"] for r in daily_rows]
        sharpe_ratio: Optional[float] = None
        if len(daily_pnls) >= 2:
            mean_d = sum(daily_pnls) / len(daily_pnls)
            var = sum((x - mean_d) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
            std = math.sqrt(var) if var > 0 else 0.0
            if std >= 1e-12:
                sharpe_ratio = (mean_d / std) * math.sqrt(365.0)
        best_day = max(daily_pnls) if daily_pnls else 0.0
        worst_day = min(daily_pnls) if daily_pnls else 0.0
        avg_daily = total_pnl / active_days if active_days else 0.0
        eq_series = [r["equity"] for r in equity_rows]
        mdd = max_drawdown_usd(eq_series)

        def _fmt_hm(h: int, m: int) -> str:
            if m != 0:
                return f"{h:02d}:{m:02d}"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            suf = "am" if h < 12 else "pm"
            return f"{h12}{suf}"

        def _fmt_cli_num(x: float) -> str:
            xf = float(x)
            if math.isfinite(xf) and abs(xf - round(xf)) < 1e-9:
                return str(int(round(xf)))
            s = f"{xf:.10f}".rstrip("0").rstrip(".")
            return s or "0"

        entry_label = _fmt_hm(entry_h, entry_m)
        notional_line = (
            f"${notional_total_usd:,.0f} per session (50% longs / 50% shorts); "
            f"each position = session total ÷ (2N) that session"
        )
        subtitle_parts: Dict[str, Any] = {
            "session": (
                f"interval {float(interval_hours)}h from {entry_h:02d}:{entry_m:02d} "
                f"{tz.key.split('/')[-1]}"
            ),
            "notional_line": notional_line,
            "notional_per_side_usd": float(notional_total_usd) / 2.0,
            "volatility_skipper": "Median 24h return (mcap-ranked universe; exclude BTC/ETH)",
            "cli_start_end": (
                f"--start-date {start_date.isoformat()} --end-date {end_date.isoformat()}"
            ),
            "cli_trade_times": (
                f"--trade-entry {entry_label} --interval {_fmt_cli_num(float(interval_hours))}"
            ),
            "cli_timezone": f"--trade-timezone {tz_name}",
            "cli_notional": f"--notional-total {_fmt_cli_num(float(notional_total_usd))}",
        }
        if mcap_rank_max is not None:
            subtitle_parts["cli_mcap_rank"] = f"--mcap-rank {int(mcap_rank_max)}"
        if max_ticker_entries is not None:
            subtitle_parts["cli_max_tickers"] = (
                f"--max-ticker-entries {int(max_ticker_entries)}"
            )
        if pick_mode != "extreme":
            subtitle_parts["cli_pick_mode"] = f"--pick-mode {pick_mode}"
        if revert:
            subtitle_parts["cli_revert"] = "--revert yes"
        if ctrlpossize:
            subtitle_parts["cli_ctrlpossize"] = "--ctrlpossize yes"
        if skipsmallqty:
            subtitle_parts["cli_skipsmallqty"] = "--skipsmallqty yes"
        if blacklist_ids:
            if isinstance(blacklist, (list, tuple)):
                bl_text = " ".join(str(t).strip() for t in blacklist if str(t).strip())
            else:
                bl_text = str(blacklist or "").strip()
            subtitle_parts["cli_blacklist"] = f"--blacklist {bl_text}"

        meta = {
            "strategy": "baseline_median_long_short",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "trade_entry_local": f"{entry_h:02d}:{entry_m:02d}",
            "trade_exit_local": "",
            "trade_timezone": tz_name,
            "subtitle_parts": subtitle_parts,
            "params": {
                "interval_hours": float(interval_hours),
                "price_basis": "ohlc_bars.open (open-to-open)",
                "median_basis": "alts only; exclude BTC/ETH",
                "notional_mode": "split_total_per_session",
                "notional_total_usd": float(notional_total_usd),
                "mcap_rank_max": mcap_rank_max,
                "mcap_rank_basis": "market_metrics.market_cap (ranking only)",
                "mcap_rank_as_of": "each_session_entry_ts",
                "max_ticker_entries": max_ticker_entries,
                "ctrlpossize": ctrlpossize,
                "ctrlpossize_ticker_threshold": CTRPOSSIZE_TICKER_THRESHOLD,
                "skipsmallqty": skipsmallqty,
                "skipsmallqty_min_legs": SKIPSMALLQTY_MIN_LEGS,
                "blacklist": sorted(blacklist_ids),
                "pick_mode": pick_mode,
                "revert": revert,
            },
            "summary": {
                "total_pnl_usd": round(total_pnl, 2),
                "win_rate": round(win_rate, 4),
                "win_days": wins,
                "loss_days": losses,
                "active_days": active_days,
                "calendar_days": calendar_days,
                "max_drawdown_usd": round(mdd, 2),
                "avg_daily_pnl_usd": round(avg_daily, 2),
                "best_day_pnl_usd": round(best_day, 2),
                "worst_day_pnl_usd": round(worst_day, 2),
                "return_on_notional": round(return_on_notional, 6),
                "sharpe_ratio": round(sharpe_ratio, 4)
                if sharpe_ratio is not None
                else None,
            },
            "trade_count": len(trades_payload),
        }

        data = {
            "meta": meta,
            "equity": equity_rows,
            "daily": daily_rows,
            "daily_overview": daily_overview,
            "monthly": monthly_out,
            "top": top,
            "worst": worst,
            "trades": trades_payload,
            "sessions": [
                {
                    "entry_local_date": s.entry_local_date,
                    "skipped": s.skipped,
                    "skip_reason": s.skip_reason,
                    "target_entry_wall_ms": s.target_entry_wall_ms,
                    "target_exit_wall_ms": s.target_exit_wall_ms,
                    "entry_ts": s.entry_ts,
                    "exit_ts": s.exit_ts,
                    "median_24h": None
                    if s.median_24h is None
                    else round(s.median_24h, 8),
                    "n_cand_for_median": s.n_cand_for_median,
                    "n_long_cand": s.n_long_cand,
                    "n_short_cand": s.n_short_cand,
                    "n_positions": s.n_positions,
                    "portfolio_pnl_usd": round(s.portfolio_pnl_usd, 2),
                }
                for s in sessions
            ],
        }
        return data, day_results

    for d in daterange_inclusive(start_date, end_date):
        ds = d.isoformat()
        target_entry_ms = ny_wall_to_utc_ms(d, entry_h, entry_m, tz)
        target_exit_ms = ny_wall_to_utc_ms(d, exit_h, exit_m, tz)
        if target_exit_ms <= target_entry_ms:
            # Overnight session (e.g. 18:00 -> 04:00): interpret exit as next local day.
            target_exit_ms = ny_wall_to_utc_ms(d + timedelta(days=1), exit_h, exit_m, tz)

        entry_ts_btc = align_ts_at_or_after(btc_ts, target_entry_ms)
        exit_ts = align_ts_at_or_after(btc_ts, target_exit_ms) if entry_ts_btc else None
        if entry_ts_btc is None or exit_ts is None or exit_ts <= entry_ts_btc:
            day_results.append(
                DayResult(
                    date=ds,
                    skipped=True,
                    skip_reason="missing_bars_align",
                    target_entry_ms=target_entry_ms,
                    target_exit_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=None,
                    n_cand_for_median=0,
                    n_long_cand=0,
                    n_short_cand=0,
                    n_positions=0,
                    portfolio_pnl_usd=0.0,
                )
            )
            continue

        mcap_allow: Optional[frozenset[str]] = None
        if mcap_rank_max is not None:
            # Universe for median/picks: top N alts by market cap (exclude BTC/ETH + blacklist),
            # with automatic backfill down the rank list.
            mcap_allow = mcap_top_alt_coin_ids_at_entry_ts(
                conn,
                int(entry_ts_btc),
                alt_count=int(mcap_rank_max),
                blacklist_ids=blacklist_ids,
            )

        # Build candidate returns (alts only; BTC/ETH excluded)
        cand: List[Tuple[str, float]] = []
        for cid in universe_all:
            if mcap_allow is not None and cid not in mcap_allow:
                continue
            sl = ts_lists.get(cid) or []
            if not sl:
                continue
            ts_e = align_ts_at_or_after(sl, target_entry_ms)
            if ts_e is None:
                continue
            chg24 = pct_change_at_entry(sl, series[cid], ts_e, 24)
            if chg24 is None:
                continue
            cand.append((cid, chg24))

        if not cand:
            day_results.append(
                DayResult(
                    date=ds,
                    skipped=True,
                    skip_reason="missing_24h_signal",
                    target_entry_ms=target_entry_ms,
                    target_exit_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=None,
                    n_cand_for_median=0,
                    n_long_cand=0,
                    n_short_cand=0,
                    n_positions=0,
                    portfolio_pnl_usd=0.0,
                )
            )
            continue

        med = float(statistics.median([v for _, v in cand]))

        longs = [(cid, v) for cid, v in cand if v > med]
        shorts = [(cid, v) for cid, v in cand if v < med]
        if pick_mode not in ("extreme", "near_median"):
            raise ValueError(f"pick_mode must be 'extreme' or 'near_median', got {pick_mode!r}")
        if pick_mode == "near_median":
            longs.sort(key=lambda t: (t[1] - med))
            shorts.sort(key=lambda t: (med - t[1]))
        else:
            longs.sort(key=lambda t: t[1], reverse=True)
            shorts.sort(key=lambda t: t[1])

        n = min(len(longs), len(shorts))
        if max_ticker_entries is not None:
            max_pairs = int(max_ticker_entries) // 2
            n = min(n, max_pairs)

        if n == 0:
            day_results.append(
                DayResult(
                    date=ds,
                    skipped=True,
                    skip_reason="no_balanced_pairs",
                    target_entry_ms=target_entry_ms,
                    target_exit_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=med,
                    n_cand_for_median=len(cand),
                    n_long_cand=len(longs),
                    n_short_cand=len(shorts),
                    n_positions=0,
                    portfolio_pnl_usd=0.0,
                )
            )
            continue

        total_legs = 2 * n
        if skipsmallqty and total_legs < SKIPSMALLQTY_MIN_LEGS:
            day_results.append(
                DayResult(
                    date=ds,
                    skipped=True,
                    skip_reason="skip_small_qty",
                    target_entry_ms=target_entry_ms,
                    target_exit_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=med,
                    n_cand_for_median=len(cand),
                    n_long_cand=len(longs),
                    n_short_cand=len(shorts),
                    n_positions=total_legs,
                    portfolio_pnl_usd=0.0,
                )
            )
            continue

        effective_book = float(notional_total_usd)
        if ctrlpossize and total_legs < CTRPOSSIZE_TICKER_THRESHOLD:
            effective_book *= 0.5
        leg_notional = effective_book / float(total_legs)

        picks_over = longs[:n]
        picks_under = shorts[:n]
        if revert:
            picks_long = picks_under
            picks_short = picks_over
        else:
            picks_long = picks_over
            picks_short = picks_under
        legs: List[TradeLeg] = []
        day_pnl = 0.0

        for cid, _ in picks_long:
            sl = ts_lists[cid]
            ts_e = align_ts_at_or_after(sl, target_entry_ms)
            ts_x = align_ts_at_or_after(sl, target_exit_ms)
            if ts_e is None or ts_x is None or ts_x <= ts_e:
                continue
            pe = price_at_exact(series[cid], ts_e)
            px = price_at_exact(series[cid], ts_x)
            if pe is None or px is None:
                continue
            r_long = px / pe - 1.0
            pnl = leg_notional * r_long
            legs.append(TradeLeg(ds, cid, "long", ts_e, ts_x, pe, px, r_long, pnl))
            day_pnl += pnl

        for cid, _ in picks_short:
            sl = ts_lists[cid]
            ts_e = align_ts_at_or_after(sl, target_entry_ms)
            ts_x = align_ts_at_or_after(sl, target_exit_ms)
            if ts_e is None or ts_x is None or ts_x <= ts_e:
                continue
            pe = price_at_exact(series[cid], ts_e)
            px = price_at_exact(series[cid], ts_x)
            if pe is None or px is None:
                continue
            r_long = px / pe - 1.0
            r_short = -r_long
            pnl = leg_notional * r_short
            legs.append(TradeLeg(ds, cid, "short", ts_e, ts_x, pe, px, r_short, pnl))
            day_pnl += pnl

        if len(legs) != total_legs:
            # If missing bars caused some legs to be dropped, treat the day as skipped for clarity.
            day_results.append(
                DayResult(
                    date=ds,
                    skipped=True,
                    skip_reason="missing_bars_align",
                    target_entry_ms=target_entry_ms,
                    target_exit_ms=target_exit_ms,
                    entry_ts=entry_ts_btc,
                    exit_ts=exit_ts,
                    median_24h=med,
                    n_cand_for_median=len(cand),
                    n_long_cand=len(longs),
                    n_short_cand=len(shorts),
                    n_positions=len(legs),
                    portfolio_pnl_usd=0.0,
                )
            )
            continue

        day_results.append(
            DayResult(
                date=ds,
                skipped=False,
                skip_reason="",
                target_entry_ms=target_entry_ms,
                target_exit_ms=target_exit_ms,
                entry_ts=entry_ts_btc,
                exit_ts=exit_ts,
                median_24h=med,
                n_cand_for_median=len(cand),
                n_long_cand=len(longs),
                n_short_cand=len(shorts),
                n_positions=len(legs),
                portfolio_pnl_usd=day_pnl,
                legs=legs,
            )
        )

    # Build dashboard JSON
    equity_cum = 0.0
    equity_rows: List[dict] = []
    daily_rows: List[dict] = []
    all_trades: List[TradeLeg] = []

    for dr in day_results:
        if not dr.skipped and dr.n_positions > 0:
            equity_cum += dr.portfolio_pnl_usd
            daily_rows.append({"date": dr.date, "pnl": round(dr.portfolio_pnl_usd, 2)})
            all_trades.extend(dr.legs)
        skipped_chart = dr.skipped or (not dr.skipped and dr.n_positions == 0)
        equity_rows.append(
            {"date": dr.date, "equity": round(equity_cum, 2), "skipped": skipped_chart}
        )

    daily_overview: List[dict] = []
    for dr in day_results:
        traded = (not dr.skipped) and dr.n_positions > 0
        daily_overview.append(
            {
                "date": dr.date,
                "traded": traded,
                "longs": (dr.n_positions // 2) if traded else 0,
                "shorts": (dr.n_positions // 2) if traded else 0,
                "pnl_usd": round(dr.portfolio_pnl_usd, 2) if traded else None,
                "reason": None if traded else _no_trade_reason_line(dr),
            }
        )

    monthly: Dict[str, float] = defaultdict(float)
    for row in daily_rows:
        ym = row["date"][:7]
        monthly[ym] += row["pnl"]
    monthly_out = {k: round(v, 2) for k, v in sorted(monthly.items())}

    coin_trades: Dict[str, int] = defaultdict(int)
    coin_pnl: Dict[str, float] = defaultdict(float)
    for t in all_trades:
        coin_trades[t.coin_id] += 1
        coin_pnl[t.coin_id] += t.pnl_usd

    traded_coins = [(c, p) for c, p in coin_pnl.items() if coin_trades[c] > 0]
    traded_coins.sort(key=lambda kv: kv[1], reverse=True)
    top = [
        {"coin_id": c, "coin": coin_label(c), "pnl": round(p, 2), "trades": coin_trades[c]}
        for c, p in traded_coins[:10]
    ]
    worst_coins = sorted(traded_coins, key=lambda kv: kv[1])[:10]
    worst = [
        {"coin_id": c, "coin": coin_label(c), "pnl": round(p, 2), "trades": coin_trades[c]}
        for c, p in worst_coins
    ]

    trades_payload = sorted(
        (
            {
                "date": t.date,
                "coin_id": t.coin_id,
                "coin": coin_label(t.coin_id),
                "side": t.side,
                "entry": t.entry_price,
                "exit": t.exit_price,
                "ret": round(t.ret_fraction * 100.0, 4),
                "pnl": round(t.pnl_usd, 2),
            }
            for t in all_trades
        ),
        key=lambda x: abs(x["pnl"]),
        reverse=True,
    )

    calendar_days = (end_date - start_date).days + 1
    active_days = len(daily_rows)
    total_pnl = equity_cum
    return_on_notional = (total_pnl / float(notional_total_usd)) if notional_total_usd else 0.0

    wins = sum(1 for r in daily_rows if r["pnl"] > 0)
    losses = sum(1 for r in daily_rows if r["pnl"] < 0)
    win_rate = wins / active_days if active_days else 0.0
    daily_pnls = [r["pnl"] for r in daily_rows]
    sharpe_ratio: Optional[float] = None
    if len(daily_pnls) >= 2:
        mean_d = sum(daily_pnls) / len(daily_pnls)
        var = sum((x - mean_d) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std >= 1e-12:
            sharpe_ratio = (mean_d / std) * math.sqrt(365.0)
    best_day = max(daily_pnls) if daily_pnls else 0.0
    worst_day = min(daily_pnls) if daily_pnls else 0.0
    avg_daily = total_pnl / active_days if active_days else 0.0
    eq_series = [r["equity"] for r in equity_rows]
    mdd = max_drawdown_usd(eq_series)

    def _fmt_hm(h: int, m: int) -> str:
        if m != 0:
            return f"{h:02d}:{m:02d}"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        suf = "am" if h < 12 else "pm"
        return f"{h12}{suf}"

    def _fmt_cli_num(x: float) -> str:
        xf = float(x)
        if math.isfinite(xf) and abs(xf - round(xf)) < 1e-9:
            return str(int(round(xf)))
        s = f"{xf:.10f}".rstrip("0").rstrip(".")
        return s or "0"

    entry_label = _fmt_hm(entry_h, entry_m)
    exit_label = _fmt_hm(exit_h, exit_m)
    notional_line = (
        f"${notional_total_usd:,.0f} total book (50% longs / 50% shorts); "
        f"each position = total ÷ (2N) that day"
    )

    subtitle_parts: Dict[str, Any] = {
        "session": f"{entry_label}–{exit_label} {tz.key.split('/')[-1]} daily",
        "notional_line": notional_line,
        "notional_per_side_usd": float(notional_total_usd) / 2.0,
        "volatility_skipper": "Median 24h return (mcap-ranked universe; exclude BTC/ETH)",
        "cli_start_end": (
            f"--start-date {start_date.isoformat()} --end-date {end_date.isoformat()}"
        ),
        "cli_trade_times": f"--trade-entry {entry_label} --trade-exit {exit_label}",
        "cli_timezone": f"--trade-timezone {tz_name}",
        "cli_notional": f"--notional-total {_fmt_cli_num(float(notional_total_usd))}",
    }
    if mcap_rank_max is not None:
        subtitle_parts["cli_mcap_rank"] = f"--mcap-rank {int(mcap_rank_max)}"
    if max_ticker_entries is not None:
        subtitle_parts["cli_max_tickers"] = f"--max-ticker-entries {int(max_ticker_entries)}"
    if pick_mode != "extreme":
        subtitle_parts["cli_pick_mode"] = f"--pick-mode {pick_mode}"
    if revert:
        subtitle_parts["cli_revert"] = "--revert yes"
    if ctrlpossize:
        subtitle_parts["cli_ctrlpossize"] = "--ctrlpossize yes"
    if skipsmallqty:
        subtitle_parts["cli_skipsmallqty"] = "--skipsmallqty yes"
    if blacklist_ids:
        # Store tokens for reproducibility; keep it compact in subtitle.
        if isinstance(blacklist, (list, tuple)):
            bl_text = " ".join(str(t).strip() for t in blacklist if str(t).strip())
        else:
            bl_text = str(blacklist or "").strip()
        subtitle_parts["cli_blacklist"] = f"--blacklist {bl_text}"

    meta = {
        "strategy": "baseline_median_long_short",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trade_entry_local": f"{entry_h:02d}:{entry_m:02d}",
        "trade_exit_local": f"{exit_h:02d}:{exit_m:02d}",
        "trade_timezone": tz_name,
        "subtitle_parts": subtitle_parts,
        "params": {
            "price_basis": "ohlc_bars.open (open-to-open)",
            "median_basis": "alts only; exclude BTC/ETH",
            "notional_mode": "split_total",
            "notional_total_usd": float(notional_total_usd),
            "mcap_rank_max": mcap_rank_max,
            "mcap_rank_basis": "market_metrics.market_cap (ranking only)",
            "mcap_rank_as_of": "each_day_entry_ts",
            "max_ticker_entries": max_ticker_entries,
            "ctrlpossize": ctrlpossize,
            "ctrlpossize_ticker_threshold": CTRPOSSIZE_TICKER_THRESHOLD,
            "skipsmallqty": skipsmallqty,
            "skipsmallqty_min_legs": SKIPSMALLQTY_MIN_LEGS,
            "blacklist": sorted(blacklist_ids),
            "pick_mode": pick_mode,
            "revert": revert,
        },
        "summary": {
            "total_pnl_usd": round(total_pnl, 2),
            "win_rate": round(win_rate, 4),
            "win_days": wins,
            "loss_days": losses,
            "active_days": active_days,
            "calendar_days": calendar_days,
            "max_drawdown_usd": round(mdd, 2),
            "avg_daily_pnl_usd": round(avg_daily, 2),
            "best_day_pnl_usd": round(best_day, 2),
            "worst_day_pnl_usd": round(worst_day, 2),
            "return_on_notional": round(return_on_notional, 6),
            "sharpe_ratio": round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        },
        "trade_count": len(trades_payload),
    }

    data = {
        "meta": meta,
        "equity": equity_rows,
        "daily": daily_rows,
        "daily_overview": daily_overview,
        "monthly": monthly_out,
        "top": top,
        "worst": worst,
        "trades": trades_payload,
    }
    return data, day_results


def write_dashboard_html_from_doc(data: Dict[str, Any], json_path: Path) -> Path:
    gen_path = Path(__file__).resolve().parent / "vari_0_generate_dashboard_html.py"
    if not gen_path.is_file():
        raise FileNotFoundError(f"Dashboard generator not found: {gen_path}")
    spec = importlib.util.spec_from_file_location("_median_baseline_dashboard_gen", gen_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dashboard generator: {gen_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    render_html = getattr(mod, "render_html", None)
    if render_html is None:
        raise RuntimeError(f"{gen_path} has no render_html()")
    html = render_html(data, title=json_path.stem)
    out_path = json_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run baseline median long/short backtest and write dashboard JSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--db-path",
        default=str(ROOT_DIR / "varidatabase" / "2026.229.sqlite"),
        help="SQLite database path",
    )
    p.add_argument("--start-date", default="2026-01-01", help="YYYY-MM-DD")
    p.add_argument("--end-date", default="2026-03-31", help="YYYY-MM-DD")
    p.add_argument(
        "--trade-entry",
        "--trade-entry-time",
        default="9am",
        metavar="TIME",
        dest="trade_entry",
        help="Local trade entry wall time, e.g. 9am, 9:00",
    )
    p.add_argument(
        "--trade-exit",
        "--trade-exit-time",
        default="9pm",
        metavar="TIME",
        dest="trade_exit",
        help="Local trade exit wall time, e.g. 9pm, 21:00",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="HOURS",
        help=(
            "If set, ignore --trade-exit and instead chain sessions every N hours: "
            "exit wall = entry wall + interval, then immediately re-enter at that exit wall "
            "with a newly computed median. Example: --trade-entry 9am --interval 12 "
            "creates 9am→9pm→9am→… sessions."
        ),
    )
    p.add_argument(
        "--trade-timezone",
        default="America/New_York",
        help="IANA timezone for entry/exit wall times",
    )
    p.add_argument(
        "--notional-total",
        type=float,
        default=10_000.0,
        metavar="USD",
        help="Total USD book per trading day (half longs, half shorts); each position gets total/(2N).",
    )
    p.add_argument(
        "--mcap-rank",
        type=int,
        default=None,
        metavar="N",
        dest="mcap_rank",
        help="If set, universe for median + picks is global mcap ranks 1..N at entry (from market_metrics).",
    )
    p.add_argument(
        "--max-ticker-entries",
        type=int,
        default=None,
        metavar="XX",
        dest="max_ticker_entries",
        help="Cap total tickers entered that day at XX (N long + N short, N=floor(XX/2)).",
    )
    p.add_argument(
        "--pick-mode",
        choices=("extreme", "near_median"),
        default="extreme",
        help=(
            "How to pick within the long/short candidate pools. "
            "'extreme' = strongest outperformers + weakest underperformers (default). "
            "'near_median' = worst outperformers + best underperformers (closest to the median boundary)."
        ),
    )
    p.add_argument(
        "--revert",
        choices=("yes", "no"),
        default="no",
        help=(
            "When yes: flip sides vs the baseline. Long underperformers (< median) and short "
            "overperformers (> median)."
        ),
    )
    p.add_argument(
        "--output",
        "-o",
        default="",
        help="Write JSON to this path (stdout if empty)",
    )
    p.add_argument(
        "--no-dashboard-html",
        action="store_true",
        help="With -o/--output, skip generating sibling .html",
    )
    p.add_argument(
        "--ctrlpossize",
        choices=("yes", "no"),
        default="no",
        help=(
            "When yes: if a traded day has fewer than 10 legs (2N<10), halve sizing for that day "
            "before splitting across legs."
        ),
    )
    p.add_argument(
        "--skipsmallqty",
        choices=("yes", "no"),
        default="no",
        help=(
            f"When yes: if total tickers entered in a day is fewer than {SKIPSMALLQTY_MIN_LEGS}, "
            "skip that day."
        ),
    )
    p.add_argument(
        "--blacklist",
        nargs="+",
        default=[],
        metavar="TICKERS",
        help=(
            "Comma/space-separated tickers (symbols) and/or coin_ids to exclude from the entire "
            "backtest (median universe + picks). Example: --blacklist PI TAO"
        ),
    )
    p.add_argument(
        "--vari-tickers-only",
        choices=("yes", "no"),
        default="no",
        help="When yes: restrict universe to coin_ids in SQLite table vari_tickers.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    if end_date < start_date:
        raise SystemExit("end-date must be >= start-date")

    entry_h, entry_m = parse_time_of_day(args.trade_entry)
    exit_h, exit_m = parse_time_of_day(args.trade_exit)
    interval_hours: Optional[float] = (
        float(args.interval) if args.interval is not None else None
    )
    if interval_hours is not None and interval_hours <= 0:
        raise SystemExit("--interval must be positive when set")

    if args.notional_total <= 0:
        raise SystemExit("--notional-total must be positive")
    if args.mcap_rank is not None and args.mcap_rank < 1:
        raise SystemExit("--mcap-rank must be >= 1 when set")
    if args.max_ticker_entries is not None and args.max_ticker_entries < 2:
        raise SystemExit("--max-ticker-entries must be >= 2 when set (need 2 for one long+short)")

    t0 = time.perf_counter()
    conn = sqlite3.connect(args.db_path)
    try:
        data, _ = run_backtest(
            conn,
            start_date=start_date,
            end_date=end_date,
            entry_h=entry_h,
            entry_m=entry_m,
            exit_h=exit_h,
            exit_m=exit_m,
            interval_hours=interval_hours,
            tz_name=args.trade_timezone,
            notional_total_usd=float(args.notional_total),
            mcap_rank_max=args.mcap_rank,
            max_ticker_entries=args.max_ticker_entries,
            ctrlpossize=args.ctrlpossize == "yes",
            skipsmallqty=args.skipsmallqty == "yes",
            pick_mode=str(args.pick_mode),
            revert=args.revert == "yes",
            blacklist=args.blacklist,
            vari_tickers_only=args.vari_tickers_only == "yes",
        )
    finally:
        conn.close()

    text = json.dumps(data, indent=2)
    if args.output:
        json_path = Path(args.output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(text, encoding="utf-8")
        if not args.no_dashboard_html:
            html_path = write_dashboard_html_from_doc(data, json_path)
            print(f"Wrote dashboard HTML: {html_path}", file=sys.stderr)
    else:
        print(text)

    elapsed = time.perf_counter() - t0
    if elapsed >= 60.0:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        print(f"Backtest completed in {mins}m{secs:02d}s.", file=sys.stderr)
    else:
        yy = f"{elapsed:.1f}" if elapsed < 1.0 else str(int(elapsed))
        print(f"Backtest completed in {yy}s.", file=sys.stderr)


if __name__ == "__main__":
    main()

