from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .client import VariClient


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
            "qty": f"{float(qty):.6f}".rstrip("0").rstrip("."),
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

        # Align qty to tick size so order placement doesn't fail (422).
        # We derive tick/min qty from the quote response if available.
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

        qty_d = Decimal(str(qty_raw))
        if tick and tick > 0:
            # floor to tick to avoid exceeding notional / invalid multiples
            steps = (qty_d / tick).to_integral_value(rounding=ROUND_DOWN)
            qty_d = steps * tick
        if min_qty and min_qty > 0 and qty_d < min_qty:
            qty_d = min_qty

        qty = float(qty_d)
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

