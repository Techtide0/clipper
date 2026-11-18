import re
from pathlib import Path

from ..config import settings
from .base import VideoInfo
from .ytdlp import YtDlpSource, chat_via_chat_downloader

_CHANNEL = re.compile(r"twitch\.tv/(?!videos/)([A-Za-z0-9_]+)")


class TwitchSource(YtDlpSource):
    platform = "twitch"

    def videos_url(self, channel_url: str) -> str:
        m = _CHANNEL.search(channel_url)
        if not m:
            raise ValueError(f"not a Twitch channel URL: {channel_url}")
        return f"https://www.twitch.tv/{m.group(1)}/videos?filter=archives"  # past broadcasts only

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        return chat_via_chat_downloader(video.url, settings.chat_timeout_s, settings.chat_idle_timeout_s)
