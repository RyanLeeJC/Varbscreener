#!/usr/bin/env python3
"""
Hyperparameter sweep for the grid re-arm simulator logic (matches grid_rearm_sim.html).

Fixed: 1000 USDC, 10x leverage, Grid reset ON.
Sweep: grid count 10–100 step 10 (even tens only), band % 0.5–10.0 step 0.1.
Scores each permutation per ticker with 50% weight on volume rank and 50% on total PnL rank
(min–max → 0–50 each), combined 0–100.

Outputs:
  - CSV of all results + summary JSON
  - One HTML per ticker with best settings + embedded CSV (open file = preset loaded)

Usage:
  python scripts/grid_rearm_hyperparam.py \\
    --template /Users/ryanlee/Documents/Dev/grid_rearm_sim.html \\
    --out-dir ./grid_hyperparam_out
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOTAL_TICKS = 200
EPS = 1e-6  # match grid_rearm_sim.html findOpen tolerance


@dataclass
class SimConfig:
    grid_num: int
    band_pct: float
    investment_usd: float = 1000.0
    leverage: float = 10.0
    grid_reset: bool = True


def parse_csv_prices_times(path: Path) -> tuple[list[float], list[str]]:
    """Match browser parseCsv: header time+close, or two-column no-header."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path}: need >= 2 rows")

    header_cells = [c.strip().lower() for c in lines[0].split(",")]
    time_idx = header_cells.index("time") if "time" in header_cells else -1
    close_idx = header_cells.index("close") if "close" in header_cells else -1
    if close_idx < 0:
        close_idx = header_cells.index("price") if "price" in header_cells else -1

    prices: list[float] = []
    times: list[str] = []

    if time_idx >= 0 and close_idx >= 0:
        for i in range(1, len(lines)):
            cells = lines[i].split(",")
            if len(cells) <= max(time_idx, close_idx):
                continue
            t = cells[time_idx].strip()
            p = float(cells[close_idx])
            if not math.isfinite(p) or p <= 0:
                continue
            times.append(t)
            prices.append(p)
    else:
        # no header: col0 time, col1 close
        for ln in lines:
            cells = ln.split(",")
            if len(cells) < 2:
                continue
            p = float(cells[1])
            if not math.isfinite(p) or p <= 0:
                continue
            times.append(cells[0].strip())
            prices.append(p)

    if len(prices) < 2:
        raise ValueError(f"{path}: fewer than 2 valid price rows")
    return prices, times


def resample_prices_to_ticks(prices: list[float], n: int = TOTAL_TICKS + 1) -> list[float]:
    """Same as JS custom branch: linear interpolation in index space to N ticks."""
    src_n = len(prices)
    if src_n == 1:
        return [prices[0]] * n
    out: list[float] = []
    for t in range(n):
        f = (t / (n - 1)) * (src_n - 1)
        i = int(math.floor(f))
        frac = f - i
        a = prices[i]
        b = prices[i + 1] if i + 1 < src_n else prices[src_n - 1]
        out.append(a + (b - a) * frac)
    return out


def derive_params(anchor: float, cfg: SimConfig) -> dict[str, Any]:
    band_fraction = cfg.band_pct / 100.0
    grid_lower = anchor * (1 - band_fraction)
    grid_upper = anchor * (1 + band_fraction)
    total_range = grid_upper - grid_lower
    spacing = total_range / cfg.grid_num
    notional_per_grid = (cfg.investment_usd * cfg.leverage) / cfg.grid_num
    qty_per_grid = notional_per_grid / anchor
    half_count = cfg.grid_num / 2.0
    max_i = int(half_count)  # JS: for (i=1; i<=halfCount; i++) with float halfCount
    levels: list[dict[str, Any]] = []
    for i in range(1, max_i + 1):
        levels.append({"level": anchor - i * spacing, "side": "buy"})
        levels.append({"level": anchor + i * spacing, "side": "sell"})
    return {
        "grid_lower": grid_lower,
        "grid_upper": grid_upper,
        "spacing": spacing,
        "qty_per_grid": qty_per_grid,
        "levels": levels,
        "half_count": half_count,
    }


def simulate(
    tick_prices: list[float],
    anchor: float,
    cfg: SimConfig,
) -> dict[str, Any]:
    """Port of grid_rearm_sim.html simulate('custom', cfg); tick_prices length TOTAL_TICKS+1."""
    params = derive_params(anchor, cfg)
    n = len(tick_prices)

    def new_order(
        oid: str,
        level: float,
        side: str,
        origin: str,
        paired_from: str | None,
        placed_at: int,
    ) -> dict[str, Any]:
        return {
            "id": oid,
            "level": level,
            "side": side,
            "status": "open",
            "origin": origin,
            "paired_from": paired_from,
            "fill_price": None,
            "filled_at_tick": None,
            "placed_at_tick": placed_at,
            "cancelled_at_tick": None,
        }

    orders: list[dict[str, Any]] = []
    for i, o in enumerate(params["levels"]):
        orders.append(
            new_order(f"o{i}", o["level"], o["side"], "initial", None, 0)
        )
    next_id = len(orders)

    inventory = 0.0
    inventory_cost = 0.0
    realized_pnl = 0.0
    volume_usd = 0.0
    reset_count = 0
    current_anchor = anchor
    current_grid_lower = params["grid_lower"]
    current_grid_upper = params["grid_upper"]
    spacing = params["spacing"]
    q = params["qty_per_grid"]

    def find_open(level: float, side: str) -> dict[str, Any] | None:
        for o in orders:
            if (
                abs(o["level"] - level) < EPS
                and o["side"] == side
                and o["status"] == "open"
            ):
                return o
        return None

    def unrealized_pnl(price: float) -> float:
        if abs(inventory) < 1e-12:
            return 0.0
        avg = inventory_cost / inventory
        return inventory * (price - avg)

    for t in range(1, n):
        p_prev = tick_prices[t - 1]
        p_now = tick_prices[t]
        lo = min(p_prev, p_now)
        hi = max(p_prev, p_now)
        if p_now > p_prev:
            direction = "up"
        elif p_now < p_prev:
            direction = "down"
        else:
            direction = "flat"

        candidates = [
            o
            for o in orders
            if o["status"] == "open"
            and o["level"] >= lo - 1e-6
            and o["level"] <= hi + 1e-6
            and (
                (o["side"] == "sell" and direction == "up")
                or (o["side"] == "buy" and direction == "down")
            )
        ]
        candidates.sort(key=lambda o: abs(o["level"] - p_prev))

        for ord_ in candidates:
            ord_["status"] = "filled"
            ord_["filled_at_tick"] = t
            ord_["fill_price"] = ord_["level"]
            trade_qty = q if ord_["side"] == "buy" else -q
            new_inventory = inventory + trade_qty

            if ord_["paired_from"] is not None:
                realized_pnl += spacing * q

            if inventory == 0:
                inventory_cost = trade_qty * ord_["level"]
            elif (trade_qty > 0) == (inventory > 0):
                inventory_cost += trade_qty * ord_["level"]
            else:
                if abs(trade_qty) <= abs(inventory):
                    close_frac = abs(trade_qty) / abs(inventory)
                    inventory_cost -= inventory_cost * close_frac
                else:
                    inventory_cost = new_inventory * ord_["level"]

            inventory = new_inventory
            if abs(inventory) < 1e-10:
                inventory = 0.0
                inventory_cost = 0.0

            volume_usd += q * ord_["level"]

            new_level = (
                ord_["level"] - spacing
                if ord_["side"] == "sell"
                else ord_["level"] + spacing
            )
            new_side = "buy" if ord_["side"] == "sell" else "sell"
            conflict = find_open(new_level, new_side)
            in_range = (
                current_grid_lower - 1e-6
                <= new_level
                <= current_grid_upper + 1e-6
            )
            if not conflict and in_range:
                oid = f"o{next_id}"
                next_id += 1
                orders.append(
                    new_order(
                        oid,
                        new_level,
                        new_side,
                        "rearm",
                        ord_["id"],
                        t,
                    )
                )

        # Grid reset (same order as fills: after processing fills this tick)
        if cfg.grid_reset and (
            p_now > current_grid_upper + 1e-6 or p_now < current_grid_lower - 1e-6
        ):
            breach_up = p_now > current_grid_upper
            new_anchor = current_grid_upper if breach_up else current_grid_lower
            reset_count += 1
            for o in orders:
                if o["status"] == "open":
                    o["status"] = "cancelled"
                    o["cancelled_at_tick"] = t
            # Match JS: halfCount = cfg.gridNum / 2 (float). Bounds use float; loops use i<=halfCount → int floors.
            half_count_f = cfg.grid_num / 2.0
            max_i = int(half_count_f)
            current_anchor = new_anchor
            current_grid_lower = new_anchor - half_count_f * spacing
            current_grid_upper = new_anchor + half_count_f * spacing
            for i in range(1, max_i + 1):
                buy_level = new_anchor - i * spacing
                sell_level = new_anchor + i * spacing
                orders.append(
                    new_order(f"o{next_id}", buy_level, "buy", "reset", None, t)
                )
                next_id += 1
                orders.append(
                    new_order(f"o{next_id}", sell_level, "sell", "reset", None, t)
                )
                next_id += 1

    final_price = tick_prices[-1]
    upnl = unrealized_pnl(final_price)
    total_pnl = realized_pnl + upnl
    return {
        "volume_usd": volume_usd,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": upnl,
        "total_pnl": total_pnl,
        "reset_count": reset_count,
        "final_inventory": inventory,
    }


def min_max_score(x: float, lo: float, hi: float, out_max: float = 50.0) -> float:
    if hi <= lo + 1e-18:
        return out_max / 2.0
    return out_max * (x - lo) / (hi - lo)


def ticker_from_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"\.[^.]+$", "", base, flags=re.I)
    pat = re.compile(
        r"(?:^|[^A-Za-z0-9])([A-Za-z0-9]+)(USDT|USDC)(?=[^A-Za-z0-9]|$)", re.I
    )
    matches = list(pat.finditer(base))
    if not matches:
        return "BTC"
    sym = matches[-1].group(1)
    return sym.upper() if sym else "BTC"


def sweep(
    tick_prices: list[float],
    anchor: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grid_nums = list(range(10, 101, 10))
    bands = [round(0.5 + 0.1 * i, 10) for i in range(96)]  # 0.5 .. 10.0
    for g in grid_nums:
        for b in bands:
            cfg = SimConfig(grid_num=g, band_pct=b)
            r = simulate(tick_prices, anchor, cfg)
            rows.append(
                {
                    "grid_num": g,
                    "band_pct": b,
                    "volume_usd": r["volume_usd"],
                    "total_pnl": r["total_pnl"],
                    "realized_pnl": r["realized_pnl"],
                    "unrealized_pnl": r["unrealized_pnl"],
                    "resets": r["reset_count"],
                }
            )
    return rows


def score_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vols = [r["volume_usd"] for r in rows]
    pnls = [r["total_pnl"] for r in rows]
    v_min, v_max = min(vols), max(vols)
    p_min, p_max = min(pnls), max(pnls)
    out = []
    for r in rows:
        sv = min_max_score(r["volume_usd"], v_min, v_max, 50.0)
        sp = min_max_score(r["total_pnl"], p_min, p_max, 50.0)
        out.append(
            {
                **r,
                "score_volume_50": sv,
                "score_pnl_50": sp,
                "score_total_100": sv + sp,
            }
        )
    return out


def write_results_csv(path: Path, ticker: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = [
        "ticker",
        "grid_num",
        "band_pct",
        "volume_usd",
        "total_pnl",
        "realized_pnl",
        "unrealized_pnl",
        "resets",
        "score_volume_50",
        "score_pnl_50",
        "score_total_100",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({"ticker": ticker, **{k: r[k] for k in keys if k != "ticker"}})


def build_embedded_json(
    ticker: str,
    csv_name: str,
    prices: list[float],
    times: list[str],
    best_grid: int,
    best_band: float,
) -> str:
    payload = {
        "ticker": ticker,
        "csvName": csv_name,
        "gridNum": best_grid,
        "bandPct": best_band,
        "prices": prices,
        "times": times,
    }
    return json.dumps(payload, separators=(",", ":"))


BOOT_OLD = """// Boot
updateConfigDisplay();
recompute();
render();
"""

BOOT_NEW = """function applyEmbeddedHyperparamPreset() {
  const el = document.getElementById('embedded-hyperparam-preset');
  if (!el || !el.textContent.trim()) return false;
  try {
    const j = JSON.parse(el.textContent);
    CFG.gridNum = j.gridNum;
    CFG.bandPct = j.bandPct;
    document.getElementById('cfg-gridnum').value = String(j.gridNum);
    document.getElementById('cfg-band').value = String(j.bandPct);
    DISPLAY_TICKER = j.ticker || 'BTC';
    CUSTOM_DATA = {
      prices: j.prices,
      times: j.times,
      name: j.csvName || 'embedded',
      displayTicker: DISPLAY_TICKER
    };
    ANCHOR = j.prices[0];
    currentScenario = 'custom';
    document.querySelectorAll('[data-scenario]').forEach(b => b.classList.remove('active'));
    const cbtn = document.getElementById('btn-scenario-custom');
    cbtn.classList.add('active');
    cbtn.style.display = '';
    csvStatus.textContent = (j.csvName || 'embedded') + ' — hyperparam preset';
    csvStatus.className = 'csv-status active';
    csvLoadBtn.classList.add('loaded');
    csvLoadBtn.textContent = 'Replace';
    csvClearBtn.style.display = '';
    return true;
  } catch (e) {
    console.error('embedded-hyperparam-preset', e);
    return false;
  }
}
// Boot
applyEmbeddedHyperparamPreset();
updateConfigDisplay();
recompute();
render();
"""


def _fmt_grid_half_label(grid_num: int) -> str:
    h = grid_num / 2.0
    if abs(h - round(h)) < 1e-9:
        hs = str(int(round(h)))
    else:
        hs = f"{h:.1f}".rstrip("0").rstrip(".")
    return f"{grid_num} <span class=\"sub\">/ {hs}B {hs}S</span>"


def write_preset_html(
    template: str,
    out_path: Path,
    embedded_json: str,
    grid_num: int,
    band_pct: float,
) -> None:
    html = template
    if BOOT_OLD not in html:
        raise ValueError("Template missing expected // Boot block — update BOOT_OLD pattern")
    html = html.replace(BOOT_OLD, BOOT_NEW, 1)
    # Allow band sweep up to 10% in the UI
    html = re.sub(
        r'(<input[^>]*id="cfg-band"[^>]*max=")5\.0(")',
        r"\g<1>10\2",
        html,
        count=1,
    )
    # Match hyperparam grid counts (e.g. 95); template often uses step="2"
    html = re.sub(
        r'<input type="range" id="cfg-gridnum"[^>]*>',
        f'<input type="range" id="cfg-gridnum" min="10" max="100" step="10" value="{grid_num}">',
        html,
        count=1,
    )
    band_s = f"{band_pct:.1f}".rstrip("0").rstrip(".")
    html = re.sub(
        r'<input type="range" id="cfg-band"[^>]*>',
        f'<input type="range" id="cfg-band" min="0.5" max="10" step="0.1" value="{band_s}">',
        html,
        count=1,
    )
    html = re.sub(
        r'<span class="val" id="val-gridnum">.*</span>',
        f'<span class="val" id="val-gridnum">{_fmt_grid_half_label(grid_num)}</span>',
        html,
        count=1,
    )
    html = re.sub(
        r'<span class="val" id="val-band">.*</span>',
        f'<span class="val" id="val-band">{band_s}% <span class="sub">±</span></span>',
        html,
        count=1,
    )
    inject = f'<script type="application/json" id="embedded-hyperparam-preset">{embedded_json}</script>\n'
    html = html.replace("<body>", "<body>\n" + inject, 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Grid sim hyperparam sweep (volume + PnL score)")
    ap.add_argument(
        "--btc-csv",
        type=Path,
        default=Path("/Users/ryanlee/Downloads/BINANCE_BTCUSDT, 15.csv"),
    )
    ap.add_argument(
        "--eth-csv",
        type=Path,
        default=Path("/Users/ryanlee/Downloads/BINANCE_ETHUSDT, 15.csv"),
    )
    ap.add_argument(
        "--template",
        type=Path,
        default=Path("/Users/ryanlee/Documents/Dev/grid_rearm_sim.html"),
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "grid_hyperparam_out",
    )
    args = ap.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    template_path: Path = args.template
    if not template_path.is_file():
        raise SystemExit(f"Template not found: {template_path}")

    template = template_path.read_text(encoding="utf-8")

    datasets: list[tuple[str, Path, list[float], list[str], list[float]]] = []
    for p in (args.btc_csv, args.eth_csv):
        if not p.is_file():
            raise SystemExit(f"CSV not found: {p}")
        prices, times = parse_csv_prices_times(p)
        ticker = ticker_from_filename(p.name)
        tick_prices = resample_prices_to_ticks(prices, TOTAL_TICKS + 1)
        datasets.append((ticker, p, prices, times, tick_prices))

    summary: dict[str, Any] = {"tickers": {}}

    for ticker, csv_path, prices, times, tick_prices in datasets:
        anchor = prices[0]
        raw_rows = sweep(tick_prices, anchor)
        scored = score_rows(raw_rows)
        scored.sort(key=lambda r: r["score_total_100"], reverse=True)
        best = scored[0]

        summary["tickers"][ticker] = {
            "csv": str(csv_path),
            "anchor_first_close": anchor,
            "permutations": len(scored),
            "best": {
                "grid_num": best["grid_num"],
                "band_pct": best["band_pct"],
                "volume_usd": best["volume_usd"],
                "total_pnl": best["total_pnl"],
                "score_volume_50": best["score_volume_50"],
                "score_pnl_50": best["score_pnl_50"],
                "score_total_100": best["score_total_100"],
            },
        }

        csv_out = out_dir / f"hyperparam_results_{ticker}.csv"
        write_results_csv(csv_out, ticker, scored)

        embedded = build_embedded_json(
            ticker,
            csv_path.name,
            prices,
            times,
            best["grid_num"],
            best["band_pct"],
        )
        html_out = out_dir / f"grid_rearm_sim_best_{ticker}.html"
        write_preset_html(template, html_out, embedded, best["grid_num"], best["band_pct"])

        print(f"\n=== {ticker} ({csv_path.name}) ===")
        print(f"  permutations: {len(scored)}")
        print(
            f"  BEST  grid={best['grid_num']}  band={best['band_pct']}%  "
            f"volume=${best['volume_usd']:,.2f}  total_pnl=${best['total_pnl']:,.4f}"
        )
        print(
            f"  SCORE vol_50={best['score_volume_50']:.2f}  pnl_50={best['score_pnl_50']:.2f}  "
            f"total={best['score_total_100']:.2f}/100"
        )
        print(f"  wrote {csv_out}")
        print(f"  wrote {html_out}")

    summary_path = out_dir / "hyperparam_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSummary: {summary_path}")


if __name__ == "__main__":
    main()
