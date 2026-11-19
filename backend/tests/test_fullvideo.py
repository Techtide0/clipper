import threading
import time
from collections import namedtuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import editor, fullvideo, pipeline
from app import main as mainmod
from app.config import settings
from app.main import app


@pytest.fixture()
def api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (mainmod, editor, pipeline, fullvideo):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        yield client, Session


def _video(Session, *, full="none", pid="abcdefghijk", title="GTA 6 PS5 Pro Gameplay", duration=900,
           path=None, tag="1"):
    with Session() as s:
        st = dbmod.Streamer(name="IGN", channel_url=f"u{tag}", platform="youtube")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id=pid, title=title, url="https://youtu.be/x",
                        duration_s=duration, full_status=full, full_path=path)
        s.add(v)
        s.commit()
        return v.id


def _row(Session, vid):
    with Session() as s:
        return s.get(dbmod.Video, vid)


class FakeYDL:
    """A stand-in for yt-dlp that writes the file it was asked for (or fails / reports progress)."""
    opts_seen = []
    behaviour = "ok"
    gate = None  # a callable run during the download, to stage events

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.opts_seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        stem = self.opts["outtmpl"].replace(".%(ext)s", "")
        if FakeYDL.gate:
            FakeYDL.gate()
        for hook in self.opts["progress_hooks"]:
            for done in (10, 30, 31, 60, 20, 95, 100):  # includes a restart (the audio track) and tiny steps
                hook({"status": "downloading", "downloaded_bytes": done, "total_bytes": 100})
        if FakeYDL.behaviour == "fail":
            with open(stem + ".mp4.part", "wb") as f:
                f.write(b"half")
            raise RuntimeError("network died")
        if FakeYDL.behaviour == "empty":
            open(stem + ".mp4", "wb").close()
            return
        with open(stem + ".mp4", "wb") as f:
            f.write(b"FULL-VIDEO-BYTES")


@pytest.fixture()
def fake_yt(monkeypatch):
    monkeypatch.setattr(fullvideo.yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.opts_seen, FakeYDL.behaviour, FakeYDL.gate = [], "ok", None
    return FakeYDL


# ---- claiming ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status,claimable", [("none", True), ("failed", True), ("queued", False),
                                              ("downloading", False), ("done", False)])
def test_only_a_video_without_a_copy_can_be_claimed(api, status, claimable):
    _, Session = api
    vid = _video(Session, full=status)
    assert fullvideo.claim_full(vid) is claimable
    assert fullvideo.claim_full(vid) is False


def test_simultaneous_requests_have_exactly_one_winner(api):
    _, Session = api
    vid = _video(Session)
    wins, barrier = [], threading.Barrier(8)

    def click():
        barrier.wait()
        wins.append(fullvideo.claim_full(vid))

    threads = [threading.Thread(target=click) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


# ---- downloading ----------------------------------------------------------------------------------

def test_a_download_saves_the_file_and_records_it(api, fake_yt):
    _, Session = api
    vid = _video(Session)
    assert fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    row = _row(Session, vid)
    assert (row.full_status, row.full_progress, row.full_error) == ("done", 100, None)
    assert row.full_path == "abcdefghijk.mp4"
    assert (settings.downloads_dir / "abcdefghijk.mp4").read_bytes() == b"FULL-VIDEO-BYTES"
    opts = fake_yt.opts_seen[0]
    assert opts["allowed_extractors"] == ["youtube.*"] and opts["merge_output_format"] == "mp4"
    assert "1080" in opts["format"]


def test_progress_only_moves_forward_and_never_reaches_100_until_the_file_exists(api, fake_yt):
    _, Session = api
    vid = _video(Session)
    seen = []
    real_set = fullvideo._set

    def spy(video_id, **values):
        if values.get("full_progress") is not None:  # every progress write, including the final 'done' one
            seen.append(values["full_progress"])
        return real_set(video_id, **values)

    fullvideo._set = spy
    try:
        fullvideo.claim_full(vid)
        fullvideo.download_full(vid)
    finally:
        fullvideo._set = real_set
    in_flight = seen[:-1]  # the last write is the final 'done' with 100
    assert in_flight == sorted(in_flight) and max(in_flight) <= 99
    assert 20 not in in_flight  # the audio track restarting at 20% didn't drag the bar backwards
    assert seen[-1] == 100


def test_a_failed_download_removes_its_partial_files_and_says_why(api, fake_yt):
    _, Session = api
    vid = _video(Session)
    fake_yt.behaviour = "fail"
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    row = _row(Session, vid)
    assert row.full_status == "failed" and "network died" in row.full_error and row.full_path is None
    assert list(settings.downloads_dir.iterdir()) == []  # no half-written multi-GB file left behind
    assert fullvideo.claim_full(vid)  # and Retry works


def test_a_download_that_produces_nothing_is_a_failure_not_a_success(api, fake_yt):
    _, Session = api
    vid = _video(Session)
    fake_yt.behaviour = "empty"
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    assert _row(Session, vid).full_status == "failed"


def test_cleaning_up_one_video_never_touches_another_with_a_similar_id(api, fake_yt):
    _, Session = api
    other = settings.downloads_dir / "twitch-v1234.mp4"
    other.write_bytes(b"someone elses finished download")
    vid = _video(Session, pid="twitch-v123")
    fake_yt.behaviour = "fail"
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    assert other.read_bytes() == b"someone elses finished download"


def test_a_download_is_refused_when_it_would_not_fit_on_the_disk(api, fake_yt, monkeypatch):
    _, Session = api
    vid = _video(Session, duration=3 * 3600)  # three hours: about 10.8 GB by the estimate
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(fullvideo.shutil, "disk_usage", lambda p: Usage(500e9, 495e9, 5e9))
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    row = _row(Session, vid)
    assert row.full_status == "failed" and "not enough disk space" in row.full_error
    assert fake_yt.opts_seen == []  # it never started downloading


def test_a_short_video_fits_even_on_a_nearly_full_disk(api, fake_yt, monkeypatch):
    _, Session = api
    vid = _video(Session, duration=600)  # 10 minutes ~ 0.6 GB + the 2 GB margin
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(fullvideo.shutil, "disk_usage", lambda p: Usage(500e9, 496e9, 4e9))
    fullvideo.claim_full(vid)
    fullvideo.download_full(vid)
    assert _row(Session, vid).full_status == "done"


def test_full_downloads_run_one_at_a_time(api, fake_yt):
    _, Session = api
    ids = [_video(Session, pid=f"vid{i}", tag=str(i)) for i in range(3)]
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def gate():
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.1)
        with lock:
            state["now"] -= 1

    fake_yt.gate = gate
    for i in ids:
        fullvideo.claim_full(i)
    threads = [threading.Thread(target=fullvideo.download_full, args=(i,)) for i in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 1 and [_row(Session, i).full_status for i in ids] == ["done"] * 3


# ---- deleting and recovering ----------------------------------------------------------------------

def test_deleting_the_copy_frees_the_file_and_resets_the_video(api):
    _, Session = api
    f = settings.downloads_dir / "abcdefghijk.mp4"
    f.write_bytes(b"x" * 10)
    vid = _video(Session, full="done", path="abcdefghijk.mp4")
    assert fullvideo.delete_full(vid) is True
    assert not f.exists() and _row(Session, vid).full_status == "none"
    assert fullvideo.delete_full(vid) is False  # nothing left to delete


def test_a_download_left_running_by_a_dead_server_can_be_retried(api):
    _, Session = api
    stuck = _video(Session, full="downloading", tag="a", pid="aaaaaaaaaaa")
    queued = _video(Session, full="queued", tag="b", pid="bbbbbbbbbbb")
    done = _video(Session, full="done", tag="c", pid="ccccccccccc", path="ccccccccccc.mp4")
    (settings.downloads_dir / "ccccccccccc.mp4").write_bytes(b"keep me")
    (settings.downloads_dir / "aaaaaaaaaaa.mp4.part").write_bytes(b"junk")
    assert fullvideo.recover_full() == 2
    assert [_row(Session, v).full_status for v in (stuck, queued, done)] == ["failed", "failed", "done"]
    assert "interrupted" in _row(Session, stuck).full_error
    assert sorted(p.name for p in settings.downloads_dir.iterdir()) == ["ccccccccccc.mp4"]  # finished file kept


# ---- the API ------------------------------------------------------------------------------------

@pytest.fixture()
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(fullvideo, "download_full", lambda vid: calls.append(vid))
    return calls


def test_the_start_endpoint_queues_one_download(api, started):
    client, Session = api
    vid = _video(Session)
    assert client.post(f"/videos/{vid}/full-download").status_code == 200
    assert client.post(f"/videos/{vid}/full-download").status_code == 409  # already queued
    assert client.post("/videos/999/full-download").status_code == 404
    assert started == [vid]
    assert _row(Session, vid).full_status == "queued"


def test_the_video_list_reports_download_progress_and_size(api):
    client, Session = api
    (settings.downloads_dir / "abcdefghijk.mp4").write_bytes(b"x" * 2_500_000)
    done = _video(Session, full="done", path="abcdefghijk.mp4")
    busy = _video(Session, full="downloading", tag="2", pid="bbbbbbbbbbb")
    with Session() as s:
        s.get(dbmod.Video, busy).full_progress = 42
        s.commit()
    by_id = {v["id"]: v for v in client.get("/videos").json()}
    assert (by_id[done]["full_status"], by_id[done]["full_size_mb"]) == ("done", 2.5)
    assert (by_id[busy]["full_status"], by_id[busy]["full_progress"], by_id[busy]["full_size_mb"]) == ("downloading", 42, None)
    assert client.get(f"/videos/{done}").json()["duration_s"] == 900


def test_the_saved_video_downloads_as_an_attachment_named_after_its_title(api):
    client, Session = api
    (settings.downloads_dir / "abcdefghijk.mp4").write_bytes(b"THE-WHOLE-VIDEO")
    vid = _video(Session, full="done", path="abcdefghijk.mp4", title='GTA 6: "PS5 Pro" / Gameplay')
    r = client.get(f"/videos/{vid}/full")
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, b"THE-WHOLE-VIDEO", "video/mp4")
    assert r.headers["content-disposition"].startswith("attachment")
    assert "GTA-6-PS5-Pro-Gameplay.mp4" in r.headers["content-disposition"]


def test_asking_for_a_video_that_is_not_downloaded_says_why(api):
    client, Session = api
    assert client.get("/videos/999/full").status_code == 404
    pending = _video(Session, full="downloading")
    assert client.get(f"/videos/{pending}/full").status_code == 409
    gone = _video(Session, full="done", path="missing.mp4", tag="2", pid="bbbbbbbbbbb")  # file deleted by hand
    assert client.get(f"/videos/{gone}/full").status_code == 404
    assert _row(Session, gone).full_status == "none"  # the dashboard is told the truth and offers a fresh download


def test_a_stored_path_cannot_escape_the_downloads_folder(api, tmp_path):
    client, Session = api
    (tmp_path / "secret.txt").write_text("not for download")
    vid = _video(Session, full="done", path="../secret.txt")
    assert client.get(f"/videos/{vid}/full").status_code == 404
    absolute = _video(Session, full="done", path=str(tmp_path / "secret.txt"), tag="2", pid="bbbbbbbbbbb")
    assert client.get(f"/videos/{absolute}/full").status_code == 404
    assert (tmp_path / "secret.txt").exists()  # and nothing outside the folder was touched


def test_the_delete_endpoint_removes_the_file(api):
    client, Session = api
    f = settings.downloads_dir / "abcdefghijk.mp4"
    f.write_bytes(b"x")
    vid = _video(Session, full="done", path="abcdefghijk.mp4")
    assert client.delete(f"/videos/{vid}/full").status_code == 200 and not f.exists()
    assert client.delete(f"/videos/{vid}/full").status_code == 404
    assert client.delete("/videos/999/full").status_code == 404
