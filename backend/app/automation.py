"""Automatic mode: check followed channels on a schedule, analyze new videos and score them.

The goal is that moments are already waiting when the owner opens the app, so their time goes on
the one job only they can do: deciding. It never approves anything and never posts anything.

Safeguards: at most a few videos per check, very long videos are skipped, only one check runs at a
time, and every analysis goes through the same claim system as everything else (so it can never
duplicate work you started by hand).
"""
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select, update

from . import pipeline, runtime
from .config import settings
from .db import SessionLocal, Streamer, Video

log = logging.getLogger("clipper.automation")
TICK_S = 30
_RUNNING = threading.Lock()


def is_running() -> bool:
    return _RUNNING.locked()


def run_cycle() -> dict | None:
    """One pass: look for new videos on followed channels, then analyze a few of them.

    Returns what happened, or None if another pass is already running.
    """
    if not _RUNNING.acquire(blocking=False):
        return None
    try:
        started_at = pipeline._now()
        runtime.set_state("auto_last_run", started_at.isoformat())  # first, so a crash can't make it retry in a hot loop
        summary = {"channels": 0, "new_videos": 0, "analyzed": 0, "skipped_long": 0, "errors": []}
        with SessionLocal() as db:
            channels = list(db.scalars(select(Streamer).where(Streamer.tracked)))
        for s in channels:
            summary["channels"] += 1
            try:
                summary["new_videos"] += pipeline.scan_streamer(s.id, limit=5)
            except Exception as e:  # one broken channel must not stop the others (its error is saved on it)
                summary["errors"].append(f"{s.name}: {type(e).__name__}: {e}"[:200])
        max_s = runtime.get("auto_max_video_minutes") * 60
        cap = runtime.get("auto_videos_per_cycle")
        with SessionLocal() as db:
            waiting = list(db.scalars(
                select(Video).join(Streamer).where(Streamer.tracked, Video.status == "new")
                .order_by(Video.discovered_at.desc(), Video.id.desc())))
        for v in waiting:
            if summary["analyzed"] >= cap:
                break
            if v.duration_s and v.duration_s > max_s:
                summary["skipped_long"] += 1
                if not v.error:  # say why it is sitting there, once
                    with SessionLocal() as db:
                        db.execute(update(Video).where(Video.id == v.id, Video.error.is_(None)).values(
                            error=f"automatic mode skips videos longer than {max_s // 60} minutes; analyze it yourself if you want it"))
                        db.commit()
                continue
            token = pipeline.claim_video(v.id, ("new",))
            if token:
                pipeline.analyze_and_judge(v.id, token)
                summary["analyzed"] += 1
        try:  # if YouTube is connected, bring back the views of clips already posted (best effort)
            from . import youtube
            if youtube.status()["connected"]:
                summary["views_updated"] = youtube.refresh_stats()
        except Exception as e:
            summary["errors"].append(f"YouTube views: {type(e).__name__}: {e}"[:200])
        summary["finished_at"] = pipeline._now().isoformat()
        runtime.set_state("auto_last_summary", summary)
        return summary
    finally:
        _RUNNING.release()


def status() -> dict:
    last = runtime.get_state("auto_last_run")
    interval = runtime.get("auto_interval_minutes")
    next_run = None
    if runtime.get("auto_mode"):
        next_run = (datetime.fromisoformat(last) + timedelta(minutes=interval)).isoformat() if last else "soon"
    return {"auto_mode": runtime.get("auto_mode"), "interval_minutes": interval, "running": is_running(),
            "last_run": last, "last_summary": runtime.get_state("auto_last_summary"), "next_run": next_run}


class Scheduler:
    """A background thread that runs a check whenever automatic mode is on and one is due."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not settings.scheduler_enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="clipper-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # never let the scheduler die: log and try again at the next tick
                log.exception("automatic check failed")

    def tick(self, now: datetime | None = None) -> bool:
        """Run a check if automatic mode is on and the interval has passed. True if one ran."""
        if not runtime.get("auto_mode"):
            return False
        now = now or pipeline._now()
        last = runtime.get_state("auto_last_run")
        due = last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=runtime.get("auto_interval_minutes"))
        return due and run_cycle() is not None
