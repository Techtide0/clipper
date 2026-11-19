import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import editor
from app import fullvideo
from app import main as mainmod
from app import pipeline
from app.config import settings
from app.main import app


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """The real app on a temporary database and a temporary clips folder."""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.init_db(engine)
    for mod in (mainmod, editor, pipeline, fullvideo):
        monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mainmod, "init_db", lambda: None)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        yield client, Session


def _clip(Session, *, title="GTA VI Intense Hostage Rescue", review="approved", render="done", tag="1",
          vertical="v.mp4", wide="w.mp4"):
    with Session() as s:
        st = dbmod.Streamer(name=f"c{tag}", channel_url=f"u{tag}", platform="youtube")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id=f"id{tag}", title="t", url="https://youtu.be/x")
        s.add(v)
        s.flush()
        c = dbmod.Candidate(video_id=v.id, start_s=0, end_s=30, score=1.0, title=title, review_status=review,
                            render_status=render, vertical_path=vertical, wide_path=wide)
        s.add(c)
        s.commit()
        return c.id


def _status(Session, cid):
    with Session() as s:
        c = s.get(dbmod.Candidate, cid)
        return c.render_status, c.render_error


# ---- downloading ------------------------------------------------------------------------------

def test_a_rendered_clip_downloads_as_an_attachment_named_after_its_title(api):
    client, Session = api
    (settings.clips_dir / "v.mp4").write_bytes(b"VERTICAL-BYTES")
    (settings.clips_dir / "w.mp4").write_bytes(b"WIDE-BYTES")
    cid = _clip(Session)
    v = client.get(f"/candidates/{cid}/download/vertical")
    w = client.get(f"/candidates/{cid}/download/wide")
    assert (v.status_code, v.content, v.headers["content-type"]) == (200, b"VERTICAL-BYTES", "video/mp4")
    assert w.content == b"WIDE-BYTES"
    assert v.headers["content-disposition"].startswith("attachment")  # the browser saves it, not plays it
    assert "GTA-VI-Intense-Hostage-Rescue-9x16.mp4" in v.headers["content-disposition"]
    assert "GTA-VI-Intense-Hostage-Rescue-16x9.mp4" in w.headers["content-disposition"]


@pytest.mark.parametrize("title,expected", [
    ('He said "stop" / then <left>', "He-said-stop-then-left"),
    ("../../etc/passwd", "etcpasswd"),
    ("line one\nline two\r\nHeader: injected", "line-one-line-two-Header-injected"),
    ("   ", "clip-1"),
    (None, "clip-1"),
    ("🔥🔥🔥", "clip-1"),
    ("É" * 200, "É" * 60),
])
def test_download_names_are_safe_whatever_the_title(title, expected):
    name = editor.download_name(title, 1, "vertical")
    assert name == f"{expected}-9x16.mp4"
    assert not any(ch in name for ch in '/\\:*?"<>|\r\n')


def test_the_download_header_cannot_be_injected_through_a_title(api):
    client, Session = api
    (settings.clips_dir / "v.mp4").write_bytes(b"x")
    cid = _clip(Session, title='evil"\r\nSet-Cookie: a=b')
    r = client.get(f"/candidates/{cid}/download/vertical")
    assert r.status_code == 200 and "set-cookie" not in {k.lower() for k in r.headers}


def test_unusable_downloads_say_why(api):
    client, Session = api
    assert client.get("/candidates/999/download/vertical").status_code == 404                  # no such clip
    not_ready = _clip(Session, render="none", tag="a")
    assert client.get(f"/candidates/{not_ready}/download/vertical").status_code == 409         # not rendered yet
    gone = _clip(Session, tag="b")  # says rendered, but the file was deleted
    assert client.get(f"/candidates/{gone}/download/vertical").status_code == 404
    ok = _clip(Session, tag="c")
    assert client.get(f"/candidates/{ok}/download/sideways").status_code == 422                # not a known format


def test_a_stored_path_cannot_escape_the_clips_folder(api, tmp_path):
    client, Session = api
    secret = tmp_path / "secret.txt"
    secret.write_text("not for download")
    cid = _clip(Session, vertical="../secret.txt")
    assert client.get(f"/candidates/{cid}/download/vertical").status_code == 404
    absolute = _clip(Session, vertical=str(secret), tag="2")
    assert client.get(f"/candidates/{absolute}/download/vertical").status_code == 404


# ---- rendering ----------------------------------------------------------------------------------

@pytest.fixture()
def rendered(monkeypatch):
    calls = []
    monkeypatch.setattr(editor, "render_candidate", lambda cid: calls.append(cid))
    return calls


def test_render_all_queues_only_approved_clips_that_need_it(api, rendered):
    client, Session = api
    want = [_clip(Session, review="approved", render="none", tag="a"),
            _clip(Session, review="approved", render="failed", tag="b")]
    skip = [_clip(Session, review="approved", render="done", tag="c"),
            _clip(Session, review="approved", render="rendering", tag="d"),
            _clip(Session, review="pending", render="none", tag="e"),
            _clip(Session, review="rejected", render="none", tag="f")]
    r = client.post("/candidates/render-approved")
    assert r.status_code == 200 and sorted(r.json()["queued"]) == sorted(want)
    assert sorted(rendered) == sorted(want)
    assert [_status(Session, c)[0] for c in want] == ["rendering", "rendering"]
    assert [_status(Session, c)[0] for c in skip] == ["done", "rendering", "none", "none"]  # untouched
    assert client.post("/candidates/render-approved").json()["queued"] == []  # a second click finds nothing to do
    assert sorted(rendered) == sorted(want)  # ...and queues nothing twice


def test_render_all_clears_an_earlier_failure_message(api, rendered):
    client, Session = api
    cid = _clip(Session, render="failed")
    with Session() as s:
        s.get(dbmod.Candidate, cid).render_error = "boom"
        s.commit()
    client.post("/candidates/render-approved")
    assert _status(Session, cid) == ("rendering", None)


def _patch(client, cid, **body):
    r = client.patch(f"/candidates/{cid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_approving_a_moment_cuts_it_straight_away(api, rendered):
    client, Session = api
    cid = _clip(Session, review="pending", render="none")
    out = _patch(client, cid, review_status="approved")
    assert out["render_status"] == "rendering" and rendered == [cid]  # queued, and the reply already says so
    assert _status(Session, cid)[0] == "rendering"


def test_approving_cuts_a_clip_whose_earlier_cut_failed(api, rendered):
    client, Session = api
    cid = _clip(Session, review="pending", render="failed")
    assert _patch(client, cid, review_status="approved")["render_status"] == "rendering"
    assert rendered == [cid]


@pytest.mark.parametrize("review,render", [("pending", "done"), ("pending", "rendering")])
def test_approving_never_cuts_a_clip_that_is_already_cut_or_being_cut(api, rendered, review, render):
    client, Session = api
    cid = _clip(Session, review=review, render=render)
    _patch(client, cid, review_status="approved")
    assert rendered == [] and _status(Session, cid)[0] == render


def test_only_approval_or_a_new_trim_starts_a_cut_not_other_edits_or_rejection(api, rendered):
    client, Session = api
    pending = _clip(Session, review="pending", render="none", tag="a")
    _patch(client, pending, title="A better title", caption="new caption")
    _patch(client, pending, review_status="rejected")
    failed_approved = _clip(Session, review="approved", render="failed", tag="b")
    _patch(client, failed_approved, caption="typo fixed")  # must not retry a failing render on every edit
    _patch(client, failed_approved, views=100)
    assert rendered == []
    assert [_status(Session, c)[0] for c in (pending, failed_approved)] == ["none", "failed"]


def test_changing_the_trim_of_an_approved_clip_cuts_it_again(api, rendered):
    client, Session = api
    cid = _clip(Session, review="approved", render="done")  # the old cut no longer matches the new bounds
    out = _patch(client, cid, start_s=5, end_s=40)
    assert out["render_status"] == "rendering" and rendered == [cid]


def test_trimming_a_moment_you_have_not_approved_does_not_cut_it(api, rendered):
    client, Session = api
    cid = _clip(Session, review="pending", render="done")
    out = _patch(client, cid, start_s=5, end_s=40)
    assert out["render_status"] == "none" and rendered == []  # the stale cut is dropped; cutting waits for you


def test_a_single_render_is_claimed_atomically(api, rendered):
    client, Session = api
    cid = _clip(Session, render="none")
    assert client.post(f"/candidates/{cid}/render").status_code == 200
    assert client.post(f"/candidates/{cid}/render").status_code == 409  # already rendering: no second job
    assert rendered == [cid]
    done = _clip(Session, render="done", tag="x")
    assert client.post(f"/candidates/{done}/render").status_code == 200  # re-rendering a finished clip is allowed
    assert client.post("/candidates/999/render").status_code == 404


def test_two_requests_cannot_both_claim_the_same_render(api, rendered):
    client, Session = api
    cid = _clip(Session, render="none")
    wins, barrier = [], threading.Barrier(6)

    def click():
        barrier.wait()
        with Session() as s:
            wins.append(mainmod._claim_render(s, cid))

    threads = [threading.Thread(target=click) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_clips_are_cut_one_at_a_time(api, monkeypatch):
    client, Session = api
    ids = [_clip(Session, render="none", tag=str(i)) for i in range(4)]
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_download(url, start, end, dest, extractors=(), exact=True):
        return Path("section.mp4")

    def fake_make_formats(src, out_dir, name, layout="fit"):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.1)  # long enough that overlapping renders would be caught
        with lock:
            state["now"] -= 1
        return Path(f"{name}_9x16.mp4"), Path(f"{name}_16x9.mp4")

    monkeypatch.setattr(editor, "download_section", fake_download)
    monkeypatch.setattr(editor, "make_formats", fake_make_formats)
    threads = [threading.Thread(target=editor.render_candidate, args=(cid,)) for cid in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["max"] == 1  # never two ffmpeg jobs at once
    assert [_status(Session, c)[0] for c in ids] == ["done"] * 4


def test_a_render_that_was_running_when_the_server_stopped_can_be_retried(api):
    client, Session = api
    stuck = _clip(Session, render="rendering", tag="a")
    fine = _clip(Session, render="done", tag="b")
    assert editor.recover_renders() == 1
    status, error = _status(Session, stuck)
    assert status == "failed" and "interrupted by a restart" in error
    assert _status(Session, fine) == ("done", None)  # finished clips are left alone
    assert client.post(f"/candidates/{stuck}/render").status_code == 200  # and the button works again


# ---- layout, fast cuts and edits that must not throw a finished cut away ---------------------------------

def test_saving_a_caption_does_not_discard_a_finished_cut(api, rendered):
    """Regression: the dashboard always sends the trim fields too, and that used to invalidate the cut."""
    client, Session = api
    cid = _clip(Session, review="approved", render="done")
    out = _patch(client, cid, title="New title", caption="New caption", start_s=0, end_s=30, layout="fit")
    assert out["render_status"] == "done" and rendered == []


def test_a_different_framing_makes_the_cut_stale_and_recuts_an_approved_clip(api, rendered):
    client, Session = api
    approved = _clip(Session, review="approved", render="done", tag="a")
    out = _patch(client, approved, layout="fill")
    assert (out["layout"], out["render_status"]) == ("fill", "rendering") and rendered == [approved]
    pending = _clip(Session, review="pending", render="done", tag="b")
    out = _patch(client, pending, layout="fill")
    assert (out["layout"], out["render_status"]) == ("fill", "none")  # stale, but cutting waits for the owner
    assert rendered == [approved]


def test_only_known_layouts_are_accepted(api):
    client, Session = api
    cid = _clip(Session)
    assert client.patch(f"/candidates/{cid}", json={"layout": "diagonal"}).status_code == 422
    assert client.get("/candidates?status=approved").json()[0]["layout"] == "fit"  # the default


@pytest.fixture()
def spy_render(monkeypatch):
    """Runs the real render_candidate with downloading and encoding replaced by recorders."""
    seen = {}

    def fake_download(url, start, end, dest, extractors=(), exact=True):
        seen["download"] = {"start": start, "end": end, "extractors": extractors, "exact": exact}
        return Path("section.mp4")

    def fake_formats(src, out_dir, name, layout="fit"):
        seen["layout"] = layout
        return Path(f"{name}_9x16.mp4"), Path(f"{name}_16x9.mp4")

    monkeypatch.setattr(editor, "download_section", fake_download)
    monkeypatch.setattr(editor, "make_formats", fake_formats)
    return seen


def test_youtube_clips_use_the_fast_cut_and_the_chosen_layout(api, spy_render):
    _, Session = api
    cid = _clip(Session, render="none")
    with Session() as s:
        s.get(dbmod.Candidate, cid).layout = "fill"
        s.commit()
    editor.render_candidate(cid)
    assert spy_render["download"]["exact"] is False and spy_render["layout"] == "fill"
    assert spy_render["download"]["extractors"] == ("youtube.*",)
    assert _status(Session, cid)[0] == "done"


@pytest.mark.parametrize("platform", ["twitch", "kick"])
def test_other_platforms_keep_the_exact_cut_until_the_fast_one_is_verified_for_them(api, spy_render, platform):
    _, Session = api
    cid = _clip(Session, render="none")
    with Session() as s:
        c = s.get(dbmod.Candidate, cid)
        c.video.streamer.platform = platform
        s.commit()
    editor.render_candidate(cid)
    assert spy_render["download"]["exact"] is True


@pytest.mark.parametrize("review, cuts, final", [("pending", 1, "none"), ("approved", 2, "done")])
def test_a_layout_change_during_a_render_marks_that_render_stale(api, monkeypatch, review, cuts, final):
    _, Session = api
    cid = _clip(Session, render="none", review=review)
    calls = []

    def download_then_user_changes_layout(url, start, end, dest, extractors=(), exact=True):
        calls.append(1)
        if len(calls) == 1:  # the edit happens during the first cut only
            with Session() as s:
                s.get(dbmod.Candidate, cid).layout = "fill"
                s.commit()
        return Path("section.mp4")

    monkeypatch.setattr(editor, "download_section", download_then_user_changes_layout)
    monkeypatch.setattr(editor, "make_formats", lambda *a, **k: (Path("v.mp4"), Path("w.mp4")))
    editor.render_candidate(cid)
    # the first output was for the old framing, so it is never presented as the clip: an approved
    # clip is cut again with the new framing, one still being reviewed just waits to be cut
    assert len(calls) == cuts and _status(Session, cid)[0] == final
