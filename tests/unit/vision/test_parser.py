"""Unit tests for vision JSON extraction and sanitisation helpers."""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from strivee_btwb.core.models import INTER, INTER_PLUS, RX, ProgrammingBlock
from strivee_btwb.vision.parser import (
    _BLOCKS_SCHEMA,
    _clean_block_text,
    _extract_json,
    _extract_levels,
    _fill_placeholder_movements,
    _is_excluded,
    _recover_block,
    _resplit_trailing_coaching,
    _sanitize_json_strings,
    _validate_blocks,
    count_block_titles,
    extract_day_programming_from_text,
)

# ---------------------------------------------------------------------------
# _sanitize_json_strings
# ---------------------------------------------------------------------------


def test_fill_placeholder_substitutes_rx_value():
    content = "AMRAP 12:00\n\n500m Bike Erg\nX Gymnastics Movement\n\nRX - 5 Ring Muscle-up"
    assert _fill_placeholder_movements(content) == "AMRAP 12:00\n\n500m Bike Erg\n5 Ring Muscle-up"


def test_fill_placeholder_noop_without_placeholder():
    # An "RX - ..." line with no "X ... Movement" slot must be left untouched.
    content = "AMRAP 12:00\nRX - 5 Ring Muscle-up"
    assert _fill_placeholder_movements(content) == content


def test_fill_placeholder_noop_on_count_mismatch():
    content = "X Gymnastics Movement\nX Strength Movement\nRX - 5 Ring Muscle-up"
    assert _fill_placeholder_movements(content) == content


# ---------------------------------------------------------------------------
# _extract_levels — deterministic RX / INTER+ / INTER split
# ---------------------------------------------------------------------------


def test_extract_levels_splits_three_levels_left_in_content():
    """The shape that defeated the prompt: all three levels inside content."""
    block = ProgrammingBlock(
        name="EMF 60 : Gymnastic Volume Building",
        content=(
            "Volume 4/4 -\n\nRX\nAMRAP 16:00\n4-6 Ring Muscle-up\n\n"
            "INTER+\nAMRAP 16:00\n4/6 Bar Muscle-up\n\nINTER\nAMRAP 16:00\n4/6 Chest to bar"
        ),
        instruction="Objectif : tout Unbroken.",
    )
    r = _extract_levels(block)
    assert r.content == "Volume 4/4 -\nAMRAP 16:00\n4-6 Ring Muscle-up"
    assert r.inter_plus == "Volume 4/4 -\nAMRAP 16:00\n4/6 Bar Muscle-up"
    assert r.inter == "Volume 4/4 -\nAMRAP 16:00\n4/6 Chest to bar"
    assert r.instruction == "Objectif : tout Unbroken."
    assert r.available_levels() == [RX, INTER_PLUS, INTER]


def test_extract_levels_shared_preamble_prefixes_every_variant():
    """With an RX section of its own, text above the headers is shared context."""
    block = ProgrammingBlock(
        name="A", content="AMRAP 12:00\n500m Bike Erg\n\nRX\n5 Ring Muscle-up\n\nINTER\n20 C2B"
    )
    r = _extract_levels(block)
    assert r.content == "AMRAP 12:00\n500m Bike Erg\n5 Ring Muscle-up"
    assert r.inter == "AMRAP 12:00\n500m Bike Erg\n20 C2B"


def test_extract_levels_without_rx_header_keeps_variant_standalone():
    """Real Sat block: the text above INTER is RX's own work, not shared context.

    Prefixing it would have the INTER athlete do the 10 strict bar muscle-ups they
    picked INTER to avoid.
    """
    block = ProgrammingBlock(
        name="EMF 60 : Strict Bar Muscle-up",
        content=(
            "For Quality :\nAccumulated 10 Strict Bar Muscle-up / Banded Bar Muscle-up\n\n"
            "INTER (Je suis capable de faire plus de 4-8 Strict Pull-up)\n\n"
            "Accumulated 6-8 Reps Banded Bar Muscle-up"
        ),
    )
    r = _extract_levels(block)
    assert r.content == "For Quality :\nAccumulated 10 Strict Bar Muscle-up / Banded Bar Muscle-up"
    assert r.inter == "Accumulated 6-8 Reps Banded Bar Muscle-up"


def test_extract_levels_lifts_variant_out_of_instruction():
    block = ProgrammingBlock(
        name="A",
        content="AMRAP 12:00\n24 DU\n6 PC #50kg",
        instruction="Objectif : DU unbroken\n\nINTER -\nAMRAP 12:00\n24 DU\n6 PC #40kg",
    )
    r = _extract_levels(block)
    assert r.content == "AMRAP 12:00\n24 DU\n6 PC #50kg"
    assert r.inter == "AMRAP 12:00\n24 DU\n6 PC #40kg"
    assert r.instruction == "Objectif : DU unbroken"


def test_extract_levels_keeps_selection_criteria_verbatim():
    """Header-only lines are advice on which level to pick, not prescriptions."""
    instruction = (
        "EMF - INTER (Je ne suis pas à l'aise sur les anneaux)\n"
        "EMF - INTER + (Je peux faire 1 Strict Muscle-up)\n"
        "EMF - RX (Je peux faire + de 2 Strict Muscle-up)"
    )
    r = _extract_levels(
        ProgrammingBlock(name="A", content="EMOMx9 :\n3 Reps", instruction=instruction)
    )
    assert r.available_levels() == [RX]
    for line in instruction.splitlines():
        assert line in r.instruction


def test_extract_levels_combined_header_offers_one_choice():
    """ "RX INTER" is one shared prescription — an identical variant is not a choice."""
    r = _extract_levels(ProgrammingBlock(name="A", content="RX INTER\n\nAMRAP 1:00\nMax rep C2B"))
    assert r.content == "AMRAP 1:00\nMax rep C2B"
    assert r.inter == ""
    assert r.available_levels() == [RX]


def test_extract_levels_ignores_inline_rx_value():
    """ "RX - 5 Ring Muscle-up" is a placeholder value, not a section header."""
    content = "AMRAP 12:00\n500m Bike Erg\nRX - 5 Ring Muscle-up"
    assert _extract_levels(ProgrammingBlock(name="A", content=content)).content == content


def test_extract_levels_keeps_model_supplied_fields():
    """A level the model extracted correctly is not overwritten by the split."""
    block = ProgrammingBlock(name="A", content="RX\n10 RMU\nINTER\n10 C2B", inter_plus="8 BMU")
    r = _extract_levels(block)
    assert r.inter_plus == "8 BMU"
    assert r.inter == "10 C2B"


def test_extract_levels_drops_duplicate_body_but_keeps_the_criterion():
    """The model wrote the variant into both its field and instruction (real Friday case)."""
    block = ProgrammingBlock(
        name="EMF 60 : Gymnastic strength",
        content="3 sets of :\nMax rep Unbroken strict Ring Muscle-up",
        inter_plus="EMOMx6 :\n1 strict Ring muscle-up",
        instruction=(
            "EMF - INTER + (Je peux faire 1 Strict Muscle-up)\nEMOMx6 :\n1 strict Ring muscle-up"
        ),
    )
    r = _extract_levels(block)
    assert r.inter_plus == "EMOMx6 :\n1 strict Ring muscle-up"
    assert r.instruction == "EMF - INTER + (Je peux faire 1 Strict Muscle-up)"
    assert "EMOMx6" not in r.instruction  # the workout is not repeated in the coaching note


def test_extract_levels_moves_content_criterion_into_the_note():
    """A qualifier on a content-side header is advice, so it survives the split."""
    block = ProgrammingBlock(
        name="A",
        content="10 Strict BMU\n\nINTER (Je fais 4-8 Strict Pull-up)\n6-8 Banded BMU",
        instruction="Objectif : force stricte",
    )
    r = _extract_levels(block)
    assert r.inter == "6-8 Banded BMU"
    assert r.instruction.startswith("Objectif : force stricte")
    assert "INTER (Je fais 4-8 Strict Pull-up)" in r.instruction


def test_extract_levels_no_rx_section_keeps_preamble_as_content():
    """The variant leaked into content below the prescription; RX is the preamble."""
    block = ProgrammingBlock(
        name="A",
        content=(
            "For Quality :\nAccumulated 10 Strict BMU\n\n"
            "INTER (Je fais 4-8 Pull-up)\n\n6-8 Banded BMU"
        ),
    )
    r = _extract_levels(block)
    assert r.content == "For Quality :\nAccumulated 10 Strict BMU"
    assert "6-8 Banded BMU" in r.inter


def test_extract_levels_joins_every_section_naming_a_level():
    """Real Wed shape: shared work under a combined header, then a per-level part."""
    block = ProgrammingBlock(
        name="EMF 60 : Ring Muscle-up Skill",
        content=(
            "RX INTER+ INTER\nFor Quality :\n30 reps High Amplitude ring Swing\n\n+\n\n"
            "RX\nFor time :\n20/16 Ring Muscle-up\n\n"
            "INTER+\nAMRAP 6:00\nMax Rep Ring Muscle-up"
        ),
    )
    r = _extract_levels(block)
    shared = "For Quality :\n30 reps High Amplitude ring Swing\n\n+"
    assert r.content == f"{shared}\nFor time :\n20/16 Ring Muscle-up"
    assert r.inter_plus == f"{shared}\nAMRAP 6:00\nMax Rep Ring Muscle-up"
    # INTER was named only by the shared header, so it is the shared work alone.
    assert r.inter == shared


def test_extract_levels_drops_prescriptions_copied_into_instruction():
    """The model filled the fields and left copies in instruction (real Wed case)."""
    block = ProgrammingBlock(
        name="A",
        content="For Quality :\n30 reps ring Swing",
        inter_plus="AMRAP 6:00\nMax Rep Ring Muscle-up",
        instruction="AMRAP 6:00\nMax Rep Ring Muscle-up\n\nObjectif : technique parfaite",
    )
    r = _extract_levels(block)
    assert r.instruction == "Objectif : technique parfaite"


def test_extract_levels_reattaches_prescription_split_after_a_plus():
    """A dangling "+" means the model cut the workout in half; the rest is in instruction."""
    block = ProgrammingBlock(
        name="A",
        content="For Quality :\n30 reps ring Swing\n\n+",
        inter_plus="AMRAP 6:00\nMax Rep Ring Muscle-up",
        instruction="For time :\n20/16 Ring Muscle-up",
    )
    r = _extract_levels(block)
    assert r.content == "For Quality :\n30 reps ring Swing\n\n+\nFor time :\n20/16 Ring Muscle-up"
    # The shared half is prefixed onto the variant too, else INTER+ loses the skill work.
    assert (
        r.inter_plus == "For Quality :\n30 reps ring Swing\n\n+\nAMRAP 6:00\nMax Rep Ring Muscle-up"
    )
    assert r.instruction == ""


def test_extract_levels_does_not_reattach_coaching_after_a_plus():
    block = ProgrammingBlock(
        name="A", content="30 reps ring Swing\n\n+", instruction="Objectif : technique parfaite"
    )
    r = _extract_levels(block)
    assert r.content == "30 reps ring Swing\n\n+"
    assert r.instruction == "Objectif : technique parfaite"


def test_extract_levels_is_a_noop_without_level_headers():
    block = ProgrammingBlock(
        name="A", content="EMOMx5 :\n3 Power snatch", instruction="Objectif : x"
    )
    assert _extract_levels(block) == block


def test_extract_levels_never_empties_content():
    """A block whose content is only a level header keeps its text rather than vanishing."""
    block = ProgrammingBlock(name="A", content="INTER\n10 C2B")
    r = _extract_levels(block)
    assert r.content.strip()


def test_resplit_moves_trailing_coaching_to_instruction():
    content = (
        "Build to 5RM Front squat\n\nDépart sol OBLIGATOIRE.\n\n"
        "Gammes : 5 @60% -> 5 @72% -> 5RM.\n\nNotez votre 5RM.\n\nNotes : coudes hauts."
    )
    kept, moved = _resplit_trailing_coaching(content, "Notez Pré VS post.")
    assert kept == "Build to 5RM Front squat\n\nDépart sol OBLIGATOIRE."
    assert moved.startswith("Gammes : 5 @60% -> 5 @72% -> 5RM.")
    assert "Notez votre 5RM." in moved
    assert moved.endswith("Notez Pré VS post.")


def test_resplit_does_not_move_max_rep_prescription():
    # "Max rep ..." is a prescription, not coaching — must stay in content.
    content = "2 sets of :\n20 sec Hollow Hold\nMax rep Unbroken Toes to bar"
    kept, moved = _resplit_trailing_coaching(content, "")
    assert kept == content
    assert moved == ""


def test_resplit_noop_when_no_marker():
    content = "AMRAP 12:00\n500m Bike Erg\n5 Ring Muscle-up"
    assert _resplit_trailing_coaching(content, "x") == (content, "x")


def test_resplit_leaves_coaching_only_block_to_model():
    # First non-empty line is a marker → no prescription seen → leave as-is.
    content = "Objectif : récupérer\nNotes : easy"
    assert _resplit_trailing_coaching(content, "") == (content, "")


def test_clean_strips_emoji():
    assert _clean_block_text("Back Squat 🦵") == "Back Squat"
    assert _clean_block_text("➡️ Clean and jerk") == "Clean and jerk"
    assert _clean_block_text("🔱 RX - 5 Ring Muscle-up") == "RX - 5 Ring Muscle-up"


def test_clean_drops_invite_footer_and_everything_after():
    txt = "10 sets of :\n2min Row\n\n0 Score\n\nInviter un ami\nà rejoindre Strivee"
    assert _clean_block_text(txt) == "10 sets of :\n2min Row"


def test_clean_drops_nav_and_score_chrome_lines():
    txt = "AMRAP 12:00\nWOD\nBox\n5 Ring Muscle-up\n2 Scores"
    assert _clean_block_text(txt) == "AMRAP 12:00\n5 Ring Muscle-up"


def test_clean_preserves_paragraph_breaks_and_real_dashes():
    txt = "3 sets of :\n\n3 Push press\n- Start every 1Min30"
    assert _clean_block_text(txt) == "3 sets of :\n\n3 Push press\n- Start every 1Min30"


def test_clean_collapses_gaps_left_by_removed_emoji_lines():
    txt = "AMRAP 12:00\n🔥\n\n\n500m Bike Erg"
    assert _clean_block_text(txt) == "AMRAP 12:00\n\n500m Bike Erg"


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


def test_validate_blocks_ignores_model_supplied_level_fields():
    """Levels come from _extract_levels only — the model does not get a say."""
    raw = [{"name": "WOD", "content": "21-15-9", "inter_plus": "15-12-9", "inter": "12-9-6"}]
    assert _validate_blocks(raw, "Mon") == [
        {"name": "WOD", "content": "21-15-9", "instruction": ""}
    ]


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


def test_extract_wraps_single_block_object(monkeypatch):
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    # Fallback path: model returns ONE unwrapped block object, not {"blocks":[...]}.
    fake = {"message": {"content": '{"name": "EMF 60 : WOD", "content": "AMRAP 12"}'}}
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: fake)
    monkeypatch.setattr("strivee_btwb.core.config.EXCLUDED_BLOCKS", [])

    result = extract_day_programming_from_text("text", "Mon", date(2026, 4, 27))
    # One real block — NOT two garbage blocks named "name" and "content".
    assert len(result.blocks) == 1
    assert result.blocks[0].name == "EMF 60 : WOD"
    assert result.blocks[0].content == "AMRAP 12"


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


def test_count_block_titles_ignores_emf_content_line(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])
    # A content line that merely starts with "EMF <n>" and has a dash LATER is not
    # a title — the separator must come directly after the level.
    text = "EMF 60 : Back Squat\n4 sets\nEMF 3 rounds - 200m run then rest"
    assert count_block_titles(text) == ["EMF 60 : Back Squat"]


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
# _recover_block / missing-block recovery
# ---------------------------------------------------------------------------


def test_recover_block_returns_block(monkeypatch):
    resp = {"message": {"content": '{"content": "45-60min Long Run", "instruction": "Endurance"}'}}
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: resp)
    block = _recover_block("text", "EMF 60 - Optional RUN", "qwen3:8b")
    assert block is not None
    assert block.name == "EMF 60 - Optional RUN"
    assert block.content == "45-60min Long Run"
    assert block.instruction == "Endurance"


def test_recover_block_empty_content_returns_none(monkeypatch):
    resp = {"message": {"content": '{"content": "   ", "instruction": "x"}'}}
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", lambda **_: resp)
    assert _recover_block("text", "EMF 60 : X", "qwen3:8b") is None


def test_extract_recovers_block_merged_into_excluded_neighbour(monkeypatch):
    import strivee_btwb.core.config as cfg

    # The full parse drops "EMF 60 - Optional RUN" (merged into the excluded
    # Hebdomadaire); the focused recovery call then restores it.
    source = "EMF 60 : Hebdomadaire\nCALL\nEMF 60 - Optional RUN\n45-60min Long Run\nObjectif"
    primary_json = '{"blocks": [{"name": "EMF 60 : Hebdomadaire", "content": "CALL"}]}'
    recover_json = '{"content": "45-60min Long Run", "instruction": "endurance"}'
    primary = {"message": {"content": primary_json}}
    recover = {"message": {"content": recover_json}}
    monkeypatch.setattr(
        "strivee_btwb.core.llm.ollama.chat", MagicMock(side_effect=[primary, recover])
    )
    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", ["Hebdomadaire"])

    result = extract_day_programming_from_text(source, "Thu", date(2026, 6, 11))
    names = [b.name for b in result.blocks]
    assert names == ["EMF 60 - Optional RUN"]  # Hebdomadaire excluded, Optional RUN recovered
    assert result.blocks[0].content == "45-60min Long Run"


def test_extract_no_recovery_when_all_titles_present(monkeypatch):
    import strivee_btwb.core.config as cfg

    source = "EMF 60 : Snatch\nBuild to 1RM"
    primary_json = '{"blocks": [{"name": "EMF 60 : Snatch", "content": "Build to 1RM"}]}'
    primary = {"message": {"content": primary_json}}
    chat = MagicMock(return_value=primary)
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", chat)
    monkeypatch.setattr(cfg, "EXCLUDED_BLOCKS", [])

    extract_day_programming_from_text(source, "Mon", date(2026, 4, 27))
    assert chat.call_count == 1  # no recovery call when nothing is missing


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


def test_recovered_block_is_restored_to_source_order(monkeypatch):
    """Recovery appends; BTWB posts in list order, so the day must be re-sorted."""
    from datetime import date

    from strivee_btwb.vision.parser import extract_day_programming_from_text

    text = "EMF 60 : Squat\n5x5\nEMF 60 : Bench\n3x3\nEMF 60 : Row\n500m"

    def fake_chat(prompt, model=None, schema=None, **_):
        if "extract ONLY the single workout block" in prompt:
            return json.dumps({"content": "3x3"})  # the recovered middle block
        return json.dumps(
            {
                "blocks": [
                    {"name": "EMF 60 : Squat", "content": "5x5"},
                    {"name": "EMF 60 : Row", "content": "500m"},
                ]
            }
        )

    monkeypatch.setattr("strivee_btwb.vision.parser.chat_json", fake_chat)
    day = extract_day_programming_from_text(text, "Mon", date(2026, 8, 17))
    assert [b.name for b in day.blocks] == ["EMF 60 : Squat", "EMF 60 : Bench", "EMF 60 : Row"]


# ---------------------------------------------------------------------------
# source-slice verification
# ---------------------------------------------------------------------------

_SAT_TEXT = (
    "EMF 60 : Strict Bar Muscle-up\n"
    "For Quality :\nAccumulated 10 Strict Bar Muscle-up / Banded Bar Muscle-up\n"
    "EMF 60 : Bench press\n"
    "3 Sets of :\n4 Reps RPE 9\n- Rest 2min between sets -\n"
)


def test_content_matches_its_own_slice():
    from strivee_btwb.vision.parser import (
        _content_matches_source,
        _norm_title,
        _source_slices,
    )

    slices = _source_slices(_SAT_TEXT)
    own = slices[_norm_title("EMF 60 : Strict Bar Muscle-up")]
    assert _content_matches_source(
        "For Quality :\nAccumulated 10 Strict Bar Muscle-up / Banded Bar Muscle-up", own
    )


def test_content_from_a_neighbour_slice_is_rejected():
    """The real Saturday failure: block 1 came back holding the Bench press workout."""
    from strivee_btwb.vision.parser import (
        _content_matches_source,
        _norm_title,
        _source_slices,
    )

    slices = _source_slices(_SAT_TEXT)
    own = slices[_norm_title("EMF 60 : Strict Bar Muscle-up")]
    assert not _content_matches_source("3 Sets of :\n4 Reps RPE 9\n- Rest 2min between sets -", own)


def test_short_content_is_never_second_guessed():
    """Nothing distinctive to check — re-extracting on a hunch would cost accuracy."""
    from strivee_btwb.vision.parser import _content_matches_source

    assert _content_matches_source("5x5\n+\nRest", "totally unrelated source text")


def test_verify_re_extracts_only_the_mismatched_block(monkeypatch):
    from strivee_btwb.vision.parser import _verify_against_source

    calls = []

    def fake_chat(prompt, model=None, schema=None, **_):
        calls.append(prompt)
        return json.dumps({"content": "For Quality :\nAccumulated 10 Strict Bar Muscle-up"})

    monkeypatch.setattr("strivee_btwb.vision.parser.chat_json", fake_chat)
    blocks = [
        # Holds its neighbour's workout — must be re-extracted.
        ProgrammingBlock(name="EMF 60 : Strict Bar Muscle-up", content="3 Sets of :\n4 Reps RPE 9"),
        # Correct — must be left untouched.
        ProgrammingBlock(
            name="EMF 60 : Bench press",
            content="3 Sets of :\n4 Reps RPE 9\n- Rest 2min between sets -",
        ),
    ]
    result = _verify_against_source(blocks, _SAT_TEXT, "Sat", "qwen3:8b")
    assert len(calls) == 1
    assert "EMF 60 : Strict Bar Muscle-up" in calls[0]
    assert result[0].content == "For Quality :\nAccumulated 10 Strict Bar Muscle-up"
    assert result[1] is blocks[1]


def test_verify_keeps_the_block_when_recovery_fails(monkeypatch):
    """A failed re-extraction must not drop or blank the block — the warning is the signal."""
    from strivee_btwb.vision.parser import _verify_against_source

    monkeypatch.setattr("strivee_btwb.vision.parser.chat_json", lambda *a, **k: "")
    block = ProgrammingBlock(
        name="EMF 60 : Strict Bar Muscle-up", content="3 Sets of :\n4 Reps RPE 9"
    )
    result = _verify_against_source([block], _SAT_TEXT, "Sat", "qwen3:8b")
    assert len(result) == 1
    assert result[0].name == block.name
    assert result[0].content.strip()


def test_foreign_tail_is_trimmed_without_re_extracting():
    """Real Saturday case: Bench press kept its own workout plus Handstand Walk's."""
    from strivee_btwb.vision.parser import _trim_foreign_content

    text = (
        "EMF 60 : Bench press\n"
        "3 Sets of :\n4 Reps RPE 9\n- Rest 2min between sets -\n"
        "EMF RX : Handstand Walk\n"
        "For quality -\n50-100m Handstand Walk\n- On cherche ici a augmenter le volume !\n"
    )
    block = ProgrammingBlock(
        name="EMF 60 : Bench press",
        content="3 Sets of :\n4 Reps RPE 9\n- Rest 2min between sets -\n50-100m Handstand Walk",
    )
    result = _trim_foreign_content([block], text, "Sat")[0]
    assert "Handstand Walk" not in result.content
    assert "- Rest 2min between sets -" in result.content


def test_trimming_never_empties_a_block():
    """If every line looks foreign the evidence is wrong — keep the block whole."""
    from strivee_btwb.vision.parser import _strip_foreign_lines

    content = "For quality - 50-100m Handstand Walk\nAccumulated 6-8 Reps Banded Bar Muscle-up"
    assert _strip_foreign_lines(content, "", [content]) == content


def test_foreign_level_section_is_trimmed_out_of_the_coaching_note():
    """Left in the note, _extract_levels lifts it into a level from another workout."""
    from strivee_btwb.vision.parser import _trim_foreign_content

    text = (
        "EMF 60 : Bench press\n3 Sets of :\n4 Reps RPE 9\n"
        "EMF RX : Handstand Walk\nINTER\nFor quality -\n50-100m Handstand Walk\n"
    )
    block = ProgrammingBlock(
        name="EMF 60 : Bench press",
        content="3 Sets of :\n4 Reps RPE 9",
        instruction="Objectif : apprentissage du RPE\nFor quality -\n50-100m Handstand Walk",
    )
    result = _trim_foreign_content([block], text, "Sat")[0]
    assert "Handstand Walk" not in result.instruction
    assert "Objectif : apprentissage du RPE" in result.instruction
    assert result.content == "3 Sets of :\n4 Reps RPE 9"


def test_variant_holding_only_a_section_label_is_not_a_level():
    """Trim residue: "For quality -" alone would offer a level that posts a label."""
    block = ProgrammingBlock(name="A", content="3 Sets of :\n4 Reps RPE 9", inter="For quality -")
    assert _extract_levels(block).available_levels() == [RX]


def test_variant_with_a_real_prescription_survives():
    block = ProgrammingBlock(
        name="A", content="3 Sets of :\n4 Reps", inter="For quality -\n50m HSW"
    )
    assert _extract_levels(block).available_levels() == [RX, INTER]
