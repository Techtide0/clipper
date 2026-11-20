import json
import subprocess

import pytest

from app.editor import make_formats


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def test_make_formats_dimensions(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(src)],
        check=True)
    vertical, wide = make_formats(src, tmp_path / "out", "t")
    assert _probe(vertical) == (1080, 1920)
    assert _probe(wide) == (1920, 1080)


def _make_source(tmp_path):
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    return src


@pytest.mark.parametrize("layout", ["fit", "fill"])
def test_both_vertical_layouts_produce_a_phone_sized_clip(tmp_path, layout):
    vertical, wide = make_formats(_make_source(tmp_path), tmp_path / "out", f"t-{layout}", layout)
    assert _probe(vertical) == (1080, 1920) and _probe(wide) == (1920, 1080)


def test_an_unknown_layout_falls_back_to_fit(tmp_path):
    vertical, _ = make_formats(_make_source(tmp_path), tmp_path / "out", "t", "sideways")
    assert _probe(vertical) == (1080, 1920)


def test_fill_really_crops_and_fit_really_keeps_the_whole_picture():
    from app import editor
    assert "crop=1080:1920" in editor.VERTICAL_FILL and "overlay" not in editor.VERTICAL_FILL
    assert "overlay" in editor.VERTICAL_FIT and "scale=270:480" in editor.VERTICAL_FIT  # the fast blur
    assert "boxblur=30" not in editor.VERTICAL_FIT  # not the slow full-size blur


class _Run:
    """A scripted subprocess.run: each call returns the next result and records the encoder used."""

    def __init__(self, *codes):
        self.codes, self.encoders = list(codes), []

    def __call__(self, cmd, **kw):
        self.encoders.append(cmd[cmd.index("-c:v") + 1])
        code = self.codes.pop(0)
        return subprocess.CompletedProcess(cmd, code, "", "" if code == 0 else "encoder failed")


@pytest.fixture()
def hw(monkeypatch):
    from app import editor
    monkeypatch.setattr(editor, "hardware_encoder", lambda: ("h264_qsv", "-global_quality", "23"))
    monkeypatch.setattr(editor.settings, "hardware_encoding", True)
    return editor


def test_the_hardware_encoder_is_used_when_the_machine_has_one(hw, monkeypatch, tmp_path):
    run = _Run(0)
    monkeypatch.setattr(hw.subprocess, "run", run)
    hw._ffmpeg(tmp_path / "in.mp4", "g", tmp_path / "out.mp4")
    assert run.encoders == ["h264_qsv"]


def test_a_hardware_failure_falls_back_to_software_so_no_clip_is_lost(hw, monkeypatch, tmp_path):
    run = _Run(1, 0)
    monkeypatch.setattr(hw.subprocess, "run", run)
    (tmp_path / "out.mp4").write_bytes(b"half written by the failed attempt")
    hw._ffmpeg(tmp_path / "in.mp4", "g", tmp_path / "out.mp4")
    assert run.encoders == ["h264_qsv", "libx264"]


def test_hardware_encoding_can_be_switched_off(hw, monkeypatch, tmp_path):
    monkeypatch.setattr(hw.settings, "hardware_encoding", False)
    run = _Run(0)
    monkeypatch.setattr(hw.subprocess, "run", run)
    hw._ffmpeg(tmp_path / "in.mp4", "g", tmp_path / "out.mp4")
    assert run.encoders == ["libx264"]


def test_when_every_encoder_fails_the_error_says_so(hw, monkeypatch, tmp_path):
    monkeypatch.setattr(hw.subprocess, "run", _Run(1, 1))
    with pytest.raises(RuntimeError, match="ffmpeg failed: encoder failed"):
        hw._ffmpeg(tmp_path / "in.mp4", "g", tmp_path / "out.mp4")


def test_hardware_detection_is_a_real_test_encode_and_tolerates_a_missing_ffmpeg(monkeypatch):
    from app import editor
    editor.hardware_encoder.cache_clear()
    monkeypatch.setattr(editor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"no device"))
    assert editor.hardware_encoder() is None
    editor.hardware_encoder.cache_clear()

    def missing(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(editor.subprocess, "run", missing)
    assert editor.hardware_encoder() is None
    editor.hardware_encoder.cache_clear()


@pytest.mark.parametrize("layout", ["fit", "fill"])
@pytest.mark.parametrize("framing", [(2.0, 0.5, 0.5), (3.0, 0.0, 1.0), (1.5, 1.0, 0.0), (1.0, 0.2, 0.8)])
def test_zoomed_and_shifted_clips_keep_the_phone_and_wide_sizes(tmp_path, layout, framing):
    vertical, wide = make_formats(_make_source(tmp_path), tmp_path / "out", "z", layout, framing)
    assert _probe(vertical) == (1080, 1920) and _probe(wide) == (1920, 1080)


def test_default_framing_uses_the_plain_layouts_unchanged():
    from app import editor
    assert editor.vertical_graph("fill") == editor.VERTICAL_FILL and editor.vertical_graph("fit") == editor.VERTICAL_FIT
    assert editor.wide_graph() == editor.WIDE
    assert editor.vertical_graph("fill", 2.0) != editor.VERTICAL_FILL
