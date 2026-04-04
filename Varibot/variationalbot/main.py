from __future__ import annotations

import os
import time
from typing import Any, Dict

from variationalbot.config import load_config
from variationalbot.domain import parse_portfolio_snapshot
from variationalbot.execution import build_intents_from_signals, execute_intents
from variationalbot.signals import run_signal_script
from variationalbot.state.store import StateStore
from variationalbot.util.logging import log_json
from variationalbot.util.time import sleep_with_drift_correction
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.errors import VariAuthError, VariCloudflareError, VariForbiddenError


def _ensure_dirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def run_once(ep: VariEndpoints, store: StateStore) -> Dict[str, Any]:
    portfolio_raw = ep.get_portfolio()
    positions_raw = ep.get_positions()
    orders_raw = ep.get_orders_v2()

    snap = parse_portfolio_snapshot(portfolio_raw)

    store.set("last_portfolio_raw", portfolio_raw)
    store.set("last_positions_raw", positions_raw)
    store.set("last_orders_raw", orders_raw)
    store.set("last_snapshot", snap)

    # Optional external signals (paper by default). Provide BOT_SIGNAL_SCRIPT=path/to/script.py
    signal_script = os.getenv("BOT_SIGNAL_SCRIPT", "").strip()
    signal_data: Dict[str, Any] = {}
    intents_count = 0
    exec_ok = None
    reduce_only = None

    if signal_script:
        sr = run_signal_script(
            script_path=signal_script,
            input_json={"portfolio": portfolio_raw, "positions": positions_raw, "orders": orders_raw},
            timeout_s=int(os.getenv("BOT_SIGNAL_TIMEOUT_S", "120")),
        )
        store.set("last_signal_result", sr)
        signal_data = sr.data if sr.ok else {}

        intents = build_intents_from_signals(
            signal_payload=signal_data,
            default_leverage=int(os.getenv("DEFAULT_LEVERAGE", "20")),
            max_slippage=float(os.getenv("MAX_SLIPPAGE", "0.002")),
        )
        intents_count = len(intents)

        # Execution (paper/live switch)
        ex = execute_intents(
            mode=os.getenv("BOT_MODE", "paper").strip().lower(),
            endpoints=ep,
            snapshot=snap,
            intents=intents,
            max_leverage=int(os.getenv("MAX_LEVERAGE", "50")),
        )
        store.set("last_execution_result", ex)
        exec_ok = ex.ok
        reduce_only = ex.guard.reduce_only

    summary = {
        "portfolio_value_usd": snap.portfolio_value_usd,
        "im_usage": snap.im_usage,
        "mm_usage": snap.mm_usage,
        "margin_ratio": snap.margin_ratio,
        "portfolio_leverage": snap.portfolio_leverage,
        "positions_count": len(positions_raw) if isinstance(positions_raw, list) else None,
        "intents_count": intents_count,
        "exec_ok": exec_ok,
        "reduce_only": reduce_only,
    }
    return summary


def main() -> int:
    cfg = load_config()
    _ensure_dirs(cfg.runs_dir)

    store = StateStore(cfg.state_db_path)
    client = VariClient(base_url=cfg.base_url, auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token))
    ep = VariEndpoints(client)

    # startup probe (auth + Cloudflare check)
    try:
        probe = client.health_probe()
        log_json("startup_probe_ok", probe=probe)
    except Exception as e:
        log_json("startup_probe_failed", error=str(e))
        return 2

    log_json(
        "bot_started",
        mode=cfg.mode,
        poll_interval_s=cfg.poll_interval_s,
        base_url=cfg.base_url,
        wallet=cfg.wallet_address,
    )

    while True:
        cycle_started = time.time()
        try:
            summary = run_once(ep, store)
            store.set("last_cycle_ok", {"ts": cycle_started, "summary": summary})
            log_json("cycle_ok", **summary)
        except VariAuthError as e:
            log_json("cycle_auth_error", error=str(e))
            return 3
        except (VariForbiddenError, VariCloudflareError) as e:
            # transient-ish, keep running; next cycle may succeed
            store.set("last_cycle_soft_error", {"ts": cycle_started, "error": str(e)})
            log_json("cycle_soft_error", error=str(e))
        except Exception as e:
            store.set("last_cycle_error", {"ts": cycle_started, "error": str(e)})
            log_json("cycle_error", error=str(e))

        sleep_with_drift_correction(period_s=cfg.poll_interval_s, started_at=cycle_started)


if __name__ == "__main__":
    raise SystemExit(main())

