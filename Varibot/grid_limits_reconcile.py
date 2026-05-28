from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from strategy.gridstrat import ROOT_STATE_SCHEMA_VERSION, _default_state_path, iter_grid_asset_metas
from strategy.gridstrat_rearm import (
    _pending_keys_from_state,
    apply_venue_cleared_limits_as_fills,
)
from strategy.gridstrat_remnant import (
    RemnantInferenceResult,
    compute_venue_actions,
    infer_ladder_from_remnants,
)
from strategy.gridstrat_state import load_state, save_state

from variationalbot.vari.endpoints import VariEndpoints, limit_price_key

# Live limit sync via remnant inference (``strategy/gridstrat_remnant``). Default ON.
# Set VARIBOT_GRID_LIMITS_RECONCILE=0 to disable.
ENV_GRID_LIMITS_RECONCILE: str = "VARIBOT_GRID_LIMITS_RECONCILE"
GRID_LIMITS_RECONCILE_DEFAULT: bool = True
# Legacy alias: drift reconcile env still enables remnant re-arm when unset.
ENV_GRID_LIMITS_DRIFT_RECONCILE: str = "VARIBOT_GRID_LIMITS_DRIFT_RECONCILE"
# Sub-gates: cancel defaults off (refill-only between cycles); set to 1 to cancel stray venue limits.
ENV_GRID_LIMITS_DRIFT_CANCEL: str = "VARIBOT_GRID_LIMITS_DRIFT_CANCEL"
ENV_GRID_LIMITS_RECONCILE_WITH_POSITIONS: str = "VARIBOT_GRID_LIMITS_RECONCILE_WITH_POSITIONS"
ENV_GRID_LIMITS_CANCEL_SLEEP_S: str = "VARIBOT_GRID_LIMITS_CANCEL_SLEEP_S"  # legacy; see pending_limit_cancel
# Set to 1 to re-fetch positions + pending every cycle (default: cycle 1 only).
ENV_GRID_LIMITS_MAP_EACH_CYCLE: str = "VARIBOT_GRID_LIMITS_MAP_EACH_CYCLE"
ENV_GRID_ORDERS_PAGE_LIMIT: str = "VARIBOT_GRID_ORDERS_PAGE_LIMIT"
# One paginated GET /api/orders/v2?status=pending for all tickers (default on). Set 0 for per-ticker fetch.
ENV_PENDING_BULK: str = "VARIBOT_PENDING_BULK"
ENV_PENDING_BULK_MAX_PAGES: str = "VARIBOT_PENDING_BULK_MAX_PAGES"


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def grid_limits_reconcile_enabled() -> bool:
    return _env_bool_default(
        ENV_GRID_LIMITS_RECONCILE, default_when_unset=GRID_LIMITS_RECONCILE_DEFAULT
    )


def _env_bool_default(name: str, *, default_when_unset: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default_when_unset)
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def _drift_cancel_enabled(meta: Optional[Dict[str, Any]] = None) -> bool:
    """Default on: cancel depth orphans via pending_limit_cancel (418-safe pacing). Set env 0 to disable."""
    _ = meta
    return _env_bool_default(ENV_GRID_LIMITS_DRIFT_CANCEL, default_when_unset=True)


def _reconcile_with_positions_allowed(meta: Optional[Dict[str, Any]] = None) -> bool:
    if _truthy_env(ENV_GRID_LIMITS_RECONCILE_WITH_POSITIONS):
        return True
    if grid_limits_reconcile_enabled() and meta and meta.get("grid_paired_limit_mode"):
        return True
    return False


def fetch_pending_limit_keys_for_asset(ep: VariEndpoints, *, asset: str) -> Set[Tuple[str, str]]:
    """Public helper for varibot: venue (side, price) keys for pending limits."""
    return _fetch_pending_limit_keys(ep, asset=asset)


def bulk_pending_fetch_enabled() -> bool:
    """Default on: one paginated pending sweep per cycle instead of per-ticker GETs."""
    return _env_bool_default(ENV_PENDING_BULK, default_when_unset=True)


def pending_limit_keys_by_asset_from_rows(
    rows: List[Dict[str, Any]],
    assets: Iterable[str],
) -> Dict[str, Set[Tuple[str, str]]]:
    """Bucket paginated pending rows into per-underlying (side, price) key sets."""
    want = {str(a).strip().upper() for a in assets if str(a).strip()}
    out: Dict[str, Set[Tuple[str, str]]] = {sym: set() for sym in want}
    for row in rows:
        if not isinstance(row, dict):
            continue
        u = _row_underlying(row)
        if u not in want:
            continue
        k = _limit_price_key(row)
        if k:
            out[u].add(k)
    return out


def fetch_pending_limit_keys_by_assets(
    ep: VariEndpoints,
    *,
    assets: Iterable[str],
) -> Dict[str, Set[Tuple[str, str]]]:
    """
    All grid tickers' pending limit keys from one paginated ``GET /api/orders/v2?status=pending``
    (no per-ticker instrument filter; RWA-safe).
    """
    rows = _fetch_pending_limit_rows_paginated(ep)
    return pending_limit_keys_by_asset_from_rows(rows, assets)


def _instrument_param(asset: str) -> Optional[str]:
    from variationalbot.vari.endpoints import instrument_query_param

    return instrument_query_param(asset)


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
    """(buy|sell, normalized_price) from a venue pending order row."""
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
        lp = row.get("trigger_price")
    if lp is None:
        return None
    try:
        return limit_price_key(side, float(lp))
    except (TypeError, ValueError):
        return None


def _row_rfq_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("rfq_id", "rfqId"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _pending_bulk_max_pages() -> int:
    try:
        return max(1, min(20, int(os.environ.get(ENV_PENDING_BULK_MAX_PAGES, "6") or "6")))
    except (TypeError, ValueError):
        return 6


def fetch_pending_order_rows_paginated(
    ep: VariEndpoints,
    *,
    instrument: Optional[str] = None,
    page_limit: Optional[int] = None,
    max_pages: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Paginated ``GET /api/orders/v2?status=pending`` (optional ``instrument`` filter).

    Returns ``(rows, hit_page_cap)``. ``hit_page_cap`` is True when ``max_pages`` was exhausted
    but the API may still have more pages (caller should warn or raise page limit).
    """
    pl = page_limit
    if pl is None:
        try:
            pl = int(os.environ.get(ENV_GRID_ORDERS_PAGE_LIMIT, "50") or "50")
        except (TypeError, ValueError):
            pl = 50
    if pl < 1:
        pl = 50
    cap = max_pages if max_pages is not None else _pending_bulk_max_pages()
    cap = max(1, int(cap))
    offset = 0
    rows_all: List[Dict[str, Any]] = []
    hit_cap = False
    for _ in range(cap):
        params: Dict[str, Any] = {
            "status": "pending",
            "limit": str(pl),
            "offset": str(offset),
            "order_by": "created_at",
            "order": "desc",
        }
        if instrument:
            params["instrument"] = str(instrument).strip()
        raw = ep.get_orders_v2_query(params)
        rows = _orders_result_rows(raw)
        if not rows:
            break
        rows_all.extend(rows)
        pag = raw.get("pagination") if isinstance(raw, dict) else None
        np = (pag or {}).get("next_page") if isinstance(pag, dict) else None
        if not isinstance(np, dict):
            break
        try:
            offset = int(np.get("offset", offset + pl))
        except (TypeError, ValueError):
            break
        if len(rows) < pl:
            break
    else:
        hit_cap = bool(rows_all)
    return rows_all, hit_cap


def _fetch_pending_limit_rows_paginated(ep: VariEndpoints) -> List[Dict[str, Any]]:
    """
    Paginated global pending book (no ``instrument`` filter).

    Required for RWAs (instrument query 400) and for bulk fetch across all grid tickers.
    """
    rows, _ = fetch_pending_order_rows_paginated(ep)
    return rows


def _fetch_pending_limit_rows(ep: VariEndpoints, *, asset: str) -> List[Dict[str, Any]]:
    from variationalbot.vari.endpoints import fetch_orders_v2_pending

    inst = _instrument_param(asset)
    # Crypto perps: use the instrument filter (fast, small result set).
    # RWA commodities: instrument filter 400 — paginate globally and filter client-side.
    if inst:
        raw = fetch_orders_v2_pending(ep.client, instrument=inst, status="pending")
        rows_all = _orders_result_rows(raw)
    else:
        rows_all = _fetch_pending_limit_rows_paginated(ep)
    want = str(asset).strip().upper()
    out: List[Dict[str, Any]] = []
    for row in rows_all:
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


def _cancel_pending_orphans(
    ep: VariEndpoints,
    *,
    asset: str,
    orphan_keys: Set[Tuple[str, str]],
    log: Callable[[str], None],
) -> int:
    """Cancel pending limits whose (side, price) keys exceed keep-depth (418-safe pacing)."""
    if not orphan_keys:
        return 0
    from pending_limit_cancel import cancel_limit_rows

    rows = _fetch_pending_limit_rows(ep, asset=asset)
    targets: List[Dict[str, Any]] = []
    for row in rows:
        k = _limit_price_key(row)
        if k is None or k not in orphan_keys:
            continue
        if not _row_rfq_id(row):
            log(f"gridlimits drift: skip cancel {k[0]} @ {k[1]} (no rfq_id)")
            continue
        targets.append(row)
    if not targets:
        return 0

    def _drift_log(msg: str) -> None:
        log(f"gridlimits drift: {msg}")

    ok, err_n = cancel_limit_rows(ep, targets, log=_drift_log)
    if err_n:
        log(f"gridlimits drift: {err_n}/{len(targets)} cancel(s) failed for {asset}")
    return ok


def _post_missing_limits(
    *,
    meta: Dict[str, Any],
    asset: str,
    want_post: List[Tuple[str, float, float, Optional[str]]],
    place_limit: Callable[..., int],
    log: Callable[[str], None],
    log_prefix: str = "gridlimits reconcile",
) -> List[Tuple[str, str]]:
    """POST missing limits; return (side, price_key) tuples successfully placed."""
    if not want_post:
        return []
    lim_mark = bool(meta.get("grid_limit_use_mark_price"))
    log(f"{log_prefix}: posting {len(want_post)} missing limit(s) …")
    placed: List[Tuple[str, str]] = []
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
                placed.append(limit_price_key(side, float(px)))
        except Exception as e:
            log(f"{log_prefix}: place {side} @ {px:g} failed ({type(e).__name__}: {e})")
    log(f"{log_prefix}: finished ({len(placed)}/{len(want_post)} placed).")
    return placed


def _append_last_venue_pending_keys(asset: str, keys: Set[Tuple[str, str]]) -> None:
    """Merge keys into gridstrat ``last_venue_pending_keys`` (drift posts run after the tick snapshot)."""
    if not keys:
        return
    path = _default_state_path()
    root = load_state(path)
    want = str(asset).strip().upper()
    asset_st: Optional[Dict[str, Any]] = None
    if int(root.get("schema_version") or 0) == ROOT_STATE_SCHEMA_VERSION and isinstance(
        root.get("assets"), dict
    ):
        raw = root["assets"].get(want)
        asset_st = raw if isinstance(raw, dict) else None
    else:
        legacy = (os.environ.get("GRID_ASSET") or "BTC").strip().upper()
        if want == legacy or not root.get("assets"):
            asset_st = root if isinstance(root, dict) else None
    if not asset_st:
        return
    merged = _pending_keys_from_state(asset_st) | set(keys)
    asset_st["last_venue_pending_keys"] = [
        [str(side).lower(), str(pxk)] for side, pxk in sorted(merged)
    ]
    if int(root.get("schema_version") or 0) == ROOT_STATE_SCHEMA_VERSION and isinstance(
        root.get("assets"), dict
    ):
        root["assets"][want] = asset_st
    else:
        root = asset_st
    save_state(path, root)


def _save_gridstrat_asset_state(asset: str, asset_st: Dict[str, Any]) -> None:
    path = _default_state_path()
    root = load_state(path)
    want = str(asset).strip().upper()
    if int(root.get("schema_version") or 0) == ROOT_STATE_SCHEMA_VERSION and isinstance(
        root.get("assets"), dict
    ):
        root["assets"][want] = asset_st
        save_state(path, root)
        return
    legacy = (os.environ.get("GRID_ASSET") or "BTC").strip().upper()
    if want == legacy or not root.get("assets"):
        save_state(path, asset_st)


def _sync_sim_fills_after_drift_post(
    ep: VariEndpoints,
    *,
    asset: str,
    posted_keys: List[Tuple[str, str]],
    log: Callable[[str], None],
    tag: str,
) -> None:
    """
    Drift limits are posted after ``pick_tickers`` snapshots venue pending. Record posted keys
    and apply venue-cleared fill sync so immediate fills re-arm in sim instead of reposting.
    """
    if not posted_keys:
        return
    _append_last_venue_pending_keys(asset, set(posted_keys))
    asset_st = _load_gridstrat_asset_state(asset)
    if not asset_st:
        return
    try:
        pending_keys = _fetch_pending_limit_keys(ep, asset=asset)
    except Exception as e:
        log(f"gridlimits{tag} drift fill-sync: pending refresh failed ({type(e).__name__}: {e})")
        return
    logs = apply_venue_cleared_limits_as_fills(asset_st, pending_keys=pending_keys)
    if not logs:
        return
    for ln in logs:
        log(f"gridlimits{tag} drift fill-sync: {ln}")
    _save_gridstrat_asset_state(asset, asset_st)


def _bootstrap_pending_map(
    ep: VariEndpoints,
    *,
    asset_metas: List[Tuple[str, Dict[str, Any]]],
    preloaded: Optional[Dict[str, Set[Tuple[str, str]]]],
    needs_pending: bool,
    log: Callable[[str], None],
) -> Optional[Dict[str, Set[Tuple[str, str]]]]:
    """
    One bulk ``GET /api/orders/v2?status=pending`` for all grid tickers when possible.

    Returns the preloaded dict unchanged, a bulk-fetched dict, or None (per-ticker fallback).
    """
    if preloaded is not None:
        return preloaded
    if not needs_pending:
        return {}
    syms = [str(a).strip().upper() for a, _ in asset_metas if str(a).strip()]
    if not syms:
        return {}
    if not bulk_pending_fetch_enabled():
        return None
    try:
        by_asset = fetch_pending_limit_keys_by_assets(ep, assets=syms)
        n_limits = sum(len(v) for v in by_asset.values())
        log(
            f"gridlimits map: pending bulk fetch OK — {n_limits} limit(s) across "
            f"{len(by_asset)} ticker(s)"
        )
        return by_asset
    except Exception as e:
        log(
            f"gridlimits map: pending bulk fetch failed ({type(e).__name__}: {e}); "
            "per-ticker fallback"
        )
        return None


def _pending_keys_for_asset_map(
    ep: VariEndpoints,
    *,
    asset: str,
    pending_map: Optional[Dict[str, Set[Tuple[str, str]]]],
    log: Callable[[str], None],
) -> Set[Tuple[str, str]]:
    if pending_map is not None:
        return set(pending_map.get(asset, set()))
    try:
        return _fetch_pending_limit_keys(ep, asset=asset)
    except Exception as e:
        log(f"gridlimits[{asset}] map: pending fetch failed ({type(e).__name__}: {e})")
        return set()


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


def _load_gridstrat_asset_state(asset: str) -> Optional[Dict[str, Any]]:
    """Per-asset slice from ``gridstrat_state.json`` (multi-asset v4 or legacy single-asset)."""
    want = str(asset).strip().upper()
    try:
        with open(_default_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(st, dict):
        return None
    if int(st.get("schema_version") or 0) == ROOT_STATE_SCHEMA_VERSION and isinstance(
        st.get("assets"), dict
    ):
        asset_st = st["assets"].get(want)
        return asset_st if isinstance(asset_st, dict) else None
    legacy = (os.environ.get("GRID_ASSET") or "BTC").strip().upper()
    if want == legacy or not st.get("assets"):
        return st
    return None


def _remnant_rearm_one_ticker(
    *,
    ep: VariEndpoints,
    asset: str,
    ameta: Dict[str, Any],
    combined_meta: Dict[str, Any],
    pending_keys: Set[Tuple[str, str]],
    mark_f: float,
    log: Callable[[str], None],
    place_limit: Callable[..., int],
) -> None:
    """
    Remnant-based re-arm (New Limits Logic, May 2026).

    1. Infer spacing/anchor from venue remnants + configured band.
    2. If insufficient → trigger hard reset in sim state (cancel all, re-seed from current mark).
    3. Otherwise compute protected window (N nearest per side), cancel outside-window orphans,
       post missing window rungs nearest-first.
    """
    tag = f"[{asset}]"
    is_init = not pending_keys

    # Pull configured geometry from per-asset meta
    configured_spacing = float(ameta.get("grid_spacing") or 0.0)
    lower = float(ameta.get("grid_lower") or 0.0)
    upper = float(ameta.get("grid_upper") or 0.0)
    grid_num = int(ameta.get("grid_num") or 10)

    if configured_spacing <= 0 or upper <= lower:
        log(f"gridlimits{tag} remnant: skip — no valid spacing/bounds in meta.")
        return

    band_pct_meta = ameta.get("grid_band_pct")
    try:
        grid_band_pct = float(band_pct_meta) if band_pct_meta is not None else None
    except (TypeError, ValueError):
        grid_band_pct = None

    result: RemnantInferenceResult = infer_ladder_from_remnants(
        mark=mark_f,
        venue_pending_keys=pending_keys,
        configured_spacing=configured_spacing,
        lower=lower,
        upper=upper,
        grid_num=grid_num,
        grid_band_pct=grid_band_pct,
    )

    if is_init:
        log(f"gridlimits{tag} init: mark={mark_f:g}, venue empty — seeding grid")
    else:
        log(
            f"gridlimits{tag} remnant: {result} "
            f"(venue pending={len(pending_keys)}, mark={mark_f:g})"
        )

    # Decide actions every cycle (proximity hug only when a side is short on in-band depth).
    cancel_keys, post_rungs = compute_venue_actions(
        asset=asset,
        result=result,
        venue_pending_keys=pending_keys,
        mark=mark_f,
    )
    if cancel_keys and _drift_cancel_enabled(combined_meta):
        n_canceled = _cancel_pending_orphans(ep, asset=asset, orphan_keys=cancel_keys, log=log)
        log(
            f"gridlimits{tag} remnant: canceled {n_canceled}/{len(cancel_keys)} "
            "out-of-band orphan(s)."
        )
        try:
            pending_keys = _fetch_pending_limit_keys(ep, asset=asset)
        except Exception as e:
            log(f"gridlimits{tag} remnant: pending refresh failed ({type(e).__name__}: {e})")
        _, post_rungs = compute_venue_actions(
            asset=asset,
            result=result,
            venue_pending_keys=pending_keys,
            mark=mark_f,
        )

    if post_rungs:
        n_b = sum(1 for s, _ in post_rungs if s == "buy")
        n_s = len(post_rungs) - n_b
        if is_init:
            log(
                f"gridlimits{tag} init: posting {len(post_rungs)} limits "
                f"({n_b} buy, {n_s} sell)"
            )
        else:
            log(
                f"gridlimits{tag} remnant: posting {len(post_rungs)} missing window rung(s) "
                f"(nearest-first: buys={n_b} sells={sum(1 for s,_ in post_rungs if s=='sell')})"
            )
        _post_remnant_rungs(
            ep=ep,
            asset=asset,
            ameta=ameta,
            post_rungs=post_rungs,
            place_limit=place_limit,
            log=log,
            tag=tag,
            log_prefix=f"gridlimits{tag} {'init' if is_init else 'remnant'}",
        )
    else:
        log(f"gridlimits{tag} remnant: window complete — no rungs to post.")
    return

    # (result.sufficient returns above)


def _post_remnant_rungs(
    *,
    ep: VariEndpoints,
    asset: str,
    ameta: Dict[str, Any],
    post_rungs: List[Tuple[str, float]],
    place_limit: Callable[..., int],
    log: Callable[[str], None],
    tag: str,
    log_prefix: Optional[str] = None,
) -> None:
    """POST rungs from remnant re-arm (side, price) list and sync sim fills."""
    if not post_rungs:
        return
    prefix = log_prefix or f"gridlimits{tag} remnant"
    per_usd = float(ameta.get("grid_per_rung_usd") or 0.0)
    qty_str: Optional[str] = str(ameta.get("grid_per_rung_qty") or "").strip() or None
    if per_usd <= 0 and not qty_str:
        log(f"{prefix}: skip post — no rung sizing in meta.")
        return

    want_post: List[Tuple[str, float, float, Optional[str]]] = [
        (side, px, per_usd, qty_str) for side, px in post_rungs
    ]
    placed = _post_missing_limits(
        meta=ameta,
        asset=asset,
        want_post=want_post,
        place_limit=place_limit,
        log=log,
        log_prefix=prefix,
    )
    _sync_sim_fills_after_drift_post(ep, asset=asset, posted_keys=placed, log=log, tag=tag)


def _reconcile_one_ticker(
    *,
    ep: VariEndpoints,
    asset: str,
    ameta: Dict[str, Any],
    combined_meta: Dict[str, Any],
    pending_keys: Set[Tuple[str, str]],
    mark_f: float,
    has_positions: bool,
    reconcile_on: bool,
    log: Callable[[str], None],
    place_limit: Callable[..., int],
) -> None:
    """Paired limit grid: remnant inference every cycle (no ``gridlimits.json`` template)."""
    tag = f"[{asset}]"

    if has_positions and not _reconcile_with_positions_allowed(combined_meta):
        log(
            f"gridlimits{tag} reconcile: skip (open positions); set "
            "VARIBOT_GRID_LIMITS_RECONCILE_WITH_POSITIONS=1 to sync limits while positioned."
        )
        return

    if not reconcile_on:
        return

    if not ameta.get("grid_paired_limit_mode"):
        log(f"gridlimits{tag} reconcile: skip — not paired_limit mode.")
        return

    _remnant_rearm_one_ticker(
        ep=ep,
        asset=asset,
        ameta=ameta,
        combined_meta=combined_meta,
        pending_keys=pending_keys,
        mark_f=mark_f,
        log=log,
        place_limit=place_limit,
    )


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
    pending_by_asset_preloaded: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
) -> None:
    """
    Fetch venue pending limits (bulk when enabled), then per ticker run remnant-based re-arm
    (``strategy/gridstrat_remnant``) when ``VARIBOT_GRID_LIMITS_RECONCILE`` is on and live limit mode.
    """
    _ = multi_script
    _ = varibot_dir
    if not meta.get("grid_mode"):
        return

    asset_metas = iter_grid_asset_metas(meta)
    if not asset_metas:
        return

    is_limit = str(meta.get("grid_order_execution") or "").strip().lower() == "limit"
    each = _truthy_env(ENV_GRID_LIMITS_MAP_EACH_CYCLE)
    heavy_map = (cycle_index or 0) <= 1 or each

    pos_raw: Any = None
    if heavy_map:
        try:
            pos_raw = ep.get_positions()
        except Exception as e:
            log(f"gridlimits map: GET /api/positions failed ({type(e).__name__}: {e})")
            pos_raw = None

    pending_by_asset: Dict[str, Set[Tuple[str, str]]] = {}
    needs_pending_map = heavy_map or (live and is_limit)
    pending_map = _bootstrap_pending_map(
        ep,
        asset_metas=asset_metas,
        preloaded=pending_by_asset_preloaded,
        needs_pending=needs_pending_map,
        log=log,
    )

    for asset, ameta in asset_metas:
        mark = ameta.get("grid_mark")
        try:
            mark_f = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            mark_f = None

        pos_s: Optional[Dict[str, Any]] = None
        pending_keys: Set[Tuple[str, str]] = set()

        if heavy_map:
            pos_s = _position_qty_summary(pos_raw or {}, asset=asset)
            if needs_pending_map:
                pending_keys = _pending_keys_for_asset_map(
                    ep, asset=asset, pending_map=pending_map, log=log
                )
        elif live and is_limit:
            pending_keys = _pending_keys_for_asset_map(
                ep, asset=asset, pending_map=pending_map, log=log
            )

        pending_by_asset[asset] = pending_keys
        if not has_positions:
            has_pos_asset = False
        elif pos_s is not None:
            has_pos_asset = bool(pos_s.get("has_position"))
        else:
            has_pos_asset = True

    reconcile_on = grid_limits_reconcile_enabled()
    if not reconcile_on:
        if int(cycle_index or 0) <= 1:
            log(
                "gridlimits reconcile: skipped (VARIBOT_GRID_LIMITS_RECONCILE=0; "
                "unset env for default-on live limit sync)"
            )
        return
    if not live or not is_limit:
        return

    for asset, ameta in asset_metas:
        mark = ameta.get("grid_mark")
        try:
            mark_f = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            mark_f = None
        if mark_f is None:
            log(f"gridlimits[{asset}] reconcile: skip (no grid_mark).")
            continue

        pos_s = _position_qty_summary(pos_raw or {}, asset=asset) if pos_raw is not None else None
        if not has_positions:
            has_pos_asset = False
        elif pos_s is not None:
            has_pos_asset = bool(pos_s.get("has_position"))
        else:
            has_pos_asset = True
        pending_keys = pending_by_asset.get(asset, set())

        _reconcile_one_ticker(
            ep=ep,
            asset=asset,
            ameta=ameta,
            combined_meta=meta,
            pending_keys=pending_keys,
            mark_f=mark_f,
            has_positions=has_pos_asset,
            reconcile_on=reconcile_on,
            log=log,
            place_limit=place_limit,
        )
