import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import pipeline
from app.config import settings
from app.sources.base import Downloaded, Source


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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    def get_source(platform):  # same behaviour as the real one: KeyError for unknown platforms
        return {"fake": FakeSource()}[platform]

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
        assert cands and set(cands[0].signals) == {"audio"}  # no chat signal, audio still scored
        best = max(cands, key=lambda c: c.score)
        assert 55 <= best.start_s + 18 <= 70  # moments start 18s before their peak; burst was at 60-65s


def test_old_streamers_get_their_platform_repaired(env):
    engine, Session = env
    with engine.begin() as conn:  # rows as the pre-detection endpoint stored them: all 'youtube'
        conn.execute(text("INSERT INTO streamers (platform, name, channel_url, content_type) VALUES "
                          "('youtube','t','https://www.twitch.tv/a','gameplay'),"
                          "('youtube','k','https://kick.com/b','gameplay'),"
                          "('youtube','y','https://www.youtube.com/@c','gameplay'),"
                          "('youtube','o','https://other.example/d','gameplay')"))
    dbmod.init_db(engine)
    with Session() as s:
        got = {x.name: x.platform for x in s.scalars(select(dbmod.Streamer))}
    assert got == {"t": "twitch", "k": "kick", "y": "youtube", "o": "youtube"}
