from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from grid_limits_reconcile import fetch_pending_order_rows_paginated  # noqa: E402
from pending_limit_cancel import (  # noqa: E402
    cancel_ban_buffer_s,
    cancel_limit_rows,
    cancel_max_retries,
    cancel_passes,
    cancel_sleep_between_s,
    order_row_underlying,
    row_rfq_id,
)
from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints


def _default_page_limit() -> int:
    raw = (os.environ.get("CANCEL_ALL_PAGE_LIMIT") or "100").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 100


def _default_max_pages() -> int:
    raw = (os.environ.get("CANCEL_ALL_MAX_PAGES") or "25").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 25


def _row_status(row: Dict[str, Any]) -> str:
    return str(row.get("status") or row.get("order_status") or row.get("state") or "").strip().lower()


def _row_order_type(row: Dict[str, Any]) -> str:
    return str(row.get("order_type") or row.get("type") or row.get("kind") or "").strip().lower()


def _is_terminal_status(st: str) -> bool:
    return st in ("filled", "cancelled", "canceled", "rejected", "failed", "done", "closed", "cleared")


def _is_limit_row(row: Dict[str, Any]) -> bool:
    ot = _row_order_type(row)
    return "limit" in ot if ot else False


def _fetch_pending_rows(
    ep: VariEndpoints,
    *,
    instrument: Optional[str],
    page_limit: int,
    max_pages: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    return fetch_pending_order_rows_paginated(
        ep,
        instrument=instrument,
        page_limit=page_limit,
        max_pages=max_pages,
    )


def _pending_limit_rows_with_rfq(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_limit_row(row):
            continue
        if _is_terminal_status(_row_status(row)):
            continue
        if row_rfq_id(row) is None:
            continue
        out.append(row)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "List or cancel all pending limit orders (paginated GET /api/orders/v2?status=pending, "
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
        default=None,
        help="Extra seconds added to API wait_until_seconds on 418 (default 0.75).",
    )
    p.add_argument(
        "--passes",
        type=int,
        default=None,
        help="How many full passes over remaining failures (default 2).",
    )
    p.add_argument(
        "--page-limit",
        type=int,
        default=None,
        help="Orders per GET page (default 100, or CANCEL_ALL_PAGE_LIMIT).",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "Max paginated GET pages to scan (default 25, or CANCEL_ALL_MAX_PAGES). "
            "Omni returns ~100 rows on an unpaginated request; use this to fetch all pages."
        ),
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print raw GET rows and each cancel response as JSON.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress cancel progress log lines.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config()
    sleep_between = (
        float(args.sleep_between_s) if args.sleep_between_s is not None else cancel_sleep_between_s()
    )
    max_retries = int(args.max_retries) if args.max_retries is not None else cancel_max_retries()
    passes = max(1, int(args.passes)) if args.passes is not None else cancel_passes()
    ban_buffer = float(args.ban_buffer_s) if args.ban_buffer_s is not None else cancel_ban_buffer_s()
    page_limit = int(args.page_limit) if args.page_limit is not None else _default_page_limit()
    max_pages = int(args.max_pages) if args.max_pages is not None else _default_max_pages()

    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    try:
        rows, hit_cap = _fetch_pending_rows(
            ep,
            instrument=args.instrument,
            page_limit=page_limit,
            max_pages=max_pages,
        )
    except Exception as e:
        print(f"ERROR: GET pending orders failed: {e}", file=sys.stderr)
        return 1

    if hit_cap:
        print(
            f"WARNING: hit --max-pages={max_pages} cap while scanning pending orders "
            f"(page_limit={page_limit}). Raise CANCEL_ALL_MAX_PAGES or --max-pages and re-run.",
            file=sys.stderr,
        )

    targets = _pending_limit_rows_with_rfq(rows)
    if args.print_json:
        print(json.dumps({"pending_limit_with_rfq": targets}, indent=2, default=str))
    else:
        print(
            f"Found {len(targets)} pending limit order(s) "
            f"({len(rows)} pending row(s) across up to {max_pages} page(s), limit={page_limit})."
        )
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
                    order_row_underlying(row) or "-",
                    row_rfq_id(row) or "-",
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

    log_fn = None if args.quiet else lambda m: print(m, file=sys.stderr)
    ok, err_n = cancel_limit_rows(
        ep,
        targets,
        log=log_fn,
        passes=passes,
        sleep_between=sleep_between,
        ban_buffer=ban_buffer,
        max_retries=max_retries,
    )
    if not args.print_json:
        print(f"Canceled {ok} pending limit order(s).")
    if err_n:
        print(f"Finished with {err_n}/{len(targets)} error(s).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
