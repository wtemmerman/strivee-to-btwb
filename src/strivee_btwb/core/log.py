"""Centralized logging configuration for the strivee-btwb pipeline."""

import logging
import sys

_RESET = "\033[0m"
_LEVEL_COLORS = {
    logging.DEBUG:    "\033[2m",      # dim
    logging.INFO:     "\033[0m",      # normal (no color change)
    logging.WARNING:  "\033[33m",     # yellow
    logging.ERROR:    "\033[31m",     # red
    logging.CRITICAL: "\033[1;31m",   # bold red
}


class _ColorFormatter(logging.Formatter):
    def __init__(self, fmt: str, datefmt: str, use_color: bool) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if not self._use_color:
            return line
        color = _LEVEL_COLORS.get(record.levelno, "")
        return f"{color}{line}{_RESET}"


def setup(debug: bool = False) -> None:
    """Configure the root logger with colored output when writing to a TTY.

    Silences noisy third-party libraries regardless of the chosen level.
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _ColorFormatter(
            fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            use_color=sys.stdout.isatty(),
        )
    )
    logging.root.setLevel(level)
    logging.root.addHandler(handler)
    for noisy in ("urllib3", "httpx", "playwright", "asyncio", "httpcore", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
