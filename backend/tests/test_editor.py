import json
import subprocess

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
