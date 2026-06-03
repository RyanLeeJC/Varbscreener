#!/usr/bin/env python3
"""
Fetch Binance USD-M 5m klines (default 3d) for BTC/ETH and grid-book tickers;
estimate per-ticker beta vs BTC and ETH; print beta-weighted hedge sizing.

Maintainer only — live hedge legs are manual or a future Varibot hook.

Example (signed qty × mark from positions export or API):

  python3 scripts/btc_eth_hedge_beta.py \\
    --position XPL:-53504:0.09334 --position FET:-13810:0.2674 \\
    --net-threshold-usd 500 --beta-threshold-usd 2000

Position format: TICKER:SIGNED_QTY:MARK (qty negative = short).
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

INTERVAL = "5m"
INTERVAL_MS = 5 * 60 * 1000
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "GridbotData" / "btc_eth_hedge_klines_3d.json"

# Keep in sync with strategy/gridstrat.py GRID_TRADING_TICKERS keys
DEFAULT_BOOK = (
    "AVAX", "FET", "ICP", "JUP", "RENDER", "LINK", "NEAR", "ONDO", "PENGU",
    "SEI", "SUI", "TAO", "VIRTUAL", "WLFI", "XPL",
)


def _fetch_klines(symbol: str, start_ms: int, end_ms: int) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    cur = start_ms
    while cur < end_ms:
        qs = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": INTERVAL,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1500,
            }
        )
        with urllib.request.urlopen(
            f"https://fapi.binance.com/fapi/v1/klines?{qs}", timeout=30
        ) as resp:
            data = json.load(resp)
        if not data:
            break
        for row in data:
            out.append((int(row[0]), float(row[4])))
        cur = int(data[-1][0]) + INTERVAL_MS
        if len(data) < 1500:
            break
        time.sleep(0.04)
    return out


def _binance_syms() -> Dict[str, str]:
    with urllib.request.urlopen(
        "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=30
    ) as resp:
        info = json.load(resp)
    return {
        s["symbol"]: s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
    }


def _map_ticker(ticker: str, valid: Dict[str, str]) -> Optional[str]:
    t = ticker.upper()
    for sym in (f"{t}USDT", f"1000{t}USDT"):
        if sym in valid:
            return sym
    return None


def _log_returns(series: Dict[int, float], ts: List[int]) -> np.ndarray:
    p = np.array([series[t] for t in ts], dtype=float)
    return np.diff(np.log(p))


def _beta(r_alt: np.ndarray, r_ref: np.ndarray) -> float:
    v = float(np.var(r_ref, ddof=1))
    if v <= 0:
        return float("nan")
    c = np.cov(r_alt, r_ref, ddof=1)
    return float(c[0, 1] / v)


def _parse_position(raw: str) -> Tuple[str, float, float]:
    parts = raw.strip().split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"expected TICKER:SIGNED_QTY:MARK, got {raw!r}"
        )
    return parts[0].upper(), float(parts[1]), float(parts[2])


def compute_betas(
    book: Tuple[str, ...], *, days: int
) -> Tuple[Dict[str, dict], Dict[int, float], Dict[int, float]]:
    valid = _binance_syms()
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    btc = dict(_fetch_klines("BTCUSDT", start_ms, end_ms))
    eth = dict(_fetch_klines("ETHUSDT", start_ms, end_ms))
    common = sorted(set(btc) & set(eth))
    r_btc = _log_returns(btc, common)
    r_eth = _log_returns(eth, common)

    betas: Dict[str, dict] = {}
    for t in book:
        sym = _map_ticker(t, valid)
        if not sym:
            continue
        alt = dict(_fetch_klines(sym, start_ms, end_ms))
        ts = [x for x in common if x in alt]
        if len(ts) < 100:
            continue
        r_alt = _log_returns(alt, ts)
        rb = _log_returns(btc, ts)
        re = _log_returns(eth, ts)
        m = min(len(r_alt), len(rb), len(re))
        r_alt, rb, re = r_alt[:m], rb[:m], re[:m]
        betas[t] = {
            "binance": sym,
            "beta_btc": _beta(r_alt, rb),
            "beta_eth": _beta(r_alt, re),
            "corr_btc": float(np.corrcoef(r_alt, rb)[0, 1]),
        }
        time.sleep(0.04)
    return betas, btc, eth


def hedge_plan(
    positions: List[Tuple[str, float, float]],
    betas: Dict[str, dict],
    *,
    net_threshold_usd: float,
    beta_threshold_usd: float,
    hedge_frac: float,
) -> dict:
    rows = []
    net = 0.0
    beta_btc = 0.0
    beta_eth = 0.0
    for ticker, qty, mark in positions:
        n = qty * mark
        b_btc = betas.get(ticker, {}).get("beta_btc", 1.5)
        b_eth = betas.get(ticker, {}).get("beta_eth", 1.2)
        net += n
        beta_btc += b_btc * n
        beta_eth += b_eth * n
        rows.append(
            {
                "ticker": ticker,
                "signed_notional": n,
                "beta_btc": b_btc,
                "contrib_beta_btc": b_btc * n,
            }
        )

    excess_net = net
    if abs(net) > net_threshold_usd:
        excess_net = net - math.copysign(net_threshold_usd, net)
    else:
        excess_net = 0.0

    excess_beta_btc = beta_btc
    if abs(beta_btc) > beta_threshold_usd:
        excess_beta_btc = beta_btc - math.copysign(beta_threshold_usd, beta_btc)
    else:
        excess_beta_btc = 0.0

    # Beta-neutral vs BTC: long BTC when book is beta-short BTC
    h_btc_full = -beta_btc
    h_btc_thresh = -excess_beta_btc * hedge_frac
    # Dollar-neutral (ignores beta): long when net short
    h_btc_dollar = -excess_net * hedge_frac

    eth_per_btc = 1.0
    if betas:
        eth_per_btc = float(
            np.mean([b.get("beta_eth", 1.2) / max(b.get("beta_btc", 1.5), 0.01) for b in betas.values()])
        )
    # Split hedge 70/30 BTC/ETH by notional (ETH has ~1.09 beta to BTC in sample)
    w_btc, w_eth = 0.7, 0.3
    h_btc_split = h_btc_thresh * w_btc
    h_eth_split = (h_btc_thresh / max(eth_per_btc, 0.5)) * w_eth

    return {
        "net_notional_usd": net,
        "beta_weighted_btc_usd": beta_btc,
        "beta_weighted_eth_usd": beta_eth,
        "hedge_btc_full_beta_neutral": h_btc_full,
        "hedge_btc_after_threshold": h_btc_thresh,
        "hedge_btc_dollar_after_threshold": h_btc_dollar,
        "hedge_btc_split_70pct": h_btc_split,
        "hedge_eth_split_30pct": h_eth_split,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--position",
        action="append",
        default=[],
        metavar="TICKER:QTY:MARK",
        help="Signed qty (negative=short); repeat",
    )
    ap.add_argument("--net-threshold-usd", type=float, default=500.0)
    ap.add_argument("--beta-threshold-usd", type=float, default=2000.0)
    ap.add_argument(
        "--hedge-frac",
        type=float,
        default=1.0,
        help="Fraction of excess beta exposure to hedge (0–1)",
    )
    args = ap.parse_args()

    betas, btc, eth = compute_betas(DEFAULT_BOOK, days=args.days)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "interval": INTERVAL,
        "n_bars": {"BTC": len(btc), "ETH": len(eth)},
        "ticker_betas": betas,
        "klines": {
            "BTCUSDT": [{"t": t, "c": c} for t, c in sorted(btc.items())],
            "ETHUSDT": [{"t": t, "c": c} for t, c in sorted(eth.items())],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"Wrote {args.out} ({args.out.stat().st_size // 1024} KB)")

    if args.position:
        pos = [_parse_position(p) for p in args.position]
        plan = hedge_plan(
            pos,
            betas,
            net_threshold_usd=args.net_threshold_usd,
            beta_threshold_usd=args.beta_threshold_usd,
            hedge_frac=args.hedge_frac,
        )
        print(json.dumps(plan, indent=2))
    else:
        avg_b = float(np.mean([v["beta_btc"] for v in betas.values()]))
        print(f"Tickers={len(betas)} equal-weight avg beta_btc={avg_b:.2f}")
        print("Pass --position TICKER:SIGNED_QTY:MARK for hedge sizing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
