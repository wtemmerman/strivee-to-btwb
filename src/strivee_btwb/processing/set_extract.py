"""Read the sets a programming block prescribes.

The counting rules live in :mod:`.volume`; this module only turns coach
shorthand into the list of movements and set counts those rules operate on.
That split is the point: "6 sets of : 1min ON / 1min OFF" followed by five
movement lines is a language problem the model handles well, while deciding
what those thirty sets are *worth* is a judgement that has to stay in a table
someone can read and a test can pin down.
"""

import json
import logging

from json_repair import repair_json

from ..core import config
from ..core.llm import chat_json
from ..core.models import ProgrammingBlock
from ..prompts import load
from .volume import BLOCK_TYPES, WorkSet

logger = logging.getLogger("processing")

_PROMPT = load("extract_sets.txt")

# Grammar-constrains the model to the six block types, so an unusable value
# reaches _validate only from a setup that ignores the schema.
_SETS_SCHEMA = {
    "type": "object",
    "properties": {
        "sets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "movement": {"type": "string"},
                    "sets": {"type": "integer"},
                    "reps": {"type": "string"},
                    "block_type": {"type": "string", "enum": list(BLOCK_TYPES)},
                },
                "required": ["movement", "sets", "reps", "block_type"],
            },
        }
    },
    "required": ["sets"],
}


def _parse(raw: str, block_name: str) -> list[dict]:
    """Return the ``sets`` array from a model response, or ``[]`` if unreadable."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(repair_json(raw))
        except (ValueError, json.JSONDecodeError):
            logger.warning(
                "[%s] set extraction returned unparseable JSON — counted as 0", block_name
            )
            return []
    if not isinstance(data, dict) or not isinstance(data.get("sets"), list):
        logger.warning(
            "[%s] set extraction returned an unexpected shape — counted as 0", block_name
        )
        return []
    return [item for item in data["sets"] if isinstance(item, dict)]


def _validate(items: list[dict], block_name: str) -> list[WorkSet]:
    """Keep the entries that describe a real set; warn about and drop the rest."""
    work_sets = []
    for item in items:
        movement = str(item.get("movement", "")).strip()
        block_type = str(item.get("block_type", "")).strip()
        try:
            sets = int(item.get("sets", 0))
        except (TypeError, ValueError):
            sets = 0
        if not movement or sets < 1 or block_type not in BLOCK_TYPES:
            logger.warning("[%s] dropping unusable set entry %r", block_name, item)
            continue
        work_sets.append(
            WorkSet(
                movement=movement,
                sets=sets,
                reps=str(item.get("reps", "")).strip(),
                block_type=block_type,
                source=block_name,
            )
        )
    return work_sets


def extract_sets(block: ProgrammingBlock, model: str | None = None) -> list[WorkSet]:
    """Return the sets *block* prescribes.

    A block the model cannot read yields ``[]`` and a warning rather than an
    exception: the audit reports on a whole week, and one unreadable block should
    under-count that muscle visibly, not abort the report. Ollama being
    unreachable still raises, like every other model call in the pipeline.
    """
    m = model or config.OLLAMA_FORMAT_MODEL
    prompt = _PROMPT.format(name=block.name, content=block.content)
    logger.debug("[%s] extracting sets with '%s'", block.name, m)
    raw = chat_json(prompt, m, schema=_SETS_SCHEMA)
    logger.debug("[%s] raw set-extraction response:\n%s", block.name, raw)
    if not raw.strip():
        logger.warning("[%s] set extraction returned empty — counted as 0", block.name)
        return []
    return _validate(_parse(raw, block.name), block.name)
