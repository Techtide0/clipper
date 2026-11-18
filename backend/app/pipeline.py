import shutil
import traceback

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import tuning
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video
from .signals import audio, chat, keywords
from .signals.shortlist import shortlist
from .sources import detect_platform, get_source
from .sources.base import VideoInfo
from .sources.urls import canonical_channel, web_url

MANUAL_CHANNEL = "link:{platform}"  # stand-in channel for videos whose channel we can't determine
IN_FLIGHT = ("queued", "processing")


# ---- claiming: a video is analyzed by exactly one worker ------------------------------------
#
# Status flow: new -> queued -> processing -> analyzed | failed. Each hop is one conditional
# UPDATE, which the database applies atomically, so when two requests race only one gets
# rowcount == 1 and that one alone proceeds. Everyone else sees "someone has it" and backs off.

def _transition(video_id: int, allowed: tuple[str, ...], to: str, *, clear_error: bool = False) -> bool:
    values = {"status": to, **({"error": None} if clear_error else {})}
    with SessionLocal() as db:
        done = db.execute(update(Video).where(Video.id == video_id, Video.status.in_(allowed)).values(**values))
        db.commit()
        return done.rowcount == 1


def claim_video(video_id: int, allowed: tuple[str, ...] = ("new", "failed")) -> bool:
    """Atomically queue a video for analysis. True only for the one caller that won the claim."""
    return _transition(video_id, allowed, "queued", clear_error=True)


def recover_interrupted() -> int:
    """Videos left queued/processing by a process that died can never finish: mark them failed
    so they can be retried. Call at server start, when nothing of ours can be running."""
    with SessionLocal() as db:
        done = db.execute(update(Video).where(Video.status.in_(IN_FLIGHT)).values(
            status="failed", error="interrupted by a restart; paste the link again to retry"))
        db.commit()
        return done.rowcount


# ---- registering a video from a link ---------------------------------------------------------

def _find_streamer(db, aliases: list[str]) -> Streamer | None:
    """A saved channel that any of the given URLs refers to, whichever form it was saved in
    (e.g. YouTube's @handle vs its /channel/UC... URL)."""
    wanted = {canonical_channel(u) for u in aliases}
    for s in db.scalars(select(Streamer)):
        if canonical_channel(s.channel_url) in wanted:
            return s
    return None


def ingest_url(url: str, content_type: str | None = None) -> tuple[int, bool]:
    """Register one video from a pasted link. Returns (video_id, newly_created).

    Only YouTube, Twitch and Kick web links are accepted. Raises ValueError with a readable
    reason for anything else, for playlists/channels, and for streams that haven't finished.
    The video is only recorded here; analysis is a separate step (claim_video, then
    analyze_and_judge). `content_type`, if given, applies to this video only.
    """
    url = web_url(url)
    platform = detect_platform(url)
    meta = get_source(platform).fetch_video(url)
    pid = meta.video.platform_id
    with SessionLocal() as db:
        existing = db.scalars(select(Video).where(Video.platform_id == pid)).first()
        if existing:
            return existing.id, False
        aliases = list(meta.channel_urls) or [MANUAL_CHANNEL.format(platform=platform)]
        streamer = _find_streamer(db, aliases)
        if not streamer:  # not tracked: scanning won't start following this channel
            try:
                with db.begin_nested():  # savepoint: a lost race rolls back only this insert
                    streamer = Streamer(name=meta.channel_name or "Added by link", channel_url=aliases[0],
                                        platform=platform, tracked=False)
                    db.add(streamer)
                    db.flush()
            except IntegrityError:  # another request created the same channel first: use theirs as it is
                streamer = db.scalars(select(Streamer).where(Streamer.channel_url == aliases[0])).one()
        v = Video(streamer_id=streamer.id, platform_id=pid, title=meta.video.title, url=meta.video.url,
                  duration_s=meta.video.duration_s, content_type=content_type)
        db.add(v)
        try:
            db.commit()
        except IntegrityError:  # the same video was registered by a concurrent request
            db.rollback()
            return db.scalars(select(Video).where(Video.platform_id == pid)).one().id, False
        return v.id, True


# ---- analysis ----------------------------------------------------------------------------------

def _finish(video_id: int) -> None:
    _transition(video_id, ("processing",), "analyzed")  # a video that failed stays failed


def _note(video_id: int, text: str) -> None:
    with SessionLocal() as db:
        db.execute(update(Video).where(Video.id == video_id).values(error=text[:500]))
        db.commit()


def _judge_video(video_id: int) -> None:
    from .judge import judge_pending  # imported here: the judge needs the Google client library
    try:
        judge_pending(video_id=video_id)
    except Exception as e:
        _note(video_id, f"AI scoring incomplete ({type(e).__name__}); run judge again later")
        return
    with SessionLocal() as db:  # it can also return normally with some moments left unscored
        left = db.scalar(select(func.count(Candidate.id)).where(
            Candidate.video_id == video_id, Candidate.ai_score.is_(None)))
    if left:
        _note(video_id, f"AI scoring incomplete ({left} moments unscored); run judge again later")


def analyze_and_judge(video_id: int) -> None:
    """Analyze one claimed video, then have the AI judge its moments if a Gemini key is set.

    The video stays 'processing' until scoring is saved, so a dashboard that sees 'analyzed'
    can rely on the moments being complete. A judging failure is recorded on the video and
    leaves its moments unscored for a later `judge` run; it never undoes the analysis.
    """
    n = process_video(video_id, finalize=False)
    if n is None:  # not ours: another worker holds this video, so don't touch its status
        return
    try:
        if n and settings.gemini_api_key:
            _judge_video(video_id)
    finally:
        _finish(video_id)


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


def process_video(video_id: int, *, finalize: bool = True) -> int | None:
    """Download -> signals -> shortlist -> store candidates.

    The video must be 'queued' (see claim_video); this takes it to 'processing' atomically, and
    returns None without doing anything if it wasn't queued (another worker has it, or it is
    already done). Otherwise returns the candidate count (0 if it failed). With finalize=False
    the video is left 'processing' for the caller to finish (see analyze_and_judge).
    """
    if not _transition(video_id, ("queued",), "processing"):
        return None
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        out_dir = settings.raw_dir / v.platform_id
        try:
            src = get_source(v.streamer.platform)  # inside the try: an unknown platform fails only this video
            dl = src.download(VideoInfo(v.platform_id, v.title, v.url, v.duration_s), out_dir)

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
            v.error = None
            if finalize:
                v.status = "analyzed"
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
    """Analyze every video still waiting ('new'), whether or not its channel is tracked."""
    with SessionLocal() as db:
        ids = list(db.scalars(select(Video.id).where(Video.status == "new")))
    for vid in ids:
        if claim_video(vid, ("new",)):  # skipped if something else claimed it meanwhile
            process_video(vid)
