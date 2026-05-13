"""
Vari grid strategy (gridbot.md): price ladder below/above mark with asymmetric buy restoration.

This module approximates a limit grid using **discrete mark snapshots** from `listingtabledata.json`
(vari_ticker `mark_price`). When mark moves down through a buy level → one **market buy** event; when
it moves up through a sell level → one **market sell** event.

Restoration rule (gridbot.md): after buy levels are consumed on the way down, **re-arm all buy rungs
below the current mark** only after mark has crossed **upward through the first sell rung** (the
lowest sell price that was active at the last full template rebuild).

Limit orders are **not** placed natively (Vari client here is market-only); use small `--period-min`
or refresh listing often if you want tighter grid fills.

Configure via environment variables (mirrors the order form):

  GRID_ASSET=BTC
  GRID_LOWER=86000
  GRID_UPPER=89000
  GRID_NUM=30              # number of equal steps from lower→upper (fenceposts = GRID_NUM+1 prices)
  GRID_TYPE=arithmetic     # or geometric
  GRID_INVESTMENT_USD=300
  GRID_LEVERAGE=25
  GRID_MARK=               # optional override for mark (else from listingtabledata.json)
  GRIDSTRAT_STATE_PATH=    # optional; default Varibot/gridstrat_state.json under repo root
  GRIDSTRAT_RESET=1        # delete state file on next pick_tickers (one-shot)

Optional compatibility: strategy key `invert_extreme` still resolves to this module on the GridBot
branch — behaviour is always this grid when env bounds are set; otherwise pick_tickers returns an
error meta and empty books.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

STRATEGY_NAME: str = "vari_grid"

# multimarketorder.py imports this as sizing divisor fallback when strategy import succeeds.
DEFAULT_MAX_TICKER_ENTRIES: int = 1

TRADE_THESIS: str = (
    "Arithmetic or geometric price ladder between GRID_LOWER and GRID_UPPER. "
    "Buy rungs sit strictly below mark; sell rungs strictly above. "
    "On each down-cross of an armed buy rung → market buy (notional from investment×leverage / rung count). "
    "On each up-cross of an armed sell rung → market sell. "
    "After any buy rung fires, further buy rungs still fire on continued drops until restoration gate: "
    "buy template is re-armed below mark only after an upward cross through the first sell anchor "
    "(minimum initial sell price). "
    "Execution uses Vari market orders driven by listing mark snapshots — not exchange-native limits."
)

WRITE_STRATEGY_OUTPUT_TXT: bool = True


def _repo_root_from_here() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _default_state_path() -> str:
    raw = (os.environ.get("GRIDSTRAT_STATE_PATH") or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(_repo_root_from_here(), "Varibot", "gridstrat_state.json")


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


GridType = Literal["arithmetic", "geometric"]


@dataclass
class GridConfig:
    asset: str
    lower: float
    upper: float
    n_grids: int
    grid_type: GridType
    investment_usd: float
    leverage: float
    mark_override: Optional[float]

    @classmethod
    def from_env(cls) -> "GridConfig":
        asset = (os.environ.get("GRID_ASSET") or "BTC").strip().upper()
        lower = float(os.environ.get("GRID_LOWER", "nan"))
        upper = float(os.environ.get("GRID_UPPER", "nan"))
        n_grids = int(os.environ.get("GRID_NUM", "0"))
        gt = (os.environ.get("GRID_TYPE") or "arithmetic").strip().lower()
        grid_type: GridType = "geometric" if gt == "geometric" else "arithmetic"
        inv = float(os.environ.get("GRID_INVESTMENT_USD", "nan"))
        lev = float(os.environ.get("GRID_LEVERAGE", "nan"))
        mo = (os.environ.get("GRID_MARK") or "").strip()
        mark_override = float(mo) if mo else None
        return cls(
            asset=asset,
            lower=lower,
            upper=upper,
            n_grids=n_grids,
            grid_type=grid_type,
            investment_usd=inv,
            leverage=lev,
            mark_override=mark_override,
        )

    def validate(self) -> Optional[str]:
        if math.isnan(self.lower) or math.isnan(self.upper):
            return "Set GRID_LOWER and GRID_UPPER (floats)."
        if self.upper <= self.lower:
            return "GRID_UPPER must be > GRID_LOWER."
        if self.n_grids < 2:
            return "GRID_NUM must be >= 2 (even count of rungs recommended in UI; we accept any integer >=2)."
        if math.isnan(self.investment_usd) or self.investment_usd <= 0:
            return "Set positive GRID_INVESTMENT_USD."
        if math.isnan(self.leverage) or self.leverage <= 0:
            return "Set positive GRID_LEVERAGE."
        return None


def _mark_from_listing(listing_json: str, asset: str) -> Optional[float]:
    try:
        with open(str(listing_json), "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    listings = doc.get("listings") if isinstance(doc, dict) else None
    if not isinstance(listings, list):
        return None
    want = str(asset).strip().upper()
    for row in listings:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("vari_ticker") or "").strip().upper()
        if sym != want:
            continue
        mp = _as_float(row.get("mark_price"))
        if mp is not None and mp > 0:
            return float(mp)
    return None


def build_price_ladder(*, lower: float, upper: float, n_grids: int, grid_type: GridType) -> List[float]:
    """
    Return sorted unique rung prices strictly between lower and upper (endpoints excluded),
    with n_grids interior points when possible.

    Arithmetic: equal absolute step.
    Geometric: equal ratio between consecutive positive prices (requires lower>0).
    """
    if upper <= lower or n_grids < 1:
        return []
    if grid_type == "arithmetic":
        step = (upper - lower) / float(n_grids + 1)
        return [lower + step * (i + 1) for i in range(n_grids)]
    # geometric
    if lower <= 0:
        return []
    ratio = (upper / lower) ** (1.0 / float(n_grids + 1))
    out: List[float] = []
    p = lower
    for _ in range(n_grids):
        p *= ratio
        if p >= upper:
            break
        out.append(float(p))
    return out


def split_buy_sell(levels: Sequence[float], mark: float) -> Tuple[List[float], List[float]]:
    buys = sorted({float(x) for x in levels if float(x) < mark})
    sells = sorted({float(x) for x in levels if float(x) > mark})
    return buys, sells


def per_rung_usd_notional(*, investment_usd: float, leverage: float, n_rungs: int) -> float:
    denom = max(1, int(n_rungs))
    return float(investment_usd) * float(leverage) / float(denom)


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _template_fingerprint(cfg: GridConfig, levels: Sequence[float]) -> str:
    return json.dumps(
        {
            "a": cfg.asset,
            "lo": cfg.lower,
            "hi": cfg.upper,
            "n": cfg.n_grids,
            "t": cfg.grid_type,
            "levels": [round(x, 8) for x in levels],
        },
        sort_keys=True,
    )


def advance_grid_state(
    *,
    cfg: GridConfig,
    mark: float,
    prev_mark: Optional[float],
    levels_template: Sequence[float],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (grid_market_events, new_state).

    grid_market_events items:
      {"action": "open_buy"|"open_sell"|"grid_restore_buys", "asset": str, "usd": float, "price": float, "reason": str}
    """
    events: List[Dict[str, Any]] = []
    levels_list = [float(x) for x in levels_template]
    fp = _template_fingerprint(cfg, levels_list)
    usd_leg = per_rung_usd_notional(
        investment_usd=cfg.investment_usd, leverage=cfg.leverage, n_rungs=max(1, len(levels_list))
    )

    if state.get("fingerprint") != fp or os.environ.get("GRIDSTRAT_RESET", "").strip() in ("1", "true", "yes"):
        buys, sells = split_buy_sell(levels_list, mark)
        first_sell = min(sells) if sells else None
        state = {
            "schema_version": 2,
            "fingerprint": fp,
            "last_mark": float(mark),
            "first_sell_price": first_sell,
            "levels_template": levels_list,
            "buy_armed": [float(x) for x in buys],
            "sell_armed": [float(x) for x in sells],
        }
        _save_state(_default_state_path(), state)
        os.environ.pop("GRIDSTRAT_RESET", None)
        return events, state

    prev = float(prev_mark) if prev_mark is not None else float(state.get("last_mark") or mark)
    buy_armed: Set[float] = {float(x) for x in (state.get("buy_armed") or [])}
    sell_armed: Set[float] = {float(x) for x in (state.get("sell_armed") or [])}
    first_sell = state.get("first_sell_price")
    first_sell_f = float(first_sell) if first_sell is not None else None
    levels_stored = [float(x) for x in (state.get("levels_template") or levels_list)]

    # --- upward cross through first sell anchor → re-arm all buy rungs below mark ---
    if first_sell_f is not None and prev < first_sell_f <= mark:
        buy_armed = {float(p) for p in levels_stored if float(p) < float(mark)}
        events.append(
            {
                "action": "grid_restore_buys",
                "asset": cfg.asset,
                "usd": 0.0,
                "price": float(first_sell_f),
                "reason": "up_cross_first_sell_anchor_rearm_buys",
            }
        )

    # --- down-cross buys (may be multiple in one snapshot) ---
    if mark < prev:
        for b in sorted(buy_armed):
            if float(mark) < float(b) < float(prev):
                events.append(
                    {
                        "action": "open_buy",
                        "asset": cfg.asset,
                        "usd": float(usd_leg),
                        "price": float(b),
                        "reason": "down_cross_buy_rung",
                    }
                )
                buy_armed.discard(float(b))
    # --- up-cross sells ---
    if mark > prev:
        for s in sorted(sell_armed):
            if float(prev) < float(s) <= float(mark):
                events.append(
                    {
                        "action": "open_sell",
                        "asset": cfg.asset,
                        "usd": float(usd_leg),
                        "price": float(s),
                        "reason": "up_cross_sell_rung",
                    }
                )
                sell_armed.discard(float(s))

    state["buy_armed"] = sorted(buy_armed)
    state["sell_armed"] = sorted(sell_armed)
    state["last_mark"] = float(mark)
    state["levels_template"] = levels_stored
    _save_state(_default_state_path(), state)
    return events, state


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,
    top_n: Optional[int] = None,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    _ = top_n
    _ = marketstate_json
    cfg = GridConfig.from_env()
    err = cfg.validate()
    state_path = _default_state_path()
    if err:
        return (
            [],
            [],
            {
                "strategy": STRATEGY_NAME,
                "thesis": TRADE_THESIS,
                "error": err,
                "grid_market_events": [],
                "grid_mode": True,
            },
        )

    mark = cfg.mark_override if cfg.mark_override is not None else _mark_from_listing(listing_json, cfg.asset)
    if mark is None or mark <= 0:
        return (
            [],
            [],
            {
                "strategy": STRATEGY_NAME,
                "thesis": TRADE_THESIS,
                "error": f"No mark_price for {cfg.asset} in listing json.",
                "grid_market_events": [],
                "grid_mode": True,
            },
        )

    levels = build_price_ladder(
        lower=cfg.lower, upper=cfg.upper, n_grids=cfg.n_grids, grid_type=cfg.grid_type
    )
    buys, sells = split_buy_sell(levels, float(mark))
    state = _load_state(state_path)
    prev_mark_f: Optional[float] = None
    lm = state.get("last_mark")
    if isinstance(lm, (int, float)):
        prev_mark_f = float(lm)

    events, new_state = advance_grid_state(
        cfg=cfg,
        mark=float(mark),
        prev_mark=prev_mark_f,
        levels_template=levels,
        state=state,
    )

    meta: Dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "grid_mode": True,
        "grid_market_events": events,
        "grid_asset": cfg.asset,
        "grid_mark": float(mark),
        "grid_lower": cfg.lower,
        "grid_upper": cfg.upper,
        "grid_num": cfg.n_grids,
        "grid_type": cfg.grid_type,
        "grid_buy_rungs": buys,
        "grid_sell_rungs": sells,
        "grid_per_rung_usd": per_rung_usd_notional(
            investment_usd=cfg.investment_usd,
            leverage=cfg.leverage,
            n_rungs=max(1, len(levels)),
        ),
        "grid_state_path": os.path.abspath(state_path),
        "first_sell_anchor": new_state.get("first_sell_price"),
        "long_count": 0,
        "short_count": 0,
    }
    return [], [], meta


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Vari gridstrat: print ladder + dry state bump.")
    ap.add_argument("--json-path", default=os.path.join(_repo_root_from_here(), "Vari Listings", "listingtabledata.json"))
    ap.add_argument("--print-json", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    longs, shorts, meta = pick_tickers(listing_json=str(args.json_path), marketstate_json=None, top_n=0)
    if args.print_json:
        print(json.dumps({**meta, "long": longs, "short": shorts}, indent=2))
        return 0
    print(f"Strategy={meta.get('strategy')} mark={meta.get('grid_mark')} events={meta.get('grid_market_events')}")
    print(f"Buys={meta.get('grid_buy_rungs')}\nSells={meta.get('grid_sell_rungs')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- Loader: shared strategy_output.txt table + run_strategy() ---

STRATEGIES: Dict[str, str] = {
    "invert_extreme": "gridstrat",
    "gridstrat": "gridstrat",
    "vari_grid": "gridstrat",
}

STRATEGY_OUTPUT_TXT: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_output.txt")

OUTPUT_COLUMNS: List[str] = [
    "side",
    "24hChg%",
    "mcap_usd",
    "vol_24h_usd",
    "oi_usd",
    "oi_skew",
    "ann_fundingrate",
]


def _parse_pct_field(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace("%", "")
    try:
        return float(s)
    except Exception:
        return None


def _load_listing_index(listing_json: str) -> Dict[str, Dict[str, Any]]:
    with open(listing_json, "r", encoding="utf-8") as f:
        doc = json.load(f)
    listings = doc.get("listings") if isinstance(doc, dict) else None
    if not isinstance(listings, list):
        return {}
    idx: Dict[str, Dict[str, Any]] = {}
    for d in listings:
        if not isinstance(d, dict):
            continue
        sym = str(d.get("vari_ticker") or "").strip().upper()
        if not sym:
            continue
        idx[sym] = d
    return idx


def _fmt_num(v: Optional[float], *, decimals: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{decimals}f}"
    except Exception:
        return "-"


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        x = float(v)
    except Exception:
        return "-"
    if x >= 1_000_000_000:
        return f"${x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.2f}K"
    return f"${x:.0f}"


def _row_for_symbol(
    sym: str,
    *,
    side: str,
    listing: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    d = listing or {}
    chg24 = _parse_pct_field(d.get("price_change_24h_pct"))
    mcap = _as_float(d.get("market_cap"))
    vol = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
    oi = _as_float(d.get("OI") if "OI" in d else d.get("open_interest"))
    skew = _as_float(d.get("OI Skew"))
    afr = _as_float(d.get("ann_fundingrate"))

    out: Dict[str, str] = {
        "Symbol": sym,
        "side": side,
        "24hChg%": (f"{chg24:.2f}%" if chg24 is not None else "-"),
        "mcap_usd": _fmt_usd(mcap),
        "vol_24h_usd": _fmt_usd(vol),
        "oi_usd": _fmt_usd(oi),
        "oi_skew": _fmt_num(skew, decimals=3),
        "ann_fundingrate": _fmt_num(afr, decimals=3),
    }
    return out


def write_strategy_output_txt(
    *,
    strategy_key: str,
    meta: Dict[str, Any],
    listing_json: str,
    longs: Iterable[str],
    shorts: Iterable[str],
) -> None:
    fetched_at_sgt: Optional[str] = None
    try:
        with open(listing_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            fa = doc.get("fetched_at")
            if isinstance(fa, str) and fa.strip():
                fetched_at_sgt = fa.strip()
    except Exception:
        fetched_at_sgt = None

    idx = _load_listing_index(listing_json)

    rows: List[Dict[str, str]] = []
    for s in [str(x).strip().upper() for x in longs if str(x).strip()]:
        rows.append(_row_for_symbol(s, side="Long", listing=idx.get(s)))
    for s in [str(x).strip().upper() for x in shorts if str(x).strip()]:
        rows.append(_row_for_symbol(s, side="Short", listing=idx.get(s)))

    cols = ["Symbol"] + [c for c in OUTPUT_COLUMNS]
    widths: Dict[str, int] = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(r.get(c, "-"))))

    def line(parts: List[str]) -> str:
        out_parts: List[str] = []
        for i in range(len(cols)):
            cell = str(parts[i])
            if i >= 2:
                out_parts.append(cell.rjust(widths[cols[i]]))
            else:
                out_parts.append(cell.ljust(widths[cols[i]]))
        return "  ".join(out_parts)

    header_lines: List[str] = [
        f"Strategy: {meta.get('strategy') or strategy_key}",
        f"listing_json: {os.path.abspath(str(listing_json))}",
        f"fetched_at: {fetched_at_sgt or '-'}",
    ]
    thesis = meta.get("thesis") if isinstance(meta, dict) else None
    if isinstance(thesis, str) and thesis.strip():
        parts = [p.strip() for p in thesis.strip().split(". ") if p.strip()]
        if len(parts) <= 1:
            header_lines.append(thesis.strip())
        else:
            for i, p in enumerate(parts):
                header_lines.append(p if p.endswith(".") else (p + "."))

    body: List[str] = []
    body.extend(header_lines)
    body.append("")
    if meta.get("grid_mode"):
        body.append(f"grid_asset={meta.get('grid_asset')} mark={meta.get('grid_mark')}")
        body.append(f"grid_events_this_cycle={json.dumps(meta.get('grid_market_events') or [])}")
        body.append("")
    if not rows:
        body.append("(no symbol rows — grid uses GRID_* env + listing mark)")
    else:
        body.append(line(cols))
        body.append(line(["-" * widths[c] for c in cols]))
        for r in rows:
            body.append(line([str(r.get(c, "-")) for c in cols]))

    with open(STRATEGY_OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


def resolve_strategy_module_name(key: str) -> str:
    k = (key or "").strip().lower()
    if not k:
        raise ValueError("strategy key is empty")
    if k.endswith(".py"):
        k = k[:-3]
    mod = STRATEGIES.get(k)
    if mod:
        return mod
    return k


def load_strategy_module(key: str):
    mod_name = resolve_strategy_module_name(key)
    return importlib.import_module(f"strategy.{mod_name}")


def run_strategy(
    *,
    strategy_key: str,
    listing_json: str,
    marketstate_json: Optional[str],
    top_n: int,
    write_output_txt: bool = True,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    mod = load_strategy_module(strategy_key)
    if not hasattr(mod, "pick_tickers"):
        raise AttributeError(
            f"Strategy module {mod.__name__!r} missing required function pick_tickers(listing_json, marketstate_json)"
        )
    longs, shorts, meta = mod.pick_tickers(
        listing_json=listing_json,
        marketstate_json=marketstate_json,
        top_n=int(top_n),
    )
    if not isinstance(meta, dict):
        meta = {"strategy": str(getattr(mod, "STRATEGY_NAME", mod.__name__))}
    meta.setdefault("strategy", str(getattr(mod, "STRATEGY_NAME", mod.__name__)))
    meta.setdefault("long_count", len(longs))
    meta.setdefault("short_count", len(shorts))

    if write_output_txt:
        try:
            write_strategy_output_txt(
                strategy_key=strategy_key,
                meta=meta,
                listing_json=listing_json,
                longs=longs,
                shorts=shorts,
            )
        except OSError:
            pass

    return list(longs), list(shorts), meta
