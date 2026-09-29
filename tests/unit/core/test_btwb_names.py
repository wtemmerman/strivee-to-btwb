"""Unit tests for the list of movement names BTWB is known to hold."""

import json

import pytest

from strivee_btwb.core import btwb_names


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    btwb_names._read.cache_clear()
    yield tmp_path
    btwb_names._read.cache_clear()


def test_no_list_yet_means_nothing_confirmed(data_dir):
    assert btwb_names.confirmed_movements() == frozenset()


def test_confirming_a_name_records_it_sorted(data_dir):
    btwb_names.confirm_movement("Seal Row")
    btwb_names.confirm_movement("Bike Erg")
    assert btwb_names.confirmed_movements() == {"Bike Erg", "Seal Row"}
    stored = json.loads((data_dir / "btwb_movements.json").read_text())
    assert stored == {"confirmed": ["Bike Erg", "Seal Row"]}


def test_confirming_a_known_name_leaves_the_file_alone(data_dir):
    btwb_names.confirm_movement("Seal Row")
    path = data_dir / "btwb_movements.json"
    before = path.stat().st_mtime_ns
    btwb_names.confirm_movement("Seal Row")
    assert path.stat().st_mtime_ns == before


def test_the_committed_list_holds_the_names_the_classic_paths_use():
    btwb_names._read.cache_clear()
    names = btwb_names.confirmed_movements()
    assert {"Bike Erg", "Run", "Seal Row", "Tempo Back Squat", "Strict Handstand Push-up"} <= names
