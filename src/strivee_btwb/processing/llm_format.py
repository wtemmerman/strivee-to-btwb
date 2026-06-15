"""LLM-based workout formatting for BTWB.

Replaces the regex approach for the post step: sends each block's raw content to
a local Ollama model, which extracts the Rx-level content and formats it cleanly
for entry into BTWB's workout form.
"""

import logging
import re

from ..core import config
from ..core.llm import chat_text
from ..core.models import ProgrammingBlock
from ..prompts import load

logger = logging.getLogger("processing")

_BTWB_EXAMPLES = load("btwb_examples.txt")

_PROMPT = load("format_block.txt")


def _movement_from_block_name(name: str) -> str | None:
    """Extract the movement label from a block title like 'EMF 60 : Clean Pull'."""
    m = re.match(r"^EMF\s+[\w\s'\"]+[:\-]\s*(.+)$", name, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _ensure_movement_in_content(block: ProgrammingBlock) -> ProgrammingBlock:
    """Prepend the movement name to content when it is only named in the block title."""
    movement = _movement_from_block_name(block.name)
    if not movement:
        return block
    if movement.lower() in block.content.lower():
        return block
    return block.replace(content=movement + "\n" + block.content)


def format_for_btwb(block: ProgrammingBlock, model: str | None = None) -> ProgrammingBlock:
    """Reformat a block's content for BTWB using a local Ollama model."""
    block = _ensure_movement_in_content(block)
    m = model or config.OLLAMA_FORMAT_MODEL
    logger.debug("[%s] formatting with model '%s'", block.name, m)
    logger.debug("[%s] input (%d chars):\n%s", block.name, len(block.content), block.content)

    prompt = _PROMPT.format(examples=_BTWB_EXAMPLES, content=block.content)
    logger.debug("[%s] prompt (%d chars):\n%s", block.name, len(prompt), prompt)
    # chat_text returns a string or raises LLMUnavailableError. We deliberately do
    # NOT catch that here: a failed model call must abort the run, not silently
    # post raw Strivee text to BTWB. A *successful* but empty response is handled
    # by the `if result:` check below (degrade to the original parsed content).
    result = chat_text(prompt, m).strip()

    # Percentages of a RM use @ not # — including ranges like "#85-90%".
    result = re.sub(r"#(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)%", r"@\1%", result)
    result = re.sub(r"\bC\s*&\s*J\b", "Clean and Jerk", result, flags=re.IGNORECASE)
    if result:
        logger.debug("[%s] output (%d chars):\n%s", block.name, len(result), result)
        return block.replace(content=result)
    logger.warning("[%s] LLM returned empty — returning original content", block.name)
    return block
