"""Learn which signals predict real-world performance.

You enter views for the clips you posted. For each signal (loudness, chat, keywords) and for the
AI score, we measure how well it ranks clips the way the views did (Spearman rank correlation),
then suggest scoring weights. Nothing changes unless you apply the suggestion, and nothing is
suggested until there is enough data for the numbers to mean anything.

Caveats worth knowing: views depend on the platform, the post time and how long the clip has
been up, so enter them after a similar waiting period for every clip. Correlation is not proof
a signal causes views, and a handful of clips is noisy, hence the minimums below.
"""
import json
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import spearmanr
from sqlalchemy import select

from .config import settings
from .db import Candidate, SessionLocal

SIGNALS = ("audio", "chat", "keywords")
MIN_CLIPS = 15          # clips with views before anything is suggested
MIN_PER_SIGNAL = 10     # clips that have a given signal before that signal is judged
WEIGHT_FLOOR, WEIGHT_CEIL = 0.1, 3.0


@dataclass
class Report:
    n: int = 0
    enough: bool = False
    correlations: dict[str, tuple[float, int]] = field(default_factory=dict)  # name -> (rho, n)
    current: dict[str, float] = field(default_factory=dict)
    suggested: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def base_weights() -> dict[str, float]:
    return {"audio": settings.w_audio, "chat": settings.w_chat, "keywords": settings.w_keywords}


def current_weights() -> dict[str, float]:
    """Configured weights, overridden by an applied tuning file. A bad file is ignored, not fatal."""
    weights = base_weights()
    try:
        data = json.loads(settings.weights_path.read_text(encoding="utf-8"))
        for k in SIGNALS:
            v = data.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
                weights[k] = float(v)
    except (OSError, ValueError, AttributeError):
        pass
    return weights


def save_weights(weights: dict[str, float]) -> None:
    clean = {k: round(float(weights[k]), 2) for k in SIGNALS if k in weights}
    settings.weights_path.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def reset_weights() -> bool:
    try:
        settings.weights_path.unlink()
        return True
    except FileNotFoundError:
        return False


def _rho(x: list[float], y: list[float]) -> float:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return 0.0  # a constant column carries no information (and would give NaN)
    r = spearmanr(x, y).statistic
    return 0.0 if r is None or not math.isfinite(r) else float(r)


def analyze(rows: list[dict], weights: dict[str, float] | None = None) -> Report:
    """rows: [{"signals": {name: 0..1}, "ai_score": float|None, "views": int}]"""
    weights = weights or current_weights()
    rep = Report(n=len(rows), current=dict(weights), suggested=dict(weights))
    if rep.n < MIN_CLIPS:
        rep.notes.append(f"Only {rep.n} clips have views; need at least {MIN_CLIPS} before suggesting changes.")
        return rep
    rep.enough = True
    for name in (*SIGNALS, "ai_score"):
        pairs = [((r["signals"] or {}).get(name) if name != "ai_score" else r["ai_score"], r["views"])
                 for r in rows]
        pairs = [(x, v) for x, v in pairs if x is not None]
        rho = _rho([p[0] for p in pairs], [p[1] for p in pairs])
        rep.correlations[name] = (rho, len(pairs))
        if name in SIGNALS:
            if len(pairs) < MIN_PER_SIGNAL:
                rep.notes.append(f"'{name}' is present on only {len(pairs)} clips; weight left unchanged.")
            else:
                # stronger positive correlation -> more weight; negative -> less, never below the floor
                rep.suggested[name] = round(min(WEIGHT_CEIL, max(WEIGHT_FLOOR, weights[name] * (1 + rho))), 2)
    return rep


def collect_rows() -> list[dict]:
    with SessionLocal() as db:
        return [{"signals": c.signals, "ai_score": c.ai_score, "views": c.views}
                for c in db.scalars(select(Candidate).where(Candidate.views.is_not(None)))]
