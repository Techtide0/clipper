"""Stage 3 editor: cut an approved moment and produce 9:16 and 16:9 versions with ffmpeg."""
import re
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

import yt_dlp
from sqlalchemy import update
from yt_dlp.utils import download_range_func

from .config import settings
from .db import Candidate, SessionLocal
from .sources import get_source

_RENDER_LOCK = threading.Lock()  # renders queue up behind each other (they stay 'rendering' while waiting)


def slugify(title: str | None, fallback: str) -> str:
    """A safe, readable file-name stem from a title (never from user-supplied paths)."""
    words = re.sub(r"\s+", " ", title or "")        # any whitespace, including line breaks, is a word break
    slug = re.sub(r"[^\w\- ]+", "", words).strip()  # then drop everything that isn't a letter, digit, - or _
    return re.sub(r"\s+", "-", slug)[:60].strip("-_") or fallback


def download_name(title: str | None, cid: int, orientation: str) -> str:
    """A safe, readable file name for a downloaded clip, from its title."""
    return f"{slugify(title, f'clip-{cid}')}-{'9x16' if orientation == 'vertical' else '16x9'}.mp4"


def recover_renders() -> int:
    """Renders left 'rendering' by a process that died can never finish; mark them failed so the
    Render button comes back. Call at server start, when nothing of ours can be rendering."""
    with SessionLocal() as db:
        done = db.execute(update(Candidate).where(Candidate.render_status == "rendering").values(
            render_status="failed", render_error="interrupted by a restart; press Render clip again"))
        db.commit()
        return done.rowcount

# 9:16 for Shorts/Reels/TikTok, two layouts:
#  fit  - the whole widescreen frame (HUD, minimap) in the middle of a blurred copy of itself.
#         The blurred background is made from a quarter-size copy scaled back up: it looks the same
#         (it is blurred anyway) and encodes about 3.5x faster than blurring the full 1080x1920 frame.
#  fill - the picture is enlarged to fill the whole phone screen, cropping the sides (centre kept).
VERTICAL_FIT = (
    "[0:v]split=2[a][b];"
    "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,scale=1080:1920[bg];"
    "[b]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
)
VERTICAL_FILL = "[0:v]scale=-2:1920,crop=1080:1920,setsar=1[v]"
VERTICAL = VERTICAL_FIT  # the default
LAYOUTS = {"fit": VERTICAL_FIT, "fill": VERTICAL_FILL}
WIDE = "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v]"

SOFTWARE_ENCODER = ("libx264", "-preset", "veryfast", "-crf", "23")


@lru_cache(maxsize=1)
def hardware_encoder() -> tuple[str, ...] | None:
    """Intel Quick Sync arguments if this machine can really use it, else None.

    Checked with a tiny test encode (an encoder being *listed* by ffmpeg proves nothing: it only
    works with matching hardware). Where it works it roughly halves the encoding time.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=320x240:rate=10",
             "-c:v", "h264_qsv", "-global_quality", "23", "-f", "null", "-"], capture_output=True, timeout=30)
        return ("h264_qsv", "-global_quality", "23") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def download_section(url: str, start: float, end: float, dest: Path,
                     extractors: tuple[str, ...] = (), exact: bool = True) -> Path:
    """Download only [start, end] of a video (a few MB, not the whole stream).

    `extractors` limits yt-dlp to the video's own platform (see YtDlpSource._opts). `exact` makes
    yt-dlp re-encode the slice so it starts precisely at `start`; that is slow (90 s of a 30 s clip,
    measured). Platforms where a plain cut was verified to start at the same frame turn it off.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        **({"allowed_extractors": list(extractors)} if extractors else {}),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": exact,
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
    encoders = []
    if settings.hardware_encoding and (hw := hardware_encoder()):
        encoders.append(hw)
    encoders.append(SOFTWARE_ENCODER)  # always the last resort, so a hardware hiccup never loses a clip
    last_error = ""
    for codec in encoders:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-filter_complex", graph,
               "-map", "[v]", "-map", "0:a?", "-c:v", *codec,
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
               *(extra or []), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
        last_error = r.stderr
        out.unlink(missing_ok=True)  # don't leave a half-written file from the failed attempt
    raise RuntimeError(f"ffmpeg failed: {last_error[-300:]}")


def make_formats(src: Path, out_dir: Path, name: str, layout: str = "fit") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    vertical, wide = out_dir / f"{name}_9x16.mp4", out_dir / f"{name}_16x9.mp4"
    _ffmpeg(src, LAYOUTS.get(layout, VERTICAL_FIT), vertical)
    _ffmpeg(src, WIDE, wide)
    return vertical, wide


def render_candidate(candidate_id: int) -> None:
    """Download the segment, render both formats, record the result. Safe to run in a background task."""
    with SessionLocal() as db:
        c = db.get(Candidate, candidate_id)
        c.render_status, c.render_error = "rendering", None
        db.commit()
        tmp = settings.raw_dir / f"clip_{c.id}"
        bounds = (c.start_s, c.end_s, c.layout)
        stale = False
        try:
            with _RENDER_LOCK:  # one at a time: encoding is heavy, and a laptop can't run several at once
                source = get_source(c.video.streamer.platform)
                src = download_section(c.video.url, c.start_s, c.end_s, tmp / "section.mp4",
                                       getattr(source, "extractor_names", ()),
                                       exact=not getattr(source, "fast_section_cuts", False))
                vertical, wide = make_formats(src, settings.clips_dir, f"clip_{c.id}", c.layout)
            db.refresh(c)  # pick up any trim or layout edit made while rendering
            c.vertical_path, c.wide_path = vertical.name, wide.name
            # if the trim or layout changed mid-render this output is stale: don't mark it usable
            stale = (c.start_s, c.end_s, c.layout) != bounds
            c.render_status = "none" if stale else "done"
        except Exception as e:
            c.render_status, c.render_error = "failed", f"{type(e).__name__}: {e}"[:500]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            db.commit()
        redo = stale and c.review_status == "approved"
    if redo:  # edited while it was being cut: replace the stale output rather than leave the clip uncut
        render_candidate(candidate_id)
