"""Unit tests for smart vertical stitching with overlap removal."""

import pytest
from PIL import Image

from strivee_btwb.capture.adb import _find_overlap_px, stitch_vertical


def _frame(height: int, bands: list[tuple[int, int, tuple[int, int, int]]]) -> Image.Image:
    """Build a synthetic RGB frame with colored horizontal bands.

    bands: list of (y_start, y_end, color_rgb)
    """
    img = Image.new("RGB", (200, height), (128, 128, 128))
    for y0, y1, color in bands:
        img.paste(Image.new("RGB", (200, y1 - y0), color), (0, y0))
    return img


# ---------------------------------------------------------------------------
# _find_overlap_px
# ---------------------------------------------------------------------------


def test_find_overlap_exact_match(monkeypatch):
    import strivee_btwb.core.config as cfg
    monkeypatch.setattr(cfg, "SCROLL_DISTANCE", 0.4)

    # prev: top 60px red, bottom 40px green
    prev = _frame(100, [(0, 60, (255, 0, 0)), (60, 100, (0, 255, 0))])
    # curr: top 40px green (overlap with prev's bottom), then 60px blue
    curr = _frame(100, [(0, 40, (0, 255, 0)), (40, 100, (0, 0, 255))])

    overlap = _find_overlap_px(prev, curr)
    assert overlap == 40


def test_find_overlap_falls_back_to_expected_when_no_match(monkeypatch):
    import strivee_btwb.core.config as cfg
    monkeypatch.setattr(cfg, "SCROLL_DISTANCE", 0.4)

    # Two completely different frames — no alignment possible
    prev = _frame(100, [(0, 100, (255, 0, 0))])
    curr = _frame(100, [(0, 100, (0, 0, 255))])

    overlap = _find_overlap_px(prev, curr)
    # Should fall back to expected: 100 - int(100 * 0.4) = 60
    assert overlap == 60


# ---------------------------------------------------------------------------
# stitch_vertical
# ---------------------------------------------------------------------------


def test_stitch_single_frame_unchanged():
    img = _frame(100, [(0, 100, (200, 200, 200))])
    result = stitch_vertical([img])
    assert result.size == img.size


def test_stitch_removes_overlap(monkeypatch):
    import strivee_btwb.core.config as cfg
    monkeypatch.setattr(cfg, "SCROLL_DISTANCE", 0.4)

    # 40 px of new content per scroll, 60 px overlap
    prev = _frame(100, [(0, 60, (255, 0, 0)), (60, 100, (0, 255, 0))])
    curr = _frame(100, [(0, 40, (0, 255, 0)), (40, 100, (0, 0, 255))])

    result = stitch_vertical([prev, curr])

    # prev full (100) + only new part of curr (60 px) = 160
    assert result.height == 160


def test_stitch_three_frames(monkeypatch):
    import strivee_btwb.core.config as cfg
    monkeypatch.setattr(cfg, "SCROLL_DISTANCE", 0.4)

    # Each frame scrolls 40px of new content
    f1 = _frame(100, [(0, 60, (255, 0, 0)), (60, 100, (0, 255, 0))])
    f2 = _frame(100, [(0, 40, (0, 255, 0)), (40, 100, (0, 0, 255))])
    f3 = _frame(100, [(0, 40, (0, 0, 255)), (40, 100, (255, 255, 0))])

    result = stitch_vertical([f1, f2, f3])

    # f1 (100) + f2 new (60) + f3 new (60) = 220
    assert result.height == 220
