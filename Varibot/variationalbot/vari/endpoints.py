from __future__ import annotations

import json
import math
import os
import re
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .client import VariClient
from .errors import VariUnexpectedResponse


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


def grid_limit_price_decimals(price: float) -> int:
    """
    Decimal places for grid limit prices and (side, price) reconcile keys.

    Two decimals collapses sub-$1 rungs (e.g. MON ~0.027 → all ``0.03``), causing duplicate
    posts and false venue-sync fills. Override with ``GRID_LIMIT_PRICE_DECIMALS``.
    """
    raw = (os.environ.get("GRID_LIMIT_PRICE_DECIMALS") or "").strip()
    if raw:
        try:
            return max(0, min(12, int(raw)))
        except ValueError:
            pass
    p = abs(float(price))
    if not math.isfinite(p) or p <= 0:
        return 2
    if p >= 1000:
        return 2
    if p >= 100:
        return 2
    if p >= 10:
        return 3
    if p >= 1:
        return 4
    if p >= 0.1:
        return 5
    if p >= 0.01:
        return 5
    return 6


def format_grid_limit_price(price: float) -> str:
    """Rounded limit price string for POST /api/orders/new/limit and reconcile keys."""
    d = grid_limit_price_decimals(price)
    return f"{round(float(price), d):.{d}f}"


def grid_limit_price_key(price: float) -> str:
    """Normalized price component of a pending/template limit key."""
    return format_grid_limit_price(price)


def limit_price_key(side: str, price: float) -> Tuple[str, str]:
    """(buy|sell, normalized_price) for template ↔ venue pending matching."""
    return (str(side).strip().lower(), grid_limit_price_key(price))


def format_qty_for_grid_limit(qty: float) -> str:
    """Format base-asset qty for POST /api/orders/new/limit (grid / reconcile path)."""
    n = grid_limit_qty_sigfigs()
    qf = float(qty)
    if not math.isfinite(qf) or qf == 0.0:
        return "0"
    s = format(qf, f".{int(n)}g")
    if "e" not in s.lower():
        return s
    # Avoid scientific notation (e.g. MON ``1.108e+04``) in limit API / gridlimits.json.
    exp = int(math.floor(math.log10(abs(qf))))
    decimals = max(0, min(8, n - exp - 1))
    rounded = round(qf, decimals)
    out = f"{rounded:.{decimals}f}"
    return out.rstrip("0").rstrip(".") if "." in out else out


def parse_cancel_ban_wait_seconds(exc: BaseException) -> Optional[float]:
    """
    Seconds to sleep after HTTP 418 cancel-ban (``/api/orders/cancel``).

    Omni body example::
        {"endpoint":"cancels","error":"banned","wait_until_seconds":11,...}
    """
    msg = str(exc)
    if "418" not in msg and "banned" not in msg.lower():
        return None
    try:
        idx = msg.index("{")
        body = json.loads(msg[idx:])
        if isinstance(body, dict):
            w = body.get("wait_until_seconds")
            if w is not None:
                return max(1.0, float(w))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    m = re.search(r"wait\s+(\d+)\s+seconds", msg, re.I)
    if m:
        return max(1.0, float(m.group(1)))
    return 12.0


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


_DEFAULT_RWA_COMMODITY_UNDERLYINGS: frozenset[str] = frozenset({"XAU", "CL", "XAG", "COPPER"})


def rwa_commodity_underlyings() -> frozenset[str]:
    """
    RWA commodity perps (``perpetual_rwa_future``).

    Primary source: ``GRID_RWA_TICKERS`` env (set from ``strategy.gridstrat.GRID_RWA_COMMODITY_TICKERS``
    on import). Falls back to ``_DEFAULT_RWA_COMMODITY_UNDERLYINGS`` if unset.
    """
    raw = (os.environ.get("GRID_RWA_TICKERS") or "").strip()
    if raw:
        return frozenset({p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()})
    return _DEFAULT_RWA_COMMODITY_UNDERLYINGS


@dataclass(frozen=True)
class Instrument:
    instrument_type: str
    underlying: str
    settlement_asset: str = "USDC"
    funding_interval_s: Optional[int] = 3600
    kind: Optional[str] = None

    def to_api_dict(self) -> Dict[str, Any]:
        """Serialize for POST /api/quotes/indicative and /api/orders/new/limit."""
        d: Dict[str, Any] = {
            "instrument_type": self.instrument_type,
            "underlying": str(self.underlying).strip().upper(),
            "settlement_asset": self.settlement_asset,
        }
        if self.kind:
            d["kind"] = self.kind
        if self.instrument_type == "perpetual_future" and self.funding_interval_s is not None:
            d["funding_interval_s"] = int(self.funding_interval_s)
        return d

    @classmethod
    def for_underlying(cls, underlying: str) -> "Instrument":
        sym = str(underlying).strip().upper()
        if sym in rwa_commodity_underlyings():
            return cls(
                instrument_type="perpetual_rwa_future",
                underlying=sym,
                settlement_asset="USDC",
                funding_interval_s=None,
                kind="commodity",
            )
        return cls(
            instrument_type="perpetual_future",
            underlying=sym,
            funding_interval_s=3600,
            settlement_asset="USDC",
        )


def instrument_query_param(asset: str) -> Optional[str]:
    """
    GET /api/orders/v2 ``instrument`` filter (crypto perps only).

  Crypto (DevTools): ``P-BTC-USDC-3600`` — four segments after ``P-``.
  RWA commodity (``perpetual_rwa_future``): ``P-XAU-USDC`` returns HTTP 400
  ``Incorrect number of fields provided``. Omit the query param and filter by
  ``instrument.underlying`` client-side (see ``grid_limits_reconcile``).
    """
    sym = str(asset).strip().upper()
    if sym in rwa_commodity_underlyings():
        return None
    return f"P-{sym}-USDC-3600"


def fetch_orders_v2_pending(
    client: VariClient,
    *,
    instrument: Optional[str] = None,
    status: str = "pending",
) -> Any:
    """GET /api/orders/v2 with optional instrument filter (omit when ``instrument`` is None)."""
    path = f"/api/orders/v2?status={status}"
    if instrument:
        path = f"{path}&instrument={instrument}"
    return client.request_json("GET", path)


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

    def get_supported_assets(self) -> Dict[str, Any]:
        """
        GET /api/metadata/supported_assets — Omni UI bulk market metadata.

        Response is a dict keyed by underlying (e.g. ``ETH``, ``XAU``); each value is a list
        of one or more rows with ``index_price``, ``price``, funding, etc.
        """
        resp = self.client.request_json("GET", "/api/metadata/supported_assets")
        if not isinstance(resp, dict):
            raise TypeError("Expected dict response from /api/metadata/supported_assets")
        return resp

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

    def cancel_order_rfq_resilient(
        self,
        *,
        rfq_id: str,
        max_attempts: int = 12,
        buffer_s: float = 0.75,
        on_wait: Optional[Callable[..., None]] = None,
    ) -> Any:
        """
        Cancel with retries when Omni returns HTTP 418 cancel-ban
        (``wait_until_seconds`` in JSON body).
        """
        rid = str(rfq_id).strip()
        if not rid:
            raise ValueError("rfq_id is empty")
        last_err: Optional[Exception] = None
        for attempt in range(max(1, int(max_attempts))):
            try:
                return self.cancel_order_rfq(rfq_id=rid)
            except VariUnexpectedResponse as e:
                wait_s = parse_cancel_ban_wait_seconds(e)
                if wait_s is None:
                    raise
                last_err = e
                if attempt >= int(max_attempts) - 1:
                    break
                sleep_s = float(wait_s) + float(buffer_s)
                if on_wait is not None:
                    try:
                        on_wait(sleep_s, attempt + 1, rid)
                    except TypeError:
                        on_wait(sleep_s)
                time.sleep(sleep_s)
        assert last_err is not None
        raise last_err

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
            "instrument": instrument.to_api_dict(),
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
        lp_s = format_grid_limit_price(limit_price)
        body: Dict[str, Any] = {
            "order_type": "limit",
            "limit_price": lp_s,
            "side": str(side).strip().lower(),
            "instrument": instrument.to_api_dict(),
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

