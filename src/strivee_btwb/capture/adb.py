"""
Android device capture via ADB.

Primary flow per day:
  1. launch_scrcpy()          — open scrcpy for visual feedback (optional)
  2. launch_strivee()         — open the Strivee app on the device
  3. scroll_to_top()          — reset scroll position
  4. capture_day_screenshots() — tap day tab, scroll down capturing frames
  5. stitch_vertical()        — combine frames into one tall image
  6. save_capture()           — persist PNG to the per-week captures directory

ADB must be on PATH and USB debugging enabled on the device.
"""

import io
import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageChops

from ..core import config

logger = logging.getLogger("capture")

_WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAYS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
_FR_MAP = dict(zip(_WEEKDAYS_EN, _WEEKDAYS_FR))


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------


def _adb(
    cmd: list[str], serial: str | None = None, timeout: int = 15
) -> subprocess.CompletedProcess[bytes]:
    """Run an adb command and return the completed process."""
    prefix = ["adb"] + (["-s", serial] if serial else [])
    return subprocess.run(prefix + cmd, capture_output=True, timeout=timeout, check=False)


def _device_size(serial: str | None = None) -> tuple[int, int]:
    """Return (width, height) of the connected device screen in pixels."""
    result = _adb(["shell", "wm", "size"], serial)
    m = re.search(r"(\d+)x(\d+)", result.stdout.decode(errors="replace"))
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2400


# ---------------------------------------------------------------------------
# App launch
# ---------------------------------------------------------------------------


def launch_scrcpy(serial: str | None = None) -> subprocess.Popen[bytes]:
    """Start scrcpy in the background for visual feedback during capture."""
    cmd = ["scrcpy", "--window-title", "strivee-mirror", "--stay-awake"]
    if serial:
        cmd += ["-s", serial]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    return proc


def find_strivee_package(serial: str | None = None) -> str:
    """Return the Strivee app package name from the connected device.

    Raises RuntimeError if no Strivee package is found.
    """
    result = _adb(["shell", "pm", "list", "packages"], serial)
    for line in result.stdout.decode(errors="replace").splitlines():
        pkg = line.removeprefix("package:").strip()
        if "strivee" in pkg.lower():
            return pkg
    raise RuntimeError(
        "Strivee not found on device. "
        "Make sure it is installed and the device is connected (adb devices)."
    )


def launch_strivee(serial: str | None = None) -> None:
    """Launch the Strivee app via Android monkey intent."""
    package = find_strivee_package(serial)
    logger.info("Launching %s", package)
    _adb(
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        serial,
    )
    time.sleep(4)


# ---------------------------------------------------------------------------
# Screenshot & navigation
# ---------------------------------------------------------------------------


def take_screenshot(serial: str | None = None) -> Image.Image:
    """Capture the current device screen and return it as an RGB PIL image.

    Raises RuntimeError if ADB returns no data (device disconnected / debugging
    disabled).
    """
    result = _adb(["exec-out", "screencap", "-p"], serial, timeout=10)
    if not result.stdout:
        raise RuntimeError(
            "ADB screenshot returned no data. Is the device connected and USB debugging enabled?"
        )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def swipe_up(serial: str | None = None, distance_fraction: float | None = None) -> None:
    """Swipe upward (scroll content down) by a fraction of the screen height."""
    frac = distance_fraction if distance_fraction is not None else config.SCROLL_DISTANCE
    w, h = _device_size(serial)
    cx = w // 2
    y_start = int(h * 0.75)
    y_end = int(h * 0.75 - h * frac)
    _adb(["shell", "input", "swipe", str(cx), str(y_start), str(cx), str(y_end), "350"], serial)
    time.sleep(0.8)


def swipe_down(serial: str | None = None, distance_fraction: float | None = None) -> None:
    """Swipe downward (scroll content up) by a fraction of the screen height."""
    frac = distance_fraction if distance_fraction is not None else config.SCROLL_DISTANCE
    w, h = _device_size(serial)
    cx = w // 2
    y_start = int(h * 0.25)
    y_end = int(h * 0.25 + h * frac)
    _adb(["shell", "input", "swipe", str(cx), str(y_start), str(cx), str(y_end), "350"], serial)
    time.sleep(0.8)


def _change_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels that changed between two screenshots.

    The top 80 px (status bar) are excluded from comparison to avoid false
    positives from the clock ticking.
    """

    def crop(img: Image.Image) -> Image.Image:
        return img.crop((0, 80, img.width, img.height))

    diff = ImageChops.difference(crop(a), crop(b))
    bbox = diff.getbbox()
    if bbox is None:
        return 0.0
    changed = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    total = a.width * (a.height - 80)
    return changed / total


def _screens_same(a: Image.Image, b: Image.Image) -> bool:
    """Return True when two screenshots are visually identical (< 1% pixel change)."""
    return _change_fraction(a, b) < 0.01


def scroll_to_top(serial: str | None = None, max_swipes: int = 12) -> None:
    """Swipe down repeatedly until the screen stops changing, indicating the top."""
    prev = take_screenshot(serial)
    for _ in range(max_swipes):
        swipe_down(serial)
        curr = take_screenshot(serial)
        if _screens_same(prev, curr):
            break
        prev = curr


def _ui_dump(serial: str | None = None) -> str:
    """Dump the current UI hierarchy XML from the device."""
    _adb(["shell", "uiautomator", "dump", "/sdcard/uidump.xml"], serial)
    result = _adb(["shell", "cat", "/sdcard/uidump.xml"], serial)
    return result.stdout.decode(errors="replace")


def _find_element_center(xml_str: str, text: str) -> tuple[int, int] | None:
    """Return the screen centre of the first UI element whose label starts with *text*."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    for node in root.iter():
        label = node.get("text", "") or node.get("content-desc", "")
        if label.lower().startswith(text.lower()):
            bounds = node.get("bounds", "")
            coords = re.findall(r"\d+", bounds)
            if len(coords) == 4:
                left, top, right, bottom = map(int, coords)
                return (left + right) // 2, (top + bottom) // 2
    return None


def _tap(x: int, y: int, serial: str | None = None) -> None:
    """Tap a point on the device screen and wait for the UI to settle."""
    _adb(["shell", "input", "tap", str(x), str(y)], serial)
    time.sleep(1.2)


def navigate_to_day(day_short: str, serial: str | None = None) -> bool:
    """Tap the day tab in Strivee's agenda view.

    Tries the English abbreviation first, then the French one.
    Returns True if the element was found and tapped, False otherwise.
    """
    xml = _ui_dump(serial)
    center = _find_element_center(xml, day_short)
    if center is None:
        fr = _FR_MAP.get(day_short)
        if fr:
            center = _find_element_center(xml, fr)
    if center is None:
        logger.warning("Day tab '%s' not found in UI — capturing current screen", day_short)
        return False
    _tap(center[0], center[1], serial)
    return True


# ---------------------------------------------------------------------------
# Multi-scroll capture
# ---------------------------------------------------------------------------


def _crop_frame(img: Image.Image) -> Image.Image:
    """Crop a frame for vision analysis, removing header and nav bar if configured."""
    top = config.CAPTURE_CROP_TOP
    bottom = config.CAPTURE_CROP_BOTTOM
    if top or bottom:
        return img.crop((0, top, img.width, img.height - bottom if bottom else img.height))
    return img


def capture_day_screenshots(
    day_short: str,
    serial: str | None = None,
    max_scrolls: int = 10,
) -> list[Image.Image]:
    """Navigate to *day_short*, then scroll down capturing frames until content ends.

    Returns one cropped image per unique scroll position. Full-resolution frames
    are used internally for scroll detection only; a frame is dropped when the
    pixel-change fraction falls below half the expected scroll distance, meaning
    we hit the bottom mid-scroll and would capture redundant overlap.
    """
    navigate_to_day(day_short, serial)
    time.sleep(0.5)

    near_bottom_threshold = config.SCROLL_DISTANCE * 0.5

    prev = take_screenshot(serial)
    images = [_crop_frame(prev)]

    for _ in range(max_scrolls):
        swipe_up(serial)
        curr = take_screenshot(serial)
        fraction = _change_fraction(prev, curr)
        if fraction < 0.01:
            break  # absolute bottom — nothing moved
        if fraction < near_bottom_threshold:
            logger.debug("%s: near bottom (%.0f%% changed) — stopping", day_short, fraction * 100)
            break
        images.append(_crop_frame(curr))
        prev = curr

    return images


def stitch_vertical(images: list[Image.Image]) -> Image.Image:
    """Concatenate a list of images vertically into one tall composite."""
    if len(images) == 1:
        return images[0]
    w = max(img.width for img in images)
    h = sum(img.height for img in images)
    combined = Image.new("RGB", (w, h), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height
    return combined


def save_capture(
    image: Image.Image,
    label: str = "",
    week_start: date | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Save a capture PNG and return its path.

    When *week_start* is provided the file is placed in a per-week subdirectory:
    ``<CAPTURES_DIR>/<week_start>/<filename>``.
    """
    base = output_dir or config.CAPTURES_DIR
    out = base / week_start.isoformat() if week_start else base
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    path = out / f"strivee_{ts}{suffix}.png"
    image.save(path)
    return path
