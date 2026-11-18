import time

import pytest

from app.sources import SOURCES, detect_platform
from app.sources.ytdlp import collect_with_watchdog


def test_watchdog_collects_messages():
    msgs = [{"time_in_seconds": 1.5, "message": "hi"}, {"time_in_seconds": -3, "message": "skip"},
            {"time_in_seconds": 4, "message": None}]
    assert collect_with_watchdog(lambda: iter(msgs), timeout_s=5, idle_s=5) == [(1.5, "hi"), (4.0, "")]


def test_watchdog_gives_up_when_the_library_hangs_before_any_message():
    def hang():  # like chat-downloader retrying a broken API internally
        time.sleep(30)
        yield {}
    start = time.monotonic()
    assert collect_with_watchdog(hang, timeout_s=20, idle_s=0.5) is None
    assert time.monotonic() - start < 5


def test_watchdog_enforces_total_timeout_and_discards_partial():
    def endless():
        while True:
            time.sleep(0.05)
            yield {"time_in_seconds": 1, "message": "x"}
    assert collect_with_watchdog(endless, timeout_s=0.5, idle_s=5) is None


def test_watchdog_returns_none_on_error():
    def boom():
        raise KeyError("data")
        yield
    assert collect_with_watchdog(boom, timeout_s=5, idle_s=5) is None


@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/@somebody", "youtube"),
    ("https://youtube.com/channel/UC123", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://www.twitch.tv/somebody", "twitch"),
    ("twitch.tv/somebody", "twitch"),
    ("https://kick.com/somebody", "kick"),
])
def test_detect_platform(url, platform):
    assert detect_platform(url) == platform


@pytest.mark.parametrize("url", ["https://example.com/x", "https://nottwitch.tv/x", "https://evil.com/twitch.tv"])
def test_detect_platform_rejects_unknown_and_lookalike_hosts(url):
    with pytest.raises(ValueError):
        detect_platform(url)


def test_videos_urls():
    assert SOURCES["youtube"].videos_url("https://www.youtube.com/@a/") == "https://www.youtube.com/@a/streams"
    assert SOURCES["youtube"].videos_url("https://www.youtube.com/@a/streams") == "https://www.youtube.com/@a/streams"
    assert SOURCES["twitch"].videos_url("https://www.twitch.tv/Some_One") == \
        "https://www.twitch.tv/Some_One/videos?filter=archives"
    assert SOURCES["kick"].videos_url("https://kick.com/some-one") == "https://kick.com/some-one/videos"
    with pytest.raises(ValueError):
        SOURCES["twitch"].videos_url("https://www.twitch.tv/videos/123")  # a video, not a channel
