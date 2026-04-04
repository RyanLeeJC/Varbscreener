Use this to validate a vr-token against Variational’s authenticated API.

Base URL
https://omni.variational.io
Token validation approach
Variational does not expose a dedicated “validate token” endpoint in this implementation.
Instead, validate by calling an authenticated endpoint with browser-style headers + cookies:
GET /api/positions (recommended)
GET /api/portfolio
GET /api/orders/v2
Interpretation:
200 => token accepted
401 => token rejected/expired
403 => forbidden (token/context/IP issue)
text/html response => Cloudflare challenge page, not API JSON
Required auth context
Send both:
Cookie: vr-token=<TOKEN>
Cookie: vr-connected-address=<wallet_lowercase>
Header: vr-connected-address: <wallet_original_case_or_normalized>
Safe Python example (no secrets)
from curl_cffi.requests import Session
BASE_URL = "https://omni.variational.io"
def validate_vr_token(vr_token: str, wallet_address: str):
  session = Session(impersonate="chrome124")
  headers = {
      "accept": "*/*",
      "content-type": "application/json",
      "origin": BASE_URL,
      "referer": f"{BASE_URL}/perpetual/BTC",
      "user-agent": (
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/142.0.0.0 Safari/537.36"
      ),
      "vr-connected-address": wallet_address,
  }
  cookies = {
      "vr-token": vr_token,
      "vr-connected-address": wallet_address.lower(),
  }
  resp = session.get(
      f"{BASE_URL}/api/positions",
      headers=headers,
      cookies=cookies,
      timeout=20,
  )
  ctype = resp.headers.get("content-type", "")
  if ctype.startswith("text/html"):
      return {"ok": False, "reason": "Cloudflare challenge page returned"}
  if resp.status_code == 401:
      return {"ok": False, "reason": "vr-token rejected or expired"}
  if resp.status_code == 403:
      return {"ok": False, "reason": "forbidden (auth context/IP/policy)"}
  if 200 <= resp.status_code < 300:
      return {"ok": True, "status_code": resp.status_code}
  return {"ok": False, "status_code": resp.status_code, "body_preview": resp.text[:300]}
Safe cURL skeleton (placeholders only)
curl 'https://omni.variational.io/api/positions' \
  -H 'accept: */*' \
  -H 'origin: https://omni.variational.io' \
  -H 'referer: https://omni.variational.io/perpetual/BTC' \
  -H 'vr-connected-address: <WALLET_ADDRESS>' \
  -H 'user-agent: Mozilla/5.0 ... Chrome/142.0.0.0 Safari/537.36' \
  -b 'vr-token=<VR_TOKEN>; vr-connected-address=<wallet_lowercase>'