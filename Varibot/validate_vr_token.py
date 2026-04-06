import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

from curl_cffi.requests import Session
from dotenv import load_dotenv


def _env_proxies() -> Optional[Dict[str, str]]:
    u = (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if not u:
        return None
    return {"http": u, "https": u}


BASE_URL = os.getenv("VARI_BASE_URL", "https://omni.variational.io")


def _build_headers(wallet_address: str) -> Dict[str, str]:
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/perpetual/BTC",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "vr-connected-address": wallet_address,
    }


def _build_cookies(vr_token: str, wallet_address: str) -> Dict[str, str]:
    return {
        "vr-token": vr_token,
        "vr-connected-address": wallet_address.lower(),
    }


def validate_vr_token(
    vr_token: str,
    wallet_address: str,
    endpoint: str = "/api/positions",
    timeout_s: int = 20,
) -> Tuple[bool, Dict[str, Any]]:
    session = Session(impersonate="chrome136")
    get_kw: Dict[str, Any] = {
        "headers": _build_headers(wallet_address),
        "cookies": _build_cookies(vr_token, wallet_address),
        "timeout": timeout_s,
    }
    px = _env_proxies()
    if px:
        get_kw["proxies"] = px
    resp = session.get(f"{BASE_URL}{endpoint}", **get_kw)

    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        return False, {
            "reason": "Cloudflare challenge page returned (HTML, not JSON)",
            "status_code": resp.status_code,
        }

    if resp.status_code == 401:
        return False, {"reason": "vr-token rejected or expired", "status_code": 401}
    if resp.status_code == 403:
        return False, {
            "reason": "forbidden (auth context/IP/policy)",
            "status_code": 403,
        }

    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return True, {"status_code": resp.status_code, "body": body}

    return False, {
        "reason": "unexpected status code",
        "status_code": resp.status_code,
        "body_preview": resp.text[:500],
    }


def main() -> int:
    load_dotenv()  # loads from .env in current working directory, if present

    vr_token = os.getenv("VR_TOKEN")
    wallet = os.getenv("VR_WALLET_ADDRESS")
    endpoint = os.getenv("VARI_AUTH_TEST_ENDPOINT", "/api/positions")

    missing = [k for k, v in [("VR_TOKEN", vr_token), ("VR_WALLET_ADDRESS", wallet)] if not v]
    if missing:
        print(
            "Missing required env vars: "
            + ", ".join(missing)
            + "\n\n"
            + "Create a local .env (NOT committed) with:\n"
            + "  VR_TOKEN=...\n"
            + "  VR_WALLET_ADDRESS=0x...\n",
            file=sys.stderr,
        )
        return 2

    ok, info = validate_vr_token(vr_token=vr_token, wallet_address=wallet, endpoint=endpoint)
    print(json.dumps({"ok": ok, **info}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

