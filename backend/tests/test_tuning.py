import json

import numpy as np
import pytest

from app import tuning
from app.config import settings
from app.sources import SOURCES


def _rows(n, seed=0):
    """Views rise with chat, fall with audio, and ignore keywords."""
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        chat, audio, kw = rng.random(3)
        rows.append({"signals": {"audio": audio, "chat": chat, "keywords": kw},
                     "ai_score": 5 + 4 * chat, "views": int(100 + 5000 * chat - 3000 * audio + rng.normal(0, 50))})
    return rows


BASE = {"audio": 1.0, "chat": 1.5, "keywords": 0.8}


def test_not_enough_clips_suggests_nothing():
    rep = tuning.analyze(_rows(5), BASE, BASE)
    assert not rep.enough and rep.suggested == rep.current == BASE and "need at least" in rep.notes[0]


def test_repeated_apply_does_not_compound():
    rows = _rows(60)
    first = tuning.analyze(rows, BASE, BASE)
    # the next run sees the tuned weights as the active ones, but scales the configured baseline
    second = tuning.analyze(rows, first.suggested, BASE)
    assert second.suggested == first.suggested
    assert second.current == first.suggested  # "current" still shows what is active


def test_suggestion_follows_the_correlation():
    base = BASE
    rep = tuning.analyze(_rows(60), base, base)
    assert rep.enough
    assert rep.correlations["chat"][0] > 0.5 and rep.correlations["audio"][0] < -0.3
    assert rep.suggested["chat"] > base["chat"]          # predictive -> more weight
    assert rep.suggested["audio"] < base["audio"]        # anti-predictive -> less weight
    assert abs(rep.suggested["keywords"] - base["keywords"]) < 0.5 * base["keywords"]  # unrelated -> about the same
    assert rep.correlations["ai_score"][0] > 0.5         # the judge's score is reported too
    assert all(tuning.WEIGHT_FLOOR <= w <= tuning.WEIGHT_CEIL for w in rep.suggested.values())


def test_signal_missing_on_most_clips_keeps_its_weight():
    rows = _rows(30)
    for r in rows[5:]:
        del r["signals"]["chat"]  # e.g. a platform with no chat replay
    rep = tuning.analyze(rows, BASE, BASE)
    assert rep.suggested["chat"] == 1.5 and any("'chat'" in n for n in rep.notes)


def test_constant_data_does_not_produce_nan():
    rows = [{"signals": {"audio": 0.5, "chat": 0.5, "keywords": 0.5}, "ai_score": 7, "views": 100} for _ in range(20)]
    rep = tuning.analyze(rows, BASE, BASE)
    assert all(np.isfinite(r) for r, _ in rep.correlations.values())
    assert rep.suggested == BASE


@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def test_weights_file_round_trip_and_reset(tmp_data):
    assert tuning.current_weights() == tuning.base_weights()
    tuning.save_weights({"audio": 0.6, "chat": 2.4, "keywords": 0.8})
    assert tuning.current_weights() == {"audio": 0.6, "chat": 2.4, "keywords": 0.8}
    assert tuning.reset_weights() is True and tuning.reset_weights() is False
    assert tuning.current_weights() == tuning.base_weights()


@pytest.mark.parametrize("content", ["not json", "[]", '{"chat": "high", "audio": -1, "keywords": NaN}', '{"chat": true}'])
def test_bad_weights_file_is_ignored(tmp_data, content):
    (tmp_data / "weights.json").write_text(content, encoding="utf-8")
    assert tuning.current_weights() == tuning.base_weights()


def test_one_bad_value_does_not_discard_the_good_ones(tmp_data):
    # 10**400 is a valid JSON integer that overflows float(); only that key may be rejected
    (tmp_data / "weights.json").write_text('{"audio": 10e999, "chat": 10' + "0" * 400 + ', "keywords": 2.5}',
                                           encoding="utf-8")
    w = tuning.current_weights()
    assert w["keywords"] == 2.5
    assert w["audio"] == settings.w_audio and w["chat"] == settings.w_chat


def test_failed_save_leaves_the_existing_file_untouched(tmp_data, monkeypatch):
    tuning.save_weights({"audio": 0.6, "chat": 2.4, "keywords": 0.8})
    before = (tmp_data / "weights.json").read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tuning.os, "replace", boom)
    with pytest.raises(OSError):
        tuning.save_weights({"audio": 9.9, "chat": 9.9, "keywords": 9.9})
    assert (tmp_data / "weights.json").read_text(encoding="utf-8") == before
    assert not (tmp_data / "weights.json.tmp").exists()  # and no stray temp file


def test_saved_values_are_written_as_given(tmp_data):
    tuning.save_weights({"audio": 0.1234, "chat": 2.0, "keywords": 0.8, "ignored": 5})
    assert json.loads((tmp_data / "weights.json").read_text(encoding="utf-8")) == {
        "audio": 0.1234, "chat": 2.0, "keywords": 0.8}  # not re-rounded, unknown keys dropped


def test_partial_weights_file_overrides_only_what_it_names(tmp_data):
    (tmp_data / "weights.json").write_text(json.dumps({"chat": 2.0}), encoding="utf-8")
    w = tuning.current_weights()
    assert w["chat"] == 2.0 and w["audio"] == settings.w_audio


class _FakeYDL:
    def __init__(self, opts): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=False):
        return {"entries": [
            {"id": "aaaaaaaaaaa", "title": "live now", "live_status": "is_live"},
            {"id": "bbbbbbbbbbb", "title": "scheduled", "live_status": "is_upcoming"},
            {"id": "ccccccccccc", "title": "finished", "live_status": "was_live", "duration": 100},
            {"id": "ddddddddddd", "title": "upload", "live_status": "not_live", "duration": 50},
            {"id": "eeeeeeeeeee", "title": "no status field"},
        ]}


def test_live_and_upcoming_streams_are_skipped_until_finished(monkeypatch):
    import app.sources.ytdlp as mod
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", _FakeYDL)
    got = SOURCES["youtube"].list_videos("https://www.youtube.com/@x")
    assert [v.title for v in got] == ["finished", "upload", "no status field"]
