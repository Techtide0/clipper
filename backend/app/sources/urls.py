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


def web_url(url: str) -> str:
    """A cleaned-up http(s) URL from user input, or ValueError.

    Scheme-less input gets https://. Any other scheme (file:, ftp:, javascript:...) is refused,
    so a pasted link can never make us read a local file or talk a non-web protocol.
    """
    url = url.strip()
    if not url or len(url) > 500:
        raise ValueError("paste a video link")
    u = parse_url(url)
    try:
        u.port  # 'javascript:alert(1)' and 'data:text/html,x' parse as host:port with a non-numeric port
    except ValueError:
        raise ValueError("that doesn't look like a web link (it must start with http:// or https://)") from None
    if u.scheme not in ("", "http", "https") or not u.hostname:
        raise ValueError("that doesn't look like a web link (it must start with http:// or https://)")
    return url if u.scheme else "https://" + url.lstrip("/")


_CASE_INSENSITIVE_HOSTS = ("twitch.tv", "kick.com")


def canonical_channel(url: str) -> str:
    """A comparable form of a channel URL, so equivalent spellings match.

    Ignores scheme, 'www.', query, fragment, a trailing slash and a trailing /streams or /videos
    tab. Names are case-insensitive on Twitch and Kick and for YouTube @handles, but YouTube
    channel ids (UC...) are case-sensitive and are left alone.
    """
    u = parse_url(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    path = u.path.rstrip("/")
    for tab in ("/streams", "/videos"):
        if path.lower().endswith(tab):
            path = path[: -len(tab)]
    if any(host_matches(host, d) for d in _CASE_INSENSITIVE_HOSTS) or path.startswith("/@"):
        path = path.lower()
    return f"{host}{path}"


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
