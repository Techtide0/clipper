import json
from pathlib import Path

import numpy as np

from .audio import baseline_zscore


def parse_live_chat(path: Path) -> list[float]:
    """Message timestamps (seconds from video start) from a yt-dlp live_chat.json file."""
    times = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            action = json.loads(line)["replayChatItemAction"]
            if "addChatItemAction" in json.dumps(action["actions"][0]):
                times.append(int(action["videoOffsetTimeMsec"]) / 1000)
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return times


def chat_score(times: list[float], duration_s: int) -> np.ndarray:
    """Per-second score: messages per second, smoothed, z-scored against the local baseline."""
    counts = np.zeros(max(duration_s, 1))
    for t in times:
        if 0 <= t < len(counts):
            counts[int(t)] += 1
    smooth = np.convolve(counts, np.ones(5) / 5, mode="same")
    return baseline_zscore(smooth)
