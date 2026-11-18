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
    chat: Path | None = None       # raw chat replay file, if the platform has one
    captions: Path | None = None   # subtitle file (.vtt), if available


class Source(ABC):
    """One adapter per platform. Add Twitch/Kick by subclassing this."""

    platform: str

    @abstractmethod
    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]: ...

    @abstractmethod
    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded: ...
