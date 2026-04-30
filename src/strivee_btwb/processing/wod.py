"""
WOD extraction: select the Rx level and strip coaching notes from parsed blocks.

Two steps applied in sequence:
  1. extract_rx  — if Rx / Inter+ / Inter sections exist, keep only Rx
  2. strip_coaching — remove objective/technique lines, keep workout structure
"""

import re

from ..core.models import ProgrammingBlock

# Level section header patterns (case-insensitive, start of line)
_LEVEL_RE = re.compile(r"^(rx|inter\+?)\s*[-:]", re.MULTILINE | re.IGNORECASE)
_RX_RE = re.compile(r"^rx\s*[-:]", re.MULTILINE | re.IGNORECASE)
_INTER_RE = re.compile(r"^inter\+?\s*[-:]", re.MULTILINE | re.IGNORECASE)

# Line prefixes that indicate coaching notes (checked lowercase)
_COACHING_PREFIXES = (
    "objectif",
    "l'objectif",
    "build to",
    "départ en",
    "stop avant",
    "le bike",
    "format associé",
    "on démarre",
    "accélérez",
    "extension maxim",
    "on descend",
    "on veut monter",
    "c'est d'arriver",
)


def _extract_rx(content: str) -> str:
    """Return only the Rx section if multiple athlete levels are present."""
    if not _LEVEL_RE.search(content):
        return content  # No level structure — return as-is

    rx_match = _RX_RE.search(content)
    if not rx_match:
        # No Rx label but Inter sections exist — take content before first Inter
        inter = _INTER_RE.search(content)
        return content[: inter.start()].strip() if inter else content

    # Find where the next level section starts after Rx
    inter = _INTER_RE.search(content, rx_match.end())
    end = inter.start() if inter else len(content)
    return content[rx_match.start() : end].strip()


def _strip_coaching(content: str) -> str:
    """Remove coaching/objective lines, keep workout structure."""
    kept = []
    for line in content.splitlines():
        s = line.strip()
        low = s.lower()

        if any(low.startswith(p) for p in _COACHING_PREFIXES):
            continue

        # All-caps instruction shouts: "DÉPART EN CLEAN AND JERK OBLIGATOIRE AMPLITUDE COMPLÈTE!"
        if s and s == s.upper() and s.endswith("!") and len(s) > 10:
            continue

        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    return "\n".join(kept)


def prepare_block(block: ProgrammingBlock) -> ProgrammingBlock:
    """Extract Rx section and strip coaching notes from a block's content."""
    content = _extract_rx(block.content)
    content = _strip_coaching(content)
    return ProgrammingBlock(name=block.name, content=content)
