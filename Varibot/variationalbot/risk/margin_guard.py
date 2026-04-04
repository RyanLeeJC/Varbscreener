from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from variationalbot.domain.models import PortfolioSnapshot


@dataclass(frozen=True)
class GuardDecision:
    ok: bool
    reason: str
    reduce_only: bool = False


def leverage_guard(
    *,
    snapshot: PortfolioSnapshot,
    max_leverage: int,
) -> GuardDecision:
    """
    Day-1 risk gate focused on leverage/margin.

    Because portfolio response schema is still evolving, we use best-effort fields:
    - portfolio_leverage if available
    - margin_ratio / IM/MM usage as secondary safety checks
    """
    lev = snapshot.portfolio_leverage
    if lev is not None and lev > float(max_leverage):
        return GuardDecision(ok=False, reason=f"portfolio_leverage {lev:.2f} > max_leverage {max_leverage}")

    # If near/over maintenance margin usage, force reduce-only behavior (no new exposure)
    if snapshot.mm_usage is not None and snapshot.mm_usage >= 0.9:
        return GuardDecision(ok=True, reason="mm_usage high; reduce-only mode", reduce_only=True)

    # If initial margin usage high, block new exposure
    if snapshot.im_usage is not None and snapshot.im_usage >= 0.8:
        return GuardDecision(ok=False, reason="im_usage too high to add risk")

    return GuardDecision(ok=True, reason="ok")

