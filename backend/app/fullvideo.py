"""Optional full-length download of a video, for when you want the whole original, not just the cuts.

Nothing else in the app keeps a full video (it only needs the audio, briefly), and a full video can
be several GB, so this is strictly opt-in, shows its progress, refuses to fill the disk, and can be
deleted again from the dashboard.
"""
import re
import shutil
import threading
from pathlib import Path

import yt_dlp
from sqlalchemy import update

from . import runtime
from .config import settings
from .db import SessionLocal, Video
from .editor import slugify
from .sources import get_source

_LOCK = threading.Lock()  # one full download at a time; others wait (and show as queued)
ESTIMATE_BYTES_PER_S = 1_000_000  # about what 1080p costs per second of video, rounded up
BUSY = ("queued", "downloading")


def _set(video_id: int, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(Video).where(Video.id == video_id).values(**values))
        db.commit()


def claim_full(video_id: int) -> bool:
    """Atomically queue a full download. True only for the one request that wins; a video that is
    already queued, downloading or downloaded is not queued again."""
    with SessionLocal() as db:
        done = db.execute(update(Video).where(
            Video.id == video_id, Video.full_status.in_(("none", "failed")),
        ).values(full_status="queued", full_progress=0, full_error=None))
        db.commit()
        return done.rowcount == 1


def full_path_of(stored: str | None) -> Path | None:
    """The file for a stored name, or None unless it is a real file inside the downloads folder."""
    if not stored:
        return None
    root = settings.downloads_dir.resolve()
    path = (root / stored).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def download_name(title: str | None, video_id: int) -> str:
    return f"{slugify(title, f'video-{video_id}')}.mp4"


def _progress_hook(video_id: int):
    last = {"pct": 0}

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            pct = int(d.get("downloaded_bytes", 0) * 100 / total)
        elif d.get("fragment_count"):
            pct = int((d.get("fragment_index") or 0) * 100 / d["fragment_count"])
        else:
            return
        pct = min(pct, 99)  # 100 only once the file is complete
        if pct >= last["pct"] + 2:  # video and audio download separately; never move backwards, and don't spam
            last["pct"] = pct
            try:
                _set(video_id, full_progress=pct)
            except Exception:
                pass  # progress is cosmetic; never let it break the download

    return hook


def _remove_partials(stem: Path) -> None:
    for p in stem.parent.glob(f"{stem.name}.*"):  # '.': 'twitch-v123.*' must never match 'twitch-v1234.mp4'
        try:
            p.unlink()
        except OSError:
            pass


def download_full(video_id: int) -> None:
    """Download the whole video into the downloads folder. Run after claim_full succeeded."""
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        url, pid, duration, platform = v.url, v.platform_id, v.duration_s, v.streamer.platform
    stem = settings.downloads_dir / re.sub(r"[^\w\-]", "_", pid)
    try:
        with _LOCK:
            _set(video_id, full_status="downloading")
            settings.downloads_dir.mkdir(parents=True, exist_ok=True)
            need = (duration or 0) * ESTIMATE_BYTES_PER_S + runtime.get("full_download_headroom_gb") * 1e9
            free = shutil.disk_usage(settings.downloads_dir).free
            if free < need:
                raise OSError(f"not enough disk space: about {need / 1e9:.1f} GB needed (including a "
                              f"{runtime.get("full_download_headroom_gb"):g} GB safety margin), {free / 1e9:.1f} GB free")
            opts = {
                "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
                "merge_output_format": "mp4",
                "outtmpl": f"{stem}.%(ext)s",
                "quiet": True, "noprogress": True, "socket_timeout": 30,
                "progress_hooks": [_progress_hook(video_id)],
            }
            names = getattr(get_source(platform), "extractor_names", ())
            if names:
                opts["allowed_extractors"] = list(names)  # only this platform's extractors, as everywhere else
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        path = stem.with_suffix(".mp4")
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("the download finished but produced no file")
        _set(video_id, full_status="done", full_path=path.name, full_progress=100, full_error=None)
    except Exception as e:
        _remove_partials(stem)  # a half-downloaded multi-GB file helps nobody
        _set(video_id, full_status="failed", full_path=None, full_progress=None,
             full_error=f"{type(e).__name__}: {e}"[:500])


def delete_full(video_id: int) -> bool:
    """Delete the downloaded copy to free the space. False if there was nothing to delete."""
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        if v is None or v.full_status != "done":
            return False
        stored = v.full_path
    path = full_path_of(stored)
    if path:
        path.unlink(missing_ok=True)
    _set(video_id, full_status="none", full_path=None, full_progress=None, full_error=None)
    return True


def recover_full() -> int:
    """Downloads left queued/downloading by a process that died can never finish: mark them failed
    (so Retry appears) and remove their half-written files. Call at server start."""
    with SessionLocal() as db:
        done = db.execute(update(Video).where(Video.full_status.in_(BUSY)).values(
            full_status="failed", full_progress=None, full_error="interrupted by a restart; press Retry"))
        db.commit()
    for p in settings.downloads_dir.glob("*.part*"):
        p.unlink(missing_ok=True)
    for p in settings.downloads_dir.glob("*.ytdl"):
        p.unlink(missing_ok=True)
    return done.rowcount
