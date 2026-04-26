from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

from variationalbot.config import load_config
from variationalbot.domain import parse_portfolio_snapshot
from variationalbot.vari import VariAuth, VariClient, VariEndpoints

# Default TP threshold (% of denominator; see _apply_tp_check).
TP_CHECK_THRESHOLD_PCT_DEFAULT: float = 0.3


def _shorten_wallet(addr: str, *, head: int = 5, tail: int = 4) -> str:
    a = (addr or "").strip()
    if len(a) <= head + tail + 3:
        return a
    return f"{a[:head]}...{a[-tail:]}"


def _build_out_dict(
    *,
    cfg: Any,
    snap: Any,
) -> Dict[str, Any]:
    im_pct = (snap.im_usage * 100.0) if snap.im_usage is not None else None
    mm_pct = (snap.mm_usage * 100.0) if snap.mm_usage is not None else None
    return {
        "ts": time.time(),
        "base_url": cfg.base_url,
        "wallet": cfg.wallet_address,
        "portfolio_value_usd": snap.portfolio_value_usd,
        "unrealized_pnl_usd": snap.unrealized_pnl_usd,
        "im_usage": snap.im_usage,
        "mm_usage": snap.mm_usage,
        "im_usage_pct": im_pct,
        "mm_usage_pct": mm_pct,
        "raw_keys": sorted(list(snap.raw.keys())) if isinstance(snap.raw, dict) else None,
    }


def _apply_tp_check(out: Dict[str, Any], *, threshold_pct: float) -> None:
    """
    Take-profit readiness: Yes when uPNL > 0 and (uPNL / denom) * 100 >= threshold_pct.

    Denominator selection:
    - If `positions_notional_usd` is present and > 0, use that (sum of open positions notional).
    - Else fall back to `portfolio_value_usd` (account/equity snapshot).
    """
    denom = out.get("positions_notional_usd")
    if denom is None:
        denom = out.get("portfolio_value_usd")
    upnl = out.get("unrealized_pnl_usd")
    ratio_pct: Optional[float] = None
    verdict = "No"
    try:
        denom_f = float(denom) if denom is not None else 0.0
        if denom_f > 0 and upnl is not None:
            upnl_f = float(upnl)
            ratio_pct = (upnl_f / denom_f) * 100.0
            if upnl_f > 0 and ratio_pct >= float(threshold_pct):
                verdict = "Yes"
    except (TypeError, ValueError):
        pass
    out["tp_check_threshold_pct"] = float(threshold_pct)
    # Keep backward-compatible key name (now "vs denom", which may be positions notional).
    out["tp_check_u_pnl_vs_portfolio_pct"] = ratio_pct
    out["tp_check_u_pnl_vs_denom_pct"] = ratio_pct
    out["tp_check"] = verdict


def _fmt_acct_usd(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{v:.2f}"


def _fmt_metric(v: Optional[float]) -> str:
    """Two decimals when non-zero; exact ``0.0`` when ~zero (matches terminal style)."""
    if v is None:
        return ""
    if abs(float(v)) < 1e-12:
        return "0.0"
    return f"{v:.2f}"


def _closeallpositions_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "closeallpositions.py")


def _maybe_invoke_closeall_on_tp_yes(
    out: Dict[str, Any],
    *,
    close_on_yes: bool,
    close_live: bool,
) -> Tuple[bool, int]:
    """
    If close_on_yes and tp_check == Yes, run closeallpositions.py in this interpreter.
    Returns (invoked, exit_code). exit_code is 0 when not invoked.
    """
    if not close_on_yes or out.get("tp_check") != "Yes":
        return False, 0
    script = _closeallpositions_script_path()
    if not os.path.isfile(script):
        print(f"tp_check=Yes but closeallpositions.py not found: {script}", file=sys.stderr)
        return True, 2
    cmd = [sys.executable, script]
    if close_live:
        cmd.append("--live")
    print(f"\ntp_check=Yes → running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=os.path.dirname(script))
    return True, int(proc.returncode)


def _fmt_tp_check_cell(out: Dict[str, Any]) -> str:
    verdict = str(out.get("tp_check") or "No")
    r = out.get("tp_check_u_pnl_vs_portfolio_pct")
    if r is None:
        return f"{verdict} (n/a)"
    return f"{verdict} ({float(r):+.2f}%)"


def _print_formatted(*, wallet: str, out: Dict[str, Any]) -> None:
    # One space after each ":"; pad before the value field so values share one right-aligned column.
    rows: list[tuple[str, str]] = [
        ("Wallet", _shorten_wallet(wallet)),
        ("Acct USD", _fmt_acct_usd(out.get("portfolio_value_usd"))),
        ("uPNL", _fmt_metric(out.get("unrealized_pnl_usd"))),
        ("TP Check", _fmt_tp_check_cell(out)),
        ("IM", _fmt_metric(out.get("im_usage"))),
        ("IM_pct", _fmt_metric(out.get("im_usage_pct"))),
        ("MM", _fmt_metric(out.get("mm_usage"))),
        ("MM_pct", _fmt_metric(out.get("mm_usage_pct"))),
    ]
    value_w = max(14, max(len(v) for _, v in rows))
    max_prefix = max(len(f"{a}: ") for a, _ in rows)
    for label, val in rows:
        prefix = f"{label}: "
        gap = max_prefix - len(prefix)
        print(f"{prefix}{' ' * gap}{val:>{value_w}}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch portfolio snapshot and print stats.")
    ap.add_argument(
        "--print-json",
        action="store_true",
        help="Emit the previous JSON blob (for scripts) instead of formatted text.",
    )
    ap.add_argument(
        "--tp-check-pct",
        type=float,
        default=TP_CHECK_THRESHOLD_PCT_DEFAULT,
        help=(
            "TP Check: Yes when uPNL>0 and uPNL/denominator >= this % "
            f"(default {TP_CHECK_THRESHOLD_PCT_DEFAULT:g})."
        ),
    )
    ap.add_argument(
        "--tp-close-on-yes",
        action="store_true",
        help="When TP Check is Yes, run closeallpositions.py (dry-run unless --tp-close-live).",
    )
    ap.add_argument(
        "--tp-close-live",
        action="store_true",
        help="With --tp-close-on-yes, pass --live to closeallpositions.py (actually close all).",
    )
    args = ap.parse_args()
    if bool(args.tp_close_live) and not bool(args.tp_close_on_yes):
        ap.error("--tp-close-live requires --tp-close-on-yes")

    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    raw = ep.get_portfolio(compute_margin=True)
    snap = parse_portfolio_snapshot(raw)
    out = _build_out_dict(cfg=cfg, snap=snap)
    _apply_tp_check(out, threshold_pct=float(args.tp_check_pct))

    if not args.print_json:
        _print_formatted(wallet=str(cfg.wallet_address), out=out)

    invoked, close_rc = _maybe_invoke_closeall_on_tp_yes(
        out,
        close_on_yes=bool(args.tp_close_on_yes),
        close_live=bool(args.tp_close_live),
    )
    if invoked:
        out["tp_closeall"] = {
            "invoked": True,
            "exit_code": close_rc,
            "live": bool(args.tp_close_live),
            "script": os.path.abspath(_closeallpositions_script_path()),
        }

    if args.print_json:
        print(json.dumps(out, indent=2))

    if invoked and close_rc != 0:
        return close_rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
