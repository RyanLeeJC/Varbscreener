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

import listingtable as lt


def main() -> int:
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

    lt.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

