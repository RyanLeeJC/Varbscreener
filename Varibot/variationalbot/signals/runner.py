from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SignalResult:
    ok: bool
    data: Dict[str, Any]
    elapsed_ms: int
    stdout: str
    stderr: str
    returncode: int


def run_signal_script(
    *,
    script_path: str,
    input_json: Dict[str, Any],
    timeout_s: int = 120,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> SignalResult:
    """
    Runs an external Python script and enforces a strict JSON stdout contract.

    Contract:
    - Script must print a single JSON object to stdout.
    - Expected shape (recommended):
      {
        "long": [{"asset":"SOL","score":0.7,"usd_size":50.0}, ...],
        "short": [...],
        "meta": {...}
      }
    """
    started = time.time()
    proc = subprocess.run(
        ["python", script_path],
        input=json.dumps(input_json).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )
    elapsed_ms = int((time.time() - started) * 1000)
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()

    try:
        data = json.loads(stdout) if stdout else {}
        ok = isinstance(data, dict)
    except Exception:
        data = {}
        ok = False

    return SignalResult(
        ok=bool(ok and proc.returncode == 0),
        data=data if isinstance(data, dict) else {},
        elapsed_ms=elapsed_ms,
        stdout=stdout,
        stderr=stderr,
        returncode=proc.returncode,
    )

