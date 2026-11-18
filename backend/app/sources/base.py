from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoInfo:
    platform_id: str
    title: str
    url: str
    duration_s: int | None = None


@dataclass
class VideoMeta:
    """What we learn about a single video from its link, before downloading anything."""
    video: VideoInfo
    channel_name: str | None = None
    channel_url: str | None = None


@dataclass
class Downloaded:
    audio: Path
    # (seconds from video start, text) for every chat message, or None if the platform has no
    # chat replay (or fetching it failed). The pipeline simply skips the chat signal then.
    chat_messages: list[tuple[float, str]] | None = None
    captions: Path | None = None   # subtitle file (.vtt), if available


class Source(ABC):
    """One adapter per platform. Add a platform by subclassing this and registering it."""

    platform: str
    # Prepended to this platform's video ids when stored (see YtDlpSource); empty means none.
    id_prefix: str = ""

    def fetch_video(self, url: str) -> VideoMeta:
        """Metadata for one video link. Raises ValueError with a user-readable reason if unusable."""
        raise NotImplementedError

    @abstractmethod
    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]: ...

    @abstractmethod
    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded: ...
