"""Unit tests for the audit's per-day set cache and its choice of source text."""

from datetime import date

import pytest

from strivee_btwb.core import config as cfg
from strivee_btwb.core.models import INTER, DayProgramming, ProgrammingBlock, WeeklyProgramming
from strivee_btwb.pipeline import (
    SETS_SCHEMA_VERSION,
    _audit_weeks,
    _crossfit_only,
    _parsed_source_mtime_ns,
    _sets_fingerprint,
    do_audit,
    load_sets_day,
    save_day,
    save_formatted_day,
    save_sets_day,
    week_for_audit,
    week_work_sets,
)
from strivee_btwb.processing.accessory import ACCESSORY_BLOCK_NAME
from strivee_btwb.processing.volume import METCON, STRENGTH, WorkSet

WEEK = date(2026, 8, 24)


def _day(content: str = "5RM Barbell bench press") -> DayProgramming:
    return DayProgramming(
        date=date(2026, 8, 29),
        day_label="Sat",
        blocks=[ProgrammingBlock(name="EMF 60 : Bench press", content=content)],
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(cfg, "FORMATTED_DIR", tmp_path / "formatted")


# ── The set cache ─────────────────────────────────────────────────────────────


def test_cached_sets_round_trip():
    day = _day()
    sets = [WorkSet("Barbell Bench Press", 1, "5", STRENGTH, source="EMF 60 : Bench press")]
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, sets)
    assert load_sets_day(day, WEEK, fingerprint) == sets


def test_an_empty_extraction_is_cached_rather_than_re_run():
    """A block that genuinely prescribes nothing must not cost an LLM call each time."""
    day = _day()
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, [])
    assert load_sets_day(day, WEEK, fingerprint) == []


def test_a_missing_cache_is_not_an_error():
    assert load_sets_day(_day(), WEEK, "whatever") is None


def test_edited_block_text_invalidates_the_cache():
    day = _day()
    save_sets_day(day, WEEK, _sets_fingerprint(day), [WorkSet("Bench Press", 1, "5", STRENGTH)])
    relevelled = _day("Accumulated 8-10 Reps Banded Bar Muscle-up")
    assert load_sets_day(relevelled, WEEK, _sets_fingerprint(relevelled)) is None


def test_the_fingerprint_covers_the_block_name_as_well_as_its_content():
    """Names carry movements the content never repeats, e.g. 'EMF 60 : Back Squat'."""
    a = DayProgramming(date(2026, 8, 29), "Sat", [ProgrammingBlock("Back Squat", "3 Reps RPE 8")])
    b = DayProgramming(date(2026, 8, 29), "Sat", [ProgrammingBlock("Deadlift", "3 Reps RPE 8")])
    assert _sets_fingerprint(a) != _sets_fingerprint(b)


def test_a_stale_schema_version_invalidates_the_cache():
    """A changed prompt must re-extract, not silently reuse the old reading."""
    day = _day()
    fingerprint = _sets_fingerprint(day)
    path = save_sets_day(day, WEEK, fingerprint, [WorkSet("Bench Press", 1, "5", STRENGTH)])
    stale = path.read_text().replace(
        f'"schema_version": {SETS_SCHEMA_VERSION}', '"schema_version": 0'
    )
    path.write_text(stale)
    assert load_sets_day(day, WEEK, fingerprint) is None


def test_every_field_survives_the_round_trip():
    day = _day()
    sets = [WorkSet("Bar Muscle-up", 6, "2", METCON, source="EMF 60 : Energy System Training")]
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, sets)
    (restored,) = load_sets_day(day, WEEK, fingerprint)
    assert restored.reps == "2"
    assert restored.block_type == METCON
    assert restored.source == "EMF 60 : Energy System Training"


# ── Which text the audit counts ───────────────────────────────────────────────


def test_a_previewed_day_is_counted_at_the_level_that_was_chosen():
    """The athlete trains the selected level, so RX would count a workout they skip."""
    parsed = _day("RX prescription")
    save_day(parsed, WEEK)
    chosen = DayProgramming(
        date=parsed.date,
        day_label="Sat",
        blocks=[ProgrammingBlock("EMF 60 : Bench press", "INTER prescription", level=INTER)],
    )
    save_formatted_day(chosen, WEEK, _parsed_source_mtime_ns(WEEK, "Sat"))
    week, fell_back = week_for_audit(["Sat"], WEEK)
    assert week.days[0].blocks[0].content == "INTER prescription"
    assert fell_back == []


def test_a_day_that_was_never_previewed_falls_back_to_rx_and_says_so():
    save_day(_day("RX prescription"), WEEK)
    week, fell_back = week_for_audit(["Sat"], WEEK)
    assert week.days[0].blocks[0].content == "RX prescription"
    assert fell_back == ["Sat"]


def test_days_with_no_analysis_at_all_are_skipped():
    save_day(_day(), WEEK)
    week, fell_back = week_for_audit(["Mon", "Sat"], WEEK)
    assert [d.day_label for d in week.days] == ["Sat"]


# ── Guard rails on the accessory flags ────────────────────────────────────────


def test_post_without_on_is_refused_before_anything_loads():
    """--post has no date to post to without --on; failing late would open a browser first."""
    with pytest.raises(SystemExit) as exit_info:
        do_audit(["Sat"], WEEK, "gym", on=None, post=True)
    assert exit_info.value.code == 1


def test_a_misspelled_day_is_refused_rather_than_silently_planned():
    with pytest.raises(SystemExit) as exit_info:
        do_audit(["Sat"], WEEK, "gym", on=["Tues"])
    assert exit_info.value.code == 1


def test_a_valid_day_gets_past_the_guards():
    """No analysis cached, so it must fail on that rather than on the day name."""
    with pytest.raises(SystemExit):
        do_audit(["Sat"], WEEK, "gym", on=["Tue"])


# ── Counting only what was actually done ──────────────────────────────────────


def _two_block_day() -> DayProgramming:
    return DayProgramming(
        date=date(2026, 8, 29),
        day_label="Sat",
        blocks=[
            ProgrammingBlock(name="EMF 60 : Bench press", content="5RM Barbell bench press"),
            ProgrammingBlock(name="EMF 60 :  Handstand Walk", content="60m Handstand Walk"),
        ],
    )


def _cache_sets(day: DayProgramming) -> None:
    save_sets_day(
        day,
        WEEK,
        _sets_fingerprint(day),
        [
            WorkSet("Barbell Bench Press", 1, "5", STRENGTH, source="EMF 60 : Bench press"),
            WorkSet("Handstand Walk", 1, "60m", METCON, source="EMF 60 :  Handstand Walk"),
        ],
    )


def test_without_completion_data_everything_programmed_is_counted():
    day = _two_block_day()
    _cache_sets(day)
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    assert len(week_work_sets(week)) == 2


def test_a_block_that_was_not_logged_as_done_is_not_counted():
    day = _two_block_day()
    _cache_sets(day)
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    done = {"2026-08-29": {"EMF 60 : Bench press"}}
    assert [s.movement for s in week_work_sets(week, done)] == ["Barbell Bench Press"]


def test_a_day_absent_from_the_completion_data_counts_as_nothing_done():
    day = _two_block_day()
    _cache_sets(day)
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    assert week_work_sets(week, {}) == []


def test_title_matching_survives_the_double_space_btwb_carries():
    """'EMF 60 :  Handstand Walk' keeps the source's own spacing on both sides."""
    day = _two_block_day()
    _cache_sets(day)
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    done = {"2026-08-29": {"EMF 60 : Handstand Walk"}}  # single space
    assert [s.movement for s in week_work_sets(week, done)] == ["Handstand Walk"]


def test_filtering_does_not_invalidate_the_extraction_cache():
    """The cache is keyed on the day's text, so --actual costs no extra model calls."""
    day = _two_block_day()
    _cache_sets(day)
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    week_work_sets(week, {"2026-08-29": {"EMF 60 : Bench press"}})
    assert load_sets_day(day, WEEK, _sets_fingerprint(day)) is not None
    assert len(week_work_sets(week)) == 2


# ── Planning this week from last week ─────────────────────────────────────────


def test_by_default_the_measured_and_planned_weeks_are_the_same():
    assert _audit_weeks(WEEK, from_last_week=False) == (WEEK, WEEK)


def test_from_last_week_measures_the_week_before_the_one_being_planned():
    """This week's delivery is not knowable until this week is over."""
    assert _audit_weeks(WEEK, from_last_week=True) == (date(2026, 8, 17), WEEK)


def test_accessory_work_is_excluded_from_the_baseline():
    """Counting it in makes the system undo itself: 5 sets, then 0, then 5 again."""
    day = DayProgramming(
        date=date(2026, 8, 25),
        day_label="Tue",
        blocks=[
            ProgrammingBlock(name="EMF 60 : Back Squat", content="5x5"),
            ProgrammingBlock(name=ACCESSORY_BLOCK_NAME, content="12 Cable Lateral Raise"),
        ],
    )
    trimmed = _crossfit_only(WeeklyProgramming(week_start=WEEK, days=[day]))
    assert [b.name for b in trimmed.days[0].blocks] == ["EMF 60 : Back Squat"]


def test_a_week_with_no_accessory_work_passes_through_unchanged():
    day = DayProgramming(
        date=date(2026, 8, 25),
        day_label="Tue",
        blocks=[ProgrammingBlock(name="EMF 60 : Back Squat", content="5x5")],
    )
    week = WeeklyProgramming(week_start=WEEK, days=[day])
    assert [b.name for b in _crossfit_only(week).days[0].blocks] == ["EMF 60 : Back Squat"]


def test_days_survive_the_baseline_trim_even_when_emptied_by_it():
    """An accessory-only day still belongs to the week; it just contributes nothing."""
    day = DayProgramming(
        date=date(2026, 8, 25),
        day_label="Tue",
        blocks=[ProgrammingBlock(name=ACCESSORY_BLOCK_NAME, content="12 Cable Lateral Raise")],
    )
    trimmed = _crossfit_only(WeeklyProgramming(week_start=WEEK, days=[day]))
    assert len(trimmed.days) == 1
    assert trimmed.days[0].blocks == []


def test_from_last_week_reports_the_week_it_could_not_find():
    """The error has to name the measured week, not the one that was asked for."""
    with pytest.raises(SystemExit) as exit_info:
        do_audit(["Mon"], WEEK, "gym", from_last_week=True)
    assert exit_info.value.code == 1
