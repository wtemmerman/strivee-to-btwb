"""Centralized logging configuration for the strivee-btwb pipeline."""

import logging
import sys


def setup(debug: bool = False) -> None:
    """Configure the root logger with a consistent format.

    Silences noisy third-party libraries regardless of the chosen level.
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.root.setLevel(level)
    logging.root.addHandler(handler)
    for noisy in ("urllib3", "httpx", "playwright", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
