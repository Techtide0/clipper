import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as dbmod
from app import judge as judgemod
from app.judge import Judge, Verdict, judge_pending, parse_verdicts


def test_parse_verdicts_clamps_and_unwraps():
    raw = json.dumps({"results": [{"id": 1, "score": 14, "title": "T", "hook": "H", "caption": "C", "reason": "R"}]})
    v = parse_verdicts(raw)
    assert v[0].id == 1 and v[0].score == 10.0


class FakeJudge(Judge):
    def judge(self, moments):
        return [Verdict(m["id"], 9.0 if m["id"] % 2 else 2.0, f"title {m['id']}", "hook", "cap", "why")
                for m in moments]


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Session = sessionmaker(engine, expire_on_commit=False)
    dbmod.Base.metadata.create_all(engine)
    monkeypatch.setattr(judgemod, "SessionLocal", Session)
    with Session() as s:
        st = dbmod.Streamer(name="x", channel_url="u")
        s.add(st)
        s.flush()
        v = dbmod.Video(streamer_id=st.id, platform_id="abc", title="t", url="u")
        s.add(v)
        s.flush()
        for i in range(3):
            s.add(dbmod.Candidate(video_id=v.id, start_s=i * 100, end_s=i * 100 + 30, score=1.0,
                                  signals={"audio": 0.5}, transcript="oh my god"))
        s.commit()
    return Session


def test_judge_pending_scores_and_rejects_low(temp_db):
    assert judge_pending(FakeJudge()) == 3
    with temp_db() as s:
        cands = list(s.scalars(select(dbmod.Candidate).order_by(dbmod.Candidate.id)))
    assert all(c.ai_score is not None and c.title for c in cands)
    assert [c.review_status for c in cands] == ["pending", "rejected", "pending"]
    assert judge_pending(FakeJudge()) == 0  # already judged: nothing to do
