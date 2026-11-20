import io
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import editor
from app import fullvideo
from app import judge as judgemod
from app import main as mainmod
from app import pipeline
from app.config import settings
from app.main import app
from app.sources import SOURCES
from app.sources.base import Downloaded, Source, VideoInfo, VideoMeta
from app.sources.urls import canonical_channel, web_url
from app.sources.ytdlp import YtDlpSource, _is_network_failure


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


@pytest.mark.parametrize("a,b", [
    ("https://www.youtube.com/@IGN", "https://youtube.com/@ign/"),
    ("https://www.youtube.com/@IGN/streams", "https://www.youtube.com/@IGN"),
    ("https://www.youtube.com/@%C3%89cole", "https://www.youtube.com/@École"),  # encoded and literal handles agree
    ("https://www.youtube.com/@%C3%A9COLE", "https://www.youtube.com/@École"),  # ...whatever the case
    ("https://www.twitch.tv/SomeOne/videos", "twitch.tv/someone"),
    ("HTTPS://KICK.COM:443/SomeOne", "https://kick.com/someone?x=1#y"),
])
def test_equivalent_channel_urls_compare_equal(a, b):
    assert canonical_channel(a) == canonical_channel(b)


def test_youtube_channel_ids_stay_case_sensitive():
    assert canonical_channel("https://www.youtube.com/channel/UCabc") != canonical_channel(
        "https://www.youtube.com/channel/UCABC")
    assert canonical_channel("https://www.youtube.com/@IGN") != canonical_channel("https://www.youtube.com/channel/UC1")


# ---- reading one video's metadata ---------------------------------------------------------

class _FakeYDL:
    info = {}
    error = None
    last_opts = {}

    def __init__(self, opts):
        _FakeYDL.last_opts = opts

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
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", _FakeYDL)  # one yt_dlp module object, shared by every source
    _FakeYDL.error = None
    return _FakeYDL


GOOD = {"id": "abcdefghijk", "title": "A video", "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "duration": 600, "uploader": "Some Channel", "channel_url": "https://www.youtube.com/channel/UC1",
        "uploader_url": "https://www.youtube.com/@some", "live_status": "not_live"}


def test_fetch_video_reads_a_single_video(fake_ydl):
    fake_ydl.info = GOOD
    m = SOURCES["youtube"].fetch_video("https://youtu.be/abcdefghijk")
    assert (m.video.platform_id, m.video.url, m.video.duration_s) == (
        "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk", 600)
    assert m.channel_name == "Some Channel"
    assert m.channel_urls == ("https://www.youtube.com/channel/UC1", "https://www.youtube.com/@some")


def test_fetch_video_never_expands_playlists(fake_ydl):
    """Regression: a channel link used to make yt-dlp load every video on the channel."""
    fake_ydl.info = GOOD
    SOURCES["youtube"].fetch_video("https://youtu.be/abcdefghijk")
    o = _FakeYDL.last_opts
    assert o["extract_flat"] == "in_playlist" and o["playlistend"] == 1 and o["socket_timeout"] <= 30


@pytest.mark.parametrize("platform,patterns", [("youtube", ["youtube.*"]), ("twitch", ["twitch.*"]), ("kick", ["kick.*"])])
def test_fetch_video_and_list_videos_are_limited_to_the_platforms_own_extractors(fake_ydl, platform, patterns):
    # (the audio download and the section render apply the same list; see the options they build)
    fake_ydl.info = {**GOOD, "id": "x1"}
    SOURCES[platform].fetch_video("https://example.test/v")
    assert _FakeYDL.last_opts["allowed_extractors"] == patterns
    SOURCES[platform].list_videos("https://www.twitch.tv/x" if platform != "kick" else "https://kick.com/x")
    assert _FakeYDL.last_opts["allowed_extractors"] == patterns


def test_the_audio_download_is_limited_to_the_platforms_own_extractors(monkeypatch, tmp_path):
    import app.sources.ytdlp as mod
    seen = {}

    class Recorder(_FakeYDL):
        def download(self, urls):
            seen["opts"] = _FakeYDL.last_opts

    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", Recorder)
    with pytest.raises(RuntimeError, match="audio download failed"):  # the recorder writes no file: expected
        SOURCES["twitch"].download(VideoInfo("twitch-v1", "t", "https://www.twitch.tv/videos/1"), tmp_path)
    assert seen["opts"]["allowed_extractors"] == ["twitch.*"]


def test_twitch_and_kick_derive_the_channel_url_from_the_name(fake_ydl):
    """Their VOD metadata has no channel_url/uploader_url, only a name/slug in uploader_id."""
    fake_ydl.info = {"id": "v1", "title": "t", "webpage_url": "https://www.twitch.tv/videos/1", "uploader_id": "someone"}
    assert SOURCES["twitch"].fetch_video("https://x.test/v").channel_urls == ("https://www.twitch.tv/someone",)
    fake_ydl.info = {"id": "u-u-i-d", "title": "t", "webpage_url": "https://kick.com/some-one/videos/u-u-i-d",
                     "uploader_id": "some-one"}
    assert SOURCES["kick"].fetch_video("https://x.test/v").channel_urls == ("https://kick.com/some-one",)


def test_youtube_resolves_both_spellings_of_a_channel(fake_ydl):
    fake_ydl.info = {"channel_id": "UC1", "channel_url": "https://www.youtube.com/channel/UC1",
                     "uploader_url": "https://www.youtube.com/@some"}
    assert SOURCES["youtube"].resolve_channel("https://www.youtube.com/@some") == (
        "https://www.youtube.com/channel/UC1", "https://www.youtube.com/@some")
    assert SOURCES["twitch"].resolve_channel("https://www.twitch.tv/x") == ()  # name-based: nothing to resolve


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


def _download_error(cause, *, via_extractor_error):
    import yt_dlp
    from yt_dlp.utils import ExtractorError
    inner = ExtractorError("Unable to download JSON metadata", cause=cause) if via_extractor_error else cause
    return yt_dlp.utils.DownloadError("ERROR: Unable to download JSON metadata\nmore detail", (type(inner), inner, None))


def _http_error(status, reason):
    from yt_dlp.networking.common import Response
    from yt_dlp.networking.exceptions import HTTPError
    return HTTPError(Response(io.BytesIO(b""), "https://x.test/v", {}, status, reason))


def test_a_network_failure_wrapped_by_the_extractor_is_not_reported_as_a_bad_link(fake_ydl):
    from yt_dlp.networking.exceptions import TransportError
    for wrapped in (False, True):  # directly in exc_info, and one layer down inside an ExtractorError
        fake_ydl.error = _download_error(TransportError("Failed to resolve 'gql.twitch.tv'"), via_extractor_error=wrapped)
        with pytest.raises(ConnectionError, match="try again"):
            SOURCES["youtube"].fetch_video("https://youtu.be/x")


@pytest.mark.parametrize("status,reason", [(503, "Service Unavailable"), (500, "Internal Server Error"),
                                           (429, "Too Many Requests"), (408, "Request Timeout")])
def test_a_struggling_site_is_worth_retrying_even_when_wrapped(fake_ydl, status, reason):
    fake_ydl.error = _download_error(_http_error(status, reason), via_extractor_error=True)
    with pytest.raises(ConnectionError, match="try again"):
        SOURCES["youtube"].fetch_video("https://youtu.be/x")


@pytest.mark.parametrize("status,reason", [(404, "Not Found"), (403, "Forbidden"), (410, "Gone")])
def test_an_http_error_that_blames_the_link_stays_a_bad_link_even_when_wrapped(fake_ydl, status, reason):
    fake_ydl.error = _download_error(_http_error(status, reason), via_extractor_error=True)
    with pytest.raises(ValueError, match="couldn't read that link"):
        SOURCES["youtube"].fetch_video("https://youtu.be/x")
    assert not _is_network_failure(fake_ydl.error)


def test_fetch_video_turns_a_download_error_into_a_readable_message(fake_ydl):
    import yt_dlp
    fake_ydl.error = yt_dlp.utils.DownloadError("ERROR: Video unavailable\nsecond line with detail")
    with pytest.raises(ValueError, match="Video unavailable") as e:
        SOURCES["youtube"].fetch_video("https://youtu.be/x")
    assert "second line" not in str(e.value)


# ---- the redirect finding, reproduced with a real local server -----------------------------

@pytest.fixture()
def redirect_server():
    """A URL that 302-redirects to a second server, recording every request either one receives."""
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append("target")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><head><title>landed</title></head></html>")

        def log_message(self, *a): pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append("redirector")
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/landed")
            self.end_headers()

        def log_message(self, *a): pass

    redirector = HTTPServer(("127.0.0.1", 0), Redirector)
    for s in (target, redirector):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{redirector.server_port}/some/unmatched/path", hits
    for s in (target, redirector):
        s.shutdown()
        s.server_close()


def test_an_unmatched_url_is_not_followed_to_another_host(redirect_server):
    url, hits = redirect_server

    class Unrestricted(YtDlpSource):  # control: yt-dlp's defaults, with no extractor limit
        platform = "unrestricted"

        def _opts(self, **opts):  # test only: bypass any system proxy so the loopback server is reached directly
            return super()._opts(**opts) | {"proxy": ""}

    # without the limit, yt-dlp's generic extractor follows the redirect and accepts the other host
    try:
        Unrestricted().fetch_video(url)
    except ValueError:
        pass
    assert "target" in hits, "the control should have followed the redirect (otherwise this test proves nothing)"

    hits.clear()
    with pytest.raises(ValueError):
        SOURCES["youtube"].fetch_video(url)  # limited to YouTube's extractors
    assert hits == []  # rejected before any request was made


# ---- registering and analyzing ------------------------------------------------------------

class FakeYT(Source):
    """A platform adapter that needs no network: it 'downloads' two minutes of synthetic audio."""
    platform = "youtube"
    meta = None
    resolved: object = ()
    on_download = None  # runs inside download(), to stage events that happen while a worker is busy

    def fetch_video(self, url):
        return self.meta

    def resolve_channel(self, channel_url):
        if isinstance(self.resolved, Exception):
            raise self.resolved
        return self.resolved

    def list_videos(self, channel_url, limit=10):
        return []

    def download(self, video, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.on_download:
            self.on_download()
        sr = 16000
        audio = np.random.default_rng(0).standard_normal(sr * 120).astype("float32") * 0.01
        audio[sr * 60: sr * 65] *= 40
        wav = out_dir / f"{video.platform_id}.wav"
        sf.write(wav, audio, sr)
        return Downloaded(audio=wav, chat_messages=None)


def _meta(vid="abcdefghijk", urls=("https://www.youtube.com/channel/UC1", "https://www.youtube.com/@some"), url=None):
    return VideoMeta(VideoInfo(vid, "A video", url or f"https://www.youtube.com/watch?v={vid}", 600),
                     "Some Channel", urls)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # same connect args as the real app: the API and its background tasks use it from several threads
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (pipeline, judgemod, mainmod, editor, fullvideo):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    fake = FakeYT()
    fake.meta = _meta()
    monkeypatch.setattr(pipeline, "get_source", lambda platform: fake)
    return Session, fake


def _streamers(Session):
    with Session() as s:
        return list(s.scalars(select(dbmod.Streamer)))


def test_ingest_creates_an_untracked_channel_and_a_video(env):
    Session, _ = env
    vid, created = pipeline.ingest_url("https://youtu.be/abcdefghijk", "news")
    assert created
    with Session() as s:
        v = s.get(dbmod.Video, vid)
        assert (v.platform_id, v.status, v.title) == ("abcdefghijk", "new", "A video")
        assert v.content_type == "news"  # the type lives on the video...
        st = v.streamer
        assert (st.name, st.platform, st.tracked) == ("Some Channel", "youtube", False)
        assert st.content_type == "gameplay"  # ...and does not change the channel's own type
        assert st.channel_url == "https://www.youtube.com/channel/UC1"


def test_ingest_without_a_type_leaves_the_video_to_follow_its_channel(env):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    with Session() as s:
        assert s.get(dbmod.Video, vid).content_type is None


def test_ingest_twice_does_not_duplicate(env):
    Session, _ = env
    first = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    again = pipeline.ingest_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert again == (first[0], False)
    with Session() as s:
        assert len(list(s.scalars(select(dbmod.Video)))) == 1


@pytest.mark.parametrize("saved_as", ["https://www.youtube.com/@some/streams", "https://www.youtube.com/@SOME",
                                      "https://www.youtube.com/channel/UC1"])
def test_ingest_reuses_a_channel_you_track_whichever_form_of_its_url_you_saved(env, saved_as):
    Session, _ = env
    with Session() as s:
        s.add(dbmod.Streamer(name="Mine", channel_url=saved_as, platform="youtube", content_type="news"))
        s.commit()
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    with Session() as s:
        streamers = list(s.scalars(select(dbmod.Streamer)))
        assert len(streamers) == 1 and streamers[0].tracked is True  # still followed, not duplicated
        assert s.get(dbmod.Video, vid).streamer_id == streamers[0].id


def test_a_kick_vod_joins_the_tracked_creator_and_keeps_its_kick_url(env):
    Session, fake = env
    with Session() as s:
        s.add(dbmod.Streamer(name="xQc", channel_url="https://kick.com/XQC", platform="kick"))
        s.commit()
    kick_url = "https://kick.com/xqc/videos/01a1dc39-53f8-7a6d-9a47-80b450da6c52"
    fake.meta = _meta("kick-01a1dc39", urls=("https://kick.com/xqc",), url=kick_url)
    vid, created = pipeline.ingest_url(kick_url)
    assert created
    with Session() as s:
        streamers = list(s.scalars(select(dbmod.Streamer)))
        assert [(x.name, x.platform) for x in streamers] == [("xQc", "kick")]  # reused, not duplicated
        v = s.get(dbmod.Video, vid)
        assert v.streamer_id == streamers[0].id and v.url == kick_url and v.platform_id == "kick-01a1dc39"


def test_ingest_without_a_channel_uses_a_stand_in(env):
    Session, fake = env
    fake.meta = _meta("zzzzzzzzzzz", urls=())
    vid, _ = pipeline.ingest_url("https://youtu.be/zzzzzzzzzzz")
    with Session() as s:
        st = s.get(dbmod.Video, vid).streamer
        assert (st.name, st.channel_url, st.tracked) == ("Some Channel", "link:youtube", False)


def test_losing_a_race_to_create_the_channel_reuses_the_winners_row(env, monkeypatch):
    Session, _ = env
    with Session() as s:  # the "winner": created between our lookup and our insert, and tracked
        s.add(dbmod.Streamer(name="Winner", channel_url="https://www.youtube.com/channel/UC1",
                             platform="youtube", tracked=True))
        s.commit()
    monkeypatch.setattr(pipeline, "_find_streamer", lambda db, aliases: None)  # our lookup missed it
    vid, created = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    assert created
    with Session() as s:
        streamers = list(s.scalars(select(dbmod.Streamer)))
        assert [(x.name, x.tracked) for x in streamers] == [("Winner", True)]  # reused, setting untouched
        assert s.get(dbmod.Video, vid).streamer_id == streamers[0].id


def test_losing_a_race_to_register_the_same_video_returns_the_existing_one(env, monkeypatch):
    Session, _ = env
    real_find = pipeline._find_streamer

    def find_then_lose_the_race(db, aliases):
        s = real_find(db, aliases)
        with Session() as other:  # a concurrent request registers the same video right now
            winner_streamer = dbmod.Streamer(name="W", channel_url="w", platform="youtube")
            other.add(winner_streamer)
            other.flush()
            other.add(dbmod.Video(streamer_id=winner_streamer.id, platform_id="abcdefghijk", title="w", url="u"))
            other.commit()
        return s

    monkeypatch.setattr(pipeline, "_find_streamer", find_then_lose_the_race)
    vid, created = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    assert created is False
    with Session() as s:
        assert len(list(s.scalars(select(dbmod.Video)))) == 1 and s.get(dbmod.Video, vid).title == "w"


@pytest.mark.parametrize("url", ["https://example.com/watch?v=1", "file:///etc/passwd", "https://notyoutube.com/x", ""])
def test_ingest_refuses_other_sites_and_schemes(env, url):
    with pytest.raises(ValueError):
        pipeline.ingest_url(url)


# ---- registering a channel (shared by the CLI and the API) ---------------------------------

def test_adding_a_channel_creates_it_and_adding_it_again_reuses_it(env):
    Session, _ = env
    sid, created = pipeline.register_streamer("IGN", "https://www.youtube.com/@IGN", "news")
    again, created2 = pipeline.register_streamer("IGN", "https://www.youtube.com/@IGN", "commentary")
    assert (created, created2, again) == (True, False, sid)
    only = _streamers(Session)[0]
    assert (len(_streamers(Session)), only.tracked, only.content_type) == (1, True, "commentary")


def test_adding_by_handle_reuses_a_channel_saved_by_its_id(env):
    Session, fake = env
    with Session() as s:  # e.g. created earlier by pasting one of its videos
        s.add(dbmod.Streamer(name="IGN", channel_url="https://www.youtube.com/channel/UC1",
                             platform="youtube", tracked=False))
        s.commit()
    fake.resolved = ("https://www.youtube.com/channel/UC1", "https://www.youtube.com/@some")  # what YouTube says
    sid, created = pipeline.register_streamer("IGN", "https://www.youtube.com/@some")
    assert created is False
    streamers = _streamers(Session)
    assert len(streamers) == 1 and streamers[0].id == sid and streamers[0].tracked is True


def test_a_channel_that_cannot_be_resolved_is_still_registered(env):
    Session, fake = env
    fake.resolved = RuntimeError("offline")
    sid, created = pipeline.register_streamer("IGN", "https://www.youtube.com/@IGN")
    assert created is True and len(_streamers(Session)) == 1


def test_registering_survives_a_concurrent_insert_of_the_same_channel(env, monkeypatch):
    Session, _ = env
    real_find = pipeline._find_streamer
    state = {"raced": False}

    def lose_the_race(db, aliases):
        found = real_find(db, aliases)
        if not state["raced"]:
            state["raced"] = True
            # ingestion commits the same channel after our lookup but before our insert
            pipeline.ingest_url("https://youtu.be/abcdefghijk")
        return found

    monkeypatch.setattr(pipeline, "_find_streamer", lose_the_race)
    sid, created = pipeline.register_streamer("Some", "https://www.youtube.com/channel/UC1", "news")
    assert created is False  # their row won; ours reused it
    streamers = _streamers(Session)
    assert len(streamers) == 1 and streamers[0].id == sid
    assert (streamers[0].tracked, streamers[0].content_type) == (True, "news")  # now followed, as asked
    pipeline.ingest_url("https://youtu.be/other000000")  # and the session machinery is still healthy


def test_registering_refuses_unsupported_sites(env):
    with pytest.raises(ValueError):
        pipeline.register_streamer("x", "https://example.com/channel")


# ---- claiming: exactly one worker per video, identified by an attempt token ------------------

def _video(Session, status="new", tag=""):
    """A video in the given status on its own channel (`tag` lets one test make several of a status)."""
    with Session() as s:
        st = dbmod.Streamer(name="c", channel_url=f"u{status}{tag}", platform="youtube")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id=f"id-{status}{tag}", title="t", url="u", status=status)
        s.add(v)
        s.commit()
        return v.id


def _status(Session, vid):
    with Session() as s:
        return s.get(dbmod.Video, vid).status


def _set(Session, vid, **values):
    with Session() as s:
        s.execute(update(dbmod.Video).where(dbmod.Video.id == vid).values(**values))
        s.commit()


def _ago(seconds):
    return pipeline._now() - timedelta(seconds=seconds)


@pytest.mark.parametrize("status,claimable", [("new", True), ("failed", True), ("queued", False),
                                              ("processing", False), ("analyzed", False)])
def test_only_idle_or_failed_videos_can_be_claimed(env, status, claimable):
    Session, _ = env
    vid = _video(Session, status)
    token = pipeline.claim_video(vid)
    assert (token is not None) is claimable
    assert _status(Session, vid) == ("queued" if claimable else status)
    assert pipeline.claim_video(vid) is None  # and a second claim never succeeds


def test_each_claim_gets_its_own_token(env):
    Session, _ = env
    vid = _video(Session, "failed")
    first = pipeline.claim_video(vid)
    _set(Session, vid, status="failed")
    assert pipeline.claim_video(vid) not in (None, first)


def test_a_claim_clears_the_previous_failure(env):
    Session, _ = env
    vid = _video(Session, "failed")
    _set(Session, vid, error="boom")
    assert pipeline.claim_video(vid)
    with Session() as s:
        assert s.get(dbmod.Video, vid).error is None


def test_simultaneous_claims_have_exactly_one_winner(env):
    Session, _ = env
    vid = _video(Session, "failed")
    results, barrier = [], threading.Barrier(8)

    def contender():
        barrier.wait()
        results.append(pipeline.claim_video(vid))

    threads = [threading.Thread(target=contender) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is not None for r in results) == 1 and sum(r is None for r in results) == 7


def test_process_video_refuses_a_claim_it_does_not_hold(env):
    Session, _ = env
    vid = _video(Session, "new")
    real = pipeline.claim_video(vid)
    assert pipeline.process_video(vid, "someone-elses-token") is None  # wrong token on a real claim
    assert _status(Session, vid) == "queued"  # untouched
    for status in ("new", "processing", "analyzed", "failed"):
        other = _video(Session, status, tag="-other")  # videos nobody has queued for this worker
        assert pipeline.process_video(other, real) is None
        assert _status(Session, other) == status


def test_a_worker_that_lost_the_claim_leaves_the_winners_status_alone(env):
    Session, _ = env
    vid = _video(Session, "processing")  # another worker is running it right now
    pipeline.analyze_and_judge(vid, "stale-token")
    assert _status(Session, vid) == "processing"  # not flipped to 'analyzed' underneath it


# ---- abandoned claims, heartbeats and takeover --------------------------------------------

def test_only_abandoned_claims_are_recovered(env):
    Session, _ = env
    ids = {s: _video(Session, s) for s in ("queued", "processing", "new", "analyzed", "failed")}
    _set(Session, ids["queued"], claimed_at=_ago(5))          # a live worker beat 5 seconds ago
    _set(Session, ids["processing"], claimed_at=_ago(3600))   # nothing has beat for an hour: it died
    assert pipeline.recover_stale() == 1
    assert {s: _status(Session, i) for s, i in ids.items()} == {
        "queued": "queued", "processing": "failed", "new": "new", "analyzed": "analyzed", "failed": "failed"}
    with Session() as s:
        row = s.get(dbmod.Video, ids["processing"])
        assert "stopped unexpectedly" in row.error and row.claim_token is None
    assert pipeline.claim_video(ids["processing"])  # and the retry works


def test_recovering_everything_in_flight_is_an_explicit_choice(env):
    Session, _ = env
    vid = _video(Session, "processing")
    _set(Session, vid, claimed_at=_ago(1))
    assert pipeline.recover_stale() == 0       # a fresh claim is protected
    assert pipeline.recover_stale(0) == 1      # `recover --all`, when you know nothing is running
    assert _status(Session, vid) == "failed"


def test_a_claim_left_by_an_older_version_with_no_heartbeat_is_recovered(env):
    Session, _ = env
    vid = _video(Session, "processing")  # claimed_at is NULL, as in rows written before this existed
    assert pipeline.recover_stale() == 1 and _status(Session, vid) == "failed"


def test_the_heartbeat_keeps_a_running_claim_fresh_and_ignores_other_workers(env, monkeypatch):
    Session, _ = env
    monkeypatch.setattr(settings, "heartbeat_s", 0.05)
    vid = _video(Session, "new")
    token = pipeline.claim_video(vid)
    assert pipeline._advance(vid, token, ("queued",), "processing")
    _set(Session, vid, claimed_at=_ago(3600))
    with pipeline._Heartbeat(vid, token):
        time.sleep(0.4)
    with Session() as s:
        assert pipeline._now() - s.get(dbmod.Video, vid).claimed_at < timedelta(seconds=10)
    assert pipeline.recover_stale() == 0  # a beating worker is never mistaken for a dead one

    _set(Session, vid, claimed_at=_ago(3600))
    with pipeline._Heartbeat(vid, "some-other-token"):
        time.sleep(0.3)
    with Session() as s:  # a beat from a worker that isn't the holder changes nothing
        assert pipeline._now() - s.get(dbmod.Video, vid).claimed_at > timedelta(seconds=3000)


def test_a_stalled_video_with_stored_moments_is_retried_without_duplicates(env):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    stale = pipeline.claim_video(vid)
    assert pipeline._advance(vid, stale, ("queued",), "processing")
    with Session() as s:  # the dead worker got as far as committing three moments
        s.add_all([dbmod.Candidate(video_id=vid, start_s=i * 10, end_s=i * 10 + 30, score=0.5) for i in range(3)])
        s.commit()
        old_ids = {c.id for c in s.scalars(select(dbmod.Candidate))}
    _set(Session, vid, claimed_at=_ago(3600))
    assert pipeline.recover_stale() == 1
    token = pipeline.claim_video(vid)
    n = pipeline.process_video(vid, token)  # the real worker, start to finish
    assert n and _status(Session, vid) == "analyzed"
    with Session() as s:
        cands = list(s.scalars(select(dbmod.Candidate).where(dbmod.Candidate.video_id == vid)))
        assert len(cands) == n and not ({c.id for c in cands} & old_ids)  # replaced, not added to
        assert len({c.start_s for c in cands}) == n


def test_a_worker_that_outlives_its_claim_cannot_overwrite_its_replacement(env):
    Session, fake = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    token_a = pipeline.claim_video(vid)
    taken = {}

    def takeover():  # runs inside worker A's download: A has stalled, is judged dead, and B takes over
        assert pipeline.recover_stale(0) == 1
        taken["b"] = pipeline.claim_video(vid)

    fake.on_download = takeover
    assert pipeline.process_video(vid, token_a) is None  # A's results are refused
    fake.on_download = None
    with Session() as s:
        assert not list(s.scalars(select(dbmod.Candidate)))  # A wrote nothing
        assert s.get(dbmod.Video, vid).claim_token == taken["b"]
    assert _status(Session, vid) == "queued"  # B's claim is intact
    # none of A's late writes can touch B's claim either
    assert not pipeline._advance(vid, token_a, ("queued", "processing"), "analyzed")
    pipeline._note(vid, "stale note", token_a)
    with Session() as s:
        assert s.get(dbmod.Video, vid).error is None
    assert pipeline.process_video(vid, taken["b"]) and _status(Session, vid) == "analyzed"  # B completes normally


def _ffprobe_seconds(path):
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def test_every_moment_gets_a_short_audio_snippet_the_ai_can_listen_to(env):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    n = pipeline.process_video(vid, pipeline.claim_video(vid))
    assert n
    with Session() as s:
        cands = list(s.scalars(select(dbmod.Candidate)))
    assert len(cands) == n and len({c.audio_path for c in cands}) == n and all(c.audio_path.endswith(".mp3") for c in cands)
    for c in cands:
        f = settings.moments_dir / c.audio_path
        assert f.is_file() and f.stat().st_size < 400_000          # small: a 30 s mono mp3
        assert 25 <= _ffprobe_seconds(f) <= 31                      # and it really is the 30 s moment


def test_replacing_a_videos_moments_replaces_their_audio_too(env):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    pipeline.process_video(vid, pipeline.claim_video(vid))
    first = {p.name for p in settings.moments_dir.iterdir()}
    _set(Session, vid, status="failed")
    pipeline.process_video(vid, pipeline.claim_video(vid))  # a retry: new moments, new ids
    with Session() as s:
        current = {c.audio_path for c in s.scalars(select(dbmod.Candidate))}
    assert {p.name for p in settings.moments_dir.iterdir()} == current  # no orphaned files from the first attempt
    assert first.isdisjoint(current)


def test_a_discarded_attempt_leaves_no_audio_behind(env, monkeypatch):
    Session, fake = env
    written = []

    def writer(wav, start, end, stem):  # deterministic stand-in: no ffmpeg needed
        settings.moments_dir.mkdir(parents=True, exist_ok=True)
        (settings.moments_dir / f"{stem}.mp3").write_bytes(b"audio")
        written.append(stem)
        return f"{stem}.mp3"

    monkeypatch.setattr(pipeline, "cut_moment_audio", writer)
    monkeypatch.setattr(settings, "judge_audio", True)
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    token = pipeline.claim_video(vid)
    fake.on_download = lambda: (pipeline.recover_stale(0), pipeline.claim_video(vid))  # our claim is taken over
    assert pipeline.process_video(vid, token) is None
    assert written  # audio really was cut for the discarded attempt...
    assert list(settings.moments_dir.iterdir()) == []  # ...and removed again


def test_analysis_still_succeeds_when_the_audio_cannot_be_cut(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")

    def no_ffmpeg(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(pipeline.subprocess, "run", no_ffmpeg)
    n = pipeline.process_video(vid, pipeline.claim_video(vid))
    assert n and _status(Session, vid) == "analyzed"
    with Session() as s:
        assert all(c.audio_path is None for c in s.scalars(select(dbmod.Candidate)))  # judged from text alone


def test_audio_snippets_can_be_switched_off(env, monkeypatch):
    Session, _ = env
    monkeypatch.setattr(settings, "judge_audio", False)
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    assert pipeline.process_video(vid, pipeline.claim_video(vid))
    assert not settings.moments_dir.exists() or list(settings.moments_dir.iterdir()) == []


def test_each_attempt_works_in_its_own_folder(env):
    Session, fake = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    seen = []
    fake.on_download = lambda: None
    original = fake.download

    def spy(video, out_dir):
        seen.append(out_dir.name)
        return original(video, out_dir)

    fake.download = spy
    token = pipeline.claim_video(vid)
    pipeline.process_video(vid, token)
    assert seen == [f"abcdefghijk-{token[:8]}"]  # a stalled attempt's cleanup can't delete a replacement's files


# ---- analyze_and_judge ------------------------------------------------------------------------

def _fake_process(Session, n=3):
    """Stands in for process_video: same claim, then stores candidates the way the real one does."""
    def process(video_id, token, finalize=True):
        if not pipeline._advance(video_id, token, ("queued",), "processing"):
            return None
        with Session() as s:
            for i in range(n):
                s.add(dbmod.Candidate(video_id=video_id, start_s=i * 100, end_s=i * 100 + 30, score=1.0))
            s.commit()
        return n
    return process


def _count(Session, vid, scored=None):
    with Session() as s:
        q = select(dbmod.Candidate).where(dbmod.Candidate.video_id == vid)
        if scored is not None:
            q = q.where(dbmod.Candidate.ai_score.is_not(None) if scored else dbmod.Candidate.ai_score.is_(None))
        return len(list(s.scalars(q)))


def _error(Session, vid):
    with Session() as s:
        return s.get(dbmod.Video, vid).error


def _score_all(Session, video_id, limit=None):
    with Session() as s:
        for c in list(s.scalars(select(dbmod.Candidate).where(dbmod.Candidate.video_id == video_id)))[:limit]:
            c.ai_score = 7.0
        s.commit()


@pytest.fixture()
def judged(env, monkeypatch):
    """A claimed video with a fake worker and a Gemini key, ready for analyze_and_judge."""
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    token = pipeline.claim_video(vid)
    monkeypatch.setattr(pipeline, "process_video", _fake_process(Session))
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    return Session, vid, token


def test_a_judge_failure_is_recorded_and_the_stored_moments_survive(judged, monkeypatch):
    Session, vid, token = judged

    def overloaded(video_id=None, claim_token=None):
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(judgemod, "judge_pending", overloaded)
    pipeline.analyze_and_judge(vid, token)  # must not raise
    assert "AI scoring incomplete" in _error(Session, vid) and _status(Session, vid) == "analyzed"
    assert _count(Session, vid) == 3  # the analysis was kept, just unscored


def test_the_video_stays_in_progress_until_scores_are_saved(judged, monkeypatch):
    Session, vid, token = judged
    seen = {}

    def judge(video_id=None, claim_token=None):
        seen["during"] = _status(Session, video_id)  # what a polling dashboard would see mid-judging
        _score_all(Session, video_id)
        return 3

    monkeypatch.setattr(judgemod, "judge_pending", judge)
    pipeline.analyze_and_judge(vid, token)
    assert seen["during"] == "processing"  # not 'analyzed' yet: scores weren't committed
    assert _status(Session, vid) == "analyzed" and _error(Session, vid) is None


def test_the_warning_appears_only_when_some_moments_stay_unscored(judged, monkeypatch):
    Session, vid, token = judged
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None, claim_token=None: _score_all(Session, video_id, limit=2))
    pipeline.analyze_and_judge(vid, token)  # returns normally, but the model skipped one moment
    assert "1 moments unscored" in _error(Session, vid)


def test_a_clean_pass_clears_a_warning_left_by_an_earlier_attempt(judged, monkeypatch):
    Session, vid, token = judged
    _set(Session, vid, error="AI scoring incomplete (ServerError); run judge again later")
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None, claim_token=None: _score_all(Session, video_id))
    pipeline.analyze_and_judge(vid, token)
    assert _error(Session, vid) is None


def test_a_failed_scoring_check_is_surfaced_and_the_video_still_finishes(judged, monkeypatch):
    Session, vid, token = judged
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None, claim_token=None: _score_all(Session, video_id))

    def broken(video_id):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(pipeline, "_unscored", broken)
    pipeline.analyze_and_judge(vid, token)
    assert "couldn't be verified" in _error(Session, vid) and "judge again later" in _error(Session, vid)
    assert _status(Session, vid) == "analyzed"  # and the dashboard stops waiting


def test_a_retry_clears_the_warning_only_for_videos_that_are_fully_scored(env):
    Session, _ = env
    done, partial, unrelated = _video(Session, "analyzed"), _video(Session, "failed"), _video(Session, "new")
    note = "AI scoring incomplete (ServerError); run judge again later"
    for v in (done, partial):
        _set(Session, v, error=note)
    _set(Session, unrelated, error="RuntimeError: something else")
    with Session() as s:
        s.add_all([dbmod.Candidate(video_id=done, start_s=0, end_s=30, score=1, ai_score=8.0),
                   dbmod.Candidate(video_id=partial, start_s=0, end_s=30, score=1, ai_score=8.0),
                   dbmod.Candidate(video_id=partial, start_s=50, end_s=80, score=1)])  # one still unscored
        s.commit()
    assert pipeline.refresh_scoring_notes() == 1
    assert _error(Session, done) is None
    assert _error(Session, partial) == note                          # still incomplete: keep the warning
    assert _error(Session, unrelated) == "RuntimeError: something else"  # not ours to touch


def test_without_a_key_or_candidates_the_judge_is_skipped_and_the_video_still_finishes(env, monkeypatch):
    Session, _ = env
    called = []
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None, claim_token=None: called.append(video_id))
    # (api key, moments found, which video): no key with moments, then a key with nothing found
    for (key, n), status in ((("", 3), "new"), (("test-key", 0), "failed")):
        vid = _video(Session, status)
        token = pipeline.claim_video(vid)
        assert token
        monkeypatch.setattr(settings, "gemini_api_key", key)
        monkeypatch.setattr(pipeline, "process_video", _fake_process(Session, n))
        pipeline.analyze_and_judge(vid, token)
        assert _status(Session, vid) == "analyzed"  # still finishes, so the dashboard stops waiting
    assert called == []


def test_judge_pending_can_be_limited_to_one_video(env):
    Session, _ = env
    a, b = _video(Session, "new"), _video(Session, "failed")
    with Session() as s:
        s.add_all([dbmod.Candidate(video_id=a, start_s=0, end_s=30, score=1),
                   dbmod.Candidate(video_id=b, start_s=0, end_s=30, score=1)])
        s.commit()

    class Fake(judgemod.Judge):
        def judge(self, moments):
            return [judgemod.Verdict(m["id"], 9.0, "t", "h", "c", "r") for m in moments]

    assert judgemod.judge_pending(Fake(), video_id=a) == 1
    assert _count(Session, a, scored=True) == 1 and _count(Session, b, scored=True) == 0


def test_the_judge_uses_the_videos_own_type_before_the_channels(env):
    Session, _ = env
    a, b = _video(Session, "new"), _video(Session, "failed")  # both on gameplay channels
    with Session() as s:
        s.get(dbmod.Video, a).content_type = "news"  # explicit type from a pasted link
        s.add_all([dbmod.Candidate(video_id=a, start_s=0, end_s=30, score=1),
                   dbmod.Candidate(video_id=b, start_s=0, end_s=30, score=1)])
        s.commit()
        by_video = {c.id: c.video_id for c in s.scalars(select(dbmod.Candidate))}
    seen = {}

    class Spy(judgemod.Judge):
        def judge(self, moments):
            seen.update({by_video[m["id"]]: m["type"] for m in moments})
            return [judgemod.Verdict(m["id"], 9.0, "t", "h", "c", "r") for m in moments]

    judgemod.judge_pending(Spy())
    assert seen == {a: "news", b: "gameplay"}  # explicit type wins; otherwise the channel's applies


# ---- upgrading an older database -------------------------------------------------------------

def test_an_upgraded_database_serves_the_new_endpoints_and_keeps_its_rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:  # the schema from before types, tracking and claims existed
        conn.execute(text("CREATE TABLE streamers (id INTEGER PRIMARY KEY, platform VARCHAR(20), name VARCHAR(100), "
                          "channel_url VARCHAR(300) UNIQUE, content_type VARCHAR(20))"))
        conn.execute(text("CREATE TABLE videos (id INTEGER PRIMARY KEY, streamer_id INTEGER, platform_id VARCHAR(50) "
                          "UNIQUE, title VARCHAR(300), url VARCHAR(300), duration_s INTEGER, status VARCHAR(20), "
                          "error VARCHAR(500), discovered_at DATETIME)"))
        conn.execute(text("INSERT INTO streamers VALUES (1, 'youtube', 'Old', 'https://www.youtube.com/@old', 'news')"))
        conn.execute(text("INSERT INTO videos VALUES (7, 1, 'legacy123456', 'Legacy', 'https://youtu.be/x', 60, "
                          "'analyzed', NULL, '2026-01-01 00:00:00')"))
    dbmod.init_db(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    for mod in (pipeline, judgemod, mainmod, editor, fullvideo):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    with TestClient(app) as client:
        r = client.get("/videos/7")
        assert r.status_code == 200
        assert r.json() == {"id": 7, "title": "Legacy", "url": "https://youtu.be/x", "status": "analyzed",
                            "error": None, "channel": "Old", "platform": "youtube", "moments": 0, "unscored": 0,
                            "content_type": "news", "clips_mb": 0.0, "added": "2026-01-01T00:00:00",
                            "duration_s": 60,  # and the full-video columns are added, defaulting to "no copy"
                            "full_status": "none", "full_progress": None, "full_error": None, "full_size_mb": None}
        assert [v["id"] for v in client.get("/videos").json()] == [7]
    with Session() as s:
        v = s.get(dbmod.Video, 7)
        assert v.content_type is None and v.claim_token is None  # new columns are empty for old rows
        assert s.get(dbmod.Streamer, 1).tracked is True and s.get(dbmod.Streamer, 1).content_type == "news"


# ---- the API --------------------------------------------------------------------------------

@pytest.fixture()
def api(env, monkeypatch):
    """The real app on the temporary database: startup can neither open nor migrate the developer's."""
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    Session, _ = env
    with TestClient(app) as client:
        yield client, Session


@pytest.mark.parametrize("payload", [
    {"url": "https://example.com/video"},
    {"url": "file:///etc/passwd"},
    {"url": "javascript:alert(1)"},
    {"url": "https://youtu.be/x", "content_type": "nonsense"},
    {"url": ""},
    {},
])
def test_api_rejects_bad_links_before_touching_anything(api, payload):
    client, _ = api
    assert client.post("/videos/from-url", json=payload).status_code == 422


def test_api_answers_502_for_a_network_failure(api, monkeypatch):
    client, _ = api

    def offline(url, content_type=None):
        raise ConnectionError("couldn't reach the site (network problem); try again in a moment")

    monkeypatch.setattr(pipeline, "ingest_url", offline)
    r = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"})
    assert r.status_code == 502 and "try again" in r.json()["detail"]


@pytest.fixture()
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid, token: calls.append((vid, token)))
    return calls


def test_pasting_a_link_queues_one_analysis_however_often_it_is_pasted(api, started):
    client, _ = api
    first = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk", "content_type": "news"}).json()
    again = client.post("/videos/from-url", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"}).json()
    assert (first["created"], first["queued"], first["status"]) == (True, True, "queued")
    assert (again["created"], again["queued"], again["status"]) == (False, False, "queued")  # already claimed
    assert [v for v, _ in started] == [first["video_id"]]  # one job, not two


def test_a_video_registered_earlier_but_never_analyzed_can_be_started_by_pasting_it(api, started):
    client, Session = api
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")  # like `add-video --no-process`
    assert _status(Session, vid) == "new"
    r = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()
    assert r["queued"] is True and [v for v, _ in started] == [vid]


def test_a_failed_video_is_retried_when_pasted_again(api, started):
    client, Session = api
    vid = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()["video_id"]
    _set(Session, vid, status="failed", error="boom")
    r = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()
    assert r["queued"] is True and [v for v, _ in started] == [vid, vid]
    assert started[0][1] != started[1][1]  # a fresh attempt token each time


def _type_of(Session, vid):
    with Session() as s:
        return s.get(dbmod.Video, vid).content_type


def test_a_type_given_with_a_repeat_paste_applies_only_when_the_claim_succeeds(api, started):
    client, Session = api
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk", "gameplay")  # waiting, with a stored type
    paste = lambda t: client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk", "content_type": t}).json()
    assert paste("news")["queued"] is True and _type_of(Session, vid) == "news"      # won the claim: applied
    assert paste("commentary")["queued"] is False and _type_of(Session, vid) == "news"  # running: left alone
    _set(Session, vid, status="analyzed")
    assert paste("commentary")["queued"] is False and _type_of(Session, vid) == "news"  # finished: left alone
    _set(Session, vid, status="failed")
    assert paste("commentary")["queued"] is True and _type_of(Session, vid) == "commentary"  # retry: applied


def test_pasting_without_a_type_keeps_the_stored_one(api, started):
    client, Session = api
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk", "news")
    client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"})
    assert _type_of(Session, vid) == "news"


def test_the_process_endpoint_claims_too(api, monkeypatch):
    client, Session = api
    started = []
    monkeypatch.setattr(pipeline, "process_video", lambda vid, token, **kw: started.append((vid, token)))
    vid = _video(Session, "new")
    assert client.post(f"/videos/{vid}/process").status_code == 200
    assert client.post(f"/videos/{vid}/process").status_code == 409  # exists, but already queued
    assert [v for v, _ in started] == [vid]
    assert client.post("/videos/99999/process").status_code == 404    # no such video is not a conflict


def test_adding_a_streamer_through_the_api_reuses_an_existing_channel(api):
    client, Session = api
    a = client.post("/streamers", params={"name": "IGN", "channel_url": "https://www.youtube.com/@IGN"}).json()
    b = client.post("/streamers", params={"name": "IGN", "channel_url": "https://www.youtube.com/@ign/"}).json()
    assert a["created"] is True and b["created"] is False and a["id"] == b["id"]
    assert len(_streamers(Session)) == 1
    assert client.post("/streamers", params={"name": "x", "channel_url": "https://example.com/c"}).status_code == 422


def test_videos_endpoints_count_moments_in_sql_and_bound_the_limit(api):
    client, Session = api
    a, b = _video(Session, "analyzed"), _video(Session, "new")
    with Session() as s:
        s.add_all([dbmod.Candidate(video_id=a, start_s=i, end_s=i + 30, score=1) for i in range(4)])
        s.commit()
    by_id = {v["id"]: v for v in client.get("/videos?limit=6").json()}
    assert len(by_id) == 2 and by_id[a]["moments"] == 4 and by_id[b]["moments"] == 0
    assert len(client.get("/videos?limit=1").json()) == 1  # the limit really limits
    one = client.get(f"/videos/{a}").json()
    assert (one["status"], one["moments"], one["channel"], one["platform"]) == ("analyzed", 4, "c", "youtube")
    assert client.get("/videos/99999").status_code == 404
    for bad in ("0", "-1", "101", "999999", "abc"):
        assert client.get(f"/videos?limit={bad}").status_code == 422
    assert client.get("/videos?limit=100").status_code == 200 and client.get("/videos").status_code == 200


def test_an_old_candidates_table_gains_the_framing_columns_with_their_defaults(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old2.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:  # a candidates table from before zoom and position existed
        conn.execute(text("CREATE TABLE candidates (id INTEGER PRIMARY KEY, video_id INTEGER, start_s FLOAT, end_s FLOAT, "
                          "score FLOAT, review_status VARCHAR(20), render_status VARCHAR(20), layout VARCHAR(10))"))
        conn.execute(text("INSERT INTO candidates VALUES (1, 1, 0, 30, 1.0, 'pending', 'none', 'fit')"))
    dbmod.init_db(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT zoom, pan_x, pan_y FROM candidates WHERE id = 1")).one()
    assert tuple(row) == (1.0, 0.5, 0.5)
