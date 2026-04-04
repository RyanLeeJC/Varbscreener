from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints


def _first_str(d: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _first_float(d: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for k in keys:
        if k not in d:
            continue
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _nested_float(d: Dict[str, Any], path: List[str]) -> Optional[float]:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    try:
        return float(cur)
    except Exception:
        return None


def _instrument_label(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    if isinstance(inst, str) and inst.strip():
        return inst.strip().upper()

    pos_info = p.get("position_info")
    if isinstance(pos_info, dict):
        inst2 = pos_info.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()

    for k in ("instrument_name", "instrument_id", "instrumentId", "symbol"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()

    underlying = p.get("underlying")
    if isinstance(underlying, str) and underlying.strip():
        return underlying.strip().upper()
    return "UNKNOWN"


def _fmt_num(v: Optional[float], *, decimals: int = 4) -> str:
    if v is None:
        return "-"
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


def _fmt_usd(v: Optional[float], *, decimals: int = 2) -> str:
    if v is None:
        return "-"
    return f"${v:,.{decimals}f}"


def _print_positions_table(positions: List[Dict[str, Any]]) -> None:
    cols = ["Instrument", "Quantity", "Mark", "Value", "Entry Price", "Liq. Price", "Funding", "uPnL", "rPnL"]
    rows: List[List[str]] = []

    for p in positions:
        qty = _first_float(p, ["qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"])
        if qty is None:
            qty = _nested_float(p, ["position_info", "qty"])
        mark = _nested_float(p, ["price_info", "price"])
        if mark is None:
            mark = _first_float(p, ["mark", "mark_price", "markPrice", "mark_px"])
        value = _first_float(p, ["value", "position_value", "notional", "notional_value", "usd_value"])
        if value is not None:
            value = abs(float(value))
        entry = _first_float(p, ["avg_entry_price", "entry_price", "average_entry_price", "entryPrice", "avgEntryPrice"])
        if entry is None:
            entry = _nested_float(p, ["position_info", "avg_entry_price"])
        liq = _first_float(
            p,
            [
                "estimated_liquidation_price",
                "liq_price",
                "liquidation_price",
                "liquidationPrice",
                "liquidation_px",
            ],
        )
        if liq is None:
            liq = _nested_float(p, ["position_info", "estimated_liquidation_price"])
        funding = _first_float(p, ["cum_funding", "funding", "funding_pnl", "fundingPnl"])
        upnl = _first_float(p, ["unrealized_pnl", "u_pnl", "upnl", "unrealizedPnl"])
        rpnl = _first_float(p, ["realized_pnl", "r_pnl", "rpnl", "realizedPnl"])

        rows.append(
            [
                _instrument_label(p),
                _fmt_num(qty, decimals=4),
                _fmt_usd(mark, decimals=4) if mark is not None else "-",
                _fmt_usd(value, decimals=2),
                _fmt_usd(entry, decimals=4) if entry is not None else "-",
                _fmt_usd(liq, decimals=4) if liq is not None else "-",
                _fmt_usd(funding, decimals=2),
                _fmt_usd(upnl, decimals=2),
                _fmt_usd(rpnl, decimals=2),
            ]
        )

    # column widths
    widths = [len(c) for c in cols]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def line(parts: List[str]) -> str:
        return "  ".join(parts[i].ljust(widths[i]) for i in range(len(parts)))

    print(line(cols))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch positions and print a UI-style table.")
    ap.add_argument("--json", action="store_true", help="Print raw JSON instead of a table.")
    args = ap.parse_args()

    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    raw = ep.get_positions()

    if args.json:
        out: Dict[str, Any] = {
            "ts": time.time(),
            "base_url": cfg.base_url,
            "wallet": cfg.wallet_address,
            "positions_count": len(raw) if isinstance(raw, list) else None,
            "positions": raw,
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    positions_list: Any = raw
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        positions_list = raw.get("positions")
    if not isinstance(positions_list, list):
        print("(0) Positions detected")
        print("Instrument  Quantity  Mark  Value  Entry Price  Liq. Price  Funding  uPnL  rPnL")
        print("No positions (or unexpected response shape). Use --json to inspect raw output.")
        return 0

    positions = [p for p in positions_list if isinstance(p, dict)]
    print(f"({len(positions)}) Positions detected")
    _print_positions_table(positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

