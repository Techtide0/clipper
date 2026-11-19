import re
from pathlib import Path
from urllib.parse import urlunparse

import yt_dlp

from ..signals.chat import parse_live_chat_messages
from .base import VideoInfo
from .urls import parse_url
from .ytdlp import YtDlpSource

_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class YouTubeSource(YtDlpSource):
    platform = "youtube"
    extractor_names = ("youtube.*",)
    # Measured on a real video: a plain cut starts at the same frame as the exact (re-encoded) one
    # (best alignment at 0.00 s offset) and downloads 2.8x faster. Not verified for other platforms.
    fast_section_cuts = True

    def videos_url(self, channel_url: str) -> str:
        # The Streams tab holds past live streams (VODs) and current lives.
        u = parse_url(channel_url)
        path = u.path.rstrip("/")
        if not path.endswith("/streams"):
            path += "/streams"  # appended to the path, so a ?query or #fragment stays where it was
        return urlunparse((u.scheme or "https", u.netloc, path, u.params, u.query, u.fragment))

    def extra_download_opts(self) -> dict:
        return {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "live_chat"],
            "subtitlesformat": "vtt/best",
            "ignoreerrors": "only_download",
        }

    def resolve_channel(self, channel_url: str) -> tuple[str, ...]:
        # YouTube returns the same channel_id/channel_url/uploader_url whether the channel was
        # asked for by @handle or by /channel/UC..., which is what lets the two spellings be matched
        opts = self._opts(quiet=True, extract_flat="in_playlist", playlistend=1, skip_download=True,
                          socket_timeout=20)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.videos_url(channel_url), download=False)
        return self.channel_urls(info or {})

    def entry_url(self, e: dict) -> str | None:
        # Always the canonical watch URL, built from the id: a flat listing may carry only an
        # opaque id in `url`, so we don't depend on what it contains.
        vid = str(e.get("id") or "")
        return f"https://www.youtube.com/watch?v={vid}" if _YT_ID.fullmatch(vid) else None

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        path = next(iter(out_dir.glob(f"{video.platform_id}*.live_chat.json")), None)
        return parse_live_chat_messages(path) if path else None
