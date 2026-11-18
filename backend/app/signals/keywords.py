import re
from pathlib import Path

import numpy as np

HYPE = [
    "oh my god", "no way", "insane", "crazy", "let's go", "lets go", "holy", "what the",
    "are you kidding", "unbelievable", "wow", "clip that", "did you see", "scream",
]
GTA = ["trailer", "gta 6", "gta vi", "vice city", "lucia", "jason", "leonida", "release date", "leak"]

_TS = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->")


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    """(start_seconds, text) cues from a WebVTT file."""
    cues, start = [], None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = _TS.match(line)
        if m:
            h, mi, s, ms = map(int, m.groups())
            start = h * 3600 + mi * 60 + s + ms / 1000
        elif start is not None and line.strip() and "-->" not in line:
            cues.append((start, re.sub(r"<[^>]+>", "", line).strip().lower()))
    return cues


def keyword_score(cues: list[tuple[float, str]], duration_s: int) -> np.ndarray:
    score = np.zeros(max(duration_s, 1))
    for t, text in cues:
        hits = sum(k in text for k in HYPE) + 0.5 * sum(k in text for k in GTA)
        if hits and 0 <= t < len(score):
            score[int(t)] += hits
    return np.convolve(score, np.ones(5), mode="same")
