"""Run the API monitor continuously outside AWS Lambda."""

from __future__ import annotations

import logging
import os
import time

from src.main import lambda_handler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    interval_seconds = float(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    if interval_seconds <= 0:
        raise ValueError("CHECK_INTERVAL_SECONDS must be greater than zero")

    next_run = time.monotonic()
    while True:
        try:
            response = lambda_handler({}, object())
            logger.info("Check cycle completed: %s", response["body"])
        except Exception:
            logger.exception("API monitoring cycle failed")

        next_run += interval_seconds
        time.sleep(max(0, next_run - time.monotonic()))


if __name__ == "__main__":
    main()
