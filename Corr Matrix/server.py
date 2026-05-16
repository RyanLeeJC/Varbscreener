#!/usr/bin/env python3
"""
Local HTTP server for the Correlations Matrix UI.

Browsers cannot read SQLite from disk directly; this process serves
`index.html` and JSON APIs that read `vari_railway_db.sqlite`.

Usage (from repo root or this folder):
  python3 "Corr Matrix/server.py"

Optional:
  VARI_CORR_DB=/path/to/vari_railway_db.sqlite
  CORR_MATRIX_PORT=8787
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_DB = REPO / "Vari Listings" / "vari_railway_db" / "vari_railway_db.sqlite"

MIN_RETURNS = 30


def db_path() -> Path:
    raw = os.environ.get("VARI_CORR_DB", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_DB


def parse_fetched_at(s: str):
    """Parse runs.fetched_at_utc (ISO, usually ...Z) to comparable UTC string."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    from datetime import datetime

    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def pearson_r(xs: list[float], ys: list[float]) -> tuple[float | None, int]:
    """Pearson r on paired samples; returns (r or None, n)."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None, n
    mx = sum(xs) / n
    my = sum(ys) / n
    num = 0.0
    denx = 0.0
    deny = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        num += dx * dy
        denx += dx * dx
        deny += dy * dy
    if denx <= 1e-18 or deny <= 1e-18:
        return None, n
    r = num / math.sqrt(denx * deny)
    if r > 1.0:
        r = 1.0
    elif r < -1.0:
        r = -1.0
    return r, n


def load_run_series(conn: sqlite3.Connection, t_iso_start: str, t_iso_end: str):
    """
    Ordered successful runs in [start, end] with per-ticker mark_price map.
    Returns (run_ids_in_order, run_id -> {"ts": str, "px": {ticker: float}}).
    """
    q = """
        SELECT r.run_id, r.fetched_at_utc, l.vari_ticker, l.mark_price
        FROM runs r
        INNER JOIN listings l ON l.run_id = r.run_id
        WHERE r.status = 'ok'
          AND r.fetched_at_utc >= ?
          AND r.fetched_at_utc <= ?
          AND l.mark_price IS NOT NULL AND l.mark_price > 0
        ORDER BY r.fetched_at_utc ASC, r.run_id ASC, l.vari_ticker ASC
    """
    run_order: list[int] = []
    by_run: dict[int, dict] = {}
    for run_id, fts, ticker, price in conn.execute(q, (t_iso_start, t_iso_end)):
        if run_id not in by_run:
            run_order.append(run_id)
            by_run[run_id] = {"ts": fts, "px": {}}
        by_run[run_id]["px"][str(ticker)] = float(price)
    return run_order, by_run


def log_returns_for_pair(
    run_order: list[int],
    by_run: dict[int, dict],
    ta: str,
    tb: str,
) -> tuple[list[float], list[float]]:
    """Aligned log returns between consecutive snapshots for tickers ta, tb."""
    ra: list[float] = []
    rb: list[float] = []
    for i in range(len(run_order) - 1):
        r0, r1 = run_order[i], run_order[i + 1]
        p0 = by_run[r0]["px"]
        p1 = by_run[r1]["px"]
        if ta not in p0 or ta not in p1 or tb not in p0 or tb not in p1:
            continue
        a0, a1 = p0[ta], p1[ta]
        b0, b1 = p0[tb], p1[tb]
        if a0 <= 0 or a1 <= 0 or b0 <= 0 or b1 <= 0:
            continue
        ra.append(math.log(a1 / a0))
        rb.append(math.log(b1 / b0))
    return ra, rb


def time_bounds(conn: sqlite3.Connection, tf: str) -> tuple[str, str, dict]:
    """
    Returns (iso_start, iso_end, meta) for window on ok runs.
    tf in 1D, 3D, 1W, ALL (case-insensitive).
    """
    from datetime import timedelta, timezone

    row = conn.execute(
        "SELECT MIN(fetched_at_utc), MAX(fetched_at_utc) FROM runs WHERE status = 'ok'"
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        raise ValueError("No successful runs in database")
    t_min_s, t_max_s = str(row[0]), str(row[1])
    end_dt = parse_fetched_at(t_max_s)
    start_full_dt = parse_fetched_at(t_min_s)
    if end_dt is None or start_full_dt is None:
        raise ValueError("Could not parse run timestamps")

    tfu = tf.strip().upper()
    if tfu == "ALL":
        start_dt = start_full_dt
    elif tfu == "1D":
        start_dt = max(start_full_dt, end_dt - timedelta(days=1))
    elif tfu == "3D":
        start_dt = max(start_full_dt, end_dt - timedelta(days=3))
    elif tfu == "1W":
        start_dt = max(start_full_dt, end_dt - timedelta(days=7))
    else:
        raise ValueError("Invalid timeframe (use 1D, 3D, 1W, ALL)")

    def fmt_z(dt):
        s = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        return s.replace("+00:00", "Z")

    meta = {
        "timeframe": tfu,
        "window_start_utc": fmt_z(start_dt),
        "window_end_utc": fmt_z(end_dt),
        "run_count_in_window": 0,
    }
    return fmt_z(start_dt), fmt_z(end_dt), meta


def matrix_payload(
    conn: sqlite3.Connection,
    tickers: list[str],
    tf: str,
) -> dict:
    t0, t1, meta = time_bounds(conn, tf)
    run_order, by_run = load_run_series(conn, t0, t1)
    meta["run_count_in_window"] = len(run_order)
    n = len(tickers)
    cells: list[list[dict]] = []
    for i in range(n):
        row = []
        for j in range(n):
            ti, tj = tickers[i], tickers[j]
            if i == j:
                row.append({"kind": "diag", "ticker": ti})
                continue
            xs, ys = log_returns_for_pair(run_order, by_run, ti, tj)
            m = len(xs)
            if m < MIN_RETURNS:
                row.append({"kind": "data", "r": None, "n": m, "display": "--"})
                continue
            r, _ = pearson_r(xs, ys)
            if r is None:
                row.append({"kind": "data", "r": None, "n": m, "display": "--"})
            else:
                row.append(
                    {
                        "kind": "data",
                        "r": r,
                        "n": m,
                        "display": f"{r:.2f}",
                    }
                )
        cells.append(row)
    return {"tickers": tickers, "cells": cells, "meta": meta}


def list_tickers(conn: sqlite3.Connection, q: str, limit: int = 400) -> list[dict]:
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        sql = """
            SELECT vari_ticker, MAX(COALESCE(vari_name, '')) AS nm
            FROM listings l
            INNER JOIN runs r ON r.run_id = l.run_id
            WHERE r.status = 'ok' AND (l.vari_ticker LIKE ? OR l.vari_name LIKE ?)
            GROUP BY l.vari_ticker
            ORDER BY l.vari_ticker ASC
            LIMIT ?
        """
        rows = conn.execute(sql, (like, like, limit))
    else:
        sql = """
            SELECT vari_ticker, MAX(COALESCE(vari_name, '')) AS nm
            FROM listings l
            INNER JOIN runs r ON r.run_id = l.run_id
            WHERE r.status = 'ok'
            GROUP BY l.vari_ticker
            ORDER BY l.vari_ticker ASC
            LIMIT ?
        """
        rows = conn.execute(sql, (limit,))
    return [{"ticker": r[0], "name": r[1] or r[0]} for r in rows]


def default_tickers(conn: sqlite3.Connection) -> list[str]:
    preferred = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE"]
    found = []
    for t in preferred:
        c = conn.execute(
            """
            SELECT 1 FROM listings l
            JOIN runs r ON r.run_id = l.run_id
            WHERE r.status = 'ok' AND l.vari_ticker = ? LIMIT 1
            """,
            (t,),
        ).fetchone()
        if c:
            found.append(t)
        if len(found) >= 4:
            break
    if len(found) >= 4:
        return found[:4]
    rows = conn.execute(
        """
        SELECT vari_ticker FROM listings l
        JOIN runs r ON r.run_id = l.run_id
        WHERE r.status = 'ok'
        GROUP BY vari_ticker
        ORDER BY vari_ticker ASC
        LIMIT 4
        """
    ).fetchall()
    return [r[0] for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "CorrMatrix/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        b = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._send(code, b, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            p = HERE / "index.html"
            if not p.is_file():
                self._json(500, {"error": "index.html missing next to server.py"})
                return
            data = p.read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return

        if path == "/api/health":
            p = db_path()
            self._json(
                200,
                {
                    "ok": p.is_file(),
                    "db": str(p),
                    "min_returns": MIN_RETURNS,
                },
            )
            return

        if not db_path().is_file():
            self._json(
                503,
                {
                    "error": "SQLite file not found",
                    "path": str(db_path()),
                },
            )
            return

        try:
            conn = sqlite3.connect(str(db_path()))
        except sqlite3.Error as e:
            self._json(500, {"error": str(e)})
            return

        try:
            if path == "/api/default-tickers":
                self._json(200, {"tickers": default_tickers(conn)})
                return

            if path == "/api/tickers":
                q = (qs.get("q") or [""])[0]
                lim = int((qs.get("limit") or ["400"])[0])
                lim = max(1, min(lim, 2000))
                self._json(200, {"items": list_tickers(conn, q, lim)})
                return

            if path == "/api/matrix":
                tf = (qs.get("tf") or ["1W"])[0]
                raw = (qs.get("tickers") or [""])[0]
                parts = [p.strip() for p in raw.split(",") if p.strip()]
                if len(parts) < 2:
                    self._json(
                        400,
                        {"error": "Provide at least two comma-separated tickers"},
                    )
                    return
                if len(parts) > 32:
                    self._json(400, {"error": "At most 32 tickers"})
                    return
                bad = [t for t in parts if not _valid_ticker(t)]
                if bad:
                    self._json(400, {"error": "Invalid ticker", "tickers": bad})
                    return
                try:
                    payload = matrix_payload(conn, parts, tf)
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                self._json(200, payload)
                return

            self._json(404, {"error": "Not found"})
        finally:
            conn.close()


def _valid_ticker(t: str) -> bool:
    if not t or len(t) > 32:
        return False
    for ch in t:
        if ch.isalnum() or ch in ("_", "-", "."):
            continue
        return False
    return True


def main():
    port = int(os.environ.get("CORR_MATRIX_PORT", "8787"))
    dbp = db_path()
    print(f"Corr Matrix server http://127.0.0.1:{port}/")
    print(f"Database: {dbp}  ({'ok' if dbp.is_file() else 'MISSING'})")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
