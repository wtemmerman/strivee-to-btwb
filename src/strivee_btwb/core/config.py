"""
Runtime configuration loaded from environment variables / .env file.

All values are module-level constants populated at import time.
Add new settings here; never read os.getenv() outside this module.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Models ────────────────────────────────────────────────────────────────────

OLLAMA_FORMAT_MODEL: str = os.getenv("OLLAMA_FORMAT_MODEL", "qwen3:8b")
"""Ollama model used for BTWB text formatting (text-only, no image input needed)."""

OLLAMA_TEXT_MODEL: str = os.getenv("OLLAMA_TEXT_MODEL", "qwen3:8b")
"""Ollama model used to parse UI accessibility text dumps (text-only, no vision needed)."""

OLLAMA_FALLBACK_TEXT_MODEL: str | None = os.getenv("OLLAMA_FALLBACK_TEXT_MODEL") or None
"""Fallback model for text parsing when the primary returns zero blocks. Set to empty to disable."""

# ── BTWB credentials ──────────────────────────────────────────────────────────

BTWB_EMAIL: str = os.getenv("BTWB_EMAIL", "")
BTWB_PASSWORD: str = os.getenv("BTWB_PASSWORD", "")

BTWB_TRACK_ID: str = os.getenv("BTWB_TRACK_ID", "")
"""Personal track ID used to filter the calendar duplicate-check.
   Visible in the BTWB planning URL as '?t=<id>' after clicking a day."""

# ── ADB / capture ─────────────────────────────────────────────────────────────

ANDROID_SERIAL: str | None = os.getenv("ANDROID_SERIAL") or None
"""ADB device serial — leave empty to auto-select the only connected device."""

MAX_SCROLLS: int = int(os.getenv("MAX_SCROLLS", "10"))
"""Maximum scrolls per day; capture stops earlier when content ends."""

SCROLL_DISTANCE: float = float(os.getenv("SCROLL_DISTANCE", "0.3"))
"""Fraction of screen height scrolled per swipe (smaller → more overlap)."""

DAY_TAB_Y: int = int(os.getenv("DAY_TAB_Y", "0"))
"""Pixel Y-coordinate of the Strivee day-tab strip used for week/day navigation taps.
Set to 0 to fall back to a screen-fraction estimate (int(h * 0.21)).
Tune once per device — 500 works well on a 1080×2400 screen."""

# ── Block filtering ───────────────────────────────────────────────────────────

_excluded_raw = os.getenv(
    "EXCLUDED_BLOCKS",
    "Hebdomadaire,GROUPE WHATS APP EMF,Warm-up,Swim Workout,Sport simulation",
)
EXCLUDED_BLOCKS: list[str] = [b.strip() for b in _excluded_raw.split(",") if b.strip()]
"""Block names (prefix-matched, case-insensitive) to drop from parsed results."""

# ── Paths ─────────────────────────────────────────────────────────────────────

CAPTURES_DIR: Path = Path(os.getenv("CAPTURES_DIR", "captures"))
"""Root directory for raw ADB screenshots (PNG files, one sub-folder per week)."""

PARSED_DIR: Path = Path(os.getenv("PARSED_DIR", "parsed"))
"""Root directory for vision-parsed JSON cache (one sub-folder per week)."""
