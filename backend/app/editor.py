"""Stage 3 editor: cut an approved moment and produce 9:16 and 16:9 versions with ffmpeg."""
import shutil
import subprocess
from pathlib import Path

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import settings
from .db import Candidate, SessionLocal

# 9:16 for Shorts/Reels/TikTok: the full frame stays visible (HUD, minimap) on a blurred
# copy of itself, rather than cropping the sides off.
VERTICAL = (
    "[0:v]split=2[a][b];"
    "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5[bg];"
    "[b]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
)
WIDE = "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v]"


def download_section(url: str, start: float, end: float, dest: Path) -> Path:
    """Download only [start, end] of a video (a few MB, not the whole stream)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "outtmpl": str(dest.with_suffix(".%(ext)s")),
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if not dest.exists():
        raise RuntimeError("section download failed")
    return dest


def _ffmpeg(src: Path, graph: str, out: Path, extra: list[str] | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", graph,
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           *(extra or []), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-300:]}")


def make_formats(src: Path, out_dir: Path, name: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    vertical, wide = out_dir / f"{name}_9x16.mp4", out_dir / f"{name}_16x9.mp4"
    _ffmpeg(src, VERTICAL, vertical)
    _ffmpeg(src, WIDE, wide)
    return vertical, wide


def render_candidate(candidate_id: int) -> None:
    """Download the segment, render both formats, record the result. Safe to run in a background task."""
    with SessionLocal() as db:
        c = db.get(Candidate, candidate_id)
        c.render_status, c.render_error = "rendering", None
        db.commit()
        tmp = settings.raw_dir / f"clip_{c.id}"
        bounds = (c.start_s, c.end_s)
        try:
            src = download_section(c.video.url, *bounds, tmp / "section.mp4")
            vertical, wide = make_formats(src, settings.clips_dir, f"clip_{c.id}")
            db.refresh(c)  # pick up any trim edit made while rendering
            c.vertical_path, c.wide_path = vertical.name, wide.name
            # if the trim changed mid-render this output is for the old bounds: don't mark it usable
            c.render_status = "done" if (c.start_s, c.end_s) == bounds else "none"
        except Exception as e:
            c.render_status, c.render_error = "failed", f"{type(e).__name__}: {e}"[:500]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            db.commit()
