from pathlib import Path

from ..signals.chat import parse_live_chat_messages
from .base import VideoInfo
from .ytdlp import YtDlpSource


class YouTubeSource(YtDlpSource):
    platform = "youtube"

    def videos_url(self, channel_url: str) -> str:
        # The Streams tab holds past live streams (VODs) and current lives.
        url = channel_url.rstrip("/")
        return url if url.endswith("/streams") else url + "/streams"

    def extra_download_opts(self) -> dict:
        return {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "live_chat"],
            "subtitlesformat": "vtt/best",
            "ignoreerrors": "only_download",
        }

    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]:
        videos = super().list_videos(channel_url, limit)
        for v in videos:  # flat listings give ids; use the canonical watch URL
            v.url = f"https://www.youtube.com/watch?v={v.platform_id}"
        return videos

    def fetch_chat(self, video: VideoInfo, out_dir: Path):
        path = next(iter(out_dir.glob(f"{video.platform_id}*.live_chat.json")), None)
        return parse_live_chat_messages(path) if path else None
