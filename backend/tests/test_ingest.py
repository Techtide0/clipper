import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import judge as judgemod
from app import main as mainmod
from app import pipeline
from app.config import settings
from app.main import app
from app.sources import SOURCES
from app.sources.base import Source, VideoInfo, VideoMeta
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
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", _FakeYDL)
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
def test_every_yt_dlp_call_is_limited_to_the_platforms_own_extractors(fake_ydl, platform, patterns):
    fake_ydl.info = {**GOOD, "id": "x1"}
    SOURCES[platform].fetch_video("https://example.test/v")
    assert _FakeYDL.last_opts["allowed_extractors"] == patterns
    SOURCES[platform].list_videos("https://www.twitch.tv/x" if platform != "kick" else "https://kick.com/x")
    assert _FakeYDL.last_opts["allowed_extractors"] == patterns


def test_twitch_and_kick_derive_the_channel_url_from_the_name(fake_ydl):
    """Their VOD metadata has no channel_url/uploader_url, only a name/slug in uploader_id."""
    fake_ydl.info = {"id": "v1", "title": "t", "webpage_url": "https://www.twitch.tv/videos/1", "uploader_id": "someone"}
    assert SOURCES["twitch"].fetch_video("https://x.test/v").channel_urls == ("https://www.twitch.tv/someone",)
    fake_ydl.info = {"id": "u-u-i-d", "title": "t", "webpage_url": "https://kick.com/some-one/videos/u-u-i-d",
                     "uploader_id": "some-one"}
    assert SOURCES["kick"].fetch_video("https://x.test/v").channel_urls == ("https://kick.com/some-one",)


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


def test_a_network_failure_wrapped_by_the_extractor_is_not_reported_as_a_bad_link(fake_ydl):
    from yt_dlp.networking.exceptions import TransportError
    for wrapped in (False, True):  # directly in exc_info, and one layer down inside an ExtractorError
        fake_ydl.error = _download_error(TransportError("Failed to resolve 'gql.twitch.tv'"), via_extractor_error=wrapped)
        with pytest.raises(ConnectionError, match="try again"):
            SOURCES["youtube"].fetch_video("https://youtu.be/x")


def test_an_http_error_stays_a_bad_link_even_when_wrapped(fake_ydl):
    import io

    from yt_dlp.networking.common import Response
    from yt_dlp.networking.exceptions import HTTPError

    # a site that answered with an error, so the link is the problem
    not_found = HTTPError(Response(io.BytesIO(b""), "https://x.test/v", {}, 404, "Not Found"))
    fake_ydl.error = _download_error(not_found, via_extractor_error=True)
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
    platform = "youtube"
    meta = None

    def fetch_video(self, url):
        return self.meta

    def list_videos(self, channel_url, limit=10):
        return []

    def download(self, video, out_dir):
        raise NotImplementedError


def _meta(vid="abcdefghijk", urls=("https://www.youtube.com/channel/UC1", "https://www.youtube.com/@some")):
    return VideoMeta(VideoInfo(vid, "A video", f"https://www.youtube.com/watch?v={vid}", 600), "Some Channel", urls)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # same connect args as the real app: the API and its background tasks use it from several threads
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (pipeline, judgemod, mainmod):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    fake = FakeYT()
    fake.meta = _meta()
    monkeypatch.setattr(pipeline, "get_source", lambda platform: fake)
    return Session, fake


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


def test_a_tracked_kick_channel_matches_a_vod_that_only_has_a_slug(env):
    Session, fake = env
    with Session() as s:
        s.add(dbmod.Streamer(name="xQc", channel_url="https://kick.com/XQC", platform="kick"))
        s.commit()
    fake.meta = _meta("kick-1", urls=("https://kick.com/xqc",))
    vid, _ = pipeline.ingest_url("https://kick.com/xqc/videos/1")
    with Session() as s:
        assert len(list(s.scalars(select(dbmod.Streamer)))) == 1


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


# ---- claiming: exactly one worker per video -----------------------------------------------

def _video(Session, status="new"):
    with Session() as s:
        st = dbmod.Streamer(name="c", channel_url=f"u{status}", platform="youtube")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id=f"id-{status}", title="t", url="u", status=status)
        s.add(v)
        s.commit()
        return v.id


def _status(Session, vid):
    with Session() as s:
        return s.get(dbmod.Video, vid).status


@pytest.mark.parametrize("status,claimable", [("new", True), ("failed", True), ("queued", False),
                                              ("processing", False), ("analyzed", False)])
def test_only_idle_or_failed_videos_can_be_claimed(env, status, claimable):
    Session, _ = env
    vid = _video(Session, status)
    assert pipeline.claim_video(vid) is claimable
    assert _status(Session, vid) == ("queued" if claimable else status)
    assert pipeline.claim_video(vid) is False  # and a second claim never succeeds


def test_a_claim_clears_the_previous_failure(env):
    Session, _ = env
    vid = _video(Session, "failed")
    with Session() as s:
        s.get(dbmod.Video, vid).error = "boom"
        s.commit()
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
    assert results.count(True) == 1 and results.count(False) == 7


def test_process_video_refuses_a_video_it_does_not_hold_the_claim_for(env):
    Session, _ = env
    for status in ("new", "processing", "analyzed", "failed"):
        vid = _video(Session, status)
        assert pipeline.process_video(vid) is None
        assert _status(Session, vid) == status  # untouched


def test_a_worker_that_lost_the_claim_leaves_the_winners_status_alone(env, monkeypatch):
    Session, _ = env
    vid = _video(Session, "processing")  # another worker is running it right now
    pipeline.analyze_and_judge(vid)
    assert _status(Session, vid) == "processing"  # not flipped to 'analyzed' underneath it


def test_interrupted_videos_are_marked_failed_so_they_can_be_retried(env):
    Session, _ = env
    ids = {s: _video(Session, s) for s in ("queued", "processing", "new", "analyzed", "failed")}
    assert pipeline.recover_interrupted() == 2
    assert {s: _status(Session, i) for s, i in ids.items()} == {
        "queued": "failed", "processing": "failed", "new": "new", "analyzed": "analyzed", "failed": "failed"}
    with Session() as s:
        assert "interrupted" in s.get(dbmod.Video, ids["queued"]).error
    assert pipeline.claim_video(ids["queued"])  # and the retry works


# ---- analyze_and_judge ------------------------------------------------------------------------

def _fake_process(Session, n=3):
    """Stands in for process_video: same claim, then stores candidates the way the real one does."""
    def process(video_id, finalize=True):
        if not pipeline._transition(video_id, ("queued",), "processing"):
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


def test_a_judge_failure_is_recorded_and_the_stored_moments_survive(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    assert pipeline.claim_video(vid)
    monkeypatch.setattr(pipeline, "process_video", _fake_process(Session))
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    def overloaded(video_id=None):
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(judgemod, "judge_pending", overloaded)
    pipeline.analyze_and_judge(vid)  # must not raise
    with Session() as s:
        v = s.get(dbmod.Video, vid)
        assert "AI scoring incomplete" in v.error and v.status == "analyzed"
    assert _count(Session, vid) == 3  # the analysis was kept, just unscored


def test_the_video_stays_in_progress_until_scores_are_saved(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    pipeline.claim_video(vid)
    monkeypatch.setattr(pipeline, "process_video", _fake_process(Session))
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    seen = {}

    def judge(video_id=None):
        seen["during"] = _status(Session, video_id)  # what a polling dashboard would see mid-judging
        with Session() as s:
            for c in s.scalars(select(dbmod.Candidate).where(dbmod.Candidate.video_id == video_id)):
                c.ai_score = 7.0
            s.commit()
        return 3

    monkeypatch.setattr(judgemod, "judge_pending", judge)
    pipeline.analyze_and_judge(vid)
    assert seen["during"] == "processing"  # not 'analyzed' yet: scores weren't committed
    assert _status(Session, vid) == "analyzed"
    with Session() as s:
        assert s.get(dbmod.Video, vid).error is None  # everything was scored: no warning


def test_the_warning_appears_only_when_some_moments_stay_unscored(env, monkeypatch):
    Session, _ = env
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    pipeline.claim_video(vid)
    monkeypatch.setattr(pipeline, "process_video", _fake_process(Session))
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    def partial(video_id=None):  # returns normally, but the model skipped one moment
        with Session() as s:
            for c in list(s.scalars(select(dbmod.Candidate).where(dbmod.Candidate.video_id == video_id)))[:2]:
                c.ai_score = 7.0
            s.commit()
        return 2

    monkeypatch.setattr(judgemod, "judge_pending", partial)
    pipeline.analyze_and_judge(vid)
    with Session() as s:
        assert "1 moments unscored" in s.get(dbmod.Video, vid).error


def test_without_a_key_or_candidates_the_judge_is_skipped_and_the_video_still_finishes(env, monkeypatch):
    Session, _ = env
    called = []
    monkeypatch.setattr(judgemod, "judge_pending", lambda video_id=None: called.append(video_id))
    # (api key, moments found, which video): no key with moments, then a key with nothing found
    for (key, n), status in ((("", 3), "new"), (("test-key", 0), "failed")):
        vid = _video(Session, status)
        assert pipeline.claim_video(vid)
        monkeypatch.setattr(settings, "gemini_api_key", key)
        monkeypatch.setattr(pipeline, "process_video", _fake_process(Session, n))
        pipeline.analyze_and_judge(vid)
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


def test_pasting_a_link_queues_one_analysis_however_often_it_is_pasted(api, monkeypatch):
    client, Session = api
    started = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid: started.append(vid))
    first = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk", "content_type": "news"}).json()
    again = client.post("/videos/from-url", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"}).json()
    assert (first["created"], first["queued"], first["status"]) == (True, True, "queued")
    assert (again["created"], again["queued"], again["status"]) == (False, False, "queued")  # already claimed
    assert started == [first["video_id"]]  # one job, not two


def test_a_video_registered_earlier_but_never_analyzed_can_be_started_by_pasting_it(api, monkeypatch):
    client, Session = api
    started = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid: started.append(vid))
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")  # like `add-video --no-process`
    assert _status(Session, vid) == "new"
    r = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()
    assert r["queued"] is True and started == [vid]


def test_a_failed_video_is_retried_when_pasted_again(api, monkeypatch):
    client, Session = api
    started = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid: started.append(vid))
    vid = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()["video_id"]
    with Session() as s:
        v = s.get(dbmod.Video, vid)
        v.status, v.error = "failed", "boom"
        s.commit()
    r = client.post("/videos/from-url", json={"url": "https://youtu.be/abcdefghijk"}).json()
    assert r["queued"] is True and started == [vid, vid]


def test_the_process_endpoint_claims_too(api, monkeypatch):
    client, Session = api
    started = []
    monkeypatch.setattr(pipeline, "process_video", lambda vid, **kw: started.append(vid))
    vid = _video(Session, "new")
    assert client.post(f"/videos/{vid}/process").status_code == 200
    assert client.post(f"/videos/{vid}/process").status_code == 409  # already queued
    assert started == [vid]


def test_videos_endpoints_count_moments_in_sql_and_bound_the_limit(api):
    client, Session = api
    a, b = _video(Session, "analyzed"), _video(Session, "new")
    with Session() as s:
        s.add_all([dbmod.Candidate(video_id=a, start_s=i, end_s=i + 30, score=1) for i in range(4)])
        s.commit()
    by_id = {v["id"]: v for v in client.get("/videos?limit=6").json()}
    assert by_id[a]["moments"] == 4 and by_id[b]["moments"] == 0
    one = client.get(f"/videos/{a}").json()
    assert (one["status"], one["moments"], one["channel"], one["platform"]) == ("analyzed", 4, "c", "youtube")
    assert client.get("/videos/99999").status_code == 404
    for bad in ("0", "-1", "101", "999999", "abc"):
        assert client.get(f"/videos?limit={bad}").status_code == 422
    assert client.get("/videos?limit=100").status_code == 200 and client.get("/videos").status_code == 200
