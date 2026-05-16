from __future__ import annotations

import math
import os
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .client import VariClient


def indicative_qty_sigfigs() -> int:
    raw = (os.environ.get("INDICATIVE_QTY_SIGFIGS") or "6").strip()
    try:
        return max(1, min(18, int(raw)))
    except ValueError:
        return 6


def format_qty_for_indicative_api(qty: float, *, significant: Optional[int] = None) -> str:
    """
    String qty for POST /api/quotes/indicative (``payload["qty"]``).

    Uses **significant figures** (general format), not fixed decimal places — e.g. ``1.244274`` →
    ``'1.24427'`` and ``0.001243`` → ``'0.001243'`` with the default 6 significant digits (matches
    ``format(float, '.6g')`` in Python).
    """
    n = int(significant) if significant is not None else indicative_qty_sigfigs()
    qf = float(qty)
    if not math.isfinite(qf) or qf == 0.0:
        return "0"
    return format(qf, f".{int(n)}g")


def grid_limit_qty_sigfigs() -> int:
    """Significant figures for grid limit ``qty`` strings (default 4)."""
    raw = (os.environ.get("GRID_LIMIT_QTY_SIGFIGS") or "4").strip()
    try:
        return max(1, min(18, int(raw)))
    except ValueError:
        return 4


def format_qty_for_grid_limit(qty: float) -> str:
    """Format base-asset qty for POST /api/orders/new/limit (grid / reconcile path)."""
    return format_qty_for_indicative_api(qty, significant=grid_limit_qty_sigfigs())


def align_order_qty_to_quote_limits(*, q1: Dict[str, Any], side: str, qty_raw: float) -> float:
    """Floor ``qty_raw`` to venue min_qty_tick (from indicative ``q1``) and enforce min_qty."""
    qty_limits = q1.get("qty_limits") if isinstance(q1.get("qty_limits"), dict) else {}
    limits_for_side = None
    if side == "buy":
        limits_for_side = qty_limits.get("ask") if isinstance(qty_limits.get("ask"), dict) else None
    elif side == "sell":
        limits_for_side = qty_limits.get("bid") if isinstance(qty_limits.get("bid"), dict) else None

    tick = None
    min_qty = None
    if limits_for_side:
        try:
            tick = Decimal(str(limits_for_side.get("min_qty_tick")))
        except Exception:
            tick = None
        try:
            min_qty = Decimal(str(limits_for_side.get("min_qty")))
        except Exception:
            min_qty = None

    qty_d = Decimal(str(float(qty_raw)))
    if tick and tick > 0:
        steps = (qty_d / tick).to_integral_value(rounding=ROUND_DOWN)
        qty_d = steps * tick
    if min_qty and min_qty > 0 and qty_d < min_qty:
        qty_d = min_qty
    return float(qty_d)


@dataclass(frozen=True)
class LeverageResult:
    current: int
    max: int


@dataclass(frozen=True)
class Instrument:
    instrument_type: str
    underlying: str
    funding_interval_s: int = 3600
    settlement_asset: str = "USDC"


class VariEndpoints:
    def __init__(self, client: VariClient) -> None:
        self.client = client

    def get_positions(self) -> Any:
        return self.client.request_json("GET", "/api/positions")

    def get_portfolio(self, *, compute_margin: bool = False) -> Any:
        path = "/api/portfolio"
        if compute_margin:
            path += "?compute_margin=true"
        return self.client.request_json("GET", path)

    def get_orders_v2(self) -> Any:
        return self.client.request_json("GET", "/api/orders/v2")

    def get_orders_v2_query(self, params: Dict[str, Any]) -> Any:
        """GET /api/orders/v2 with query params (pagination, instrument, status, date window)."""
        q = {str(k): str(v) for k, v in params.items() if v is not None}
        path = "/api/orders/v2"
        if q:
            path = f"{path}?{urlencode(q)}"
        return self.client.request_json("GET", path)

    def cancel_order_rfq(self, *, rfq_id: str) -> Any:
        """POST /api/orders/cancel — Omni uses ``{"rfq_id": "<id>"}`` for resting / RFQ flow."""
        rid = str(rfq_id).strip()
        if not rid:
            raise ValueError("rfq_id is empty")
        return self.client.request_json("POST", "/api/orders/cancel", json_body={"rfq_id": rid})

    def set_leverage(self, *, asset: str, leverage: int) -> LeverageResult:
        body = {"asset": asset, "leverage": str(int(leverage))}
        resp = self.client.request_json("POST", "/api/settlement_pools/set_leverage", json_body=body)
        # Expected: {"current":"20","max":"50"}
        current = int(resp.get("current")) if isinstance(resp, dict) and "current" in resp else int(leverage)
        max_lev = int(resp.get("max")) if isinstance(resp, dict) and "max" in resp else int(leverage)
        return LeverageResult(current=current, max=max_lev)

    def quote_indicative(
        self,
        *,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        DevTools-confirmed endpoint: POST /api/quotes/indicative

        Expected request payload (observed):
          {
            "instrument": {
              "instrument_type": "perpetual_future",
              "underlying": "SOL",
              "funding_interval_s": 3600,
              "settlement_asset": "USDC"
            },
            "qty": "0.601"
          }
        """
        resp = self.client.request_json("POST", "/api/quotes/indicative", json_body=payload)
        if not isinstance(resp, dict):
            raise TypeError("Expected dict response from /api/quotes/indicative")
        return resp

    def quote_indicative_simple(
        self,
        *,
        instrument: Instrument,
        qty: float,
    ) -> Dict[str, Any]:
        payload = {
            "instrument": {
                "instrument_type": instrument.instrument_type,
                "underlying": instrument.underlying,
                "funding_interval_s": int(instrument.funding_interval_s),
                "settlement_asset": instrument.settlement_asset,
            },
            "qty": format_qty_for_indicative_api(float(qty)),
        }
        return self.quote_indicative(payload=payload)

    def quote_id_for_usd_notional(
        self,
        *,
        instrument: Instrument,
        side: str,
        usd_notional: float,
        price_hint_qty: float = 1.0,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        The quote endpoint takes `qty` (token amount), but your bot sizes in USD.
        We do a 2-step quote:
        1) quote with qty=price_hint_qty to get a price (mark/index/ask/bid)
        2) compute qty ~= usd_notional / mark_price (fallback to index/ask)
        3) quote again with computed qty and return quote_id (+ final quote response)
        """
        q1 = self.quote_indicative_simple(instrument=instrument, qty=price_hint_qty)
        price = None
        for k in ("mark_price", "index_price", "ask", "bid"):
            if k in q1 and q1[k] is not None:
                try:
                    price = float(q1[k])
                    break
                except Exception:
                    continue
        if not price or price <= 0:
            raise ValueError("Could not derive price from indicative quote response")

        qty_raw = float(usd_notional) / float(price)
        qty = align_order_qty_to_quote_limits(q1=q1, side=side, qty_raw=qty_raw)
        q2 = self.quote_indicative_simple(instrument=instrument, qty=qty)
        quote_id = q2.get("quote_id") or q2.get("quoteId") or q2.get("id")
        if not quote_id:
            raise ValueError("Indicative quote response missing quote_id")
        return str(quote_id), q2

    def quote_id_for_order_qty(
        self,
        *,
        instrument: Instrument,
        side: str,
        order_qty: float,
        price_hint_qty: float = 1.0,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Two-step indicative quote for a target **base-asset qty** (same tick alignment as USD path).

        1) ``price_hint_qty`` probe for ``qty_limits`` on the relevant side.
        2) Floor ``order_qty`` to ``min_qty_tick`` / ``min_qty``, then quote again for ``quote_id``.
        """
        q1 = self.quote_indicative_simple(instrument=instrument, qty=float(price_hint_qty))
        qty = align_order_qty_to_quote_limits(q1=q1, side=side, qty_raw=float(order_qty))
        q2 = self.quote_indicative_simple(instrument=instrument, qty=qty)
        quote_id = q2.get("quote_id") or q2.get("quoteId") or q2.get("id")
        if not quote_id:
            raise ValueError("Indicative quote response missing quote_id")
        return str(quote_id), q2

    def place_order_market(
        self,
        *,
        quote_id: str,
        side: str,
        max_slippage: float,
        is_reduce_only: bool = False,
    ) -> Dict[str, Any]:
        body = {
            "quote_id": quote_id,
            "side": side,
            "max_slippage": float(max_slippage),
            "is_reduce_only": bool(is_reduce_only),
        }
        resp = self.client.request_json("POST", "/api/orders/new/market", json_body=body)
        if not isinstance(resp, dict):
            # sometimes APIs return text; preserve for debugging
            return {"raw": resp}
        return resp

    def qty_string_for_usd_at_price(
        self,
        *,
        instrument: Instrument,
        side: str,
        usd_notional: float,
        price: float,
        price_hint_qty: float = 1.0,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        Size a limit leg: convert USD notional to an indicative qty string at the given limit price
        (same idea as multimarket grid / ``multimarketorder_simple``).
        """
        px = float(price)
        if not math.isfinite(px) or px <= 0:
            raise ValueError("qty_string_for_usd_at_price: invalid limit price")
        q1 = self.quote_indicative_simple(instrument=instrument, qty=float(price_hint_qty))
        qty_raw = float(usd_notional) / px
        qty = align_order_qty_to_quote_limits(q1=q1, side=side, qty_raw=qty_raw)
        q2 = self.quote_indicative_simple(instrument=instrument, qty=qty)
        return format_qty_for_indicative_api(qty), q1, q2

    def normalize_grid_limit_qty(
        self,
        *,
        instrument: Instrument,
        side: str,
        qty_raw: float,
        price_hint_qty: float = 1.0,
    ) -> str:
        """
        Floor to venue ``min_qty_tick`` from indicative quote, then format with
        ``GRID_LIMIT_QTY_SIGFIGS`` (default 4).
        """
        q1 = self.quote_indicative_simple(instrument=instrument, qty=float(price_hint_qty))
        qty = align_order_qty_to_quote_limits(q1=q1, side=side, qty_raw=float(qty_raw))
        return format_qty_for_grid_limit(qty)

    def place_order_limit(
        self,
        *,
        instrument: Instrument,
        side: str,
        limit_price: float,
        qty: str,
        slippage_limit: float,
        use_mark_price: bool = False,
        is_reduce_only: bool = False,
        is_auto_resize: bool = False,
    ) -> Dict[str, Any]:
        """POST /api/orders/new/limit (Omni RFQ flow)."""
        lp = round(float(limit_price), 2)
        body: Dict[str, Any] = {
            "order_type": "limit",
            "limit_price": f"{lp:.2f}",
            "side": str(side).strip().lower(),
            "instrument": {
                "instrument_type": instrument.instrument_type,
                "underlying": instrument.underlying,
                "funding_interval_s": int(instrument.funding_interval_s),
                "settlement_asset": instrument.settlement_asset,
            },
            "qty": str(qty).strip(),
            "slippage_limit": str(float(slippage_limit)),
            "use_mark_price": bool(use_mark_price),
            "is_reduce_only": bool(is_reduce_only),
            "is_auto_resize": bool(is_auto_resize),
        }
        resp = self.client.request_json("POST", "/api/orders/new/limit", json_body=body)
        if not isinstance(resp, dict):
            return {"raw": resp}
        return resp

