#!/usr/bin/env python3
"""
Fetch Render service logs for a time window (default: last 24 hours).

Uses GET /v1/logs (not /v1/services/{id}/logs). Loads Render_API_KEY from
Varibot/.env. Paginates at 100 lines/page; retries 429s and paces requests
to avoid rate limits. Streams to disk and supports resume.

Example:
  python3 Varibot/fetch_render_logs.py -o logs.txt
  python3 Varibot/fetch_render_logs.py --hours 24 -o logs.txt --resume
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from dotenv import load_dotenv

API_BASE = "https://api.render.com/v1"
DEFAULT_SERVICE_ID = "srv-d86tvu3eo5us73ccj3jg"
PAGE_LIMIT = 100
_ENV_PATH = Path(__file__).resolve().parent / ".env"
DEFAULT_PAGE_DELAY_S = 1.25
DEFAULT_CHUNK_HOURS = 4.0
DEFAULT_CHUNK_PAUSE_S = 5.0
MAX_HTTP_RETRIES = 12


def _load_local_env() -> None:
    if _ENV_PATH.is_file():
        load_dotenv(_ENV_PATH)


def _api_key() -> str:
    _load_local_env()
    key = os.environ.get("RENDER_API_KEY") or os.environ.get("Render_API_KEY")
    if not key or not key.strip():
        print(
            f"Set RENDER_API_KEY or Render_API_KEY in {_ENV_PATH} "
            "or export it in the shell.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key.strip()


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logs_url(
    *,
    owner_id: str,
    service_id: str,
    start: str,
    end: str,
    log_types: Optional[List[str]],
) -> str:
    pairs: List[tuple[str, str]] = [
        ("ownerId", owner_id),
        ("resource", service_id),
        ("startTime", start),
        ("endTime", end),
        ("direction", "forward"),
        ("limit", str(PAGE_LIMIT)),
    ]
    if log_types:
        pairs.extend(("type", t) for t in log_types)
    return f"{API_BASE}/logs?{urllib.parse.urlencode(pairs)}"


def _format_log_line(entry: Dict[str, Any]) -> str:
    ts = entry.get("timestamp") or ""
    msg = entry.get("message") or ""
    if isinstance(msg, str):
        return f"{ts}\t{msg.rstrip()}"
    return f"{ts}\t{json.dumps(msg, ensure_ascii=False)}"


def _retry_after_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    raw = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return min(120.0, 5.0 * (2**attempt))


def _get_json(
    url: str,
    *,
    api_key: str,
    page_delay_s: float,
) -> Any:
    for attempt in range(MAX_HTTP_RETRIES):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.load(resp)
            if page_delay_s > 0:
                time.sleep(page_delay_s)
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < MAX_HTTP_RETRIES - 1:
                wait = _retry_after_seconds(exc, attempt)
                print(
                    f"Rate limited (429); waiting {wait:.0f}s "
                    f"(retry {attempt + 1}/{MAX_HTTP_RETRIES - 1})…",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            print(f"HTTP {exc.code}: {body}", file=sys.stderr)
            raise SystemExit(1) from exc
    raise SystemExit("exhausted retries")  # pragma: no cover


def _owner_id_for_service(service_id: str, *, api_key: str, page_delay_s: float) -> str:
    data = _get_json(
        f"{API_BASE}/services/{service_id}",
        api_key=api_key,
        page_delay_s=0,
    )
    owner = data.get("ownerId")
    if isinstance(owner, str) and owner:
        return owner
    print(f"Could not read ownerId from service {service_id}", file=sys.stderr)
    raise SystemExit(1)


def _progress_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".progress.json")


def _load_progress(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_progress(path: Path, state: Dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _time_chunks(
    start: datetime, end: datetime, chunk_hours: float
) -> List[Tuple[datetime, datetime]]:
    if chunk_hours <= 0:
        return [(start, end)]
    chunks: List[Tuple[datetime, datetime]] = []
    cursor = start
    delta = timedelta(hours=chunk_hours)
    while cursor < end:
        chunk_end = min(cursor + delta, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def _fetch_range(
    *,
    owner_id: str,
    service_id: str,
    range_start: str,
    range_end: str,
    log_types: Optional[List[str]],
    api_key: str,
    page_delay_s: float,
    out: TextIO,
    resume_cursor: Optional[Tuple[str, str]],
    progress_path: Path,
    progress_meta: Dict[str, Any],
) -> int:
    """Fetch one [start, end] window; return lines written."""
    if resume_cursor:
        start_s, end_s = resume_cursor
    else:
        start_s, end_s = range_start, range_end

    url = _logs_url(
        owner_id=owner_id,
        service_id=service_id,
        start=start_s,
        end=end_s,
        log_types=log_types,
    )
    page = _get_json(url, api_key=api_key, page_delay_s=0)
    written = 0
    pages = 0

    while True:
        pages += 1
        batch = page.get("logs") or []
        for entry in batch:
            if isinstance(entry, dict):
                out.write(_format_log_line(entry) + "\n")
                written += 1

        progress_meta["lines"] = progress_meta.get("lines", 0) + len(batch)
        progress_meta["nextStartTime"] = page.get("nextStartTime")
        progress_meta["nextEndTime"] = page.get("nextEndTime")
        _save_progress(progress_path, progress_meta)

        if pages % 25 == 0:
            print(
                f"  … {progress_meta['lines']} lines, {pages} pages "
                f"in current chunk",
                file=sys.stderr,
            )

        if not page.get("hasMore"):
            progress_meta.pop("nextStartTime", None)
            progress_meta.pop("nextEndTime", None)
            break

        nstart = page.get("nextStartTime")
        nend = page.get("nextEndTime")
        if not nstart or not nend:
            break

        url = _logs_url(
            owner_id=owner_id,
            service_id=service_id,
            start=nstart,
            end=nend,
            log_types=log_types,
        )
        page = _get_json(url, api_key=api_key, page_delay_s=page_delay_s)

    return written


def fetch_logs_to_file(
    *,
    service_id: str,
    owner_id: str,
    start: datetime,
    end: datetime,
    output: Path,
    api_key: str,
    log_types: Optional[List[str]],
    page_delay_s: float,
    chunk_hours: float,
    chunk_pause_s: float,
    resume: bool,
) -> int:
    progress_path = _progress_path(output)
    saved: Optional[Dict[str, Any]] = None
    resume_cursor: Optional[Tuple[str, str]] = None
    skip_before: Optional[str] = None
    append = False

    window_start = _iso_utc(start)
    window_end = _iso_utc(end)

    if resume:
        saved = _load_progress(progress_path)
        if (
            saved
            and saved.get("service_id") == service_id
            and saved.get("window_start") == window_start
            and saved.get("window_end") == window_end
        ):
            append = output.is_file()
            skip_before = saved.get("last_completed_chunk_end")
            if saved.get("nextStartTime") and saved.get("nextEndTime"):
                resume_cursor = (
                    str(saved["nextStartTime"]),
                    str(saved["nextEndTime"]),
                )
            print(
                f"Resuming ({saved.get('lines', 0)} lines on disk"
                + (f", cursor {resume_cursor[0]}" if resume_cursor else "")
                + ")",
                file=sys.stderr,
            )
        elif saved:
            print("Progress file mismatch; starting fresh.", file=sys.stderr)
            saved = None

    progress_meta: Dict[str, Any] = {
        "service_id": service_id,
        "owner_id": owner_id,
        "window_start": window_start,
        "window_end": window_end,
        "log_types": log_types,
        "lines": int(saved.get("lines", 0)) if saved and append else 0,
        "last_completed_chunk_end": skip_before,
    }
    _save_progress(progress_path, progress_meta)

    chunks = _time_chunks(start, end, chunk_hours)
    total_written = 0
    mode = "a" if append else "w"
    cursor_for_chunk: Optional[Tuple[str, str]] = resume_cursor

    with output.open(mode, encoding="utf-8") as out:
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            cs = _iso_utc(chunk_start)
            ce = _iso_utc(chunk_end)

            if skip_before and ce <= skip_before:
                continue

            if len(chunks) > 1:
                print(
                    f"Chunk {i + 1}/{len(chunks)}: {cs} → {ce}",
                    file=sys.stderr,
                )

            progress_meta["chunk_start"] = cs
            progress_meta["chunk_end"] = ce
            n = _fetch_range(
                owner_id=owner_id,
                service_id=service_id,
                range_start=cs,
                range_end=ce,
                log_types=log_types,
                api_key=api_key,
                page_delay_s=page_delay_s,
                out=out,
                resume_cursor=cursor_for_chunk,
                progress_path=progress_path,
                progress_meta=progress_meta,
            )
            cursor_for_chunk = None
            progress_meta["last_completed_chunk_end"] = ce
            total_written += n
            out.flush()

            if i < len(chunks) - 1 and chunk_pause_s > 0:
                print(
                    f"Chunk done ({n} lines); pausing {chunk_pause_s:.0f}s…",
                    file=sys.stderr,
                )
                time.sleep(chunk_pause_s)

    if progress_path.is_file():
        progress_path.unlink()

    return total_written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Render service logs for a time window.",
    )
    parser.add_argument("--service-id", default=DEFAULT_SERVICE_ID)
    parser.add_argument(
        "--owner-id",
        default="",
        help="Workspace owner id (tea-...). Resolved from service if omitted.",
    )
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("-o", "--output", default="logs.txt")
    parser.add_argument(
        "--type",
        action="append",
        dest="log_types",
        metavar="TYPE",
        help="Log type filter (repeatable): app, request, build",
    )
    parser.add_argument(
        "--all-types",
        action="store_true",
        help="Include build/request logs (more API pages; default is app only)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_PAGE_DELAY_S,
        metavar="SECONDS",
        help=f"Pause after each page (default {DEFAULT_PAGE_DELAY_S})",
    )
    parser.add_argument(
        "--chunk-hours",
        type=float,
        default=DEFAULT_CHUNK_HOURS,
        metavar="HOURS",
        help=f"Split window into chunks (default {DEFAULT_CHUNK_HOURS}; 0=disabled)",
    )
    parser.add_argument(
        "--chunk-pause",
        type=float,
        default=DEFAULT_CHUNK_PAUSE_S,
        metavar="SECONDS",
        help=f"Pause between chunks (default {DEFAULT_CHUNK_PAUSE_S})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from logs.txt.progress.json after a 429/interrupt",
    )
    args = parser.parse_args()

    log_types: Optional[List[str]]
    if args.all_types:
        log_types = args.log_types or None
    elif args.log_types:
        log_types = args.log_types
    else:
        log_types = ["app"]

    api_key = _api_key()
    owner_id = args.owner_id.strip() or _owner_id_for_service(
        args.service_id, api_key=api_key, page_delay_s=0
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    output = Path(args.output)

    est_pages = max(1, int(args.hours * 80))  # rough: ~80 pages/hour at busy bot
    est_min = est_pages * args.delay / 60.0
    print(
        f"Service {args.service_id} | owner {owner_id}\n"
        f"Window {_iso_utc(start)} → {_iso_utc(end)} | types={log_types or 'all'}\n"
        f"Pacing: {args.delay}s/page, chunks={args.chunk_hours}h "
        f"(pause {args.chunk_pause}s) | ~{est_min:.0f} min estimated",
        file=sys.stderr,
    )

    total = fetch_logs_to_file(
        service_id=args.service_id,
        owner_id=owner_id,
        start=start,
        end=end,
        output=output,
        api_key=api_key,
        log_types=log_types,
        page_delay_s=args.delay,
        chunk_hours=args.chunk_hours,
        chunk_pause_s=args.chunk_pause,
        resume=args.resume,
    )

    print(f"Wrote {total} new lines to {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
