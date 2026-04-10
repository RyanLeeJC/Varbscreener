from __future__ import annotations

"""
Varibot orchestrator — implements the VariBotFlowchart workflow:

  Auth (validate_vr_token) -> every T minutes: portfolio + TP Check ->
  if positions -> TP exit and/or time-in-position exit (closeallpositions.py) ->
  if flat -> listingtable -> marketstate -> strategy -> multimarketorder
  (strategy funding_pairs skips marketstate on entry; per-pair exits in funding_pairs manager; portfolio TP /
   --tp-pct can still close-all; global time-in-position is skipped for funding_pairs only.)

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
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Imports assume sibling scripts + variationalbot live under this directory.
_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
_LISTINGS_DIR = os.path.join(_REPO_ROOT, "Vari Listings")
_DEFAULT_MARKETSTATE_JSON = os.path.join(_LISTINGS_DIR, "marketstate.json")
_POSITION_LATCH_PATH = os.path.join(_VARIBOT_DIR, ".varibot_position_latch.json")

_TIME_IN_POSITION_POST_CLOSE_SLEEP_S: float = 15.0 # after a live time-in-position close, sleep this long then start the next cycle (skip wall-clock wait)
DEFAULT_TICKER_QTY: int = 10 # default ticker qty (total universe size before split; becomes half long / half short)
DEFAULT_FUNDING_PAIRS_TOP_N: int = 60 # funding_pairs uses top-N-by-volume universe; keep separate from DEFAULT_TICKER_QTY
_DEFAULT_CYCLE_PERIOD_MIN: int = 5 # how often to run a new cycle (wall-clock aligned by default)
max_time_position: int = 240 # close-all time-in-position threshold (minutes)
_DEFAULT_TP_PCT: float = 5.0 # take profit percentage
_COINGECKO_PLAN: str = "pro"  # set to "pro" to use listingtable_pro.py

# User setting: which strategy to run when flat.
# You can put a module name (preferred) or a filename:
#   "revert_median" or "revert_median.py"
Strategy: str = os.getenv("VARIBOT_STRATEGY", "funding_pairs.py").strip() or "funding_pairs.py"

# Rolling log (wrapper mode): varibot.py can self-wrap to prefix lines and keep a rolling logfile,
# so you can run just `python3 varibot.py --live` and still get the run_varibot_logged behavior.
_VARIBOT_LOG_MAX_LINES: int = 1000
_VARIBOT_WRAPPED_ENV: str = "VARIBOT_WRAPPED"

# Post-multimarket verification: /api/positions can lag behind fills by a second or two.
POST_MULTIMARKET_POSITIONS_MAX_WAIT_S: float = 2.0
POST_MULTIMARKET_POSITIONS_POLL_S: float = 0.5

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

from check_portfolio_stats import _apply_tp_check, _build_out_dict  # noqa: E402
from positions import _instrument_label  # noqa: E402
from validate_vr_token import validate_vr_token  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402

from multimarketorder import DEFAULT_IM_TARGET_PCT  # noqa: E402
from strategy import strategies as strategies_mod  # noqa: E402
from strategy import funding_pairs as funding_pairs_mod  # noqa: E402


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


def _log(msg: str) -> None:
    print(msg, flush=True)


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
    script_name = "listingtable_pro.py" if _COINGECKO_PLAN.strip().lower() == "pro" else "listingtable.py"
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
        return int(DEFAULT_FUNDING_PAIRS_TOP_N)
    return int(DEFAULT_TICKER_QTY)


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
    return 0.0025


def _extract_quote_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("quote_id", "quoteId", "id"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


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
            # quote_indicative_simple doesn't include side; stash override in response container
            from variationalbot.vari.endpoints import Instrument  # local import

            instrument = Instrument(instrument_type="perpetual_future", underlying=sym)
            qresp = ep.quote_indicative_simple(instrument=instrument, qty=qty_abs)
            if isinstance(qresp, dict):
                qresp["_close_side_override"] = close_side
            quote_id = _extract_quote_id(qresp)
            if not quote_id:
                raise ValueError(f"indicative quote missing quote_id for {sym} qty={qty_abs}")
            ep.place_order_market(
                quote_id=str(quote_id),
                side=close_side,
                max_slippage=float(max_slip),
                is_reduce_only=True,
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
                    run_multimarket(
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
                    run_multimarket(
                        multi_script=str(args.multi_script),
                        longs=[new_long],
                        shorts=[new_short],
                        im_target_pct=pct,
                        live=True,
                    )
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


def one_cycle(
    *,
    ep: VariEndpoints,
    cfg: Any,
    args: argparse.Namespace,
) -> bool:
    """
    Returns True when a live time-in-position close-all just succeeded; main() should
    use a short cooldown then run the next cycle instead of sleeping to the wall clock.
    """
    strat_key = str(getattr(args, "strategy", "") or Strategy).strip() or Strategy
    raw_pf = ep.get_portfolio(compute_margin=True)
    snap = parse_portfolio_snapshot(raw_pf)
    out = _build_out_dict(cfg=cfg, snap=snap)
    _apply_tp_check(out, threshold_pct=float(args.tp_pct))

    _log(
        f"Portfolio uPNL={out.get('unrealized_pnl_usd')} "
        f"acct={out.get('portfolio_value_usd')} TP={out.get('tp_check')} "
        f"({out.get('tp_check_u_pnl_vs_portfolio_pct')})"
    )

    raw_pos = ep.get_positions()
    has_pos = has_open_positions(raw_pos)

    if not has_pos:
        _clear_position_latch()
        if _strategy_key_normalized(strat_key) == "funding_pairs":
            _log("No open positions -> listingtable -> strategy -> multimarket (marketstate skipped for funding_pairs)")
        else:
            _log("No open positions -> listingtable -> marketstate -> strategy -> multimarket")
        plan = _COINGECKO_PLAN.strip().lower()
        _log(f"step: running listingtable ({'CoinGecko Pro' if plan == 'pro' else 'CoinGecko Free'}) (may take a while)...")
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
            if args.live:
                _log_post_multimarket_positions_tally(ep=ep, longs=longs, shorts=shorts)
        return False

    # --- have positions: funding_pairs per-slot manager, then portfolio TP, then time-in-position ---
    _funding_pairs_manager(ep=ep, args=args, positions_raw=raw_pos)

    if out.get("tp_check") == "Yes":
        if args.live:
            rc = run_closeallpositions(live=True)
            if rc != 0:
                _log(f"closeallpositions exited {rc}")
        else:
            _log("TP Check Yes — [dry-run] would run closeallpositions.py --live")
        return False

    if _strategy_key_normalized(strat_key) == "funding_pairs":
        return False

    if str(args.time_exit_source) in ("auto", "orders", "latch"):
        if _read_position_latch_ts() is None:
            _write_position_latch(time.time())

    hold_limit_s = float(max_time_position) * 60.0 * max(1, int(args.time_exit_periods))
    ms_path = str(args.marketstate_json or _DEFAULT_MARKETSTATE_JSON)
    market_ts = read_marketstate_position_epoch_ts(ms_path)
    order_ts = last_non_reduce_order_ts(ep.get_orders_v2())
    latch_ts = _read_position_latch_ts()
    if str(args.time_exit_source) == "marketstate":
        ref_ts = market_ts
        src = "marketstate"
    elif str(args.time_exit_source) == "latch":
        ref_ts = latch_ts
        src = "latch"
    elif str(args.time_exit_source) == "orders":
        ref_ts = order_ts
        src = "orders"
    else:
        if market_ts is not None:
            ref_ts, src = market_ts, "marketstate"
        elif order_ts is not None:
            ref_ts, src = order_ts, "orders"
        else:
            ref_ts, src = latch_ts, "latch"

    if ref_ts is None:
        _log(
            "Time-in-position: no reference time "
            f"(source={args.time_exit_source}; marketstate={ms_path}); skip this exit."
        )
        return False

    age = time.time() - ref_ts
    age_fmt = _format_duration_s(age)
    limit_fmt = _format_duration_s(hold_limit_s)
    if age >= hold_limit_s:
        if args.live:
            _log(
                f"Time-in-position exceeded ({src} age={age_fmt} >= limit {limit_fmt}) -> close all"
            )
            rc = run_closeallpositions(live=True)
            if rc != 0:
                _log(f"closeallpositions exited {rc}")
                return False
            return True
        else:
            _log(
                f"Time-in-position would trigger ({src} age={age_fmt} >= limit {limit_fmt}) "
                f"[dry-run] closeall"
            )
            return False
    else:
        _log(f"Holding: time-in-position {src} age={age_fmt} / limit={limit_fmt}")
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
        default=_DEFAULT_CYCLE_PERIOD_MIN,
        help=f"Wall-clock cycle interval minutes (default {_DEFAULT_CYCLE_PERIOD_MIN}).",
    )
    p.add_argument(
        "--tp-pct",
        type=float,
        default=_DEFAULT_TP_PCT,
        help=(
            f"Portfolio TP threshold %% of account: tp_check Yes -> closeallpositions when uPNL%% of account "
            f"≥ this (default {_DEFAULT_TP_PCT:g}). Applies to all strategies including funding_pairs."
        ),
    )
    p.add_argument(
        "--time-exit-periods",
        type=int,
        default=1,
        help=f"Close all if time-in-position exceeds max_time_position × this many periods (default 1).",
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
            "Multimarket IM%% sizing: per-order USD = (portfolio_value_usd × leverage × PCT/100) / n_orders. "
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

    cycle_n = 0
    while True:
        cycle_n += 1
        _log(f"=== cycle {cycle_n} ===")
        try:
            ti_just_closed = one_cycle(ep=ep, cfg=cfg, args=args)
        except Exception as e:
            _log(f"cycle error: {type(e).__name__}: {e}")
            if args.once:
                return 1
            ti_just_closed = False

        if args.once:
            return 0

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
