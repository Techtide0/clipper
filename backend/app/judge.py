"""Stage 2: an AI judge scores shortlisted moments and writes titles, hooks and captions.

All provider-specific code lives behind the `Judge` interface, so swapping Gemini for
another model means writing one new class.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import select
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import settings
from .db import Candidate, SessionLocal

TYPE_CRITERIA = {
    "gameplay": "Gameplay: surprising, funny or skillful in-game moments and live reactions score high.",
    "news": "News: a clear, new, specific piece of information (announcement, leak, confirmed detail) "
            "explained so a newcomer understands it scores high; rumours with no substance, repeats "
            "of old news and vague speculation score low.",
    "commentary": "Commentary: a clear, well-argued take or breakdown with a point of view and a payoff "
                  "(a conclusion, a reveal, a strong opinion) scores high; meandering, hedging and "
                  "plain recap score low.",
}

PROMPT = """You pick highlight clips from GTA VI (Grand Theft Auto 6) content for short-form video.
For each moment below, judge how well it would work as a standalone 20-30 second clip.
Each moment has a "type"; judge it by that type's criteria:
{criteria}
In every type, dead air, sponsor reads and setup with no payoff score low. Score 0-10.

Return a JSON list with one object per moment, in the same order, each with:
  id (int, copied from input), score (number 0-10), title (max 70 chars, no clickbait lies),
  hook (the first line a viewer should see/hear, max 100 chars),
  caption (1-2 sentences plus 3-5 hashtags), reason (one sentence on why you scored it so).

Moments:
"""


@dataclass
class Verdict:
    id: int
    score: float
    title: str
    hook: str
    caption: str
    reason: str


class Judge(ABC):
    @abstractmethod
    def judge(self, moments: list[dict]) -> list[Verdict]:
        """moments: [{id, transcript, chat, signals}] -> one Verdict per moment."""


class GeminiJudge(Judge):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai  # imported lazily so tests don't need a key
        key = api_key or settings.gemini_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (put it in backend/.env)")
        self.client = genai.Client(api_key=key)
        self.model = model or settings.gemini_model

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=5, max=60), reraise=True)
    def _call(self, prompt: str) -> str:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0.3},
        )
        return resp.text

    def judge(self, moments: list[dict]) -> list[Verdict]:
        return parse_verdicts(self._call(build_prompt(moments)))


def build_prompt(moments: list[dict]) -> str:
    """Only the criteria for types actually present are included, keeping the prompt short."""
    moments = [{**m, "type": m.get("type") or "gameplay"} for m in moments]  # copy; keeps explicit types
    types = sorted({m["type"] for m in moments})
    criteria = "\n".join(f"- {TYPE_CRITERIA[t]}" for t in types if t in TYPE_CRITERIA)
    return PROMPT.replace("{criteria}", criteria) + json.dumps(moments, ensure_ascii=False)


def parse_verdicts(raw: str) -> list[Verdict]:
    data = json.loads(raw)
    if isinstance(data, dict):  # some models wrap the list in an object
        data = next(v for v in data.values() if isinstance(v, list))
    return [
        Verdict(
            id=int(d["id"]),
            score=max(0.0, min(10.0, float(d["score"]))),
            title=str(d.get("title", ""))[:200],
            hook=str(d.get("hook", ""))[:300],
            caption=str(d.get("caption", "")),
            reason=str(d.get("reason", "")),
        )
        for d in data
    ]


def judge_pending(judge: Judge | None = None, video_id: int | None = None) -> int:
    """Judge candidates that have no AI score yet (only one video's, if video_id is given).

    Returns how many were scored.
    """
    judge = judge or GeminiJudge()
    done = 0
    with SessionLocal() as db:
        q = select(Candidate).where(Candidate.ai_score.is_(None))
        if video_id is not None:
            q = q.where(Candidate.video_id == video_id)
        todo = list(db.scalars(q))
        for i in range(0, len(todo), settings.judge_batch_size):
            batch = todo[i: i + settings.judge_batch_size]
            by_id = {c.id: c for c in batch}
            payload = [{"id": c.id, "type": c.video.content_type or c.video.streamer.content_type,
                        "transcript": c.transcript, "chat": c.chat_sample,
                        "signals": {k: round(v, 2) for k, v in c.signals.items()}} for c in batch]
            for v in judge.judge(payload):
                c = by_id.get(v.id)
                if not c:
                    continue
                c.ai_score, c.title, c.hook, c.caption, c.ai_reason = v.score, v.title, v.hook, v.caption, v.reason
                if v.score < settings.judge_min_score:
                    c.review_status = "rejected"
                done += 1
            db.commit()  # commit per batch so a rate-limit failure keeps earlier work
    return done
