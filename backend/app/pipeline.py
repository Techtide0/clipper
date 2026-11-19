import shutil
import subprocess
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import runtime, tuning
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video
from .signals import audio, chat, keywords
from .signals.shortlist import shortlist
from .sources import detect_platform, get_source
from .sources.base import VideoInfo
from .sources.urls import canonical_channel, web_url

MANUAL_CHANNEL = "link:{platform}"  # stand-in channel for videos whose channel we can't determine
IN_FLIGHT = ("queued", "processing")
SCORING_NOTE = "AI scoring"  # every note about unfinished scoring starts with this


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, like the other timestamps


# ---- claiming: a video is analyzed by exactly one worker ------------------------------------
#
# Status flow: new -> queued -> processing -> analyzed | failed.
#
# Each hop is one conditional UPDATE, which the database applies atomically, so when workers race
# only one gets rowcount == 1 and proceeds. A claim also stamps a fresh random token on the row;
# every later write by that worker is conditional on the token still being the row's. A worker
# whose claim was recovered (it stalled, then the claim went stale and someone else took over)
# therefore finds its writes refused instead of overwriting its replacement.
#
# While a worker runs it refreshes claimed_at (heartbeat). A claim whose heartbeat has stopped for
# claim_stale_s is abandoned: its worker died. Only such claims are recovered, never live ones.

def claim_video(video_id: int, allowed: tuple[str, ...] = ("new", "failed"),
                content_type: str | None = None) -> str | None:
    """Atomically queue a video for analysis. Returns the claim token, or None if not claimed.

    `content_type`, if given, is applied in the same step, so an override only ever takes effect
    for the caller that actually won the claim (never for a video already running or finished).
    """
    token = uuid.uuid4().hex
    values = {"status": "queued", "error": None, "claim_token": token, "claimed_at": _now()}
    if content_type:
        values["content_type"] = content_type
    with SessionLocal() as db:
        done = db.execute(update(Video).where(Video.id == video_id, Video.status.in_(allowed)).values(**values))
        db.commit()
        return token if done.rowcount == 1 else None


def _advance(video_id: int, token: str, allowed: tuple[str, ...], to: str, **extra) -> bool:
    """Move the video to `to` only if this worker's claim is still the current one."""
    with SessionLocal() as db:
        done = db.execute(update(Video).where(
            Video.id == video_id, Video.claim_token == token, Video.status.in_(allowed),
        ).values(status=to, claimed_at=_now(), **extra))
        db.commit()
        return done.rowcount == 1


class _Heartbeat:
    """Keeps a claim fresh from a background thread while the (long, blocking) work runs."""

    def __init__(self, video_id: int, token: str):
        self.video_id, self.token = video_id, token
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(settings.heartbeat_s):
            try:
                with SessionLocal() as db:
                    db.execute(update(Video).where(
                        Video.id == self.video_id, Video.claim_token == self.token,
                        Video.status.in_(IN_FLIGHT)).values(claimed_at=_now()))
                    db.commit()
            except Exception:  # a missed beat is harmless; the next one retries
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


def recover_stale(max_age_s: float | None = None) -> int:
    """Mark abandoned claims failed so they can be retried. Returns how many were recovered.

    Only claims whose heartbeat is older than max_age_s (default: claim_stale_s) count, so a worker
    that is still running, whether in this process or another, is never touched. Pass 0 to recover
    everything in flight, which is only right when you know nothing is running.
    """
    cutoff = _now() - timedelta(seconds=settings.claim_stale_s if max_age_s is None else max_age_s)
    with SessionLocal() as db:
        done = db.execute(update(Video).where(
            Video.status.in_(IN_FLIGHT), (Video.claimed_at.is_(None)) | (Video.claimed_at < cutoff),
        ).values(status="failed", claim_token=None,
                 error="its analysis stopped unexpectedly; paste the link again or run `recover`, then retry"))
        db.commit()
        return done.rowcount


# ---- registering channels and videos ---------------------------------------------------------

def _find_streamer(db, aliases: list[str]) -> Streamer | None:
    """A saved channel that any of the given URLs refers to, whichever form it was saved in
    (e.g. YouTube's @handle vs its /channel/UC... URL)."""
    wanted = {canonical_channel(u) for u in aliases}
    for s in db.scalars(select(Streamer)):
        if canonical_channel(s.channel_url) in wanted:
            return s
    return None


def register_streamer(name: str, channel_url: str, content_type: str = "gameplay") -> tuple[int, bool]:
    """Start following a channel. Returns (streamer_id, newly_created). Used by the CLI and the API.

    If the channel is already saved, under any spelling of its URL, that row is reused and made
    tracked, so one channel never ends up as two rows. Raises ValueError for unsupported sites.
    """
    platform = detect_platform(channel_url)
    aliases = [channel_url]
    try:  # best effort: lets '@handle' and '/channel/UC...' be recognised as one channel
        aliases += list(get_source(platform).resolve_channel(channel_url))
    except Exception:
        pass  # offline or unresolvable: exact and canonical-URL matching still apply
    with SessionLocal() as db:
        s = _find_streamer(db, aliases)
        created = False
        if s is None:
            try:
                with db.begin_nested():  # savepoint: a lost race rolls back only this insert
                    s = Streamer(name=name, channel_url=channel_url, platform=platform, content_type=content_type)
                    db.add(s)
                    db.flush()
                created = True
            except IntegrityError:  # someone saved the same channel first: reuse their row
                s = db.scalars(select(Streamer).where(Streamer.channel_url == channel_url)).one()
        if not created:  # e.g. created earlier by pasting one of its videos: just start following it
            s.tracked, s.content_type = True, content_type
        db.commit()
        return s.id, created


def ingest_url(url: str, content_type: str | None = None) -> tuple[int, bool]:
    """Register one video from a pasted link. Returns (video_id, newly_created).

    Only YouTube, Twitch and Kick web links are accepted. Raises ValueError with a readable
    reason for anything else, for playlists/channels, and for streams that haven't finished.
    The video is only recorded here; analysis is a separate step (claim_video, then
    analyze_and_judge). `content_type`, if given, applies to a newly created video; for one that
    already exists it is applied by claim_video, and only if that claim succeeds.
    """
    url = web_url(url)
    platform = detect_platform(url)
    meta = get_source(platform).fetch_video(url)
    pid = meta.video.platform_id  # unique across sites: Twitch and Kick ids are prefixed, YouTube's are 11 random chars
    with SessionLocal() as db:
        existing = db.scalars(select(Video).where(Video.platform_id == pid)).first()
        if existing:
            return existing.id, False
        aliases = list(meta.channel_urls) or [MANUAL_CHANNEL.format(platform=platform)]
        streamer = _find_streamer(db, aliases)
        if not streamer:  # not tracked: scanning won't start following this channel
            try:
                with db.begin_nested():
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

def _note(video_id: int, text: str | None, token: str | None = None) -> None:
    """Set (or with None clear) the video's note. With a token, only if that claim still holds."""
    q = update(Video).where(Video.id == video_id)
    if token:
        q = q.where(Video.claim_token == token)
    with SessionLocal() as db:
        db.execute(q.values(error=text[:500] if text else None))
        db.commit()


def _unscored(video_id: int) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count(Candidate.id)).where(
            Candidate.video_id == video_id, Candidate.ai_score.is_(None))) or 0


def refresh_scoring_notes() -> int:
    """Clear 'scoring incomplete' notes on videos whose moments are all scored now. Returns how
    many were cleared. Run after a retry, so a finished video doesn't keep a stale warning."""
    cleared = 0
    with SessionLocal() as db:
        flagged = list(db.scalars(select(Video.id).where(Video.error.like(f"{SCORING_NOTE}%"))))
    for vid in flagged:
        if _unscored(vid) == 0:
            _note(vid, None)
            cleared += 1
    return cleared


def _judge_video(video_id: int, token: str | None) -> None:
    from .judge import judge_pending  # imported here: the judge needs the Google client library
    try:
        judge_pending(video_id=video_id)
    except Exception as e:
        _note(video_id, f"{SCORING_NOTE} incomplete ({type(e).__name__}); run judge again later", token)
        return
    try:  # it can also return normally with some moments left unscored
        left = _unscored(video_id)
    except Exception:
        _note(video_id, f"{SCORING_NOTE} couldn't be verified; run judge again later", token)
        return
    # a clean pass clears any warning left by an earlier attempt; leftovers keep (or set) it
    _note(video_id, f"{SCORING_NOTE} incomplete ({left} moments unscored); run judge again later" if left else None,
          token)


def analyze_and_judge(video_id: int, token: str) -> None:
    """Analyze one claimed video, then have the AI judge its moments if a Gemini key is set.

    `token` is the claim from claim_video. The video stays 'processing' until scoring is saved,
    so a dashboard that sees 'analyzed' can rely on the moments being complete. A judging failure
    is recorded on the video and leaves its moments unscored for a later `judge` run; it never
    undoes the analysis.
    """
    n = process_video(video_id, token, finalize=False)
    if n is None:  # not ours: our claim was taken over, so don't touch the video
        return
    try:
        if n and settings.gemini_api_key:
            with _Heartbeat(video_id, token):  # judging can take minutes
                _judge_video(video_id, token)
    finally:
        _advance(video_id, token, ("processing",), "analyzed")  # a video that failed stays failed


def cut_moment_audio(wav: Path, start: float, end: float, candidate_id: int) -> str | None:
    """Save the moment's audio as a small mp3 (about 180 KB for 30 s) so the AI can listen to it.

    Returns the file name inside the moments folder, or None if it couldn't be made (the moment is
    then judged from its text alone). Never raises: audio is an extra, not a requirement.
    """
    name = f"{candidate_id}.mp3"
    dest = settings.moments_dir / name
    try:
        settings.moments_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{max(end - start, 1):.2f}",
             "-i", str(wav), "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(dest)],
            check=True, capture_output=True, timeout=120)
        if dest.is_file() and dest.stat().st_size > 0:
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    dest.unlink(missing_ok=True)
    return None


def remove_moment_audio(names) -> None:
    root = settings.moments_dir.resolve()
    for name in names:
        if not name:
            continue
        path = (root / name).resolve()
        if path.is_relative_to(root):  # only ever files inside the moments folder
            path.unlink(missing_ok=True)


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
    """Discover new videos for a streamer. Returns how many were new.

    Records when the channel was checked and, if the check failed, why (then re-raises).
    """
    with SessionLocal() as db:
        s = db.get(Streamer, streamer_id)
        known = set(db.scalars(select(Video.platform_id)))
        new = 0
        try:
            for v in get_source(s.platform).list_videos(s.channel_url, limit):
                if v.platform_id in known:
                    continue
                db.add(Video(streamer_id=s.id, platform_id=v.platform_id, title=v.title,
                             url=v.url, duration_s=v.duration_s))
                new += 1
            s.last_scan_at, s.last_scan_error = _now(), None
            db.commit()
            return new
        except Exception as e:
            db.rollback()
            db.execute(update(Streamer).where(Streamer.id == streamer_id).values(
                last_scan_at=_now(), last_scan_error=f"{type(e).__name__}: {e}"[:500]))
            db.commit()
            raise


class _Saved:
    """The file names of a moment that is about to be (or has been) deleted from the database."""

    def __init__(self, audio_path, vertical_path, wide_path, id):
        self.audio_path, self.vertical_path, self.wide_path, self.id = audio_path, vertical_path, wide_path, id


def remove_candidate_files(candidates) -> None:
    """Delete everything stored on disk for these moments: audio snippet, cut clips, exports."""
    remove_moment_audio(c.audio_path for c in candidates)
    clips, exports = settings.clips_dir.resolve(), settings.exports_dir.resolve()
    for c in candidates:
        for name in (c.vertical_path, c.wide_path):
            if name and (p := (clips / name).resolve()).is_relative_to(clips):
                p.unlink(missing_ok=True)
        folder = (exports / str(c.id)).resolve()
        if folder.is_relative_to(exports) and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


def delete_video(video_id: int) -> None:
    """Remove a video, its moments and every file made for them (clips, exports, audio, full copy).

    Raises LookupError if there is no such video, and ValueError (with a reason) if something is
    still running on it, because deleting underneath a running job would corrupt it.
    """
    from . import fullvideo  # local import keeps module start-up simple
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        if v is None:
            raise LookupError("no such video")
        if v.status in IN_FLIGHT:
            raise ValueError("it is being analyzed right now; try again when it has finished")
        if v.full_status in fullvideo.BUSY:
            raise ValueError("its full video is downloading right now; try again when it has finished")
        if any(c.render_status == "rendering" for c in v.candidates):
            raise ValueError("a clip from it is being cut right now; try again when it has finished")
        cands = list(v.candidates)
        full = fullvideo.full_path_of(v.full_path)
        remove_files = [(c.audio_path, c.vertical_path, c.wide_path, c.id) for c in cands]
        for c in cands:
            db.delete(c)
        db.delete(v)
        db.commit()
    # files go only after the rows are gone, so a failure above never leaves rows pointing at nothing
    remove_candidate_files([_Saved(*row) for row in remove_files])
    if full:
        full.unlink(missing_ok=True)


_SCORING: set[int] = set()
_SCORING_LOCK = threading.Lock()


def rescore(video_id: int) -> bool:
    """Ask the AI to score whatever is still unscored on this video. False if already running."""
    with _SCORING_LOCK:
        if video_id in _SCORING:
            return False
        _SCORING.add(video_id)
    try:
        _judge_video(video_id, None)  # records its own note on failure, or clears the old one on success
    finally:
        with _SCORING_LOCK:
            _SCORING.discard(video_id)
    return True


def process_video(video_id: int, token: str, *, finalize: bool = True) -> int | None:
    """Download -> signals -> shortlist -> store candidates.

    `token` is the claim from claim_video. This takes the video from 'queued' to 'processing'
    atomically and returns None, doing nothing, if this claim no longer holds. Otherwise it
    returns the candidate count (0 if it failed). Results are written only while the claim still
    holds, so a worker that stalled past its claim cannot overwrite whoever took over. With
    finalize=False the video is left 'processing' for the caller to finish (see analyze_and_judge).
    """
    if not _advance(video_id, token, ("queued",), "processing"):
        return None
    with _Heartbeat(video_id, token), SessionLocal() as db:
        v = db.get(Video, video_id)
        # private to this attempt, so a stalled earlier attempt can't delete a replacement's files
        out_dir = settings.raw_dir / f"{v.platform_id}-{token[:8]}"
        made: list[str] = []  # moment-audio files this attempt created, removed again if the attempt is discarded
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

            moments = shortlist(sig, weights, duration, runtime.get("shortlist_size"),
                                settings.clip_window_s, settings.min_gap_s)
            replaced = list(v.candidates)  # files of the moments being replaced are cleaned up after the commit
            replaced_files = [(c.audio_path, c.vertical_path, c.wide_path, c.id) for c in replaced]
            for old in replaced:  # a retry replaces the earlier attempt's moments, never adds to them
                db.delete(old)
            fresh = []
            for m in moments:
                cand = Candidate(video_id=v.id, start_s=m.start_s, end_s=m.end_s,
                                 score=m.score, signals=m.signals,
                                 transcript=_transcript(cues, m.start_s, m.end_s),
                                 chat_sample=_chat_sample(msgs, m.start_s, m.end_s))
                db.add(cand)
                fresh.append((cand, m))
            if settings.judge_audio and fresh:
                db.flush()  # the snippets are named after the moments' ids
                for cand, m in fresh:
                    name = cut_moment_audio(dl.audio, m.start_s, m.end_s, cand.id)
                    if name:
                        cand.audio_path = name
                        made.append(name)
            done = db.execute(update(Video).where(  # the claim check and the results commit together
                Video.id == video_id, Video.claim_token == token, Video.status == "processing",
            ).values(duration_s=duration, error=None, claimed_at=_now(),
                     **({"status": "analyzed"} if finalize else {})))
            if done.rowcount != 1:  # our claim was recovered and given to someone else: write nothing
                db.rollback()
                remove_moment_audio(made)
                return None
            db.commit()
            remove_candidate_files([_Saved(*row) for row in replaced_files])  # their audio, clips and exports
            return len(moments)
        except Exception as e:  # keep the batch going; record why this one failed
            db.rollback()
            remove_moment_audio(made)
            _advance(video_id, token, ("processing",), "failed", error=f"{type(e).__name__}: {e}"[:500])
            traceback.print_exc()
            return 0
        finally:
            if runtime.get("delete_working_files"):
                shutil.rmtree(out_dir, ignore_errors=True)


def process_pending() -> None:
    """Analyze every video still waiting ('new'), whether or not its channel is tracked."""
    with SessionLocal() as db:
        ids = list(db.scalars(select(Video.id).where(Video.status == "new")))
    for vid in ids:
        token = claim_video(vid, ("new",))  # skipped if something else claimed it meanwhile
        if token:
            process_video(vid, token)
