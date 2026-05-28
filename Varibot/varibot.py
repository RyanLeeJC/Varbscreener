from __future__ import annotations

"""
Varibot orchestrator — implements the VariBotFlowchart workflow:

  Auth (validate_vr_token) -> every T minutes: portfolio snapshot ->
  if positions -> PM / managers (see strategy); portfolio-wide TP close-all removed ->
  if flat (or gridstrat with GRIDSTRAT_IGNORE_VENUE_POSITIONS) -> listing -> strategy -> grid limits
  elif flat (non-grid) -> venue listing snapshot -> strategy -> multimarketorder

Run from the Varibot directory (or any cwd; this file fixes imports):

  cd .../Varibot && python3 varibot.py
  python3 varibot.py --live              # default: IM-target sizing (see multimarketorder.DEFAULT_IM_TARGET_PCT)
  python3 varibot.py --usd 20            # fixed USD per order instead

Dependencies: repo layout
  ../strategy/gridstrat.py, ./validate_vr_token.py, check_portfolio_stats.py,
  ./closeallpositions.py, ./multimarketorder.py (or *_cadence_1s.py)
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
# Grid / strategy JSON under Varibot (no ``Vari Listings`` pipeline).
_STRATEGY_LISTING_SNAPSHOT_JSON = os.path.join(_VARIBOT_DIR, "strategy_listing_snapshot.json")
_STRATEGY_MARKETSTATE_JSON = os.path.join(_VARIBOT_DIR, "strategy_marketstate.json")
_DEFAULT_MARKETSTATE_JSON = _STRATEGY_MARKETSTATE_JSON
_POSITION_LATCH_PATH = os.path.join(_VARIBOT_DIR, ".varibot_position_latch.json")

# Check interval (minutes) between cycles/sessions when --period-min is not provided.
CHECK_INTERVAL_MIN: int = 1

# --- User-tunable settings (surface here for quick edits) ---
#
# Portfolio Manager (PM) for strategy near_median:
# - Each CHECK_INTERVAL_MIN, PM can close (long,short) pairs when combined uPnL% clears a threshold.
# - Default threshold: portfolio_manager_pairs.PAIR_TP_THRESHOLD_PCT_DEFAULT (imported below as PM_PAIR_...).
# - Optionally refill closed slots by refreshing the Varibot strategy listing snapshot and opening replacements.
PM_REFILL_DEFAULT_ON: bool = True

# Grid (``strategy/gridstrat``): after each ``grid_mode`` tick, ``grid_limits_reconcile`` runs remnant
# re-arm (``VARIBOT_GRID_LIMITS_RECONCILE=0`` to disable live limit POST/cancel).
# Live limit reconcile defaults ON (``grid_limits_reconcile.GRID_LIMITS_RECONCILE_DEFAULT``); no Railway env
# required. Set ``VARIBOT_GRID_LIMITS_RECONCILE=0`` to disable. Drift refill auto-on with paired_limit;
# drift cancel defaults on (keep-depth orphans; 418-safe pacing via pending_limit_cancel).

# Strategies that use the "session" loop in _child_main: enter now, TP checks on CHECK_INTERVAL_MIN cadence,
# then close-all at the next wall multiple of STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN (see seconds_until_next_wall_interval).
#
# NOTE (this branch): `near_median` no longer uses wall-clock close-all; exits are driven by the portfolio manager.
STRATEGY_SESSION_CLOSEALL_KEYS: frozenset[str] = frozenset({"revert_near_median"})
STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN: int = 360

_TIME_IN_POSITION_POST_CLOSE_SLEEP_S: float = 15.0 # after a live time-in-position close, sleep this long then start the next cycle (skip wall-clock wait)

# Strategy risk control: hard max hold time for position batches (invert_extreme only).
# If the oldest open position's `position_info.opened_at` is >= this many hours, varibot will
# close all positions (live only) at the start of the "have positions" cycle.
TIME_KILL_POSITION_HOURS: float = 12.0

# User setting: which strategy to run when flat.
# You can put a module name (preferred) or a filename:
#   "gridstrat" or "gridstrat.py" (Vari price-ladder grid; see strategy/gridstrat.py + GRID_* env)
Strategy: str = os.getenv("VARIBOT_STRATEGY", "gridstrat.py").strip()
if not Strategy:
    Strategy = "gridstrat.py"

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
from grid_limits_reconcile import (  # noqa: E402
    _position_qty_summary,
    bulk_pending_fetch_enabled,
    fetch_pending_limit_keys_by_assets,
    fetch_pending_limit_keys_for_asset,
    grid_limits_reconcile_enabled,
    run_grid_limits_bootstrap,
)
from positions import _instrument_label  # noqa: E402
from validate_vr_token import validate_vr_token  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402
from variationalbot.vari.endpoints import Instrument, format_qty_for_indicative_api  # noqa: E402

from multimarketorder import (  # noqa: E402
    DEFAULT_IM_TARGET_PCT,
    DEFAULT_LEVERAGE as MULTIMARKET_DEFAULT_LEVERAGE,
    MULTIMARKET_LAST_RESULT_JSON,
    USD_NOTIONAL_ROUND_STEP,
    _order_response_rejected,
)
import strategy.gridstrat as strategies_mod  # noqa: E402
from strategy.gridstrat import (  # noqa: E402
    DEFAULT_MAX_TICKER_ENTRIES as INVERT_EXTREME_MAX_TICKER_ENTRIES,
    grid_leverage_for_asset,
    grid_trading_ticker_band_pcts,
    gridstrat_ignore_venue_positions,
)
from portfolio_manager_pairs import (
    PAIR_TP_THRESHOLD_PCT_DEFAULT as PM_PAIR_TP_THRESHOLD_PCT_DEFAULT,
    PairCandidate,
    LEG_SL_THRESHOLD_PCT_DEFAULT as PM_LEG_SL_THRESHOLD_PCT_DEFAULT,
    LEG_TP_THRESHOLD_PCT_DEFAULT as PM_LEG_TP_THRESHOLD_PCT_DEFAULT,
    filter_replacements,
    filter_replacements_one_side,
    select_legs_to_close,
    positions_to_rows,
    scan_best_winner_opposite_pair,
    select_pairs_greedy_grid,
)  # noqa: E402


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


def _near_median_im_gap_metrics(
    *,
    snap: Any,
    portfolio_value_usd: float,
    leverage: int,
    pos_notional_usd: Optional[float] = None,
) -> Tuple[float, float, float, str]:
    """
    IM gap vs DEFAULT_IM_TARGET_PCT and available_position_value = gap_ratio × pv × lev.

    Prefer snap.im_usage from GET /api/portfolio?compute_margin=true (parsed ratio 0..1).
    If missing, fall back to pos_notional_usd / (pv × leverage).
    """
    den = float(portfolio_value_usd) * float(leverage)
    cap_ratio = float(DEFAULT_IM_TARGET_PCT) / 100.0
    if den <= 1e-12:
        return 0.0, 0.0, 0.0, "none"

    im_usage_snap = getattr(snap, "im_usage", None)
    if im_usage_snap is not None:
        im_ratio = float(im_usage_snap)
        gap_ratio = max(0.0, cap_ratio - im_ratio)
        available_pv_usd = gap_ratio * den
        return im_ratio, gap_ratio, available_pv_usd, "im_usage"

    if pos_notional_usd is not None:
        im_ratio = max(0.0, float(pos_notional_usd) / den)
        gap_ratio = max(0.0, cap_ratio - im_ratio)
        available_pv_usd = gap_ratio * den
        return im_ratio, gap_ratio, available_pv_usd, "pos_notional_fallback"

    return 0.0, 0.0, 0.0, "none"


def _near_median_usd_per_pair_budget(*, usd_per_leg: float) -> float:
    """$/pair = 2 × usd_per_leg (paired long + short at multimarket leg size)."""
    return 2.0 * float(usd_per_leg)


def _near_median_max_pairs_from_available_position_value(
    *, available_position_value_usd: float, usd_per_pair: float
) -> int:
    """How many new pairs fit at $/pair = 2 × usd_per_leg."""
    cost = float(usd_per_pair)
    if cost <= 1e-12 or available_position_value_usd <= 0:
        return 0
    return int(available_position_value_usd // cost)


def _near_median_align_pair_candidates(
    longs: List[str],
    shorts: List[str],
    *,
    wanted_pairs: int,
) -> Tuple[List[str], List[str], int]:
    """
    Pair long/short candidate lists to equal length (min of sides and budget). Used after
    filter_replacements when one side has fewer fresh symbols than the planned pair count.
    """
    n = min(len(longs), len(shorts), int(wanted_pairs))
    return longs[:n], shorts[:n], n


def _near_median_slot_usd_per_leg(*, portfolio_value_usd: float, leverage: int) -> float:
    """Per-leg USD slot: pv × leverage × (DEFAULT_IM_TARGET_PCT/100) / INVERT_EXTREME_MAX_TICKER_ENTRIES."""
    im_frac = float(DEFAULT_IM_TARGET_PCT) / 100.0
    raw = (float(portfolio_value_usd) * float(leverage) * im_frac) / float(INVERT_EXTREME_MAX_TICKER_ENTRIES)
    step = float(USD_NOTIONAL_ROUND_STEP)
    return float(math.ceil(raw / step) * step)


def _near_median_pm_usd_for_multimarket(
    *,
    snap: Any,
    args: argparse.Namespace,
    jobs_tag: str,
    book_pos_notional_usd: Optional[float] = None,
) -> Optional[float]:
    """
    near_median PM refill / top-up: per-leg USD = (pv × leverage × DEFAULT_IM_TARGET_PCT/100) / DEFAULT_MAX_TICKER_ENTRIES.
    Hard-cap behavior: if snap.im_usage >= DEFAULT_IM_TARGET_PCT, do not open new exposure.
    (No "remaining IM"/gap budgeting for refills; sizing stays constant per slot.)
    """
    if args.usd is not None:
        return float(args.usd)

    pv = getattr(snap, "portfolio_value_usd", None)
    if pv is None or float(pv) <= 0:
        _log(f"PM(near_median): skip {jobs_tag} — portfolio_value_usd missing or non-positive.")
        return None

    im_usage = getattr(snap, "im_usage", None)
    if im_usage is None:
        _log(f"PM(near_median): skip {jobs_tag} — im_usage missing; cannot enforce IM hard cap.")
        return None
    cap_ratio = float(DEFAULT_IM_TARGET_PCT) / 100.0
    if float(im_usage) >= cap_ratio:
        _log(
            f"PM(near_median): skip {jobs_tag} — IM hard cap "
            f"(IM%={float(im_usage) * 100.0:.2f}%; cap {float(DEFAULT_IM_TARGET_PCT):g}%)."
        )
        return None

    lev = _multimarket_effective_leverage()
    slot = _near_median_slot_usd_per_leg(portfolio_value_usd=float(pv), leverage=int(lev))
    _log(
        f"PM(near_median): {jobs_tag} sizing — "
        f"IM%={float(im_usage) * 100.0:.2f}% (cap {float(DEFAULT_IM_TARGET_PCT):g}%); "
        f"usd_per_leg={slot:g} (pv×lev×({DEFAULT_IM_TARGET_PCT:g}%/100)/{INVERT_EXTREME_MAX_TICKER_ENTRIES})."
    )
    return float(slot)


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
    """Live mark from POST /api/quotes/indicative (not cached listing files)."""
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


def _grid_marks_source() -> str:
    """``supported_assets`` (default) or ``indicative`` (per-ticker POST /api/quotes/indicative)."""
    return (os.getenv("VARIBOT_MARKS_SOURCE") or "supported_assets").strip().lower()


def _use_bulk_supported_assets_marks() -> bool:
    raw = _grid_marks_source()
    return raw not in ("indicative", "per_ticker", "quote", "quotes")


def mark_price_from_supported_asset_entry(entry: Any) -> float:
    """Parse ``index_price`` (preferred) or ``price`` from one supported_assets row."""
    row = entry[0] if isinstance(entry, list) and entry else entry
    if not isinstance(row, dict):
        raise TypeError(f"supported_assets entry is not a dict: {type(row).__name__}")
    for k in ("index_price", "price", "mark_price"):
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


def _grid_mark_for_asset(
    ep: VariEndpoints,
    *,
    asset: str,
    bulk_map: Optional[Dict[str, float]] = None,
) -> float:
    """Mark for one ticker: bulk map when enabled, else indicative."""
    sym = str(asset).strip().upper()
    if _use_bulk_supported_assets_marks():
        m = bulk_map if bulk_map is not None else _fetch_supported_assets_mark_map(ep)
        if sym in m:
            return float(m[sym])
    return _fetch_venue_mark_for_asset(ep, asset=sym)


def _fetch_grid_marks_for_assets(
    ep: VariEndpoints,
    assets: Iterable[str],
    *,
    bulk_map: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Marks for grid tickers (one bulk GET by default, per-ticker indicative fallback)."""
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
        raise RuntimeError("No grid ticker marks to write listing snapshot.")
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


def _grid_listing_snapshot_assets(*, asset_hint: Optional[str] = None) -> List[str]:
    """Tickers to include in ``strategy_listing_snapshot.json`` (``GRID_TRADING_TICKERS`` keys)."""
    tickers = grid_trading_ticker_band_pcts()
    assets = [str(k).strip().upper() for k in tickers.keys() if str(k).strip()]
    if not assets:
        assets = [(asset_hint or os.getenv("GRID_ASSET") or "BTC").strip().upper()]
    if asset_hint:
        hint = str(asset_hint).strip().upper()
        if hint and hint not in assets:
            assets.append(hint)
    return assets


def _refresh_strategy_listing_snapshot_from_venue(
    ep: VariEndpoints,
    *,
    asset_hint: Optional[str] = None,
    grid_marks: Optional[Dict[str, float]] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> str:
    """
    Write ``strategy_listing_snapshot.json`` with one row per grid ticker from
    ``grid_trading_ticker_band_pcts()`` (``GRID_TRADING_TICKERS`` in gridstrat.py).

    Default marks: one GET ``/api/metadata/supported_assets`` (``VARIBOT_MARKS_SOURCE``).
    Set ``VARIBOT_MARKS_SOURCE=indicative`` for per-ticker POST /api/quotes/indicative.
    """
    assets = _grid_listing_snapshot_assets(asset_hint=asset_hint)
    if grid_marks is None:
        grid_marks = _fetch_grid_marks_for_assets(ep, assets, bulk_map=bulk_map)
    if not grid_marks:
        raise RuntimeError(f"Could not fetch any grid ticker marks for listing snapshot (assets={assets!r})")
    src = (
        "varibot_supported_assets"
        if _use_bulk_supported_assets_marks()
        else "varibot_indicative"
    )
    return _write_strategy_listing_snapshot_from_marks(grid_marks, source=src)


def _prepare_varibot_strategy_feed(
    ep: VariEndpoints,
    *,
    args: Optional[argparse.Namespace] = None,
    asset_hint: Optional[str] = None,
    grid_marks: Optional[Dict[str, float]] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> Tuple[str, str]:
    """Refresh listing snapshot + marketstate JSON under Varibot/. Returns (listing_json, marketstate_json)."""
    listing_path = _refresh_strategy_listing_snapshot_from_venue(
        ep, asset_hint=asset_hint, grid_marks=grid_marks, bulk_map=bulk_map
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
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    ms_path = _resolve_marketstate_json_path(args=args)
    _ensure_strategy_marketstate_json(ms_path)
    if not os.path.isfile(str(listing_json)):
        raise FileNotFoundError(
            f"Strategy listing snapshot missing: {listing_json}. "
            "Call _prepare_varibot_strategy_feed(ep, ...) before run_strategy_pick_tickers."
        )
    return strategies_mod.run_strategy(
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
    )


def _fetch_cycle_pending_by_asset(
    ep: VariEndpoints,
    *,
    assets: Iterable[str],
) -> Optional[Dict[str, Set[Tuple[str, str]]]]:
    """
    One paginated pending sweep for all grid tickers (pass 1 + pass 2 reuse).

    Returns None when bulk is disabled or the request fails (caller falls back per-ticker).
    """
    if not bulk_pending_fetch_enabled():
        return None
    syms = [str(a).strip().upper() for a in assets if str(a).strip()]
    if not syms:
        return {}
    try:
        by_asset = fetch_pending_limit_keys_by_assets(ep, assets=syms)
        n_limits = sum(len(v) for v in by_asset.values())
        _log(
            f"step: pending bulk fetch OK — {n_limits} limit(s) across {len(by_asset)} ticker(s) "
            f"(paginated GET /api/orders/v2?status=pending)"
        )
        return by_asset
    except Exception as e:
        _log(
            f"step: pending bulk fetch failed ({type(e).__name__}: {e}); "
            "falling back to per-ticker pending GETs"
        )
        return None


def _fetch_venue_pending_for_grid(
    ep: VariEndpoints, *, asset: str
) -> Optional[Set[Tuple[str, str]]]:
    """Pending limit keys for one grid ticker (live paired_limit venue sync)."""
    sym = str(asset).strip().upper()
    try:
        return fetch_pending_limit_keys_for_asset(ep, asset=sym)
    except Exception as e:
        _log(f"gridlimits[{sym}]: pending fetch before strategy failed ({type(e).__name__}: {e})")
        return None


def _grid_venue_inputs_for_cycle(
    ep: VariEndpoints,
    *,
    args: argparse.Namespace,
    positions_raw: Any,
    ignore_venue_positions: bool = False,
    venue_marks_by_asset: Optional[Dict[str, float]] = None,
    venue_pending_by_asset: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
    bulk_map: Optional[Dict[str, float]] = None,
) -> Tuple[Dict[str, float], Dict[str, Set[Tuple[str, str]]], Dict[str, bool]]:
    """Per-ticker venue marks, pending limit keys, and flat flags for ``pick_tickers``."""
    marks: Dict[str, float] = {}
    pending_by: Dict[str, Set[Tuple[str, str]]] = {}
    flat_by: Dict[str, bool] = {}
    tickers = grid_trading_ticker_band_pcts()
    if bool(args.live) and venue_marks_by_asset is None and _use_bulk_supported_assets_marks():
        bulk_map = bulk_map if bulk_map is not None else _fetch_supported_assets_mark_map(ep)
    for asset in tickers:
        if bool(args.live):
            if venue_marks_by_asset is not None and asset in venue_marks_by_asset:
                marks[asset] = float(venue_marks_by_asset[asset])
            else:
                try:
                    marks[asset] = float(
                        _grid_mark_for_asset(ep, asset=asset, bulk_map=bulk_map)
                    )
                except Exception as e:
                    _log(f"gridlimits[{asset}]: venue mark failed ({type(e).__name__}: {e})")
            if venue_pending_by_asset is not None:
                if asset in venue_pending_by_asset:
                    pending_by[asset] = set(venue_pending_by_asset[asset])
            else:
                pk = _fetch_venue_pending_for_grid(ep, asset=asset)
                # Fetch failure → omit key (gridstrat sees pending=None). Do not use set() or
                # fresh_flat_start wrongly assumes an empty venue book (RWA instrument query 400).
                if pk is not None:
                    pending_by[asset] = pk
        else:
            pending_by[asset] = set()
        if ignore_venue_positions:
            # Grid session is flat-path; pending limits must not block flat rebalance logic.
            flat_by[asset] = True
        else:
            pos_s = _position_qty_summary(positions_raw or {}, asset=asset)
            flat_by[asset] = not bool(pos_s.get("has_position"))
    return marks, pending_by, flat_by


def _meta_flag_any_asset(meta: Dict[str, Any], key: str) -> bool:
    if bool(meta.get(key)):
        return True
    by = meta.get("grid_by_asset")
    if isinstance(by, dict):
        for am in by.values():
            if isinstance(am, dict) and bool(am.get(key)):
                return True
    return False


def _log_gridstrat_paired_step(meta: Dict[str, Any], *, prefix: str) -> None:
    by_asset = meta.get("grid_by_asset")
    if isinstance(by_asset, dict) and by_asset:
        for sym, am in by_asset.items():
            if not isinstance(am, dict):
                continue
            logs = am.get("grid_paired_step_logs")
            if not isinstance(logs, list) or not logs:
                continue
            for line in logs[-6:]:
                _log(f"{prefix}[{sym}]: {line}")
        return
    logs = meta.get("grid_paired_step_logs")
    if not isinstance(logs, list) or not logs:
        return
    for line in logs[-8:]:
        _log(f"{prefix}: {line}")


def _log_gridstrat_step_summary(meta: Dict[str, Any], *, positions_label: str) -> None:
    by_asset = meta.get("grid_by_asset")
    if isinstance(by_asset, dict) and by_asset:
        for sym, am in by_asset.items():
            if not isinstance(am, dict):
                continue
            n_b = len(am.get("grid_buy_rungs") or [])
            n_s = len(am.get("grid_sell_rungs") or [])
            _log(
                f"step: gridstrat[{sym}] ({positions_label}) rungs buy={n_b} sell={n_s} "
                f"mark={am.get('grid_mark')!r} band=±{am.get('grid_band_pct')}% "
                f"source={am.get('grid_mark_source')!r}"
            )
            if am.get("grid_bounds_explicit"):
                _log(
                    f"step: gridstrat[{sym}] bounds explicit lower={am.get('grid_lower')} "
                    f"upper={am.get('grid_upper')}"
                )
            elif am.get("grid_band_pct") is not None:
                _log(
                    f"step: gridstrat[{sym}] bounds ±{am.get('grid_band_pct')}% (pinned) "
                    f"lower={am.get('grid_lower')} upper={am.get('grid_upper')}"
                )
        return
    if meta.get("grid_paired_limit_mode"):
        n_b = len(meta.get("grid_buy_rungs") or [])
        n_s = len(meta.get("grid_sell_rungs") or [])
        _log(
            f"step: gridstrat paired_limit open_rungs buy={n_b} sell={n_s} "
            f"mark={meta.get('grid_mark')!r} source={meta.get('grid_mark_source')!r}"
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
    cmd_args.extend(["--max-ticker-entries", str(int(INVERT_EXTREME_MAX_TICKER_ENTRIES))])
    if extra_args:
        cmd_args.extend(extra_args)
    _log(f"Invoking {multi_script} longs={len(longs)} shorts={len(shorts)} live={live}")
    return _run_script(script, cwd=_VARIBOT_DIR, args=cmd_args, timeout_s=None)


def _is_grid_like_strategy(key: str) -> bool:
    """Strategy keys that use strategy/gridstrat.py (mark-ladder grid)."""
    return _strategy_key_normalized(key) in ("gridstrat", "vari_grid", "invert_extreme")


def _gridstrat_ignores_venue_positions(strat_key: str) -> bool:
    """Grid-only: treat venue positions as irrelevant for orchestration (see gridstrat env)."""
    if not gridstrat_ignore_venue_positions():
        return False
    return _strategy_key_normalized(strat_key) in ("gridstrat", "vari_grid")


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
    cmd_args.extend(["--max-ticker-entries", str(int(INVERT_EXTREME_MAX_TICKER_ENTRIES))])
    if extra_args:
        cmd_args.extend(extra_args)
    _log(f"Invoking {multi_script} asset={sym} side={sd} {'qty=' + cmd_args[1] if qty is not None else 'usd=' + str(usd)} live={live}")
    return _run_script(script, cwd=_VARIBOT_DIR, args=cmd_args, timeout_s=None)


def _grid_limits_place_limit_fn(
    ep: VariEndpoints,
    args: argparse.Namespace,
) -> Callable[..., int]:
    slip = float(_resolve_max_slippage())

    def place_limit(
        asset: str,
        side: str,
        usd: float,
        px: float,
        use_mark: bool,
        lq: Optional[str],
    ) -> int:
        sym = str(asset).strip().upper()
        sd = str(side).strip().lower()
        if sd not in ("buy", "sell"):
            return 1
        inst = Instrument.for_underlying(sym)
        lev = int(grid_leverage_for_asset(sym))
        try:
            ep.set_leverage(asset=sym, leverage=lev)
        except Exception as e:
            _log(f"gridlimits reconcile: set_leverage failed ({type(e).__name__}: {e})")
        try:
            if lq is not None and str(lq).strip():
                raw_q = float(str(lq).strip())
            else:
                raw_q = float(usd) / float(px) if float(px) > 0 else 0.0
            qty_str = ep.normalize_grid_limit_qty(
                instrument=inst,
                side=sd,
                qty_raw=raw_q,
            )
            resp = ep.place_order_limit(
                instrument=inst,
                side=sd,
                limit_price=float(px),
                qty=qty_str,
                slippage_limit=float(slip),
                use_mark_price=bool(use_mark),
                is_reduce_only=False,
                is_auto_resize=False,
            )
        except Exception as e:
            _log(f"gridlimits reconcile: place_order_limit failed ({type(e).__name__}: {e})")
            return 1
        if _order_response_rejected(resp):
            _log(f"gridlimits reconcile: venue rejected limit response={str(resp)[:500]!r}")
            return 1
        return 0

    return place_limit


def _run_grid_limits_bootstrap_if_grid(
    *,
    ep: VariEndpoints,
    meta: Dict[str, Any],
    args: argparse.Namespace,
    cycle_index: int,
    has_positions: bool,
    pending_by_asset: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
) -> None:
    if not meta.get("grid_mode"):
        return
    if not grid_limits_reconcile_enabled():
        return
    run_grid_limits_bootstrap(
        ep=ep,
        meta=meta,
        varibot_dir=_VARIBOT_DIR,
        cycle_index=int(cycle_index),
        has_positions=bool(has_positions),
        log=_log,
        place_limit=_grid_limits_place_limit_fn(ep, args),
        live=bool(args.live),
        multi_script=str(args.multi_script),
        pending_by_asset_preloaded=pending_by_asset,
    )


def _execute_grid_market_events(meta: Dict[str, Any], *, args: argparse.Namespace) -> None:
    evs = meta.get("grid_market_events") or []
    if not evs:
        return
    script = str(args.multi_script)
    sizing = str(meta.get("grid_market_sizing") or "qty").strip().lower()
    for raw in evs:
        if not isinstance(raw, dict):
            continue
        act = str(raw.get("action") or "")
        if act == "grid_restore_buys":
            _log(
                "gridstrat: buy ladder re-armed after first-sell anchor cross "
                f"(anchor={raw.get('price')!r})."
            )
            continue
        if act not in ("open_buy", "open_sell"):
            continue
        asset = str(raw.get("asset") or meta.get("grid_asset") or "").strip().upper()
        if not asset:
            _log("gridstrat: skip event (missing asset)")
            continue
        side = "buy" if act == "open_buy" else "sell"
        px = raw.get("price")
        qty_ev = raw.get("qty")
        use_qty = (
            sizing != "usd"
            and qty_ev is not None
            and float(qty_ev) > 0.0
        )
        if use_qty:
            qf = float(qty_ev)
            _log(f"gridstrat: {act} {asset} qty={format_qty_for_indicative_api(qf)} mark_rung={px!r} live={bool(args.live)}")
            rc = run_multimarket_asset_side(
                multi_script=script,
                asset=asset,
                side=side,
                qty=qf,
                live=bool(args.live),
            )
        else:
            usd = float(raw.get("usd") or 0.0)
            if usd <= 0:
                continue
            _log(f"gridstrat: {act} {asset} usd={usd:g} mark_rung={px!r} live={bool(args.live)}")
            rc = run_multimarket_asset_side(
                multi_script=script,
                asset=asset,
                side=side,
                usd=usd,
                live=bool(args.live),
            )
        if rc != 0:
            _log(f"{script} grid event exited {rc}")


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


def _near_median_substitute_skew_rejected_multimarket(
    *,
    ep: VariEndpoints,
    args: argparse.Namespace,
    strat_key: str,
    listing_json: str,
    jobs_tag: str,
    usd: Optional[float],
) -> None:
    """
    After multimarketorder records OI-skew / risk-cap rejects or SlippageExhausted (all slippage retries
    failed), open alternate tickers from the same strategy ranked lists (excluding open book + failed
    symbols). Controlled by VARIBOT_SKEW_REPLACE_MAX_ROUNDS (default 1).
    """
    if _strategy_key_normalized(strat_key) != "invert_extreme":
        return
    if not bool(args.live):
        return
    skew_sub_extra: List[str] = []
    if (getattr(args, "mm_probe_short", None) or "").strip():
        skew_sub_extra.append("--skip-im-hard-cap")
    try:
        max_rounds = max(1, int(os.getenv("VARIBOT_SKEW_REPLACE_MAX_ROUNDS", "1")))
    except Exception:
        max_rounds = 1

    symbols_failed: Set[str] = set()

    for _ in range(max_rounds):
        skew = _read_multimarket_skew_rejected()
        slip = _read_multimarket_slippage_exhausted()
        rejects: List[Dict[str, str]] = list(skew) + list(slip)
        if not rejects:
            return

        try:
            raw_pos = ep.get_positions()
        except Exception as e:
            _log(
                f"PM(near_median): reject substitution ({jobs_tag}) skipped — positions fetch failed ({e})."
            )
            return

        open_syms = {str(r.ticker).strip().upper() for r in positions_to_rows(raw_pos) if r.ticker}
        for x in rejects:
            a = str(x.get("asset") or "").strip().upper()
            if a:
                symbols_failed.add(a)
        disallow = set(open_syms) | set(symbols_failed)

        n_buy = sum(1 for x in rejects if str(x.get("side") or "").lower() in ("buy", "b"))
        n_sell = sum(1 for x in rejects if str(x.get("side") or "").lower() in ("sell", "s"))

        top_n = _top_n_for_strategy(strat_key)
        try:
            listing_json, _ = _prepare_varibot_strategy_feed(ep, args=args)
            longs, shorts, meta = run_strategy_pick_tickers(
                strategy_key=strat_key,
                listing_json=listing_json,
                top_n=top_n,
                args=args,
            )
        except Exception as e:
            _log(
                f"PM(near_median): reject substitution ({jobs_tag}) — strategy refresh failed "
                f"({type(e).__name__}: {e})."
            )
            return

        new_l = _take_side_candidates(longs, disallow, n_buy)
        new_s = _take_side_candidates(shorts, disallow, n_sell)

        if not new_l and not new_s:
            _log(
                f"PM(near_median): reject substitution ({jobs_tag}) — no alternate tickers "
                f"(need buy×{n_buy} sell×{n_sell}; strategy={meta.get('strategy')})."
            )
            return

        _log(
            f"PM(near_median): reject substitution ({jobs_tag}) — venue blocked skew={skew} "
            f"slippage_exhausted={slip}; "
            f"retry longs={new_l} shorts={new_s} (same sizing mode as parent multimarket)."
        )

        if usd is not None:
            rc2 = run_multimarket(
                multi_script=str(args.multi_script),
                longs=new_l,
                shorts=new_s,
                usd=float(usd),
                live=True,
                extra_args=skew_sub_extra or None,
            )
        else:
            pct = _resolve_im_target_pct_for_multimarket(
                args_im_target_pct=float(args.im_target_pct) if args.im_target_pct is not None else None,
            )
            rc2 = run_multimarket(
                multi_script=str(args.multi_script),
                longs=new_l,
                shorts=new_s,
                im_target_pct=pct,
                live=True,
                extra_args=skew_sub_extra or None,
            )

        if int(rc2) == 0:
            _log_post_multimarket_positions_tally(ep=ep, longs=new_l, shorts=new_s)


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

    last_err: Optional[Exception] = None
    for attempt in range(1, int(max_attempts) + 1):
        slip = float(max_slip) + float(attempt - 1) * float(_SLIPPAGE_RETRY_INCREMENT)
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


def _near_median_pm_dry_run_refill_preview(
    *,
    ep: VariEndpoints,
    args: argparse.Namespace,
    strat_key: str,
    positions_raw: Any,
    pairs: List[PairCandidate],
    snap: Any,
) -> None:
    """
    Dry-run only: same universe refresh + replacement picking as live refill, but without closes or orders.
    Uses hypothetical opens after PM closes (current opens minus legs tagged for close).
    """
    refill_on = bool(getattr(args, "pm_refill", False)) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        refill_on = bool(PM_REFILL_DEFAULT_ON) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        _log("PM(near_median): dry-run preview — refill disabled (--pm-no-refill); skip listing / sizing preview.")
        return

    n_pairs = len(pairs)
    if n_pairs <= 0:
        return

    closed_preview: Set[str] = set()
    for p in pairs:
        closed_preview.add(str(p.long_ticker).strip().upper())
        closed_preview.add(str(p.short_ticker).strip().upper())

    open_syms: Set[str] = set()
    for pos in _positions_list(positions_raw):
        sym = _instrument_label(pos).strip().upper()
        q = _position_qty(pos)
        if sym and q is not None and abs(float(q)) > 1e-12:
            open_syms.add(sym)

    disallow = set(open_syms) - closed_preview
    _log(
        f"PM(near_median): dry-run preview — hypothetical post-close disallow set size={len(disallow)} "
        f"(removed {len(closed_preview)} close-leg symbol(s) from current {len(open_syms)} open)."
    )

    _log("PM(near_median): dry-run preview — refreshing strategy feed (venue mark → Varibot JSON)...")
    try:
        listing_json, _ = _prepare_varibot_strategy_feed(ep, args=args)

        top_n = _top_n_for_strategy(strat_key)
        longs, shorts, meta = run_strategy_pick_tickers(
            strategy_key=strat_key,
            listing_json=listing_json,
            top_n=top_n,
            args=args,
        )
        need_each_side = int(n_pairs)
        repl_l, repl_s = filter_replacements(
            longs=longs,
            shorts=shorts,
            disallow=disallow,
            need_each_side=need_each_side,
        )
        got_l, got_s = len(repl_l), len(repl_s)
        repl_l, repl_s, n_do = _near_median_align_pair_candidates(
            repl_l, repl_s, wanted_pairs=need_each_side
        )
        if n_do <= 0:
            _log(
                f"PM(near_median): dry-run preview — insufficient replacements "
                f"(wanted {need_each_side} pair(s); got {got_l}L/{got_s}S after filter)."
            )
            return
        if n_do < need_each_side:
            _log(
                f"PM(near_median): dry-run preview — partial replacements "
                f"(wanted {need_each_side} pair(s), simulating {n_do}; candidates {got_l}L/{got_s}S)."
            )

        _log(
            f"PM(near_median): dry-run preview — multimarketorder (no --live) for sizing — "
            f"strategy={meta.get('strategy')} pairs={n_do}"
        )
        pos_n = _positions_notional_usd(positions_raw)
        usd_run = _near_median_pm_usd_for_multimarket(
            snap=snap,
            args=args,
            jobs_tag="dry-run preview",
            book_pos_notional_usd=float(pos_n),
        )
        if usd_run is None:
            return
        run_multimarket(
            multi_script=str(args.multi_script),
            longs=repl_l,
            shorts=repl_s,
            usd=float(usd_run),
            live=False,
        )
    except Exception as e:
        _log(f"PM(near_median): dry-run preview error: {type(e).__name__}: {e}")


def _near_median_pm_manager(
    *,
    ep: VariEndpoints,
    cfg: Any,
    args: argparse.Namespace,
    positions_raw: Any,
    snap: Any,
) -> None:
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    strat_norm = _strategy_key_normalized(strat_key)
    if strat_norm != "invert_extreme":
        return

    rows = positions_to_rows(positions_raw)
    if not rows:
        return

    # This branch uses individual-leg TP/SL monitoring (no pairing):
    # - close legs with upnl_pct >= +5% or <= -10% (thresholds are defaults below)
    tp_pct = float(PM_LEG_TP_THRESHOLD_PCT_DEFAULT)
    sl_pct = float(PM_LEG_SL_THRESHOLD_PCT_DEFAULT)
    legs = select_legs_to_close(rows=rows, tp_pct=tp_pct, sl_pct=sl_pct)
    if not legs:
        _log(f"PM(invert_extreme): no eligible legs to close (tp>={tp_pct:g}%, sl<=-{sl_pct:g}%).")
        return

    _log(f"PM(invert_extreme): {len(legs)} leg(s) to close (tp>={tp_pct:g}%, sl<=-{sl_pct:g}%).")
    for x in legs:
        _log(
            f"  close_if_live: {x.side} {x.ticker} upnl%={x.upnl_pct:.3f}% "
            f"(${x.upnl_usd:,.2f} / ${x.value_usd:,.2f}) reason={x.reason}"
        )

    if not bool(args.live):
        _log("PM(invert_extreme): dry-run (not live) — would close legs and (optionally) replace.")
        return

    # Build qty lookup once.
    by_sym: Dict[str, float] = {}
    for pos in _positions_list(positions_raw):
        sym = _instrument_label(pos).strip().upper()
        q = _position_qty(pos)
        if sym and q is not None and abs(float(q)) > 1e-12:
            by_sym[sym] = float(q)

    max_slip = _resolve_max_slippage()
    closed_syms: Set[str] = set()
    closed_long_n = 0
    closed_short_n = 0

    for x in legs:
        sym = str(x.ticker).strip().upper()
        q = by_sym.get(sym)
        if q is None or abs(float(q)) <= 1e-12:
            _log(f"PM(invert_extreme): skip close {sym} (no open qty found).")
            continue
        close_side = "sell" if float(q) > 0 else "buy"
        qty_abs = abs(float(q))
        _log(f"PM(invert_extreme): closing {sym} qty={qty_abs:g} side={close_side} reduce-only...")
        _close_reduce_only_with_slippage_steps(
            ep=ep,
            sym=sym,
            qty_abs=float(qty_abs),
            close_side=str(close_side),
            max_slip=float(max_slip),
        )
        closed_syms.add(sym)
        if x.side == "L":
            closed_long_n += 1
        else:
            closed_short_n += 1

    # Replacement: open new tickers in the SAME direction as closed legs, avoiding:
    # - symbols closed earlier in this interval check
    # - currently open symbols after closes
    refill_on = bool(getattr(args, "pm_refill", False)) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        # Default behavior for this branch: refill unless explicitly disabled.
        refill_on = bool(PM_REFILL_DEFAULT_ON) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        return

    try:
        raw_pf_after = ep.get_portfolio(compute_margin=True)
        snap_after = parse_portfolio_snapshot(raw_pf_after)
        open_after = ep.get_positions()
        pos_n_after = _positions_notional_usd(open_after)
    except Exception:
        _log("PM(invert_extreme): skip replacements — could not fetch portfolio/positions after closes.")
        return

    pv_after = getattr(snap_after, "portfolio_value_usd", None)
    if pv_after is None or float(pv_after) <= 0:
        _log("PM(invert_extreme): skip replacements — post-close portfolio_value_usd missing or non-positive.")
        return

    lev_m = _multimarket_effective_leverage()
    leg_refill = (
        float(args.usd)
        if args.usd is not None
        else _near_median_slot_usd_per_leg(
            portfolio_value_usd=float(pv_after),
            leverage=int(lev_m),
        )
    )
    im_usage_after = getattr(snap_after, "im_usage", None)
    if im_usage_after is None:
        _log("PM(invert_extreme): skip replacements — im_usage missing; cannot enforce IM hard cap.")
        return
    cap_ratio = float(DEFAULT_IM_TARGET_PCT) / 100.0
    if float(im_usage_after) >= cap_ratio:
        _log(
            f"PM(invert_extreme): skip replacements — IM hard cap "
            f"(IM%={float(im_usage_after) * 100.0:.2f}%; cap {float(DEFAULT_IM_TARGET_PCT):g}%)."
        )
        return
    usd_run = float(leg_refill)

    # Refresh strategy listing snapshot (venue mark) for replacement picks.
    _log("PM(invert_extreme): refreshing strategy feed for replacements...")
    listing_json, _ = _prepare_varibot_strategy_feed(ep, args=args)

    # Disallow: closed this cycle + currently open positions (post-close).
    open_syms: Set[str] = set()
    for pos in _positions_list(open_after):
        sym = _instrument_label(pos).strip().upper()
        q = _position_qty(pos)
        if sym and q is not None and abs(float(q)) > 1e-12:
            open_syms.add(sym)
    disallow = set(open_syms) | set(closed_syms)

    top_n = _top_n_for_strategy(strat_key)
    longs, shorts, meta = run_strategy_pick_tickers(
        strategy_key=strat_key, listing_json=listing_json, top_n=top_n, args=args
    )

    # Pick per-side replacements.
    repl_l = filter_replacements_one_side(candidates=longs, disallow=disallow, need=int(closed_long_n))
    repl_s = filter_replacements_one_side(candidates=shorts, disallow=disallow, need=int(closed_short_n))

    # Never open more replacement tickers than slots remaining under max book size (e.g. 60).
    # If both long and short refills were planned but only one slot remains, keep the candidate
    # with larger abs(7d change %) from strategy listing JSON (tie: earlier in sorted list wins).
    max_open_tickers = int(INVERT_EXTREME_MAX_TICKER_ENTRIES)
    slot_budget = max(0, max_open_tickers - len(open_syms))
    planned_n = len(repl_l) + len(repl_s)
    if planned_n > slot_budget:
        abs7_map = _ticker_abs_7d_from_listing_json(str(listing_json))

        def _abs7_score(sym: str) -> float:
            v = abs7_map.get(str(sym).strip().upper())
            return float(v) if v is not None else -1.0

        tagged: List[Tuple[str, str]] = [("L", str(s).strip().upper()) for s in repl_l] + [
            ("S", str(s).strip().upper()) for s in repl_s
        ]
        tagged.sort(key=lambda t: _abs7_score(t[1]), reverse=True)
        kept_l: List[str] = []
        kept_s: List[str] = []
        seen: Set[str] = set()
        for side, sym_u in tagged:
            if not sym_u or sym_u in seen:
                continue
            seen.add(sym_u)
            if side == "L":
                kept_l.append(sym_u)
            else:
                kept_s.append(sym_u)
            if len(kept_l) + len(kept_s) >= slot_budget:
                break
        repl_l, repl_s = kept_l, kept_s
        _log(
            f"PM(invert_extreme): replacement slot cap — budget={slot_budget}/{max_open_tickers} "
            f"(planned was {planned_n}); |7d| ranked — L={repl_l} S={repl_s}"
        )

    if closed_long_n and not repl_l:
        _log(f"PM(invert_extreme): WARNING no long replacements (closed_long={closed_long_n}).")
    if closed_short_n and not repl_s:
        _log(f"PM(invert_extreme): WARNING no short replacements (closed_short={closed_short_n}).")

    if repl_l:
        _log(f"PM(invert_extreme): opening long replacements n={len(repl_l)} (strategy={meta.get('strategy')})...")
        rc_l = run_multimarket(
            multi_script=str(args.multi_script),
            longs=repl_l,
            shorts=[],
            usd=float(usd_run),
            live=bool(args.live),
        )
        if bool(args.live) and int(rc_l) == 0:
            _log_post_multimarket_positions_tally(ep=ep, longs=repl_l, shorts=[])
    if repl_s:
        _log(f"PM(invert_extreme): opening short replacements n={len(repl_s)} (strategy={meta.get('strategy')})...")
        rc_s = run_multimarket(
            multi_script=str(args.multi_script),
            longs=[],
            shorts=repl_s,
            usd=float(usd_run),
            live=bool(args.live),
        )
        if bool(args.live) and int(rc_s) == 0:
            _log_post_multimarket_positions_tally(ep=ep, longs=[], shorts=repl_s)


def _invert_extreme_topup_if_needed(
    *,
    ep: VariEndpoints,
    args: argparse.Namespace,
    snap: Any,
    positions_raw: Any,
    cycle_index: int = 0,
) -> None:
    """
    Top-up (invert_extreme only): fill empty ticker slots up to DEFAULT_MAX_TICKER_ENTRIES.

    This is intentionally NON-PAIRED and can be lopsided (strategy picks are lopsided).
    We enforce the same IM hard cap as other entry paths: if snap.im_usage >= DEFAULT_IM_TARGET_PCT,
    do not add new exposure.
    """
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    strat_norm = _strategy_key_normalized(strat_key)
    if strat_norm != "invert_extreme":
        return

    pv = getattr(snap, "portfolio_value_usd", None)
    if pv is None or float(pv) <= 0:
        _log("PM(invert_extreme): skip top-up — portfolio_value_usd missing or non-positive.")
        return

    rows = positions_to_rows(positions_raw)
    if not rows:
        return

    target_total = int(INVERT_EXTREME_MAX_TICKER_ENTRIES)
    if target_total <= 0:
        return
    cur_total = len(rows)
    cur_long = sum(1 for r in rows if r.side == "L")
    cur_short = sum(1 for r in rows if r.side == "S")
    slots_total = max(0, int(target_total) - int(cur_total))

    if int(cycle_index) == 1:
        syms = sorted({str(r.ticker).strip().upper() for r in rows if r.ticker})
        preview = ", ".join(syms[:24])
        if len(syms) > 24:
            preview += f", … (+{len(syms) - 24} more)"
        _log(
            f"PM(invert_extreme): init — loaded open book tickers={cur_total} "
            f"(L={cur_long}, S={cur_short}): {preview}"
        )

    _log(
        f"PM(invert_extreme): book snapshot — tickers={cur_total}/{target_total} "
        f"L={cur_long} S={cur_short}; slots_total={slots_total}."
    )

    im_usage = getattr(snap, "im_usage", None)
    if im_usage is None:
        _log("PM(invert_extreme): skip top-up — im_usage missing; cannot enforce IM hard cap.")
        return
    cap_ratio = float(DEFAULT_IM_TARGET_PCT) / 100.0
    if float(im_usage) >= cap_ratio:
        if int(cycle_index) == 1:
            _log(
                "PM(invert_extreme): init — no top-up: IM at or above portfolio target "
                f"({float(DEFAULT_IM_TARGET_PCT):g}%); book snapshot above. Next cycles same checks apply."
            )
        else:
            _log(
                "PM(invert_extreme): skip top-up — IM hard cap "
                f"(IM%={float(im_usage) * 100.0:.2f}%; cap {float(DEFAULT_IM_TARGET_PCT):g}%)."
            )
        return

    if cur_total >= target_total:
        _log(
            f"PM(invert_extreme): skip top-up — book at max tickers ({cur_total}/{target_total}; "
            f"DEFAULT_MAX_TICKER_ENTRIES)."
        )
        return

    if int(slots_total) <= 0:
        _log("PM(invert_extreme): skip top-up — no slots needed (book already at target).")
        return

    open_syms: Set[str] = {r.ticker.strip().upper() for r in rows if r.ticker}
    _log(
        f"PM(invert_extreme): top-up plan — current={cur_total}/{target_total} "
        f"(L={cur_long}, S={cur_short}); slots_total={slots_total} (non-paired; follow strategy picks)."
    )

    if not bool(args.live):
        _log(
            "PM(invert_extreme): dry-run — refreshing strategy feed + strategy picks + multimarketorder "
            "(no --live; prints sizing line and per-ticker quote dry-run)."
        )

    listing_json, _ = _prepare_varibot_strategy_feed(ep, args=args)

    top_n = _top_n_for_strategy(strat_key)
    longs, shorts, meta = run_strategy_pick_tickers(
        strategy_key=strat_key, listing_json=listing_json, top_n=top_n, args=args
    )
    disallow = set(open_syms)

    # Fill by global priority: biggest abs(7d chg%) first (ties: keep strategy order/ticker).
    abs7 = _ticker_abs_7d_from_listing_json(str(listing_json))
    # (abs7, seq, side, sym) so ties keep strategy-list ordering.
    combined: List[Tuple[float, int, str, str]] = []
    seq = 0
    for t in longs:
        sym = str(t).strip().upper()
        if sym:
            combined.append((float(abs7.get(sym, 0.0)), seq, "L", sym))
            seq += 1
    for t in shorts:
        sym = str(t).strip().upper()
        if sym:
            combined.append((float(abs7.get(sym, 0.0)), seq, "S", sym))
            seq += 1
    combined.sort(key=lambda x: (-float(x[0]), int(x[1])))

    add_l: List[str] = []
    add_s: List[str] = []
    for _, __, side, sym in combined:
        if sym in disallow:
            continue
        if side == "L":
            add_l.append(sym)
        else:
            add_s.append(sym)
        disallow.add(sym)
        if len(add_l) + len(add_s) >= int(slots_total):
            break
    n_open = int(len(add_l) + len(add_s))
    if n_open <= 0:
        _log(
            "PM(invert_extreme): top-up skipped — insufficient new tickers "
            f"(slots_total={slots_total}; got {len(add_l)}L/{len(add_s)}S after filter)."
        )
        return

    _log(
        f"PM(invert_extreme): topping up with n={n_open} (non-paired) "
        f"(strategy={meta.get('strategy')}) longs={add_l} shorts={add_s}"
    )
    usd_run = _near_median_pm_usd_for_multimarket(
        snap=snap,
        args=args,
        jobs_tag="top-up",
    )
    if usd_run is None:
        return
    rc_mm = run_multimarket(
        multi_script=str(args.multi_script),
        longs=add_l,
        shorts=add_s,
        usd=float(usd_run),
        live=bool(args.live),
    )
    if bool(args.live) and int(rc_mm) == 0:
        _log_post_multimarket_positions_tally(ep=ep, longs=add_l, shorts=add_s)
    if bool(args.live):
        _near_median_substitute_skew_rejected_multimarket(
            ep=ep,
            args=args,
            strat_key=strat_key,
            listing_json=listing_json,
            jobs_tag="PM top-up",
            usd=float(usd_run),
        )


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

    cycle_index is the 1-based cycle counter from the main loop (used for near_median init logging).
    """
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
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
    if has_pos:
        rebalance_dry = bool(getattr(args, "rebalance_dry_run", False)) or not bool(args.live)
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
                _grid_mark_for_asset(ep, asset=sym, bulk_map=cycle_bulk_marks)
            ),
            varibot_dir=_VARIBOT_DIR,
        )

    grid_ignore_pos = _gridstrat_ignores_venue_positions(strat_key)

    if not has_pos or grid_ignore_pos:
        _clear_position_latch()
        if grid_ignore_pos and has_pos:
            _log(
                "gridstrat: ignoring venue open positions (GRIDSTRAT_IGNORE_VENUE_POSITIONS) — "
                "flat / fresh-book grid path"
            )
        else:
            _log("No open positions -> venue listing snapshot -> strategy -> multimarket")
        marks_src = _grid_marks_source()
        _log(f"step: refreshing strategy feed ({marks_src} mark → Varibot JSON)...")
        grid_marks_once: Dict[str, float] = {}
        if _is_grid_like_strategy(strat_key) and bool(args.live):
            grid_marks_once = _fetch_grid_marks_for_assets(
                ep, grid_trading_ticker_band_pcts().keys(), bulk_map=cycle_bulk_marks
            )
        listing_json, _ = _prepare_varibot_strategy_feed(
            ep, args=args, grid_marks=grid_marks_once or None, bulk_map=cycle_bulk_marks
        )
        top_n = _top_n_for_strategy(strat_key)
        marks_by: Dict[str, float] = {}
        pending_by: Dict[str, Set[Tuple[str, str]]] = {}
        flat_by: Dict[str, bool] = {}
        cycle_pending_by: Optional[Dict[str, Set[Tuple[str, str]]]] = None
        if _is_grid_like_strategy(strat_key):
            cycle_pending_by = _fetch_cycle_pending_by_asset(
                ep, assets=grid_trading_ticker_band_pcts().keys()
            )
        if _is_grid_like_strategy(strat_key):
            marks_by, pending_by, flat_by = _grid_venue_inputs_for_cycle(
                ep,
                args=args,
                positions_raw=raw_pos,
                ignore_venue_positions=grid_ignore_pos,
                venue_marks_by_asset=grid_marks_once or None,
                venue_pending_by_asset=cycle_pending_by,
                bulk_map=cycle_bulk_marks,
            )
            for sym, mk in sorted(marks_by.items()):
                _log(f"step: venue mark {sym}={mk:g} ({marks_src})")
        longs, shorts, meta = run_strategy_pick_tickers(
            strategy_key=strat_key,
            listing_json=listing_json,
            top_n=top_n,
            args=args,
            venue_marks_by_asset=marks_by or None,
            venue_pending_by_asset=pending_by or None,
            account_flat_by_asset=flat_by or None,
            account_flat=True,
        )
        _log("step: strategy feed ready")
        _log(f"step: strategy finished (strategy={meta.get('strategy')}, longs={len(longs)}, shorts={len(shorts)})")
        if meta.get("grid_fresh_flat_start"):
            _log("gridstrat: fresh flat session — symmetric paired ladder reinit at current mark")
        if _meta_flag_any_asset(meta, "grid_flat_inventory_rebalance"):
            _log(
                "gridstrat: flat inventory — rebalancing buy/sell limits symmetrically around venue mark "
                "(drift reconcile will cancel lopsided venue limits)"
            )
        if meta.get("grid_paired_limit_mode"):
            _log_gridstrat_paired_step(meta, prefix="gridstrat")

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

        if meta.get("grid_mode"):
            n_ev = len(meta.get("grid_market_events") or [])
            _log(f"step: gridstrat grid_mode events={n_ev}")
            if meta.get("grid_paired_limit_mode"):
                _log_gridstrat_step_summary(meta, positions_label="flat")
            # Prevent double-posting: if gridstrat emitted limit events this cycle, let those run
            # and skip remnant reconcile (which may otherwise post the same rung concurrently).
            if n_ev == 0:
                _run_grid_limits_bootstrap_if_grid(
                    ep=ep,
                    meta=meta,
                    args=args,
                    cycle_index=cycle_index,
                    has_positions=bool(has_pos and not grid_ignore_pos),
                    pending_by_asset=cycle_pending_by,
                )
            else:
                _log("gridlimits reconcile: skip (gridstrat emitted events this cycle).")
            _execute_grid_market_events(meta, args=args)
            return False

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
        if _strategy_key_normalized(strat_key) == "invert_extreme" and bool(args.live):
            _near_median_substitute_skew_rejected_multimarket(
                ep=ep,
                args=args,
                strat_key=strat_key,
                listing_json=listing_json,
                jobs_tag="flat entry",
                usd=float(args.usd) if args.usd is not None else None,
            )
        return False

    # --- have positions ---
    strat_norm = _strategy_key_normalized(strat_key)

    if _is_grid_like_strategy(strat_key) and not grid_ignore_pos:
        try:
            grid_marks_pos = (
                _fetch_grid_marks_for_assets(
                    ep, grid_trading_ticker_band_pcts().keys(), bulk_map=cycle_bulk_marks
                )
                if bool(args.live)
                else {}
            )
            listing_grid, _ = _prepare_varibot_strategy_feed(
                ep, args=args, grid_marks=grid_marks_pos or None, bulk_map=cycle_bulk_marks
            )
            top_n_g = _top_n_for_strategy(strat_key)
            cycle_pending_pos = _fetch_cycle_pending_by_asset(
                ep, assets=grid_trading_ticker_band_pcts().keys()
            )
            marks_by, pending_by, flat_by = _grid_venue_inputs_for_cycle(
                ep,
                args=args,
                positions_raw=raw_pos,
                ignore_venue_positions=False,
                venue_marks_by_asset=grid_marks_pos or None,
                venue_pending_by_asset=cycle_pending_pos,
                bulk_map=cycle_bulk_marks,
            )
            _, _, meta_g = run_strategy_pick_tickers(
                strategy_key=strat_key,
                listing_json=listing_grid,
                top_n=top_n_g,
                args=args,
                venue_marks_by_asset=marks_by,
                venue_pending_by_asset=pending_by,
                account_flat_by_asset=flat_by,
                account_flat=False,
            )
            if meta_g.get("grid_mode"):
                n_ev = len(meta_g.get("grid_market_events") or [])
                if n_ev:
                    _log(f"gridstrat (open positions): events={n_ev}")
                if meta_g.get("grid_fresh_flat_start"):
                    _log("gridstrat (open positions): fresh flat reinit skipped — has position")
                if meta_g.get("grid_paired_limit_mode"):
                    _log_gridstrat_step_summary(meta_g, positions_label="open positions")
                    _log_gridstrat_paired_step(meta_g, prefix="gridstrat")
                if n_ev == 0:
                    _run_grid_limits_bootstrap_if_grid(
                        ep=ep,
                        meta=meta_g,
                        args=args,
                        cycle_index=cycle_index,
                        has_positions=True,
                        pending_by_asset=cycle_pending_pos,
                    )
                else:
                    _log("gridlimits reconcile: skip (gridstrat emitted events this cycle).")
                _execute_grid_market_events(meta_g, args=args)
                return False
        except Exception as e:
            _log(f"WARNING: gridstrat cycle with open positions ({type(e).__name__}: {e})")

    # Log oldest open position (helps sanity-check time-kill / holds).
    if strat_norm == "invert_extreme":
        oldest = _oldest_position_summary(raw_pos, now_ts=time.time())
        if oldest is not None:
            sym, age_s = oldest
            _log(f"oldest_pos= {sym} ({_format_duration_s(age_s)})")

    # Hard time-kill (invert_extreme only): if the oldest open position is too old, close all.
    if strat_norm == "invert_extreme":
        kill_hours = float(TIME_KILL_POSITION_HOURS)
        try:
            v = (os.getenv("VARIBOT_TIME_KILL_POSITION_HOURS", "") or "").strip()
            if v:
                kill_hours = float(v)
        except Exception:
            pass
        if kill_hours > 0:
            kill_s = float(kill_hours) * 3600.0
            candidates = _positions_time_kill_candidates(
                positions_raw=raw_pos,
                now_ts=time.time(),
                kill_after_s=kill_s,
            )
            if candidates:
                _log(
                    f"time-kill: {len(candidates)} position(s) age >= {_format_duration_s(kill_s)} "
                    f"(hours={kill_hours:g}); "
                    f"{'closing reduce-only (LIVE)' if args.live else 'dry-run only'}..."
                )
                if bool(args.live):
                    max_slip = _resolve_max_slippage()
                    for sym, qty, age_s in candidates:
                        close_side = "sell" if float(qty) > 0 else "buy"
                        qty_abs = abs(float(qty))
                        try:
                            _log(
                                f"time-kill: close {sym} qty={qty:g} age={_format_duration_s(age_s)} "
                                f"(reduce-only {close_side} qty_abs={qty_abs:g})"
                            )
                            _close_reduce_only_with_slippage_steps(
                                ep=ep,
                                sym=sym,
                                qty_abs=qty_abs,
                                close_side=close_side,
                                max_slip=float(max_slip),
                            )
                        except Exception as e:
                            _log(f"time-kill: close {sym} failed ({type(e).__name__}: {e})")
                    # Refresh book before PM/top-up logic.
                    try:
                        raw_pos = ep.get_positions()
                    except Exception as e:
                        _log(f"WARNING: time-kill post-close positions refresh failed ({type(e).__name__}: {e})")

    # 1) PM(near_median): per-pair threshold exits / refill (before top-up IM checks).
    _near_median_pm_manager(ep=ep, cfg=cfg, args=args, positions_raw=raw_pos, snap=snap)

    # 2) Live PM closes change the book — refresh snapshot before top-up / other managers.
    if strat_norm == "invert_extreme" and bool(args.live):
        try:
            raw_pf = ep.get_portfolio(compute_margin=True)
            snap = parse_portfolio_snapshot(raw_pf)
            raw_pos = ep.get_positions()
        except Exception as e:
            _log(f"WARNING: post-PM portfolio refresh failed ({type(e).__name__}: {e}); using stale snapshot for rest of cycle.")

    # 3) Top-up only after PM(pair-threshold) step above (uses refreshed snap when live).
    _invert_extreme_topup_if_needed(
        ep=ep, args=args, snap=snap, positions_raw=raw_pos, cycle_index=int(cycle_index)
    )

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
        help=f"Strategy key to use when flat (default {Strategy!r}; see strategy/gridstrat.py).",
    )
    p.add_argument(
        "--grid-band-pct",
        type=float,
        default=None,
        dest="grid_band_pct",
        metavar="PCT",
        help=(
            "Grid only: set GRID_BAND_PCT for this process (symmetric ±%% bracket when "
            "GRID_LOWER/GRID_UPPER are not both set; see strategy/gridstrat.py)."
        ),
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
            "(notional from --usd if set, else --mm-probe-usd), pass --skip-im-hard-cap to the child script, "
            "then run invert_extreme skew substitution if .multimarket_last_result.json lists rejects."
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
    if getattr(args, "grid_band_pct", None) is not None:
        os.environ["GRID_BAND_PCT"] = str(float(args.grid_band_pct))
    probe_ticker = (getattr(args, "mm_probe_short", None) or "").strip()
    if probe_ticker:
        if not bool(args.live):
            print("varibot: --mm-probe-short requires --live (places a real order).", file=sys.stderr)
            return 2
        run_auth_or_exit()
        cfg, ep = build_endpoints()
        usd_probe = float(args.usd) if args.usd is not None else float(getattr(args, "mm_probe_usd", 100.0) or 100.0)
        listing_json, _ = _prepare_varibot_strategy_feed(ep, args=args, asset_hint=sym_u)
        strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
        sym_u = probe_ticker.upper()
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
        _near_median_substitute_skew_rejected_multimarket(
            ep=ep,
            args=args,
            strat_key=strat_key,
            listing_json=str(listing_json),
            jobs_tag="mm_probe_short",
            usd=float(usd_probe),
        )
        return int(rc_mm)

    if args.usd is not None and args.im_target_pct is not None:
        print("varibot: pass at most one of --usd and --im-target-pct.", file=sys.stderr)
        return 2
    run_auth_or_exit()
    cfg, ep = build_endpoints()

    # Session mode (strategies in STRATEGY_SESSION_CLOSEALL_KEYS):
    # - Enter immediately (strategy → multimarket if flat)
    # - While holding, run TP/PnL check every CHECK_INTERVAL_MIN minutes
    # - Close all at the next wall multiple of STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN
    # - Sleep 15 seconds, then restart (enter again)
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    strat_norm = _strategy_key_normalized(strat_key)
    if strat_norm in STRATEGY_SESSION_CLOSEALL_KEYS:
        session_n = 0
        while True:
            session_n += 1
            _log(
                f"=== session {session_n} (strategy={strat_norm}, "
                f"tp_check_interval_min={int(CHECK_INTERVAL_MIN)}, "
                f"closeall_wall_interval_min={int(STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN)}, "
                f"live={bool(args.live)}) ==="
            )
            _log("session: entering now (ignoring schedule)...")
            try:
                one_cycle(ep=ep, cfg=cfg, args=args)
            except Exception as e:
                _log(f"session enter error: {type(e).__name__}: {e}")
                return 1

            # Hold loop: TP check cadence while waiting for wall-clock close-all.
            close_delay = seconds_until_next_wall_interval(
                period_minutes=int(STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN)
            )
            close_at = time.time() + float(close_delay)
            _log(
                f"session: next close-all (wall {int(STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN)}m) in "
                f"{_format_duration_s(close_delay)} at {_format_wake_at_sgt(close_delay)} SGT"
            )

            while True:
                remaining = float(close_at - time.time())
                if remaining <= 0:
                    break

                # If positions are already flat (e.g. TP close fired), restart quickly.
                try:
                    if not has_open_positions(ep.get_positions()):
                        _log("session: positions flat before scheduled close-all → restart in 15s")
                        time.sleep(_TIME_IN_POSITION_POST_CLOSE_SLEEP_S)
                        break
                except Exception:
                    pass

                step_s = float(int(CHECK_INTERVAL_MIN) * 60)
                sleep_s = min(step_s, remaining)
                _log(f"session: next TP/PnL check in {_format_duration_s(sleep_s)}")
                time.sleep(max(1.0, sleep_s))

                # Run one_cycle while holding (it will only do TP check / management when positions exist).
                try:
                    one_cycle(ep=ep, cfg=cfg, args=args)
                except Exception as e:
                    _log(f"session check error: {type(e).__name__}: {e}")
                    if not bool(args.live):
                        return 1

            # If we broke out early due to TP flatten, restart next session.
            try:
                if not has_open_positions(ep.get_positions()):
                    if not bool(args.live):
                        return 0
                    continue
            except Exception:
                pass

            _log(
                f"session: closing all at {int(STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN)}m wall boundary "
                f"({'LIVE' if args.live else 'dry-run'})..."
            )
            try:
                rc = run_closeallpositions(live=bool(args.live))
                if rc != 0:
                    _log(f"closeallpositions exited {rc}")
            except Exception as e:
                _log(f"session close error: {type(e).__name__}: {e}")
                return 1

            if not bool(args.live):
                return 0
            time.sleep(_TIME_IN_POSITION_POST_CLOSE_SLEEP_S)

    cycle_n = 0
    while True:
        cycle_n += 1
        _log(f"=== cycle {cycle_n} ===")
        try:
            ti_just_closed = one_cycle(ep=ep, cfg=cfg, args=args, cycle_index=cycle_n)
        except Exception as e:
            _log(f"cycle error: {type(e).__name__}: {e}")
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
