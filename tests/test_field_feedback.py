"""Canvas apps and step reports: change detection, read-back, paste, black screens."""
import sys

import pytest
from PIL import Image, ImageDraw

from deskpilot.perceive import changed, is_black, norm_text, thumb

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def screen(text_px: int = 0, caret: bool = False):
    img = Image.new("RGB", (1920, 1080), (250, 250, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((300, 200, 700, 240), outline=(120, 120, 120))
    if text_px:  # glyphs typed into the field
        d.rectangle((310, 210, 310 + text_px, 230), fill=(20, 20, 20))
    if caret:
        d.line((312 + text_px, 208, 312 + text_px, 232), fill=(0, 0, 0), width=1)
    return img


def test_blinking_caret_is_not_a_change_but_typing_is():
    base = thumb(screen())
    assert not changed(base, thumb(screen(caret=True)))
    assert changed(base, thumb(screen(text_px=24)))       # about three characters
    assert not changed(base, thumb(screen()))
    assert changed(None, base)


def test_a_playing_video_is_ignored_but_other_changes_still_count():
    from deskpilot.perceive import diff_box, moving_box

    def frame(shade, typed=0):
        img = screen(text_px=typed)
        ImageDraw.Draw(img).rectangle((1000, 300, 1600, 640), fill=(shade, 40, 90))  # the "video"
        return thumb(img)

    f0, f1, f2 = frame(10), frame(120), frame(230)
    b1, b2 = diff_box(f0, f1), diff_box(f1, f2)
    mask = moving_box(b1, b2, f2.size)
    assert mask is not None
    assert not changed(f2, frame(60), mask)                 # only the video moved
    assert changed(f2, frame(60, typed=24), mask)           # typing elsewhere still shows
    loading_a, loading_b = (10, 10, 60, 40), (200, 150, 300, 200)
    assert moving_box(loading_a, loading_b, f2.size) is None  # changes in different places: still loading


def test_black_screen_detection():
    assert is_black(Image.new("RGB", (1920, 1080), (0, 0, 0)))
    assert is_black(Image.new("RGB", (1920, 1080), (3, 3, 3)))
    assert not is_black(screen())
    dark_ui = Image.new("RGB", (800, 600), (18, 18, 18))
    ImageDraw.Draw(dark_ui).rectangle((100, 100, 700, 500), fill=(200, 200, 200))
    assert not is_black(dark_ui)                            # a dark theme with content is not "black"


def test_ocr_readback_normalisation():
    assert norm_text("Unlock Mission") == norm_text("unlock  mission")
    assert norm_text("200") in norm_text("Height 2OO")      # O read as 0
    assert norm_text("2") in norm_text("200") and norm_text("200") not in norm_text("2")


@win
def test_step_helpers():
    from deskpilot.desktop import EFFECT_STEPS, _delay, _kind, _step_text

    assert _kind({"into": "Name", "type": "x"}) == "type"
    assert _kind({"paste_file": "a.dart", "into": "e3"}) == "paste_file"
    assert _kind({"click": {"ocr": "Save"}}) == "click" and "click" in EFFECT_STEPS
    assert "sleep" not in EFFECT_STEPS and "wait_stable" not in EFFECT_STEPS
    assert _delay({"delay_ms": 80}, 0.012) == 0.08
    assert _delay({}, 0.012) == 0.012 and _delay({"delay_ms": "x"}, 0.01) == 0.01
    assert _delay({"delay_ms": 99999}, 0) == 1.0
    assert _step_text({"paste": "a" * 10000}) == "paste 10000 chars"   # never echoes the pasted text
    assert _step_text({"paste_file": r"C:\code\body.dart"}) == "paste file body.dart"
    assert _step_text({"into": "e4", "type": "hello"}) == 'type "hello" into "e4"'


@win
def test_read_file_encodings(tmp_path):
    from deskpilot.clip import read_file

    p = tmp_path / "a.dart"
    p.write_bytes("// héllo\nvoid main() {}\n".encode("utf-8-sig"))
    assert read_file(str(p)) == "// héllo\nvoid main() {}\n"
    p.write_bytes("x = 1".encode("utf-16"))
    assert read_file(str(p)) == "x = 1"
    with pytest.raises(FileNotFoundError):
        read_file(str(tmp_path / "missing.txt"))


@win
def test_clipboard_survives_a_paste_round_trip():
    from deskpilot import clip

    try:
        original, _ = clip.save()
    except RuntimeError:
        pytest.skip("clipboard busy")
    try:
        clip.set_text("the user's 10 KB of code")
        saved, lost = clip.save()
        clip.set_text("temporary paste", private=True)
        assert clip.get_text() == "temporary paste"
        clip.restore(saved)
        assert clip.get_text() == "the user's 10 KB of code" and lost == 0
    finally:
        clip.restore(original)
