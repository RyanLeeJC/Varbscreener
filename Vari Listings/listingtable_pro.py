"""
Wrapper around listingtable.py that uses a CoinGecko API key (Analyst/Pro)
to fetch markets data more efficiently.

Behavior:
- If COINGECKO_API_KEY is set, increase /coins/markets `per_page` and (optionally) id batch sizes.
- Also reduce the minimum delay between CoinGecko calls (still respects 429 Retry-After).

Run:
  COINGECKO_API_KEY=... python3 "Vari Listings/listingtable_pro.py"
"""

from __future__ import annotations

import os
import sys

try:
    # Optional: allows local runs to pick up Varibot/.env without manual export.
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

import listingtable as lt


def main() -> int:
    if load_dotenv is not None:
        # Local convenience: load ../Varibot/.env if present. (Railway uses service Variables instead.)
        here = os.path.dirname(os.path.abspath(__file__))
        maybe_env = os.path.abspath(os.path.join(here, "..", "Varibot", ".env"))
        if os.path.isfile(maybe_env):
            load_dotenv(maybe_env)

    api_key = os.getenv(lt.COINGECKO_API_KEY_ENV, "").strip()
    if api_key:
        # Pro API keys must use the Pro root URL.
        lt.COINGECKO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
        # CoinGecko /coins/markets allows up to 250 per page.
        lt.COINGECKO_MARKETS_PER_PAGE = 250
        # For id-based calls: large batches can produce a URL that's too long (HTTP 400),
        # so keep this conservative unless you override explicitly.
        lt.COINGECKO_ID_BATCH_SIZE = int(os.getenv("COINGECKO_ID_BATCH_SIZE_PRO", "100"))
        # Keep symbol lookup capped at 50 (CoinGecko docs / our code comments).
        lt.COINGECKO_SYMBOL_BATCH_SIZE = min(int(getattr(lt, "COINGECKO_SYMBOL_BATCH_SIZE", 50)), 50)
        # Analyst/Pro plans have higher rate limits; this just reduces our conservative pacing.
        # 429 handling still sleeps based on Retry-After.
        lt.COINGECKO_MIN_SECONDS_BETWEEN_CALLS = float(
            os.getenv("COINGECKO_MIN_SECONDS_BETWEEN_CALLS_PRO", "0.25")
        )

    # One-line banner so it's obvious why this is slow (usually: missing key -> Free limits).
    plan = "pro" if api_key else "free"
    key_hint = (api_key[:6] + "..." + api_key[-4:]) if api_key and len(api_key) >= 12 else ("set" if api_key else "missing")
    print(
        f"[listingtable_pro] plan={plan} api_key={key_hint} base={lt.COINGECKO_BASE_URL} "
        f"per_page={lt.COINGECKO_MARKETS_PER_PAGE} id_batch={lt.COINGECKO_ID_BATCH_SIZE} "
        f"min_sleep_s={lt.COINGECKO_MIN_SECONDS_BETWEEN_CALLS}",
        file=sys.stderr,
    )

    lt.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

