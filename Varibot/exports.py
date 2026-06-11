from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient
from variationalbot.vari.errors import VariUnexpectedResponse

SGT = timezone(timedelta(hours=8))


def _iso_z(dt: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and Z suffix (Omni export filters)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    ms = int(dt.microsecond / 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _parse_iso(s: str) -> datetime:
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw).astimezone(timezone.utc)


_MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

_RESOURCE_PREFIX = {
    "transfers": "RealizedPNL",
    "trades": "Trades",
}


def _day_mon(dt: datetime) -> str:
    return f"{dt.day}{_MONTH_ABBR[dt.month - 1]}"


def export_range_suffix(
    gte: datetime,
    lte: datetime,
    *,
    window: Optional[str] = None,
) -> str:
    if window == "24h":
        return "24h"
    if window == "7d":
        return "7d"

    hours = (lte - gte).total_seconds() / 3600.0
    if 23.0 <= hours <= 25.0:
        return "24h"
    days = hours / 24.0
    if 6.5 <= days <= 7.5:
        return "7d"
    return f"{_day_mon(gte)}-{_day_mon(lte)}"


def export_filename(
    *,
    resource: str,
    created_at_gte: datetime,
    created_at_lte: datetime,
    window: Optional[str] = None,
) -> str:
    """e.g. RealizedPNL_24h.csv, Trades_3jun-6jun.csv"""
    prefix = _RESOURCE_PREFIX.get(resource.strip().lower(), resource.strip() or "Export")
    suffix = export_range_suffix(created_at_gte, created_at_lte, window=window)
    return f"{prefix}_{suffix}.csv"


def build_export_payload(
    *,
    resource: str,
    created_at_gte: str,
    created_at_lte: str,
    transfer_types: Optional[List[str]],
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {
        "created_at_gte": created_at_gte,
        "created_at_lte": created_at_lte,
    }
    if resource.strip().lower() == "transfers" and transfer_types:
        filters["transfer_types"] = transfer_types
    return {"resource": resource, "filters": filters}


def _looks_like_timeout(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("curl: (28)" in str(exc)) or ("operation timed out" in msg) or ("timeout" in msg)


def _parse_export_ban_wait_seconds(exc: BaseException) -> Optional[float]:
    """Seconds to sleep after HTTP 418 export-ban (``POST /api/exports``)."""
    msg = str(exc)
    if "418" not in msg and "banned" not in msg.lower():
        return None
    try:
        idx = msg.index("{")
        body = json.loads(msg[idx:])
        if isinstance(body, dict):
            w = body.get("wait_until_seconds")
            if w is not None:
                return max(1.0, float(w))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    m = re.search(r"wait\s+(\d+)\s+seconds", msg, re.I)
    if m:
        return max(1.0, float(m.group(1)))
    return 12.0


def create_export(client: VariClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/exports — returns {id, status, created_at}. Retries on timeout and 418 ban."""
    timeout_s = int(os.getenv("EXPORT_CREATE_TIMEOUT_S", "60"))
    max_attempts = int(os.getenv("EXPORT_CREATE_MAX_ATTEMPTS", "5"))
    last_err: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            resp = client.request_json(
                "POST",
                "/api/exports",
                json_body=payload,
                timeout_s=timeout_s,
                retries=0,
            )
            if not isinstance(resp, dict):
                raise TypeError(f"Expected dict from POST /api/exports, got {type(resp).__name__}")
            return resp
        except VariUnexpectedResponse as e:
            last_err = e
            wait_s = _parse_export_ban_wait_seconds(e)
            if wait_s is not None and attempt < max_attempts - 1:
                time.sleep(wait_s + 0.5)
                continue
            raise
        except Exception as e:
            last_err = e
            if _looks_like_timeout(e) and attempt < max_attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 15.0))
                continue
            raise

    assert last_err is not None
    raise last_err


def get_export(client: VariClient, export_id: str) -> Dict[str, Any]:
    """GET /api/exports/{id} — poll until status is no longer pending."""
    resp = client.request_json("GET", f"/api/exports/{export_id}")
    if not isinstance(resp, dict):
        raise TypeError(f"Expected dict from GET /api/exports/{export_id}, got {type(resp).__name__}")
    return resp


def _download_url_from_export(export: Dict[str, Any]) -> Optional[str]:
    for key in ("download_url", "url", "file_url", "presigned_url", "signed_url"):
        v = export.get(key)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()
    return None


def download_export_file(
    client: VariClient,
    *,
    export_id: str,
    export: Dict[str, Any],
    out_path: Path,
) -> Path:
    """Download completed export CSV (presigned URL or GET /api/exports/{id}/download)."""
    url = _download_url_from_export(export)
    if url:
        req_kw: Dict[str, Any] = {
            "method": "GET",
            "url": url,
            "timeout": client.timeout_s,
        }
        if client._proxies:
            req_kw["proxies"] = client._proxies
        resp = client.session.request(**req_kw)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Download failed ({resp.status_code}): {resp.text[:500]}")
        out_path.write_bytes(resp.content)
        return out_path

    path = f"/api/exports/{export_id}/download"
    url = f"{client.base_url}{path}"
    req_kw = {
        "method": "GET",
        "url": url,
        "headers": client._headers(),
        "cookies": client._cookies(),
        "timeout": client.timeout_s,
    }
    if client._proxies:
        req_kw["proxies"] = client._proxies
    resp = client.session.request(**req_kw)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"Download failed ({resp.status_code}): {resp.text[:500]}")
    out_path.write_bytes(resp.content)
    return out_path


def parse_since_sgt(value: str, *, default_date: datetime) -> datetime:
    """
    Parse a Singapore-time cutoff for post-download row filtering.

    Formats:
      - ``21:00`` / ``9pm`` → that clock time on ``default_date``'s SGT calendar day
      - ``2026-06-08T21:00`` / ``2026-06-08 21:00`` → explicit SGT datetime
    """
    raw = value.strip().lower().replace("sgt", "").strip()
    if raw in ("9pm", "21:00", "21:00:00"):
        d = default_date.astimezone(SGT)
        return datetime(d.year, d.month, d.day, 21, 0, 0, tzinfo=SGT)

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(raw, fmt)
            return naive.replace(tzinfo=SGT)
        except ValueError:
            continue
    raise ValueError(f"Could not parse --since-sgt value: {value!r}")


def filter_csv_since(
    csv_path: Path,
    *,
    cutoff: datetime,
    timestamp_col: str = "created_at",
) -> Tuple[int, int, datetime]:
    """Keep rows with ``timestamp_col`` >= ``cutoff`` (UTC-aware). Overwrites csv_path."""
    cutoff_utc = cutoff.astimezone(timezone.utc)
    text = csv_path.read_text(encoding="utf-8")
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"{csv_path}: missing CSV header")
    if timestamp_col not in reader.fieldnames:
        raise ValueError(f"{csv_path}: column {timestamp_col!r} not found")

    rows = list(reader)
    kept_rows: List[Dict[str, str]] = []
    dropped = 0
    for row in rows:
        raw = (row.get(timestamp_col) or "").strip()
        if not raw:
            dropped += 1
            continue
        if _parse_iso(raw) >= cutoff_utc:
            kept_rows.append(row)
        else:
            dropped += 1

    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(kept_rows)
    csv_path.write_text(out.getvalue(), encoding="utf-8")
    return len(kept_rows), dropped, cutoff_utc


def filter_csv_by_max_age(
    csv_path: Path,
    *,
    max_age_hours: float,
    timestamp_col: str = "created_at",
) -> Tuple[int, int, datetime, datetime]:
    """
    Drop rows older than ``max_age_hours`` before the latest ``timestamp_col`` value.

    Overwrites ``csv_path`` in place. Returns (kept, dropped, latest_ts, cutoff_ts).
    """
    text = csv_path.read_text(encoding="utf-8")
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise ValueError(f"{csv_path}: missing CSV header")
    if timestamp_col not in reader.fieldnames:
        raise ValueError(f"{csv_path}: column {timestamp_col!r} not found")

    rows = list(reader)
    if not rows:
        return 0, 0, datetime.now(timezone.utc), datetime.now(timezone.utc)

    parsed: List[Tuple[datetime, Dict[str, str]]] = []
    for row in rows:
        raw = (row.get(timestamp_col) or "").strip()
        if not raw:
            continue
        parsed.append((_parse_iso(raw), row))

    if not parsed:
        return 0, len(rows), datetime.now(timezone.utc), datetime.now(timezone.utc)

    latest_ts = max(ts for ts, _ in parsed)
    cutoff_ts = latest_ts - timedelta(hours=float(max_age_hours))
    kept_rows = [row for ts, row in parsed if ts >= cutoff_ts]
    dropped = len(parsed) - len(kept_rows)

    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(kept_rows)
    csv_path.write_text(out.getvalue(), encoding="utf-8")
    return len(kept_rows), dropped, latest_ts, cutoff_ts


def poll_export(
    client: VariClient,
    export_id: str,
    *,
    poll_interval_s: float = 2.0,
    timeout_s: float = 120.0,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_export(client, export_id)
        status = str(last.get("status") or "").strip().lower()
        if status in ("completed", "complete", "ready", "done", "success"):
            return last
        if status in ("failed", "error", "cancelled", "canceled"):
            raise RuntimeError(f"Export {export_id} ended with status={status!r}: {json.dumps(last)}")
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"Export {export_id} still pending after {timeout_s}s; last={json.dumps(last, default=str)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Request a Vari Omni export (POST /api/exports) and save RealizedPNL_*.csv or Trades_*.csv."
    )
    ap.add_argument(
        "--resource",
        default="transfers",
        choices=["transfers", "trades"],
        help='Export resource: transfers → RealizedPNL_*.csv; trades → Trades_*.csv.',
    )
    ap.add_argument(
        "--transfer-type",
        action="append",
        dest="transfer_types",
        default=None,
        help='Filter transfer_types (repeatable). Default: realized_pnl.',
    )
    ap.add_argument(
        "--gte",
        dest="created_at_gte",
        default=None,
        help='created_at_gte ISO timestamp (e.g. "2026-06-07T16:00:00.000Z").',
    )
    ap.add_argument(
        "--lte",
        dest="created_at_lte",
        default=None,
        help="created_at_lte ISO timestamp (default: now UTC).",
    )
    ap.add_argument(
        "--window",
        choices=["24h", "7d"],
        default=None,
        help="Preset lookback from --lte (filename suffix 24h or 7d). Overrides --days/--gte.",
    )
    ap.add_argument(
        "--days",
        type=float,
        default=None,
        help="If --gte/--window omitted, use this many days before --lte (default: 2).",
    )
    ap.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between status polls.")
    ap.add_argument("--timeout", type=float, default=120.0, help="Max seconds to wait for export completion.")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Save completed CSV here (default: RealizedPNL_24h.csv / Trades_3jun-6jun.csv etc.).",
    )
    ap.add_argument(
        "--no-poll",
        action="store_true",
        help="Only POST /api/exports and print the pending job JSON.",
    )
    ap.add_argument("--json", action="store_true", help="Print full result JSON to stdout.")
    ap.add_argument(
        "--max-age-hours",
        type=float,
        default=None,
        help="After download, drop CSV rows older than this many hours before the latest created_at.",
    )
    ap.add_argument(
        "--since-sgt",
        default=None,
        metavar="TIME",
        help='After download, keep rows from this SGT time onwards (e.g. "21:00", "9pm", "2026-06-08 21:00"). '
        "Bare time uses the export window start date in SGT.",
    )
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    created_at_lte = args.created_at_lte or _iso_z(now)
    lte_dt = _parse_iso(created_at_lte)
    if args.window == "24h":
        created_at_gte = _iso_z(lte_dt - timedelta(hours=24))
    elif args.window == "7d":
        created_at_gte = _iso_z(lte_dt - timedelta(days=7))
    elif args.created_at_gte:
        created_at_gte = args.created_at_gte
    else:
        days = 2.0 if args.days is None else float(args.days)
        created_at_gte = _iso_z(lte_dt - timedelta(days=days))

    gte_dt = _parse_iso(created_at_gte)

    transfer_types: Optional[List[str]] = None
    if args.resource == "transfers":
        transfer_types = args.transfer_types if args.transfer_types else ["realized_pnl"]

    payload = build_export_payload(
        resource=args.resource,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        transfer_types=transfer_types,
    )

    cfg = load_config()
    client = VariClient(
        base_url=cfg.base_url,
        auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
    )

    created = create_export(client, payload)
    export_id = str(created.get("id") or "").strip()
    if not export_id:
        print(json.dumps({"error": "missing export id", "response": created}, indent=2), file=sys.stderr)
        return 1

    if args.no_poll:
        out = {"payload": payload, "created": created}
        print(json.dumps(out, indent=2, default=str))
        return 0

    completed = poll_export(
        client,
        export_id,
        poll_interval_s=args.poll_interval,
        timeout_s=args.timeout,
    )

    out_path = args.out or Path(
        export_filename(
            resource=args.resource,
            created_at_gte=gte_dt,
            created_at_lte=lte_dt,
            window=args.window,
        )
    )
    saved_path = download_export_file(
        client,
        export_id=export_id,
        export=completed,
        out_path=out_path,
    )
    saved = str(saved_path)

    age_filter: Optional[Dict[str, Any]] = None
    if args.max_age_hours is not None and args.max_age_hours > 0:
        kept, dropped, latest_ts, cutoff_ts = filter_csv_by_max_age(
            saved_path,
            max_age_hours=float(args.max_age_hours),
        )
        age_filter = {
            "max_age_hours": float(args.max_age_hours),
            "kept_rows": kept,
            "dropped_rows": dropped,
            "latest_created_at": _iso_z(latest_ts),
            "cutoff_created_at": _iso_z(cutoff_ts),
        }

    since_filter: Optional[Dict[str, Any]] = None
    if args.since_sgt:
        since_sgt = parse_since_sgt(args.since_sgt, default_date=gte_dt)
        kept, dropped, cutoff_utc = filter_csv_since(saved_path, cutoff=since_sgt)
        since_filter = {
            "since_sgt": since_sgt.isoformat(),
            "since_utc": _iso_z(cutoff_utc),
            "kept_rows": kept,
            "dropped_rows": dropped,
        }

    result: Dict[str, Any] = {
        "payload": payload,
        "created": created,
        "completed": completed,
        "saved_to": saved,
    }
    if age_filter is not None:
        result["age_filter"] = age_filter
    if since_filter is not None:
        result["since_filter"] = since_filter
    if args.json or saved is None:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Export {export_id} saved to {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
