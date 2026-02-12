"""Dry-run mode: Full pipeline without placing real orders.

Run this to verify the bot's signal quality before going live.
Usage: python dry_run.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Force dry-run mode
os.environ["DRY_RUN"] = "true"

from bot import run
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dry_run")


def main():
    log.info("=" * 60)
    log.info("STARTING DRY RUN — No real orders will be placed")
    log.info("=" * 60)

    config = Config()
    assert config.dry_run, "DRY_RUN should be true — check env override"

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("Dry run stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
