import re
from pathlib import Path

from ..config import settings
from .base import VideoInfo
from .ytdlp import YtDlpSource, chat_via_chat_downloader

_CHANNEL = re.compile(r"kick\.com/([A-Za-z0-9_\-]+)")


class KickSource(YtDlpSource):
    platform = "kick"

    def videos_url(self, channel_url: str) -> str:
        m = _CHANNEL.search(channel_url)
        if not m:
            raise ValueError(f"not a Kick channel URL: {channel_url}")
        return f"https://kick.com/{m.group(1)}/videos"

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        # Best effort: returns None (no chat signal) if the replay isn't available.
        return chat_via_chat_downloader(video.url, settings.chat_timeout_s, settings.chat_idle_timeout_s)
