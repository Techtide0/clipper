import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import judge as judgemod
from app import pipeline
from app.config import settings
from app.main import app
from app.sources import SOURCES
from app.sources.base import Source, VideoInfo, VideoMeta
from app.sources.urls import web_url


# ---- link cleaning -------------------------------------------------------------------------

@pytest.mark.parametrize("raw,clean", [
    ("https://youtu.be/abc", "https://youtu.be/abc"),
    ("  https://www.youtube.com/watch?v=abc  ", "https://www.youtube.com/watch?v=abc"),
    ("youtube.com/watch?v=abc", "https://youtube.com/watch?v=abc"),
    ("//twitch.tv/videos/1", "https://twitch.tv/videos/1"),
])
def test_web_url_accepts_web_links(raw, clean):
    assert web_url(raw) == clean


@pytest.mark.parametrize("bad", ["", "   ", "file:///etc/passwd", "ftp://youtube.com/x", "javascript:alert(1)",
                                 "data:text/html,hi", "http://", "x" * 501])
def test_web_url_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        web_url(bad)


# ---- reading one video's metadata ---------------------------------------------------------

class _FakeYDL:
    info = {}
    error = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if self.error:
            raise self.error
        return self.info


@pytest.fixture()
def fake_ydl(monkeypatch):
    import app.sources.ytdlp as mod
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", _FakeYDL)
    _FakeYDL.error = None
    return _FakeYDL


GOOD = {"id": "abcdefghijk", "title": "A video", "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "duration": 600, "uploader": "Some Channel", "channel_url": "https://www.youtube.com/channel/UC1",
        "live_status": "not_live"}


def test_fetch_video_reads_a_single_video(fake_ydl):
    fake_ydl.info = GOOD
    m = SOURCES["youtube"].fetch_video("https://youtu.be/abcdefghijk")
    assert (m.video.platform_id, m.video.url, m.video.duration_s) == (
        "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk", 600)
    assert (m.channel_name, m.channel_url) == ("Some Channel", "https://www.youtube.com/channel/UC1")


def test_fetch_video_never_expands_playlists(fake_ydl, monkeypatch):
    """Regression: a channel link used to make yt-dlp load every video on the channel."""
    seen = {}
    original_init = _FakeYDL.__init__

    def spy(self, opts):
        seen.update(opts)
        original_init(self, opts)

    monkeypatch.setattr(_FakeYDL, "__init__", spy)
    fake_ydl.info = GOOD
    SOURCES["youtube"].fetch_video("https://youtu.be/abcdefghijk")
    assert seen["extract_flat"] == "in_playlist" and seen["playlistend"] == 1 and seen["socket_timeout"] <= 30


def test_fetch_video_qualifies_non_youtube_ids(fake_ydl):
    fake_ydl.info = {**GOOD, "id": "v123", "webpage_url": "https://www.twitch.tv/videos/123"}
    assert SOURCES["twitch"].fetch_video("https://www.twitch.tv/videos/123").video.platform_id == "twitch-v123"


@pytest.mark.parametrize("info,fragment", [
    ({"_type": "playlist", "entries": [{}]}, "channel or playlist"),
    ({**GOOD, "entries": []}, "channel or playlist"),
    ({**GOOD, "live_status": "is_live"}, "hasn't finished"),
    ({**GOOD, "live_status": "is_upcoming"}, "hasn't finished"),
    ({**GOOD, "webpage_url": "not a url"}, "usable video"),
    ({"title": "no id"}, "usable video"),
    (None, "couldn't read"),
])
def test_fetch_video_rejects_unusable_results(fake_ydl, info, fragment):
    fake_ydl.info = info
    with pytest.raises(ValueError, match=fragment):
        SOURCES["youtube"].fetch_video("https://youtu.be/x")


def test_a_network_failure_is_not_reported_as_a_bad_link(fake_ydl):
    import yt_dlp
    from yt_dlp.networking.exceptions import TransportError
    cause = TransportError("Failed to resolve 'gql.twitch.tv'")
    fake_ydl.error = yt_dlp.utils.DownloadError("ERROR: Unable to download JSON metadata", (TransportError, cause, None))
    with pytest.raises(ConnectionError, match="try again"):
        SOURCES["youtube"].fetch_video("https://youtu.be/x")


def test_api_answers_502_for_a_network_failure_and_422_for_a_bad_link(monkeypatch):
    def offline(url, content_type="gameplay"):
        raise ConnectionError("couldn't reach the site (network problem); try again in a moment")

    monkeypatch.setattr(pipeline, "ingest_url", offline)
    with TestClient(app) as c:
        r = c.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"})
    assert r.status_code == 502 and "try again" in r.json()["detail"]


def test_fetch_video_turns_a_download_error_into_a_readable_message(fake_ydl):
    import yt_dlp
    fake_ydl.error = yt_dlp.utils.DownloadError("ERROR: Video unavailable\nsecond line with detail")
    with pytest.raises(ValueError, match="Video unavailable") as e:
        SOURCES["youtube"].fetch_video("https://youtu.be/x")
    assert "second line" not in str(e.value)


# ---- registering and analyzing ------------------------------------------------------------

class FakeYT(Source):
    platform = "youtube"
    meta = None

    def fetch_video(self, url):
        return self.meta

    def list_videos(self, channel_url, limit=10):
        return []

    def download(self, video, out_dir):
        raise NotImplementedError


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)
    monkeypatch.setattr(judgemod, "SessionLocal", Session)
    fake = FakeYT()
    fake.meta = VideoMeta(VideoInfo("abcdefghijk", "A video", "https://www.youtube.com/watch?v=abcdefghijk", 600),
                          "Some Channel", "https://www.youtube.com/channel/UC1")
    monkeypatch.setattr(pipeline, "get_source", lambda platform: fake)
    return Session, fake


def test_ingest_creates_an_untracked_channel_and_a_video(env):
    Session, _ = env
    vid, created = pipeline.ingest_url("https://youtu.be/abcdefghijk", "news")
    assert created
    with Session() as s:
        v = s.get(dbmod.Video, vid)
        assert (v.platform_id, v.status, v.title) == ("abcdefghijk", "new", "A video")
        st = v.streamer
        assert (st.name, st.platform, st.content_type, st.tracked) == ("Some Channel", "youtube", "news", False)


def test_ingest_twice_does_not_duplicate(env):
    Session, _ = env
    first = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    again = pipeline.ingest_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert again == (first[0], False)
    with Session() as s:
        assert len(list(s.scalars(select(dbmod.Video)))) == 1


def test_ingest_reuses_a_channel_you_already_track(env):
    Session, _ = env
    with Session() as s:
        s.add(dbmod.Streamer(name="Mine", channel_url="https://www.youtube.com/channel/UC1", platform="youtube"))
        s.commit()
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    with Session() as s:
        streamers = list(s.scalars(select(dbmod.Streamer)))
        assert len(streamers) == 1 and streamers[0].tracked is True  # still followed, not downgraded
        assert s.get(dbmod.Video, vid).streamer_id == streamers[0].id


def test_ingest_without_a_channel_uses_a_stand_in(env):
    Session, fake = env
    fake.meta = VideoMeta(VideoInfo("zzzzzzzzzzz", "Orphan", "https://www.youtube.com/watch?v=zzzzzzzzzzz"))
    vid, _ = pipeline.ingest_url("https://youtu.be/zzzzzzzzzzz")
    with Session() as s:
        st = s.get(dbmod.Video, vid).streamer
        assert (st.name, st.channel_url, st.tracked) == ("Added by link", "link:youtube", False)


@pytest.mark.parametrize("url", ["https://example.com/watch?v=1", "file:///etc/passwd", "https://notyoutube.com/x", ""])
def test_ingest_refuses_other_sites_and_schemes(env, url):
    with pytest.raises(ValueError):
        pipeline.ingest_url(url)


def test_analyze_and_judge_records_a_judge_failure_without_losing_the_analysis(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    monkeypatch.setattr(pipeline, "process_video", lambda v: 3)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    def overloaded(video_id=None):
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(judgemod, "judge_pending", overloaded)
    pipeline.analyze_and_judge(vid)  # must not raise
    with Session() as s:
        assert "AI scoring incomplete" in s.get(dbmod.Video, vid).error


def test_analyze_and_judge_skips_the_judge_without_a_key_or_candidates(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    called = []
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None: called.append(video_id))
    monkeypatch.setattr(pipeline, "process_video", lambda v: 3)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    pipeline.analyze_and_judge(vid)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(pipeline, "process_video", lambda v: 0)  # nothing found
    pipeline.analyze_and_judge(vid)
    assert called == []


def test_judge_pending_can_be_limited_to_one_video(env):
    Session, _ = env
    with Session() as s:
        st = dbmod.Streamer(name="x", channel_url="u")
        s.add(st)
        s.flush()
        a = dbmod.Video(streamer_id=st.id, platform_id="a", title="a", url="u")
        b = dbmod.Video(streamer_id=st.id, platform_id="b", title="b", url="u")
        s.add_all([a, b])
        s.flush()
        s.add_all([dbmod.Candidate(video_id=a.id, start_s=0, end_s=30, score=1),
                   dbmod.Candidate(video_id=b.id, start_s=0, end_s=30, score=1)])
        s.commit()
        a_id = a.id

    class Fake(judgemod.Judge):
        def judge(self, moments):
            return [judgemod.Verdict(m["id"], 9.0, "t", "h", "c", "r") for m in moments]

    assert judgemod.judge_pending(Fake(), video_id=a_id) == 1
    with Session() as s:
        scored = {c.video_id: c.ai_score for c in s.scalars(select(dbmod.Candidate))}
    assert scored[a_id] == 9.0 and [v for k, v in scored.items() if k != a_id] == [None]


# ---- the API --------------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"url": "https://example.com/video"},
    {"url": "file:///etc/passwd"},
    {"url": "javascript:alert(1)"},
    {"url": "https://youtu.be/x", "content_type": "nonsense"},
    {"url": ""},
    {},
])
def test_api_rejects_bad_links_before_touching_anything(payload):
    with TestClient(app) as c:
        assert c.post("/videos/from-url", json=payload).status_code == 422
