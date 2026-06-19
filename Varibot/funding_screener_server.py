#!/usr/bin/env python3
"""Serve funding_screener.html and live ``GET /api/screener`` JSON.

Run from Varibot/ (needs ``.env`` for Vari funding + leverage):

  python3 funding_screener_server.py
  python3 funding_screener_server.py --port 8765

Open http://127.0.0.1:8765/funding_screener.html — auto-refresh calls ``/api/screener``.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

_VARIBOT_DIR = Path(__file__).resolve().parent
if str(_VARIBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_VARIBOT_DIR))

from fundingratecheck import fetch_screener_payload  # noqa: E402


class ScreenerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_VARIBOT_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/"):
            super().log_message(fmt, *args)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if self.path.split("?", 1)[0] == "/api/screener":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            return
        super().do_OPTIONS()

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/api/screener":
            self._handle_screener_api()
            return
        super().do_GET()

    def _handle_screener_api(self) -> None:
        try:
            payload = fetch_screener_payload()
            body = json.dumps(payload).encode("utf-8")
        except Exception as exc:
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Funding screener static server + live API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    server = ThreadingHTTPServer((args.host, int(args.port)), ScreenerHandler)
    print(f"Screener server → http://{args.host}:{args.port}/funding_screener.html", file=sys.stderr)
    print(f"Live API       → http://{args.host}:{args.port}/api/screener", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
