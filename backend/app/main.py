from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from sqlalchemy import select

from . import pipeline
from .db import Candidate, SessionLocal, Streamer, Video, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Clipper", lifespan=lifespan)


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


@app.get("/candidates")
def candidates(video_id: int | None = None, limit: int = 50):
    with SessionLocal() as db:
        q = select(Candidate).order_by(Candidate.score.desc()).limit(limit)
        if video_id:
            q = q.where(Candidate.video_id == video_id)
        return [dict(id=c.id, video_id=c.video_id, start_s=c.start_s, end_s=c.end_s,
                     score=c.score, signals=c.signals, review_status=c.review_status)
                for c in db.scalars(q)]
