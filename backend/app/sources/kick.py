from pathlib import Path

from ..config import settings
from .base import VideoInfo
from .urls import channel_slug
from .ytdlp import YtDlpSource, chat_via_chat_downloader


class KickSource(YtDlpSource):
    platform = "kick"
    id_prefix = "kick-"
    extractor_names = ("kick.*",)

    def channel_urls(self, info: dict) -> tuple[str, ...]:
        # Kick VOD metadata carries no channel URL, only the channel's slug (uploader_id)
        slug = info.get("uploader_id")
        built = (f"https://kick.com/{slug}",) if isinstance(slug, str) and slug else ()
        return super().channel_urls(info) + built

    def videos_url(self, channel_url: str) -> str:
        name = channel_slug(channel_url, "kick.com", r"[A-Za-z0-9_\-]+", reserved=frozenset({"video", "videos"}))
        return f"https://kick.com/{name}/videos"

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        # Best effort: returns None (no chat signal) if the replay isn't available.
        return chat_via_chat_downloader(video.url, settings.chat_timeout_s, settings.chat_idle_timeout_s)
