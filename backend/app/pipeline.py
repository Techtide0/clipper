import shutil
import traceback

from sqlalchemy import select

from . import tuning
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video
from .signals import audio, chat, keywords
from .signals.shortlist import shortlist
from .sources import detect_platform, get_source
from .sources.base import VideoInfo
from .sources.urls import web_url

MANUAL_CHANNEL = "link:{platform}"  # stand-in channel for videos whose channel we can't determine


def ingest_url(url: str, content_type: str = "gameplay") -> tuple[int, bool]:
    """Register one video from a pasted link. Returns (video_id, newly_created).

    Only YouTube, Twitch and Kick web links are accepted. Raises ValueError with a readable
    reason for anything else, for playlists/channels, and for streams that haven't finished.
    The video is only recorded here; analysis is a separate step (process_video).
    """
    url = web_url(url)
    platform = detect_platform(url)
    meta = get_source(platform).fetch_video(url)
    with SessionLocal() as db:
        existing = db.scalars(select(Video).where(Video.platform_id == meta.video.platform_id)).first()
        if existing:
            return existing.id, False
        channel_url = meta.channel_url or MANUAL_CHANNEL.format(platform=platform)
        streamer = db.scalars(select(Streamer).where(Streamer.channel_url == channel_url)).first()
        if not streamer:  # not tracked: scanning won't start following this channel
            streamer = Streamer(name=meta.channel_name or "Added by link", channel_url=channel_url,
                                platform=platform, content_type=content_type, tracked=False)
            db.add(streamer)
            db.flush()
        v = Video(streamer_id=streamer.id, platform_id=meta.video.platform_id, title=meta.video.title,
                  url=meta.video.url, duration_s=meta.video.duration_s)
        db.add(v)
        db.commit()
        return v.id, True


def analyze_and_judge(video_id: int) -> None:
    """Analyze one video, then have the AI judge its moments if a Gemini key is configured.

    A judging failure (rate limit, overloaded model) is recorded on the video and leaves its
    moments unscored for a later `judge` run; it never undoes the analysis.
    """
    if not process_video(video_id) or not settings.gemini_api_key:
        return
    from .judge import judge_pending  # imported here: the judge needs the Google client library
    try:
        judge_pending(video_id=video_id)
    except Exception as e:
        with SessionLocal() as db:
            v = db.get(Video, video_id)
            v.error = f"AI scoring incomplete ({type(e).__name__}); run judge again later"[:500]
            db.commit()


def _transcript(cues: list[tuple[float, str]], start: float, end: float) -> str:
    lines: list[str] = []
    for t, text in cues:
        if start <= t <= end and (not lines or lines[-1] != text):  # auto-captions repeat lines
            lines.append(text)
    return " ".join(lines)[:2000]


def _chat_sample(msgs: list[tuple[float, str]], start: float, end: float, n: int = 40) -> str:
    inside = [text for t, text in msgs if start <= t <= end and text]
    step = max(1, len(inside) // n)
    return " | ".join(inside[::step][:n])[:1500]


def scan_streamer(streamer_id: int, limit: int = 10) -> int:
    """Discover new videos for a streamer. Returns how many were new."""
    with SessionLocal() as db:
        s = db.get(Streamer, streamer_id)
        known = set(db.scalars(select(Video.platform_id)))
        new = 0
        for v in get_source(s.platform).list_videos(s.channel_url, limit):
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
        out_dir = settings.raw_dir / v.platform_id
        try:
            src = get_source(v.streamer.platform)  # inside the try: an unknown platform fails only this video
            dl = src.download(VideoInfo(v.platform_id, v.title, v.url, v.duration_s), out_dir)
            v.status = "downloaded"
            db.commit()

            loud = audio.loudness_per_second(dl.audio)
            duration = len(loud)
            sig = {"audio": audio.baseline_zscore(loud)}
            msgs, cues = [], []
            if dl.chat_messages:
                msgs = dl.chat_messages
                sig["chat"] = chat.chat_score([t for t, _ in msgs], duration)
            if dl.captions:
                cues = keywords.parse_vtt(dl.captions)
                sig["keywords"] = keywords.keyword_score(cues, duration)
            weights = tuning.current_weights()  # configured weights, plus anything `tune --apply` saved

            moments = shortlist(sig, weights, duration, settings.shortlist_size,
                                settings.clip_window_s, settings.min_gap_s)
            for old in list(v.candidates):
                db.delete(old)
            for m in moments:
                db.add(Candidate(video_id=v.id, start_s=m.start_s, end_s=m.end_s,
                                 score=m.score, signals=m.signals,
                                 transcript=_transcript(cues, m.start_s, m.end_s),
                                 chat_sample=_chat_sample(msgs, m.start_s, m.end_s)))
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
