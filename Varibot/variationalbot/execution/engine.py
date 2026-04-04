from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from variationalbot.domain.models import PortfolioSnapshot
from variationalbot.risk.margin_guard import GuardDecision, leverage_guard
from variationalbot.vari.endpoints import Instrument, VariEndpoints


@dataclass(frozen=True)
class OrderIntent:
    asset: str
    side: str  # "buy" | "sell"
    usd_size: float
    leverage: int
    max_slippage: float
    reduce_only: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    mode: str
    guard: GuardDecision
    intents: List[OrderIntent]
    placed: List[Dict[str, Any]]
    errors: List[str]


def build_intents_from_signals(
    *,
    signal_payload: Dict[str, Any],
    default_leverage: int,
    max_slippage: float,
) -> List[OrderIntent]:
    intents: List[OrderIntent] = []

    def _handle(side: str, items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            asset = str(it.get("asset") or it.get("symbol") or "").strip().upper()
            usd_size = it.get("usd_size") or it.get("usd") or it.get("size_usd") or it.get("notional_usd")
            if not asset:
                continue
            try:
                usd = float(usd_size)
            except Exception:
                continue
            lev = int(it.get("leverage") or default_leverage)
            intents.append(
                OrderIntent(
                    asset=asset,
                    side=side,
                    usd_size=usd,
                    leverage=lev,
                    max_slippage=float(it.get("max_slippage") or max_slippage),
                    reduce_only=bool(it.get("reduce_only") or False),
                    meta={k: v for k, v in it.items() if k not in {"asset", "symbol", "usd_size", "usd", "size_usd", "notional_usd"}},
                )
            )

    _handle("buy", signal_payload.get("long"))
    _handle("sell", signal_payload.get("short"))
    return intents


def execute_intents(
    *,
    mode: str,
    endpoints: VariEndpoints,
    snapshot: PortfolioSnapshot,
    intents: List[OrderIntent],
    max_leverage: int,
) -> ExecutionResult:
    """
    v0 execution:
    - Always apply leverage guard.
    - In paper mode: no API mutations; just return intents + guard.
    - In live mode: for each intent:
      1) set leverage for the asset
      2) request indicative quote to get quote_id (USD sizing handled via 2-step quote)
      3) place market order
    """
    guard = leverage_guard(snapshot=snapshot, max_leverage=max_leverage)
    placed: List[Dict[str, Any]] = []
    errors: List[str] = []

    if not guard.ok:
        return ExecutionResult(ok=False, mode=mode, guard=guard, intents=intents, placed=placed, errors=[guard.reason])

    if mode != "live":
        # paper
        return ExecutionResult(ok=True, mode=mode, guard=guard, intents=intents, placed=placed, errors=errors)

    for intent in intents:
        try:
            lev_res = endpoints.set_leverage(asset=intent.asset, leverage=intent.leverage)
            if lev_res.current != intent.leverage:
                errors.append(f"leverage mismatch for {intent.asset}: wanted {intent.leverage}, got {lev_res.current}")

            instrument = Instrument(
                instrument_type="perpetual_future",
                underlying=intent.asset,
                funding_interval_s=3600,
                settlement_asset="USDC",
            )
            quote_id, quote = endpoints.quote_id_for_usd_notional(
                instrument=instrument,
                side=intent.side,
                usd_notional=float(intent.usd_size),
            )

            if guard.reduce_only:
                # do not open new risk if we're near MM
                continue

            resp = endpoints.place_order_market(
                quote_id=quote_id,
                side=intent.side,
                max_slippage=float(intent.max_slippage),
                is_reduce_only=bool(intent.reduce_only),
            )
            placed.append({"asset": intent.asset, "quote": quote, "resp": resp, "ts": time.time()})
        except Exception as e:
            errors.append(f"{intent.asset}: {e}")

    return ExecutionResult(ok=len(errors) == 0, mode=mode, guard=guard, intents=intents, placed=placed, errors=errors)

