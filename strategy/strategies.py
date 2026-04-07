from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Strategy key -> submodule name under the `strategy` package (must expose `pick_tickers()`).
STRATEGIES: Dict[str, str] = {
    "median_filter": "median_filter",
    "median_revert": "revert_median",
    "revert_median": "revert_median",
    "funding_pairs": "funding_pairs",
}

# Shared strategy output file (written when strategies are triggered via this loader).
STRATEGY_OUTPUT_TXT: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_output.txt")

# Default columns for the shared table output (edit as you like).
# Supported keys are documented in `_row_for_symbol`.
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


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
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

    # Map supported columns to display strings.
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
    # Also pull the listing's own fetched_at (SGT string) for the header.
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
    # Compute widths
    widths: Dict[str, int] = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(r.get(c, "-"))))

    def line(parts: List[str]) -> str:
        out_parts: List[str] = []
        for i in range(len(cols)):
            cell = str(parts[i])
            # Right-align numeric-ish columns from the 3rd column onward.
            # cols[0]=Symbol, cols[1]=side, cols[2:]=criteria columns.
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
        # Break thesis into one sentence per line for readability.
        parts = [p.strip() for p in thesis.strip().split(". ") if p.strip()]
        if len(parts) <= 1:
            header_lines.append(thesis.strip())
        else:
            for i, p in enumerate(parts):
                header_lines.append(p if p.endswith(".") else (p + "."))

    body: List[str] = []
    body.extend(header_lines)
    body.append("")
    if not rows:
        body.append("(no symbols)")
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
    # Allow passing a module name directly (must exist under strategy/).
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

