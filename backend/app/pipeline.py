import shutil
import traceback

from sqlalchemy import select

from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video
from .signals import audio, chat, keywords
from .signals.shortlist import shortlist
from .sources.base import Source, VideoInfo
from .sources.youtube import YouTubeSource

SOURCES: dict[str, Source] = {"youtube": YouTubeSource()}


def scan_streamer(streamer_id: int, limit: int = 10) -> int:
    """Discover new videos for a streamer. Returns how many were new."""
    with SessionLocal() as db:
        s = db.get(Streamer, streamer_id)
        known = set(db.scalars(select(Video.platform_id)))
        new = 0
        for v in SOURCES[s.platform].list_videos(s.channel_url, limit):
            if v.platform_id in known:
                continue
            db.add(Video(streamer_id=s.id, platform_id=v.platform_id, title=v.title,
                         url=v.url, duration_s=v.duration_s))
            new += 1
        db.commit()
        return new


def process_video(video_id: int) -> int:
    """Download -> signals -> shortlist -> store candidates. Returns candidate count."""
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        src = SOURCES[v.streamer.platform]
        out_dir = settings.raw_dir / v.platform_id
        try:
            dl = src.download(VideoInfo(v.platform_id, v.title, v.url, v.duration_s), out_dir)
            v.status = "downloaded"
            db.commit()

            loud = audio.loudness_per_second(dl.audio)
            duration = len(loud)
            sig = {"audio": audio.baseline_zscore(loud)}
            if dl.chat:
                sig["chat"] = chat.chat_score(chat.parse_live_chat(dl.chat), duration)
            if dl.captions:
                sig["keywords"] = keywords.keyword_score(keywords.parse_vtt(dl.captions), duration)
            weights = {"audio": settings.w_audio, "chat": settings.w_chat, "keywords": settings.w_keywords}

            moments = shortlist(sig, weights, duration, settings.shortlist_size,
                                settings.clip_window_s, settings.min_gap_s)
            for old in list(v.candidates):
                db.delete(old)
            for m in moments:
                db.add(Candidate(video_id=v.id, start_s=m.start_s, end_s=m.end_s,
                                 score=m.score, signals=m.signals))
            v.duration_s = duration
            v.status, v.error = "analyzed", None
            db.commit()
            return len(moments)
        except Exception as e:  # keep the batch going; record why this one failed
            v.status, v.error = "failed", f"{type(e).__name__}: {e}"[:500]
            db.commit()
            traceback.print_exc()
            return 0
        finally:
            if settings.delete_raw_after_processing:
                shutil.rmtree(out_dir, ignore_errors=True)


def process_pending() -> None:
    with SessionLocal() as db:
        ids = list(db.scalars(select(Video.id).where(Video.status == "new")))
    for vid in ids:
        process_video(vid)
