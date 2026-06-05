from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from variationalbot.util import telegram_notify as tn
from variationalbot.vari.errors import VariAuthError


def test_is_vari_portfolio_auth_error() -> None:
    assert tn.is_vari_portfolio_auth_error(
        VariAuthError("401 unauthorized for GET /api/portfolio?compute_margin=true")
    )
    assert not tn.is_vari_portfolio_auth_error(
        VariAuthError("401 unauthorized for GET /api/positions")
    )
    assert not tn.is_vari_portfolio_auth_error(RuntimeError("nope"))


def test_maybe_notify_skips_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    tn._LAST_SENT_MONO = 0.0
    exc = VariAuthError("401 unauthorized for GET /api/portfolio?compute_margin=true")
    assert tn.maybe_notify_vari_portfolio_auth_failure(exc) is False


def test_mask_wallet() -> None:
    assert tn._mask_wallet("0xbc31c7582489484e81ba39a7ce020a76d73e9a59") == "0xbc31…9a59"
    assert tn._mask_wallet("0x123456") == "0x123456"
    assert tn._mask_wallet("") == "—"


def test_wallet_label_prefers_explicit_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VR_WALLET_ADDRESS", "0xbc31c7582489484e81ba39a7ce020a76d73e9a59")
    assert tn._wallet_label(wallet_address="0x1c993748bd640c7263fa72fdf3e1a506fe2baf96") == "0x1c99…af96"


def test_format_auth_alert() -> None:
    fixed = tn.datetime(2026, 6, 5, 15, 24, 4, tzinfo=tn._SGT)
    with patch.object(tn, "_deployment_label", return_value="Gridbot"), patch.object(
        tn, "_wallet_label", return_value="0xbc31…9a59"
    ), patch("variationalbot.util.telegram_notify.datetime") as dt_mod:
        dt_mod.now.return_value = fixed
        text = tn._format_auth_alert(cycle_index=1380)
    assert text == (
        "Auth failure on Vari>Gridbot (0xbc31…9a59)\n"
        "Cycle: 1380\n"
        "Time: 15:24:04 SGT 5 Jun 2026\n"
        "Please update vr-token in Render."
    )


def test_maybe_notify_sends_via_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "Gridbot")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    tn._LAST_SENT_MONO = 0.0
    exc = VariAuthError("401 unauthorized for GET /api/portfolio?compute_margin=true")

    with patch.object(tn, "_post_json") as post:
        assert tn.maybe_notify_vari_portfolio_auth_failure(exc, cycle_index=1380) is True
        post.assert_called_once()
        payload = post.call_args.kwargs["payload"]
        assert payload["cycle"] == 1380
        assert payload["cycle_index"] == 1380
        assert payload["deployment"] == "Gridbot"
        assert "Auth failure on Vari>Gridbot" in payload["text"]
        assert "Please update vr-token in Render." in payload["text"]
        assert "/api/portfolio" in payload["error"]
        assert "Cycle: 1380" in payload["text"]


def test_maybe_notify_respects_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setenv("TELEGRAM_AUTH_ALERT_COOLDOWN_S", "3600")
    tn._LAST_SENT_MONO = 0.0
    exc = VariAuthError("401 unauthorized for GET /api/portfolio?compute_margin=true")

    with patch.object(tn, "_post_json"):
        assert tn.maybe_notify_vari_portfolio_auth_failure(exc) is True
        assert tn.maybe_notify_vari_portfolio_auth_failure(exc) is False
