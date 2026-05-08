from __future__ import annotations

"""
Varibot orchestrator — implements the VariBotFlowchart workflow:

  Auth (validate_vr_token) -> every T minutes: portfolio snapshot ->
  if positions -> PM / managers (see strategy); portfolio-wide TP close-all removed ->
  if flat -> listingtable -> marketstate -> strategy -> multimarketorder
  (strategy funding_pairs skips marketstate on entry; per-pair exits in funding_pairs manager.)

Run from the Varibot directory (or any cwd; this file fixes imports):

  cd .../Varibot && python3 varibot.py
  python3 varibot.py --live              # default: IM-target sizing (see multimarketorder.DEFAULT_IM_TARGET_PCT)
  python3 varibot.py --usd 20            # fixed USD per order instead

Dependencies: repo layout
  ../Vari Listings/listingtable.py, marketstate.py, *.json
  ../strategy/ (strategy modules), ./validate_vr_token.py, check_portfolio_stats.py,
  ./closeallpositions.py, ./multimarketorder.py (or *_cadence_1s.py)
"""

import argparse
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
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

# Imports assume sibling scripts + variationalbot live under this directory.
_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
_LISTINGS_DIR = os.path.join(_REPO_ROOT, "Vari Listings")
_DEFAULT_MARKETSTATE_JSON = os.path.join(_LISTINGS_DIR, "marketstate.json")
_POSITION_LATCH_PATH = os.path.join(_VARIBOT_DIR, ".varibot_position_latch.json")

# Check interval (minutes) between cycles/sessions when --period-min is not provided.
CHECK_INTERVAL_MIN: int = 15

# --- User-tunable settings (surface here for quick edits) ---
#
# Portfolio Manager (PM) for strategy near_median:
# - Each CHECK_INTERVAL_MIN, PM can close (long,short) pairs when combined uPnL% clears a threshold.
# - Default threshold: portfolio_manager_pairs.PAIR_TP_THRESHOLD_PCT_DEFAULT (imported below as PM_PAIR_...).
# - Optionally refill closed slots by refreshing listingtable (pro) and opening replacements.
PM_REFILL_DEFAULT_ON: bool = True

# Strategies that use the "session" loop in _child_main: enter now, TP checks on CHECK_INTERVAL_MIN cadence,
# then close-all at the next wall multiple of STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN (see seconds_until_next_wall_interval).
#
# NOTE (this branch): `near_median` no longer uses wall-clock close-all; exits are driven by the portfolio manager.
STRATEGY_SESSION_CLOSEALL_KEYS: frozenset[str] = frozenset({"revert_near_median"})
STRATEGY_SESSION_CLOSEALL_INTERVAL_MIN: int = 360

_TIME_IN_POSITION_POST_CLOSE_SLEEP_S: float = 15.0 # after a live time-in-position close, sleep this long then start the next cycle (skip wall-clock wait)
# funding_pairs manager: refresh listingtable before opening replacement pairs
_FP_REFRESH_LISTINGTABLE_ENV: str = "VARIBOT_FUNDING_PAIRS_REFRESH_LISTINGTABLE_ON_ROTATE"
_FP_REFRESH_MIN_AGE_S_ENV: str = "VARIBOT_FUNDING_PAIRS_REFRESH_LISTINGTABLE_MIN_AGE_S"
_FP_REFRESH_DEFAULT_ON: bool = True
_FP_REFRESH_DEFAULT_MIN_AGE_S: float = 300.0  # refresh if listingtabledata.json older than 5 minutes

# User setting: which strategy to run when flat.
# You can put a module name (preferred) or a filename:
#   "invert_extreme" or "invert_extreme.py"
Strategy: str = os.getenv("VARIBOT_STRATEGY", "invert_extreme.py").strip()
if not Strategy:
    Strategy = "revert_near_median.py"

# Rolling log (wrapper mode): varibot.py can self-wrap to prefix lines and keep a rolling logfile,
# so you can run just `python3 varibot.py --live` and still get the run_varibot_logged behavior.
_VARIBOT_LOG_MAX_LINES: int = 1000
_VARIBOT_WRAPPED_ENV: str = "VARIBOT_WRAPPED"

# Post-multimarket verification: /api/positions can lag behind fills by a second or two.
POST_MULTIMARKET_POSITIONS_MAX_WAIT_S: float = 2.0
POST_MULTIMARKET_POSITIONS_POLL_S: float = 0.5

# Reduce-only pair closes (PM near_median, funding_pairs; _close_reduce_only_with_slippage_steps).
# Same defaults as multimarketorder.py (_DEFAULT_MAX_SLIPPAGE / _SLIPPAGE_RETRY_INCREMENT / _MAX_LIVE_ATTEMPTS).
# Default max slippage when MAX_SLIPPAGE env is unset (fraction of notional).
_DEFAULT_MAX_SLIPPAGE: float = 0.0005

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
from positions import _instrument_label  # noqa: E402
from validate_vr_token import validate_vr_token  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402

from multimarketorder import (  # noqa: E402
    DEFAULT_IM_TARGET_PCT,
    DEFAULT_LEVERAGE as MULTIMARKET_DEFAULT_LEVERAGE,
    MULTIMARKET_LAST_RESULT_JSON,
    USD_NOTIONAL_ROUND_STEP,
    _order_response_rejected,
)
from strategy import strategies as strategies_mod  # noqa: E402
from strategy import funding_pairs as funding_pairs_mod  # noqa: E402
from strategy.invert_extreme import (  # noqa: E402
    DEFAULT_MAX_TICKER_ENTRIES as INVERT_EXTREME_MAX_TICKER_ENTRIES,
)
from portfolio_manager_pairs import (
    PAIR_TP_THRESHOLD_PCT_DEFAULT as PM_PAIR_TP_THRESHOLD_PCT_DEFAULT,
    PairCandidate,
    filter_replacements,
    positions_to_rows,
    scan_best_winner_opposite_pair,
    select_pairs_greedy_grid,
)  # noqa: E402


def _strategy_key_normalized(strategy: str) -> str:
    k = (strategy or "").strip().lower()
    if k.endswith(".py"):
        k = k[:-3]
    return k


def _resolve_im_target_pct_for_multimarket(
    *,
    strategy_key: str,
    n_long: int,
    n_short: int,
    args_im_target_pct: Optional[float],
) -> float:
    """
    For strategy funding_pairs, multimarketorder's --im-target-pct is derived so each pair uses
    funding_pairs.FUNDING_PAIR_MAX_IM_PCT of (portfolio_value × leverage), split across two legs.
    Other strategies use --im-target-pct or DEFAULT_IM_TARGET_PCT.
    """
    if _strategy_key_normalized(strategy_key) == "funding_pairs":
        n = int(n_long) + int(n_short)
        if n <= 0:
            return float(args_im_target_pct) if args_im_target_pct is not None else float(DEFAULT_IM_TARGET_PCT)
        return float(
            funding_pairs_mod.im_target_pct_for_funding_pairs_multimarket(int(n_long), int(n_short))
        )
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
    IM gap uses snap.im_usage when present; else pos_notional / (pv × leverage).
    Pair-count vs IM budget: $/pair = 2 × usd_per_leg; max_new_pairs ≈ avail / $/pair.
    """
    if args.usd is not None:
        return float(args.usd)

    pv = getattr(snap, "portfolio_value_usd", None)
    if pv is None or float(pv) <= 0:
        _log(f"PM(near_median): skip {jobs_tag} — portfolio_value_usd missing or non-positive.")
        return None

    lev = _multimarket_effective_leverage()
    slot = _near_median_slot_usd_per_leg(portfolio_value_usd=float(pv), leverage=int(lev))
    pair_budget = _near_median_usd_per_pair_budget(usd_per_leg=float(slot))
    im_ratio, gap_ratio, avail_pv, src = _near_median_im_gap_metrics(
        snap=snap,
        portfolio_value_usd=float(pv),
        leverage=int(lev),
        pos_notional_usd=float(book_pos_notional_usd) if book_pos_notional_usd is not None else None,
    )
    if src == "none":
        _log(
            f"PM(near_median): skip {jobs_tag} — no IM basis "
            f"(im_usage missing and pos_notional unset; cap {float(DEFAULT_IM_TARGET_PCT):g}%)."
        )
        return None
    if gap_ratio <= 0.0:
        src_note = "im_usage (portfolio)" if src == "im_usage" else "pos_notional/(pv×lev)"
        _log(
            f"PM(near_median): skip {jobs_tag} — no IM gap (IM%={im_ratio * 100.0:.2f}% via {src_note}; "
            f"cap {float(DEFAULT_IM_TARGET_PCT):g}%)."
        )
        return None

    max_pairs = _near_median_max_pairs_from_available_position_value(
        available_position_value_usd=avail_pv, usd_per_pair=pair_budget
    )
    im_line = (
        f"IM%={im_ratio * 100.0:.2f}% (im_usage from /api/portfolio)"
        if src == "im_usage"
        else (
            f"IM%={im_ratio * 100.0:.2f}% (fallback pos_notional/(pv×lev); "
            f"pos_notional ${float(book_pos_notional_usd):,.2f} / ${float(pv) * float(lev):,.2f})"
        )
    )
    _log(
        f"PM(near_median): {jobs_tag} sizing — {im_line}; "
        f"gap={(gap_ratio * 100.0):.2f}% vs target {float(DEFAULT_IM_TARGET_PCT):g}% "
        f"→ available_position_value=${avail_pv:,.2f}; "
        f"$/pair=${pair_budget:,.2f} (2×usd_per_leg); "
        f"usd_per_leg={slot:g} (pv×lev×({DEFAULT_IM_TARGET_PCT:g}%/100)/{INVERT_EXTREME_MAX_TICKER_ENTRIES}); "
        f"max_new_pairs≈{max_pairs} (avail / $/pair)."
    )
    return float(slot)


def _log(msg: str) -> None:
    print(msg, flush=True)


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


def run_listingtable_or_use_cache(*, timeout_s: float = 120.0) -> str:
    plan = (os.getenv("VARIBOT_COINGECKO_PLAN", "pro") or "pro").strip().lower()
    script_name = "listingtable_pro.py" if plan == "pro" else "listingtable.py"
    script = os.path.join(_LISTINGS_DIR, script_name)
    json_path = os.path.join(_LISTINGS_DIR, "listingtabledata.json")
    if not os.path.isfile(script):
        raise FileNotFoundError(f"{script_name} not found: {script}")
    rc = _run_script(script, cwd=_LISTINGS_DIR, timeout_s=timeout_s)
    if rc != 0 and os.path.isfile(json_path):
        _log(f"{script_name} exited {rc}; using cached listingtabledata.json if present.")
    elif rc != 0:
        raise RuntimeError(f"{script_name} failed (code {rc}) and no cache at {json_path}")
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"Expected {json_path} after listingtable.")
    return json_path


def _should_refresh_listingtable_for_funding_pairs_rotate() -> Tuple[bool, float]:
    """
    Returns (enabled, min_age_s).
      - enabled defaults ON (can be disabled via env)
      - min_age_s defaults to a small value to avoid stale funding/volume when rotating pairs
    """
    v = (os.getenv(_FP_REFRESH_LISTINGTABLE_ENV, "") or "").strip().lower()
    if v in ("0", "false", "no", "n", "off"):
        enabled = False
    elif v in ("1", "true", "yes", "y", "on"):
        enabled = True
    else:
        enabled = bool(_FP_REFRESH_DEFAULT_ON)

    min_age_s = float(_FP_REFRESH_DEFAULT_MIN_AGE_S)
    try:
        s = (os.getenv(_FP_REFRESH_MIN_AGE_S_ENV, "") or "").strip()
        if s:
            min_age_s = max(0.0, float(s))
    except Exception:
        pass

    return enabled, float(min_age_s)


def _listingtable_age_s(path: str) -> Optional[float]:
    try:
        if not os.path.isfile(path):
            return None
        return max(0.0, time.time() - float(os.path.getmtime(path)))
    except Exception:
        return None


def run_marketstate(*, timeout_s: float = 90.0) -> None:
    script = os.path.join(_LISTINGS_DIR, "marketstate.py")
    if not os.path.isfile(script):
        raise FileNotFoundError(f"marketstate.py not found: {script}")
    rc = _run_script(script, cwd=_LISTINGS_DIR, timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError(f"marketstate.py exited {rc}")


def run_strategy_pick_tickers(
    *,
    strategy_key: str,
    listing_json: str,
    top_n: int,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    ms_path = os.path.join(_LISTINGS_DIR, "marketstate.json")
    # funding_pairs.pick_tickers does not use marketstate; avoid requiring marketstate.json on disk.
    if _strategy_key_normalized(strategy_key) == "funding_pairs":
        return strategies_mod.run_strategy(
            strategy_key=strategy_key,
            listing_json=listing_json,
            marketstate_json=None,
            top_n=int(top_n),
            write_output_txt=True,
        )
    if not os.path.isfile(ms_path):
        raise FileNotFoundError(f"marketstate.json missing at {ms_path} (run marketstate.py).")
    return strategies_mod.run_strategy(
        strategy_key=strategy_key,
        listing_json=listing_json,
        marketstate_json=ms_path,
        top_n=int(top_n),
        write_output_txt=True,
    )


def _top_n_for_strategy(strategy_key: str) -> int:
    """
    Some strategies interpret `top_n` differently. Keep funding_pairs separate from the
    default long/short ticker-universe sizing used by other strategies.
    """
    k = (strategy_key or "").strip().lower()
    if k.endswith(".py"):
        k = k[:-3]
    if k == "funding_pairs":
        # Optional env override for quick tuning without code changes.
        v = (os.getenv("VARIBOT_FUNDING_PAIRS_TOP_N", "") or "").strip()
        if v:
            try:
                return max(1, int(float(v)))
            except Exception:
                pass
        return 60
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
    if extra_args:
        cmd_args.extend(extra_args)
    _log(f"Invoking {multi_script} longs={len(longs)} shorts={len(shorts)} live={live}")
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
            longs, shorts, meta = run_strategy_pick_tickers(
                strategy_key=strat_key,
                listing_json=listing_json,
                top_n=top_n,
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
            )
        else:
            pct = _resolve_im_target_pct_for_multimarket(
                strategy_key=strat_key,
                n_long=len(new_l),
                n_short=len(new_s),
                args_im_target_pct=(
                    float(args.im_target_pct) if args.im_target_pct is not None else None
                ),
            )
            rc2 = run_multimarket(
                multi_script=str(args.multi_script),
                longs=new_l,
                shorts=new_s,
                im_target_pct=pct,
                live=True,
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


def _funding_pairs_manager(
    *,
    ep: VariEndpoints,
    args: argparse.Namespace,
    positions_raw: Any,
) -> None:
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    k = strat_key.strip().lower()
    if k.endswith(".py"):
        k = k[:-3]
    if k != "funding_pairs":
        return

    snap = funding_pairs_mod.pair_interval_status(positions_raw=positions_raw)
    for line in funding_pairs_mod.format_pair_interval_log_lines(snap):
        _log(line)

    plan = funding_pairs_mod.desired_actions_from_positions(positions_raw=positions_raw)
    pairs_to_close = plan.get("pairs_to_close") if isinstance(plan, dict) else None
    if not isinstance(pairs_to_close, list):
        return
    if not pairs_to_close:
        _log("funding_pairs manager: no slots to close (per-pair TP/max-age).")
        return

    _log(f"funding_pairs manager: {len(pairs_to_close)} slot(s) flagged to close:")
    for p in pairs_to_close:
        if not isinstance(p, dict):
            continue
        slot = p.get("slot")
        long_t = p.get("long")
        short_t = p.get("short")
        up = p.get("combined_upnl_pct")
        reason = p.get("close_reason")
        age_s = p.get("age_s")
        up_s = "-" if up is None else f"{float(up):.3f}%"
        age_s_s = "-" if age_s is None else _format_duration_s(float(age_s))
        _log(f"  slot={slot} {reason} age={age_s_s} combined_uPNL%={up_s} LONG {long_t} / SHORT {short_t}")

    if not bool(args.live):
        _log("funding_pairs manager: dry-run (not live) — would close flagged slot legs and open replacements.")
        return

    # Build a quick lookup of current position qty by underlying.
    by_sym: Dict[str, float] = {}
    for pos in _positions_list(positions_raw):
        sym = _instrument_label(pos).strip().upper()
        q = _position_qty(pos)
        if sym and q is not None and abs(float(q)) > 1e-12:
            by_sym[sym] = float(q)

    max_slip = _resolve_max_slippage()
    listing_json = os.path.join(_LISTINGS_DIR, "listingtabledata.json")
    top_n = _top_n_for_strategy(strat_key)

    # If we're rotating pairs, refresh listingtable so replacement selection isn't based on stale vol/funding.
    refresh_on, min_age_s = _should_refresh_listingtable_for_funding_pairs_rotate()
    if refresh_on:
        age = _listingtable_age_s(listing_json)
        if age is None or float(age) >= float(min_age_s):
            age_s = "missing" if age is None else f"{age:.0f}s"
            _log(
                f"funding_pairs manager: refreshing listingtable before replacements (age={age_s}, min_age={min_age_s:.0f}s)..."
            )
            try:
                listing_json = run_listingtable_or_use_cache(timeout_s=float(getattr(args, "listing_timeout_s", 120.0)))
            except Exception as e:
                _log(f"funding_pairs manager: WARNING listingtable refresh failed; using existing cache ({type(e).__name__}: {e})")

    for p in pairs_to_close:
        if not isinstance(p, dict):
            continue
        try:
            slot_i = int(p.get("slot"))
        except Exception:
            continue
        long_t = str(p.get("long") or "").strip().upper()
        short_t = str(p.get("short") or "").strip().upper()
        if not long_t or not short_t:
            continue

        # Close both legs reduce-only at full size.
        for sym in (long_t, short_t):
            q = by_sym.get(sym)
            if q is None or abs(float(q)) <= 1e-12:
                _log(f"funding_pairs manager: skip close {sym} (no open qty found).")
                continue
            close_side = "sell" if float(q) > 0 else "buy"
            qty_abs = abs(float(q))
            _log(f"funding_pairs manager: closing {sym} qty={qty_abs:g} side={close_side} reduce-only...")
            _close_reduce_only_with_slippage_steps(
                ep=ep,
                sym=sym,
                qty_abs=float(qty_abs),
                close_side=str(close_side),
                max_slip=float(max_slip),
            )

        # Open a replacement pair for that slot (best-effort).
        try:
            repl = funding_pairs_mod.pick_replacement_pair(
                listing_json=os.path.abspath(listing_json),
                state_json_path=None,
                top_n_by_vol=int(top_n),
                extra_disallow=set(by_sym.keys()),
            )
            new_long = str(repl.get("long") or "").strip().upper()
            new_short = str(repl.get("short") or "").strip().upper()
            if new_long and new_short:
                _log(f"funding_pairs manager: opening replacement for slot {slot_i}: LONG {new_long} / SHORT {new_short}")
                if args.usd is not None:
                    rc_mm = run_multimarket(
                        multi_script=str(args.multi_script),
                        longs=[new_long],
                        shorts=[new_short],
                        usd=float(args.usd),
                        live=True,
                    )
                else:
                    pct = _resolve_im_target_pct_for_multimarket(
                        strategy_key=strat_key,
                        n_long=1,
                        n_short=1,
                        args_im_target_pct=(
                            float(args.im_target_pct) if args.im_target_pct is not None else None
                        ),
                    )
                    rc_mm = run_multimarket(
                        multi_script=str(args.multi_script),
                        longs=[new_long],
                        shorts=[new_short],
                        im_target_pct=pct,
                        live=True,
                    )
                if int(rc_mm) == 0:
                    _log_post_multimarket_positions_tally(ep=ep, longs=[new_long], shorts=[new_short])
                try:
                    funding_pairs_mod.replace_state_pair_slot(
                        state_json_path=None,
                        slot=int(slot_i),
                        new_long=new_long,
                        new_short=new_short,
                        opened_unix=time.time(),
                    )
                except Exception as e:
                    _log(f"funding_pairs manager: WARNING could not update state slot {slot_i}: {e}")
        except Exception as e:
            _log(f"funding_pairs manager: replacement open skipped (error: {type(e).__name__}: {e})")


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

    _log("PM(near_median): dry-run preview — refreshing listingtable (pro) + marketstate as needed...")
    try:
        listing_json = run_listingtable_or_use_cache(timeout_s=float(getattr(args, "listing_timeout_s", 120.0)))
        ms_path = os.path.join(_LISTINGS_DIR, "marketstate.json")
        if not os.path.isfile(ms_path):
            _log("PM(near_median): dry-run preview — marketstate.json missing; running marketstate.py...")
            run_marketstate(timeout_s=float(getattr(args, "marketstate_timeout_s", 90.0)))

        top_n = _top_n_for_strategy(strat_key)
        longs, shorts, meta = run_strategy_pick_tickers(
            strategy_key=strat_key,
            listing_json=listing_json,
            top_n=top_n,
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

    threshold_pct = float(getattr(args, "pm_pair_tp_pct", PM_PAIR_TP_THRESHOLD_PCT_DEFAULT))
    pairs = select_pairs_greedy_grid(rows=rows, threshold_pct=threshold_pct)
    if not pairs:
        nearest = scan_best_winner_opposite_pair(rows=rows)
        if nearest is None:
            _log(
                f"PM(near_median): no eligible close pairs (threshold={threshold_pct:g}%) — "
                "no winner×opposite pairing evaluated (no positive-uPnL leg or missing L/S side)."
            )
        else:
            _log(
                f"PM(near_median): no eligible close pairs (threshold={threshold_pct:g}%) — "
                f"nearest: LONG {nearest.long_ticker} + SHORT {nearest.short_ticker} "
                f"combined_uPNL%={nearest.combined_upnl_pct:.3f}% "
                f"(${nearest.combined_upnl_usd:,.2f} / ${nearest.combined_value_usd:,.2f})."
            )
        return

    _log(f"PM(near_median): {len(pairs)} eligible pair(s) to close (threshold={threshold_pct:g}%).")
    for p in pairs:
        _log(
            f"  close_if_live: LONG {p.long_ticker} + SHORT {p.short_ticker} "
            f"combined_uPNL%={p.combined_upnl_pct:.3f}% "
            f"(${p.combined_upnl_usd:,.2f} / ${p.combined_value_usd:,.2f})"
        )

    if not bool(args.live):
        _log("PM(near_median): dry-run (not live) — would close pairs and (optionally) refill.")
        _near_median_pm_dry_run_refill_preview(
            ep=ep,
            args=args,
            strat_key=strat_key,
            positions_raw=positions_raw,
            pairs=pairs,
            snap=snap,
        )
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

    for p in pairs:
        for sym in (p.long_ticker, p.short_ticker):
            q = by_sym.get(sym)
            if q is None or abs(float(q)) <= 1e-12:
                _log(f"PM(near_median): skip close {sym} (no open qty found).")
                continue
            close_side = "sell" if float(q) > 0 else "buy"
            qty_abs = abs(float(q))
            _log(f"PM(near_median): closing {sym} qty={qty_abs:g} side={close_side} reduce-only...")
            _close_reduce_only_with_slippage_steps(
                ep=ep,
                sym=sym,
                qty_abs=float(qty_abs),
                close_side=str(close_side),
                max_slip=float(max_slip),
            )
            closed_syms.add(str(sym).strip().upper())

    # Refill: refresh listingtable_pro then open replacements (same strategy logic), excluding closed + open.
    refill_on = bool(getattr(args, "pm_refill", False)) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        # Default behavior for this branch: refill unless explicitly disabled.
        refill_on = bool(PM_REFILL_DEFAULT_ON) and not bool(getattr(args, "pm_no_refill", False))
    if not refill_on:
        return

    n_pairs = len(pairs)
    if n_pairs <= 0:
        return

    try:
        raw_pf_after = ep.get_portfolio(compute_margin=True)
        snap_after = parse_portfolio_snapshot(raw_pf_after)
        open_after = ep.get_positions()
        pos_n_after = _positions_notional_usd(open_after)
    except Exception:
        _log("PM(near_median): skip refill — could not fetch portfolio/positions after closes.")
        return

    pv_after = getattr(snap_after, "portfolio_value_usd", None)
    if pv_after is None or float(pv_after) <= 0:
        _log("PM(near_median): skip refill — post-close portfolio_value_usd missing or non-positive.")
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
    pair_budget_refill = _near_median_usd_per_pair_budget(usd_per_leg=float(leg_refill))

    _im_r, gap_r, avail_pv, _im_src = _near_median_im_gap_metrics(
        snap=snap_after,
        portfolio_value_usd=float(pv_after),
        leverage=int(lev_m),
        pos_notional_usd=float(pos_n_after),
    )
    max_pairs_budget = _near_median_max_pairs_from_available_position_value(
        available_position_value_usd=float(avail_pv),
        usd_per_pair=float(pair_budget_refill),
    )
    need_pairs = min(int(n_pairs), int(max_pairs_budget))
    if need_pairs <= 0:
        _log(
            f"PM(near_median): skip refill — IM%={_im_r * 100.0:.2f}% leaves "
            f"available_position_value=${avail_pv:,.2f} (< $/pair for one pair at ${pair_budget_refill:,.2f})."
        )
        return
    if need_pairs < int(n_pairs):
        _log(
            f"PM(near_median): refill capped by IM budget — pairs wanted={n_pairs}, "
            f"affordable={need_pairs} (available_position_value=${avail_pv:,.2f})."
        )

    if args.usd is not None:
        if gap_r <= 0.0:
            _log(
                f"PM(near_median): skip refill — no IM gap (IM%={_im_r * 100.0:.2f}%; cap "
                f"{float(DEFAULT_IM_TARGET_PCT):g}%)."
            )
            return
        usd_run = float(args.usd)
    else:
        usd_run_opt = _near_median_pm_usd_for_multimarket(
            snap=snap_after,
            args=args,
            jobs_tag="PM refill (post-close)",
            book_pos_notional_usd=float(pos_n_after),
        )
        if usd_run_opt is None:
            return
        usd_run = float(usd_run_opt)

    # Refresh listing cache (pro) and ensure marketstate exists (strategy runner requires it for non-funding_pairs).
    _log("PM(near_median): refreshing listingtable (pro) for replacements...")
    listing_json = run_listingtable_or_use_cache(timeout_s=float(getattr(args, "listing_timeout_s", 120.0)))
    ms_path = os.path.join(_LISTINGS_DIR, "marketstate.json")
    if not os.path.isfile(ms_path):
        _log("PM(near_median): marketstate.json missing; running marketstate.py...")
        run_marketstate(timeout_s=float(getattr(args, "marketstate_timeout_s", 90.0)))

    # Disallow: closed this cycle + currently open positions (post-close).
    open_syms: Set[str] = set()
    for pos in _positions_list(open_after):
        sym = _instrument_label(pos).strip().upper()
        q = _position_qty(pos)
        if sym and q is not None and abs(float(q)) > 1e-12:
            open_syms.add(sym)
    disallow = set(open_syms) | set(closed_syms)

    top_n = _top_n_for_strategy(strat_key)
    longs, shorts, meta = run_strategy_pick_tickers(strategy_key=strat_key, listing_json=listing_json, top_n=top_n)
    need_each_side = int(need_pairs)
    repl_l, repl_s = filter_replacements(
        longs=longs,
        shorts=shorts,
        disallow=disallow,
        need_each_side=need_each_side,
    )
    got_l, got_s = len(repl_l), len(repl_s)
    repl_l, repl_s, n_do = _near_median_align_pair_candidates(repl_l, repl_s, wanted_pairs=need_each_side)
    if n_do <= 0:
        _log(
            f"PM(near_median): WARNING insufficient replacements after disallow filter "
            f"(wanted {need_each_side} pair(s); got {got_l}L/{got_s}S). Skip refill."
        )
        return
    if n_do < need_each_side:
        _log(
            f"PM(near_median): refill partial — wanted {need_each_side} pair(s), opening {n_do} "
            f"(candidates {got_l}L/{got_s}S)."
        )

    _log(
        f"PM(near_median): opening replacements (strategy={meta.get('strategy')}) "
        f"pairs={n_do} longs={len(repl_l)} shorts={len(repl_s)}..."
    )
    rc_mm = run_multimarket(
        multi_script=str(args.multi_script),
        longs=repl_l,
        shorts=repl_s,
        usd=float(usd_run),
        live=bool(args.live),
    )
    if bool(args.live) and int(rc_mm) == 0:
        _log_post_multimarket_positions_tally(ep=ep, longs=repl_l, shorts=repl_s)
    if bool(args.live):
        _near_median_substitute_skew_rejected_multimarket(
            ep=ep,
            args=args,
            strat_key=strat_key,
            listing_json=listing_json,
            jobs_tag="PM refill",
            usd=float(usd_run),
        )


def _near_median_topup_if_needed(
    *,
    ep: VariEndpoints,
    args: argparse.Namespace,
    snap: Any,
    positions_raw: Any,
    cycle_index: int = 0,
) -> None:
    """
    Top-up adds paired long/short slots toward DEFAULT_MAX_TICKER_ENTRIES when affordable.

    IM gap uses snap.im_usage when present; else pos_notional / (pv × leverage).
    available_position_value → max_new_pairs ≈ avail / $/pair with
    $/pair = 2 × usd_per_leg (same leg size as pv×lev×(DEFAULT_IM_TARGET_PCT/100)/N or --usd). Need not reach target tickers if budget is smaller.

    Book / positions are evaluated before IM gap (cycle 1 logs an explicit init inventory so startup
    does not look like a blind IM-only no-op).
    """
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    strat_norm = _strategy_key_normalized(strat_key)
    if strat_norm != "invert_extreme":
        return

    pv = getattr(snap, "portfolio_value_usd", None)
    if pv is None or float(pv) <= 0:
        _log("PM(near_median): skip top-up — portfolio_value_usd missing or non-positive.")
        return

    rows = positions_to_rows(positions_raw)
    if not rows:
        return

    target_total = int(INVERT_EXTREME_MAX_TICKER_ENTRIES)
    if target_total <= 0:
        return
    cur_total = len(rows)
    target_per_side = target_total // 2
    cur_long = sum(1 for r in rows if r.side == "L")
    cur_short = sum(1 for r in rows if r.side == "S")
    need_long = max(0, int(target_per_side) - int(cur_long))
    need_short = max(0, int(target_per_side) - int(cur_short))

    # If still short on total due to oddities, allocate remaining to the smaller side.
    remaining = max(0, int(target_total) - int(cur_total) - int(need_long) - int(need_short))
    if remaining > 0:
        if cur_long <= cur_short:
            need_long += remaining
        else:
            need_short += remaining

    # Structural pairs toward 20 tickers; IM budget may allow fewer (or zero).
    n_pairs_struct = min(int(need_long), int(need_short))

    if int(cycle_index) == 1:
        syms = sorted({str(r.ticker).strip().upper() for r in rows if r.ticker})
        preview = ", ".join(syms[:24])
        if len(syms) > 24:
            preview += f", … (+{len(syms) - 24} more)"
        _log(
            f"PM(near_median): init — loaded open book tickers={cur_total} "
            f"(L={cur_long}, S={cur_short}): {preview}"
        )

    _log(
        f"PM(near_median): book snapshot — tickers={cur_total}/{target_total} "
        f"L={cur_long} S={cur_short}; structural top-up need≈{n_pairs_struct} pair(s) "
        f"(need_long={need_long}, need_short={need_short})."
    )

    lev_m = _multimarket_effective_leverage()
    pos_notional = float(_positions_notional_usd(positions_raw))
    im_ratio, gap_ratio, avail_pv, _im_src = _near_median_im_gap_metrics(
        snap=snap,
        portfolio_value_usd=float(pv),
        leverage=int(lev_m),
        pos_notional_usd=pos_notional,
    )
    slot_pre = (
        float(args.usd)
        if args.usd is not None
        else _near_median_slot_usd_per_leg(portfolio_value_usd=float(pv), leverage=int(lev_m))
    )
    pair_budget_top = _near_median_usd_per_pair_budget(usd_per_leg=float(slot_pre))
    max_pairs_budget = _near_median_max_pairs_from_available_position_value(
        available_position_value_usd=float(avail_pv),
        usd_per_pair=float(pair_budget_top),
    )

    if _im_src == "im_usage":
        _log(
            f"PM(near_median): IM%={im_ratio * 100.0:.2f}% (im_usage from /api/portfolio); "
            f"gap={(gap_ratio * 100.0):.2f}% vs target {float(DEFAULT_IM_TARGET_PCT):g}% "
            f"→ available_position_value=${avail_pv:,.2f}; "
            f"max_new_pairs≈{max_pairs_budget} at ${pair_budget_top:,.2f}/pair "
            f"(2×usd_per_leg={slot_pre:g})."
        )
    else:
        _log(
            f"PM(near_median): IM%={im_ratio * 100.0:.2f}% "
            f"(fallback pos_notional ${pos_notional:,.2f} / (pv×lev)="
            f"${float(pv) * float(lev_m):,.2f}); gap={(gap_ratio * 100.0):.2f}% vs target "
            f"{float(DEFAULT_IM_TARGET_PCT):g}% → available_position_value=${avail_pv:,.2f}; "
            f"max_new_pairs≈{max_pairs_budget} at ${pair_budget_top:,.2f}/pair "
            f"(2×usd_per_leg={slot_pre:g})."
        )

    if gap_ratio <= 0.0:
        if int(cycle_index) == 1:
            _log(
                "PM(near_median): init — no top-up: IM at or above portfolio target "
                f"({float(DEFAULT_IM_TARGET_PCT):g}%); book snapshot above. Next cycles same checks apply."
            )
        else:
            _log(
                "PM(near_median): skip top-up — no IM gap "
                f"(IM% vs DEFAULT_IM_TARGET_PCT {float(DEFAULT_IM_TARGET_PCT):g}%)."
            )
        return

    if cur_total >= target_total:
        _log(
            f"PM(near_median): skip top-up — book at max tickers ({cur_total}/{target_total}; "
            f"DEFAULT_MAX_TICKER_ENTRIES)."
        )
        return

    if n_pairs_struct <= 0:
        _log(
            f"PM(near_median): skip top-up — no paired slots (cur L/S={cur_long}/{cur_short}, "
            f"target_per_side={target_per_side}, need_long={need_long}, need_short={need_short})."
        )
        return

    n_pairs = min(int(n_pairs_struct), int(max_pairs_budget))
    if n_pairs <= 0:
        _log(
            "PM(near_median): skip top-up — IM budget allows 0 new pairs "
            f"(wanted_structurally={n_pairs_struct})."
        )
        return
    if n_pairs < n_pairs_struct:
        _log(
            f"PM(near_median): top-up capped by IM budget — structural_need={n_pairs_struct}, "
            f"opening={n_pairs} pair(s)."
        )

    need_long = n_pairs
    need_short = n_pairs

    open_syms: Set[str] = {r.ticker.strip().upper() for r in rows if r.ticker}
    _log(
        f"PM(near_median): top-up plan — current={cur_total}/{target_total} "
        f"(L={cur_long}, S={cur_short}); add {n_pairs} pair(s)."
    )

    if not bool(args.live):
        _log(
            "PM(near_median): dry-run — running listingtable + strategy picks + multimarketorder "
            "(no --live; prints sizing line and per-ticker quote dry-run)."
        )

    listing_json = run_listingtable_or_use_cache(timeout_s=float(getattr(args, "listing_timeout_s", 120.0)))
    ms_path = os.path.join(_LISTINGS_DIR, "marketstate.json")
    if not os.path.isfile(ms_path):
        run_marketstate(timeout_s=float(getattr(args, "marketstate_timeout_s", 90.0)))

    top_n = _top_n_for_strategy(strat_key)
    longs, shorts, meta = run_strategy_pick_tickers(strategy_key=strat_key, listing_json=listing_json, top_n=top_n)
    add_l, add_s = filter_replacements(longs=longs, shorts=shorts, disallow=set(open_syms), need_each_side=int(n_pairs))
    got_l, got_s = len(add_l), len(add_s)
    add_l, add_s, n_open = _near_median_align_pair_candidates(add_l, add_s, wanted_pairs=n_pairs)
    if n_open <= 0:
        _log(
            f"PM(near_median): top-up skipped — insufficient new tickers "
            f"(wanted {n_pairs} pair(s); got {got_l}L/{got_s}S after filter)."
        )
        return
    if n_open < n_pairs:
        _log(
            f"PM(near_median): top-up partial — wanted {n_pairs} pair(s), opening {n_open} "
            f"(candidates {got_l}L/{got_s}S)."
        )

    _log(
        f"PM(near_median): topping up with {n_open} pair(s) "
        f"(strategy={meta.get('strategy')}) longs={add_l} shorts={add_s}"
    )
    usd_run = _near_median_pm_usd_for_multimarket(
        snap=snap,
        args=args,
        jobs_tag="top-up",
        book_pos_notional_usd=float(pos_notional),
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

    has_pos = has_open_positions(raw_pos)

    if not has_pos:
        _clear_position_latch()
        if _strategy_key_normalized(strat_key) == "funding_pairs":
            _log("No open positions -> listingtable -> strategy -> multimarket (marketstate skipped for funding_pairs)")
        else:
            _log("No open positions -> listingtable -> marketstate -> strategy -> multimarket")
        cg_plan = (os.getenv("VARIBOT_COINGECKO_PLAN", "pro") or "pro").strip().lower()
        _log(
            f"step: running listingtable ({'CoinGecko Pro' if cg_plan == 'pro' else 'CoinGecko Free'}) "
            f"(may take a while)..."
        )
        listing_json = run_listingtable_or_use_cache(timeout_s=float(args.listing_timeout_s))
        if _strategy_key_normalized(strat_key) != "funding_pairs":
            _log("step: running marketstate.py...")
            run_marketstate(timeout_s=float(args.marketstate_timeout_s))
        top_n = _top_n_for_strategy(strat_key)
        longs, shorts, meta = run_strategy_pick_tickers(
            strategy_key=strat_key,
            listing_json=listing_json,
            top_n=top_n,
        )
        if _strategy_key_normalized(strat_key) != "funding_pairs":
            _log("step: marketstate finished")
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
                strategy_key=strat_key,
                n_long=len(longs),
                n_short=len(shorts),
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

    _funding_pairs_manager(ep=ep, args=args, positions_raw=raw_pos)

    # 3) Top-up only after PM(pair-threshold) step above (uses refreshed snap when live).
    _near_median_topup_if_needed(
        ep=ep, args=args, snap=snap, positions_raw=raw_pos, cycle_index=int(cycle_index)
    )

    return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Varibot flowchart orchestrator (see VariBotFlowchart.jpg).")
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually close positions and place orders (otherwise dry-run).",
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
            "Portfolio Manager: combined uPnL% threshold vs combined value for pair closes "
            f"(default {PM_PAIR_TP_THRESHOLD_PCT_DEFAULT:g})."
        ),
    )
    p.add_argument(
        "--pm-refill",
        action="store_true",
        help="Portfolio Manager: after closing eligible pairs, refresh listingtable_pro and open replacements.",
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
            "Time-in-position clock: marketstate=Vari Listings/marketstate.json "
            "(fetched_at_unix / fetched_at from the run just before orders; default); "
            "auto=marketstate then orders then latch; latch|orders=see help text."
        ),
    )
    p.add_argument(
        "--marketstate-json",
        default=None,
        help="Override path to marketstate.json for time-in-position (default: Vari Listings/marketstate.json).",
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
            f"If --usd is omitted and this is omitted, defaults to {DEFAULT_IM_TARGET_PCT:g}%% "
            "(non-funding_pairs strategies). "
            "For strategy funding_pairs, this flag is ignored unless you use --usd; "
            "notional per leg is sized from strategy.funding_pairs.FUNDING_PAIR_MAX_IM_PCT of (pv×leverage). "
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
        help=f"Strategy key to use when flat (default {Strategy!r}; see strategy/strategies.py).",
    )
    p.add_argument("--listing-timeout-s", type=float, default=120.0)
    p.add_argument("--marketstate-timeout-s", type=float, default=90.0)
    p.add_argument("--once", action="store_true", help="Run a single cycle then exit (no sleep loop).")
    p.add_argument(
        "--no-align",
        action="store_true",
        help="Sleep fixed --period-min between cycles instead of aligning to wall clock.",
    )
    return p.parse_args()


def _child_main() -> int:
    args = parse_args()
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
    if os.getenv(_VARIBOT_WRAPPED_ENV, "").strip() != "1":
        raise SystemExit(_run_self_wrapped())
    raise SystemExit(_child_main())
