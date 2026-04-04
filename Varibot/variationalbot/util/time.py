from __future__ import annotations

import time


def sleep_with_drift_correction(*, period_s: int, started_at: float) -> None:
    """
    Sleeps until started_at + period_s, preventing drift accumulation.
    If we're behind schedule, returns immediately.
    """
    target = started_at + float(period_s)
    now = time.time()
    if target > now:
        time.sleep(target - now)

