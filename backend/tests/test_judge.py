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


def _item(i, **over):
    return {"id": i, "score": 7, "title": f"T{i}", "hook": f"H{i}", "caption": f"C{i} #GTA6", "reason": f"R{i}", **over}


def _reply(*ids):
    return json.dumps([_item(i) for i in ids])


def test_parse_accepts_a_fenced_reply():
    assert [v.id for v in parse_verdicts("```json\n" + _reply(1, 2) + "\n```")] == [1, 2]


def test_a_reply_cut_off_mid_object_keeps_the_complete_ones():
    whole = _reply(1, 2, 3)
    cut = whole[: whole.index('"id": 3') + 25]  # the third object is truncated
    assert [v.id for v in parse_verdicts(cut)] == [1, 2]


def test_an_unescaped_quote_costs_only_its_own_object():
    bad = '{"id": 2, "score": 7, "title": "He said "stop" twice", "hook": "h", "caption": "c", "reason": "r"}'
    raw = "[" + json.dumps(_item(1)) + ", " + bad + ", " + json.dumps(_item(3)) + "]"
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)  # the whole reply is invalid JSON, as in the real failure...
    assert [v.id for v in parse_verdicts(raw)] == [1, 3]  # ...yet the intact moments are kept


def test_individually_malformed_items_are_skipped_not_fatal():
    raw = json.dumps([_item(1), {"id": 2, "title": "no score"}, _item(3, score="n/a"),
                      _item(4, score=float("nan")), "junk", _item(5)])
    assert [v.id for v in parse_verdicts(raw)] == [1, 5]


@pytest.mark.parametrize("raw", ["", "I'm sorry, I can't do that.", "[]", "{}", '{"results": "none"}', "[1, 2]",
                                 '```json\nnot json\n```'])
def test_a_reply_with_nothing_usable_raises_bad_reply(raw):
    with pytest.raises(judgemod.BadReply):
        parse_verdicts(raw)


# ---- the Gemini wrapper: scripted replies instead of the network ----------------------------

def _server_error(code=503):
    from google.genai import errors
    return errors.ServerError(code, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}, None)


def _client_error(code):
    from google.genai import errors
    return errors.ClientError(code, {"error": {"message": "nope", "status": "X"}}, None)


class Scripted(judgemod.GeminiJudge):
    """A GeminiJudge whose raw API call is replaced by a script of replies (or errors to raise)."""

    def __init__(self, *script):
        self.model, self.script, self.calls = "primary", list(script), []

    def _generate(self, model, prompt):
        self.calls.append((model, prompt))
        step = self.script.pop(0) if self.script else self.script_default
        if isinstance(step, Exception):
            raise step
        return step

    script_default = "no more replies"


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(judgemod.settings, "judge_retry_wait_s", 0)
    monkeypatch.setattr(judgemod.settings, "gemini_fallback_models", ["fallback"])
    monkeypatch.setattr(judgemod.settings, "judge_attempts_per_model", 2)
    monkeypatch.setattr(judgemod.settings, "judge_reply_attempts", 3)


MOMENTS = [{"id": i, "transcript": f"line {i}", "chat": "", "signals": {}} for i in (1, 2, 3)]


@pytest.fixture()
def moments_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(judgemod.settings, "data_dir", tmp_path)
    judgemod.settings.moments_dir.mkdir(parents=True, exist_ok=True)
    return judgemod.settings.moments_dir


def _with_audio(i, name):
    return {"id": i, "transcript": f"line {i}", "chat": "", "signals": {}, "audio_path": name}


def test_without_audio_the_contents_are_just_the_text_prompt(moments_dir):
    contents = judgemod.build_contents(MOMENTS)
    assert isinstance(contents, str) and "audio recording" not in contents


def test_attached_audio_is_labelled_per_moment_and_the_model_is_told_to_listen(moments_dir):
    (moments_dir / "1.mp3").write_bytes(b"ID3-one")
    (moments_dir / "3.mp3").write_bytes(b"ID3-three")
    moments = [_with_audio(1, "1.mp3"), _with_audio(2, None), _with_audio(3, "3.mp3")]
    contents = judgemod.build_contents(moments)
    assert isinstance(contents, list) and len(contents) == 5  # prompt + (label, audio) x 2
    assert "audio recording" in contents[0] and "HEAR" in contents[0]
    assert [contents[1], contents[3]] == ["Audio of moment 1:", "Audio of moment 3:"]
    assert contents[2].inline_data.data == b"ID3-one" and contents[2].inline_data.mime_type == "audio/mpeg"
    assert "audio_path" not in contents[0]  # a file name on this machine means nothing to the model


@pytest.mark.parametrize("name", ["missing.mp3", "../outside.mp3", "/etc/passwd", "", None])
def test_audio_that_is_missing_or_outside_the_folder_is_ignored(moments_dir, tmp_path, name):
    (tmp_path / "outside.mp3").write_bytes(b"not ours")
    assert isinstance(judgemod.build_contents([_with_audio(1, name)]), str)


def test_oversized_audio_is_skipped(moments_dir):
    (moments_dir / "1.mp3").write_bytes(b"x" * (judgemod.MAX_AUDIO_BYTES + 1))
    assert isinstance(judgemod.build_contents([_with_audio(1, "1.mp3")]), str)


def test_audio_can_be_switched_off(moments_dir, monkeypatch):
    (moments_dir / "1.mp3").write_bytes(b"x")
    monkeypatch.setattr(judgemod.settings, "judge_audio", False)
    assert isinstance(judgemod.build_contents([_with_audio(1, "1.mp3")]), str)


def test_the_retry_attaches_audio_only_for_the_moments_still_missing(fast, moments_dir):
    for i in (1, 2, 3):
        (moments_dir / f"{i}.mp3").write_bytes(f"audio{i}".encode())
    moments = [_with_audio(i, f"{i}.mp3") for i in (1, 2, 3)]
    j = Scripted(_reply(1, 2), _reply(3))
    assert sorted(v.id for v in j.judge(moments)) == [1, 2, 3]
    first, second = j.calls[0][1], j.calls[1][1]
    assert [p for p in first if isinstance(p, str) and p.startswith("Audio of")] == [
        "Audio of moment 1:", "Audio of moment 2:", "Audio of moment 3:"]
    assert [p for p in second if isinstance(p, str) and p.startswith("Audio of")] == ["Audio of moment 3:"]


def test_only_the_missing_moments_are_asked_for_again(fast):
    j = Scripted(_reply(1, 2), _reply(3))  # the first reply left moment 3 out
    got = j.judge(MOMENTS)
    assert sorted(v.id for v in got) == [1, 2, 3]
    assert len(j.calls) == 2
    retry_prompt = j.calls[1][1]
    assert '"id": 3' in retry_prompt and '"id": 1' not in retry_prompt  # progress isn't redone


def test_a_damaged_first_reply_is_repaired_by_asking_for_just_what_is_missing(fast):
    damaged = "[" + json.dumps(_item(1)) + ', {"id": 2, "score": 7, "title": "a "quote" b"}, ' + json.dumps(_item(3)) + "]"
    j = Scripted(damaged, _reply(2))
    assert sorted(v.id for v in j.judge(MOMENTS)) == [1, 2, 3]
    assert '"id": 2' in j.calls[1][1] and '"id": 3' not in j.calls[1][1]


def test_when_the_model_keeps_leaving_moments_out_the_partial_result_is_returned(fast):
    j = Scripted(_reply(1), _reply(1), _reply(1))
    assert [v.id for v in j.judge(MOMENTS)] == [1]  # the other two stay unscored for a later run
    assert len(j.calls) == 3


def test_replies_for_moments_nobody_asked_about_are_ignored(fast):
    j = Scripted(_reply(1, 99), _reply(2, 3))
    assert sorted(v.id for v in j.judge(MOMENTS)) == [1, 2, 3]


def test_unreadable_replies_every_time_raise_bad_reply(fast):
    j = Scripted("garbage", "more garbage", "still garbage")
    with pytest.raises(judgemod.BadReply):
        j.judge(MOMENTS)
    assert len(j.calls) == 3


def test_an_overloaded_model_is_retried_then_the_fallback_model_is_used(fast):
    j = Scripted(_server_error(), _server_error(), _reply(1, 2, 3))
    assert sorted(v.id for v in j.judge(MOMENTS)) == [1, 2, 3]
    assert [m for m, _ in j.calls] == ["primary", "primary", "fallback"]


def test_a_rate_limit_is_retried(fast):
    j = Scripted(_client_error(429), _reply(1, 2, 3))
    assert len(j.judge(MOMENTS)) == 3
    assert [m for m, _ in j.calls] == ["primary", "primary"]


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_an_error_no_retry_can_fix_is_raised_at_once(fast, code):
    j = Scripted(_client_error(code), _reply(1, 2, 3))
    with pytest.raises(Exception) as e:
        j.judge(MOMENTS)
    assert getattr(e.value, "code", None) == code
    assert len(j.calls) == 1  # no retry, no fallback model: they would only hide the real problem


def test_every_model_being_overloaded_raises_the_overload(fast):
    from google.genai import errors
    j = Scripted(*[_server_error() for _ in range(4)])
    with pytest.raises(errors.ServerError):
        j.judge(MOMENTS)
    assert [m for m, _ in j.calls] == ["primary", "primary", "fallback", "fallback"]


# ---- judge_pending around the judge ---------------------------------------------------------

def test_an_unreadable_batch_is_skipped_and_the_others_still_scored(temp_db, monkeypatch):
    monkeypatch.setattr(judgemod.settings, "judge_batch_size", 1)

    class Flaky(Judge):
        def judge(self, moments):
            if moments[0]["id"] == 2:
                raise judgemod.BadReply("garbled")
            return FakeJudge().judge(moments)

    assert judge_pending(Flaky()) == 2
    with temp_db() as s:
        scored = {c.id: c.ai_score is not None for c in s.scalars(select(dbmod.Candidate))}
    assert scored == {1: True, 2: False, 3: True}  # the garbled moment stays unscored, for a later run


def test_if_every_batch_is_unreadable_the_error_is_raised(temp_db, monkeypatch):
    class Garbled(Judge):
        def judge(self, moments):
            raise judgemod.BadReply("garbled")

    with pytest.raises(judgemod.BadReply):
        judge_pending(Garbled())


def test_an_unavailable_api_stops_the_run_but_keeps_earlier_batches(temp_db, monkeypatch):
    monkeypatch.setattr(judgemod.settings, "judge_batch_size", 1)

    class Dies(Judge):
        def __init__(self):
            self.n = 0

        def judge(self, moments):
            self.n += 1
            if self.n == 2:
                raise _server_error()
            return FakeJudge().judge(moments)

    from google.genai import errors
    with pytest.raises(errors.ServerError):
        judge_pending(Dies())
    with temp_db() as s:
        assert sum(c.ai_score is not None for c in s.scalars(select(dbmod.Candidate))) == 1  # batch 1 was kept


def test_judge_pending_scores_and_rejects_low(temp_db):
    assert judge_pending(FakeJudge()) == 3
    with temp_db() as s:
        cands = list(s.scalars(select(dbmod.Candidate).order_by(dbmod.Candidate.id)))
    assert all(c.ai_score is not None and c.title for c in cands)
    assert [c.review_status for c in cands] == ["pending", "rejected", "pending"]
    assert judge_pending(FakeJudge()) == 0  # already judged: nothing to do
