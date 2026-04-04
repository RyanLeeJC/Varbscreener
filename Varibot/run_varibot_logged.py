#!/usr/bin/env python3
"""
Run varibot.py and mirror stdout to the terminal while keeping a rolling text log
(last 1000 lines only) for post-mortem review.

Usage (same flags as varibot.py):
  cd .../Varibot && python3 run_varibot_logged.py --once
  python3 run_varibot_logged.py --live

Log path: VARIBOT_LOG_PATH env, else ./varibot_output.txt next to this script.

Sizing: with no --usd / --im-target-pct, varibot uses default IM%% target (see
  multimarketorder.DEFAULT_IM_TARGET_PCT). Override with --im-target-pct PCT or --usd.

Close-all: closeallpositions.py retries close_all with +0.05% notional slippage per attempt
  (see SLIPPAGE_RETRY_INCREMENT / MAX_CLOSEALL_LIVE_ATTEMPTS) until GET /api/positions is flat.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

_SGT = ZoneInfo("Asia/Singapore")


def _sgt_stamp() -> str:
    """Wall time in Asia/Singapore with millisecond resolution (for accurate deltas between lines)."""
    now = datetime.now(_SGT)
    ms = now.microsecond // 1000
    return f"{now.strftime('%H:%M:%S')}.{ms:03d} {now.day} {now.strftime('%b')}"


def _sgt_prefix() -> str:
    return f"[{_sgt_stamp()}] "

_MAX_LINES = 1000


def _default_log_path() -> str:
    override = os.getenv("VARIBOT_LOG_PATH", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "varibot_output.txt")


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    varibot_py = os.path.join(here, "varibot.py")
    if not os.path.isfile(varibot_py):
        print(f"run_varibot_logged: varibot.py not found at {varibot_py}", file=sys.stderr)
        return 2

    log_path = _default_log_path()
    child_args = [sys.executable, "-u", varibot_py] + sys.argv[1:]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    lines: deque[str] = deque(maxlen=_MAX_LINES)
    lines_lock = threading.Lock()
    intro = f"[run_varibot_logged] logging last {_MAX_LINES} lines to: {log_path}\n"
    banner = f"[run_varibot_logged] start log={log_path} pid={os.getpid()}\n"
    lines.append(f"{_sgt_prefix()}{intro}")
    lines.append(f"{_sgt_prefix()}{banner}")

    def _write_snap_to_disk(snap: list[str]) -> None:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(snap)
                f.flush()
        except OSError as e:
            print(f"\n[run_varibot_logged] log write failed: {e}", file=sys.stderr, flush=True)

    def _write_log_sync() -> None:
        with lines_lock:
            snap = list(lines)
        _write_snap_to_disk(snap)

    # Disk writes must not run on the read loop: rewriting the rolling log after every line
    # blocked the next pipe read and made later lines look ~tens of seconds “late” vs wall time.
    _FLUSH = object()
    _STOP = object()
    log_q: queue.Queue[object] = queue.Queue()

    def _log_writer() -> None:
        while True:
            token = log_q.get()
            if token is _STOP:
                _write_log_sync()
                return
            if token is not _FLUSH:
                continue
            time.sleep(0.05)
            while True:
                try:
                    t2 = log_q.get_nowait()
                    if t2 is _STOP:
                        _write_log_sync()
                        return
                except queue.Empty:
                    break
            _write_log_sync()

    writer = threading.Thread(target=_log_writer, name="varibot-log-writer", daemon=True)
    writer.start()

    def _request_log_flush() -> None:
        log_q.put(_FLUSH)

    sys.stdout.writelines(lines)
    sys.stdout.flush()
    _write_log_sync()

    proc = subprocess.Popen(
        child_args,
        cwd=here,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            stamped = f"{_sgt_prefix()}{line}"
            with lines_lock:
                lines.append(stamped)
            sys.stdout.write(stamped)
            sys.stdout.flush()
            _request_log_flush()
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait()
        log_q.put(_STOP)
        writer.join(timeout=30.0)
        if writer.is_alive():
            _write_log_sync()

    return int(rc) if rc is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
