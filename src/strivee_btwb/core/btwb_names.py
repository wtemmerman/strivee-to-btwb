"""Movement names BTWB is known to have, so preview can warn before post skips a block.

The classic builder looks a movement up by its exact name, and a name BTWB lacks
skips that block at post time — after the week has already been formatted and
approved. Names posting has confirmed are kept in data/btwb_movements.json, and
preview flags any other name a classic block would need.
"""

import json
from functools import cache

from . import config


def _path():
    return config.DATA_DIR / "btwb_movements.json"


@cache
def _read() -> frozenset[str]:
    path = _path()
    if not path.exists():
        return frozenset()
    return frozenset(json.loads(path.read_text())["confirmed"])


def confirmed_movements() -> frozenset[str]:
    """Every movement name BTWB's search has been seen to hold."""
    return _read()


def confirm_movement(name: str) -> None:
    """Record that BTWB holds *name*; a no-op for a name already recorded."""
    if name in _read():
        return
    names = sorted(_read() | {name})
    _path().write_text(json.dumps({"confirmed": names}, indent=2, ensure_ascii=False) + "\n")
    _read.cache_clear()
