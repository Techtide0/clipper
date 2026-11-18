import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import pipeline
from app.config import settings
from app.sources.base import Downloaded, Source, VideoInfo


class FakeSource(Source):
    """Audio only: chat_messages is None, as when a platform has no chat replay."""
    platform = "fake"

    def list_videos(self, channel_url, limit=10):
        return []

    def download(self, video, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        sr = 16000
        audio = np.random.randn(sr * 120).astype("float32") * 0.01
        audio[sr * 60: sr * 65] *= 40
        wav = out_dir / f"{video.platform_id}.wav"
        sf.write(wav, audio, sr)
        return Downloaded(audio=wav, chat_messages=None)


class FakeTwitch(Source):
    """Emits ids the way the real adapter does: 'twitch-' + the id Twitch uses (which starts with 'v')."""
    platform = "twitch"

    def list_videos(self, channel_url, limit=10):
        return [VideoInfo("twitch-v123", "already known", "https://www.twitch.tv/videos/123"),
                VideoInfo("twitch-v999", "genuinely new", "https://www.twitch.tv/videos/999")]

    def download(self, video, out_dir):
        raise NotImplementedError


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    def get_source(platform):  # same behaviour as the real one: KeyError for unknown platforms
        return {"fake": FakeSource(), "twitch": FakeTwitch()}[platform]

    monkeypatch.setattr(pipeline, "get_source", get_source)
    return engine, Session


def test_audio_only_video_is_analyzed_and_an_unknown_platform_fails_alone(env):
    _, Session = env
    with Session() as s:
        bad = dbmod.Streamer(name="bad", channel_url="u1", platform="nope")
        good = dbmod.Streamer(name="good", channel_url="u2", platform="fake")
        s.add_all([bad, good])
        s.flush()
        s.add_all([dbmod.Video(streamer_id=bad.id, platform_id="b", title="b", url="u"),
                   dbmod.Video(streamer_id=good.id, platform_id="g", title="g", url="u")])
        s.commit()

    pipeline.process_pending()  # the first video's failure must not stop the second

    with Session() as s:
        videos = {v.platform_id: v for v in s.scalars(select(dbmod.Video))}
        assert videos["b"].status == "failed" and "KeyError" in videos["b"].error
        assert videos["g"].status == "analyzed"
        cands = list(s.scalars(select(dbmod.Candidate).where(dbmod.Candidate.video_id == videos["g"].id)))
        assert cands and all(set(c.signals) == {"audio"} for c in cands)  # no chat signal, audio still scored
        best = max(cands, key=lambda c: c.score)
        assert 55 <= best.start_s + 18 <= 70  # moments start 18s before their peak; burst was at 60-65s


def test_waiting_videos_are_analyzed_even_when_their_channel_is_untracked(env):
    """`add-video --no-process` videos live on untracked channels and must still be reachable by `process`."""
    _, Session = env
    with Session() as s:
        st = dbmod.Streamer(name="link", channel_url="link:fake", platform="fake", tracked=False)
        s.add(st)
        s.flush()
        s.add(dbmod.Video(streamer_id=st.id, platform_id="waiting", title="w", url="u"))
        s.commit()
    pipeline.process_pending()
    with Session() as s:
        assert s.scalars(select(dbmod.Video)).one().status == "analyzed"


def test_reinitializing_keeps_the_tracked_flag_as_it_was(env):
    engine, Session = env
    with Session() as s:
        s.add_all([dbmod.Streamer(name="followed", channel_url="a", tracked=True),
                   dbmod.Streamer(name="from a link", channel_url="b", tracked=False)])
        s.commit()
    dbmod.init_db(engine)  # every start-up runs the migration again
    dbmod.init_db(engine)
    with Session() as s:
        assert {x.name: x.tracked for x in s.scalars(select(dbmod.Streamer))} == {
            "followed": True, "from a link": False}


def test_old_streamers_get_their_platform_repaired(env):
    engine, Session = env
    with engine.begin() as conn:  # rows as the pre-detection endpoint stored them: all 'youtube'
        conn.execute(text("INSERT INTO streamers (platform, name, channel_url, content_type, tracked) VALUES "
                          "('youtube','t','https://www.twitch.tv/a','gameplay',1),"
                          "('youtube','k','https://kick.com/b','gameplay',1),"
                          "('youtube','y','https://www.youtube.com/@c','gameplay',1),"
                          "('youtube','o','https://other.example/d','gameplay',1),"
                          "('twitch','u','https://unrecognised.example/e','gameplay',1)"))
    dbmod.init_db(engine)
    with Session() as s:
        got = {x.name: x.platform for x in s.scalars(select(dbmod.Streamer))}
    # unrecognised URLs keep whatever platform was stored, including a non-default one
    assert got == {"t": "twitch", "k": "kick", "y": "youtube", "o": "youtube", "u": "twitch"}


def _add_twitch_streamer(Session):
    with Session() as s:
        tw = dbmod.Streamer(name="t", channel_url="https://www.twitch.tv/a", platform="twitch")
        s.add(tw)
        s.commit()
        return tw.id


def test_scan_after_upgrade_reuses_the_migrated_row_and_adds_only_new_videos(env):
    engine, Session = env
    sid = _add_twitch_streamer(Session)
    with Session() as s:  # a Twitch VOD analyzed by the version that stored the bare id
        old = dbmod.Video(streamer_id=sid, platform_id="v123", title="old", url="u", status="analyzed")
        s.add(old)
        s.flush()
        s.add(dbmod.Candidate(video_id=old.id, start_s=0, end_s=30, score=1.0))
        s.commit()

    dbmod.init_db(engine)
    dbmod.init_db(engine)  # idempotent: never "twitch-twitch-"

    assert pipeline.scan_streamer(sid) == 1  # only v999 is new; v123 is recognised, not duplicated
    with Session() as s:
        videos = {v.platform_id: v for v in s.scalars(select(dbmod.Video))}
        assert set(videos) == {"twitch-v123", "twitch-v999"}
        assert videos["twitch-v123"].status == "analyzed"  # not queued for a second analysis
        assert len(videos["twitch-v123"].candidates) == 1   # and its candidates are still attached
        assert videos["twitch-v999"].status == "new"
    assert pipeline.scan_streamer(sid) == 0  # a rescan finds nothing new


def test_repair_leaves_a_row_alone_when_the_prefixed_id_is_already_taken(env):
    engine, Session = env
    sid = _add_twitch_streamer(Session)
    with Session() as s:
        s.add_all([dbmod.Video(streamer_id=sid, platform_id="v123", title="bare", url="u1", status="analyzed"),
                   dbmod.Video(streamer_id=sid, platform_id="twitch-v123", title="prefixed", url="u2")])
        s.commit()

    dbmod.init_db(engine)  # must not violate the unique constraint...
    dbmod.init_db(engine)  # ...on any run

    with Session() as s:
        ids = {v.title: v.platform_id for v in s.scalars(select(dbmod.Video))}
    assert ids == {"bare": "v123", "prefixed": "twitch-v123"}  # nothing lost, nothing overwritten
