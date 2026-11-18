from dataclasses import dataclass, field

import numpy as np


@dataclass
class Moment:
    start_s: float
    end_s: float
    score: float
    signals: dict = field(default_factory=dict)


def _norm(x: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n)
    out[: min(n, len(x))] = x[:n]
    peak = out.max()
    return out / peak if peak > 0 else out


def shortlist(
    signals: dict[str, np.ndarray],
    weights: dict[str, float],
    duration_s: int,
    top_n: int = 25,
    window_s: int = 30,
    min_gap_s: int = 45,
) -> list[Moment]:
    """Combine per-second signals, then greedily pick the best well-separated peaks."""
    norm = {k: _norm(v, duration_s) for k, v in signals.items()}
    total = sum(norm[k] * weights.get(k, 1.0) for k in norm)
    # a moment's score is the average over the window, so sustained excitement beats a single blip
    win = np.convolve(total, np.ones(window_s), mode="same") / window_s
    taken: list[int] = []
    out: list[Moment] = []
    for idx in np.argsort(win)[::-1]:
        if len(out) >= top_n or win[idx] <= 0:
            break
        if any(abs(idx - t) < min_gap_s for t in taken):
            continue
        taken.append(int(idx))
        start = max(0.0, idx - window_s * 0.6)  # lead-in: the reaction follows the event
        lo, hi = max(0, idx - window_s // 2), idx + window_s // 2
        out.append(Moment(
            start_s=float(start),
            end_s=float(min(duration_s, start + window_s)),
            score=float(win[idx]),
            signals={k: float(norm[k][lo:hi].mean()) for k in norm},
        ))
    return out
