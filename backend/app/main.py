from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from . import editor, pipeline
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Clipper", lifespan=lifespan)
app.mount("/clips", StaticFiles(directory=settings.clips_dir), name="clips")


@app.get("/streamers")
def streamers():
    with SessionLocal() as db:
        return [dict(id=s.id, name=s.name, platform=s.platform, channel_url=s.channel_url)
                for s in db.scalars(select(Streamer))]


@app.post("/streamers")
def add_streamer(name: str, channel_url: str):
    with SessionLocal() as db:
        s = Streamer(name=name, channel_url=channel_url)
        db.add(s)
        db.commit()
        return dict(id=s.id)


@app.post("/streamers/{sid}/scan")
def scan(sid: int):
    return {"new": pipeline.scan_streamer(sid)}


@app.get("/videos")
def videos():
    with SessionLocal() as db:
        return [dict(id=v.id, title=v.title, url=v.url, status=v.status, error=v.error)
                for v in db.scalars(select(Video).order_by(Video.discovered_at.desc()))]


@app.post("/videos/{vid}/process")
def process(vid: int, bg: BackgroundTasks):
    bg.add_task(pipeline.process_video, vid)
    return {"queued": vid}


def _cand(c: Candidate) -> dict:
    return dict(
        id=c.id, video_id=c.video_id, video_title=c.video.title, video_url=c.video.url,
        youtube_id=c.video.platform_id, start_s=c.start_s, end_s=c.end_s, score=c.score,
        signals=c.signals, ai_score=c.ai_score, title=c.title, hook=c.hook, caption=c.caption,
        ai_reason=c.ai_reason, transcript=c.transcript, review_status=c.review_status,
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


@app.patch("/candidates/{cid}")
def edit_candidate(cid: int, edit: CandidateEdit):
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        if not c:
            raise HTTPException(404)
        data = edit.model_dump(exclude_none=True)
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
