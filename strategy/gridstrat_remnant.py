"""
Remnant-based re-arm logic (New Limits Logic, May 2026).

This module defines *mark-centered* limit maintenance:

1. Read the current mark.
2. Check if there are at least N/2 sell limits above mark and N/2 buy limits below mark
   within the configured ±band around mark.
3. Post missing rungs from the mark outward (nearest first): count-fill when short on depth,
   and gap-fill toward mark when the nearest limit is more than ~1 spacing away (even if
   count is already sufficient).
4. Optionally cancel venue limits outside that band (keeps the venue book tidy).

No anchor concept is required for the reconcile decisions.

Pure functions only — no filesystem / HTTP. All venue I/O stays in grid_limits_reconcile.py.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from variationalbot.vari.endpoints import grid_limit_price_key, grid_limit_price_decimals
except ImportError:  # pragma: no cover — fallback for standalone tests

    def grid_limit_price_decimals(price: float) -> int:  # type: ignore[misc]
        p = abs(float(price))
        if not math.isfinite(p) or p <= 0:
            return 2
        if p >= 10:
            return 3
        if p >= 1:
            return 4
        if p >= 0.1:
            return 5
        return 6

    def grid_limit_price_key(price: float) -> str:  # type: ignore[misc]
        d = grid_limit_price_decimals(price)
        return f"{round(float(price), d):.{d}f}"


# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------

def half_band_fraction(*, grid_band_pct: Optional[float], lower: float, upper: float) -> float:
    """
    Configured half-band as a fraction of mark.

    Prefer ``grid_band_pct`` from meta (e.g. ``3.0`` for ±3%). When unset or invalid,
    fall back to the half-band implied by pinned ``lower`` / ``upper``.
    """
    if grid_band_pct is not None:
        try:
            pct = float(grid_band_pct)
        except (TypeError, ValueError):
            pct = 0.0
        if pct > 0:
            return pct / 100.0
    return abs((float(upper) / max(1e-12, float(lower))) - 1.0) / 2.0


def _tick_at(price: float) -> float:
    """One venue price tick at the given price level (10^-decimals)."""
    d = grid_limit_price_decimals(float(price))
    return 10.0 ** (-d)


def _price_key_float(price: float) -> float:
    """Round price to key value as float (round-trip through key)."""
    return float(grid_limit_price_key(price))


def _prices_match(a: float, b: float) -> bool:
    """
    Two prices are the same rung when:
      - same price_key string, AND
      - raw float difference < half-tick + eps.
    """
    if grid_limit_price_key(a) != grid_limit_price_key(b):
        return False
    tick = _tick_at((abs(a) + abs(b)) / 2.0)
    return abs(a - b) < 0.5 * tick + 1e-9


def _spacings_match(inferred: float, configured: float, ref_price: float) -> bool:
    """
    Spacing is acceptable when the difference is within a configurable number
    of venue ticks at the reference price level.

    Notes
    -----
    This is deliberately separate from whether we *use* the inferred spacing.
    In many cases we want to treat spacing as \"matching\" (to avoid hard resets),
    but still snap execution back to configured spacing to avoid churning limits
    due to rounding noise in remnants.
    """
    import os

    tick = _tick_at(ref_price)
    raw = (os.environ.get("GRID_SPACING_MATCH_TICKS") or "").strip()
    try:
        match_ticks = int(raw) if raw else 1
    except ValueError:
        match_ticks = 1
    match_ticks = max(0, match_ticks)
    return abs(inferred - configured) <= (match_ticks * tick) + 1e-9


def _snap_spacing_to_config(inferred: float, configured: float, ref_price: float) -> float:
    """
    If inferred spacing is very close to configured spacing, snap back to
    configured spacing to prevent limit churn from tiny rounding differences.
    """
    import os

    tick = _tick_at(ref_price)
    raw = (os.environ.get("GRID_SPACING_SNAP_TICKS") or "").strip()
    try:
        snap_ticks = int(raw) if raw else 10
    except ValueError:
        snap_ticks = 10
    snap_ticks = max(0, snap_ticks)
    if abs(inferred - configured) <= (snap_ticks * tick) + 1e-9:
        return float(configured)
    return float(inferred)


# ---------------------------------------------------------------------------
# Ladder geometry helpers
# ---------------------------------------------------------------------------

def _all_ladder_rungs(
    *,
    anchor: float,
    spacing: float,
    lower: float,
    upper: float,
) -> Tuple[List[float], List[float]]:
    """
    Generate every arithmetic rung within [lower, upper] from the inferred anchor.
    Returns (buy_rungs_desc, sell_rungs_asc) — buy prices below anchor, sell above.
    """
    buys: List[float] = []
    sells: List[float] = []
    if spacing <= 0:
        return buys, sells
    # how many steps fit each side
    n_buy = int(math.floor((anchor - lower) / spacing + 1e-9))
    n_sell = int(math.floor((upper - anchor) / spacing + 1e-9))
    for i in range(1, n_buy + 1):
        lv = anchor - i * spacing
        if lv >= lower - 1e-9:
            buys.append(lv)
    for i in range(1, n_sell + 1):
        lv = anchor + i * spacing
        if lv <= upper + 1e-9:
            sells.append(lv)
    buys.sort(reverse=True)  # nearest-to-anchor first (highest buy)
    sells.sort()              # nearest-to-anchor first (lowest sell)
    return buys, sells


def _mark_window_rungs(
    *,
    mark: float,
    spacing: float,
    n: int,
    win_lower: float,
    win_upper: float,
) -> Tuple[List[float], List[float]]:
    """
    Target N rungs per side from mark outward: nearest to mark first, then further out.

    Buys: mark - spacing, mark - 2*spacing, … (descending = nearest-first)
    Sells: mark + spacing, mark + 2*spacing, … (ascending = nearest-first)
    """
    buys: List[float] = []
    sells: List[float] = []
    for i in range(1, n + 1):
        b = _price_key_float(mark - i * spacing)
        s = _price_key_float(mark + i * spacing)
        if b >= win_lower - 1e-9:
            buys.append(b)
        if s <= win_upper + 1e-9:
            sells.append(s)
    return buys, sells


def _on_venue(venue_pending_keys: Set[Tuple[str, str]], side: str, px: float) -> bool:
    return (side, grid_limit_price_key(px)) in venue_pending_keys


def _hug_targets_from_nearest(
    *,
    side: str,
    mark: float,
    spacing: float,
    n: int,
    inband: List[float],
    win_lower: float,
    win_upper: float,
) -> List[float]:
    """
    Proximity rule: build a target set that "hugs" the mark by filling *from the nearest
    existing in-band limit toward the mark*, then outward to reach ``n``.

    Example (sell side):
      nearest sell = 2064.84, spacing=2.06, mark≈2056.7
      targets start 2064.84, 2062.78, 2060.72, 2058.66, then outward 2066.90 ...
    """
    if spacing <= 0 or n <= 0:
        return []
    side_n = str(side).strip().lower()
    mark_f = float(mark)
    out: List[float] = []
    seen: Set[str] = set()

    def add(px: float) -> None:
        k = grid_limit_price_key(px)
        if k in seen:
            return
        seen.add(k)
        out.append(float(px))

    # Seed: if no remnants, fall back to mark-centered window.
    if not inband:
        if side_n == "buy":
            seed, _ = _mark_window_rungs(
                mark=mark_f, spacing=spacing, n=n, win_lower=win_lower, win_upper=win_upper
            )
        else:
            _, seed = _mark_window_rungs(
                mark=mark_f, spacing=spacing, n=n, win_lower=win_lower, win_upper=win_upper
            )
        for px in seed:
            add(px)
        return out[:n]

    if side_n == "sell":
        base = min(inband)  # nearest sell above mark
        add(_price_key_float(base))
        # Toward mark
        px = float(base)
        for _ in range(n + 6):
            cand = _price_key_float(px - spacing)
            if cand <= mark_f + 1e-9 or cand < win_lower - 1e-9:
                break
            add(cand)
            px = cand
            if len(out) >= n:
                return out[:n]
        # Outward
        px = float(base)
        for _ in range(n + 6):
            cand = _price_key_float(px + spacing)
            if cand > win_upper + 1e-9:
                break
            add(cand)
            px = cand
            if len(out) >= n:
                return out[:n]
    else:
        base = max(inband)  # nearest buy below mark
        add(_price_key_float(base))
        # Toward mark
        px = float(base)
        for _ in range(n + 6):
            cand = _price_key_float(px + spacing)
            if cand >= mark_f - 1e-9 or cand > win_upper + 1e-9:
                break
            add(cand)
            px = cand
            if len(out) >= n:
                return out[:n]
        # Outward
        px = float(base)
        for _ in range(n + 6):
            cand = _price_key_float(px - spacing)
            if cand < win_lower - 1e-9:
                break
            add(cand)
            px = cand
            if len(out) >= n:
                return out[:n]

    return out[:n]


def _needs_gap_toward_mark(
    *,
    side: str,
    mark: float,
    inband: List[float],
    spacing: float,
) -> bool:
    """
    True when the nearest in-band limit on this side is more than ~1 spacing from mark.

    Used to run gap-fill even when in-band count already meets the minimum window depth.
    """
    if spacing <= 0 or not inband:
        return False
    mark_f = float(mark)
    side_n = str(side).strip().lower()
    sp = float(spacing)
    if side_n == "sell":
        dist = float(min(inband)) - mark_f
    else:
        dist = mark_f - float(max(inband))
    return dist > sp * 1.05 + 1e-9


def _gap_posts_toward_mark(
    *,
    side: str,
    mark: float,
    spacing: float,
    inband: List[float],
    win_lower: float,
    win_upper: float,
    venue_pending_keys: Set[Tuple[str, str]],
    max_steps: int,
) -> List[float]:
    """
    Proximity rule (minimal churn): if the nearest in-band limit is more than ~1 spacing
    away from the mark, post intermediate rungs by stepping from that nearest limit
    *toward the mark*.
    """
    if spacing <= 0 or not inband:
        return []
    side_n = str(side).strip().lower()
    mark_f = float(mark)
    posts: List[float] = []
    steps = max(1, int(max_steps))

    if side_n == "sell":
        base = min(inband)
        for _ in range(steps):
            cand = _price_key_float(base - spacing)
            if cand <= mark_f + 1e-9:
                break
            if cand < win_lower - 1e-9 or cand > win_upper + 1e-9:
                break
            if not _on_venue(venue_pending_keys, "sell", cand):
                posts.append(cand)
            base = cand
            # stop when nearest is within ~1 spacing
            if base - mark_f <= spacing * 1.05:
                break
        return posts

    # buy
    base = max(inband)
    for _ in range(steps):
        cand = _price_key_float(base + spacing)
        if cand >= mark_f - 1e-9:
            break
        if cand < win_lower - 1e-9 or cand > win_upper + 1e-9:
            break
        if not _on_venue(venue_pending_keys, "buy", cand):
            posts.append(cand)
        base = cand
        if mark_f - base <= spacing * 1.05:
            break
    return posts


def _missing_rungs_to_post(
    *,
    side: str,
    mark: float,
    spacing: float,
    n: int,
    inband: List[float],
    win_lower: float,
    win_upper: float,
    venue_pending_keys: Set[Tuple[str, str]],
) -> List[float]:
    """
    Post only the *count* needed to reach ``n`` in-band limits on this side.

    Fill toward the mark first, then outward from existing remnants (no full re-lattice).
    If there are no remnants on this side, seed from the mark-centered window.
    """
    initial_need = n - len(inband)
    if initial_need <= 0:
        return []

    need = initial_need
    mark_f = float(mark)
    posts: List[float] = []

    if not inband:
        if side == "buy":
            seed, _ = _mark_window_rungs(
                mark=mark_f, spacing=spacing, n=n, win_lower=win_lower, win_upper=win_upper
            )
        else:
            _, seed = _mark_window_rungs(
                mark=mark_f, spacing=spacing, n=n, win_lower=win_lower, win_upper=win_upper
            )
        for px in seed:
            if not _on_venue(venue_pending_keys, side, px):
                posts.append(px)
        return posts[:initial_need]

    if side == "sell":
        # Toward mark first: step down from nearest sell above mark.
        base = min(inband)
        for _ in range(n + 2):
            if need <= 0:
                break
            cand = _price_key_float(base - spacing)
            if cand <= mark_f + 1e-9:
                break
            if cand >= win_lower - 1e-9 and not _on_venue(venue_pending_keys, side, cand):
                posts.append(cand)
                need -= 1
            base = cand
        # Outward: step up from furthest sell.
        if need > 0:
            base = max(inband)
            for _ in range(n + 2):
                if need <= 0:
                    break
                cand = _price_key_float(base + spacing)
                if cand > win_upper + 1e-9:
                    break
                if cand > mark_f + 1e-9 and not _on_venue(venue_pending_keys, side, cand):
                    posts.append(cand)
                    need -= 1
                base = cand
    else:
        # buy: toward mark = step up from highest buy; outward = step down from lowest buy.
        base = max(inband)
        for _ in range(n + 2):
            if need <= 0:
                break
            cand = _price_key_float(base + spacing)
            if cand >= mark_f - 1e-9:
                break
            if cand <= win_upper + 1e-9 and not _on_venue(venue_pending_keys, side, cand):
                posts.append(cand)
                need -= 1
            base = cand
        if need > 0:
            base = min(inband)
            for _ in range(n + 2):
                if need <= 0:
                    break
                cand = _price_key_float(base - spacing)
                if cand < win_lower - 1e-9:
                    break
                if cand < mark_f - 1e-9 and not _on_venue(venue_pending_keys, side, cand):
                    posts.append(cand)
                    need -= 1
                base = cand

    return posts[:initial_need]


def _infer_spacing_from_remnants(
    sorted_prices: List[float],
) -> Optional[float]:
    """
    Given ≥ 2 sorted, normalised prices from the same side, estimate spacing as
    the median of consecutive gaps (robust to one outlier).
    """
    if len(sorted_prices) < 2:
        return None
    gaps = [abs(sorted_prices[i + 1] - sorted_prices[i]) for i in range(len(sorted_prices) - 1)]
    gaps.sort()
    return gaps[len(gaps) // 2]  # median


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def _env_nearest_n(grid_num: int) -> int:
    """Default protected window depth: GRID_NUM/2 per side (mirrors initial ladder half)."""
    import os
    raw = (os.environ.get("GRID_NEAREST_N") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, int(math.ceil(grid_num / 2.0)))


def _default_grid_limits_keep_depth() -> int:
    """Per-side depth cap when ``VARIBOT_GRID_LIMITS_KEEP_DEPTH`` unset; tracks ``GRID_NUM``."""
    import os

    raw_n = (os.environ.get("GRID_NUM") or "").strip()
    if raw_n:
        try:
            return max(0, int(raw_n))
        except ValueError:
            pass
    from strategy.gridstrat import DEFAULT_GRID_NUM

    return int(DEFAULT_GRID_NUM)


class RemnantInferenceResult:
    """
    Output of ``infer_ladder_from_remnants`` (mark-centered window).

    Attributes
    ----------
    sufficient       : enough venue limits already exist within configured band.
    spacing          : inferred (or configured-fallback) rung spacing used to generate targets.
    lower            : expanded-band lower bound (mark * (1 - band_tol)).
    upper            : expanded-band upper bound (mark * (1 + band_tol)).
    protected_buys   : reference target buys (mark-centered window) for logging.
    protected_sells  : reference target sells (mark-centered window) for logging.
    inband_buys      : current venue buy prices within band (below mark).
    inband_sells     : current venue sell prices within band (above mark).
  window_n           : required rungs per side (GRID_NUM/2).
  buy_spacing / sell_spacing : spacing used when filling gaps on each side.
    """

    def __init__(
        self,
        *,
        sufficient: bool,
        spacing: float,
        lower: float,
        upper: float,
        band_frac: float,
        grid_num: int,
        protected_buys: List[float],
        protected_sells: List[float],
        inband_buys: List[float],
        inband_sells: List[float],
        window_n: int,
        buy_spacing: float,
        sell_spacing: float,
    ) -> None:
        self.sufficient = sufficient
        self.spacing = spacing
        self.lower = lower
        self.upper = upper
        self.band_frac = float(band_frac)
        self.grid_num = int(grid_num)
        self.protected_buys = protected_buys
        self.protected_sells = protected_sells
        self.inband_buys = inband_buys
        self.inband_sells = inband_sells
        self.window_n = int(window_n)
        self.buy_spacing = float(buy_spacing)
        self.sell_spacing = float(sell_spacing)

    def __repr__(self) -> str:
        return (
            f"RemnantInferenceResult(sufficient={self.sufficient}, "
            f"spacing={self.spacing:g}, window={len(self.protected_buys)}b/{len(self.protected_sells)}s, "
            f"inband={len(self.inband_buys)}b/{len(self.inband_sells)}s)"
        )


def infer_ladder_from_remnants(
    *,
    mark: float,
    venue_pending_keys: Set[Tuple[str, str]],
    configured_spacing: float,
    lower: float,
    upper: float,
    grid_num: int,
    grid_band_pct: Optional[float] = None,
    nearest_n: Optional[int] = None,
) -> RemnantInferenceResult:
    """
    Mark-centered window maintenance.

    Parameters
    ----------
    mark                : current mark price.
    venue_pending_keys  : {(side, price_key_str)} from venue.
    configured_spacing  : spacing derived from config (band / GRID_NUM).
    lower / upper       : pinned ladder bounds (spacing / breach; not used for in-band window).
    grid_num            : GRID_NUM env; used to default nearest_n.
    grid_band_pct       : configured ±band % from meta (e.g. ``3.0`` for TON ±3%).
    nearest_n           : override protected-window depth (defaults to GRID_NUM/2).
    """
    mark_f = float(mark)
    n = nearest_n if nearest_n is not None else _env_nearest_n(grid_num)

    # Band around today's mark from configured grid_band_pct (or pinned bounds fallback).
    band_frac = half_band_fraction(grid_band_pct=grid_band_pct, lower=lower, upper=upper)
    band_tol = float(band_frac)
    win_lower = mark_f * (1.0 - band_tol)
    win_upper = mark_f * (1.0 + band_tol)

    # Current in-band limits (strictly below/above mark)
    inband_buys: List[float] = []
    inband_sells: List[float] = []
    all_buys: List[float] = []
    all_sells: List[float] = []
    for side, pxk in venue_pending_keys:
        try:
            px = float(pxk)
        except (ValueError, TypeError):
            continue
        if side == "buy":
            all_buys.append(px)
            if win_lower - 1e-9 <= px < mark_f - 1e-9:
                inband_buys.append(px)
        elif side == "sell":
            all_sells.append(px)
            if mark_f + 1e-9 < px <= win_upper + 1e-9:
                inband_sells.append(px)

    inband_buys.sort(reverse=True)   # nearest-first
    inband_sells.sort()              # nearest-first

    # Infer spacing from available venue limits (median adjacent gap), else fall back to configured spacing.
    inferred_candidates: List[float] = []
    if len(all_buys) >= 2:
        inferred_candidates.append(_infer_spacing_from_remnants(sorted(all_buys)) or 0.0)
    if len(all_sells) >= 2:
        inferred_candidates.append(_infer_spacing_from_remnants(sorted(all_sells)) or 0.0)
    inferred_candidates = [x for x in inferred_candidates if x and x > 0]
    inferred_spacing = inferred_candidates[0] if inferred_candidates else None

    # Use inferred spacing if close enough, but snap back to configured to avoid churn.
    use_spacing = float(configured_spacing)
    if inferred_spacing is not None:
        ref_price = float(mark_f)
        if _spacings_match(inferred_spacing, configured_spacing, ref_price):
            use_spacing = _snap_spacing_to_config(inferred_spacing, configured_spacing, ref_price)
        else:
            use_spacing = float(configured_spacing)

    # If we already have enough limits within the band, we are sufficient and do nothing.
    sufficient = (len(inband_buys) >= n) and (len(inband_sells) >= n)

    # Side-specific spacing (prefer in-band median gap when available).
    buy_spacing = use_spacing
    sell_spacing = use_spacing
    if len(inband_buys) >= 2:
        sp = _infer_spacing_from_remnants(sorted(inband_buys))
        if sp and sp > 0:
            buy_spacing = (
                _snap_spacing_to_config(sp, configured_spacing, mark_f)
                if _spacings_match(sp, configured_spacing, mark_f)
                else use_spacing
            )
    if len(inband_sells) >= 2:
        sp = _infer_spacing_from_remnants(sorted(inband_sells))
        if sp and sp > 0:
            sell_spacing = (
                _snap_spacing_to_config(sp, configured_spacing, mark_f)
                if _spacings_match(sp, configured_spacing, mark_f)
                else use_spacing
            )

    # Target window: N rungs from mark outward (toward mark first, then further out).
    protected_buys, _ = _mark_window_rungs(
        mark=mark_f,
        spacing=buy_spacing,
        n=n,
        win_lower=win_lower,
        win_upper=win_upper,
    )
    _, protected_sells = _mark_window_rungs(
        mark=mark_f,
        spacing=sell_spacing,
        n=n,
        win_lower=win_lower,
        win_upper=win_upper,
    )

    return RemnantInferenceResult(
        sufficient=sufficient,
        spacing=use_spacing,
        lower=float(win_lower),
        upper=float(win_upper),
        band_frac=float(band_frac),
        grid_num=int(grid_num),
        protected_buys=protected_buys,
        protected_sells=protected_sells,
        inband_buys=inband_buys,
        inband_sells=inband_sells,
        window_n=n,
        buy_spacing=buy_spacing,
        sell_spacing=sell_spacing,
    )


def compute_venue_actions(
    *,
    asset: str,
    result: RemnantInferenceResult,
    venue_pending_keys: Set[Tuple[str, str]],
    mark: float,
) -> Tuple[Set[Tuple[str, str]], List[Tuple[str, float]]]:
    """
    Given the inference result and current venue pending keys, decide:

    - ``cancel_keys``: (side, price_key) to cancel — venue limits outside the expanded
      mark-centered band (keeps venue tidy).
    - ``post_rungs``: (side, price) to post — missing target window rungs, nearest-first.

    Returns
    -------
    (cancel_keys, post_rungs)
    """
    import os

    from strategy.gridstrat import is_rwa_commodity_ticker

    mark_f = float(mark)
    n = int(result.window_n)
    grid_num = max(1, int(getattr(result, "grid_num", n * 2)))
    band_frac = float(getattr(result, "band_frac", 0.0))

    # Slippage caps: default 0.10%, RWAs 0.05%.
    #
    # Too-close rule (per-side, nearest-to-mark only):
    #   abs(px - mark)/mark <= 0.5*slippage_cap + rung_gap_frac
    #
    # where rung_gap_frac ≈ (2*band_frac) / grid_num (percent width per rung across the ±band).
    # This intentionally forces price to clear roughly one rung before we repost the opposing side
    # near mark, reducing "ping-pong" reposts right at the mid.
    slippage_cap = 0.0005 if is_rwa_commodity_ticker(asset) else 0.001
    rung_gap_frac = (2.0 * band_frac) / float(grid_num) if band_frac > 0 and grid_num > 0 else 0.0
    too_close_frac = 0.5 * float(slippage_cap) + float(rung_gap_frac)

    # Never post within 1/4 rung of mark.
    # rung_pct ≈ (2 * band_frac) / grid_num  => buffer = rung_pct / 4 = band_frac / (2*grid_num)
    buffer_frac = band_frac / (2.0 * grid_num) if band_frac > 0 else 0.0
    min_gap = mark_f * buffer_frac

    def _far_enough(side: str, px: float) -> bool:
        if min_gap <= 0:
            return True
        if side == "buy":
            return (mark_f - float(px)) >= (min_gap - 1e-9)
        return (float(px) - mark_f) >= (min_gap - 1e-9)

    # Protected window keys
    protected_keys: Set[Tuple[str, str]] = set()
    for px in result.protected_buys:
        protected_keys.add(("buy", grid_limit_price_key(px)))
    for px in result.protected_sells:
        protected_keys.add(("sell", grid_limit_price_key(px)))

    # --- Cancellations: depth-based only (avoid heavy churn/rate limits) ---
    #
    # Keep the nearest K per side to mark; cancel anything beyond that depth.
    # This is intentionally independent of the expanded-band window.
    raw_keep = (os.environ.get("VARIBOT_GRID_LIMITS_KEEP_DEPTH") or "").strip()
    try:
        keep_depth = int(raw_keep) if raw_keep else _default_grid_limits_keep_depth()
    except ValueError:
        keep_depth = _default_grid_limits_keep_depth()
    keep_depth = max(0, keep_depth)

    cancel_keys: Set[Tuple[str, str]] = set()
    if keep_depth >= 0:
        buys: List[Tuple[float, Tuple[str, str]]] = []
        sells: List[Tuple[float, Tuple[str, str]]] = []
        for side, pxk in venue_pending_keys:
            try:
                px = float(pxk)
            except (ValueError, TypeError):
                continue
            if side == "buy" and px < mark_f - 1e-9:
                buys.append((mark_f - px, (side, pxk)))  # distance to mark
            elif side == "sell" and px > mark_f + 1e-9:
                sells.append((px - mark_f, (side, pxk)))
        buys.sort(key=lambda x: x[0])   # nearest-first
        sells.sort(key=lambda x: x[0])  # nearest-first

        if keep_depth == 0:
            for _, k in buys:
                cancel_keys.add(k)
            for _, k in sells:
                cancel_keys.add(k)
        else:
            for _, k in buys[keep_depth:]:
                cancel_keys.add(k)
            for _, k in sells[keep_depth:]:
                cancel_keys.add(k)

    # --- Posts: fill toward mark, then fill missing count per side ---
    post_rungs: List[Tuple[str, float]] = []
    tmp_pending = set(venue_pending_keys)

    def _filter_nearest_too_close(side: str, prices: List[float]) -> List[float]:
        """Skip the nearest-to-mark rung if it's within the too-close threshold."""
        if not prices or too_close_frac <= 0 or mark_f <= 0:
            return prices
        # Compute distances in fractional terms; prices are all on the given side already.
        best_i = None
        best_df = None
        for i, px in enumerate(prices):
            df = abs(float(px) - mark_f) / mark_f
            if best_df is None or df < best_df:
                best_df = df
                best_i = i
        if best_i is not None and best_df is not None and best_df <= too_close_frac + 1e-12:
            out = list(prices)
            out.pop(int(best_i))
            return out
        return prices

    def _append_posts(side: str, prices: List[float]) -> None:
        for px in _filter_nearest_too_close(side, prices):
            if not _far_enough(side, px):
                continue
            post_rungs.append((side, px))
            tmp_pending.add((side, grid_limit_price_key(px)))

    def _fill_side_posts(*, side: str, inband: List[float], spacing: float) -> List[float]:
        """Count-fill when short; gap-fill only when count is already sufficient."""
        if len(inband) < n:
            return _missing_rungs_to_post(
                side=side,
                mark=mark_f,
                spacing=float(spacing),
                n=n,
                inband=list(inband),
                win_lower=float(result.lower),
                win_upper=float(result.upper),
                venue_pending_keys=tmp_pending,
            )
        if _needs_gap_toward_mark(
            side=side,
            mark=mark_f,
            inband=list(inband),
            spacing=float(spacing),
        ):
            return _gap_posts_toward_mark(
                side=side,
                mark=mark_f,
                spacing=float(spacing),
                inband=list(inband),
                win_lower=float(result.lower),
                win_upper=float(result.upper),
                venue_pending_keys=tmp_pending,
                max_steps=n + 2,
            )
        return []

    _append_posts("buy", _fill_side_posts(side="buy", inband=list(result.inband_buys), spacing=result.buy_spacing))
    _append_posts("sell", _fill_side_posts(side="sell", inband=list(result.inband_sells), spacing=result.sell_spacing))

    return cancel_keys, post_rungs
