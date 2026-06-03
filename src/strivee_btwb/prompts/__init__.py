"""Prompt templates for the LLM steps.

Kept as plain-text files alongside this module so they can be edited and
reviewed without touching Python source (and without forcing line-length
exceptions on multi-hundred-line prompt strings). Templates use ``str.format``
placeholders; load one with :func:`load` and call ``.format(...)`` on it.
"""

from importlib.resources import files


def load(name: str) -> str:
    """Return the text of a prompt template file shipped in this package."""
    return files(__package__).joinpath(name).read_text(encoding="utf-8")
