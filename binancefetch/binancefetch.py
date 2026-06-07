#!/usr/bin/env python3
"""Fetch Binance klines or USDT-M funding rate history → JSON (+ optional .data.js).

See binancefetch.md for API intervals, pagination, and rate limits.

Examples:
  python3 binancefetch/binancefetch.py
  python3 binancefetch/binancefetch.py --symbol BTCUSDT --interval 1h --days 30 --full
  python3 binancefetch/binancefetch.py --symbol CLUSDT --funding --days 30 --data-host futures --full
  python3 binancefetch/binancefetch.py --symbol ETHUSDT --interval 5m \\
      --start 2025-05-01T00:00:00Z --end 2025-05-15T00:00:00Z --full
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_DATA_KLINES = "https://data-api.binance.vision/api/v3/klines"
BINANCE_FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FUNDING_RATE = "https://fapi.binance.com/fapi/v1/fundingRate"

FUNDING_STALE_MS = 60 * 60 * 1000  # re-fetch if last record older than 1h

VALID_INTERVALS: frozenset[str] = frozenset(
    {
        "1s",
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
        "1M",
    }
)

# Approximate bar length in ms for stale detection after incremental merge.
INTERVAL_MS: dict[str, int] = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    "1M": 30 * 24 * 60 * 60_000,
}

DEFAULT_SYMBOL = "ETHBTC"
DEFAULT_INTERVAL = "15m"
DEFAULT_DAYS = 14
ROOT = Path(__file__).resolve().parent


def parse_iso_ms(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def default_out_path(symbol: str, interval: str, days: int, *, funding: bool = False) -> Path:
    if funding:
        return ROOT / f"{symbol}_funding_last{days}d.json"
    return ROOT / f"{symbol}_{interval}_last{days}d.json"


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = BINANCE_KLINES,
    sleep_s: float = 0.1,
) -> list[list]:
    """Binance klines with startTime inclusive, paginated (max 1000 per request)."""
    rows: list[list] = []
    cur = int(start_ms)
    end_ms = int(end_ms)
    while cur < end_ms:
        resp = requests.get(
            base_url,
            params={
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
        if sleep_s > 0:
            time.sleep(sleep_s)
    return rows


def fetch_klines_window(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = BINANCE_KLINES,
    sleep_s: float = 0.1,
) -> list[list]:
    return fetch_klines_range(
        symbol,
        interval,
        start_ms,
        end_ms,
        base_url=base_url,
        sleep_s=sleep_s,
    )


def kline_to_bar(k: list) -> dict:
    open_ms = int(k[0])
    return {
        "time": datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc).isoformat(),
        "open_time_ms": open_ms,
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time_ms": int(k[6]),
        "quote_volume": float(k[7]),
        "trades": int(k[8]),
    }


def load_json_payload(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def trim_bars(bars: list[dict], days: int, *, now_ms: int | None = None) -> list[dict]:
    if not bars:
        return []
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = end_ms - max(1, days) * 24 * 3600 * 1000
    return sorted(
        (b for b in bars if int(b.get("open_time_ms") or 0) >= cutoff_ms),
        key=lambda b: int(b["open_time_ms"]),
    )


def merge_bars(existing: list[dict], new_bars: list[dict]) -> list[dict]:
    by_ms: dict[int, dict] = {}
    for b in existing + new_bars:
        ms = b.get("open_time_ms")
        if ms is not None:
            by_ms[int(ms)] = b
    return sorted(by_ms.values(), key=lambda b: int(b["open_time_ms"]))


def build_payload(
    symbol: str,
    interval: str,
    bars: list[dict],
    days: int,
    *,
    source: str = BINANCE_KLINES,
) -> dict:
    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "days": days,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bar_count": len(bars),
        "bars": bars,
    }


def fetch_funding_range(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    base_url: str = BINANCE_FUNDING_RATE,
    sleep_s: float = 0.1,
) -> list[dict]:
    """Funding rate history, ascending, paginated (max 1000 per request)."""
    rows: list[dict] = []
    cur = int(start_ms)
    end_ms = int(end_ms)
    while cur < end_ms:
        resp = requests.get(
            base_url,
            params={
                "symbol": symbol.upper(),
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
        if sleep_s > 0:
            time.sleep(sleep_s)
    return rows


def funding_to_record(row: dict) -> dict:
    funding_ms = int(row["fundingTime"])
    return {
        "time": datetime.fromtimestamp(funding_ms / 1000, tz=timezone.utc).isoformat(),
        "funding_time_ms": funding_ms,
        "funding_rate": float(row["fundingRate"]),
        "mark_price": float(row["markPrice"]),
    }


def trim_funding(records: list[dict], days: int, *, now_ms: int | None = None) -> list[dict]:
    if not records:
        return []
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = end_ms - max(1, days) * 24 * 3600 * 1000
    return sorted(
        (r for r in records if int(r.get("funding_time_ms") or 0) >= cutoff_ms),
        key=lambda r: int(r["funding_time_ms"]),
    )


def merge_funding(existing: list[dict], new_records: list[dict]) -> list[dict]:
    by_ms: dict[int, dict] = {}
    for r in existing + new_records:
        ms = r.get("funding_time_ms")
        if ms is not None:
            by_ms[int(ms)] = r
    return sorted(by_ms.values(), key=lambda r: int(r["funding_time_ms"]))


def build_funding_payload(symbol: str, records: list[dict], days: int) -> dict:
    return {
        "symbol": symbol.upper(),
        "days": days,
        "source": BINANCE_FUNDING_RATE,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }


def update_funding_incremental(
    json_path: Path,
    symbol: str,
    days: int,
    *,
    sleep_s: float = 0.1,
    force: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fetch_end_ms = end_ms if end_ms is not None else now_ms
    window_start_ms, _ = resolve_window(days=days, start_ms=start_ms, end_ms=fetch_end_ms)
    cutoff_ms = fetch_end_ms - max(1, days) * 24 * 3600 * 1000

    payload = load_json_payload(json_path)
    records: list[dict] = []
    if payload and isinstance(payload.get("records"), list):
        records = [r for r in payload["records"] if isinstance(r, dict)]

    added = 0
    fetched = False

    if not records or force:
        win_start, win_end = resolve_window(days=days, start_ms=start_ms, end_ms=fetch_end_ms)
        raw = fetch_funding_range(symbol, win_start, win_end, sleep_s=sleep_s)
        records = [funding_to_record(r) for r in raw]
        added = len(records)
        fetched = True
    else:
        last_ms = max(int(r["funding_time_ms"]) for r in records)
        gap_ms = fetch_end_ms - last_ms
        if gap_ms > FUNDING_STALE_MS:
            chunk_start = max(cutoff_ms, window_start_ms, last_ms + 1)
            if chunk_start < fetch_end_ms:
                raw = fetch_funding_range(symbol, chunk_start, fetch_end_ms, sleep_s=sleep_s)
                new_records = [funding_to_record(r) for r in raw]
                before = len(records)
                records = merge_funding(records, new_records)
                added = len(records) - before
                fetched = True

    trimmed_before = len(records)
    records = trim_funding(records, days, now_ms=fetch_end_ms)
    trimmed = trimmed_before - len(records)

    out = build_funding_payload(symbol, records, days)
    write_json(json_path, out)

    return {
        "path": str(json_path),
        "symbol": symbol.upper(),
        "fetched": fetched,
        "added": added,
        "trimmed": trimmed,
        "record_count": len(records),
    }


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_data_js(path: Path, payload: dict) -> None:
    js = (
        "/* Auto-generated by binancefetch.py — do not edit */\n"
        "window.__BINANCE_KLINE_DATA__ = "
        + json.dumps(payload, separators=(",", ":"))
        + ";\n"
    )
    path.write_text(js, encoding="utf-8")


def resolve_window(
    *,
    days: int,
    start_ms: int | None,
    end_ms: int | None,
) -> tuple[int, int]:
    end = end_ms if end_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if start_ms is not None:
        start = start_ms
    else:
        start = end - max(1, days) * 24 * 3600 * 1000
    if start >= end:
        raise ValueError("start must be before end")
    return start, end


def update_json_incremental(
    json_path: Path,
    symbol: str,
    interval: str,
    days: int,
    *,
    base_url: str = BINANCE_KLINES,
    sleep_s: float = 0.1,
    force: bool = False,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    """Append missing bars since last stored open; trim older than ``days``."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fetch_end_ms = end_ms if end_ms is not None else now_ms
    window_start_ms, _ = resolve_window(days=days, start_ms=start_ms, end_ms=fetch_end_ms)
    cutoff_ms = fetch_end_ms - max(1, days) * 24 * 3600 * 1000
    stale_after_ms = INTERVAL_MS.get(interval, 60_000)

    payload = load_json_payload(json_path)
    bars: list[dict] = []
    if payload and isinstance(payload.get("bars"), list):
        bars = [b for b in payload["bars"] if isinstance(b, dict)]

    added = 0
    fetched = False

    if not bars or force:
        win_start, win_end = resolve_window(days=days, start_ms=start_ms, end_ms=fetch_end_ms)
        raw = fetch_klines_window(
            symbol,
            interval,
            win_start,
            win_end,
            base_url=base_url,
            sleep_s=sleep_s,
        )
        bars = [kline_to_bar(k) for k in raw]
        added = len(bars)
        fetched = True
    else:
        last_ms = max(int(b["open_time_ms"]) for b in bars)
        gap_ms = fetch_end_ms - last_ms
        if gap_ms > stale_after_ms:
            chunk_start = max(cutoff_ms, window_start_ms, last_ms + 1)
            if chunk_start < fetch_end_ms:
                raw = fetch_klines_range(
                    symbol,
                    interval,
                    chunk_start,
                    fetch_end_ms,
                    base_url=base_url,
                    sleep_s=sleep_s,
                )
                new_bars = [kline_to_bar(k) for k in raw]
                before = len(bars)
                bars = merge_bars(bars, new_bars)
                added = len(bars) - before
                fetched = True

    trimmed_before = len(bars)
    bars = trim_bars(bars, days, now_ms=fetch_end_ms)
    trimmed = trimmed_before - len(bars)

    out = build_payload(symbol, interval, bars, days, source=base_url)
    write_json(json_path, out)

    return {
        "path": str(json_path),
        "symbol": symbol.upper(),
        "interval": interval,
        "fetched": fetched,
        "added": added,
        "trimmed": trimmed,
        "bar_count": len(bars),
    }


def run_funding(args: argparse.Namespace, symbol: str) -> None:
    if args.data_host != "futures":
        raise SystemExit("--funding requires --data-host futures")

    days = max(1, args.days)
    start_ms = parse_iso_ms(args.start) if args.start else None
    end_ms = parse_iso_ms(args.end) if args.end else None
    out_path = args.out or default_out_path(symbol, "", days, funding=True)

    if args.full:
        win_start, win_end = resolve_window(days=days, start_ms=start_ms, end_ms=end_ms)
        raw = fetch_funding_range(symbol, win_start, win_end, sleep_s=args.sleep)
        records = trim_funding([funding_to_record(r) for r in raw], days, now_ms=win_end)
        payload = build_funding_payload(symbol, records, days)
        write_json(out_path, payload)
        stats = {"fetched": True, "added": len(records), "trimmed": 0, "record_count": len(records)}
    else:
        stats = update_funding_incremental(
            out_path,
            symbol,
            days,
            sleep_s=args.sleep,
            force=False,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        payload = load_json_payload(out_path) or {}

    msg = f"{symbol} funding: {stats['record_count']} records → {out_path.name}"
    if stats.get("fetched"):
        msg += f" (+{stats.get('added', 0)} new"
        if stats.get("trimmed"):
            msg += f", -{stats['trimmed']} trimmed"
        msg += ")"
    print(msg)

    if args.data_js and payload:
        js_path = out_path.with_suffix(".data.js")
        js = (
            "/* Auto-generated by binancefetch.py — do not edit */\n"
            "window.__BINANCE_FUNDING_DATA__ = "
            + json.dumps(payload, separators=(",", ":"))
            + ";\n"
        )
        js_path.write_text(js, encoding="utf-8")
        print(f"Wrote {js_path.name}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch Binance klines or futures funding history (see binancefetch.md)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"trading pair (default {DEFAULT_SYMBOL})")
    ap.add_argument(
        "--funding",
        action="store_true",
        help="fetch USDT-M funding rate history (requires --data-host futures)",
    )
    ap.add_argument(
        "--interval",
        default=DEFAULT_INTERVAL,
        choices=sorted(VALID_INTERVALS, key=lambda x: (x[-1], x)),
        help=f"candle resolution (default {DEFAULT_INTERVAL})",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"rolling window kept in JSON (default {DEFAULT_DAYS})",
    )
    ap.add_argument("--start", metavar="ISO", help="UTC range start, e.g. 2025-01-01T00:00:00Z")
    ap.add_argument("--end", metavar="ISO", help="UTC range end (default: now)")
    ap.add_argument("--out", type=Path, help="output JSON path (default: ./{symbol}_{interval}_last{days}d.json)")
    ap.add_argument(
        "--full",
        action="store_true",
        help="re-download full window (default: incremental append + trim)",
    )
    ap.add_argument(
        "--data-js",
        action="store_true",
        help="also write sibling .data.js with window.__BINANCE_KLINE_DATA__",
    )
    ap.add_argument(
        "--data-host",
        choices=["api", "data-api", "futures"],
        default="api",
        help="spot api (default), data-api.binance.vision, or fapi USDT-M perpetuals",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.1,
        metavar="SEC",
        help="pause between paginated requests (default 0.1)",
    )
    args = ap.parse_args()

    symbol = re.sub(r"[^A-Za-z0-9]", "", args.symbol).upper()
    if not symbol:
        ap.error("invalid --symbol")

    if args.funding:
        run_funding(args, symbol)
        return

    days = max(1, args.days)
    start_ms = parse_iso_ms(args.start) if args.start else None
    end_ms = parse_iso_ms(args.end) if args.end else None
    base_url = {
        "api": BINANCE_KLINES,
        "data-api": BINANCE_DATA_KLINES,
        "futures": BINANCE_FUTURES_KLINES,
    }[args.data_host]
    out_path = args.out or default_out_path(symbol, args.interval, days)

    if args.full:
        win_start, win_end = resolve_window(days=days, start_ms=start_ms, end_ms=end_ms)
        raw = fetch_klines_window(
            symbol,
            args.interval,
            win_start,
            win_end,
            base_url=base_url,
            sleep_s=args.sleep,
        )
        bars = [kline_to_bar(k) for k in raw]
        bars = trim_bars(bars, days, now_ms=win_end)
        payload = build_payload(symbol, args.interval, bars, days, source=base_url)
        write_json(out_path, payload)
        stats = {"fetched": True, "added": len(bars), "trimmed": 0, "bar_count": len(bars)}
    else:
        stats = update_json_incremental(
            out_path,
            symbol,
            args.interval,
            days,
            base_url=base_url,
            sleep_s=args.sleep,
            force=False,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        payload = load_json_payload(out_path) or {}

    msg = f"{symbol} {args.interval}: {stats['bar_count']} bars → {out_path.name}"
    if stats.get("fetched"):
        msg += f" (+{stats.get('added', 0)} new"
        if stats.get("trimmed"):
            msg += f", -{stats['trimmed']} trimmed"
        msg += ")"
    print(msg)

    if args.data_js and payload:
        js_path = out_path.with_suffix(".data.js")
        write_data_js(js_path, payload)
        print(f"Wrote {js_path.name}")


if __name__ == "__main__":
    main()
