from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PortfolioSnapshot:
    portfolio_value_usd: Optional[float] = None
    unrealized_pnl_usd: Optional[float] = None
    margin_ratio: Optional[float] = None
    portfolio_leverage: Optional[float] = None

    im_usage: Optional[float] = None  # 0..1
    mm_usage: Optional[float] = None  # 0..1

    raw: Dict[str, Any] = field(default_factory=dict)


def _first_number(obj: Any, keys: List[str]) -> Optional[float]:
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] is not None:
            try:
                return float(obj[k])
            except Exception:
                continue
    return None


def _as_ratio(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    # Some APIs return percent (0-100). Normalize to 0..1 when needed.
    if v > 1.5:
        return v / 100.0
    return v


def parse_portfolio_snapshot(payload: Any) -> PortfolioSnapshot:
    """
    Best-effort parsing of /api/portfolio JSON into normalized fields.

    We don't yet have the exact response schema, so this supports multiple
    possible key names and preserves `raw` for debugging.
    """
    raw: Dict[str, Any]
    if isinstance(payload, dict):
        raw = payload
    else:
        raw = {"_raw": payload}

    # Common key guesses (+ observed /api/portfolio?compute_margin=true response)
    portfolio_value = _first_number(
        raw,
        [
            "portfolio_value",
            "portfolioValue",
            "equity",
            "equity_usd",
            "equityUsd",
            "balance",  # observed
        ],
    )
    unrealized_pnl = _first_number(
        raw,
        [
            "unrealized_pnl",
            "unrealizedPnl",
            "unrealized_pnl_usd",
            "unrealizedPnlUsd",
            "upnl",  # observed
        ],
    )

    margin_ratio = _first_number(raw, ["margin_ratio", "marginRatio"])
    portfolio_leverage = _first_number(raw, ["portfolio_leverage", "portfolioLeverage", "leverage"])

    im_usage = _first_number(raw, ["im_usage", "imUsage", "initial_margin_usage", "initialMarginUsage", "im", "IM"])
    mm_usage = _first_number(raw, ["mm_usage", "mmUsage", "maintenance_margin_usage", "maintenanceMarginUsage", "mm", "MM"])

    # Observed nested: margin_usage: {initial_margin:"...", maintenance_margin:"..."}
    margin_usage = raw.get("margin_usage") if isinstance(raw.get("margin_usage"), dict) else None
    if margin_usage:
        initial_margin = _first_number(margin_usage, ["initial_margin"])
        maintenance_margin = _first_number(margin_usage, ["maintenance_margin"])
        if portfolio_value and portfolio_value > 0:
            if initial_margin is not None:
                im_usage = initial_margin / portfolio_value
            if maintenance_margin is not None:
                mm_usage = maintenance_margin / portfolio_value

    # Sometimes nested objects
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    if usage:
        im_usage = im_usage if im_usage is not None else _first_number(usage, ["im", "im_usage", "initial", "initial_margin"])
        mm_usage = mm_usage if mm_usage is not None else _first_number(usage, ["mm", "mm_usage", "maintenance", "maintenance_margin"])

    return PortfolioSnapshot(
        portfolio_value_usd=portfolio_value,
        unrealized_pnl_usd=unrealized_pnl,
        margin_ratio=_as_ratio(margin_ratio),
        portfolio_leverage=portfolio_leverage,
        im_usage=_as_ratio(im_usage),
        mm_usage=_as_ratio(mm_usage),
        raw=raw,
    )

