from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional

from dotenv import load_dotenv


Mode = Literal["paper", "live"]


@dataclass(frozen=True)
class BotConfig:
    base_url: str
    wallet_address: str
    vr_token: str

    mode: Mode
    poll_interval_s: int

    max_slippage: float
    max_leverage: int

    runs_dir: str
    state_db_path: str


def load_config(*, env_path: Optional[str] = None) -> BotConfig:
    """
    Loads config from environment. Locally, reads `.env` via python-dotenv.
    In Railway, set env vars in the service config.
    """
    load_dotenv(dotenv_path=env_path)

    base_url = os.getenv("VARI_BASE_URL", "https://omni.variational.io").rstrip("/")
    wallet_address = os.getenv("VR_WALLET_ADDRESS", "").strip()
    vr_token = os.getenv("VR_TOKEN", "").strip()

    mode = (os.getenv("BOT_MODE", "paper").strip().lower() or "paper")  # type: ignore[assignment]
    if mode not in ("paper", "live"):
        raise ValueError("BOT_MODE must be 'paper' or 'live'")

    poll_interval_s = int(os.getenv("BOT_POLL_INTERVAL_S", "300"))

    max_slippage = float(os.getenv("MAX_SLIPPAGE", "0.002"))
    max_leverage = int(os.getenv("MAX_LEVERAGE", "50"))

    runs_dir = os.getenv("BOT_RUNS_DIR", "./runs")
    state_db_path = os.getenv("BOT_STATE_DB", "./state.db")

    missing = []
    if not wallet_address:
        missing.append("VR_WALLET_ADDRESS")
    if not vr_token:
        missing.append("VR_TOKEN")
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return BotConfig(
        base_url=base_url,
        wallet_address=wallet_address,
        vr_token=vr_token,
        mode=mode,  # type: ignore[arg-type]
        poll_interval_s=poll_interval_s,
        max_slippage=max_slippage,
        max_leverage=max_leverage,
        runs_dir=runs_dir,
        state_db_path=state_db_path,
    )

