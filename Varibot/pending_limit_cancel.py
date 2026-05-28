"""
Resilient pending limit cancels (Omni 418 ban-aware).

Shared by ``cancelalllimitorders.py`` and ``grid_limits_reconcile`` drift cancel.
Pacing: ``CANCEL_ALL_SLEEP_BETWEEN_S`` (default 1.5s between successful cancels).
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from variationalbot.vari.endpoints import VariEndpoints, parse_cancel_ban_wait_seconds
from variationalbot.vari.errors import VariUnexpectedResponse

ENV_CANCEL_SLEEP_BETWEEN_S: str = "CANCEL_ALL_SLEEP_BETWEEN_S"
ENV_CANCEL_MAX_RETRIES: str = "CANCEL_ALL_MAX_RETRIES"
ENV_CANCEL_BAN_BUFFER_S: str = "CANCEL_ALL_BAN_BUFFER_S"
ENV_CANCEL_PASSES: str = "CANCEL_ALL_PASSES"
# Legacy grid reconcile alias (same pacing when set).
ENV_GRID_LIMITS_CANCEL_SLEEP_S: str = "VARIBOT_GRID_LIMITS_CANCEL_SLEEP_S"


def cancel_sleep_between_s() -> float:
    raw = (
        os.environ.get(ENV_CANCEL_SLEEP_BETWEEN_S)
        or os.environ.get(ENV_GRID_LIMITS_CANCEL_SLEEP_S)
        or "1.5"
    ).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.5


def cancel_max_retries() -> int:
    raw = (os.environ.get(ENV_CANCEL_MAX_RETRIES) or "12").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


def cancel_ban_buffer_s() -> float:
    raw = (os.environ.get(ENV_CANCEL_BAN_BUFFER_S) or "0.75").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.75


def cancel_passes() -> int:
    raw = (os.environ.get(ENV_CANCEL_PASSES) or "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def row_rfq_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("rfq_id", "rfqId"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def order_row_underlying(row: Dict[str, Any]) -> str:
    inst = row.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    for k in ("underlying", "asset", "symbol"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().upper()
            if s.endswith("-PERP"):
                s = s[: -len("-PERP")]
            return s
    return ""


def cancel_one_limit_row(
    ep: VariEndpoints,
    *,
    row: Dict[str, Any],
    max_attempts: Optional[int] = None,
    buffer_s: Optional[float] = None,
    on_wait: Optional[Callable[..., None]] = None,
) -> None:
    """POST /api/orders/cancel with 418 ban retries (``cancel_order_rfq_resilient``)."""
    rid = row_rfq_id(row)
    if not rid:
        raise ValueError("missing rfq_id")
    ep.cancel_order_rfq_resilient(
        rfq_id=rid,
        max_attempts=max_attempts if max_attempts is not None else cancel_max_retries(),
        buffer_s=buffer_s if buffer_s is not None else cancel_ban_buffer_s(),
        on_wait=on_wait,
    )


def cancel_limit_rows(
    ep: VariEndpoints,
    rows: List[Dict[str, Any]],
    *,
    log: Optional[Callable[[str], None]] = None,
    passes: Optional[int] = None,
    sleep_between: Optional[float] = None,
    ban_buffer: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Cancel pending limit rows with ``cancelalllimitorders`` pacing.

    Returns ``(ok_count, error_count)``.
    """
    if not rows:
        return 0, 0

    sleep_s = cancel_sleep_between_s() if sleep_between is None else float(sleep_between)
    buffer = cancel_ban_buffer_s() if ban_buffer is None else float(ban_buffer)
    retries = cancel_max_retries() if max_retries is None else int(max_retries)
    n_passes = cancel_passes() if passes is None else max(1, int(passes))

    pending: List[Dict[str, Any]] = list(rows)
    errors: List[Tuple[str, str]] = []

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    for pass_n in range(1, n_passes + 1):
        if pass_n > 1:
            if not errors:
                break
            _log(f"retry pass {pass_n}/{n_passes}: {len(errors)} failed cancel(s)")
            err_ids = {rid for rid, _ in errors}
            errors = []
            pending = [r for r in pending if (row_rfq_id(r) or "") in err_ids]
            if not pending:
                break
            time.sleep(max(sleep_s, 2.0))

        for i, row in enumerate(pending):
            rid = row_rfq_id(row)
            if not rid:
                continue
            sym = order_row_underlying(row) or "?"
            side = str(row.get("side") or "").strip().lower()
            lp = row.get("limit_price") or row.get("trigger_price") or "?"

            def on_wait(sleep_wait: float, attempt: int, rfq: str) -> None:
                _log(
                    f"cancel ban: wait {sleep_wait:.1f}s before retry "
                    f"({attempt}/{retries}) {sym} rfq_id={rfq[:8]}…"
                )

            try:
                cancel_one_limit_row(
                    ep,
                    row=row,
                    max_attempts=retries,
                    buffer_s=buffer,
                    on_wait=on_wait,
                )
                _log(f"canceled {side} @ {lp} ({sym})")
            except VariUnexpectedResponse as e:
                wait_extra = parse_cancel_ban_wait_seconds(e)
                msg = f"{type(e).__name__}: {e}"
                errors.append((rid, msg))
                _log(f"cancel {side} @ {lp} failed ({msg})")
                if wait_extra is not None and i < len(pending) - 1:
                    time.sleep(float(wait_extra) + buffer)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                errors.append((rid, msg))
                _log(f"cancel {side} @ {lp} failed ({msg})")
            if i < len(pending) - 1 and sleep_s > 0:
                time.sleep(sleep_s)

    ok = len(rows) - len(errors)
    return ok, len(errors)
