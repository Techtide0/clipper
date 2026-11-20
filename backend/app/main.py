import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, update

from . import automation, editor, export, fullvideo, health, pipeline, runtime, tuning, youtube
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    pipeline.recover_stale()  # frees claims whose worker died; a run still beating (e.g. from the CLI) is left alone
    editor.recover_renders()  # a restart killed any clip that was being cut
    fullvideo.recover_full()  # ...and any full-video download in progress
    youtube.recover_uploads()  # ...and any YouTube upload in progress
    scheduler = automation.Scheduler()
    scheduler.start()  # runs checks only while automatic mode is on (see Settings)
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="Clipper", lifespan=lifespan)
app.mount("/clips", StaticFiles(directory=settings.clips_dir), name="clips")
app.mount("/exports", StaticFiles(directory=settings.exports_dir), name="exports")

CONTENT_TYPES = ("gameplay", "news", "commentary")
MAX_COUNT = 2**53 - 1  # Number.MAX_SAFE_INTEGER


def _streamer(s: Streamer, videos: int, waiting: int) -> dict:
    return dict(id=s.id, name=s.name, platform=s.platform, channel_url=s.channel_url,
                content_type=s.content_type, tracked=s.tracked, videos=videos, waiting=waiting,
                last_scan_at=s.last_scan_at.isoformat() if s.last_scan_at else None,
                last_scan_error=s.last_scan_error)


def _streamer_counts(db) -> dict[int, tuple[int, int]]:
    """(videos, videos still waiting) per channel, in one grouped query."""
    rows = db.execute(select(Video.streamer_id, func.count(Video.id),
                             func.sum(case((Video.status == "new", 1), else_=0))).group_by(Video.streamer_id)).all()
    return {sid: (n, int(w or 0)) for sid, n, w in rows}


@app.get("/streamers")
def streamers():
    with SessionLocal() as db:
        counts = _streamer_counts(db)
        return [_streamer(s, *counts.get(s.id, (0, 0))) for s in db.scalars(select(Streamer).order_by(Streamer.name))]


class StreamerEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    content_type: str | None = None
    tracked: bool | None = None  # follow (True) or stop following (False)


@app.patch("/streamers/{sid}")
def edit_streamer(sid: int, edit: StreamerEdit):
    if edit.content_type is not None and edit.content_type not in CONTENT_TYPES:
        raise HTTPException(422, f"content_type must be one of {CONTENT_TYPES}")
    with SessionLocal() as db:
        s = db.get(Streamer, sid)
        if s is None:
            raise HTTPException(404, "no such channel")
        for k, v in edit.model_dump(exclude_none=True).items():
            setattr(s, k, v)
        db.commit()
        counts = _streamer_counts(db)
        return _streamer(s, *counts.get(s.id, (0, 0)))


@app.delete("/streamers/{sid}")
def delete_streamer(sid: int):
    """Remove a channel. Only possible when it has no videos; otherwise stop following it instead."""
    with SessionLocal() as db:
        s = db.get(Streamer, sid)
        if s is None:
            raise HTTPException(404, "no such channel")
        if db.scalar(select(func.count(Video.id)).where(Video.streamer_id == sid)):
            raise HTTPException(409, "this channel has videos; stop following it instead (or delete its videos first)")
        db.delete(s)
        db.commit()
    return {"deleted": sid}


@app.post("/streamers")
def add_streamer(name: str, channel_url: str, content_type: str = "gameplay"):
    if content_type not in CONTENT_TYPES:
        raise HTTPException(422, f"content_type must be one of {CONTENT_TYPES}")
    try:
        sid, created = pipeline.register_streamer(name, channel_url, content_type)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return dict(id=sid, created=created)


@app.post("/streamers/{sid}/scan")
def scan(sid: int):
    """Look for new videos on this channel now. Their analysis waits for automatic mode or you."""
    with SessionLocal() as db:
        if db.get(Streamer, sid) is None:
            raise HTTPException(404, "no such channel")
    try:
        return {"new": pipeline.scan_streamer(sid)}
    except Exception as e:  # the reason is also saved on the channel, so the screen can show it later
        raise HTTPException(502, f"couldn't check this channel: {type(e).__name__}: {str(e)[:200]}")


def _clips_mb(v: Video) -> float:
    total = 0
    for c in v.candidates:
        for name in (c.vertical_path, c.wide_path):
            p = (settings.clips_dir / name) if name else None
            if p and p.is_file():
                total += p.stat().st_size
    return round(total / 1e6, 1)


def _video(v: Video, moments: int, unscored: int = 0) -> dict:
    path = fullvideo.full_path_of(v.full_path) if v.full_status == "done" else None
    return dict(id=v.id, title=v.title, url=v.url, status=v.status, error=v.error,
                channel=v.streamer.name, platform=v.streamer.platform, moments=moments, unscored=unscored,
                content_type=v.content_type or v.streamer.content_type, clips_mb=_clips_mb(v),
                added=v.discovered_at.isoformat() if v.discovered_at else None,
                duration_s=v.duration_s, full_status=v.full_status, full_progress=v.full_progress,
                full_error=v.full_error, full_size_mb=round(path.stat().st_size / 1e6, 1) if path else None)


def _moment_counts(db, ids: list[int]) -> dict[int, int]:
    """Candidates per video in one grouped query, without loading the candidate rows."""
    if not ids:
        return {}
    rows = db.execute(select(Candidate.video_id, func.count(Candidate.id))
                      .where(Candidate.video_id.in_(ids)).group_by(Candidate.video_id)).all()
    return dict(rows)


def _unscored_counts(db, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = db.execute(select(Candidate.video_id, func.count(Candidate.id))
                      .where(Candidate.video_id.in_(ids), Candidate.ai_score.is_(None))
                      .group_by(Candidate.video_id)).all()
    return dict(rows)


@app.get("/videos")
def videos(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """Newest first, `limit` at a time; pass `offset` to page back to older ones."""
    with SessionLocal() as db:
        vs = list(db.scalars(select(Video).order_by(Video.discovered_at.desc(), Video.id.desc())
                             .limit(limit).offset(offset)))
        ids = [v.id for v in vs]
        counts, unscored = _moment_counts(db, ids), _unscored_counts(db, ids)
        return [_video(v, counts.get(v.id, 0), unscored.get(v.id, 0)) for v in vs]


@app.get("/videos/{vid}")
def get_video(vid: int):
    """One video's status, for following it to completion (it may have scrolled off /videos)."""
    with SessionLocal() as db:
        v = db.get(Video, vid)
        if not v:
            raise HTTPException(404)
        return _video(v, _moment_counts(db, [v.id]).get(v.id, 0), _unscored_counts(db, [v.id]).get(v.id, 0))


@app.delete("/videos/{vid}")
def delete_video(vid: int):
    """Remove a video and everything made from it (moments, clips, exports, full copy)."""
    try:
        pipeline.delete_video(vid)
    except LookupError:
        raise HTTPException(404, "no such video")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"deleted": vid}


@app.post("/videos/{vid}/reanalyze")
def reanalyze_video(vid: int, bg: BackgroundTasks):
    """Analyze the video again from scratch. Its current moments (and any clips cut from them) are replaced."""
    with SessionLocal() as db:
        if db.get(Video, vid) is None:
            raise HTTPException(404, "no such video")
    token = pipeline.claim_video(vid, allowed=("new", "failed", "analyzed"))
    if not token:
        raise HTTPException(409, "that video is already queued or running")
    bg.add_task(pipeline.analyze_and_judge, vid, token)
    return {"queued": vid}


@app.post("/videos/{vid}/rescore")
def rescore_video(vid: int, bg: BackgroundTasks):
    """Ask the AI to score the moments of this video that are still unscored."""
    with SessionLocal() as db:
        if db.get(Video, vid) is None:
            raise HTTPException(404, "no such video")
        if not _unscored_counts(db, [vid]).get(vid):
            raise HTTPException(409, "every moment of this video is already scored")
    if not settings.gemini_api_key:
        raise HTTPException(409, "no AI key is set; put GEMINI_API_KEY=... in backend/.env and restart")
    bg.add_task(pipeline.rescore, vid)
    return {"queued": vid}


@app.post("/videos/{vid}/full-download")
def start_full_download(vid: int, bg: BackgroundTasks):
    """Download the whole original video (opt-in; it can be several GB)."""
    with SessionLocal() as db:
        if db.get(Video, vid) is None:
            raise HTTPException(404, "no such video")
    if not fullvideo.claim_full(vid):  # atomic: a double-click can't start it twice
        raise HTTPException(409, "the full video is already downloading or downloaded")
    bg.add_task(fullvideo.download_full, vid)
    return {"queued": vid}


@app.get("/videos/{vid}/full")
def get_full_video(vid: int):
    """The downloaded full video, saved under a name made from its title."""
    with SessionLocal() as db:
        v = db.get(Video, vid)
        if v is None:
            raise HTTPException(404, "no such video")
        if v.full_status != "done":
            raise HTTPException(409, "the full video hasn't been downloaded")
        stored, title = v.full_path, v.title
    path = fullvideo.full_path_of(stored)
    if path is None:  # deleted by hand, or moved: tell the truth and let it be downloaded again
        fullvideo._set(vid, full_status="none", full_path=None, full_progress=None)
        raise HTTPException(404, "the file is gone; download it again")
    return FileResponse(path, media_type="video/mp4", filename=fullvideo.download_name(title, vid))


@app.delete("/videos/{vid}/full")
def delete_full_video(vid: int):
    """Delete the downloaded copy to free disk space (the clips and moments are unaffected)."""
    if not fullvideo.delete_full(vid):
        raise HTTPException(404, "there is no downloaded copy")
    return {"deleted": vid}


class VideoLink(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    # None = use the channel's type; a value applies to this video only
    content_type: str | None = None


@app.post("/videos/from-url")
def add_video_link(link: VideoLink, bg: BackgroundTasks):
    """Paste a YouTube, Twitch or Kick video link: register it and analyze it in the background."""
    if link.content_type is not None and link.content_type not in CONTENT_TYPES:
        raise HTTPException(422, f"content_type must be one of {CONTENT_TYPES}")
    try:
        vid, created = pipeline.ingest_url(link.url, link.content_type)
    except ValueError as e:  # unsupported site, playlist/channel, unfinished stream, unreadable link
        raise HTTPException(422, str(e))
    except Exception:
        raise HTTPException(502, "couldn't reach the site to read that link; try again in a moment")
    # One atomic claim decides who runs the analysis: a brand-new video, one registered earlier
    # but never analyzed ('new') and one that failed all qualify, while a video that is already
    # queued, running or finished does not, so simultaneous pastes can never start it twice.
    # A type given with the link applies only if this request wins the claim, so it can never
    # change a video that is already running or finished.
    token = pipeline.claim_video(vid, content_type=link.content_type)
    if token:
        bg.add_task(pipeline.analyze_and_judge, vid, token)
    with SessionLocal() as db:
        v = db.get(Video, vid)
        return dict(video_id=vid, title=v.title, created=created, queued=bool(token), status=v.status)


@app.post("/videos/{vid}/process")
def process(vid: int, bg: BackgroundTasks):
    with SessionLocal() as db:
        if db.get(Video, vid) is None:
            raise HTTPException(404, "no such video")
    token = pipeline.claim_video(vid)
    if not token:  # the video exists but isn't claimable
        raise HTTPException(409, "that video is already queued, running or finished")
    bg.add_task(pipeline.process_video, vid, token)
    return {"queued": vid}


def _cand(c: Candidate) -> dict:
    return dict(
        id=c.id, video_id=c.video_id, video_title=c.video.title, video_url=c.video.url,
        content_type=c.video.content_type or c.video.streamer.content_type, platform=c.video.streamer.platform,
        youtube_id=c.video.platform_id, start_s=c.start_s, end_s=c.end_s, score=c.score,
        signals=c.signals, ai_score=c.ai_score, title=c.title, hook=c.hook, caption=c.caption,
        ai_reason=c.ai_reason, transcript=c.transcript, review_status=c.review_status, layout=c.layout,
        zoom=c.zoom, pan_x=c.pan_x, pan_y=c.pan_y,
        posted_at=c.posted_at.isoformat() if c.posted_at else None,
        views=c.views, likes=c.likes,
        render_status=c.render_status, render_error=c.render_error,
        yt_status=c.yt_status, yt_progress=c.yt_progress, yt_error=c.yt_error,
        yt_url=f"https://www.youtube.com/watch?v={c.yt_video_id}" if c.yt_video_id else None,
        vertical_url=f"/clips/{c.vertical_path}" if c.vertical_path else None,
        wide_url=f"/clips/{c.wide_path}" if c.wide_path else None,
    )


@app.get("/candidates")
def candidates(status: str | None = None, video_id: int | None = None, limit: int = 100):
    with SessionLocal() as db:
        q = select(Candidate).order_by(Candidate.ai_score.desc().nulls_last(), Candidate.score.desc())
        if status:
            q = q.where(Candidate.review_status == status)
        if video_id:
            q = q.where(Candidate.video_id == video_id)
        return [_cand(c) for c in db.scalars(q.limit(limit))]


class CandidateEdit(BaseModel):
    review_status: str | None = None   # pending | approved | rejected
    title: str | None = None
    hook: str | None = None
    caption: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    # results entered after posting; capped at JavaScript's largest exactly-representable integer
    # so the dashboard shows back exactly what was stored. Sending null clears a saved value.
    layout: Literal["fit", "fill"] | None = None   # how the 9:16 clip is framed
    zoom: float | None = Field(default=None, ge=1.0, le=3.0)       # 1 = whole picture
    pan_x: float | None = Field(default=None, ge=0.0, le=1.0)      # where the zoomed picture sits; 0.5 = centred
    pan_y: float | None = Field(default=None, ge=0.0, le=1.0)
    posted: bool | None = None                      # True = mark as posted now, False = unmark
    views: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    likes: int | None = Field(default=None, ge=0, le=MAX_COUNT)


@app.patch("/candidates/{cid}")
def edit_candidate(cid: int, edit: CandidateEdit, bg: BackgroundTasks):
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        if not c:
            raise HTTPException(404)
        data = edit.model_dump(exclude_none=True)
        for k in ("views", "likes"):  # an explicit null (not just an absent field) clears the saved value
            if k in edit.model_fields_set and getattr(edit, k) is None:
                data[k] = None
        if data.get("review_status") not in (None, "pending", "approved", "rejected"):
            raise HTTPException(422, "invalid review_status")
        new_start, new_end = data.get("start_s", c.start_s), data.get("end_s", c.end_s)
        if new_end - new_start < 3 or new_start < 0:
            raise HTTPException(422, "clip must be at least 3 seconds and start at or after 0")
        # a different trim or framing makes the existing cut stale
        framing_now = (c.layout, c.zoom, c.pan_x, c.pan_y)
        framing_new = tuple(data.get(k, getattr(c, k)) for k in ("layout", "zoom", "pan_x", "pan_y"))
        trim_changed = (new_start, new_end) != (c.start_s, c.end_s) or framing_new != framing_now
        if trim_changed and c.render_status == "done":
            c.render_status = "none"
        if "posted" in data:  # not a column: it is recorded as the time it was marked
            c.posted_at = pipeline._now() if data.pop("posted") else None
        for k, v in data.items():
            setattr(c, k, v)
        db.commit()
        # Approving a clip (or re-trimming an approved one) cuts it right away, so its downloads are
        # ready by the time you look. It joins the one-at-a-time render queue. Other edits (a
        # caption, view counts) never start a render, so a clip whose render failed isn't retried
        # on every keystroke; use the Cut clip button for that.
        wants_cut = c.review_status == "approved" and (data.get("review_status") == "approved" or trim_changed)
        queued = wants_cut and _claim_render(db, cid, approved_only=True)
        if queued:
            db.refresh(c)
            bg.add_task(editor.render_candidate, cid)
        return _cand(c)


@app.get("/candidates/{cid}/export")
def get_export(cid: int):
    """The manifest from the last export, if there is one."""
    path = settings.exports_dir / str(cid) / "manifest.json"
    if not path.exists():
        raise HTTPException(404, "not exported yet")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/candidates/{cid}/export")
def export_clip(cid: int):
    try:
        return export.export_candidate(cid)
    except LookupError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(409, str(e))


def _claim_render(db, cid: int, *, approved_only: bool = False) -> bool:
    """Atomically mark a clip 'rendering'. False if it already is (or isn't eligible): only the
    request that wins this update queues the work, so a double-click can't render a clip twice."""
    q = update(Candidate).where(Candidate.id == cid, Candidate.render_status != "rendering")
    if approved_only:
        q = q.where(Candidate.review_status == "approved", Candidate.render_status.in_(("none", "failed")))
    done = db.execute(q.values(render_status="rendering", render_error=None))
    db.commit()
    return done.rowcount == 1


@app.post("/candidates/render-approved")
def render_all_approved(bg: BackgroundTasks):
    """Queue every approved clip that hasn't been rendered (or whose render failed).

    They are cut one after another, not at the same time, so this is safe on a laptop.
    """
    with SessionLocal() as db:
        ids = list(db.scalars(select(Candidate.id).where(
            Candidate.review_status == "approved", Candidate.render_status.in_(("none", "failed")),
        ).order_by(Candidate.ai_score.desc().nulls_last(), Candidate.id)))
        queued = [cid for cid in ids if _claim_render(db, cid, approved_only=True)]
    for cid in queued:
        bg.add_task(editor.render_candidate, cid)
    return {"queued": queued}


@app.post("/candidates/{cid}/render")
def render(cid: int, bg: BackgroundTasks):
    with SessionLocal() as db:
        if db.get(Candidate, cid) is None:
            raise HTTPException(404)
        if not _claim_render(db, cid):
            raise HTTPException(409, "already rendering")
    bg.add_task(editor.render_candidate, cid)
    return {"queued": cid}


@app.get("/candidates/{cid}/download/{fmt}")
def download_clip(cid: int, fmt: Literal["vertical", "wide"]):
    """The rendered clip as a download named after its title, ready to post."""
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        if c is None:
            raise HTTPException(404, "no such clip")
        if c.render_status != "done":
            raise HTTPException(409, "this clip hasn't been rendered yet")
        stored, title = (c.vertical_path if fmt == "vertical" else c.wide_path), c.title
    root = settings.clips_dir.resolve()
    path = (root / (stored or "")).resolve()
    if not stored or not path.is_relative_to(root) or not path.is_file():  # only files inside the clips folder
        raise HTTPException(404, "the clip's file is missing; render it again")
    return FileResponse(path, media_type="video/mp4", filename=editor.download_name(title, cid, fmt))


# ---- settings, health, automatic mode, today's summary and performance ---------------------------------

@app.get("/settings")
def get_settings():
    """Every editable setting with its label, help text, range, default and current value."""
    return runtime.describe()


@app.put("/settings")
def put_settings(changes: dict):
    """Change some settings. All or nothing: one invalid value refuses the whole change."""
    try:
        runtime.update(changes)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return runtime.describe()


@app.get("/health")
def get_health():
    """Is everything the app needs in place, and how much space is it using?"""
    return health.check()


@app.get("/automation")
def get_automation():
    return automation.status()


@app.post("/automation/run")
def run_automation_now(bg: BackgroundTasks):
    """Run one automatic check right now, whether or not automatic mode is on."""
    if automation.is_running():
        raise HTTPException(409, "a check is already running")
    bg.add_task(automation.run_cycle)
    return {"started": True}


@app.get("/summary")
def summary():
    """The numbers for a 'today' screen, in one call."""
    with SessionLocal() as db:
        def count(*where):
            return db.scalar(select(func.count(Candidate.id)).where(*where)) or 0

        def vcount(*where):
            return db.scalar(select(func.count(Video.id)).where(*where)) or 0

        return dict(
            to_review=count(Candidate.review_status == "pending"),
            approved=count(Candidate.review_status == "approved"),
            rejected=count(Candidate.review_status == "rejected"),
            cutting=count(Candidate.render_status == "rendering"),
            ready_to_post=count(Candidate.review_status == "approved", Candidate.render_status == "done",
                                Candidate.posted_at.is_(None)),
            cuts_failed=count(Candidate.review_status != "rejected", Candidate.render_status == "failed"),
            waiting_to_cut=count(Candidate.review_status == "approved", Candidate.render_status.in_(("none", "failed"))),
            unscored=count(Candidate.ai_score.is_(None), Candidate.review_status != "rejected"),
            analyzing=vcount(Video.status.in_(pipeline.IN_FLIGHT)),
            videos_waiting=vcount(Video.status == "new"),
            videos_failed=vcount(Video.status == "failed"),
            downloading_full=vcount(Video.full_status.in_(fullvideo.BUSY)),
            automation=automation.status(),
        )


@app.get("/performance")
def performance():
    """What performed, and what the app would change about its scoring because of it."""
    rep = tuning.analyze(tuning.collect_rows())
    with SessionLocal() as db:
        posted = [dict(id=c.id, title=c.title, views=c.views, likes=c.likes, ai_score=c.ai_score,
                       signals=c.signals, content_type=c.video.content_type or c.video.streamer.content_type)
                  for c in db.scalars(select(Candidate).where(Candidate.views.is_not(None))
                                      .order_by(Candidate.views.desc()))]
    return dict(clips_with_views=rep.n, enough_data=rep.enough, needs=tuning.MIN_CLIPS,
                correlations={k: dict(correlation=round(r, 3), clips=n) for k, (r, n) in rep.correlations.items()},
                current_weights=rep.current, suggested_weights=rep.suggested, notes=rep.notes, posted=posted)


@app.post("/performance/apply")
def apply_performance():
    """Save the suggested weights so future analyses use them."""
    rep = tuning.analyze(tuning.collect_rows())
    if not rep.enough:
        raise HTTPException(409, rep.notes[0] if rep.notes else "not enough data yet")
    tuning.save_weights(rep.suggested)
    return {"applied": rep.suggested}


@app.post("/performance/reset")
def reset_performance():
    """Go back to the default weights."""
    return {"reset": tuning.reset_weights()}


# ---- YouTube: post clips and read their views back ----------------------------------------------------------

@app.get("/youtube")
def youtube_status():
    return youtube.status()


@app.post("/youtube/connect")
def youtube_connect():
    """Open Google's sign-in in the browser (on this computer). Poll GET /youtube for the result."""
    if not youtube.secrets_path().is_file():
        raise HTTPException(409, f"the Google client file is missing: save it as {youtube.secrets_path()} (see the setup steps)")
    if not youtube.start_connect():
        raise HTTPException(409, "a sign-in window is already open")
    return {"started": True}


@app.post("/youtube/disconnect")
def youtube_disconnect():
    youtube.disconnect()
    return {"disconnected": True}


@app.post("/youtube/refresh-stats")
def youtube_refresh_stats():
    """Fetch views and likes of every uploaded clip from YouTube."""
    try:
        return {"updated": youtube.refresh_stats()}
    except youtube.NotConnected as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, youtube._friendly(e))


@app.post("/candidates/{cid}/publish/youtube")
def publish_to_youtube(cid: int, bg: BackgroundTasks):
    """Upload the clip's 9:16 video to YouTube (private by default; see Settings)."""
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        if c is None:
            raise HTTPException(404, "no such clip")
        state = (c.review_status, c.render_status, c.yt_status)
    if not youtube.status()["connected"]:
        raise HTTPException(409, "YouTube isn't connected yet: open Settings & health and press Connect YouTube")
    if not youtube.claim_upload(cid):
        review, render, yt = state
        if yt in ("uploading", "uploaded"):
            raise HTTPException(409, "this clip is already uploading or uploaded")
        raise HTTPException(409, "only an approved clip that has been cut can be posted" if (review, render) != ("approved", "done")
                            else "this clip can't be posted right now")
    bg.add_task(youtube.upload, cid)
    return {"queued": cid}


# ---- housekeeping and scoring weights --------------------------------------------------------------------------------

@app.post("/cleanup")
def cleanup_files():
    """Delete exports older than 30 days and leftover working files. Cut clips and downloads are never touched."""
    import shutil
    import time
    freed, removed, cutoff = 0, 0, time.time() - 30 * 86400
    for root, older_than in ((settings.exports_dir, cutoff), (settings.raw_dir, time.time() - 86400)):
        for p in root.iterdir() if root.exists() else []:
            try:
                if p.stat().st_mtime >= older_than:
                    continue
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                freed, removed = freed + size, removed + 1
            except OSError:
                pass  # in use or already gone: leave it
    health.invalidate_storage_cache()
    return {"freed_mb": round(freed / 1e6, 1), "removed": removed}


@app.post("/recover")
def recover_stalled():
    """Free analyses whose worker died (nothing is touched while a worker is still running)."""
    return {"recovered": pipeline.recover_stale()}


@app.get("/weights")
def get_weights():
    return {"current": tuning.current_weights(), "defaults": tuning.base_weights(),
            "min": tuning.WEIGHT_FLOOR, "max": tuning.WEIGHT_CEIL}


@app.put("/weights")
def put_weights(weights: dict):
    """Set how much each signal counts when moments are picked. Each must be between the minimum and maximum."""
    clean = {}
    for k, v in weights.items():
        if k not in tuning.SIGNALS:
            raise HTTPException(422, f"unknown signal: {k}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (tuning.WEIGHT_FLOOR <= v <= tuning.WEIGHT_CEIL):
            raise HTTPException(422, f"{k}: must be a number between {tuning.WEIGHT_FLOOR:g} and {tuning.WEIGHT_CEIL:g}")
        clean[k] = float(v)
    tuning.save_weights({**tuning.current_weights(), **clean})
    return get_weights()


@app.get("/jobs")
def jobs():
    """Everything running in the background right now, for the 'Background work' list."""
    out = []
    with SessionLocal() as db:
        for v in db.scalars(select(Video).where(Video.status.in_(pipeline.IN_FLIGHT)).order_by(Video.id)):
            out.append(dict(kind="analyzing" if v.status == "processing" else "queued", title=v.title,
                            detail="Finding moments and scoring them; a few minutes." if v.status == "processing"
                            else "Starts when the current analysis finishes.", pct=None))
        for v in db.scalars(select(Video).where(Video.full_status.in_(fullvideo.BUSY)).order_by(Video.id)):
            out.append(dict(kind="full video", title=v.title, pct=v.full_progress if v.full_status == "downloading" else None,
                            detail="Waiting to download…" if v.full_status == "queued" else f"Downloading {v.full_progress or 0}%"))
        for c in db.scalars(select(Candidate).where(Candidate.render_status == "rendering").order_by(Candidate.id)):
            out.append(dict(kind="cutting", title=c.title or f"Moment {c.id}", pct=None,
                            detail="Cutting both formats; about a minute. Clips are cut one at a time."))
        for c in db.scalars(select(Candidate).where(Candidate.yt_status == "uploading").order_by(Candidate.id)):
            out.append(dict(kind="uploading", title=c.title or f"Moment {c.id}", pct=c.yt_progress,
                            detail=f"Uploading to YouTube {c.yt_progress or 0}%"))
    return out


# ---- one-command startup: serve the built dashboard from this same server ----------------------------------
# KEEP THIS LAST in the file. The dashboard is mounted at '/', which matches every path, so anything
# registered after it would be unreachable (a test guards this).

DASHBOARD_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def add_api_prefix_support(application: FastAPI) -> None:
    """Accept /api/... as well as /...: the dashboard always calls /api/..., which the development
    server rewrites, and this does the same when the built dashboard is served by this server."""
    @application.middleware("http")
    async def strip_api_prefix(request, call_next):
        path = request.scope["path"]
        if path.startswith("/api/"):
            request.scope["path"] = path[4:]
            raw = request.scope.get("raw_path")
            if raw and raw.startswith(b"/api/"):
                request.scope["raw_path"] = raw[4:]
        return await call_next(request)


def serve_dashboard(application: FastAPI, dist: Path) -> bool:
    """Serve the built dashboard at '/' if it has been built. Registered last, so it never hides an API route."""
    if not (dist / "index.html").is_file():
        return False
    application.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    return True


add_api_prefix_support(app)
serve_dashboard(app, DASHBOARD_DIST)
