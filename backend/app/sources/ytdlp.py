"""Shared yt-dlp logic. YouTube, Twitch and Kick differ only in a few small hooks."""
import json
import multiprocessing
import re
import tempfile
import time
from pathlib import Path

import yt_dlp

from .base import Downloaded, Source, VideoInfo

_ABSOLUTE = re.compile(r"^https?://", re.I)


class YtDlpSource(Source):
    # Prepended to platform ids so ids from different sites can never collide in the database
    # or in the raw-download folder. Empty for YouTube, whose ids are already distinctive.
    id_prefix = ""

    def videos_url(self, channel_url: str) -> str:
        """The page that lists a channel's past broadcasts."""
        return channel_url

    def extra_download_opts(self) -> dict:
        return {}

    def fetch_chat(self, video: VideoInfo, out_dir: Path) -> list[tuple[float, str]] | None:
        return None

    def find_captions(self, video: VideoInfo, out_dir: Path) -> Path | None:
        return next(iter(out_dir.glob(f"{video.platform_id}*.vtt")), None)

    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]:
        opts = {"extract_flat": True, "playlistend": limit, "quiet": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.videos_url(channel_url), download=False)
        out = []
        for e in info.get("entries", []) or []:
            if not e or not e.get("id"):
                continue
            # only absolute http(s) URLs are kept: they are stored, opened in the dashboard and
            # fed back to yt-dlp, so an opaque id or relative path would break all three
            url = next((u for u in (e.get("url"), e.get("webpage_url")) if u and _ABSOLUTE.match(u)), None)
            if not url:
                continue
            out.append(VideoInfo(
                platform_id=f"{self.id_prefix}{e['id']}",
                title=e.get("title") or str(e["id"]),
                url=url,
                duration_s=int(e["duration"]) if e.get("duration") else None,
            ))
        return out

    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded:
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / f"{video.platform_id}.%(ext)s"),
            "quiet": True,
            "noprogress": True,
            # 16 kHz mono wav: tiny and all the signal analysis needs
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
            "postprocessor_args": {"extractaudio": ["-ac", "1", "-ar", "16000"]},
            **self.extra_download_opts(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video.url])
        audio = out_dir / f"{video.platform_id}.wav"
        if not audio.exists():
            raise RuntimeError("audio download failed")
        # chat and captions are optional extras: a failure in either must never lose the audio
        try:
            chat = self.fetch_chat(video, out_dir)
        except Exception:
            chat = None
        try:
            captions = self.find_captions(video, out_dir)
        except Exception:
            captions = None
        return Downloaded(audio=audio, chat_messages=chat, captions=captions)


# --- chat replay through the chat-downloader library, in a killable child process -----------

def _chat_worker(url: str, out_path: str) -> None:
    """Runs in a child process: stream messages to a file as JSON lines."""
    from chat_downloader import ChatDownloader
    chat = ChatDownloader().get_chat(url, message_types=["text_message"])
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            for m in chat:
                t = m.get("time_in_seconds")
                if t is not None and t >= 0:
                    f.write(json.dumps([float(t), str(m.get("message") or "")]) + "\n")
                    f.flush()
    finally:
        close = getattr(chat, "close", None)
        if close:
            close()


def run_chat_process(target, args: tuple, timeout_s: float, idle_s: float) -> list[tuple[float, str]] | None:
    """Run `target(*args, out_path)` in a child process and read the JSON lines it writes.

    Returns None if the whole run exceeds timeout_s, if the output stops growing for idle_s,
    or if the child fails. The child is terminated and reaped in every case, so nothing keeps
    running or holding messages after we give up. This matters because chat-downloader retries
    a broken API for minutes inside its own code, where no in-process check could interrupt it.
    A partial replay is discarded on purpose: chat covering only the start of a stream would
    make the rest look quiet and skew the scoring. A replay that finishes is always accepted,
    however late we notice.
    """
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="clipper_chat_") as tmp:
        out = Path(tmp) / "chat.jsonl"
        proc = ctx.Process(target=target, args=(*args, str(out)), daemon=True)
        proc.start()
        start = last_growth = time.monotonic()
        size = 0
        try:
            while proc.is_alive():
                proc.join(0.2)
                if not proc.is_alive():
                    break
                now = time.monotonic()
                # judge against the previous deadline *before* crediting new output
                if now - start > timeout_s or now - last_growth > idle_s:
                    return None
                cur = out.stat().st_size if out.exists() else 0
                if cur != size:
                    size, last_growth = cur, now
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        if proc.exitcode != 0 or not out.exists():
            return None
        try:
            return [(float(t), str(text)) for t, text in
                    (json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line)]
        except (ValueError, TypeError):
            return None


def chat_via_chat_downloader(url: str, timeout_s: float, idle_s: float = 90) -> list[tuple[float, str]] | None:
    """Chat replay for a VOD URL, or None if unavailable (unsupported site, broken API, too slow)."""
    return run_chat_process(_chat_worker, (url,), timeout_s, idle_s)
