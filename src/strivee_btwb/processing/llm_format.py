"""LLM-based workout formatting for BTWB.

Replaces the regex approach for the post step: sends each block's raw content to
a local Ollama model, which extracts the Rx-level content and formats it cleanly
for entry into BTWB's workout form.
"""

import logging
import re

from ..core import config
from ..core.llm import LLMUnavailableError, chat_text
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
    return ProgrammingBlock(
        name=block.name,
        content=movement + "\n" + block.content,
        instruction=block.instruction,
    )


def format_for_btwb(block: ProgrammingBlock, model: str | None = None) -> ProgrammingBlock:
    """Reformat a block's content for BTWB using a local Ollama model."""
    block = _ensure_movement_in_content(block)
    m = model or config.OLLAMA_FORMAT_MODEL
    logger.debug("[%s] formatting with model '%s'", block.name, m)
    logger.debug("[%s] input (%d chars):\n%s", block.name, len(block.content), block.content)

    prompt = _PROMPT.format(examples=_BTWB_EXAMPLES, content=block.content)
    logger.debug("[%s] prompt (%d chars):\n%s", block.name, len(prompt), prompt)
    try:
        result = chat_text(prompt, m).strip()
    except LLMUnavailableError:
        # Infrastructure failure: do NOT silently fall back to unformatted content,
        # which would push raw Strivee text to BTWB. Abort so the caller can stop.
        raise
    except Exception as exc:
        # Content problem (not infrastructure) — degrade gracefully to the original.
        logger.warning("[%s] LLM format error (%s) — returning original content", block.name, exc)
        return block

    result = re.sub(r"#(\d+(?:\.\d+)?)%", r"@\1%", result)
    result = re.sub(r"\bC\s*&\s*J\b", "Clean and Jerk", result, flags=re.IGNORECASE)
    if result:
        logger.debug("[%s] output (%d chars):\n%s", block.name, len(result), result)
        return ProgrammingBlock(name=block.name, content=result, instruction=block.instruction)
    logger.warning("[%s] LLM returned empty — returning original content", block.name)
    return block
