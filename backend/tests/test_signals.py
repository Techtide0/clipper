import numpy as np
import soundfile as sf

from app.signals.audio import baseline_zscore, loudness_per_second
from app.signals.chat import chat_score
from app.signals.keywords import keyword_score
from app.signals.shortlist import shortlist


def test_loud_burst_detected(tmp_path):
    sr = 16000
    audio = np.random.randn(sr * 120).astype("float32") * 0.01
    audio[sr * 60: sr * 65] *= 40  # shout at 60-65s
    f = tmp_path / "a.wav"
    sf.write(f, audio, sr)
    z = baseline_zscore(loudness_per_second(f), window=60)
    assert 60 <= int(np.argmax(z)) <= 65


def test_chat_spike():
    times = [float(t) for t in range(0, 200, 5)] + [100 + i * 0.1 for i in range(100)]
    s = chat_score(times, 200)
    assert 95 <= int(np.argmax(s)) <= 110


def test_keywords():
    s = keyword_score([(30.0, "oh my god no way")], 60)
    assert s[30] > 0 and s[50] == 0


def test_shortlist_separates_peaks():
    a = np.zeros(600)
    a[100] = 5
    a[105] = 4
    a[400] = 3
    moments = shortlist({"audio": a}, {"audio": 1}, 600, top_n=5, window_s=30, min_gap_s=45)
    starts = sorted(m.start_s for m in moments)
    assert len(moments) == 2
    assert starts[0] < 120 < 350 < starts[1]
