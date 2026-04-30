"""Unit tests for vision JSON extraction and sanitisation helpers."""

import json

import pytest

from strivee_btwb.vision.parser import _extract_json, _is_excluded, _sanitize_json_strings

# ---------------------------------------------------------------------------
# _sanitize_json_strings
# ---------------------------------------------------------------------------


def test_sanitize_replaces_literal_newline_in_string():
    raw = '{"content": "line1\nline2"}'
    result = _sanitize_json_strings(raw)
    assert "\\n" in result
    assert json.loads(result)["content"] == "line1\nline2"


def test_sanitize_preserves_escaped_newline():
    raw = '{"content": "line1\\nline2"}'
    result = _sanitize_json_strings(raw)
    assert json.loads(result)["content"] == "line1\nline2"


def test_sanitize_outside_string_untouched():
    raw = '{\n"key": "value"\n}'
    result = _sanitize_json_strings(raw)
    # Structural newlines kept as-is
    assert json.loads(result)["key"] == "value"


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_bare():
    raw = '{"blocks": [{"name": "WOD", "content": "5 rounds"}]}'
    result = json.loads(_extract_json(raw))
    assert result["blocks"][0]["name"] == "WOD"


def test_extract_json_with_markdown_fence():
    raw = '```json\n{"blocks": [{"name": "WOD", "content": "5x5"}]}\n```'
    result = json.loads(_extract_json(raw))
    assert result["blocks"][0]["name"] == "WOD"


def test_extract_json_with_leading_prose():
    raw = 'Here is the JSON:\n{"blocks": []}'
    result = json.loads(_extract_json(raw))
    assert result["blocks"] == []


def test_extract_json_repairs_premature_array_close():
    # LLM emits ], before the next block instead of ,
    raw = '{"blocks": [{"name": "A", "content": "x"}, \n{"name": "B", "content": "y"}]}'
    result = json.loads(_extract_json(raw))
    assert len(result["blocks"]) == 2


def test_extract_json_raises_on_no_object():
    with pytest.raises(ValueError, match="No JSON object found"):
        _extract_json("just some text with no JSON")


# ---------------------------------------------------------------------------
# _is_excluded
# ---------------------------------------------------------------------------


def test_is_excluded_matching_prefix(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Warm-up", "Hebdomadaire"])
    assert _is_excluded("Warm-up part 2")
    assert _is_excluded("hebdomadaire recap")  # case-insensitive


def test_is_excluded_non_matching(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Warm-up"])
    assert not _is_excluded("Back Squat")
    assert not _is_excluded("WOD")


def test_is_excluded_empty_list(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])
    assert not _is_excluded("Anything")
