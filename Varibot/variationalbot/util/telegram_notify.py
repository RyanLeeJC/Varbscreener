from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

from variationalbot.vari.errors import VariAuthError

_SGT = ZoneInfo("Asia/Singapore")
_LOCK = threading.Lock()
_LAST_SENT_MONO: float = 0.0


def is_vari_portfolio_auth_error(exc: BaseException) -> bool:
    """True for VariAuthError on GET /api/portfolio (e.g. compute_margin=true)."""
    if not isinstance(exc, VariAuthError):
        return False
    return "/api/portfolio" in str(exc).lower()


def _notify_enabled() -> bool:
    webhook = (os.getenv("TELEGRAM_WEBHOOK_URL") or "").strip()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return bool(webhook or (token and chat_id))


def _cooldown_s() -> float:
    raw = (os.getenv("TELEGRAM_AUTH_ALERT_COOLDOWN_S") or "1800").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1800.0


def _deployment_label() -> str:
    """Render service / git branch name shown after Vari> in alerts (e.g. Gridbot)."""
    for key in (
        "VARIBOT_NOTIFY_DEPLOYMENT_NAME",
        "RENDER_SERVICE_NAME",
        "RENDER_GIT_BRANCH",
        "RAILWAY_SERVICE_NAME",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return "Gridbot"


def _wallet_label() -> str:
    wallet = (os.getenv("VR_WALLET_ADDRESS") or "").strip()
    if not wallet:
        return "—"
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:6]}…{wallet[-4:]}"


def _format_sgt_time(dt: datetime) -> str:
    return f"{dt.strftime('%H:%M:%S')} SGT {dt.day} {dt.strftime('%b %Y')}"


def _format_auth_alert(*, cycle_index: Optional[int] = None) -> str:
    now = datetime.now(_SGT)
    lines = [
        f"Auth failure on Vari>{_deployment_label()} ({_wallet_label()})",
    ]
    if cycle_index is not None:
        lines.append(f"Cycle: {cycle_index}")
    lines.extend(
        [
            f"Time: {_format_sgt_time(now)}",
            "Please update vr-token in Render.",
        ]
    )
    return "\n".join(lines)


def _post_json(*, url: str, payload: Dict[str, Any], timeout_s: float) -> None:
    resp = requests.post(url, json=payload, timeout=timeout_s)
    resp.raise_for_status()


def _build_webhook_payload(
    *,
    text: str,
    error: str,
    cycle_index: Optional[int],
) -> Dict[str, Any]:
    now = datetime.now(_SGT)
    payload: Dict[str, Any] = {
        "text": text,
        "error": error,
        "deployment": _deployment_label(),
        "wallet": _wallet_label(),
        "time": _format_sgt_time(now),
        "event": "vari_portfolio_auth_failure",
    }
    if cycle_index is not None:
        payload["cycle"] = cycle_index
        payload["cycle_index"] = cycle_index
    return payload


def _send_auth_alert(*, text: str, error: str, cycle_index: Optional[int]) -> None:
    webhook = (os.getenv("TELEGRAM_WEBHOOK_URL") or "").strip()
    timeout_s = float((os.getenv("TELEGRAM_NOTIFY_TIMEOUT_S") or "15").strip() or "15")

    if webhook:
        _post_json(
            url=webhook,
            payload=_build_webhook_payload(text=text, error=error, cycle_index=cycle_index),
            timeout_s=timeout_s,
        )
        return

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _post_json(
        url=url,
        payload={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout_s=timeout_s,
    )


def maybe_notify_vari_portfolio_auth_failure(
    exc: BaseException,
    *,
    cycle_index: Optional[int] = None,
    log: Optional[Any] = None,
) -> bool:
    """
    Send a Telegram alert for portfolio VariAuthError (401).

    Returns True when a notification was sent. Respects TELEGRAM_AUTH_ALERT_COOLDOWN_S
    (default 30m) so an expired VR_TOKEN does not spam every cycle.
    """
    if not is_vari_portfolio_auth_error(exc):
        return False
    if not _notify_enabled():
        if log is not None:
            log(
                "telegram: skip auth alert — set TELEGRAM_WEBHOOK_URL or "
                "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"
            )
        return False

    global _LAST_SENT_MONO
    cooldown = _cooldown_s()
    now = time.monotonic()
    with _LOCK:
        if cooldown > 0 and _LAST_SENT_MONO and (now - _LAST_SENT_MONO) < cooldown:
            return False
        _LAST_SENT_MONO = now

    error = str(exc)
    text = _format_auth_alert(cycle_index=cycle_index)
    try:
        _send_auth_alert(text=text, error=error, cycle_index=cycle_index)
    except Exception as send_err:
        if log is not None:
            log(f"telegram: auth alert failed ({type(send_err).__name__}: {send_err})")
        with _LOCK:
            _LAST_SENT_MONO = 0.0
        return False

    if log is not None:
        log("telegram: sent auth failure alert")
    return True
