from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from strategy.gridstrat import _default_state_path

from variationalbot.vari.endpoints import VariEndpoints

# Canonical ladder template for mid-session recovery (written from strategy meta).
DEFAULT_GRID_LIMITS_PATH_ENV: str = "GRID_LIMITS_JSON_PATH"
DEFAULT_GRID_LIMITS_FILENAME: str = "gridlimits.json"

# Set to 1 to POST missing limits from gridlimits.json when flat and gridstrat emitted 0 events.
ENV_GRID_LIMITS_RECONCILE: str = "VARIBOT_GRID_LIMITS_RECONCILE"
# Set to 1 to cancel venue limits not in the strategy template and post missing template rungs (sim drift).
ENV_GRID_LIMITS_DRIFT_RECONCILE: str = "VARIBOT_GRID_LIMITS_DRIFT_RECONCILE"
# Sub-gates (default on when drift reconcile is on): set to 0/false to disable one leg only.
ENV_GRID_LIMITS_DRIFT_CANCEL: str = "VARIBOT_GRID_LIMITS_DRIFT_CANCEL"
ENV_GRID_LIMITS_DRIFT_REFILL: str = "VARIBOT_GRID_LIMITS_DRIFT_REFILL"
ENV_GRID_LIMITS_RECONCILE_WITH_POSITIONS: str = "VARIBOT_GRID_LIMITS_RECONCILE_WITH_POSITIONS"
ENV_GRID_LIMITS_CANCEL_SLEEP_S: str = "VARIBOT_GRID_LIMITS_CANCEL_SLEEP_S"
# Set to 1 to fetch paginated order history and log the mental map every cycle (noisy / API heavy).
ENV_GRID_LIMITS_MAP_EACH_CYCLE: str = "VARIBOT_GRID_LIMITS_MAP_EACH_CYCLE"
ENV_GRID_ORDERS_HISTORY_DAYS: str = "VARIBOT_GRID_ORDERS_HISTORY_DAYS"
ENV_GRID_ORDERS_PAGE_LIMIT: str = "VARIBOT_GRID_ORDERS_PAGE_LIMIT"
ENV_GRID_ORDERS_MAX_PAGES: str = "VARIBOT_GRID_ORDERS_MAX_PAGES"


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _env_bool_default(name: str, *, default_when_unset: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default_when_unset)
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def _drift_reconcile_enabled(meta: Optional[Dict[str, Any]] = None) -> bool:
    """Explicit env, or auto-on for paired_limit when base reconcile is enabled."""
    if _truthy_env(ENV_GRID_LIMITS_DRIFT_RECONCILE):
        return True
    if _truthy_env(ENV_GRID_LIMITS_RECONCILE) and meta and meta.get("grid_paired_limit_mode"):
        return True
    return False


def _drift_cancel_enabled(meta: Optional[Dict[str, Any]] = None) -> bool:
    return _env_bool_default(
        ENV_GRID_LIMITS_DRIFT_CANCEL, default_when_unset=_drift_reconcile_enabled(meta)
    )


def _drift_refill_enabled(meta: Optional[Dict[str, Any]] = None) -> bool:
    return _env_bool_default(
        ENV_GRID_LIMITS_DRIFT_REFILL, default_when_unset=_drift_reconcile_enabled(meta)
    )


def _reconcile_with_positions_allowed(meta: Optional[Dict[str, Any]] = None) -> bool:
    if _truthy_env(ENV_GRID_LIMITS_RECONCILE_WITH_POSITIONS):
        return True
    if _drift_reconcile_enabled(meta):
        return True
    if _truthy_env(ENV_GRID_LIMITS_RECONCILE) and meta and meta.get("grid_paired_limit_mode"):
        return True
    return False


def fetch_pending_limit_keys_for_asset(ep: VariEndpoints, *, asset: str) -> Set[Tuple[str, str]]:
    """Public helper for varibot: venue (side, price) keys for pending limits."""
    return _fetch_pending_limit_keys(ep, asset=asset)


def _grid_limits_json_path(varibot_dir: str) -> str:
    raw = (os.environ.get(DEFAULT_GRID_LIMITS_PATH_ENV) or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(varibot_dir, DEFAULT_GRID_LIMITS_FILENAME)


def _instrument_param(asset: str) -> str:
    return f"P-{str(asset).strip().upper()}-USDC-3600"


def _orders_result_rows(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("result", "orders", "data", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _row_underlying(row: Dict[str, Any]) -> str:
    inst = row.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    return ""


def _limit_price_key(row: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(buy|sell, "80139.26") for matching grid template rows to venue pending."""
    side = str(row.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    ot = str(row.get("order_type") or "").lower()
    if "limit" not in ot:
        return None
    st = str(row.get("status") or "").strip().lower()
    if st != "pending":
        return None
    lp = row.get("limit_price")
    if lp is None:
        return None
    try:
        p = round(float(lp), 2)
    except (TypeError, ValueError):
        return None
    return (side, f"{p:.2f}")


def _meta_fingerprint(meta: Dict[str, Any]) -> str:
    doc = {
        "asset": str(meta.get("grid_asset") or "").strip().upper(),
        "lower": meta.get("grid_lower"),
        "upper": meta.get("grid_upper"),
        "n": meta.get("grid_num"),
        "type": meta.get("grid_type"),
        "bounds_auto": meta.get("grid_bounds_auto"),
        "band_pct": meta.get("grid_band_pct"),
    }
    return json.dumps(doc, sort_keys=True, default=str)


def build_gridlimits_doc(
    meta: Dict[str, Any],
    *,
    venue_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    asset = str(meta.get("grid_asset") or os.environ.get("GRID_ASSET") or "BTC").strip().upper()
    per = meta.get("grid_per_rung_usd")
    try:
        per_f = float(per) if per is not None else 0.0
    except (TypeError, ValueError):
        per_f = 0.0
    qty_s = meta.get("grid_per_rung_qty")
    qty_str = str(qty_s).strip() if qty_s is not None and str(qty_s).strip() else None
    limits: List[Dict[str, Any]] = []
    for px in meta.get("grid_buy_rungs") or []:
        try:
            row: Dict[str, Any] = {"side": "buy", "limit_price": float(px), "usd": float(per_f)}
            if qty_str:
                row["qty"] = qty_str
            limits.append(row)
        except (TypeError, ValueError):
            continue
    for px in meta.get("grid_sell_rungs") or []:
        try:
            row = {"side": "sell", "limit_price": float(px), "usd": float(per_f)}
            if qty_str:
                row["qty"] = qty_str
            limits.append(row)
        except (TypeError, ValueError):
            continue
    doc: Dict[str, Any] = {
        "version": 2,
        "meta_fingerprint": _meta_fingerprint(meta),
        "grid_asset": asset,
        "grid_mark_at_write": meta.get("grid_mark"),
        "bounds": {
            "lower": meta.get("grid_lower"),
            "upper": meta.get("grid_upper"),
            "n_grids": meta.get("grid_num"),
            "grid_type": meta.get("grid_type"),
        },
        "per_rung_usd": per_f,
        "per_rung_qty": qty_str,
        "grid_limit_sizing": meta.get("grid_limit_sizing"),
        "limits": limits,
    }
    if venue_snapshot is not None:
        doc["venue_snapshot"] = venue_snapshot
    return doc


def sync_gridlimits_json(
    *,
    meta: Dict[str, Any],
    varibot_dir: str,
    venue_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Write ``gridlimits.json`` from strategy meta plus optional venue snapshot. Returns path or None."""
    if not meta.get("grid_mode"):
        return None
    path = _grid_limits_json_path(varibot_dir)
    doc = build_gridlimits_doc(meta, venue_snapshot=venue_snapshot)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def _load_gridlimits(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _row_rfq_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("rfq_id", "rfqId"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _fetch_pending_limit_rows(ep: VariEndpoints, *, asset: str) -> List[Dict[str, Any]]:
    inst = _instrument_param(asset)
    raw = ep.client.request_json("GET", f"/api/orders/v2?status=pending&instrument={inst}")
    want = str(asset).strip().upper()
    out: List[Dict[str, Any]] = []
    for row in _orders_result_rows(raw):
        if _row_underlying(row) != want:
            continue
        if _limit_price_key(row):
            out.append(row)
    return out


def _fetch_pending_limit_keys(ep: VariEndpoints, *, asset: str) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for row in _fetch_pending_limit_rows(ep, asset=asset):
        k = _limit_price_key(row)
        if k:
            out.add(k)
    return out


def _cancel_sleep_s() -> float:
    try:
        return max(0.0, float(os.environ.get(ENV_GRID_LIMITS_CANCEL_SLEEP_S, "0.2") or "0.2"))
    except (TypeError, ValueError):
        return 0.2


def _cancel_pending_orphans(
    ep: VariEndpoints,
    *,
    asset: str,
    orphan_keys: Set[Tuple[str, str]],
    log: Callable[[str], None],
) -> int:
    """Cancel pending limits whose (side, price) keys are not in the current template."""
    if not orphan_keys:
        return 0
    rows = _fetch_pending_limit_rows(ep, asset=asset)
    targets: List[Tuple[Tuple[str, str], str]] = []
    for row in rows:
        k = _limit_price_key(row)
        if k is None or k not in orphan_keys:
            continue
        rid = _row_rfq_id(row)
        if not rid:
            log(f"gridlimits drift: skip cancel {k[0]} @ {k[1]} (no rfq_id)")
            continue
        targets.append((k, rid))
    sleep_s = _cancel_sleep_s()
    ok = 0
    for i, (k, rid) in enumerate(targets):
        try:
            ep.cancel_order_rfq(rfq_id=rid)
            ok += 1
            log(f"gridlimits drift: canceled orphan {k[0]} @ {k[1]}")
        except Exception as e:
            log(f"gridlimits drift: cancel {k[0]} @ {k[1]} failed ({type(e).__name__}: {e})")
        if sleep_s > 0 and i < len(targets) - 1:
            time.sleep(sleep_s)
    return ok


def _collect_want_post_limits(
    *,
    lims: List[Any],
    pending_keys: Set[Tuple[str, str]],
    mark_f: float,
    apply_mark_filter: bool = True,
) -> List[Tuple[str, float, float, Optional[str]]]:
    """
    Template rows missing on venue.

    When ``apply_mark_filter`` is False (paired drift re-arm), post every sim open rung
    the venue lacks — same ladder as ``open_rungs_for_meta`` / simulator.
    """
    want_post: List[Tuple[str, float, float, Optional[str]]] = []
    for row in lims:
        if not isinstance(row, dict):
            continue
        side = str(row.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            continue
        try:
            px = float(row.get("limit_price"))
            usd = float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            continue
        if usd <= 0:
            continue
        rq = row.get("qty")
        lq = str(rq).strip() if rq is not None and str(rq).strip() else None
        key = (side, f"{round(px, 2):.2f}")
        if key in pending_keys:
            continue
        if apply_mark_filter:
            if side == "buy" and not (px < mark_f):
                continue
            if side == "sell" and not (px > mark_f):
                continue
        want_post.append((side, px, usd, lq))
    return want_post


def _post_missing_limits(
    *,
    meta: Dict[str, Any],
    asset: str,
    want_post: List[Tuple[str, float, float, Optional[str]]],
    place_limit: Callable[..., int],
    log: Callable[[str], None],
    log_prefix: str = "gridlimits reconcile",
) -> int:
    if not want_post:
        return 0
    lim_mark = bool(meta.get("grid_limit_use_mark_price"))
    log(f"{log_prefix}: posting {len(want_post)} missing limit(s) …")
    ok = 0
    for side, px, usd, lq in want_post:
        try:
            rc = place_limit(
                str(meta.get("grid_asset") or asset).strip().upper(),
                side,
                float(usd),
                float(px),
                lim_mark,
                lq,
            )
            if int(rc) == 0:
                ok += 1
        except Exception as e:
            log(f"{log_prefix}: place {side} @ {px:g} failed ({type(e).__name__}: {e})")
    log(f"{log_prefix}: finished ({ok}/{len(want_post)} placed).")
    return ok


def _history_window_iso() -> Tuple[str, str]:
    days = float(os.environ.get(ENV_GRID_ORDERS_HISTORY_DAYS, "7") or "7")
    if days <= 0:
        days = 7.0
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    gte = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    lte = now.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return gte, lte


def fetch_orders_v2_history_pages(
    ep: VariEndpoints,
    *,
    instrument: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Paginate ``GET /api/orders/v2`` newest-first (``order_by=created_at&order=desc``)."""
    page_limit = int(os.environ.get(ENV_GRID_ORDERS_PAGE_LIMIT, "50") or "50")
    if page_limit < 1:
        page_limit = 50
    cap = int(os.environ.get(ENV_GRID_ORDERS_MAX_PAGES, "40") or "40")
    if max_pages is not None:
        cap = min(cap, int(max_pages))
    gte, lte = _history_window_iso()
    offset = 0
    all_rows: List[Dict[str, Any]] = []
    for _ in range(max(1, cap)):
        params: Dict[str, Any] = {
            "limit": str(page_limit),
            "offset": str(offset),
            "order_by": "created_at",
            "order": "desc",
            "created_at_gte": gte,
            "created_at_lte": lte,
        }
        if instrument:
            params["instrument"] = instrument
        raw = ep.get_orders_v2_query(params)
        rows = _orders_result_rows(raw)
        if not rows:
            break
        all_rows.extend(rows)
        pag = raw.get("pagination") if isinstance(raw, dict) else None
        np = (pag or {}).get("next_page") if isinstance(pag, dict) else None
        if not isinstance(np, dict):
            break
        try:
            offset = int(np.get("offset", offset + page_limit))
        except (TypeError, ValueError):
            break
        if len(rows) < page_limit:
            break
    return all_rows


def _summarize_history(rows: List[Dict[str, Any]], *, asset: str) -> Dict[str, Any]:
    want = str(asset).strip().upper()
    n_lim = n_pend = n_cleared = n_canceled = n_other = 0
    last: Dict[str, Optional[str]] = {"cleared_limit_buy": None, "cleared_limit_sell": None}
    for row in rows:
        if _row_underlying(row) != want:
            continue
        ot = str(row.get("order_type") or "").lower()
        st = str(row.get("status") or "").strip().lower()
        if "limit" in ot:
            n_lim += 1
        if st == "pending":
            n_pend += 1
        elif st == "cleared":
            n_cleared += 1
            if "limit" in ot:
                sd = str(row.get("side") or "").lower()
                if sd == "buy":
                    last["cleared_limit_buy"] = str(row.get("created_at"))
                elif sd == "sell":
                    last["cleared_limit_sell"] = str(row.get("created_at"))
        elif st == "canceled":
            n_canceled += 1
        else:
            n_other += 1
    return {
        "rows_in_window": len(rows),
        "limit_like_rows_for_asset": n_lim,
        "status_pending": n_pend,
        "status_cleared": n_cleared,
        "status_canceled": n_canceled,
        "status_other": n_other,
        "last_cleared_limit_buy_created_at": last["cleared_limit_buy"],
        "last_cleared_limit_sell_created_at": last["cleared_limit_sell"],
    }


def _position_qty_summary(positions_raw: Any, *, asset: str) -> Dict[str, Any]:
    want = str(asset).strip().upper()
    qty = 0.0
    found = False
    if isinstance(positions_raw, dict) and isinstance(positions_raw.get("positions"), list):
        plist = positions_raw["positions"]
    elif isinstance(positions_raw, list):
        plist = positions_raw
    else:
        plist = []
    for p in plist:
        if not isinstance(p, dict):
            continue
        sym = None
        inst = p.get("instrument")
        if isinstance(inst, dict):
            sym = inst.get("underlying")
        sym = str(sym or p.get("underlying") or "").strip().upper()
        if sym != want:
            continue
        for k in ("qty", "quantity", "position_qty", "net_qty", "size"):
            if k in p:
                try:
                    qty = float(p[k])
                    found = True
                except (TypeError, ValueError):
                    pass
                break
        pi = p.get("position_info")
        if not found and isinstance(pi, dict) and "qty" in pi:
            try:
                qty = float(pi["qty"])
                found = True
            except (TypeError, ValueError):
                pass
    return {"asset": want, "qty": qty, "has_position": found and abs(qty) > 1e-12}


def _template_limit_keys_from_meta(meta: Dict[str, Any]) -> Set[Tuple[str, str]]:
    """(side, rounded_limit_price) keys aligned with reconcile / pending matching."""
    out: Set[Tuple[str, str]] = set()
    for px in meta.get("grid_buy_rungs") or []:
        try:
            out.add(("buy", f"{round(float(px), 2):.2f}"))
        except (TypeError, ValueError):
            continue
    for px in meta.get("grid_sell_rungs") or []:
        try:
            out.add(("sell", f"{round(float(px), 2):.2f}"))
        except (TypeError, ValueError):
            continue
    return out


def _build_venue_snapshot(
    *,
    meta: Dict[str, Any],
    asset: str,
    pending_keys: Set[Tuple[str, str]],
    pos_s: Optional[Dict[str, Any]],
    history_summary: Optional[Dict[str, Any]],
    has_positions: bool,
    mark_f: Optional[float],
) -> Dict[str, Any]:
    template_keys = _template_limit_keys_from_meta(meta)
    matched = template_keys & pending_keys
    pending_orphans = pending_keys - template_keys
    template_missing = template_keys - pending_keys

    def key_obj(side: str, pxk: str) -> Dict[str, str]:
        return {"side": side, "limit_price": pxk}

    notes: List[str] = []
    if pending_orphans:
        notes.append(
            "Venue has pending limit order(s) not in this rung template (manual orders, older grid, or drift)."
        )
    if template_missing and matched:
        notes.append(
            "Some template rungs are missing from the venue pending book (filled, cancelled, or never placed)."
        )
    elif template_missing and not pending_keys and not has_positions:
        notes.append("No pending limits on venue for this asset; full template ladder may need seeding or refill.")
    elif template_keys and pending_keys == template_keys:
        notes.append("Pending book keys match template rung keys (within round(price,2)).")

    gs_fp: Any = None
    try:
        with open(_default_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            gs_fp = st.get("fingerprint")
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    snap: Dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "grid_asset": str(asset).strip().upper(),
        "live_limit_query": True,
        "grid_mark_at_check": mark_f,
        "has_positions": bool(has_positions),
        "gridstrat_state_fingerprint_present": isinstance(gs_fp, str) and bool(str(gs_fp).strip()),
        "template_limit_keys_count": len(template_keys),
        "venue_pending_limit_keys_count": len(pending_keys),
        "pending_matched_template_keys_count": len(matched),
        "pending_keys_not_in_template": [key_obj(a, b) for a, b in sorted(pending_orphans)],
        "template_keys_missing_on_venue": [key_obj(a, b) for a, b in sorted(template_missing)],
        "notes": notes,
    }
    if pos_s is not None:
        snap["position"] = pos_s
    if history_summary is not None:
        snap["history_summary"] = history_summary
    return snap


def run_grid_limits_bootstrap(
    *,
    ep: VariEndpoints,
    meta: Dict[str, Any],
    varibot_dir: str,
    cycle_index: int,
    has_positions: bool,
    log: Callable[[str], None],
    place_limit: Callable[..., int],
    live: bool,
    multi_script: str,
) -> None:
    """
    1) Query venue when live+limit: pending limits (every cycle); positions + paginated order history
       on first cycle(s) or when ``VARIBOT_GRID_LIMITS_MAP_EACH_CYCLE`` is set.
    2) Persist ``gridlimits.json`` from strategy meta **and** embed a ``venue_snapshot`` (template vs
       pending keys, optional history summary, gridstrat state hints) so restarts show leftover grid.
    3) Log a compact mental map when the heavy map path runs.
    4) If ``VARIBOT_GRID_LIMITS_RECONCILE`` and live and limit mode and gridstrat emitted
       0 events: POST missing template limits from ``gridlimits.json``.
    5) If ``VARIBOT_GRID_LIMITS_DRIFT_RECONCILE``: on template/venue key drift, cancel
       ``pending_keys_not_in_template`` then post ``template_keys_missing_on_venue`` (works with
       open positions when drift reconcile is enabled).
    """
    _ = multi_script
    if not meta.get("grid_mode"):
        return

    asset = str(meta.get("grid_asset") or os.environ.get("GRID_ASSET") or "BTC").strip().upper()
    is_limit = str(meta.get("grid_order_execution") or "").strip().lower() == "limit"
    n_ev = len(meta.get("grid_market_events") or [])
    each = _truthy_env(ENV_GRID_LIMITS_MAP_EACH_CYCLE)
    heavy_map = (cycle_index or 0) <= 1 or each

    mark = meta.get("grid_mark")
    try:
        mark_f = float(mark) if mark is not None else None
    except (TypeError, ValueError):
        mark_f = None

    pos_raw: Any = None
    pos_s: Optional[Dict[str, Any]] = None
    pending_keys: Set[Tuple[str, str]] = set()
    hist: List[Dict[str, Any]] = []
    hsum: Optional[Dict[str, Any]] = None

    if heavy_map:
        try:
            pos_raw = ep.get_positions()
        except Exception as e:
            log(f"gridlimits map: GET /api/positions failed ({type(e).__name__}: {e})")
            pos_raw = None

        pos_s = _position_qty_summary(pos_raw or {}, asset=asset)

        try:
            pending_keys = _fetch_pending_limit_keys(ep, asset=asset)
        except Exception as e:
            log(f"gridlimits map: pending fetch failed ({type(e).__name__}: {e})")
            pending_keys = set()

        inst = _instrument_param(asset)
        try:
            hist = fetch_orders_v2_history_pages(ep, instrument=inst)
        except Exception as e:
            log(f"gridlimits map: history fetch failed ({type(e).__name__}: {e})")
            hist = []

        hsum = _summarize_history(hist, asset=asset)
    elif live and is_limit:
        try:
            pending_keys = _fetch_pending_limit_keys(ep, asset=asset)
        except Exception as e:
            log(f"gridlimits map: pending fetch failed ({type(e).__name__}: {e})")
            pending_keys = set()

    venue_snapshot: Optional[Dict[str, Any]] = None
    if live and is_limit:
        venue_snapshot = _build_venue_snapshot(
            meta=meta,
            asset=asset,
            pending_keys=pending_keys,
            pos_s=pos_s,
            history_summary=hsum,
            has_positions=has_positions,
            mark_f=mark_f,
        )

    path = sync_gridlimits_json(meta=meta, varibot_dir=varibot_dir, venue_snapshot=venue_snapshot)
    if path:
        log(f"gridlimits: synced template + venue snapshot → {path}")

    n_lims = len(meta.get("grid_buy_rungs") or []) + len(meta.get("grid_sell_rungs") or [])
    if heavy_map:
        mental = {
            "grid_asset": asset,
            "grid_mark": mark_f,
            "has_positions": bool(has_positions),
            "position": pos_s,
            "pending_limit_keys_count": len(pending_keys),
            "history_window_days": float(os.environ.get(ENV_GRID_ORDERS_HISTORY_DAYS, "7") or "7"),
            "history_summary": hsum,
            "gridlimits_template_rows": n_lims,
            "gridstrat_events_this_cycle": n_ev,
        }
        log(f"gridlimits mental_map: {json.dumps(mental, default=str)}")

    reconcile_on = _truthy_env(ENV_GRID_LIMITS_RECONCILE)
    drift_on = _drift_reconcile_enabled(meta)
    if not reconcile_on and not drift_on:
        if int(cycle_index or 0) <= 1:
            log(
                "gridlimits reconcile: skipped (set VARIBOT_GRID_LIMITS_RECONCILE=1 and/or "
                "VARIBOT_GRID_LIMITS_DRIFT_RECONCILE=1 for live limit sync)"
            )
        return
    if not live or not is_limit:
        return

    if has_positions and not _reconcile_with_positions_allowed(meta):
        log(
            "gridlimits reconcile: skip (open positions); paired_limit auto-sync needs "
            "VARIBOT_GRID_LIMITS_RECONCILE=1 (or VARIBOT_GRID_LIMITS_RECONCILE_WITH_POSITIONS=1)."
        )
        return

    if mark_f is None:
        log("gridlimits reconcile: skip (no grid_mark).")
        return

    gl = _load_gridlimits(_grid_limits_json_path(varibot_dir)) or {}
    lims = gl.get("limits") if isinstance(gl.get("limits"), list) else []
    template_keys = _template_limit_keys_from_meta(meta)
    pending_orphans = pending_keys - template_keys
    template_missing = template_keys - pending_keys
    has_drift = bool(pending_orphans or template_missing)

    reset_n = meta.get("grid_reset_count")
    paired_drift = bool(meta.get("grid_paired_limit_mode"))
    if drift_on and has_drift:
        log(
            f"gridlimits re-arm: template/venue mismatch "
            f"(orphans={len(pending_orphans)} missing={len(template_missing)}"
            f"{f' grid_reset_count={reset_n}' if reset_n is not None else ''})"
        )
        if _drift_cancel_enabled(meta) and pending_orphans:
            n_cancel = _cancel_pending_orphans(
                ep, asset=asset, orphan_keys=pending_orphans, log=log
            )
            log(f"gridlimits drift: canceled {n_cancel}/{len(pending_orphans)} orphan limit(s).")
            try:
                pending_keys = _fetch_pending_limit_keys(ep, asset=asset)
            except Exception as e:
                log(f"gridlimits drift: pending refresh failed ({type(e).__name__}: {e})")
        if _drift_refill_enabled(meta):
            want_post = _collect_want_post_limits(
                lims=lims,
                pending_keys=pending_keys,
                mark_f=mark_f,
                apply_mark_filter=not paired_drift,
            )
            if want_post:
                _post_missing_limits(
                    meta=meta,
                    asset=asset,
                    want_post=want_post,
                    place_limit=place_limit,
                    log=log,
                    log_prefix="gridlimits re-arm",
                )
            elif template_missing:
                log(
                    "gridlimits re-arm: template rungs still missing on venue "
                    "(nothing to post after filters)."
                )
        return

    if not reconcile_on:
        return
    if n_ev > 0:
        log("gridlimits reconcile: skip refill (gridstrat has events this cycle).")
        return

    want_post = _collect_want_post_limits(lims=lims, pending_keys=pending_keys, mark_f=mark_f)
    if not want_post:
        if has_drift and meta.get("grid_paired_limit_mode"):
            log(
                "gridlimits reconcile: re-arm drift detected; enable VARIBOT_GRID_LIMITS_RECONCILE=1 "
                "to post missing sim rungs on venue."
            )
        elif has_drift:
            log(
                "gridlimits reconcile: drift detected; set VARIBOT_GRID_LIMITS_DRIFT_RECONCILE=1 "
                "to cancel orphans and refill."
            )
        else:
            log("gridlimits reconcile: no missing buy/sell limits to refill (vs template + mark).")
        return

    _post_missing_limits(
        meta=meta,
        asset=asset,
        want_post=want_post,
        place_limit=place_limit,
        log=log,
        log_prefix="gridlimits reconcile",
    )
