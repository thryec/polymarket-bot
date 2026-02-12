from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val or ""


@dataclass(frozen=True)
class Config:
    # Wallet
    private_key: str = field(default_factory=lambda: _env("PRIVATE_KEY", required=True))
    wallet_address: str = field(default_factory=lambda: _env("WALLET_ADDRESS", required=True))

    # API keys
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", required=True))
    brave_search_api_key: str = field(default_factory=lambda: _env("BRAVE_SEARCH_API_KEY", ""))

    # Polymarket CLOB
    clob_api_url: str = field(default_factory=lambda: _env("CLOB_API_URL", "https://clob.polymarket.com"))
    chain_id: int = field(default_factory=lambda: int(_env("CHAIN_ID", "137")))

    # Tuning
    scan_interval: int = field(default_factory=lambda: int(_env("SCAN_INTERVAL", "60")))
    min_edge: float = field(default_factory=lambda: float(_env("MIN_EDGE", "0.05")))
    kelly_fraction: float = field(default_factory=lambda: float(_env("KELLY_FRACTION", "0.6")))
    max_bet_usdc: float = field(default_factory=lambda: float(_env("MAX_BET_USDC", "500")))
    max_drawdown_pct: float = field(default_factory=lambda: float(_env("MAX_DRAWDOWN_PCT", "0.35")))

    # Mode
    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN", "false").lower() == "true")

    # Paths
    db_path: Path = field(default_factory=lambda: DATA_DIR / "bot.db")
