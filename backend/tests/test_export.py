import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import export as exportmod
from app.config import settings
from app.judge import build_prompt


def _platform(key):
    return next(p for p in exportmod.PLATFORMS if p.key == key)


def test_split_caption_separates_and_dedupes_trailing_tags():
    body, tags = exportmod.split_caption("Huge reveal here. #GTAVI #Trailer #GTAVI")
    assert body == "Huge reveal here." and tags == ["#GTAVI", "#Trailer"]


def test_split_caption_keeps_inline_hashtag_like_text():
    body, tags = exportmod.split_caption("This is the #1 reason to wait. #Clutch #GTAVI")
    assert body == "This is the #1 reason to wait." and tags == ["#Clutch", "#GTAVI"]


def test_build_post_respects_limits():
    tags = " ".join(f"#t{i}" for i in range(40))
    t = exportmod.build_post(_platform("tiktok"), "T", "Hook line", f"A caption. {tags}")
    assert t["title"] == "" and t["text"].startswith("Hook line")
    assert len(t["text"]) <= 2200 and t["text"].count("#") <= 10
    y = exportmod.build_post(_platform("youtube_shorts"), "x" * 300, "h", "c #GTAVI")
    assert len(y["title"]) <= 100 and "#Shorts" in y["text"]


def test_hashtag_limit_is_shared_with_the_hook():
    tiktok = _platform("tiktok")
    # untagged hook still allows all ten caption tags
    ok = exportmod.build_post(tiktok, "", "Plain hook", "Cap. " + " ".join(f"#a{i}" for i in range(10)))
    assert ok["text"].count("#") == 10
    # a hook with eleven hashtags is cut to the limit even with an empty caption
    hook = "Wow " + " ".join(f"#h{i}" for i in range(11))
    cut = exportmod.build_post(tiktok, "", hook, "")
    assert cut["text"].count("#") == 10


def test_build_prompt_includes_only_present_types_and_defaults_missing():
    p = build_prompt([{"id": 1, "type": "news"}])
    assert "News:" in p and "Commentary:" not in p and "Gameplay:" not in p
    q = build_prompt([{"id": 1}, {"id": 2, "type": "news"}])
    assert "Gameplay:" in q and "News:" in q
    assert '"type": "gameplay"' in q and '"type": "news"' in q


def test_old_database_gains_new_columns_and_keeps_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # schema as it was before Stage 4
        conn.execute(text("CREATE TABLE streamers (id INTEGER PRIMARY KEY, platform VARCHAR(20), "
                          "name VARCHAR(100), channel_url VARCHAR(300))"))
        conn.execute(text("INSERT INTO streamers (platform, name, channel_url) VALUES ('youtube','Old','u')"))
    dbmod.init_db(engine)
    Session = sessionmaker(engine)
    with Session() as s:
        streamer = s.query(dbmod.Streamer).one()
        assert streamer.name == "Old" and streamer.content_type == "gameplay"
    dbmod.init_db(engine)  # running it again is harmless


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.Base.metadata.create_all(engine)
    monkeypatch.setattr(exportmod, "SessionLocal", Session)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert settings.exports_dir.is_relative_to(tmp_path)  # exports can't escape the temp dir
    settings.clips_dir.mkdir()
    (settings.clips_dir / "v.mp4").write_bytes(b"vertical")
    (settings.clips_dir / "w.mp4").write_bytes(b"wide")
    with Session() as s:
        st = dbmod.Streamer(name="x", channel_url="u")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id="a", title="t", url="u")
        s.add(v)
        s.flush()
        common = dict(video_id=v.id, start_s=0, end_s=30, score=1, render_status="done",
                      vertical_path="v.mp4", wide_path="w.mp4")
        ok = dbmod.Candidate(title="Title", hook="Hook", caption="Cap. #GTAVI", review_status="approved", **common)
        pending = dbmod.Candidate(review_status="pending", **common)
        s.add_all([ok, pending])
        s.commit()
        return ok.id, pending.id


def test_export_writes_folder_per_platform(env):
    ok_id, _ = env
    m = exportmod.export_candidate(ok_id)
    assert [p["key"] for p in m["platforms"]] == [p.key for p in exportmod.PLATFORMS]
    root = settings.exports_dir / str(ok_id)
    assert (root / "tiktok" / f"clip_{ok_id}.mp4").read_bytes() == b"vertical"
    assert (root / "youtube_video" / f"clip_{ok_id}.mp4").read_bytes() == b"wide"
    assert "TITLE: Title" in (root / "youtube_shorts" / "post.txt").read_text(encoding="utf-8")
    assert (root / "manifest.json").exists()
    exportmod.export_candidate(ok_id)  # re-export over an existing folder works
    assert not list(settings.exports_dir.glob(".*"))  # no staging leftovers


def test_export_is_independent_of_later_rerenders(env):
    ok_id, _ = env
    exportmod.export_candidate(ok_id)
    with open(settings.clips_dir / "v.mp4", "r+b") as f:  # re-render rewrites the file in place
        f.write(b"CHANGED!")
    exported = settings.exports_dir / str(ok_id) / "tiktok" / f"clip_{ok_id}.mp4"
    assert exported.read_bytes() == b"vertical"


def test_failed_reexport_keeps_previous_bundle(env, monkeypatch):
    ok_id, _ = env
    exportmod.export_candidate(ok_id)
    (settings.clips_dir / "w.mp4").unlink()  # make the next export fail midway
    with pytest.raises(FileNotFoundError):
        exportmod.export_candidate(ok_id)
    root = settings.exports_dir / str(ok_id)
    assert (root / "tiktok" / f"clip_{ok_id}.mp4").read_bytes() == b"vertical"
    assert not (settings.exports_dir / f".{ok_id}.staging").exists()


def test_export_refuses_unapproved(env):
    _, pending_id = env
    with pytest.raises(ValueError, match="approved"):
        exportmod.export_candidate(pending_id)
