from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.endpoints import parse_cancel_ban_wait_seconds
from variationalbot.vari.errors import VariUnexpectedResponse


def _default_sleep_between_s() -> float:
    raw = (os.environ.get("CANCEL_ALL_SLEEP_BETWEEN_S") or "1.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.5


def _default_max_cancel_retries() -> int:
    raw = (os.environ.get("CANCEL_ALL_MAX_RETRIES") or "12").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


def _orders_v2_result_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("result", "orders", "data", "items"):
            v = raw.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _order_row_underlying(row: Dict[str, Any]) -> str:
    inst = row.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    for k in ("underlying", "asset", "symbol"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().upper()
            if s.endswith("-PERP"):
                s = s[: -len("-PERP")]
            return s
    return ""


def _row_status(row: Dict[str, Any]) -> str:
    return str(row.get("status") or row.get("order_status") or row.get("state") or "").strip().lower()


def _row_order_type(row: Dict[str, Any]) -> str:
    return str(row.get("order_type") or row.get("type") or row.get("kind") or "").strip().lower()


def _row_rfq_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("rfq_id", "rfqId"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _is_terminal_status(st: str) -> bool:
    return st in ("filled", "cancelled", "canceled", "rejected", "failed", "done", "closed", "cleared")


def _is_limit_row(row: Dict[str, Any]) -> bool:
    ot = _row_order_type(row)
    return "limit" in ot if ot else False


def _fetch_pending_rows(ep: VariEndpoints, *, instrument: Optional[str]) -> List[Dict[str, Any]]:
    path = "/api/orders/v2?status=pending"
    if instrument:
        inst = str(instrument).strip()
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}instrument={inst}"
    raw = ep.client.request_json("GET", path)
    return _orders_v2_result_items(raw)


def _pending_limit_rows_with_rfq(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_limit_row(row):
            continue
        if _is_terminal_status(_row_status(row)):
            continue
        if _row_rfq_id(row) is None:
            continue
        out.append(row)
    return out


def _cancel_one(
    ep: VariEndpoints,
    *,
    row: Dict[str, Any],
    max_attempts: int,
    buffer_s: float,
    verbose: bool,
) -> None:
    rid = _row_rfq_id(row)
    if not rid:
        raise ValueError("missing rfq_id")
    sym = _order_row_underlying(row) or "?"

    def on_wait(sleep_s: float, attempt: int, rfq: str) -> None:
        if verbose:
            print(
                f"cancel ban: wait {sleep_s:.1f}s before retry "
                f"({attempt}/{max_attempts}) {sym} rfq_id={rfq[:8]}…",
                file=sys.stderr,
            )

    ep.cancel_order_rfq_resilient(
        rfq_id=rid,
        max_attempts=max_attempts,
        buffer_s=buffer_s,
        on_wait=on_wait,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "List or cancel all pending limit orders (GET /api/orders/v2?status=pending, "
            "then POST /api/orders/cancel per rfq_id). Honors Omni cancel-ban (HTTP 418) "
            "via wait_until_seconds retries."
        )
    )
    p.add_argument(
        "--instrument",
        default=None,
        help="Optional instrument query param, e.g. P-BTC-USDC-3600 (passed to orders/v2).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually POST /api/orders/cancel for each resting limit. Without this, dry-run only.",
    )
    p.add_argument(
        "--sleep-between-s",
        type=float,
        default=None,
        help=(
            "Seconds between successful cancel calls (default 1.5, or CANCEL_ALL_SLEEP_BETWEEN_S). "
            "418 cancel-ban waits are handled separately."
        ),
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Max cancel attempts per order when temporarily banned (default 12, or CANCEL_ALL_MAX_RETRIES).",
    )
    p.add_argument(
        "--ban-buffer-s",
        type=float,
        default=0.75,
        help="Extra seconds added to API wait_until_seconds on 418 (default 0.75).",
    )
    p.add_argument(
        "--passes",
        type=int,
        default=2,
        help="How many full passes over remaining failures (default 2).",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print raw GET rows and each cancel response as JSON.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress cancel-ban wait messages on stderr.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config()
    sleep_between = (
        float(args.sleep_between_s)
        if args.sleep_between_s is not None
        else _default_sleep_between_s()
    )
    max_retries = (
        int(args.max_retries) if args.max_retries is not None else _default_max_cancel_retries()
    )
    passes = max(1, int(args.passes))
    verbose = not bool(args.quiet)

    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    try:
        rows = _fetch_pending_rows(ep, instrument=args.instrument)
    except Exception as e:
        print(f"ERROR: GET pending orders failed: {e}", file=sys.stderr)
        return 1

    targets = _pending_limit_rows_with_rfq(rows)
    if args.print_json:
        print(json.dumps({"pending_limit_with_rfq": targets}, indent=2, default=str))
    else:
        print(f"Found {len(targets)} pending limit order(s).")
        if args.live and targets:
            print(
                f"Live cancel: sleep_between={sleep_between:g}s "
                f"max_retries={max_retries} passes={passes}",
            )

    if not targets:
        return 0

    if not args.live:
        cols = ["underlying", "rfq_id", "status", "order_type", "side"]
        widths = [len(c) for c in cols]
        data: List[List[str]] = []
        for row in targets:
            data.append(
                [
                    _order_row_underlying(row) or "-",
                    _row_rfq_id(row) or "-",
                    _row_status(row) or "-",
                    _row_order_type(row) or "-",
                    str(row.get("side") or row.get("order_side") or "-"),
                ]
            )
        for r in data:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], len(cell))

        def line(parts: List[str]) -> str:
            return "  ".join(parts[i].ljust(widths[i]) for i in range(len(parts)))

        print(line(cols))
        print(line(["-" * w for w in widths]))
        for r in data:
            print(line(r))
        print("Dry-run only. Re-run with --live to POST /api/orders/cancel for each row.")
        return 0

    pending: List[Dict[str, Any]] = list(targets)
    errors: List[Tuple[str, str]] = []

    for pass_n in range(1, passes + 1):
        if pass_n > 1:
            if not errors:
                break
            if verbose:
                print(
                    f"Pass {pass_n}/{passes}: retrying {len(errors)} failed cancel(s)…",
                    file=sys.stderr,
                )
            err_ids = {rid for rid, _ in errors}
            errors = []
            pending = [r for r in pending if (_row_rfq_id(r) or "") in err_ids]
            if not pending:
                break
            time.sleep(max(sleep_between, 2.0))

        for i, row in enumerate(pending):
            rid = _row_rfq_id(row)
            if not rid:
                continue
            sym = _order_row_underlying(row) or "?"
            try:
                _cancel_one(
                    ep,
                    row=row,
                    max_attempts=max_retries,
                    buffer_s=float(args.ban_buffer_s),
                    verbose=verbose,
                )
                if args.print_json:
                    print(json.dumps({"rfq_id": rid, "canceled": True}, indent=2))
            except VariUnexpectedResponse as e:
                wait_s = parse_cancel_ban_wait_seconds(e)
                msg = f"{type(e).__name__}: {e}"
                errors.append((rid, msg))
                print(f"ERROR cancel {sym} rfq_id={rid}: {msg}", file=sys.stderr)
                if wait_s is not None and i < len(pending) - 1:
                    time.sleep(float(wait_s) + float(args.ban_buffer_s))
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                errors.append((rid, msg))
                print(f"ERROR cancel {sym} rfq_id={rid}: {msg}", file=sys.stderr)
            if i < len(pending) - 1 and sleep_between > 0:
                time.sleep(sleep_between)

    if not args.print_json:
        n_ok = len(targets) - len(errors)
        print(f"Canceled {n_ok} pending limit order(s).")
    if errors:
        print(f"Finished with {len(errors)}/{len(targets)} error(s).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
