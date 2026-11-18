import json
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import editor, export, pipeline
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video, init_db
from .sources import detect_platform


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    pipeline.recover_interrupted()  # a restart killed any analysis that was in flight
    yield


app = FastAPI(title="Clipper", lifespan=lifespan)
app.mount("/clips", StaticFiles(directory=settings.clips_dir), name="clips")
app.mount("/exports", StaticFiles(directory=settings.exports_dir), name="exports")

CONTENT_TYPES = ("gameplay", "news", "commentary")
MAX_COUNT = 2**53 - 1  # Number.MAX_SAFE_INTEGER


@app.get("/streamers")
def streamers():
    with SessionLocal() as db:
        return [dict(id=s.id, name=s.name, platform=s.platform, channel_url=s.channel_url,
                     content_type=s.content_type)
                for s in db.scalars(select(Streamer))]


@app.post("/streamers")
def add_streamer(name: str, channel_url: str, content_type: str = "gameplay"):
    if content_type not in CONTENT_TYPES:
        raise HTTPException(422, f"content_type must be one of {CONTENT_TYPES}")
    try:
        platform = detect_platform(channel_url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with SessionLocal() as db:
        s = db.scalars(select(Streamer).where(Streamer.channel_url == channel_url)).first()
        if s:  # e.g. created earlier by pasting one of its videos: just start following it
            s.tracked, s.content_type = True, content_type
        else:
            s = Streamer(name=name, channel_url=channel_url, platform=platform, content_type=content_type)
            db.add(s)
        db.commit()
        return dict(id=s.id)


@app.post("/streamers/{sid}/scan")
def scan(sid: int):
    return {"new": pipeline.scan_streamer(sid)}


def _video(v: Video, moments: int) -> dict:
    return dict(id=v.id, title=v.title, url=v.url, status=v.status, error=v.error,
                channel=v.streamer.name, platform=v.streamer.platform, moments=moments)


def _moment_counts(db, ids: list[int]) -> dict[int, int]:
    """Candidates per video in one grouped query, without loading the candidate rows."""
    if not ids:
        return {}
    rows = db.execute(select(Candidate.video_id, func.count(Candidate.id))
                      .where(Candidate.video_id.in_(ids)).group_by(Candidate.video_id)).all()
    return dict(rows)


@app.get("/videos")
def videos(limit: int = Query(20, ge=1, le=100)):
    with SessionLocal() as db:
        vs = list(db.scalars(select(Video).order_by(Video.discovered_at.desc(), Video.id.desc()).limit(limit)))
        counts = _moment_counts(db, [v.id for v in vs])
        return [_video(v, counts.get(v.id, 0)) for v in vs]


@app.get("/videos/{vid}")
def get_video(vid: int):
    """One video's status, for following it to completion (it may have scrolled off /videos)."""
    with SessionLocal() as db:
        v = db.get(Video, vid)
        if not v:
            raise HTTPException(404)
        return _video(v, _moment_counts(db, [v.id]).get(v.id, 0))


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
    claimed = pipeline.claim_video(vid)
    if claimed:
        bg.add_task(pipeline.analyze_and_judge, vid)
    with SessionLocal() as db:
        v = db.get(Video, vid)
        return dict(video_id=vid, title=v.title, created=created, queued=claimed, status=v.status)


@app.post("/videos/{vid}/process")
def process(vid: int, bg: BackgroundTasks):
    if not pipeline.claim_video(vid):
        raise HTTPException(409, "that video is already queued, running or finished")
    bg.add_task(pipeline.process_video, vid)
    return {"queued": vid}


def _cand(c: Candidate) -> dict:
    return dict(
        id=c.id, video_id=c.video_id, video_title=c.video.title, video_url=c.video.url,
        content_type=c.video.content_type or c.video.streamer.content_type, platform=c.video.streamer.platform,
        youtube_id=c.video.platform_id, start_s=c.start_s, end_s=c.end_s, score=c.score,
        signals=c.signals, ai_score=c.ai_score, title=c.title, hook=c.hook, caption=c.caption,
        ai_reason=c.ai_reason, transcript=c.transcript, review_status=c.review_status,
        views=c.views, likes=c.likes,
        render_status=c.render_status, render_error=c.render_error,
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
    views: int | None = Field(default=None, ge=0, le=MAX_COUNT)
    likes: int | None = Field(default=None, ge=0, le=MAX_COUNT)


@app.patch("/candidates/{cid}")
def edit_candidate(cid: int, edit: CandidateEdit):
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
        if ("start_s" in data or "end_s" in data) and c.render_status == "done":
            c.render_status = "none"  # trim changed: the old render is stale
        for k, v in data.items():
            setattr(c, k, v)
        db.commit()
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


@app.post("/candidates/{cid}/render")
def render(cid: int, bg: BackgroundTasks):
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        if not c:
            raise HTTPException(404)
        if c.render_status == "rendering":
            raise HTTPException(409, "already rendering")
        c.render_status = "rendering"
        db.commit()
    bg.add_task(editor.render_candidate, cid)
    return {"queued": cid}
