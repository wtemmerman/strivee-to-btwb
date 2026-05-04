"""Unit tests for stitch_vertical (plain vertical concatenation)."""

from PIL import Image

from strivee_btwb.capture.adb import stitch_vertical


def _frame(width: int, height: int, color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def test_stitch_single_frame_unchanged():
    img = _frame(200, 100, (200, 200, 200))
    assert stitch_vertical([img]) is img


def test_stitch_two_frames_total_height():
    imgs = [_frame(200, 100, (255, 0, 0)), _frame(200, 80, (0, 0, 255))]
    result = stitch_vertical(imgs)
    assert result.height == 180
    assert result.width == 200


def test_stitch_uses_max_width():
    imgs = [_frame(80, 50, (0, 0, 0)), _frame(120, 50, (0, 0, 0)), _frame(60, 50, (0, 0, 0))]
    result = stitch_vertical(imgs)
    assert result.width == 120
    assert result.height == 150


def test_stitch_five_frames():
    imgs = [_frame(200, 50, (i * 40, 0, 0)) for i in range(5)]
    result = stitch_vertical(imgs)
    assert result.height == 250
