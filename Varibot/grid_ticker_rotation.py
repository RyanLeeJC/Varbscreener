"""
Daily grid ticker rotation — evaluate (Cron Job) and apply (varibot cycle).

Evaluate: top-N volume universe → 48h band hyperparam → pending swap plan in RotationStore.
Apply: cancel limits + flatten removed tickers → update roster JSON → reconcile orphans.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

_VARIBOT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _VARIBOT_DIR.parent
for p in (str(_REPO_ROOT), str(_VARIBOT_DIR), str(_REPO_ROOT / "binancefetch" / "gridbot_study")):
    if p not in sys.path:
        sys.path.insert(0, p)

from strategy.gridstrat import (  # noqa: E402
    _default_roster_path,
    grid_trading_ticker_band_pcts,
    grid_trading_ticker_band_pcts_from_static,
    is_rwa_ticker,
    load_grid_trading_roster,
    save_grid_trading_roster,
    seed_grid_trading_roster_if_missing,
)
from strategy.gridstrat_state import load_state, save_state  # noqa: E402
from strategy.gridstrat import _default_state_path  # noqa: E402

ENV_ENABLED = "GRID_TICKER_ROTATION_ENABLED"
ENV_ROSTER_SIZE = "GRID_ROSTER_SIZE"
ENV_CANDIDATE_POOL = "GRID_ROTATION_CANDIDATE_POOL"
ENV_SWAP_COUNT = "GRID_ROTATION_SWAP_COUNT"
ENV_STORE = "GRID_ROTATION_STORE"
ENV_PENDING_PATH = "GRID_ROTATION_PENDING_PATH"
ENV_BLACKLIST_JSON = "GRID_ROTATION_BLACKLIST_JSON"
ENV_RUN_DIR = "GRID_ROTATION_RUN_DIR"
ENV_KEY_VALUE_URL = "KEY_VALUE_URL"
ENV_KEY_VALUE_KEY = "GRID_ROTATION_KEYVALUE_KEY"
ENV_DATABASE_URL = "DATABASE_URL"

DEFAULT_ENABLED = False
DEFAULT_ROSTER_SIZE = 20
DEFAULT_CANDIDATE_POOL = 40
DEFAULT_SWAP_COUNT = 10
DEFAULT_STORE = "file"
DEFAULT_PENDING_NAME = ".grid_rotation_pending.json"
DEFAULT_KEYVALUE_KEY = "grid:rotation:pending"
PENDING_SCHEMA = 1
GATE_EXCLUDE = {"BTC", "ETH"}


def _progress(msg: str) -> None:
    """Stdout progress for Cron / Render logs (flush so lines appear immediately)."""
    print(f"grid_rotation: {msg}", flush=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def rotation_enabled() -> bool:
    return _env_bool(ENV_ENABLED, DEFAULT_ENABLED)


def roster_size() -> int:
    return max(1, _env_int(ENV_ROSTER_SIZE, DEFAULT_ROSTER_SIZE))


def candidate_pool_size() -> int:
    return max(1, _env_int(ENV_CANDIDATE_POOL, DEFAULT_CANDIDATE_POOL))


def swap_count() -> int:
    return max(0, _env_int(ENV_SWAP_COUNT, DEFAULT_SWAP_COUNT))


def default_pending_path() -> str:
    raw = (os.environ.get(ENV_PENDING_PATH) or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return str(_VARIBOT_DIR / DEFAULT_PENDING_NAME)


def default_blacklist_json_path() -> Path:
    raw = (os.environ.get(ENV_BLACKLIST_JSON) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _REPO_ROOT / "Vari Listings" / "vari_crypto_categories.json"


def load_blacklist(path: Optional[Path] = None) -> Set[str]:
    p = path or default_blacklist_json_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    items = doc.get("vari-blacklist") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return set()
    out: Set[str] = set()
    for row in items:
        if isinstance(row, dict):
            sym = str(row.get("asset") or "").strip().upper()
            if sym:
                out.add(sym)
    return out


def _volume_from_asset_row(row: Dict[str, Any]) -> float:
    for key in ("volume_24h", "vol_24h", "quote_volume_24h", "volume24h"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return 0.0


def _is_crypto_perp_row(row: Dict[str, Any]) -> bool:
    it = str(row.get("instrument_type") or row.get("type") or "").strip().lower()
    if it in ("perpetual_rwa_future", "perpetual_rwa", "rwa"):
        return False
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("commodity", "equity", "tradfi", "rwa"):
        return False
    return True


def fetch_top_volume_universe(
    ep: Any,
    *,
    top_n: int,
    blacklist: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    """Return [(ticker, volume_24h_usd), ...] sorted desc."""
    bl = blacklist if blacklist is not None else load_blacklist()
    raw = ep.get_supported_assets()
    if not isinstance(raw, dict):
        return []

    scored: List[Tuple[str, float]] = []
    for asset_key, rows in raw.items():
        sym = str(asset_key).strip().upper()
        if not sym or sym in GATE_EXCLUDE or sym in bl or is_rwa_ticker(sym):
            continue
        if not isinstance(rows, list) or not rows:
            continue
        row = rows[0] if isinstance(rows[0], dict) else {}
        if not _is_crypto_perp_row(row):
            continue
        vol = _volume_from_asset_row(row)
        if vol <= 0:
            continue
        scored.append((sym, vol))

    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[: int(top_n)]


def current_roster_tickers() -> Dict[str, float]:
    seed_grid_trading_roster_if_missing()
    doc = load_grid_trading_roster()
    if doc and isinstance(doc.get("tickers"), dict):
        return dict(doc["tickers"])
    return grid_trading_ticker_band_pcts_from_static()


def fetch_live_snapshot_pnl(
    ep: Any,
    *,
    tickers: Sequence[str],
    hours: float = 24.0,
) -> Dict[str, float]:
    from gridbotsnapshot import aggregate_rpnl, build_snapshot_rows, fetch_export_csv, fetch_upnl_by_ticker
    from variationalbot.vari import VariClient

    client = ep.client if hasattr(ep, "client") else None
    if not isinstance(client, VariClient):
        return {str(t).upper(): 0.0 for t in tickers}

    lte_dt = datetime.now(timezone.utc)
    gte_dt = lte_dt - timedelta(hours=float(hours))
    export_timeout_s = float(os.environ.get("EXPORT_POLL_TIMEOUT_S", "120"))

    want = {str(t).strip().upper() for t in tickers if str(t).strip()}
    rpnl_rows = fetch_export_csv(
        client,
        resource="transfers",
        gte_dt=gte_dt,
        lte_dt=lte_dt,
        timeout_s=export_timeout_s,
    )
    upnl_by = fetch_upnl_by_ticker(ep)
    rpnl_by = aggregate_rpnl(rpnl_rows)
    rows = build_snapshot_rows(
        rpnl_by={k: v for k, v in rpnl_by.items() if k in want},
        upnl_by={k: v for k, v in upnl_by.items() if k in want},
        vol_by={},
    )
    return {str(r["ticker"]).upper(): float(r["total_pnl"]) for r in rows}


def build_swap_plan(
    *,
    hyperparam_results: Sequence[Dict[str, Any]],
    current_roster: Dict[str, float],
    live_pnl: Dict[str, float],
    roster_sz: int,
    max_swaps: int,
    paused_tickers: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Build pending swap document."""
    paused = {str(t).strip().upper() for t in (paused_tickers or set()) if str(t).strip()}
    current = {str(k).strip().upper(): float(v) for k, v in current_roster.items()}
    roster_after = dict(current)

    candidates = [
        r for r in hyperparam_results
        if str(r.get("ticker") or "").strip().upper() not in current
    ]
    candidates.sort(key=lambda x: float(x.get("best_pnl") or -1e18), reverse=True)

    to_add: List[Dict[str, Any]] = []
    to_remove: List[str] = []

    # Fill empty slots first
    while len(roster_after) < roster_sz and candidates:
        pick = candidates.pop(0)
        sym = str(pick["ticker"]).strip().upper()
        roster_after[sym] = float(pick["best_band_pct"])
        to_add.append(
            {
                "ticker": sym,
                "band_pct": float(pick["best_band_pct"]),
                "sim_pnl": float(pick.get("best_pnl") or 0.0),
            }
        )

    if max_swaps <= 0 or not candidates:
        return _pending_doc(
            remove=to_remove,
            add=to_add,
            roster_after=roster_after,
            live_evict_pnl={},
        )

    # Rank current roster by live 24h PnL asc for eviction
    ranked = sorted(
        current.keys(),
        key=lambda s: (float(live_pnl.get(s, 0.0)), s),
    )
    evict_pool = [s for s in ranked if s not in paused]

    n_swap = min(int(max_swaps), len(candidates), len(evict_pool))
    for i in range(n_swap):
        rem = evict_pool[i]
        pick = candidates[i]
        sym = str(pick["ticker"]).strip().upper()
        if sym in roster_after:
            continue
        roster_after.pop(rem, None)
        roster_after[sym] = float(pick["best_band_pct"])
        to_remove.append(rem)
        to_add.append(
            {
                "ticker": sym,
                "band_pct": float(pick["best_band_pct"]),
                "sim_pnl": float(pick.get("best_pnl") or 0.0),
            }
        )

    live_evict = {s: float(live_pnl.get(s, 0.0)) for s in to_remove}
    return _pending_doc(
        remove=to_remove,
        add=to_add,
        roster_after=roster_after,
        live_evict_pnl=live_evict,
    )


def _pending_doc(
    *,
    remove: List[str],
    add: List[Dict[str, Any]],
    roster_after: Dict[str, float],
    live_evict_pnl: Dict[str, float],
) -> Dict[str, Any]:
    return {
        "schema": PENDING_SCHEMA,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
        "remove": list(remove),
        "add": list(add),
        "roster_after": {str(k).upper(): float(v) for k, v in roster_after.items()},
        "live_evict_pnl": live_evict_pnl,
    }


class RotationStore(Protocol):
    def has_pending(self) -> bool: ...
    def load_pending(self) -> Optional[Dict[str, Any]]: ...
    def write_pending(self, doc: Dict[str, Any]) -> None: ...
    def mark_applied(self, doc: Dict[str, Any]) -> None: ...


class FileRotationStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = str(path or default_pending_path())

    def has_pending(self) -> bool:
        doc = self.load_pending()
        return bool(doc and not doc.get("applied_at"))

    def load_pending(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def write_pending(self, doc: Dict[str, Any]) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, self.path)

    def mark_applied(self, doc: Dict[str, Any]) -> None:
        doc = dict(doc)
        doc["applied_at"] = datetime.now(timezone.utc).isoformat()
        self.write_pending(doc)


class KeyValueRotationStore:
    def __init__(self, url: Optional[str] = None, key: Optional[str] = None) -> None:
        self.url = (url or os.environ.get(ENV_KEY_VALUE_URL) or "").strip()
        self.key = (key or os.environ.get(ENV_KEY_VALUE_KEY) or DEFAULT_KEYVALUE_KEY).strip()
        if not self.url:
            raise ValueError(f"{ENV_KEY_VALUE_URL} is required for keyvalue store")

    def _client(self) -> Any:
        import redis

        return redis.from_url(self.url, decode_responses=True)

    def has_pending(self) -> bool:
        doc = self.load_pending()
        return bool(doc and not doc.get("applied_at"))

    def load_pending(self) -> Optional[Dict[str, Any]]:
        raw = self._client().get(self.key)
        if not raw:
            return None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return doc if isinstance(doc, dict) else None

    def write_pending(self, doc: Dict[str, Any]) -> None:
        self._client().set(self.key, json.dumps(doc, indent=2))

    def mark_applied(self, doc: Dict[str, Any]) -> None:
        doc = dict(doc)
        doc["applied_at"] = datetime.now(timezone.utc).isoformat()
        self.write_pending(doc)


def make_rotation_store() -> RotationStore:
    kind = (os.environ.get(ENV_STORE) or DEFAULT_STORE).strip().lower()
    if kind == "keyvalue":
        return KeyValueRotationStore()
    return FileRotationStore()


def clear_pause_state_for_tickers(tickers: Set[str], *, varibot_dir: str) -> None:
    if not tickers:
        return
    try:
        from grid_vol_pause import default_state_path as vp_path, load_state as vp_load, save_state as vp_save
        from ticker_pause import default_state_path as tp_path, load_pause_state, save_pause_state

        vp = vp_load(vp_path(varibot_dir))
        for sym in tickers:
            if isinstance(vp.get("paused"), dict):
                vp["paused"].pop(sym, None)
            if isinstance(vp.get("calm_cycles"), dict):
                vp["calm_cycles"].pop(sym, None)
        vp_save(vp_path(varibot_dir), vp)

        tp = load_pause_state(tp_path(varibot_dir))
        if isinstance(tp.get("paused"), dict):
            for sym in tickers:
                tp["paused"].pop(sym, None)
        save_pause_state(tp_path(varibot_dir), tp)
    except ImportError:
        pass


def prune_gridstrat_assets(removed: Set[str]) -> None:
    if not removed:
        return
    path = _default_state_path()
    root = load_state(path)
    assets = root.get("assets")
    if not isinstance(assets, dict):
        return
    for sym in removed:
        assets.pop(sym, None)
    root["assets"] = assets
    save_state(path, root)


def exit_ticker_from_grid(
    ep: Any,
    sym: str,
    *,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
) -> None:
    from portfolio_manager_pairs import _instrument_label, _position_qty, _positions_list
    from ticker_pause import cancel_ticker_limits

    sym_u = str(sym).strip().upper()
    cancel_ticker_limits(ep, ticker=sym_u, log=log, live=bool(live and not dry_run))

    try:
        positions_raw = ep.get_positions()
    except Exception as e:
        log(f"grid_rotation[{sym_u}]: positions fetch failed ({type(e).__name__}: {e})")
        return

    qty = None
    for p in _positions_list(positions_raw):
        if not isinstance(p, dict):
            continue
        if _instrument_label(p).strip().upper() != sym_u:
            continue
        qty = _position_qty(p)
        break

    qty_abs = abs(float(qty or 0.0))
    if qty_abs <= 1e-12:
        return
    close_side = "sell" if float(qty) > 0 else "buy"
    if live and not dry_run:
        close_position(sym_u, qty_abs, close_side)
        log(f"grid_rotation[{sym_u}]: flattened {close_side} qty={qty_abs:g}")
    else:
        log(f"grid_rotation[{sym_u}]: dry-run — would flatten {close_side} qty={qty_abs:g}")


def symbols_with_grid_exposure(
    ep: Any,
    *,
    positions_raw: Any = None,
) -> Set[str]:
    from grid_limits_reconcile import _fetch_pending_limit_rows
    from portfolio_manager_pairs import _instrument_label, _position_qty, _positions_list

    out: Set[str] = set()
    if positions_raw is None:
        try:
            positions_raw = ep.get_positions()
        except Exception:
            positions_raw = []

    for p in _positions_list(positions_raw):
        if not isinstance(p, dict):
            continue
        sym = _instrument_label(p).strip().upper()
        qty = _position_qty(p)
        if sym and qty is not None and abs(float(qty)) > 1e-12:
            out.add(sym)

    try:
        from grid_limits_reconcile import _fetch_pending_limit_rows_paginated, _row_underlying

        for row in _fetch_pending_limit_rows_paginated(ep):
            if isinstance(row, dict):
                u = _row_underlying(row).strip().upper()
                if u:
                    out.add(u)
    except ImportError:
        pass

    return out


def reconcile_orphan_tickers(
    ep: Any,
    *,
    allowed: Set[str],
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
    positions_raw: Any = None,
) -> Set[str]:
    """Exit tickers with limits/positions not in *allowed* roster."""
    orphans = symbols_with_grid_exposure(ep, positions_raw=positions_raw) - allowed
    for sym in sorted(orphans):
        log(f"grid_rotation: orphan exit {sym} (not in active roster)")
        exit_ticker_from_grid(
            ep,
            sym,
            live=live,
            dry_run=dry_run,
            log=log,
            close_position=close_position,
        )
    return orphans


def apply_pending_swap(
    ep: Any,
    pending: Dict[str, Any],
    *,
    store: RotationStore,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
    varibot_dir: str,
) -> bool:
    if pending.get("applied_at"):
        return False

    roster_after = pending.get("roster_after")
    if not isinstance(roster_after, dict) or not roster_after:
        log("grid_rotation: pending missing roster_after — skip")
        return False

    remove = [str(s).strip().upper() for s in (pending.get("remove") or []) if str(s).strip()]
    for sym in remove:
        exit_ticker_from_grid(
            ep,
            sym,
            live=live,
            dry_run=dry_run,
            log=log,
            close_position=close_position,
        )

    tickers = {str(k).strip().upper(): float(v) for k, v in roster_after.items() if str(k).strip()}
    if not dry_run:
        save_grid_trading_roster(
            tickers,
            roster_size=roster_size(),
            extra={"source": "grid_ticker_rotation"},
        )
        prune_gridstrat_assets(set(remove))
        clear_pause_state_for_tickers(set(remove), varibot_dir=varibot_dir)
        store.mark_applied(pending)
        log(
            f"grid_rotation: applied swap remove={remove} "
            f"add={[a.get('ticker') for a in pending.get('add') or []]} "
            f"roster_size={len(tickers)}"
        )
    elif dry_run:
        log(
            f"grid_rotation: dry-run would apply remove={remove} "
            f"add={[a.get('ticker') for a in pending.get('add') or []]}"
        )
    return True


def run_evaluate(
    ep: Any,
    *,
    store: RotationStore,
    write_pending: bool = False,
    db_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    from grid_rotation_sim import fetch_klines_48h5m, run_band_hyperparam

    pool = candidate_pool_size()
    _progress(f"evaluate start — pool={pool} roster_size={roster_size()} swap_count={swap_count()}")
    universe = fetch_top_volume_universe(ep, top_n=pool)
    tickers = [t for t, _ in universe]
    _progress(f"universe ranked — {len(tickers)} tickers (top vol, blacklist scrubbed)")

    if db_dir is not None:
        run_dir = db_dir
    else:
        raw_run = (os.environ.get(ENV_RUN_DIR) or "").strip()
        run_dir = Path(raw_run).expanduser() if raw_run else (_VARIBOT_DIR / "runs" / "rotation")
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / f"rotation_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.sqlite"

    _progress(f"fetching 48h Binance 5m klines → {db_path.name} (~{len(tickers) + 2} symbols)")
    fetch_meta = fetch_klines_48h5m(tickers, db_path=db_path, progress=_progress)
    _progress(
        f"klines done — fetched={len(fetch_meta.get('fetched') or [])} "
        f"skipped={len(fetch_meta.get('skipped') or [])}"
    )

    _progress("running band hyperparam (vol-pause sim, 5 bands per ticker)...")
    hyper = run_band_hyperparam(db_path, tickers=tickers, progress=_progress)
    _progress(f"hyperparam done — {len(hyper)} tickers scored")

    current = current_roster_tickers()
    _progress(f"current roster — {len(current)} tickers: {', '.join(sorted(current.keys()))}")
    _progress("fetching live 24h PnL exports (RPNL + uPNL)...")
    try:
        live_pnl = fetch_live_snapshot_pnl(ep, tickers=list(current.keys()), hours=24.0)
        _progress("live PnL export done")
    except Exception as e:
        _progress(
            f"live PnL export FAILED ({type(e).__name__}: {e}) — "
            "continuing with zero live PnL for swap ranking"
        )
        live_pnl = {str(t).strip().upper(): 0.0 for t in current.keys()}

    paused: Set[str] = set()
    try:
        from grid_vol_pause import default_state_path as vp_path, load_state as vp_load, paused_ticker_set
        from ticker_pause import default_state_path as tp_path, load_pause_state, paused_ticker_set as tp_set

        paused |= paused_ticker_set(vp_load(vp_path(str(_VARIBOT_DIR))))
        paused |= tp_set(load_pause_state(tp_path(str(_VARIBOT_DIR))))
    except ImportError:
        pass

    plan = build_swap_plan(
        hyperparam_results=hyper,
        current_roster=current,
        live_pnl=live_pnl,
        roster_sz=roster_size(),
        max_swaps=swap_count(),
        paused_tickers=paused,
    )

    payload = {
        "universe": [{"ticker": t, "vol_24h": v} for t, v in universe],
        "fetch": fetch_meta,
        "hyperparam_top": hyper[:15],
        "plan": plan,
    }

    n_rem = len(plan.get("remove") or [])
    n_add = len(plan.get("add") or [])
    _progress(
        f"swap plan — remove={n_rem} add={n_add} "
        f"roster_after={len(plan.get('roster_after') or {})}"
    )
    if plan.get("remove"):
        _progress(f"  remove: {plan.get('remove')}")
    if plan.get("add"):
        add_syms = [a.get("ticker") for a in (plan.get("add") or []) if isinstance(a, dict)]
        _progress(f"  add: {add_syms}")

    if write_pending and (plan.get("remove") or plan.get("add")):
        store.write_pending(plan)
        _progress("wrote pending plan to store (Gridbot will apply on next cycle)")
    elif write_pending:
        payload["plan_skipped"] = "no changes"
        _progress("no roster changes — pending not written")

    _progress("evaluate complete")
    return payload


def maybe_apply_grid_ticker_rotation(
    ep: Any,
    *,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    close_position: Callable[[str, float, str], None],
    varibot_dir: str,
    positions_raw: Any = None,
) -> bool:
    if not rotation_enabled():
        return False

    store = make_rotation_store()
    if store.has_pending():
        pending = store.load_pending()
        if pending:
            apply_pending_swap(
                ep,
                pending,
                store=store,
                live=live,
                dry_run=dry_run,
                log=log,
                close_position=close_position,
                varibot_dir=varibot_dir,
            )

    allowed = set(grid_trading_ticker_band_pcts().keys())
    reconcile_orphan_tickers(
        ep,
        allowed=allowed,
        live=live,
        dry_run=dry_run,
        log=log,
        close_position=close_position,
        positions_raw=positions_raw,
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Grid ticker rotation evaluate / apply")
    ap.add_argument("--evaluate", action="store_true", help="Run universe + hyperparam + build plan")
    ap.add_argument("--write-pending", action="store_true", help="Write pending plan to store")
    ap.add_argument("--apply-pending", action="store_true", help="Apply pending plan from store")
    ap.add_argument("--live", action="store_true", help="Live venue actions")
    ap.add_argument("--json", action="store_true", help="Print JSON result")
    args = ap.parse_args()

    from variationalbot.config import load_config
    from variationalbot.vari import VariAuth, VariClient, VariEndpoints

    cfg = load_config()
    client = VariClient(
        base_url=cfg.base_url,
        auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
    )
    ep = VariEndpoints(client)
    store = make_rotation_store()

    if args.apply_pending:
        pending = store.load_pending()
        if not pending or pending.get("applied_at"):
            print("No unapplied pending plan.", file=sys.stderr)
            return 1

        def _close(sym: str, qty: float, side: str) -> None:
            from varibot import _close_reduce_only_with_slippage_steps, _resolve_max_slippage

            _close_reduce_only_with_slippage_steps(
                ep=ep,
                sym=sym,
                qty_abs=float(qty),
                close_side=str(side),
                max_slip=float(_resolve_max_slippage()),
            )

        apply_pending_swap(
            ep,
            pending,
            store=store,
            live=bool(args.live),
            dry_run=not bool(args.live),
            log=lambda m: print(m),
            close_position=_close,
            varibot_dir=str(_VARIBOT_DIR),
        )
        return 0

    if args.evaluate:
        _progress(
            f"CLI evaluate (write_pending={bool(args.write_pending)}, "
            f"store={(os.environ.get(ENV_STORE) or DEFAULT_STORE).strip()})"
        )
        payload = run_evaluate(ep, store=store, write_pending=bool(args.write_pending))
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            plan = payload.get("plan") or {}
            print(f"Universe: {len(payload.get('universe') or [])} tickers")
            print(f"Remove: {plan.get('remove')}")
            print(f"Add: {plan.get('add')}")
            if args.write_pending:
                print("Wrote pending plan to store.")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
