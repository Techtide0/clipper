"""URL parsing shared by platform detection and the adapters.

Hosts are compared after parsing (so uppercase hosts and explicit ports like :443 work) and the
channel is read from the path, never by pattern-matching the raw string.
"""
import re
from urllib.parse import ParseResult, unquote, urlparse

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*:)?//")


def parse_url(url: str) -> ParseResult:
    """urlparse that also accepts scheme-less input such as 'twitch.tv/name'."""
    url = url.strip()
    return urlparse(url if _SCHEME.match(url) else "//" + url)


def host_of(url: str) -> str:
    return (parse_url(url).hostname or "").lower()


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def path_segments(url: str) -> list[str]:
    return [s for s in parse_url(url).path.split("/") if s]


def channel_slug(url: str, domain: str, pattern: str, reserved: frozenset[str] = frozenset()) -> str:
    """First path segment of a channel URL on `domain`, validated against `pattern`."""
    if not host_matches(host_of(url), domain):
        raise ValueError(f"not a {domain} URL: {url}")
    segs = path_segments(url)
    slug = unquote(segs[0]) if segs else ""  # judge the decoded segment: '%2F' must not smuggle in a '/'
    if not slug or slug.lower() in reserved or not re.fullmatch(pattern, slug):
        raise ValueError(f"not a {domain} channel URL: {url}")
    return slug
