from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

from curl_cffi.requests import Session

from .errors import (
    VariAuthError,
    VariCloudflareError,
    VariForbiddenError,
    VariUnexpectedResponse,
)


def vari_http_proxies() -> Optional[Dict[str, str]]:
    """
    If HTTPS_PROXY or HTTP_PROXY is set (e.g. on Railway behind Cloudflare), use it for
    all Omni requests. Example: https://user:pass@host:port
    """
    u = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if not u:
        return None
    return {"http": u, "https": u}


def _default_user_agent() -> str:
    # Keep close to a modern Chrome UA; curl_cffi impersonation handles the rest.
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    )


@dataclass(frozen=True)
class VariAuth:
    wallet_address: str
    vr_token: str


class VariClient:
    """
    HTTP client for Variational Omni.

    Rate limits (documented): per IP 10 requests / 10s; global 1000/min.
    We enforce the per-IP cap with a sliding window so bursts (e.g. multi-market
    loops) don't trigger 429s. Override with VARI_RATE_LIMIT_MAX / VARI_RATE_LIMIT_WINDOW_S
    (set max to 0 to disable the limiter).
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: VariAuth,
        impersonate: str = "chrome124",
        timeout_s: int = 20,
        user_agent: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout_s = timeout_s
        self.session = Session(impersonate=impersonate)
        self.user_agent = user_agent or _default_user_agent()
        self._proxies = vari_http_proxies()
        # Sliding window: at most `_rate_max` calls per `_rate_window_s` seconds (monotonic clock).
        self._rate_window_s = float(os.getenv("VARI_RATE_LIMIT_WINDOW_S", "10"))
        self._rate_max = int(os.getenv("VARI_RATE_LIMIT_MAX", "10"))
        self._request_times: Deque[float] = deque()

    def _wait_for_rate_limit(self) -> None:
        if self._rate_max <= 0:
            return
        while True:
            now = time.monotonic()
            while self._request_times and self._request_times[0] <= now - self._rate_window_s:
                self._request_times.popleft()
            if len(self._request_times) < self._rate_max:
                self._request_times.append(now)
                return
            wait_s = self._rate_window_s - (now - self._request_times[0]) + 0.02
            if wait_s > 0:
                time.sleep(wait_s)

    def _headers(self) -> Dict[str, str]:
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": self.base_url,
            "referer": f"{self.base_url}/perpetual/BTC",
            "user-agent": self.user_agent,
            "vr-connected-address": self.auth.wallet_address,
        }

    def _cookies(self) -> Dict[str, str]:
        return {
            "vr-token": self.auth.vr_token,
            "vr-connected-address": self.auth.wallet_address.lower(),
        }

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[int] = None,
        retries: int = 2,
        retry_backoff_s: float = 1.0,
    ) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        last_err: Optional[Exception] = None

        for attempt in range(retries + 1):
            try:
                resp = None
                n429 = 0
                while True:
                    self._wait_for_rate_limit()
                    req_kw: Dict[str, Any] = {
                        "method": method.upper(),
                        "url": url,
                        "headers": self._headers(),
                        "cookies": self._cookies(),
                        "json": json_body,
                        "timeout": timeout_s or self.timeout_s,
                    }
                    if self._proxies:
                        req_kw["proxies"] = self._proxies
                    resp = self.session.request(**req_kw)

                    ctype = (resp.headers.get("content-type", "") or "").lower()
                    if ctype.startswith("text/html"):
                        raise VariCloudflareError(f"Cloudflare HTML challenge for {method} {path}")

                    if resp.status_code == 401:
                        raise VariAuthError(f"401 unauthorized for {method} {path}")
                    if resp.status_code == 403:
                        raise VariForbiddenError(f"403 forbidden for {method} {path}")
                    if resp.status_code == 429:
                        n429 += 1
                        if n429 > 12:
                            raise VariUnexpectedResponse(
                                f"429 rate limited for {method} {path} too many times in one attempt"
                            )
                        ra = resp.headers.get("Retry-After")
                        try:
                            wait_s = float(ra) if ra is not None and str(ra).strip() else 10.0
                        except ValueError:
                            wait_s = 10.0
                        time.sleep(max(wait_s, 1.0))
                        continue
                    break

                assert resp is not None
                if 200 <= resp.status_code < 300:
                    # Some endpoints might still return text; attempt json then fallback.
                    try:
                        return resp.json()
                    except Exception:
                        return resp.text

                body_preview = resp.text[:800]
                raise VariUnexpectedResponse(
                    f"Unexpected status {resp.status_code} for {method} {path}: {body_preview}"
                )
            except (VariCloudflareError, VariForbiddenError) as e:
                # transient-ish; retry with backoff
                last_err = e
            except Exception as e:
                last_err = e

            if attempt < retries:
                time.sleep(retry_backoff_s * (2**attempt))

        assert last_err is not None
        raise last_err

    def health_probe(self) -> Dict[str, Any]:
        """
        A lightweight authenticated call to verify cookies/headers still work.
        """
        data = self.request_json("GET", "/api/positions", retries=0)
        return {"ok": True, "positions_type": type(data).__name__}

    def dump_auth_context(self) -> str:
        """
        For debugging only: returns a *redacted* view of auth context.
        """
        tok = self.auth.vr_token
        redacted = tok[:6] + "..." + tok[-4:] if len(tok) >= 12 else "***"
        return json.dumps(
            {
                "base_url": self.base_url,
                "wallet_address": self.auth.wallet_address,
                "vr_token": redacted,
            },
            indent=2,
        )

