"""Unit tests for vision JSON extraction and sanitisation helpers."""

import json

import pytest

from strivee_btwb.vision.parser import (
    _BLOCKS_SCHEMA,
    _extract_json,
    _is_excluded,
    _sanitize_json_strings,
    _validate_blocks,
    count_block_titles,
)

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
    # LLM emits ], before the next block — re.sub fix handles this
    raw = '{"blocks": [{"name": "A", "content": "x"}], \n{"name": "B", "content": "y"}]}'
    result = json.loads(_extract_json(raw))
    assert len(result["blocks"]) == 2


def test_extract_json_uses_repair_json_as_last_resort():
    # JSON with braces but broken interior that re.sub cannot fix — falls through to repair_json
    raw = '{"blocks": [{"name": "WOD", "content": "21-15-9}}'
    result = json.loads(_extract_json(raw))
    assert "blocks" in result


def test_extract_json_bare_array():
    raw = '[{"name": "WOD", "content": "21-15-9"}, {"name": "Strength", "content": "5x5"}]'
    result = json.loads(_extract_json(raw))
    assert isinstance(result, list)
    assert result[0]["name"] == "WOD"


def test_extract_json_bare_array_needs_repair():
    """Bare array with invalid JSON triggers repair_json fallback (lines 140-141)."""
    # Array starts first, but has broken syntax → falls through to repair_json
    raw = '[{"name": "WOD", "content": "21-15-9}'  # unclosed object in array
    # repair_json may return a list or dict depending on heuristics — just ensure it parses
    result = json.loads(_extract_json(raw))
    assert result is not None


def test_extract_json_raises_on_no_object():
    with pytest.raises(ValueError, match="No JSON value found"):
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


# ---------------------------------------------------------------------------
# _validate_blocks
# ---------------------------------------------------------------------------


def test_validate_blocks_keeps_wellformed():
    raw = [{"name": "WOD", "content": "21-15-9", "instruction": "go hard"}]
    assert _validate_blocks(raw, "Mon") == raw


def test_validate_blocks_defaults_missing_fields():
    result = _validate_blocks([{"name": "Squat"}], "Mon")
    assert result == [{"name": "Squat", "content": "", "instruction": ""}]


def test_validate_blocks_drops_non_objects_and_empty_names(caplog):
    import logging

    raw = [
        {"name": "WOD", "content": "x"},
        "not an object",
        {"name": "   ", "content": "orphan"},
    ]
    with caplog.at_level(logging.WARNING, logger="vision"):
        result = _validate_blocks(raw, "Mon")
    assert [b["name"] for b in result] == ["WOD"]
    assert sum("dropping" in r.message for r in caplog.records) == 2


def test_validate_blocks_non_list_returns_empty():
    assert _validate_blocks({"blocks": []}, "Mon") == []


# ---------------------------------------------------------------------------
# structured output (schema passed to the model)
# ---------------------------------------------------------------------------


def test_extract_passes_schema_to_model(monkeypatch):
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {"message": {"content": '{"blocks": [{"name": "WOD", "content": "5x5"}]}'}}

    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", fake_chat)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))
    assert captured["format"] == _BLOCKS_SCHEMA
    assert result.blocks[0].name == "WOD"


# ---------------------------------------------------------------------------
# count_block_titles
# ---------------------------------------------------------------------------


def test_count_block_titles_counts_emf_titles(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])
    text = "EMF 60 : Snatch\nBuild to 1RM\nEMF RX - Optional RUN\n5k easy"
    assert count_block_titles(text) == ["EMF 60 : Snatch", "EMF RX - Optional RUN"]


def test_count_block_titles_ignores_minute_header(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])
    # "EMF 60'" is the app header (prime symbol, no separator) — not a block title.
    text = "EMF 60'\nEMF 60 : Back Squat\n4 sets"
    assert count_block_titles(text) == ["EMF 60 : Back Squat"]


def test_count_block_titles_skips_excluded(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Sport simulation"])
    text = "EMF 60 : Friday sport Simulation\nblah\nEMF 60 : Back Squat\n4 sets"
    assert count_block_titles(text) == ["EMF 60 : Back Squat"]


def test_count_block_titles_ignores_subsection_and_emoji_lines(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])
    text = "Main Part -\n📌 Session\n🔱 Rx\nEMF 60 : Clean\n1 rep"
    assert count_block_titles(text) == ["EMF 60 : Clean"]


def test_extract_warns_when_blocks_dropped(monkeypatch, caplog):
    import logging
    from datetime import date

    import strivee_btwb.core.config as cfg
    from strivee_btwb.vision.parser import extract_day_programming_from_text

    # Source has two EMF titles, but the model only returns one block.
    source = "EMF 60 : Snatch\nBuild to 1RM\nEMF RX : Conditioning\nAMRAP 10"
    fake_response = {
        "message": {
            "content": '{"blocks": [{"name": "EMF 60 : Snatch", "content": "Build to 1RM"}]}'
        }
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])

    with caplog.at_level(logging.WARNING, logger="vision"):
        result = extract_day_programming_from_text(source, "Mon", date(2026, 4, 27))

    assert len(result.blocks) == 1
    assert any("may have dropped a block" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# extract_day_programming_from_text (ollama mocked — no model required)
# ---------------------------------------------------------------------------


def test_extract_from_text_parses_mocked_response(monkeypatch):
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {
        "message": {
            "content": (
                '{"blocks": [{"name": "EMF 60 : Snatch", "content": "Build to 1RM",'
                ' "instruction": "Objectif: focus"},'
                ' {"name": "WOD", "content": "AMRAP 12:00", "instruction": ""}]}'
            )
        }
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("some text", "Mon", date(2026, 4, 27))

    assert result.day_label == "Mon"
    assert len(result.blocks) == 2
    assert result.blocks[0].name == "EMF 60 : Snatch"
    assert result.blocks[0].instruction == "Objectif: focus"
    assert result.blocks[1].instruction == ""


def test_extract_from_text_drops_excluded_blocks(monkeypatch):
    from datetime import date

    import strivee_btwb.core.config as cfg
    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {
        "message": {
            "content": (
                '{"blocks": [{"name": "🔥 Warm-up 🔥", "content": "5 min", "instruction": ""},'
                ' {"name": "WOD", "content": "21-15-9", "instruction": ""}]}'
            )
        }
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Warm-up"])

    result = extract_day_programming_from_text("some text", "Mon", date(2026, 4, 27))

    assert len(result.blocks) == 1
    assert result.blocks[0].name == "WOD"


def test_extract_from_text_empty_response_returns_zero_blocks(monkeypatch):
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    monkeypatch.setattr(
        "strivee_btwb.core.llm.ollama.chat",
        lambda **_: {"message": {"content": ""}},
    )
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("some text", "Tue", date(2026, 4, 28))
    assert result.blocks == []


def test_extract_from_text_raises_on_unparseable_response(monkeypatch):
    from datetime import date

    import pytest

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    monkeypatch.setattr(
        "strivee_btwb.core.llm.ollama.chat",
        lambda **_: {"message": {"content": "sorry, I cannot parse this"}},
    )
    with pytest.raises(ValueError, match="Text parsing failed"):
        extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))


def test_extract_from_text_normalises_list_response(monkeypatch):
    """Model returns a bare list instead of {"blocks": [...]}."""
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {
        "message": {"content": '[{"name": "WOD", "content": "AMRAP 12", "instruction": ""}]'}
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))
    assert len(result.blocks) == 1
    assert result.blocks[0].name == "WOD"


def test_extract_from_text_normalises_name_as_key_format(monkeypatch):
    """Model returns {"BlockName": "content"} instead of {"blocks": [...]}."""
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {"message": {"content": '{"Back Squat": "5x5 @ 80%", "WOD": "21-15-9"}'}}
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("text", "Fri", date(2026, 4, 25))
    assert len(result.blocks) == 2
    assert result.blocks[0].name == "Back Squat"


def test_extract_from_text_normalises_wrapped_list_format(monkeypatch):
    """Model returns {"converted_data": [...]} — list_vals path (line 231)."""
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {
        "message": {
            "content": (
                '{"converted_data": [{"name": "WOD", "content": "AMRAP 12", "instruction": ""}]}'
            )
        }
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))
    assert len(result.blocks) == 1
    assert result.blocks[0].name == "WOD"


def test_extract_from_text_excluded_blocks_logged(monkeypatch, caplog):
    """Excluded blocks count is logged at DEBUG level."""
    import logging
    from datetime import date

    import strivee_btwb.core.config as cfg
    from strivee_btwb.vision.parser import extract_day_programming_from_text

    fake_response = {
        "message": {
            "content": '{"blocks": [{"name": "Warm-up", "content": "5 min", "instruction": ""}, '
            '{"name": "WOD", "content": "21-15-9", "instruction": ""}]}'
        }
    }
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake_response)
    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Warm-up"])

    with caplog.at_level(logging.DEBUG, logger="vision"):
        result = extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))

    assert len(result.blocks) == 1
    assert any(
        "dropped" in r.message.lower() or "exclusion" in r.message.lower() for r in caplog.records
    )
