"""Unit tests for capture helpers (no ADB required)."""

from datetime import date

from PIL import Image

from strivee_btwb.capture import _change_fraction, _screens_same, save_capture, stitch_vertical

# ---------------------------------------------------------------------------
# _change_fraction / _screens_same
# ---------------------------------------------------------------------------


def _solid(color: tuple[int, int, int], width: int = 100, height: int = 200) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def test_change_fraction_identical_images():
    img = _solid((255, 255, 255))
    assert _change_fraction(img, img) == 0.0


def test_change_fraction_completely_different():
    white = _solid((255, 255, 255))
    black = _solid((0, 0, 0))
    frac = _change_fraction(white, black)
    assert frac > 0.5


def test_screens_same_identical():
    img = _solid((200, 200, 200))
    assert _screens_same(img, img)


def test_screens_same_different():
    white = _solid((255, 255, 255))
    black = _solid((0, 0, 0))
    assert not _screens_same(white, black)


# ---------------------------------------------------------------------------
# stitch_vertical
# ---------------------------------------------------------------------------


def test_stitch_vertical_single_image():
    img = _solid((100, 100, 100), width=80, height=120)
    result = stitch_vertical([img])
    assert result is img


def test_stitch_vertical_multiple_images():
    imgs = [_solid((i * 50, 0, 0), width=100, height=50) for i in range(3)]
    result = stitch_vertical(imgs)
    assert result.width == 100
    assert result.height == 150


def test_stitch_vertical_different_widths():
    imgs = [_solid((0, 0, 0), width=w, height=50) for w in (80, 100, 60)]
    result = stitch_vertical(imgs)
    assert result.width == 100  # max width


# ---------------------------------------------------------------------------
# save_capture
# ---------------------------------------------------------------------------


def test_save_capture_creates_file(tmp_path):
    img = _solid((255, 0, 0), width=50, height=50)
    path = save_capture(img, label="Mon", output_dir=tmp_path)
    assert path.exists()
    assert path.suffix == ".png"
    assert "Mon" in path.name


def test_save_capture_per_week_folder(tmp_path):
    img = _solid((0, 255, 0), width=50, height=50)
    ws = date(2026, 4, 27)
    path = save_capture(img, label="Tue", week_start=ws, output_dir=tmp_path)
    assert path.parent.name == "2026-04-27"
    assert path.exists()


def test_save_capture_no_label(tmp_path):
    img = _solid((0, 0, 255), width=50, height=50)
    path = save_capture(img, output_dir=tmp_path)
    assert path.exists()
    assert path.name.startswith("strivee_")
