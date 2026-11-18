"""Shared yt-dlp logic. YouTube, Twitch and Kick all differ only in a few small hooks."""
import threading
import time
from pathlib import Path

import yt_dlp

from .base import Downloaded, Source, VideoInfo


class YtDlpSource(Source):
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
            out.append(VideoInfo(
                platform_id=str(e["id"]),
                title=e.get("title") or str(e["id"]),
                url=e.get("url") or e.get("webpage_url") or "",
                duration_s=int(e["duration"]) if e.get("duration") else None,
            ))
        return [v for v in out if v.url]

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
        return Downloaded(audio=audio, chat_messages=self.fetch_chat(video, out_dir),
                          captions=self.find_captions(video, out_dir))


def collect_with_watchdog(make_iter, timeout_s: float, idle_s: float) -> list[tuple[float, str]] | None:
    """Collect chat messages from make_iter() on a background thread, with hard time limits.

    Gives up (returns None) if the whole thing takes longer than timeout_s, if nothing new
    arrives for idle_s, or on any error. The idle limit matters because chat-downloader retries
    a broken API with delays of up to minutes *inside* its own code, where a check between
    messages would never run. A partial replay is discarded on purpose: chat covering only the
    start of a stream would make the rest look quiet and skew the scoring.
    """
    out: list[tuple[float, str]] = []
    state = {"done": False, "error": None, "last": time.monotonic()}

    def work() -> None:
        try:
            for m in make_iter():
                t = m.get("time_in_seconds")
                if t is not None and t >= 0:
                    out.append((float(t), str(m.get("message") or "")))
                state["last"] = time.monotonic()
        except Exception as e:  # unsupported site, network error, API change...
            state["error"] = e
        finally:
            state["done"] = True

    thread = threading.Thread(target=work, daemon=True)  # abandoned (not killed) if we give up
    start = time.monotonic()
    state["last"] = start
    thread.start()
    while not state["done"]:
        thread.join(0.1)
        now = time.monotonic()
        if now - start > timeout_s or now - state["last"] > idle_s:
            return None
    return None if state["error"] else out


def chat_via_chat_downloader(url: str, timeout_s: float, idle_s: float = 90) -> list[tuple[float, str]] | None:
    """Chat replay through the chat-downloader library, or None if unavailable."""
    def make_iter():
        from chat_downloader import ChatDownloader
        return ChatDownloader().get_chat(url, message_types=["text_message"])

    return collect_with_watchdog(make_iter, timeout_s, idle_s)

