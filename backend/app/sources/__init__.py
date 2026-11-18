from urllib.parse import urlparse

from .base import Source
from .kick import KickSource
from .twitch import TwitchSource
from .youtube import YouTubeSource

SOURCES: dict[str, Source] = {s.platform: s for s in (YouTubeSource(), TwitchSource(), KickSource())}

_HOSTS = {
    "youtube.com": "youtube", "youtu.be": "youtube",
    "twitch.tv": "twitch",
    "kick.com": "kick",
}


def detect_platform(url: str) -> str:
    """Platform key from a channel URL. Raises ValueError for sites we have no adapter for."""
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    for domain, platform in _HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    raise ValueError(f"unsupported site: {url!r} (supported: {', '.join(sorted(SOURCES))})")


def get_source(platform: str) -> Source:
    return SOURCES[platform]
