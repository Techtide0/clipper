"""Is everything the app needs in place? Answers the question the owner would otherwise discover from an error."""
import os
import shutil
import subprocess

import yt_dlp

from . import editor, runtime
from .config import settings


def _dir_size(path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for e in entries:
                try:
                    total += _dir_size(e.path) if e.is_dir(follow_symlinks=False) else e.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _ffmpeg_version() -> str | None:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0].split(" Copyright")[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def check() -> dict:
    """Each check: name, ok, a plain detail, and what to do if it isn't ok. `ok` is true when nothing required is missing."""
    checks = []

    def add(name, ok, detail, fix="", required=True):
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix, "required": required})

    ff = _ffmpeg_version()
    add("Video tools (ffmpeg)", bool(ff), ff or "ffmpeg wasn't found",
        "Install ffmpeg (for example: winget install Gyan.FFmpeg) and restart the app.")
    add("Downloader (yt-dlp)", True, f"version {yt_dlp.version.__version__}")
    key = bool(settings.gemini_api_key)
    add("AI scoring (Gemini)", key,
        f"key set; main model {settings.gemini_model}, backup {', '.join(settings.gemini_fallback_models) or 'none'}"
        if key else "no API key set",
        "Put GEMINI_API_KEY=... in backend/.env and restart the app.")
    free = shutil.disk_usage(settings.data_dir).free
    add("Free disk space", free > 5e9, f"{free / 1e9:.1f} GB free",
        "Free some space, delete full-video copies you don't need, or move the project.")
    add("Hardware video encoding", True,
        "using Intel Quick Sync (faster cutting)" if settings.hardware_encoding and editor.hardware_encoder()
        else "not available on this machine; cutting uses the software encoder (slower, still fine)", required=False)
    folders = {"clips": settings.clips_dir, "exports": settings.exports_dir, "full videos": settings.downloads_dir,
               "moment audio": settings.moments_dir, "working files": settings.raw_dir}
    return {
        "ok": all(c["ok"] or not c["required"] for c in checks),
        "checks": checks,
        "storage_mb": {name: round(_dir_size(p) / 1e6, 1) for name, p in folders.items()},
        "free_disk_gb": round(free / 1e9, 1),
        "settings": runtime.all_values(),
    }
