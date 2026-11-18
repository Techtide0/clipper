import json
from pathlib import Path

import numpy as np

from .audio import baseline_zscore


def parse_live_chat_messages(path: Path) -> list[tuple[float, str]]:
    """(seconds from video start, text) for each chat message in a yt-dlp live_chat.json file."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            action = json.loads(line)["replayChatItemAction"]
            item = action["actions"][0]["addChatItemAction"]["item"]
            renderer = next(iter(item.values()))
            runs = renderer.get("message", {}).get("runs", [])
            text = "".join(r.get("text", r.get("emoji", {}).get("shortcuts", [""])[0]) for r in runs)
            out.append((int(action["videoOffsetTimeMsec"]) / 1000, text))
        except (KeyError, ValueError, IndexError, TypeError, StopIteration, AttributeError):
            continue
    return out


def parse_live_chat(path: Path) -> list[float]:
    """Message timestamps only."""
    return [t for t, _ in parse_live_chat_messages(path)]


def chat_score(times: list[float], duration_s: int) -> np.ndarray:
    """Per-second score: messages per second, smoothed, z-scored against the local baseline."""
    counts = np.zeros(max(duration_s, 1))
    for t in times:
        if 0 <= t < len(counts):
            counts[int(t)] += 1
    smooth = np.convolve(counts, np.ones(5) / 5, mode="same")
    return baseline_zscore(smooth)
