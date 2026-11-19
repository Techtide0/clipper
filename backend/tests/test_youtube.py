"""YouTube posting and view-count reading, against a stand-in for YouTube's API (no network, no Google account)."""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import automation, pipeline, runtime, youtube
from app import db as dbmod
from app import main as mainmod
from app.config import settings
from app.main import app


@pytest.fixture()
def yt(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (mainmod, youtube, pipeline, automation):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    return Session


def _clip(Session, *, review="approved", render="done", yt_status="none", tag="1", title="GTA VI Intense Hostage Rescue",
          hook="Come untie me, man!", caption="A tense rescue. #GTA6 #Gameplay", vertical="v.mp4", write=True):
    if write and vertical:
        (settings.clips_dir / vertical).write_bytes(b"\x00" * 2048)
    with Session() as s:
        st = dbmod.Streamer(name=f"c{tag}", channel_url=f"u{tag}", platform="youtube")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id=f"id{tag}", title="t", url="u")
        s.add(v)
        s.flush()
        c = dbmod.Candidate(video_id=v.id, start_s=0, end_s=30, score=1.0, title=title, hook=hook, caption=caption,
                            review_status=review, render_status=render, yt_status=yt_status, vertical_path=vertical)
        s.add(c)
        s.commit()
        return c.id


def _row(Session, cid):
    with Session() as s:
        return s.get(dbmod.Candidate, cid)


class _Req:
    """What videos().insert(...) returns: an upload that finishes after a few chunks."""

    def __init__(self, steps, final):
        self.steps, self.final = list(steps), final

    def next_chunk(self):
        step = self.steps.pop(0) if self.steps else None
        if isinstance(step, Exception):
            raise step
        if step is None:
            return None, self.final

        class Progress:
            def progress(self_inner):
                return step
        return Progress(), None


class FakeYouTube:
    def __init__(self, steps=(0.3, 0.7), final=None, stats=None, error=None):
        self.inserted, self.list_calls = [], []
        self.steps, self.final = steps, final or {"id": "VIDEO123"}
        self.stats, self.error = stats or {}, error

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted.append({"part": part, "body": body, "media": media_body})
        return _Req(self.steps if not self.error else [self.error], self.final)

    def list(self, part, id):
        self.list_calls.append(id.split(","))

        class Call:
            def __init__(self_inner, items):
                self_inner.items = items

            def execute(self_inner):
                return {"items": self_inner.items}
        return Call([{"id": v, "statistics": self.stats[v]} for v in id.split(",") if v in self.stats])


def _http_error(status, reason):
    body = json.dumps({"error": {"code": status, "errors": [{"reason": reason}], "status": reason}}).encode()
    return HttpError(Response({"status": status}), body)


# ---- sign-in state ------------------------------------------------------------------------------

def test_status_reflects_the_files_present(yt):
    s = youtube.status()
    assert (s["configured"], s["connected"], s["connecting"], s["channel"]) == (False, False, False, None)
    assert len(s["setup_steps"]) >= 5 and "client_secret.json" in s["setup_steps"][4]
    youtube.secrets_path().write_text("{}")
    youtube.token_path().write_text("{}")
    youtube._account_path().write_text(json.dumps({"title": "My Channel"}))
    s = youtube.status()
    assert (s["configured"], s["connected"], s["channel"], s["privacy"]) == (True, True, "My Channel", "private")


def test_connecting_without_the_google_client_file_explains_what_to_do(yt):
    youtube.connect()
    assert "client file is missing" in youtube.status()["connect_error"] and "client_secret.json" in youtube.status()["connect_error"]
    assert youtube.status()["connecting"] is False


def test_only_one_sign_in_window_at_a_time(yt):
    youtube._CONNECTING.set()
    try:
        assert youtube.start_connect() is False
    finally:
        youtube._CONNECTING.clear()


def test_disconnecting_forgets_the_sign_in_but_keeps_the_google_client_file(yt):
    youtube.secrets_path().write_text("{}")
    youtube.token_path().write_text("{}")
    youtube._account_path().write_text("{}")
    youtube.disconnect()
    s = youtube.status()
    assert (s["configured"], s["connected"]) == (True, False)


def test_using_youtube_without_signing_in_says_so(yt):
    with pytest.raises(youtube.NotConnected, match="isn't connected yet"):
        youtube._credentials()


# ---- uploading ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw,ok", [
    (dict(), True), (dict(yt_status="failed"), True),
    (dict(review="pending"), False), (dict(review="rejected"), False), (dict(render="none"), False),
    (dict(render="rendering"), False), (dict(yt_status="uploading"), False), (dict(yt_status="uploaded"), False),
])
def test_only_an_approved_cut_clip_that_is_not_already_up_can_start_an_upload(yt, kw, ok):
    cid = _clip(yt, **kw)
    assert youtube.claim_upload(cid) is ok
    assert youtube.claim_upload(cid) is False


def test_simultaneous_upload_requests_have_one_winner(yt):
    cid = _clip(yt)
    wins, barrier = [], threading.Barrier(6)

    def click():
        barrier.wait()
        wins.append(youtube.claim_upload(cid))

    threads = [threading.Thread(target=click) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_an_upload_sends_the_vertical_clip_with_a_title_description_and_private_by_default(yt, monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(youtube, "_service", lambda: fake)
    cid = _clip(yt)
    assert youtube.claim_upload(cid)
    youtube.upload(cid)
    row = _row(yt, cid)
    assert (row.yt_status, row.yt_video_id, row.yt_progress, row.yt_error) == ("uploaded", "VIDEO123", 100, None)
    sent = fake.inserted[0]
    assert sent["part"] == "snippet,status"
    snippet, status = sent["body"]["snippet"], sent["body"]["status"]
    assert snippet["title"] == "GTA VI Intense Hostage Rescue" and snippet["categoryId"] == "20"
    assert "#Shorts" in snippet["description"] and "#GTA6" in snippet["description"]  # same rules as the export
    assert {"Shorts", "GTA6", "Gameplay"} <= set(snippet["tags"])
    assert status == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert sent["media"].size() == 2048 and sent["media"].resumable()                  # the 9:16 file, resumable


def test_the_privacy_setting_is_honoured(yt, monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setattr(youtube, "_service", lambda: fake)
    runtime.update({"youtube_privacy": "unlisted"})
    cid = _clip(yt)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    assert fake.inserted[0]["body"]["status"]["privacyStatus"] == "unlisted"


def test_progress_is_recorded_while_uploading(yt, monkeypatch):
    seen = []
    real_set = youtube._set
    monkeypatch.setattr(youtube, "_set", lambda cid, **v: (seen.append(v.get("yt_progress")), real_set(cid, **v))[1])
    monkeypatch.setattr(youtube, "_service", lambda: FakeYouTube(steps=(0.25, 0.6, 0.999)))
    cid = _clip(yt)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    assert [p for p in seen if p is not None] == [25, 60, 99, 100]  # never 100 until it is really done


@pytest.mark.parametrize("reason,fragment", [
    ("quotaExceeded", "allowance for this app is used up"),
    ("uploadLimitExceeded", "upload limit"),
    ("insufficientPermissions", "connect again"),
    ("authError", "sign-in has expired"),
])
def test_youtubes_errors_are_turned_into_plain_sentences(yt, monkeypatch, reason, fragment):
    monkeypatch.setattr(youtube, "_service", lambda: FakeYouTube(error=_http_error(403, reason)))
    cid = _clip(yt)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    row = _row(yt, cid)
    assert row.yt_status == "failed" and fragment in row.yt_error and row.yt_video_id is None
    assert youtube.claim_upload(cid)  # and it can be retried


def test_an_expired_sign_in_fails_the_upload_with_instructions(yt, monkeypatch):
    def not_connected():
        raise youtube.NotConnected("the YouTube sign-in has expired: connect again in Settings & health")

    monkeypatch.setattr(youtube, "_service", not_connected)
    cid = _clip(yt)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    assert "connect again" in _row(yt, cid).yt_error


def test_a_clip_whose_file_is_gone_fails_clearly(yt, monkeypatch):
    monkeypatch.setattr(youtube, "_service", lambda: FakeYouTube())
    cid = _clip(yt, write=False)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    assert "file is missing" in _row(yt, cid).yt_error


def test_a_stored_path_cannot_make_an_upload_read_outside_the_clips_folder(yt, monkeypatch, tmp_path):
    fake = FakeYouTube()
    monkeypatch.setattr(youtube, "_service", lambda: fake)
    (tmp_path / "secret.mp4").write_bytes(b"not a clip")
    cid = _clip(yt, vertical="../secret.mp4", write=False)
    youtube.claim_upload(cid)
    youtube.upload(cid)
    assert fake.inserted == [] and _row(yt, cid).yt_status == "failed"


def test_an_upload_interrupted_by_a_restart_can_be_retried(yt):
    stuck = _clip(yt, yt_status="uploading", tag="a")
    done = _clip(yt, yt_status="uploaded", tag="b")
    assert youtube.recover_uploads() == 1
    assert _row(yt, stuck).yt_status == "failed" and "interrupted" in _row(yt, stuck).yt_error
    assert _row(yt, done).yt_status == "uploaded"


# ---- reading views back -------------------------------------------------------------------------------

def _uploaded(Session, n, **kw):
    ids = []
    for i in range(n):
        cid = _clip(Session, tag=str(i), vertical=f"v{i}.mp4", **kw)
        with Session() as s:
            c = s.get(dbmod.Candidate, cid)
            c.yt_video_id, c.yt_status = f"VID{i:03d}", "uploaded"
            s.commit()
        ids.append(cid)
    return ids


def test_views_and_likes_are_copied_from_youtube(yt, monkeypatch):
    ids = _uploaded(yt, 2)
    fake = FakeYouTube(stats={"VID000": {"viewCount": "1234", "likeCount": "56"}, "VID001": {"viewCount": "7"}})
    monkeypatch.setattr(youtube, "_service", lambda: fake)
    with yt() as s:  # the owner had typed a likes number by hand for the second clip
        s.get(dbmod.Candidate, ids[1]).likes = 3
        s.commit()
    assert youtube.refresh_stats() == 2
    a, b = _row(yt, ids[0]), _row(yt, ids[1])
    assert (a.views, a.likes) == (1234, 56)
    assert (b.views, b.likes) == (7, 3)  # likes hidden on YouTube: the number already saved is kept


def test_a_clip_youtube_does_not_return_is_left_alone(yt, monkeypatch):
    (cid,) = _uploaded(yt, 1)
    monkeypatch.setattr(youtube, "_service", lambda: FakeYouTube(stats={}))  # e.g. deleted on YouTube
    assert youtube.refresh_stats() == 0 and _row(yt, cid).views is None


def test_statistics_are_requested_in_batches_of_fifty(yt, monkeypatch):
    _uploaded(yt, 60)
    fake = FakeYouTube(stats={f"VID{i:03d}": {"viewCount": str(i)} for i in range(60)})
    monkeypatch.setattr(youtube, "_service", lambda: fake)
    assert youtube.refresh_stats() == 60
    assert [len(c) for c in fake.list_calls] == [50, 10]


def test_with_nothing_uploaded_youtube_is_not_even_contacted(yt, monkeypatch):
    def boom():
        raise AssertionError("must not contact YouTube")

    monkeypatch.setattr(youtube, "_service", boom)
    assert youtube.refresh_stats() == 0


def test_automatic_checks_bring_the_views_back_when_connected(yt, monkeypatch):
    monkeypatch.setattr(youtube, "status", lambda: {"connected": True})
    monkeypatch.setattr(youtube, "refresh_stats", lambda: 3)
    assert automation.run_cycle()["views_updated"] == 3
    monkeypatch.setattr(youtube, "refresh_stats", lambda: (_ for _ in ()).throw(RuntimeError("quota")))
    assert "YouTube views: RuntimeError: quota" in automation.run_cycle()["errors"][0]  # a failure never stops the check


# ---- settings and API -------------------------------------------------------------------------------

def test_the_privacy_setting_only_accepts_the_three_youtube_values(yt):
    assert runtime.update({"youtube_privacy": "public"})["youtube_privacy"] == "public"
    for bad in ("secret", "", 5, None):
        with pytest.raises(ValueError, match="must be one of private, unlisted, public"):
            runtime.update({"youtube_privacy": bad})
    spec = next(s for s in runtime.describe() if s["key"] == "youtube_privacy")
    assert spec["choices"] == ["private", "unlisted", "public"] and spec["value"] == "public"


@pytest.fixture()
def client(yt):
    with TestClient(app) as c:
        yield c


def test_the_publish_endpoint_checks_everything_before_queuing(yt, client, monkeypatch):
    queued = []
    monkeypatch.setattr(youtube, "upload", lambda cid: queued.append(cid))
    cid = _clip(yt)
    assert client.post("/candidates/999/publish/youtube").status_code == 404
    r = client.post(f"/candidates/{cid}/publish/youtube")
    assert r.status_code == 409 and "Connect YouTube" in r.json()["detail"]             # not signed in
    youtube.token_path().write_text("{}")
    pending = _clip(yt, review="pending", tag="p")
    assert "approved clip that has been cut" in client.post(f"/candidates/{pending}/publish/youtube").json()["detail"]
    assert client.post(f"/candidates/{cid}/publish/youtube").status_code == 200
    r = client.post(f"/candidates/{cid}/publish/youtube")
    assert r.status_code == 409 and "already uploading or uploaded" in r.json()["detail"]
    assert queued == [cid]


def test_a_clip_reports_its_youtube_state_and_link(yt, client):
    cid = _clip(yt)
    with yt() as s:
        c = s.get(dbmod.Candidate, cid)
        c.yt_status, c.yt_video_id = "uploaded", "VIDEO123"
        s.commit()
    card = client.get("/candidates?status=approved").json()[0]
    assert (card["yt_status"], card["yt_url"]) == ("uploaded", "https://www.youtube.com/watch?v=VIDEO123")


def test_the_connect_endpoint_explains_a_missing_client_file(yt, client, monkeypatch):
    r = client.post("/youtube/connect")
    assert r.status_code == 409 and "client_secret.json" in r.json()["detail"]
    youtube.secrets_path().write_text("{}")
    started = []
    monkeypatch.setattr(youtube, "start_connect", lambda: started.append(1) or True)
    assert client.post("/youtube/connect").status_code == 200 and started == [1]
    monkeypatch.setattr(youtube, "start_connect", lambda: False)
    assert client.post("/youtube/connect").status_code == 409
    assert client.get("/youtube").json()["configured"] is True
    assert client.post("/youtube/disconnect").json() == {"disconnected": True}


def test_refreshing_views_through_the_api(yt, client, monkeypatch):
    assert client.post("/youtube/refresh-stats").json() == {"updated": 0}  # nothing posted yet: nothing to fetch
    _uploaded(yt, 1)
    r = client.post("/youtube/refresh-stats")
    assert r.status_code == 409 and "isn't connected yet" in r.json()["detail"]
    monkeypatch.setattr(youtube, "refresh_stats", lambda: 4)
    assert client.post("/youtube/refresh-stats").json() == {"updated": 4}
    monkeypatch.setattr(youtube, "refresh_stats", lambda: (_ for _ in ()).throw(_http_error(403, "quotaExceeded")))
    r = client.post("/youtube/refresh-stats")
    assert r.status_code == 502 and "allowance" in r.json()["detail"]
