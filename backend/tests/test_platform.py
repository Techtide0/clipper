"""Settings, automatic mode, health, channel and video management, summary and performance."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app import automation, editor, fullvideo, health, pipeline, runtime, tuning
from app import db as dbmod
from app import judge as judgemod
from app import main as mainmod
from app.config import settings
from app.main import app
from tests.test_ingest import FakeYT, _meta, _status


@pytest.fixture()
def plat(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (pipeline, judgemod, mainmod, editor, fullvideo, automation, tuning, runtime):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for d in (settings.clips_dir, settings.exports_dir, settings.downloads_dir, settings.moments_dir, settings.raw_dir):
        d.mkdir(parents=True, exist_ok=True)
    fake = FakeYT()
    fake.meta = _meta()
    monkeypatch.setattr(pipeline, "get_source", lambda platform: fake)
    with TestClient(app) as client:
        yield client, Session, fake


def _streamer(Session, name="IGN", url="https://www.youtube.com/@IGN", tracked=True):
    with Session() as s:
        st = dbmod.Streamer(name=name, channel_url=url, platform="youtube", tracked=tracked)
        s.add(st)
        s.commit()
        return st.id


def _video(Session, sid, pid="abcdefghijk", status="new", duration=900, **kw):
    with Session() as s:
        v = dbmod.Video(streamer_id=sid, platform_id=pid, title=f"video {pid}", url="u", status=status,
                        duration_s=duration, **kw)
        s.add(v)
        s.commit()
        return v.id


def _cand(Session, vid, **kw):
    with Session() as s:
        c = dbmod.Candidate(video_id=vid, start_s=0, end_s=30, score=1.0, signals={"audio": 0.5}, **kw)
        s.add(c)
        s.commit()
        return c.id


# ---- dashboard settings -------------------------------------------------------------------------

def test_settings_start_at_their_defaults_and_describe_themselves(plat):
    client, _, _ = plat
    by_key = {s["key"]: s for s in client.get("/settings").json()}
    assert by_key["auto_mode"]["value"] is False and by_key["auto_interval_minutes"]["value"] == 60
    assert by_key["judge_min_score"]["value"] == settings.judge_min_score
    s = by_key["auto_interval_minutes"]
    assert (s["type"], s["min"], s["max"]) == ("int", 5, 1440) and s["label"] and s["help"]


def test_a_saved_setting_sticks(plat):
    client, _, _ = plat
    out = client.put("/settings", json={"auto_mode": True, "auto_interval_minutes": 30, "judge_min_score": 5.5}).json()
    assert {s["key"]: s["value"] for s in out}["auto_interval_minutes"] == 30
    assert runtime.get("auto_mode") is True and runtime.get("judge_min_score") == 5.5


@pytest.mark.parametrize("bad,message", [
    ({"auto_interval_minutes": 2}, "between 5 and 1440"),
    ({"auto_interval_minutes": 90.5}, "whole number"),
    ({"auto_interval_minutes": "soon"}, "must be a number"),
    ({"auto_mode": "yes"}, "on or off"),
    ({"judge_min_score": 11}, "between 0 and 10"),
    ({"judge_min_score": True}, "must be a number"),
    ({"nonsense": 1}, "unknown setting"),
])
def test_invalid_settings_are_refused_with_a_message(plat, bad, message):
    client, _, _ = plat
    r = client.put("/settings", json=bad)
    assert r.status_code == 422 and message in r.json()["detail"]


def test_one_bad_value_refuses_the_whole_change(plat):
    client, _, _ = plat
    r = client.put("/settings", json={"auto_mode": True, "auto_interval_minutes": 2})
    assert r.status_code == 422 and runtime.get("auto_mode") is False  # the valid half was not saved either


def test_a_bad_stored_value_falls_back_to_the_default(plat):
    _, Session, _ = plat
    with Session() as s:
        s.add(dbmod.AppSetting(key="auto_interval_minutes", value="garbage"))
        s.commit()
    assert runtime.get("auto_interval_minutes") == 60


def test_the_reject_threshold_and_moments_per_video_come_from_the_settings(plat):
    _, Session, _ = plat
    vid = _video(Session, _streamer(Session))
    ids = [_cand(Session, vid) for _ in range(3)]

    class Judge(judgemod.Judge):
        def judge(self, moments):
            return [judgemod.Verdict(m["id"], 7.0, "t", "h", "c", "r") for m in moments]

    runtime.update({"judge_min_score": 8})  # a 7.0 is now below the bar
    judgemod.judge_pending(Judge())
    with Session() as s:
        assert {c.review_status for c in s.scalars(select(dbmod.Candidate))} == {"rejected"}
    assert runtime.get("shortlist_size") == settings.shortlist_size
    runtime.update({"shortlist_size": 7})
    assert runtime.get("shortlist_size") == 7


# ---- automatic mode -----------------------------------------------------------------------------------

@pytest.fixture()
def auto(plat, monkeypatch):
    client, Session, fake = plat
    started = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid, token: started.append((vid, token)))
    return client, Session, fake, started


class _Listing:
    """A source whose channel listing is scripted per channel URL."""

    def __init__(self, by_url):
        self.by_url = by_url

    def list_videos(self, channel_url, limit=10):
        out = self.by_url[channel_url]
        if isinstance(out, Exception):
            raise out
        return out


def _info(pid, title="t", dur=600):
    from app.sources.base import VideoInfo
    return VideoInfo(pid, title, f"https://youtu.be/{pid}", dur)


def test_a_cycle_scans_followed_channels_and_analyzes_the_newest_few(auto, monkeypatch):
    client, Session, _, started = auto
    followed = _streamer(Session, "Followed", "https://www.youtube.com/@a")
    _streamer(Session, "Link only", "https://www.youtube.com/@b", tracked=False)
    monkeypatch.setattr(pipeline, "get_source", lambda p: _Listing({
        "https://www.youtube.com/@a": [_info("aaaaaaaaaaa"), _info("bbbbbbbbbbb"), _info("ccccccccccc")],
        "https://www.youtube.com/@b": AssertionError("an unfollowed channel must never be scanned")}))
    runtime.update({"auto_videos_per_cycle": 2})
    summary = automation.run_cycle()
    assert (summary["channels"], summary["new_videos"], summary["analyzed"], summary["errors"]) == (1, 3, 2, [])
    assert len(started) == 2  # capped
    with Session() as s:
        statuses = {v.platform_id: v.status for v in s.scalars(select(dbmod.Video))}
        assert sorted(statuses.values()) == ["new", "queued", "queued"]  # one left for the next check
        # ties on discovery time fall back to the newest id, so the two found last are the ones started
        assert statuses == {"aaaaaaaaaaa": "new", "bbbbbbbbbbb": "queued", "ccccccccccc": "queued"}
        assert s.get(dbmod.Streamer, followed).last_scan_error is None and s.get(dbmod.Streamer, followed).last_scan_at


def test_very_long_videos_are_skipped_with_a_note_that_says_why(auto, monkeypatch):
    client, Session, _, started = auto
    _streamer(Session, "Followed", "https://www.youtube.com/@a")
    monkeypatch.setattr(pipeline, "get_source", lambda p: _Listing({"https://www.youtube.com/@a": [
        _info("lllllllllll", dur=10 * 3600), _info("sssssssssss", dur=600)]}))
    runtime.update({"auto_max_video_minutes": 120})
    summary = automation.run_cycle()
    assert (summary["skipped_long"], summary["analyzed"]) == (1, 1)
    with Session() as s:
        long_one = s.scalars(select(dbmod.Video).where(dbmod.Video.platform_id == "lllllllllll")).one()
        assert long_one.status == "new" and "longer than 120 minutes" in long_one.error
    automation.run_cycle()  # the note is written once, not piled up on every check
    with Session() as s:
        assert s.scalars(select(dbmod.Video).where(dbmod.Video.platform_id == "lllllllllll")).one().error.count("longer") == 1


def test_one_broken_channel_does_not_stop_the_others_and_its_error_is_saved(auto, monkeypatch):
    client, Session, _, started = auto
    bad = _streamer(Session, "Broken", "https://www.youtube.com/@bad")
    _streamer(Session, "Fine", "https://www.youtube.com/@ok")
    monkeypatch.setattr(pipeline, "get_source", lambda p: _Listing({
        "https://www.youtube.com/@bad": ConnectionError("offline"),
        "https://www.youtube.com/@ok": [_info("ooooooooooo")]}))
    summary = automation.run_cycle()
    assert summary["analyzed"] == 1 and "Broken: ConnectionError: offline" in summary["errors"][0]
    with Session() as s:
        assert "ConnectionError: offline" in s.get(dbmod.Streamer, bad).last_scan_error


def test_only_one_cycle_runs_at_a_time(auto):
    assert automation._RUNNING.acquire(blocking=False)
    try:
        assert automation.is_running() and automation.run_cycle() is None
    finally:
        automation._RUNNING.release()


def test_the_scheduler_runs_a_check_only_when_automatic_mode_is_on_and_one_is_due(auto, monkeypatch):
    client, Session, _, started = auto
    sched = automation.Scheduler()
    ran = []
    monkeypatch.setattr(automation, "run_cycle", lambda: ran.append(1) or {})
    now = pipeline._now()
    assert sched.tick(now) is False and ran == []                        # automatic mode is off
    runtime.update({"auto_mode": True, "auto_interval_minutes": 30})
    assert sched.tick(now) is True and len(ran) == 1                     # never run before: due
    runtime.set_state("auto_last_run", (now - timedelta(minutes=10)).isoformat())
    assert sched.tick(now) is False and len(ran) == 1                    # 10 of 30 minutes: not due
    runtime.set_state("auto_last_run", (now - timedelta(minutes=31)).isoformat())
    assert sched.tick(now) is True and len(ran) == 2                     # due again


def test_the_scheduler_does_not_start_when_disabled(plat):
    sched = automation.Scheduler()
    sched.start()  # the test setup switches it off
    assert sched._thread is None


def test_the_automation_endpoints(auto):
    client, Session, _, _ = auto
    st = client.get("/automation").json()
    assert (st["auto_mode"], st["running"], st["last_run"], st["next_run"]) == (False, False, None, None)
    assert client.post("/automation/run").status_code == 200
    after = client.get("/automation").json()
    assert after["last_run"] and after["last_summary"]["channels"] == 0
    runtime.update({"auto_mode": True})
    assert client.get("/automation").json()["next_run"]
    assert automation._RUNNING.acquire(blocking=False)
    try:
        assert client.post("/automation/run").status_code == 409
    finally:
        automation._RUNNING.release()


# ---- health -----------------------------------------------------------------------------------------

def test_health_reports_what_is_missing_and_how_to_fix_it(plat, monkeypatch):
    client, _, _ = plat
    monkeypatch.setattr(health, "_ffmpeg_version", lambda: None)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    h = client.get("/health").json()
    by_name = {c["name"]: c for c in h["checks"]}
    assert h["ok"] is False
    assert by_name["Video tools (ffmpeg)"]["ok"] is False and "winget" in by_name["Video tools (ffmpeg)"]["fix"]
    assert by_name["AI scoring (Gemini)"]["ok"] is False and "GEMINI_API_KEY" in by_name["AI scoring (Gemini)"]["fix"]


def test_health_is_ok_when_the_requirements_are_met_and_measures_storage(plat, monkeypatch):
    client, _, _ = plat
    monkeypatch.setattr(health, "_ffmpeg_version", lambda: "ffmpeg version test")
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    (settings.clips_dir / "a.mp4").write_bytes(b"x" * 2_000_000)
    h = client.get("/health").json()
    assert h["ok"] is True and h["storage_mb"]["clips"] == 2.0 and h["free_disk_gb"] > 0
    assert next(c for c in h["checks"] if c["name"] == "Hardware video encoding")["required"] is False


def test_health_flags_a_nearly_full_disk(plat, monkeypatch):
    from collections import namedtuple
    client, _, _ = plat
    monkeypatch.setattr(health, "_ffmpeg_version", lambda: "ffmpeg")
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(500e9, 498e9, 2e9))
    h = client.get("/health").json()
    assert h["ok"] is False and not next(c for c in h["checks"] if c["name"] == "Free disk space")["ok"]


# ---- channels -----------------------------------------------------------------------------------------

def test_the_channel_list_says_when_it_was_checked_and_how_much_is_waiting(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    _video(Session, sid, "aaaaaaaaaaa", "new")
    _video(Session, sid, "bbbbbbbbbbb", "analyzed")
    ch = client.get("/streamers").json()[0]
    assert (ch["name"], ch["tracked"], ch["videos"], ch["waiting"], ch["last_scan_at"]) == ("IGN", True, 2, 1, None)


def test_a_channel_can_be_renamed_retyped_and_unfollowed(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    out = client.patch(f"/streamers/{sid}", json={"name": "IGN Gaming", "content_type": "news", "tracked": False}).json()
    assert (out["name"], out["content_type"], out["tracked"]) == ("IGN Gaming", "news", False)
    assert client.patch(f"/streamers/{sid}", json={"content_type": "podcast"}).status_code == 422
    assert client.patch(f"/streamers/{sid}", json={"name": ""}).status_code == 422
    assert client.patch("/streamers/999", json={"tracked": True}).status_code == 404


def test_a_channel_with_videos_cannot_be_deleted_but_an_empty_one_can(plat):
    client, Session, _ = plat
    used, empty = _streamer(Session, "Used", "https://www.youtube.com/@u"), _streamer(Session, "Empty", "https://www.youtube.com/@e")
    _video(Session, used)
    assert client.delete(f"/streamers/{used}").status_code == 409
    assert client.delete(f"/streamers/{empty}").status_code == 200
    assert client.delete("/streamers/999").status_code == 404
    assert [c["name"] for c in client.get("/streamers").json()] == ["Used"]


def test_a_manual_check_reports_failures_and_saves_them_on_the_channel(plat, monkeypatch):
    client, Session, _ = plat
    sid = _streamer(Session)
    monkeypatch.setattr(pipeline, "get_source", lambda p: _Listing({"https://www.youtube.com/@IGN": [_info("ddddddddddd")]}))
    assert client.post(f"/streamers/{sid}/scan").json() == {"new": 1}
    monkeypatch.setattr(pipeline, "get_source", lambda p: _Listing({"https://www.youtube.com/@IGN": ConnectionError("offline")}))
    r = client.post(f"/streamers/{sid}/scan")
    assert r.status_code == 502 and "offline" in r.json()["detail"]
    assert "offline" in client.get("/streamers").json()[0]["last_scan_error"]
    assert client.post("/streamers/999/scan").status_code == 404


# ---- deleting, re-analyzing and re-scoring videos ---------------------------------------------------------------

def _files_for(Session, vid, tag):
    """A moment with every kind of file made for it, so deleting can be checked for completeness."""
    (settings.moments_dir / f"{tag}.mp3").write_bytes(b"a")
    (settings.clips_dir / f"{tag}_9x16.mp4").write_bytes(b"v")
    (settings.clips_dir / f"{tag}_16x9.mp4").write_bytes(b"w")
    cid = _cand(Session, vid, audio_path=f"{tag}.mp3", vertical_path=f"{tag}_9x16.mp4", wide_path=f"{tag}_16x9.mp4",
                render_status="done")
    (settings.exports_dir / str(cid)).mkdir()
    (settings.exports_dir / str(cid) / "manifest.json").write_text("{}")
    return cid


def _everything_on_disk():
    return sorted(str(p.relative_to(settings.data_dir)).replace("\\", "/")
                  for d in (settings.clips_dir, settings.exports_dir, settings.moments_dir, settings.downloads_dir)
                  for p in d.rglob("*") if p.is_file())


def test_deleting_a_video_removes_its_moments_and_every_file_but_leaves_other_videos_alone(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    doomed = _video(Session, sid, "aaaaaaaaaaa", "analyzed", full_status="done", full_path="aaaaaaaaaaa.mp4")
    (settings.downloads_dir / "aaaaaaaaaaa.mp4").write_bytes(b"full")
    keep = _video(Session, sid, "bbbbbbbbbbb", "analyzed")
    _files_for(Session, doomed, "d1")
    kept_cid = _files_for(Session, keep, "k1")
    assert client.delete(f"/videos/{doomed}").status_code == 200
    with Session() as s:
        assert [v.platform_id for v in s.scalars(select(dbmod.Video))] == ["bbbbbbbbbbb"]
        assert [c.id for c in s.scalars(select(dbmod.Candidate))] == [kept_cid]
    assert _everything_on_disk() == sorted([
        "moments/k1.mp3", "clips/k1_9x16.mp4", "clips/k1_16x9.mp4", f"exports/{kept_cid}/manifest.json"])
    assert client.delete(f"/videos/{doomed}").status_code == 404


@pytest.mark.parametrize("busy", [dict(status="processing"), dict(status="queued"), dict(full_status="downloading")])
def test_a_video_with_work_running_on_it_cannot_be_deleted(plat, busy):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), **{"status": "analyzed", **busy})
    assert client.delete(f"/videos/{vid}").status_code == 409
    with Session() as s:
        assert s.get(dbmod.Video, vid) is not None


def test_a_video_with_a_clip_being_cut_cannot_be_deleted(plat):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    _cand(Session, vid, render_status="rendering")
    assert client.delete(f"/videos/{vid}").status_code == 409


def test_reanalyzing_replaces_the_old_moments_and_cleans_up_their_files(plat):
    client, Session, fake = plat
    vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk")
    old = _files_for(Session, vid, "o1")
    with Session() as s:
        s.execute(update(dbmod.Video).where(dbmod.Video.id == vid).values(status="analyzed"))
        s.commit()
    token = pipeline.claim_video(vid, allowed=("analyzed",))
    assert token and pipeline.process_video(vid, token)  # the real worker, on the fake source
    with Session() as s:
        ids = {c.id for c in s.scalars(select(dbmod.Candidate))}
    assert old not in ids and ids
    leftovers = [f for f in _everything_on_disk() if f.startswith("clips/") or f.startswith("exports/")]
    assert leftovers == []  # the old moment's clips and export are gone, nothing is orphaned
    with Session() as s:  # audio is named per attempt now: what is left must belong to a current moment
        current = {c.audio_path for c in s.scalars(select(dbmod.Candidate))}
    assert all(f.split("/", 1)[1] in current for f in _everything_on_disk())


def test_the_reanalyze_endpoint_claims_atomically(plat, monkeypatch):
    client, Session, _ = plat
    started = []
    monkeypatch.setattr(pipeline, "analyze_and_judge", lambda vid, token: started.append(vid))
    vid = _video(Session, _streamer(Session), status="analyzed")
    assert client.post(f"/videos/{vid}/reanalyze").status_code == 200
    assert client.post(f"/videos/{vid}/reanalyze").status_code == 409  # already queued
    assert started == [vid] and client.post("/videos/999/reanalyze").status_code == 404


def test_rescoring_needs_unscored_moments_and_a_key(plat, monkeypatch):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    _cand(Session, vid, ai_score=7.0)
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    assert client.post(f"/videos/{vid}/rescore").status_code == 409                    # everything is scored
    _cand(Session, vid)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    r = client.post(f"/videos/{vid}/rescore")
    assert r.status_code == 409 and "GEMINI_API_KEY" in r.json()["detail"]              # no key
    called = []
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    monkeypatch.setattr(pipeline, "rescore", lambda v: called.append(v))
    assert client.post(f"/videos/{vid}/rescore").status_code == 200 and called == [vid]
    assert client.post("/videos/999/rescore").status_code == 404
    assert client.get(f"/videos/{vid}").json()["unscored"] == 1


def test_rescoring_clears_the_warning_when_everything_gets_scored_and_runs_only_once_at_a_time(plat, monkeypatch):
    _, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed", error="AI scoring incomplete (ServerError); run judge again later")
    _cand(Session, vid)
    inside = []

    def judge(video_id=None):
        inside.append(pipeline.rescore(video_id))  # a second request arriving mid-run is refused
        with Session() as s:
            s.execute(update(dbmod.Candidate).values(ai_score=7.0))
            s.commit()

    monkeypatch.setattr(judgemod, "judge_pending", judge)
    assert pipeline.rescore(vid) is True
    assert inside == [False]
    with Session() as s:
        assert s.get(dbmod.Video, vid).error is None


# ---- summary and performance ---------------------------------------------------------------------------

def test_the_summary_counts_everything_a_today_screen_needs(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    done = _video(Session, sid, "aaaaaaaaaaa", "analyzed")
    _video(Session, sid, "bbbbbbbbbbb", "processing")
    _video(Session, sid, "ccccccccccc", "failed")
    _video(Session, sid, "ddddddddddd", "new", full_status="downloading")
    _cand(Session, done, ai_score=7)                                                       # to review
    _cand(Session, done, ai_score=7, review_status="approved", render_status="done")       # ready to post
    _cand(Session, done, ai_score=7, review_status="approved", render_status="rendering")  # cutting
    _cand(Session, done, review_status="approved")                                         # waiting to cut + unscored
    _cand(Session, done, ai_score=3, review_status="rejected")
    s = client.get("/summary").json()
    assert (s["to_review"], s["approved"], s["ready_to_post"], s["cutting"], s["waiting_to_cut"], s["unscored"]) == (1, 3, 1, 1, 1, 1)
    assert (s["analyzing"], s["videos_failed"], s["videos_waiting"], s["downloading_full"]) == (1, 1, 1, 1)
    assert s["automation"]["auto_mode"] is False


def test_performance_explains_when_there_is_not_enough_data(plat):
    client, Session, _ = plat
    p = client.get("/performance").json()
    assert (p["enough_data"], p["clips_with_views"], p["needs"]) == (False, 0, 15)
    assert client.post("/performance/apply").status_code == 409 and not (settings.weights_path.exists())


def test_performance_suggests_applies_and_resets_weights(plat):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    import random
    rng = random.Random(1)
    for i in range(30):
        chat, audio = rng.random(), rng.random()
        with Session() as s:
            s.add(dbmod.Candidate(video_id=vid, start_s=i, end_s=i + 30, score=1,
                                  signals={"audio": audio, "chat": chat, "keywords": rng.random()},
                                  views=int(100 + 5000 * chat - 3000 * audio), ai_score=5 + 4 * chat))
            s.commit()
    p = client.get("/performance").json()
    assert p["enough_data"] and p["correlations"]["chat"]["correlation"] > 0.5
    assert p["suggested_weights"]["chat"] > p["current_weights"]["chat"]
    assert len(p["posted"]) == 30 and p["posted"][0]["views"] >= p["posted"][-1]["views"]  # best first
    assert client.post("/performance/apply").json()["applied"] == p["suggested_weights"]
    assert json.loads(settings.weights_path.read_text())["chat"] == p["suggested_weights"]["chat"]
    assert client.post("/performance/reset").json() == {"reset": True} and not settings.weights_path.exists()


# ---- one-command startup ---------------------------------------------------------------------------

def test_the_built_dashboard_is_served_and_never_hides_an_api_route(tmp_path):
    from fastapi import FastAPI
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><title>Clipper dashboard</title></html>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    application = FastAPI()

    @application.get("/summary")
    def api_route():
        return {"from": "the API"}

    mainmod.add_api_prefix_support(application)
    assert mainmod.serve_dashboard(application, dist) is True   # registered after the routes, as in the real app
    c = TestClient(application)
    assert "Clipper dashboard" in c.get("/").text
    assert c.get("/assets/app.js").text == "console.log('hi')"
    assert c.get("/summary").json() == {"from": "the API"}      # the API still wins over the dashboard
    assert c.get("/api/summary").json() == {"from": "the API"}  # and /api/... reaches it, as the dashboard calls it


def test_nothing_is_served_when_the_dashboard_has_not_been_built(tmp_path):
    from fastapi import FastAPI
    application = FastAPI()
    assert mainmod.serve_dashboard(application, tmp_path / "missing") is False
    assert TestClient(application).get("/").status_code == 404


def test_the_real_app_answers_on_both_the_plain_and_the_api_prefixed_paths(plat):
    client, _, _ = plat
    assert client.get("/summary").json() == client.get("/api/summary").json()
    assert client.get("/api/videos?limit=1").status_code == 200
    assert client.post("/api/automation/run").status_code == 200  # writes work through the prefix too


def test_the_dashboard_mount_is_registered_after_every_api_route():
    """Regression: the dashboard is mounted at '/', so a route added after it would be silently swallowed."""
    from starlette.routing import Mount
    routes = mainmod.app.routes
    mounts = [i for i, r in enumerate(routes) if isinstance(r, Mount) and r.path == ""]
    api_routes = [i for i, r in enumerate(routes) if getattr(r, "methods", None) and r.path not in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")]
    if mounts:  # only present when the dashboard has been built
        assert max(api_routes) < min(mounts)


# ---- what the redesigned screens need ------------------------------------------------------------------------

def test_a_clip_can_be_marked_as_posted_and_unmarked(plat):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    cid = _cand(Session, vid, review_status="approved", render_status="done")
    assert client.get("/candidates?status=approved").json()[0]["posted_at"] is None
    out = client.patch(f"/candidates/{cid}", json={"posted": True}).json()
    assert out["posted_at"] and out["review_status"] == "approved"
    assert client.get("/candidates?status=approved").json()[0]["posted_at"] == out["posted_at"]
    assert client.patch(f"/candidates/{cid}", json={"posted": False}).json()["posted_at"] is None


def test_marking_posted_does_not_start_a_cut_or_discard_one(plat, monkeypatch):
    client, Session, _ = plat
    cut = []
    monkeypatch.setattr(editor, "render_candidate", lambda c: cut.append(c))
    vid = _video(Session, _streamer(Session), status="analyzed")
    cid = _cand(Session, vid, review_status="approved", render_status="done")
    assert client.patch(f"/candidates/{cid}", json={"posted": True}).json()["render_status"] == "done" and cut == []


def test_the_video_list_gives_the_facts_the_videos_screen_shows(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    vid = _video(Session, sid, status="analyzed")
    (settings.clips_dir / "a.mp4").write_bytes(b"x" * 1_500_000)
    (settings.clips_dir / "b.mp4").write_bytes(b"x" * 500_000)
    _cand(Session, vid, vertical_path="a.mp4", wide_path="b.mp4", render_status="done")
    with Session() as s:
        s.get(dbmod.Streamer, sid).content_type = "news"
        s.commit()
    v = client.get("/videos").json()[0]
    assert (v["content_type"], v["clips_mb"]) == ("news", 2.0) and v["added"]
    with Session() as s:
        s.get(dbmod.Video, vid).content_type = "commentary"  # a type chosen for this one video wins
        s.commit()
    assert client.get(f"/videos/{vid}").json()["content_type"] == "commentary"


def test_cleanup_removes_old_exports_and_leftover_work_files_but_never_clips_or_downloads(plat):
    import os
    import time
    client, _, _ = plat
    old_export, new_export = settings.exports_dir / "1", settings.exports_dir / "2"
    old_raw, new_raw = settings.raw_dir / "old", settings.raw_dir / "new"
    for d in (old_export, new_export, old_raw, new_raw):
        d.mkdir()
        (d / "f.bin").write_bytes(b"x" * 1_000_000)
    (settings.clips_dir / "keep.mp4").write_bytes(b"x" * 1000)
    (settings.downloads_dir / "keep_full.mp4").write_bytes(b"x" * 1000)
    long_ago = time.time() - 40 * 86400
    for d in (old_export, old_raw):
        os.utime(d, (long_ago, long_ago))
    out = client.post("/cleanup").json()
    assert out == {"freed_mb": 2.0, "removed": 2}
    assert not old_export.exists() and not old_raw.exists() and new_export.exists() and new_raw.exists()
    assert (settings.clips_dir / "keep.mp4").exists() and (settings.downloads_dir / "keep_full.mp4").exists()
    assert client.post("/cleanup").json() == {"freed_mb": 0.0, "removed": 0}


def test_recover_frees_only_analyses_that_have_stalled(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    stalled = _video(Session, sid, "aaaaaaaaaaa", "processing")
    live = _video(Session, sid, "bbbbbbbbbbb", "processing", claimed_at=pipeline._now())
    with Session() as s:
        s.get(dbmod.Video, stalled).claimed_at = pipeline._now() - timedelta(hours=1)
        s.commit()
    assert client.post("/recover").json() == {"recovered": 1}
    with Session() as s:
        assert (s.get(dbmod.Video, stalled).status, s.get(dbmod.Video, live).status) == ("failed", "processing")
    assert client.post("/recover").json() == {"recovered": 0}


def test_signal_weights_can_be_read_and_set_within_their_limits(plat):
    client, _, _ = plat
    w = client.get("/weights").json()
    assert w["current"] == w["defaults"] and (w["min"], w["max"]) == (0.1, 3.0)
    out = client.put("/weights", json={"chat": 2.5}).json()
    assert out["current"]["chat"] == 2.5 and out["current"]["audio"] == w["defaults"]["audio"]  # only what was sent changes
    for bad, fragment in (({"chat": 5}, "between 0.1 and 3"), ({"chat": 0}, "between"), ({"chat": "high"}, "number"),
                          ({"chat": True}, "number"), ({"magic": 1}, "unknown signal")):
        r = client.put("/weights", json=bad)
        assert r.status_code == 422 and fragment in r.json()["detail"]
    assert client.get("/weights").json()["current"]["chat"] == 2.5  # a refused change changed nothing
    client.post("/performance/reset")
    assert client.get("/weights").json()["current"] == w["defaults"]


def test_working_files_are_kept_or_deleted_according_to_the_setting(plat):
    _, Session, _ = plat
    for keep in (False, True):
        runtime.update({"delete_working_files": not keep})
        vid, _ = pipeline.ingest_url("https://youtu.be/abcdefghijk") if not keep else (_video(Session, _streamer(Session, "k", "k"), "kkkkkkkkkkk"), 0)
        token = pipeline.claim_video(vid, allowed=("new", "failed", "analyzed"))
        assert pipeline.process_video(vid, token)  # the analysis completed, so what is left is the setting's doing
        assert _status(Session, vid) == "analyzed"
        left = [p for p in settings.raw_dir.iterdir()]
        assert bool(left) is keep, f"delete_working_files={not keep}: working files left = {left}"


def test_the_full_download_safety_margin_comes_from_the_settings(plat, monkeypatch):
    from collections import namedtuple
    _, Session, _ = plat
    vid = _video(Session, _streamer(Session), duration=600)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(fullvideo.shutil, "disk_usage", lambda p: Usage(500e9, 480e9, 20e9))
    runtime.update({"full_download_headroom_gb": 30})  # 20 GB free is not enough with a 30 GB margin
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    with Session() as s:
        row = s.get(dbmod.Video, vid)
        assert row.full_status == "failed" and "30 GB safety margin" in row.full_error


def test_the_jobs_list_shows_everything_running_in_the_background(plat):
    client, Session, _ = plat
    sid = _streamer(Session)
    assert client.get("/jobs").json() == []
    _video(Session, sid, "aaaaaaaaaaa", "processing")
    _video(Session, sid, "bbbbbbbbbbb", "queued")
    _video(Session, sid, "ccccccccccc", "analyzed", full_status="downloading", full_progress=42)
    done = _video(Session, sid, "ddddddddddd", "analyzed")
    _cand(Session, done, render_status="rendering", title="A cut in progress")
    _cand(Session, done, yt_status="uploading", yt_progress=30, title="An upload")
    jobs = client.get("/jobs").json()
    assert sorted(j["kind"] for j in jobs) == ["analyzing", "cutting", "full video", "queued", "uploading"]
    full = next(j for j in jobs if j["kind"] == "full video")
    assert (full["pct"], full["detail"]) == (42, "Downloading 42%")
    assert next(j for j in jobs if j["kind"] == "cutting")["title"] == "A cut in progress"


def test_posted_clips_stop_counting_as_ready_to_post_and_failed_cuts_are_counted(plat):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    _cand(Session, vid, review_status="approved", render_status="done")
    posted = _cand(Session, vid, review_status="approved", render_status="done")
    _cand(Session, vid, review_status="approved", render_status="failed")
    assert client.get("/summary").json()["ready_to_post"] == 2
    client.patch(f"/candidates/{posted}", json={"posted": True})
    s = client.get("/summary").json()
    assert (s["ready_to_post"], s["cuts_failed"]) == (1, 1)


def test_non_finite_numbers_are_refused_as_settings(plat):
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            runtime.update({"judge_min_score": bad})
    assert runtime.update({"judge_min_score": 10})["judge_min_score"] == 10  # the boundary itself is fine


def test_a_heartbeat_slower_than_half_the_stale_limit_is_refused():
    from pydantic import ValidationError
    from app.config import Settings
    with pytest.raises(ValidationError):
        Settings(heartbeat_s=100, claim_stale_s=180)
    Settings(heartbeat_s=30, claim_stale_s=180)


def test_a_video_with_a_clip_uploading_cannot_be_deleted(plat):
    client, Session, _ = plat
    vid = _video(Session, _streamer(Session), status="analyzed")
    with Session() as s:
        s.add(dbmod.Candidate(video_id=vid, start_s=0, end_s=30, score=1, yt_status="uploading"))
        s.commit()
    r = client.delete(f"/videos/{vid}")
    assert r.status_code == 409 and "uploading" in r.json()["detail"]
