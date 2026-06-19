from __future__ import annotations

"""
Varibot orchestrator — Vari exchange workflow:

  Auth (validate_vr_token) -> every T minutes: portfolio snapshot ->
  if positions -> optional IM rebalance ->
  if flat -> listing snapshot -> strategy (VARIBOT_STRATEGY) -> multimarketorder

Run from the Varibot directory:

  cd .../Varibot && python3 varibot.py
  python3 varibot.py --live
  python3 varibot.py --usd 20

Set VARIBOT_STRATEGY to a module under strategy/ (e.g. funding_arb.py).
"""

import argparse
import http.server
import json
import math
import os
import queue
import re
import importlib
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

# Imports assume sibling scripts + variationalbot live under this directory.
_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
# Strategy JSON under Varibot (no ``Vari Listings`` pipeline).
_STRATEGY_LISTING_SNAPSHOT_JSON = os.path.join(_VARIBOT_DIR, "strategy_listing_snapshot.json")
_STRATEGY_MARKETSTATE_JSON = os.path.join(_VARIBOT_DIR, "strategy_marketstate.json")
_DEFAULT_MARKETSTATE_JSON = _STRATEGY_MARKETSTATE_JSON
_POSITION_LATCH_PATH = os.path.join(_VARIBOT_DIR, ".varibot_position_latch.json")

# Check interval (minutes) between cycles/sessions when --period-min is not provided.
CHECK_INTERVAL_MIN: int = 1

# --- User-tunable settings (surface here for quick edits) ---
PM_REFILL_DEFAULT_ON: bool = True

_TIME_IN_POSITION_POST_CLOSE_SLEEP_S: float = 15.0

Strategy: str = os.getenv("VARIBOT_STRATEGY", "").strip()

DEFAULT_MAX_TICKER_ENTRIES: int = 40

# Rolling log (wrapper mode): varibot.py can self-wrap to prefix lines and keep a rolling logfile,
# so you can run just `python3 varibot.py --live` and still get the run_varibot_logged behavior.
_VARIBOT_LOG_MAX_LINES: int = 1000
_VARIBOT_WRAPPED_ENV: str = "VARIBOT_WRAPPED"

# Post-multimarket verification: /api/positions can lag behind fills by a second or two.
POST_MULTIMARKET_POSITIONS_MAX_WAIT_S: float = 2.0
POST_MULTIMARKET_POSITIONS_POLL_S: float = 0.5

# Reduce-only closes (portfolio manager, time-kill; _close_reduce_only_with_slippage_steps).
# Same defaults as multimarketorder.py (_DEFAULT_MAX_SLIPPAGE / _SLIPPAGE_RETRY_INCREMENT / _MAX_LIVE_ATTEMPTS).
# Default max slippage when MAX_SLIPPAGE env is unset (fraction of notional).
_DEFAULT_MAX_SLIPPAGE: float = 0.001

# Retry behavior for reduce-only closes (same values as multimarketorder.py).
_SLIPPAGE_RETRY_INCREMENT: float = 0.0005
_MAX_LIVE_ATTEMPTS: int = 6

# Local debugging: write the strategy output we *actually used* to Varibot/strategy_output.json
# Set VARIBOT_WRITE_STRATEGY_OUTPUT=1 to enable (local only; ignored on Railway).
_VARIBOT_WRITE_STRATEGY_OUTPUT_ENV: str = "VARIBOT_WRITE_STRATEGY_OUTPUT"

_MONTH_ABBR_TO_NUM = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from check_portfolio_stats import _build_out_dict  # noqa: E402
from portfolio_rebalance import rebalance_portfolio  # noqa: E402
from positions import _instrument_label  # noqa: E402
from validate_vr_token import validate_vr_token  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402
from variationalbot.vari.endpoints import Instrument, format_qty_for_indicative_api  # noqa: E402
from variationalbot.util.telegram_notify import maybe_notify_vari_portfolio_auth_failure  # noqa: E402

from multimarketorder import (  # noqa: E402
    DEFAULT_IM_TARGET_PCT,
    DEFAULT_LEVERAGE as MULTIMARKET_DEFAULT_LEVERAGE,
    MULTIMARKET_LAST_RESULT_JSON,
    USD_NOTIONAL_ROUND_STEP,
    _order_response_rejected,
)
from portfolio_manager_pairs import (
    PAIR_TP_THRESHOLD_PCT_DEFAULT as PM_PAIR_TP_THRESHOLD_PCT_DEFAULT,
)  # noqa: E402

_strategy_module_cache: Dict[str, Any] = {}


def _load_strategy_module(strategy_key: str) -> Any:
    key = _strategy_key_normalized(strategy_key)
    if not key:
        raise RuntimeError("VARIBOT_STRATEGY is required (e.g. funding_arb.py under strategy/).")
    if key in _strategy_module_cache:
        return _strategy_module_cache[key]
    mod_name = key if key.endswith(".py") else f"{key}.py"
    if mod_name.endswith(".py"):
        mod_name = mod_name[:-3]
    mod = importlib.import_module(f"strategy.{mod_name}")
    _strategy_module_cache[key] = mod
    return mod


def _strategy_key_normalized(strategy: str) -> str:
    k = (strategy or "").strip().lower()
    if k.endswith(".py"):
        k = k[:-3]
    return k


def _resolve_im_target_pct_for_multimarket(*, args_im_target_pct: Optional[float]) -> float:
    """Use --im-target-pct when set, else multimarketorder.DEFAULT_IM_TARGET_PCT."""
    return float(args_im_target_pct) if args_im_target_pct is not None else float(DEFAULT_IM_TARGET_PCT)


def _multimarket_effective_leverage() -> int:
    v = (os.environ.get("DEFAULT_LEVERAGE", "") or "").strip()
    if v:
        try:
            return max(1, int(float(v)))
        except Exception:
            pass
    return int(MULTIMARKET_DEFAULT_LEVERAGE)


def _log(msg: str) -> None:
    print(msg, flush=True)


_HEALTH_LISTENER_STARTED = False
_HEALTH_LISTENER_LOCK = threading.Lock()


def _maybe_start_render_health_listener() -> None:
    """
    When PORT is set (Render web/private services), bind 0.0.0.0:PORT for deploy health checks.
    Called from __main__ before the log-wrapper spawns a child so Render's port scan succeeds early.
    Set VARIBOT_HEALTH_LISTENER=0 to disable. Background workers on Render omit PORT.
    """
    global _HEALTH_LISTENER_STARTED
    with _HEALTH_LISTENER_LOCK:
        if _HEALTH_LISTENER_STARTED:
            return
        flag = (os.getenv("VARIBOT_HEALTH_LISTENER") or "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return
        raw = (os.getenv("PORT") or "").strip()
        if not raw:
            return
        try:
            port = int(raw)
        except ValueError:
            _log(f"health: skip — invalid PORT={raw!r}")
            return
        if port <= 0 or port > 65535:
            _log(f"health: skip — PORT out of range ({port})")
            return

        class _HealthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] in ("/", "/health", "/healthz"):
                    body = b"ok\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        try:
            httpd = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
        except OSError as e:
            _log(f"health: bind failed on 0.0.0.0:{port} ({type(e).__name__}: {e})")
            return

        threading.Thread(
            target=httpd.serve_forever,
            name="varibot-health",
            daemon=True,
        ).start()
        _HEALTH_LISTENER_STARTED = True
        _log(f"health: listening on 0.0.0.0:{port} (/ /health /healthz)")


def _fmt_portfolio_snapshot_line(out: Dict[str, Any]) -> str:
    """Port Value, uPNL, IM%, MM% — two decimals (keys from _build_out_dict)."""

    def f2(key: str) -> str:
        v = out.get(key)
        if v is None:
            return "—"
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return "—"

    return (
        f"Port Value={f2('portfolio_value_usd')} "
        f"Port uPNL={f2('unrealized_pnl_usd')} "
        f"IM%={f2('im_usage_pct')} "
        f"MM%={f2('mm_usage_pct')}"
    )


def _should_write_strategy_output() -> bool:
    v = (os.getenv(_VARIBOT_WRITE_STRATEGY_OUTPUT_ENV, "") or "").strip().lower()
    if v not in ("1", "true", "yes", "y", "on"):
        return False
    # Railway services should not write these local debug artifacts.
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID"):
        return False
    return True


def _sgt_stamp_ms() -> str:
    now = datetime.now(ZoneInfo("Asia/Singapore"))
    ms = now.microsecond // 1000
    return f"{now.strftime('%H:%M:%S')}.{ms:03d} {now.day} {now.strftime('%b')}"


def _sgt_prefix() -> str:
    return f"[{_sgt_stamp_ms()}] "


def _default_roll_log_path() -> str:
    override = os.getenv("VARIBOT_LOG_PATH", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "varibot_output.txt")


def _run_self_wrapped() -> int:
    """
    Wrapper mode: spawn this script as a child, stamp each output line with SGT time,
    and keep a rolling logfile of the last N lines (like run_varibot_logged.py used to do).
    """
    if os.getenv(_VARIBOT_WRAPPED_ENV, "").strip() == "1":
        return 0

    log_path = _default_roll_log_path()
    child_args = [sys.executable, "-u", os.path.abspath(__file__)] + sys.argv[1:]
    env = os.environ.copy()
    env[_VARIBOT_WRAPPED_ENV] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")

    lines: deque[str] = deque(maxlen=_VARIBOT_LOG_MAX_LINES)
    lines_lock = threading.Lock()
    intro = f"[varibot] logging last {_VARIBOT_LOG_MAX_LINES} lines to: {log_path}\n"
    banner = f"[varibot] start log={log_path} pid={os.getpid()}\n"
    lines.append(f"{_sgt_prefix()}{intro}")
    lines.append(f"{_sgt_prefix()}{banner}")

    def _write_snap_to_disk(snap: list[str]) -> None:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(snap)
                f.flush()
        except OSError as e:
            print(f"\n[varibot] log write failed: {e}", file=sys.stderr, flush=True)

    def _write_log_sync() -> None:
        with lines_lock:
            snap = list(lines)
        _write_snap_to_disk(snap)

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
        cwd=os.path.dirname(os.path.abspath(__file__)),
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


def _format_duration_s(secs: float) -> str:
    s = max(0, int(round(float(secs))))
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m{r}s"
    h, rem = divmod(s, 3600)
    m, r = divmod(rem, 60)
    return f"{h}h{m}m{r}s"


def _format_wake_at_sgt(delay_s: float) -> str:
    sgt = ZoneInfo("Asia/Singapore")
    now_sg = datetime.now(sgt)
    wake = datetime.fromtimestamp(time.time() + float(delay_s), tz=sgt)
    if wake.date() == now_sg.date():
        return wake.strftime("%H:%M:%S")
    return wake.strftime("%d %b %H:%M:%S")


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _position_qty(p: Dict[str, Any]) -> Optional[float]:
    for k in ("qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"):
        if k not in p:
            continue
        try:
            return float(p[k])
        except (TypeError, ValueError):
            continue
    pi = p.get("position_info")
    if isinstance(pi, dict) and "qty" in pi:
        try:
            return float(pi["qty"])
        except (TypeError, ValueError):
            pass
    return None


def _positions_notional_usd(positions_raw: Any) -> float:
    """
    Sum absolute USD notional across open positions (best-effort across schema variants).
    Used as TP-check denominator when present.
    """
    total = 0.0
    for p in _positions_list(positions_raw):
        # Common shapes: value / position_value / notional / usd_value, sometimes nested under position_info.
        v = None
        for k in ("value", "position_value", "notional", "notional_value", "usd_value"):
            if k in p and p.get(k) is not None:
                v = p.get(k)
                break
        if v is None:
            pi = p.get("position_info")
            if isinstance(pi, dict):
                v = pi.get("value") if pi.get("value") is not None else pi.get("position_value")
        try:
            if v is None:
                continue
            total += abs(float(v))
        except Exception:
            continue
    return float(total)


def has_open_positions(positions_raw: Any) -> bool:
    for p in _positions_list(positions_raw):
        q = _position_qty(p)
        if q is not None and abs(float(q)) > 1e-12:
            return True
    return False


def _oldest_position_opened_at_ts(positions_raw: Any) -> Optional[float]:
    """
    Best-effort: return unix ts for the oldest open position's opened_at.
    Observed live shape: position_info.opened_at = ISO-8601 string.
    """
    best: Optional[float] = None
    for p in _positions_list(positions_raw):
        pi = p.get("position_info")
        if not isinstance(pi, dict):
            continue
        ts = _parse_ts(pi.get("opened_at"))
        if ts is None:
            continue
        if best is None or ts < best:
            best = float(ts)
    return best


def _positions_time_kill_candidates(
    *,
    positions_raw: Any,
    now_ts: Optional[float],
    kill_after_s: float,
) -> List[Tuple[str, float, float]]:
    """
    Return [(sym, qty, age_s), ...] for positions whose opened_at age >= kill_after_s.

    qty is signed (same sign convention as positions); age_s is seconds since opened_at.
    """
    out: List[Tuple[str, float, float]] = []
    now = float(now_ts) if now_ts is not None else float(time.time())
    for p in _positions_list(positions_raw):
        q = _position_qty(p)
        if q is None or abs(float(q)) <= 1e-12:
            continue
        pi = p.get("position_info")
        if not isinstance(pi, dict):
            continue
        opened = _parse_ts(pi.get("opened_at"))
        if opened is None:
            continue
        age_s = max(0.0, now - float(opened))
        if age_s < float(kill_after_s):
            continue
        sym = _instrument_label(p).strip().upper()
        if not sym:
            continue
        out.append((sym, float(q), float(age_s)))
    # Close oldest first for determinism/log readability.
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def _parse_pct_str(v: Any) -> Optional[float]:
    """
    Parse strings like "1.23%" or numeric-ish values into a float percent (e.g. 1.23).
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _ticker_abs_7d_from_listing_json(listing_json: str) -> Dict[str, float]:
    """
    Best-effort map: ticker -> abs(7d change %), from strategy listing JSON (often only ``GRID_ASSET`` has fields).
    """
    try:
        with open(listing_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}

    listings = None
    if isinstance(payload, dict) and isinstance(payload.get("listings"), list):
        listings = payload.get("listings")
    elif isinstance(payload, list):
        listings = payload
    if not isinstance(listings, list):
        return {}

    out: Dict[str, float] = {}
    for row in listings:
        if not isinstance(row, dict):
            continue
        t = row.get("vari_ticker") or row.get("ticker") or row.get("symbol")
        if not t:
            continue
        sym = str(t).strip().upper()
        if not sym:
            continue
        chg7 = _parse_pct_str(row.get("price_change_7d_pct") or row.get("price_change_7d") or row.get("chg_7d_pct"))
        if chg7 is None:
            continue
        out[sym] = abs(float(chg7))
    return out


def _oldest_position_summary(positions_raw: Any, *, now_ts: Optional[float] = None) -> Optional[Tuple[str, float]]:
    """
    Return (symbol, age_s) for the oldest open position by position_info.opened_at.
    """
    now = float(now_ts) if now_ts is not None else float(time.time())
    best_sym: Optional[str] = None
    best_age: Optional[float] = None
    for p in _positions_list(positions_raw):
        q = _position_qty(p)
        if q is None or abs(float(q)) <= 1e-12:
            continue
        pi = p.get("position_info")
        if not isinstance(pi, dict):
            continue
        opened = _parse_ts(pi.get("opened_at"))
        if opened is None:
            continue
        age_s = max(0.0, now - float(opened))
        sym = _instrument_label(p).strip().upper()
        if not sym:
            continue
        if best_age is None or age_s > best_age:
            best_age = float(age_s)
            best_sym = sym
    if best_sym is None or best_age is None:
        return None
    return best_sym, best_age


def _log_post_multimarket_positions_tally(
    *,
    ep: VariEndpoints,
    longs: List[str],
    shorts: List[str],
) -> None:
    """
    GET /api/positions and compare to tickers we attempted to open (live only).

    The venue can take a moment to reflect the last fill(s), so we poll briefly and
    use the best snapshot (fewest missing/bad) to avoid false "missing" warnings.
    """
    exp_l = [str(t).strip().upper() for t in longs]
    exp_s = [str(t).strip().upper() for t in shorts]

    best: Optional[Tuple[Dict[str, float], List[str], List[str], List[str], List[str]]] = None
    best_score: Optional[int] = None
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        raw = ep.get_positions()
        by_ticker: Dict[str, float] = {}
        for p in _positions_list(raw):
            sym = _instrument_label(p).strip().upper()
            q = _position_qty(p)
            if sym and q is not None:
                by_ticker[sym] = float(q)

        ok_l: List[str] = []
        miss_l: List[str] = []
        bad_l: List[str] = []
        for u in exp_l:
            q = by_ticker.get(u)
            if q is None or abs(q) <= 1e-12:
                miss_l.append(u)
            elif q <= 0:
                bad_l.append(f"{u} qty={q}")
            else:
                ok_l.append(u)

        ok_s: List[str] = []
        miss_s: List[str] = []
        bad_s: List[str] = []
        for u in exp_s:
            q = by_ticker.get(u)
            if q is None or abs(q) <= 1e-12:
                miss_s.append(u)
            elif q >= 0:
                bad_s.append(f"{u} qty={q}")
            else:
                ok_s.append(u)

        score = (len(miss_l) + len(bad_l) + len(miss_s) + len(bad_s))
        if best is None or best_score is None or score < best_score:
            best = (by_ticker, ok_l, miss_l, ok_s, miss_s)
            best_score = score

        if not (miss_l or bad_l or miss_s or bad_s):
            break
        if time.monotonic() - start >= POST_MULTIMARKET_POSITIONS_MAX_WAIT_S:
            break
        time.sleep(POST_MULTIMARKET_POSITIONS_POLL_S)

    assert best is not None
    by_ticker, ok_l, miss_l, ok_s, miss_s = best
    # Recompute "bad" for final reporting from best snapshot.
    bad_l = [f"{u} qty={by_ticker.get(u)}" for u in exp_l if u in by_ticker and float(by_ticker[u]) <= 0]
    bad_s = [f"{u} qty={by_ticker.get(u)}" for u in exp_s if u in by_ticker and float(by_ticker[u]) >= 0]

    n_exp = len(longs) + len(shorts)
    n_ok = len(ok_l) + len(ok_s)
    _log(
        f"Post-multimarket GET /api/positions: longs {len(ok_l)}/{len(longs)} OK, "
        f"shorts {len(ok_s)}/{len(shorts)} OK (signed qty vs intent; {n_ok}/{n_exp} total)"
    )
    if miss_l:
        _log(f"  missing long (flat or absent): {', '.join(miss_l)}")
    if bad_l:
        _log(f"  long intent but qty not > 0: {', '.join(bad_l)}")
    if miss_s:
        _log(f"  missing short (flat or absent): {', '.join(miss_s)}")
    if bad_s:
        _log(f"  short intent but qty not < 0: {', '.join(bad_s)}")
    if not (miss_l or bad_l or miss_s or bad_s):
        _log("  All expected tickers show non-zero positions with correct sign.")


def _orders_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("result", "orders", "data"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _parse_ts(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000.0 if x > 1e12 else x
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit():
            x = float(s)
            return x / 1000.0 if x > 1e12 else x
        try:
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def _clear_position_latch(path: str = _POSITION_LATCH_PATH) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _read_position_latch_ts(path: str = _POSITION_LATCH_PATH) -> Optional[float]:
    """
    Persisted unix time when the current position batch was first seen (flat -> occupied).
    Survives bot restarts; cleared when flat. No trade-history API required.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        v = d.get("position_batch_started_unix")
        return float(v) if v is not None else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _parse_fetched_at_sgt_string(s: str) -> Optional[float]:
    """Parse marketstate.json ``fetched_at`` like '1:00pm 4 Apr 2026 SGT' -> unix (Asia/Singapore)."""
    m = re.match(
        r"^(\d{1,2}):(\d{2})(am|pm)\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+SGT\s*$",
        (s or "").strip(),
        re.I,
    )
    if not m:
        return None
    h_s, mi_s, ap, d_s, mon_s, y_s = m.groups()
    hour = int(h_s)
    minute = int(mi_s)
    ap_l = ap.lower()
    if ap_l == "pm" and hour != 12:
        hour += 12
    if ap_l == "am" and hour == 12:
        hour = 0
    mon = _MONTH_ABBR_TO_NUM.get(mon_s.lower()[:3])
    if mon is None:
        return None
    dt = datetime(int(y_s), mon, int(d_s), hour, minute, tzinfo=ZoneInfo("Asia/Singapore"))
    return float(dt.timestamp())


def read_marketstate_position_epoch_ts(
    path: str = _DEFAULT_MARKETSTATE_JSON,
) -> Optional[float]:
    """
    Time-in-position anchor: when ``marketstate.py`` last wrote JSON (just before strategy + orders in varibot).
    Prefers ``fetched_at_unix``; falls back to parsing ``fetched_at`` for older files.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    u = d.get("fetched_at_unix")
    if u is not None:
        try:
            return float(u)
        except (TypeError, ValueError):
            pass
    fa = d.get("fetched_at")
    if isinstance(fa, str):
        return _parse_fetched_at_sgt_string(fa)
    return None


def _write_position_latch(ts: float, path: str = _POSITION_LATCH_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"position_batch_started_unix": float(ts)}, f, indent=2)
    except OSError as e:
        _log(f"WARNING: could not write position latch {path}: {e}")


def last_non_reduce_order_ts(orders_raw: Any) -> Optional[float]:
    """Most recent timestamp among buy/sell orders not marked reduce-only (best-effort schema)."""
    best: Optional[float] = None
    for o in _orders_list(orders_raw):
        if o.get("is_reduce_only") is True or o.get("reduce_only") is True:
            continue
        side = str(o.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        status = str(o.get("status") or "").lower()
        if status in ("cancelled", "canceled", "rejected", "failed"):
            continue
        ts: Optional[float] = None
        for key in ("created_at", "createdAt", "inserted_at", "timestamp", "opened_at"):
            if key in o:
                ts = _parse_ts(o.get(key))
                if ts is not None:
                    break
        if ts is None:
            continue
        if best is None or ts > best:
            best = ts
    return best


def seconds_until_next_wall_interval(*, period_minutes: int) -> float:
    """Seconds until the next wall-clock multiple of period_minutes (e.g. 15 -> :00,:15,:30,:45)."""
    if period_minutes <= 0:
        return 1.0
    t = time.localtime()
    sec_into_day = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    period = int(period_minutes) * 60
    n = (sec_into_day // period) + 1
    next_boundary = n * period
    wait = float(next_boundary - sec_into_day)
    if wait <= 0.5:
        wait += float(period)
    return wait


def run_auth_or_exit() -> None:
    load_config()
    token = os.getenv("VR_TOKEN", "").strip()
    wallet = os.getenv("VR_WALLET_ADDRESS", "").strip()
    endpoint = os.getenv("VARI_AUTH_TEST_ENDPOINT", "/api/positions")
    if not token or not wallet:
        _log("ERROR: Missing VR_TOKEN or VR_WALLET_ADDRESS in environment (.env).")
        raise SystemExit(2)
    ok, info = validate_vr_token(vr_token=token, wallet_address=wallet, endpoint=endpoint)
    if not ok:
        _log(f"ERROR: Auth failed — notify owner. Details: {json.dumps(info, default=str)[:500]}")
        raise SystemExit(1)
    _log("Auth OK (validate_vr_token).")


def _run_script(
    script_path: str,
    *,
    cwd: str,
    args: Optional[List[str]] = None,
    timeout_s: Optional[float] = None,
) -> int:
    cmd = [sys.executable, "-u", script_path] + (args or [])
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT: {' '.join(cmd)}")
        return 124
    return int(proc.returncode)


def _resolve_marketstate_json_path(*, args: Optional[argparse.Namespace] = None) -> str:
    if args is not None:
        v = getattr(args, "marketstate_json", None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raw = (os.getenv("VARIBOT_MARKETSTATE_JSON") or "").strip()
    if raw:
        return raw
    return _STRATEGY_MARKETSTATE_JSON


def _ensure_strategy_marketstate_json(path: Optional[str] = None) -> str:
    """Write a minimal ``marketstate.json`` (timestamp only) for ``run_strategy`` compatibility."""
    out_path = (path or _STRATEGY_MARKETSTATE_JSON).strip() or _STRATEGY_MARKETSTATE_JSON
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    doc: Dict[str, Any] = {"fetched_at_unix": int(time.time()), "source": "varibot"}
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, out_path)
    return out_path


def _fetch_venue_mark_for_asset(ep: VariEndpoints, *, asset: str) -> float:
    """Indicative mark for order execution (POST /api/quotes/indicative; ``mark_price`` first)."""
    sym = str(asset).strip().upper()
    inst = Instrument.for_underlying(sym)
    q = ep.quote_indicative_simple(instrument=inst, qty=1.0)
    if isinstance(q, dict):
        for k in ("mark_price", "index_price", "ask", "bid"):
            if k in q and q[k] is not None:
                try:
                    mf = float(q[k])
                    if mf > 0:
                        return mf
                except (TypeError, ValueError):
                    continue
    raise RuntimeError(f"Could not read a positive mark for {sym} from indicative quote.")


def _marks_source() -> str:
    """``supported_assets`` (default) or ``indicative`` (per-ticker POST /api/quotes/indicative)."""
    return (os.getenv("VARIBOT_MARKS_SOURCE") or "supported_assets").strip().lower()


def _use_bulk_supported_assets_marks() -> bool:
    raw = _marks_source()
    return raw not in ("indicative", "per_ticker", "quote", "quotes")


def mark_price_from_supported_asset_entry(entry: Any) -> float:
    """
    Venue mark from one ``supported_assets`` row.

    Prefer ``price`` (tracks UI / indicative mark). ``index_price`` is fallback only.
    Order placement uses indicative quotes separately (``_fetch_venue_mark_for_asset``).
    """
    row = entry[0] if isinstance(entry, list) and entry else entry
    if not isinstance(row, dict):
        raise TypeError(f"supported_assets entry is not a dict: {type(row).__name__}")
    for k in ("price", "mark_price", "index_price"):
        v = row.get(k)
        if v is None:
            continue
        try:
            mf = float(v)
            if mf > 0:
                return mf
        except (TypeError, ValueError):
            continue
    sym = str(row.get("asset") or "?").strip().upper()
    raise RuntimeError(f"Could not read a positive mark for {sym} from supported_assets.")


def _fetch_supported_assets_mark_map(ep: VariEndpoints) -> Dict[str, float]:
    """One GET /api/metadata/supported_assets → uppercased ticker → mark."""
    bulk = ep.get_supported_assets()
    out: Dict[str, float] = {}
    for sym, entry in bulk.items():
        key = str(sym).strip().upper()
        if not key:
            continue
        try:
            out[key] = float(mark_price_from_supported_asset_entry(entry))
        except Exception:
            continue
    if not out:
        raise RuntimeError("supported_assets returned no parseable marks.")
    return out


def _mark_for_asset(
    ep: VariEndpoints,
    *,
    asset: str,
    bulk_map: Optional[Dict[str, float]] = None,
) -> float:
    """``supported_assets`` bulk map when enabled, else indicative fallback."""
    sym = str(asset).strip().upper()
    if _use_bulk_supported_assets_marks():
        m = bulk_map if bulk_map is not None else _fetch_supported_assets_mark_map(ep)
        if sym in m:
            return float(m[sym])
    return _fetch_venue_mark_for_asset(ep, asset=sym)


def _fetch_marks_for_assets(
    ep: VariEndpoints,
    assets: Iterable[str],
    *,
    bulk_map: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Marks for tickers (one bulk GET by default, per-ticker indicative fallback)."""
    syms = [str(a).strip().upper() for a in assets if str(a).strip()]
    if not syms:
        return {}
    bulk = bulk_map
    if bulk is None and _use_bulk_supported_assets_marks():
        bulk = _fetch_supported_assets_mark_map(ep)
    marks: Dict[str, float] = {}
    for sym in syms:
        try:
            if bulk is not None and sym in bulk:
                marks[sym] = float(bulk[sym])
            else:
                marks[sym] = float(_fetch_venue_mark_for_asset(ep, asset=sym))
        except Exception as e:
            _log(f"strategy listing: mark failed {sym} ({type(e).__name__}: {e})")
    return marks


def _write_strategy_listing_snapshot_from_marks(
    marks: Dict[str, float],
    *,
    source: str,
) -> str:
    if not marks:
        raise RuntimeError("No ticker marks to write listing snapshot.")
    listings = [{"vari_ticker": sym, "mark_price": float(marks[sym])} for sym in sorted(marks.keys())]
    doc: Dict[str, Any] = {
        "fetched_at_unix": int(time.time()),
        "source": source,
        "listings": listings,
    }
    out_path = _STRATEGY_LISTING_SNAPSHOT_JSON
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, out_path)
    return out_path


def _listing_snapshot_assets(
    ep: VariEndpoints,
    *,
    asset_hint: Optional[str] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> List[str]:
    """Tickers for strategy_listing_snapshot.json (env VARIBOT_LISTING_TICKERS or supported_assets)."""
    if asset_hint:
        return [str(asset_hint).strip().upper()]
    raw = (os.getenv("VARIBOT_LISTING_TICKERS") or "").strip()
    if raw:
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    if bulk_map:
        return sorted(bulk_map.keys())
    return sorted(_fetch_supported_assets_mark_map(ep).keys())



def _refresh_strategy_listing_snapshot_from_venue(
    ep: VariEndpoints,
    *,
    asset_hint: Optional[str] = None,
    marks: Optional[Dict[str, float]] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> str:
    """Write strategy_listing_snapshot.json from venue marks."""
    assets = _listing_snapshot_assets(ep, asset_hint=asset_hint, bulk_map=bulk_map)
    if marks is None:
        marks = _fetch_marks_for_assets(ep, assets, bulk_map=bulk_map)
    if not marks:
        raise RuntimeError(f"Could not fetch any ticker marks for listing snapshot (assets={assets!r})")
    src = (
        "varibot_supported_assets"
        if _use_bulk_supported_assets_marks()
        else "varibot_indicative"
    )
    return _write_strategy_listing_snapshot_from_marks(marks, source=src)


def _prepare_varibot_strategy_feed(
    ep: VariEndpoints,
    *,
    args: Optional[argparse.Namespace] = None,
    asset_hint: Optional[str] = None,
    marks: Optional[Dict[str, float]] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> Tuple[str, str]:
    """Refresh listing snapshot + marketstate JSON under Varibot/. Returns (listing_json, marketstate_json)."""
    listing_path = _refresh_strategy_listing_snapshot_from_venue(
        ep, asset_hint=asset_hint, marks=marks, bulk_map=bulk_map
    )
    ms_path = _ensure_strategy_marketstate_json(_resolve_marketstate_json_path(args=args))
    return listing_path, ms_path


def run_strategy_pick_tickers(
    *,
    strategy_key: str,
    listing_json: str,
    top_n: int,
    args: Optional[argparse.Namespace] = None,
    venue_pending_keys: Optional[Set[Tuple[str, str]]] = None,
    venue_mark: Optional[float] = None,
    account_flat: bool = False,
    venue_pending_by_asset: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
    venue_marks_by_asset: Optional[Dict[str, float]] = None,
    account_flat_by_asset: Optional[Dict[str, bool]] = None,
    paused_assets: Optional[Set[str]] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    ms_path = _resolve_marketstate_json_path(args=args)
    _ensure_strategy_marketstate_json(ms_path)
    if not os.path.isfile(str(listing_json)):
        raise FileNotFoundError(
            f"Strategy listing snapshot missing: {listing_json}. "
            "Call _prepare_varibot_strategy_feed(ep, ...) before run_strategy_pick_tickers."
        )
    mod = _load_strategy_module(strategy_key)
    if not hasattr(mod, "run_strategy"):
        raise AttributeError(f"strategy module {mod.__name__} missing run_strategy()")
    return mod.run_strategy(
        strategy_key=strategy_key,
        listing_json=listing_json,
        marketstate_json=ms_path,
        top_n=int(top_n),
        write_output_txt=True,
        venue_pending_keys=venue_pending_keys,
        venue_mark=venue_mark,
        account_flat=bool(account_flat),
        venue_pending_by_asset=venue_pending_by_asset,
        venue_marks_by_asset=venue_marks_by_asset,
        account_flat_by_asset=account_flat_by_asset,
        paused_assets=paused_assets,
    )


def _top_n_for_strategy(strategy_key: str) -> int:
    """Ticker-universe width for strategy.pick_tickers (optional VARIBOT_TOP_N)."""
    _ = strategy_key
    v = (os.getenv("VARIBOT_TOP_N", "") or "").strip()
    if v:
        try:
            return max(1, int(float(v)))
        except Exception:
            pass
    return 60


def run_closeallpositions(
    *,
    live: bool,
    slippage_percent: Optional[float] = None,
    log_invoke: bool = True,
) -> int:
    script = os.path.join(_VARIBOT_DIR, "closeallpositions.py")
    args: List[str] = []
    if slippage_percent is not None:
        args.extend(["--slippage-percent", str(float(slippage_percent))])
    if live:
        args.append("--live")
    if log_invoke:
        slip_txt = (
            "default/env (~0.1% if unset)"
            if slippage_percent is None
            else f"--slippage-percent {slippage_percent} ({float(slippage_percent) * 100:.2f}%)"
        )
        _log(f"Invoking closeallpositions.py ({slip_txt}) {'--live' if live else '(dry-run)'}")
    return _run_script(script, cwd=_VARIBOT_DIR, args=args, timeout_s=300.0)


def run_multimarket(
    *,
    multi_script: str,
    longs: List[str],
    shorts: List[str],
    usd: Optional[float] = None,
    im_target_pct: Optional[float] = None,
    live: bool,
    extra_args: Optional[List[str]] = None,
) -> int:
    script = os.path.join(_VARIBOT_DIR, multi_script)
    if not os.path.isfile(script):
        raise FileNotFoundError(f"Multi-market script not found: {script}")
    if (usd is None) == (im_target_pct is None):
        raise ValueError("run_multimarket: pass exactly one of usd= or im_target_pct=")
    if im_target_pct is not None:
        cmd_args: List[str] = [
            "--im-target-pct",
            str(float(im_target_pct)),
            "--long",
            ",".join(longs),
            "--short",
            ",".join(shorts),
        ]
    else:
        cmd_args = [
            "--usd",
            str(float(usd)),
            "--long",
            ",".join(longs),
            "--short",
            ",".join(shorts),
        ]
    if live:
        cmd_args.append("--live")
    # Ensure the child script uses the same sizing divisor as the strategy (do not rely on its ability to import).
    # This keeps slot sizing stable when DEFAULT_MAX_TICKER_ENTRIES is edited.
    cmd_args.extend(["--max-ticker-entries", str(int(DEFAULT_MAX_TICKER_ENTRIES))])
    if extra_args:
        cmd_args.extend(extra_args)
    _log(f"Invoking {multi_script} longs={len(longs)} shorts={len(shorts)} live={live}")
    return _run_script(script, cwd=_VARIBOT_DIR, args=cmd_args, timeout_s=None)


def run_multimarket_asset_side(
    *,
    multi_script: str,
    asset: str,
    side: str,
    usd: Optional[float] = None,
    qty: Optional[float] = None,
    live: bool,
    extra_args: Optional[List[str]] = None,
) -> int:
    """Single-asset market job: --assets ASSET --side buy|sell with exactly one of --usd or --qty."""
    script = os.path.join(_VARIBOT_DIR, multi_script)
    if not os.path.isfile(script):
        raise FileNotFoundError(f"Multi-market script not found: {script}")
    if (usd is None) == (qty is None):
        raise ValueError("run_multimarket_asset_side: pass exactly one of usd= or qty=")
    sym = str(asset).strip().upper()
    sd = str(side).strip().lower()
    if sd not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if qty is not None:
        cmd_args: List[str] = [
            "--qty",
            format_qty_for_indicative_api(float(qty)),
            "--assets",
            sym,
            "--side",
            sd,
        ]
    else:
        cmd_args = [
            "--usd",
            str(float(usd or 0.0)),
            "--assets",
            sym,
            "--side",
            sd,
        ]
    if live:
        cmd_args.append("--live")
    cmd_args.extend(["--max-ticker-entries", str(int(DEFAULT_MAX_TICKER_ENTRIES))])
    if extra_args:
        cmd_args.extend(extra_args)
    _log(f"Invoking {multi_script} asset={sym} side={sd} {'qty=' + cmd_args[1] if qty is not None else 'usd=' + str(usd)} live={live}")
    return _run_script(script, cwd=_VARIBOT_DIR, args=cmd_args, timeout_s=None)


def _read_multimarket_skew_rejected() -> List[Dict[str, str]]:
    """skew_rejected rows written by multimarketorder._write_multimarket_last_result."""
    try:
        with open(MULTIMARKET_LAST_RESULT_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    raw = d.get("skew_rejected")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for x in raw:
        if isinstance(x, dict) and x.get("asset"):
            out.append(
                {
                    "asset": str(x.get("asset") or "").strip().upper(),
                    "side": str(x.get("side") or "buy").strip().lower(),
                }
            )
    return out


def _read_multimarket_slippage_exhausted() -> List[Dict[str, str]]:
    """slippage_exhausted rows written by multimarketorder._write_multimarket_last_result."""
    try:
        with open(MULTIMARKET_LAST_RESULT_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    raw = d.get("slippage_exhausted")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for x in raw:
        if isinstance(x, dict) and x.get("asset"):
            out.append(
                {
                    "asset": str(x.get("asset") or "").strip().upper(),
                    "side": str(x.get("side") or "buy").strip().lower(),
                }
            )
    return out


def _take_side_candidates(candidates: Sequence[str], disallow: Set[str], need: int) -> List[str]:
    if need <= 0:
        return []
    out: List[str] = []
    for t in candidates:
        sym = str(t).strip().upper()
        if not sym or sym in disallow:
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= need:
            break
    return out


def build_endpoints() -> Tuple[Any, VariEndpoints]:
    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )
    return cfg, ep


def _resolve_max_slippage() -> float:
    try:
        v = os.getenv("MAX_SLIPPAGE", "").strip()
        if v:
            return float(v)
    except Exception:
        pass
    return float(_DEFAULT_MAX_SLIPPAGE)


def _try_parse_float_env(key: str) -> Optional[float]:
    v = (os.environ.get(key, "") or "").strip()
    if not v:
        return None
    try:
        out = float(v)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _max_slippage_cap_for_asset(asset: str, *, default_cap: float) -> float:
    """
    Per-asset slippage cap override for market orders.

    - Default: use *default_cap* (from MAX_SLIPPAGE / _DEFAULT_MAX_SLIPPAGE).
    - Override: MAX_SLIPPAGE_<ASSET>, e.g. MAX_SLIPPAGE_LIGHTER=0.0015 (0.15%).
    """
    sym = str(asset).strip().upper()
    if not sym:
        return float(default_cap)
    v = _try_parse_float_env(f"MAX_SLIPPAGE_{sym}")
    if v is None:
        return float(default_cap)
    return float(v)


def _looks_like_slippage_reject(msg: str) -> bool:
    m = (msg or "").lower()
    return ("max slippage" in m and ("exceed" in m or "exceeded" in m)) or ("slippage" in m and "exceed" in m)


def _current_qty_for_sym(ep: VariEndpoints, sym: str) -> Optional[float]:
    want = str(sym).strip().upper()
    raw = ep.get_positions()
    for pos in _positions_list(raw):
        s = _instrument_label(pos).strip().upper()
        if s != want:
            continue
        q = _position_qty(pos)
        if q is None:
            continue
        return float(q)
    return 0.0


def _extract_quote_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("quote_id", "quoteId", "id"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _close_reduce_only_with_slippage_steps(
    *,
    ep: VariEndpoints,
    sym: str,
    qty_abs: float,
    close_side: str,
    max_slip: float,
    max_attempts: int = _MAX_LIVE_ATTEMPTS,
) -> None:
    from variationalbot.vari.endpoints import Instrument  # local import

    sym_u = str(sym).strip().upper()
    instrument = Instrument(instrument_type="perpetual_future", underlying=sym_u)

    cap_slip = _max_slippage_cap_for_asset(sym_u, default_cap=float(max_slip))
    # Flatten market orders: allow a higher slippage cap to actually get flat under venue limits.
    # LIGHTER tends to require a wider cap; use 3× there, 2× elsewhere.
    from portfolio_rebalance import flatten_slippage_extra  # noqa: WPS433

    flatten_mult = 3.0 if sym_u == "LIGHTER" else 2.0
    cap_slip = float(flatten_mult) * float(cap_slip) + flatten_slippage_extra()
    base_slip = min(float(max_slip), float(cap_slip))

    last_err: Optional[Exception] = None
    for attempt in range(1, int(max_attempts) + 1):
        slip = float(base_slip) + float(attempt - 1) * float(_SLIPPAGE_RETRY_INCREMENT)
        slip = min(float(slip), float(cap_slip))
        try:
            qresp = ep.quote_indicative_simple(instrument=instrument, qty=float(qty_abs))
            if isinstance(qresp, dict):
                qresp["_close_side_override"] = str(close_side)
                qresp["_attempt"] = attempt
                qresp["_max_slippage"] = float(slip)
            quote_id = _extract_quote_id(qresp)
            if not quote_id:
                raise ValueError(f"indicative quote missing quote_id for {sym_u} qty={qty_abs}")
            resp = ep.place_order_market(
                quote_id=str(quote_id),
                side=str(close_side),
                max_slippage=float(slip),
                is_reduce_only=True,
            )
            # HTTP 200 with status=Rejected does not raise; treat like slippage failure and retry.
            if _order_response_rejected(resp):
                last_err = RuntimeError(
                    f"close {sym_u} order rejected in API response (attempt {attempt}/{int(max_attempts)})"
                )
                if attempt < int(max_attempts):
                    _log(f"{last_err}; retry with higher max_slippage...")
                    time.sleep(0.25)
                    continue
                _log(f"ERROR: {last_err}; giving up this leg.")
                return

            # confirm close by polling positions a few times
            time.sleep(1.0)
            ok = False
            for _ in range(8):
                cur_q = _current_qty_for_sym(ep, sym_u)
                if cur_q is not None and abs(float(cur_q)) <= 1e-12:
                    ok = True
                    break
                time.sleep(1.0)
            if not ok:
                last_err = RuntimeError(
                    f"close submitted but {sym_u} still open after polling (attempt {attempt}/{int(max_attempts)})"
                )
                if attempt < int(max_attempts):
                    _log(
                        f"WARNING: close submitted but {sym_u} still open after polling; "
                        f"retry {attempt + 1}/{int(max_attempts)} with higher max_slippage..."
                    )
                    time.sleep(0.25)
                    continue
                _log(
                    f"ERROR: close submitted but {sym_u} still open after {int(max_attempts)} attempt(s); "
                    "giving up this leg."
                )
                return
            return
        except Exception as e:
            last_err = e
            if not _looks_like_slippage_reject(str(e)):
                raise
            if attempt < int(max_attempts):
                _log(
                    f"close {sym_u} rejected for slippage; retry {attempt+1}/{int(max_attempts)} with higher max_slippage..."
                )
                time.sleep(0.25)
    if last_err is not None:
        raise last_err


def one_cycle(
    *,
    ep: VariEndpoints,
    cfg: Any,
    args: argparse.Namespace,
    cycle_index: int = 0,
) -> bool:
    """
    Returns True when a live time-in-position close-all just succeeded; main() should
    use a short cooldown then run the next cycle instead of sleeping to the wall clock.
    """
    _ = cycle_index
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    if not strat_key:
        raise RuntimeError("VARIBOT_STRATEGY is required (e.g. my_strategy.py under strategy/).")

    raw_pf = ep.get_portfolio(compute_margin=True)
    snap = parse_portfolio_snapshot(raw_pf)
    out = _build_out_dict(cfg=cfg, snap=snap)

    raw_pos = ep.get_positions()
    _log(_fmt_portfolio_snapshot_line(out))

    cycle_bulk_marks: Optional[Dict[str, float]] = None
    if bool(args.live) and _use_bulk_supported_assets_marks():
        try:
            cycle_bulk_marks = _fetch_supported_assets_mark_map(ep)
        except Exception as e:
            _log(
                f"supported_assets bulk marks failed ({type(e).__name__}: {e}); "
                "falling back to per-ticker indicative where needed."
            )

    has_pos = has_open_positions(raw_pos)
    rebalance_dry = bool(getattr(args, "rebalance_dry_run", False)) or not bool(args.live)

    if has_pos:
        try:
            lev = int(getattr(cfg, "max_leverage", 0) or 0)
            if lev <= 0:
                lev = int(os.getenv("MAX_LEVERAGE", "50") or "50")
        except (TypeError, ValueError):
            lev = 50
        rebalance_portfolio(
            ep=ep,
            snap=snap,
            positions_raw=raw_pos,
            max_leverage=lev,
            live=bool(args.live),
            dry_run=rebalance_dry,
            log=_log,
            max_slippage=float(_resolve_max_slippage()),
            mark_fetcher=lambda sym: float(
                _mark_for_asset(ep, asset=sym, bulk_map=cycle_bulk_marks)
            ),
            varibot_dir=_VARIBOT_DIR,
        )

    if not has_pos:
        _clear_position_latch()
        _log("No open positions -> venue listing snapshot -> strategy -> multimarket")
        marks_src = _marks_source()
        _log(f"step: refreshing strategy feed ({marks_src} mark → Varibot JSON)...")
        listing_json, _ = _prepare_varibot_strategy_feed(
            ep, args=args, marks=None, bulk_map=cycle_bulk_marks
        )
        top_n = _top_n_for_strategy(strat_key)
        longs, shorts, meta = run_strategy_pick_tickers(
            strategy_key=strat_key,
            listing_json=listing_json,
            top_n=top_n,
            args=args,
            account_flat=True,
        )
        _log("step: strategy feed ready")
        _log(f"step: strategy finished (strategy={meta.get('strategy')}, longs={len(longs)}, shorts={len(shorts)})")

        if _should_write_strategy_output():
            out_path = os.path.join(_VARIBOT_DIR, "strategy_output.json")
            try:
                payload = {
                    "written_at": _sgt_stamp_ms(),
                    "listing_json": os.path.abspath(str(listing_json)),
                    "strategy_key": strat_key,
                    "meta": meta,
                    "long": longs,
                    "short": shorts,
                }
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except OSError as e:
                _log(f"WARNING: could not write {out_path}: {e}")

        if not longs and not shorts:
            _log("strategy returned no tickers; skip multimarket.")
            return False

        _log(f"step: running {args.multi_script} (many API calls possible)...")
        if args.usd is not None:
            rc = run_multimarket(
                multi_script=str(args.multi_script),
                longs=longs,
                shorts=shorts,
                usd=float(args.usd),
                live=bool(args.live),
            )
        else:
            pct = _resolve_im_target_pct_for_multimarket(
                args_im_target_pct=float(args.im_target_pct) if args.im_target_pct is not None else None,
            )
            rc = run_multimarket(
                multi_script=str(args.multi_script),
                longs=longs,
                shorts=shorts,
                im_target_pct=pct,
                live=bool(args.live),
            )
        if rc != 0:
            _log(f"{args.multi_script} exited {rc}")
        else:
            _log(f"step: {args.multi_script} finished OK")
            if bool(args.live):
                _log_post_multimarket_positions_tally(ep=ep, longs=longs, shorts=shorts)
        return False

    _log("Open positions — cycle complete (no flat-entry path).")
    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Varibot flowchart orchestrator.")
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually close positions and place orders (otherwise dry-run).",
    )
    p.add_argument(
        "--rebalance-dry-run",
        action="store_true",
        help="Interval risk rebalance: log planned orders only (no cancels or market orders).",
    )
    p.add_argument(
        "--period-min",
        type=int,
        default=CHECK_INTERVAL_MIN,
        help=f"Wall-clock cycle interval minutes (default {CHECK_INTERVAL_MIN}).",
    )
    p.add_argument(
        "--pm-pair-tp-pct",
        type=float,
        default=PM_PAIR_TP_THRESHOLD_PCT_DEFAULT,
        help=(
            "Portfolio Manager: combined uPnL%% threshold vs combined value for pair closes "
            f"(default {PM_PAIR_TP_THRESHOLD_PCT_DEFAULT:g})."
        ),
    )
    p.add_argument(
        "--pm-refill",
        action="store_true",
        help="Portfolio Manager: after closing eligible pairs, refresh strategy feed and open replacements.",
    )
    p.add_argument(
        "--pm-no-refill",
        action="store_true",
        help="Portfolio Manager: disable replacements refill (overrides --pm-refill).",
    )
    p.add_argument(
        "--time-exit-periods",
        type=int,
        default=1,
        help="(deprecated) Previously used for time-in-position close-all; no longer used.",
    )
    p.add_argument(
        "--time-exit-source",
        choices=("marketstate", "auto", "latch", "orders"),
        default="marketstate",
        help=(
            "Time-in-position clock: marketstate=Varibot strategy_marketstate.json "
            "(fetched_at_unix from the feed refresh before orders; default); "
            "auto=marketstate then orders then latch; latch|orders=see help text."
        ),
    )
    p.add_argument(
        "--marketstate-json",
        default=None,
        help="Override path to marketstate JSON for time-in-position (default: Varibot/strategy_marketstate.json).",
    )
    p.add_argument(
        "--usd",
        type=float,
        default=None,
        help="Fixed USD notional per multimarket order. If omitted, uses --im-target-pct or built-in default (same as multimarketorder.DEFAULT_IM_TARGET_PCT).",
    )
    p.add_argument(
        "--im-target-pct",
        type=float,
        default=None,
        dest="im_target_pct",
        metavar="PCT",
        help=(
            "Multimarket sizing (multimarketorder --im-target-pct): per-order USD = "
            "(portfolio_value_usd × leverage × PCT/100) / n_orders (see multimarketorder). "
            f"If --usd is omitted and this is omitted, defaults to {DEFAULT_IM_TARGET_PCT:g}%%. "
            "Do not pass both --usd and --im-target-pct."
        ),
    )
    p.add_argument(
        "--multi-script",
        default="multimarketorder.py",
        help="Script name under Varibot/ (default multimarketorder.py; try multimarketorder_cadence_1s.py).",
    )
    p.add_argument(
        "--strategy",
        default=Strategy,
        help="Strategy module under strategy/ (default VARIBOT_STRATEGY env).",
    )
    p.add_argument("--once", action="store_true", help="Run a single cycle then exit (no sleep loop).")
    p.add_argument(
        "--no-align",
        action="store_true",
        help="Sleep fixed --period-min between cycles instead of aligning to wall clock.",
    )
    p.add_argument(
        "--mm-probe-short",
        default=None,
        metavar="TICKER",
        help=(
            "Bypass the normal cycle: after auth, run one live multimarket short for this ticker "
            "(notional from --usd if set, else --mm-probe-usd); passes --skip-im-hard-cap to the child."
        ),
    )
    p.add_argument(
        "--mm-probe-usd",
        type=float,
        default=100.0,
        help="With --mm-probe-short: USD per order when --usd is omitted (default 100).",
    )
    return p.parse_args()


def _child_main() -> int:
    args = parse_args()
    probe_ticker = (getattr(args, "mm_probe_short", None) or "").strip()
    if probe_ticker:
        if not bool(args.live):
            print("varibot: --mm-probe-short requires --live (places a real order).", file=sys.stderr)
            return 2
        run_auth_or_exit()
        cfg, ep = build_endpoints()
        usd_probe = float(args.usd) if args.usd is not None else float(getattr(args, "mm_probe_usd", 100.0) or 100.0)
        sym_u = probe_ticker.upper()
        strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
        _log(
            f"mm-probe: multimarket live short {sym_u} usd={usd_probe:g} (child --skip-im-hard-cap); "
            f"strategy={strat_key}"
        )
        rc_mm = run_multimarket(
            multi_script=str(args.multi_script),
            longs=[],
            shorts=[sym_u],
            usd=float(usd_probe),
            live=True,
            extra_args=["--skip-im-hard-cap"],
        )
        return int(rc_mm)

    if args.usd is not None and args.im_target_pct is not None:
        print("varibot: pass at most one of --usd and --im-target-pct.", file=sys.stderr)
        return 2
    run_auth_or_exit()
    cfg, ep = build_endpoints()

    cycle_n = 0
    while True:
        cycle_n += 1
        _log(f"=== cycle {cycle_n} ===")
        try:
            ti_just_closed = one_cycle(ep=ep, cfg=cfg, args=args, cycle_index=cycle_n)
        except Exception as e:
            _log(f"cycle error: {type(e).__name__}: {e}")
            maybe_notify_vari_portfolio_auth_failure(
                e, cycle_index=cycle_n, wallet_address=cfg.wallet_address, log=_log
            )
            if args.once:
                return 1
            ti_just_closed = False

        if args.once:
            return 0

        _log("cycle: complete.")
        if ti_just_closed:
            delay = _TIME_IN_POSITION_POST_CLOSE_SLEEP_S
            _log(
                f"Sleep {_format_duration_s(delay)} after time-in-position close, then next cycle "
                f"(skipping wait until wall-clock interval)"
            )
        elif args.no_align:
            delay = max(1.0, float(args.period_min) * 60.0)
            _log(
                f"Sleep {_format_duration_s(delay)} until next interval "
                f"at {_format_wake_at_sgt(delay)} SGT"
            )
        else:
            delay = seconds_until_next_wall_interval(period_minutes=int(args.period_min))
            _log(
                f"Sleep {_format_duration_s(delay)} until next interval "
                f"at {_format_wake_at_sgt(delay)} SGT"
            )
        time.sleep(delay)


if __name__ == "__main__":
    _maybe_start_render_health_listener()
    if os.getenv(_VARIBOT_WRAPPED_ENV, "").strip() != "1":
        raise SystemExit(_run_self_wrapped())
    raise SystemExit(_child_main())
