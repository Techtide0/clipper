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
class Downloaded:
    audio: Path
    # (seconds from video start, text) for every chat message, or None if the platform has no
    # chat replay (or fetching it failed). The pipeline simply skips the chat signal then.
    chat_messages: list[tuple[float, str]] | None = None
    captions: Path | None = None   # subtitle file (.vtt), if available


class Source(ABC):
    """One adapter per platform. Add a platform by subclassing this and registering it."""

    platform: str

    @abstractmethod
    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]: ...

    @abstractmethod
    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded: ...
