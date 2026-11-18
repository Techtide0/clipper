from pathlib import Path

from ..config import settings
from .base import VideoInfo
from .urls import channel_slug
from .ytdlp import YtDlpSource, chat_via_chat_downloader


class TwitchSource(YtDlpSource):
    platform = "twitch"
    id_prefix = "twitch-"

    def videos_url(self, channel_url: str) -> str:
        # twitch.tv/videos/123 is a single video, not a channel
        name = channel_slug(channel_url, "twitch.tv", r"[A-Za-z0-9_]+", reserved=frozenset({"videos"}))
        return f"https://www.twitch.tv/{name}/videos?filter=archives"  # past broadcasts only

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        return chat_via_chat_downloader(video.url, settings.chat_timeout_s, settings.chat_idle_timeout_s)
