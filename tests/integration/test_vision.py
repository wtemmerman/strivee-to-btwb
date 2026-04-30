"""Integration tests for vision parsing against a real Ollama instance.

These tests require Ollama to be running locally with a vision-capable model.
They are skipped automatically when Ollama is not reachable.
Set OLLAMA_MODEL in the environment or .env to override the default model.
"""

from datetime import date

import pytest

try:
    import ollama

    ollama.list()
    _OLLAMA_AVAILABLE = True
except Exception:
    _OLLAMA_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _OLLAMA_AVAILABLE, reason="Ollama not reachable — skipping vision integration tests"
)


@pytest.fixture()
def sample_image():
    """Return a small white image; the LLM should return an empty blocks list."""
    from PIL import Image

    return Image.new("RGB", (100, 200), (255, 255, 255))


def test_extract_day_programming_returns_day_programming(sample_image):
    from strivee_btwb.vision import extract_day_programming

    result = extract_day_programming(
        images=[sample_image],
        day_label="Mon",
        target_date=date(2026, 4, 27),
    )
    assert result.day_label == "Mon"
    assert result.date == date(2026, 4, 27)
    assert isinstance(result.blocks, list)


def test_extract_day_programming_excludes_configured_blocks(sample_image, monkeypatch):
    """Excluded blocks configured in EXCLUDED_BLOCKS must not appear in results."""
    import strivee_btwb.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Warm-up"])

    from strivee_btwb.vision import extract_day_programming

    result = extract_day_programming(
        images=[sample_image],
        day_label="Mon",
        target_date=date(2026, 4, 27),
    )
    for block in result.blocks:
        assert not block.name.lower().startswith("warm-up")
