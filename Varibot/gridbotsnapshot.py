from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints

from exports import (
    _iso_z,
    _parse_iso,
    build_export_payload,
    create_export,
    download_export_file,
    filter_csv_by_max_age,
    filter_csv_since,
    parse_since_sgt,
    poll_export,
)


def _instrument_label(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    pos_info = p.get("position_info")
    if isinstance(pos_info, dict):
        inst2 = pos_info.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()
    for k in ("underlying", "instrument_name", "symbol"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return "UNKNOWN"


def _upnl_of(p: Dict[str, Any]) -> float:
    for k in ("upnl", "unrealized_pnl", "u_pnl", "unrealizedPnl"):
        if k in p and p[k] is not None:
            return float(p[k])
    return 0.0


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_export_csv(
    client: VariClient,
    *,
    resource: str,
    gte_dt: datetime,
    lte_dt: datetime,
    since_sgt: Optional[str] = None,
    max_age_hours: Optional[float] = None,
    timeout_s: float = 120.0,
) -> List[Dict[str, str]]:
    transfer_types = ["realized_pnl"] if resource == "transfers" else None
    payload = build_export_payload(
        resource=resource,
        created_at_gte=_iso_z(gte_dt),
        created_at_lte=_iso_z(lte_dt),
        transfer_types=transfer_types,
    )
    created = create_export(client, payload)
    export_id = str(created.get("id") or "").strip()
    if not export_id:
        raise RuntimeError(f"POST /api/exports missing id: {created}")

    completed = poll_export(client, export_id, timeout_s=timeout_s)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        download_export_file(client, export_id=export_id, export=completed, out_path=tmp_path)
        if since_sgt:
            filter_csv_since(tmp_path, cutoff=parse_since_sgt(since_sgt, default_date=gte_dt))
        if max_age_hours is not None and max_age_hours > 0:
            filter_csv_by_max_age(tmp_path, max_age_hours=float(max_age_hours))
        return _read_csv_rows(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def aggregate_rpnl(rows: List[Dict[str, str]]) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for row in rows:
        t = (row.get("underlying") or "").strip().upper()
        if not t:
            continue
        out[t] += float(row.get("qty") or 0)
    return dict(out)


def aggregate_volume(rows: List[Dict[str, str]]) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for row in rows:
        t = (row.get("underlying") or "").strip().upper()
        if not t:
            continue
        out[t] += abs(float(row.get("price") or 0) * float(row.get("qty") or 0))
    return dict(out)


def fetch_upnl_by_ticker(ep: VariEndpoints) -> Dict[str, float]:
    raw = ep.get_positions()
    positions = raw if isinstance(raw, list) else raw.get("positions", []) if isinstance(raw, dict) else []
    out: Dict[str, float] = defaultdict(float)
    for p in positions:
        if isinstance(p, dict):
            out[_instrument_label(p)] += _upnl_of(p)
    return dict(out)


def build_snapshot_rows(
    *,
    rpnl_by: Dict[str, float],
    upnl_by: Dict[str, float],
    vol_by: Dict[str, float],
) -> List[Dict[str, Any]]:
    tickers = sorted(set(rpnl_by) | set(upnl_by) | set(vol_by))
    rows: List[Dict[str, Any]] = []
    for t in tickers:
        rpnl = float(rpnl_by.get(t, 0.0))
        upnl = float(upnl_by.get(t, 0.0))
        vol = float(vol_by.get(t, 0.0))
        rows.append(
            {
                "ticker": t,
                "total_pnl": rpnl + upnl,
                "rpnl": rpnl,
                "upnl": upnl,
                "vol": vol,
            }
        )
    rows.sort(key=lambda x: (-float(x["total_pnl"]), -float(x["vol"]), str(x["ticker"])))
    return rows


def print_snapshot_table(
    rows: List[Dict[str, Any]],
    *,
    wallet: str,
    gte_dt: datetime,
    lte_dt: datetime,
) -> None:
    print(f"Wallet: {wallet}")
    print(f"Window: {_iso_z(gte_dt)} → {_iso_z(lte_dt)}")
    print(f"Snapshot: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print()
    print(f"{'Ticker':<10} {'Total PNL':>12} {'RPNL':>12} {'UPNL':>12} {'Vol':>14}")
    print("-" * 64)
    for row in rows:
        print(
            f"{row['ticker']:<10} "
            f"{row['total_pnl']:>12,.2f} "
            f"{row['rpnl']:>12,.2f} "
            f"{row['upnl']:>12,.2f} "
            f"{row['vol']:>14,.2f}"
        )
    print("-" * 64)
    print(
        f"{'TOTAL':<10} "
        f"{sum(r['total_pnl'] for r in rows):>12,.2f} "
        f"{sum(r['rpnl'] for r in rows):>12,.2f} "
        f"{sum(r['upnl'] for r in rows):>12,.2f} "
        f"{sum(r['vol'] for r in rows):>14,.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Grid bot snapshot: per-ticker Total PNL (RPNL + live uPNL) and volume."
    )
    ap.add_argument(
        "--wallet",
        default=None,
        help="Override VR_WALLET_ADDRESS (default: .env).",
    )
    ap.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Lookback window in hours (default: 24).",
    )
    ap.add_argument(
        "--gte",
        default=None,
        help="Window start ISO timestamp (overrides --hours).",
    )
    ap.add_argument(
        "--lte",
        default=None,
        help="Window end ISO timestamp (default: now UTC).",
    )
    ap.add_argument(
        "--since-sgt",
        default=None,
        metavar="TIME",
        help='Post-filter RPNL/trades rows from this SGT time (e.g. "9pm", "21:00").',
    )
    ap.add_argument(
        "--max-age-hours",
        type=float,
        default=None,
        help="Post-filter export rows older than this many hours before latest created_at.",
    )
    ap.add_argument("--timeout", type=float, default=120.0, help="Export poll timeout seconds.")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    args = ap.parse_args()

    if args.wallet:
        os.environ["VR_WALLET_ADDRESS"] = args.wallet.strip()

    cfg = load_config()
    lte_dt = _parse_iso(args.lte) if args.lte else datetime.now(timezone.utc)
    if args.gte:
        gte_dt = _parse_iso(args.gte)
    else:
        gte_dt = lte_dt - timedelta(hours=float(args.hours))

    client = VariClient(
        base_url=cfg.base_url,
        auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
    )
    ep = VariEndpoints(client)

    rpnl_rows = fetch_export_csv(
        client,
        resource="transfers",
        gte_dt=gte_dt,
        lte_dt=lte_dt,
        since_sgt=args.since_sgt,
        max_age_hours=args.max_age_hours,
        timeout_s=args.timeout,
    )
    trade_rows = fetch_export_csv(
        client,
        resource="trades",
        gte_dt=gte_dt,
        lte_dt=lte_dt,
        since_sgt=args.since_sgt,
        max_age_hours=args.max_age_hours,
        timeout_s=args.timeout,
    )
    upnl_by = fetch_upnl_by_ticker(ep)

    rows = build_snapshot_rows(
        rpnl_by=aggregate_rpnl(rpnl_rows),
        upnl_by=upnl_by,
        vol_by=aggregate_volume(trade_rows),
    )

    if args.json:
        out = {
            "wallet": cfg.wallet_address,
            "gte": _iso_z(gte_dt),
            "lte": _iso_z(lte_dt),
            "hours": (lte_dt - gte_dt).total_seconds() / 3600.0,
            "rows": rows,
            "totals": {
                "total_pnl": sum(r["total_pnl"] for r in rows),
                "rpnl": sum(r["rpnl"] for r in rows),
                "upnl": sum(r["upnl"] for r in rows),
                "vol": sum(r["vol"] for r in rows),
            },
        }
        print(json.dumps(out, indent=2))
    else:
        print_snapshot_table(rows, wallet=cfg.wallet_address, gte_dt=gte_dt, lte_dt=lte_dt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
