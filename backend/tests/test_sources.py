import multiprocessing
import time

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.sources import SOURCES, detect_platform
from app.sources.ytdlp import run_chat_process
from tests import chat_workers


# ---- platform detection and URL handling ---------------------------------------------------

@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/@somebody", "youtube"),
    ("https://youtube.com/channel/UC123", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://www.twitch.tv/somebody", "twitch"),
    ("twitch.tv/somebody", "twitch"),
    ("HTTPS://WWW.TWITCH.TV:443/somebody", "twitch"),
    ("https://kick.com/somebody", "kick"),
    ("//kick.com/somebody", "kick"),
])
def test_detect_platform(url, platform):
    assert detect_platform(url) == platform


@pytest.mark.parametrize("url", [
    "https://example.com/x", "https://nottwitch.tv/x", "https://evil.com/twitch.tv",
    "evil.com/x?next=http://twitch.tv/a",  # '//' appears later in the string, but the host is evil.com
])
def test_detect_platform_rejects_unknown_and_lookalike_hosts(url):
    with pytest.raises(ValueError):
        detect_platform(url)


def test_youtube_videos_url_appends_to_the_path_only():
    yt = SOURCES["youtube"]
    assert yt.videos_url("https://www.youtube.com/@a/") == "https://www.youtube.com/@a/streams"
    assert yt.videos_url("https://www.youtube.com/@a/streams") == "https://www.youtube.com/@a/streams"
    assert yt.videos_url("https://www.youtube.com/@a?si=xyz#top") == "https://www.youtube.com/@a/streams?si=xyz#top"
    assert yt.videos_url("youtube.com/@a") == "https://youtube.com/@a/streams"


@pytest.mark.parametrize("url", [
    "https://www.twitch.tv/Some_One", "HTTPS://WWW.TWITCH.TV:443/Some_One",
    "twitch.tv/Some_One", "https://www.twitch.tv/Some_One/videos", "https://www.twitch.tv/Some_One/videos?filter=all",
])
def test_twitch_videos_url_is_consistent(url):
    assert SOURCES["twitch"].videos_url(url) == "https://www.twitch.tv/Some_One/videos?filter=archives"


def test_twitch_rejects_single_video_urls():
    for url in ("https://www.twitch.tv/videos/123", "https://www.twitch.tv/", "https://kick.com/x"):
        with pytest.raises(ValueError):
            SOURCES["twitch"].videos_url(url)


@pytest.mark.parametrize("url", ["https://kick.com/some-one", "HTTPS://KICK.COM:443/some-one/", "kick.com/some-one/videos"])
def test_kick_videos_url_is_consistent(url):
    assert SOURCES["kick"].videos_url(url) == "https://kick.com/some-one/videos"


# ---- listing: qualified ids and absolute urls ----------------------------------------------

class _FakeYDL:
    entries = []

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {"entries": self.entries}


def test_list_videos_qualifies_ids_and_drops_non_absolute_urls(monkeypatch):
    import app.sources.ytdlp as mod
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", _FakeYDL)
    _FakeYDL.entries = [
        {"id": "123", "title": "ok", "url": "https://x.example/v/123", "duration": 5},
        {"id": "456", "title": "relative", "url": "/videos/456"},
        {"id": "789", "title": "opaque", "url": "789"},
        {"id": "abc", "title": "fallback", "webpage_url": "https://x.example/v/abc"},
    ]
    twitch = SOURCES["twitch"].list_videos("https://www.twitch.tv/x")
    assert [v.platform_id for v in twitch] == ["twitch-123", "twitch-abc"]
    kick = SOURCES["kick"].list_videos("https://kick.com/x")
    assert kick[0].platform_id == "kick-123"  # same raw id on another site can't collide
    yt = SOURCES["youtube"].list_videos("https://www.youtube.com/@x")
    assert yt[0].platform_id == "123" and yt[0].url == "https://www.youtube.com/watch?v=123"


# ---- chat replay in a child process --------------------------------------------------------

def test_chat_process_returns_messages_with_timeline_seconds():
    assert run_chat_process(chat_workers.ok, (), timeout_s=60, idle_s=30) == [
        (90.0, "replay at 90 seconds"), (91.5, "second")]


def test_chat_process_accepts_a_replay_that_pauses_but_finishes():
    assert run_chat_process(chat_workers.slow_but_finishes, (), timeout_s=60, idle_s=30) == [(1.0, "a"), (2.0, "b")]


def test_chat_process_gives_up_on_silence_and_kills_the_child():
    start = time.monotonic()
    assert run_chat_process(chat_workers.hang_before_any_message, (), timeout_s=60, idle_s=1.0) is None
    assert time.monotonic() - start < 15
    assert not multiprocessing.active_children()  # terminated and reaped, not left running


def test_chat_process_total_timeout_discards_partial_and_kills_the_child():
    assert run_chat_process(chat_workers.endless, (), timeout_s=1.5, idle_s=30) is None
    assert not multiprocessing.active_children()


def test_chat_process_returns_none_when_the_worker_fails():
    assert run_chat_process(chat_workers.explode, (), timeout_s=60, idle_s=30) is None


# ---- settings ------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["chat_timeout_s", "chat_idle_timeout_s"])
@pytest.mark.parametrize("bad", [0, -5, float("inf"), float("nan")])
def test_chat_timeouts_must_be_positive_and_finite(field, bad):
    with pytest.raises(ValidationError):
        Settings(**{field: bad})


def test_chat_timeout_defaults_unchanged():
    s = Settings()
    assert (s.chat_timeout_s, s.chat_idle_timeout_s) == (900, 90)
