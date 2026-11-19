"""Stage 2: an AI judge scores shortlisted moments and writes titles, hooks and captions.

All provider-specific code lives behind the `Judge` interface, so swapping Gemini for
another model means writing one new class.
"""
import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import func, select
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from . import runtime
from .config import settings
from .db import Candidate, SessionLocal, Video

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
The moments quote spoken dialogue: when you reuse any of it, escape quotation marks inside strings.

Moments:
"""


class BadReply(ValueError):
    """The model answered, but nothing usable could be read from the answer."""


@dataclass
class Verdict:
    id: int
    score: float
    title: str
    hook: str
    caption: str
    reason: str


class VerdictOut(BaseModel):
    """The shape Gemini is asked to produce (enforced by the API, not just requested in the prompt)."""
    id: int
    score: float
    title: str
    hook: str
    caption: str
    reason: str


class Judge(ABC):
    @abstractmethod
    def judge(self, moments: list[dict]) -> list[Verdict]:
        """moments: [{id, transcript, chat, signals}] -> a Verdict for each moment it could judge.

        May return fewer than were given (the rest stay unscored and are retried later).
        Raises BadReply if nothing at all could be read.
        """


def _retryable(exc: BaseException) -> bool:
    """Overload, rate limits and dropped connections are worth another try; a bad key or a
    retired model name is not, and retrying would only delay the real error."""
    import httpx
    from google.genai import errors
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return exc.code in (408, 429)
    return isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError))


class GeminiJudge(Judge):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai  # imported lazily so tests don't need a key
        key = api_key or settings.gemini_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (put it in backend/.env)")
        self.client = genai.Client(api_key=key)
        self.model = model or settings.gemini_model

    def _generate(self, model: str, prompt) -> str:
        """One raw API call."""
        from google.genai import errors
        config = {"response_mime_type": "application/json", "temperature": 0.3}
        try:  # a response schema makes the API itself enforce valid, correctly-shaped JSON
            resp = self.client.models.generate_content(
                model=model, contents=prompt, config={**config, "response_schema": list[VerdictOut]})
        except errors.ClientError as e:
            if e.code != 400 or "schema" not in str(e).lower():
                raise
            resp = self.client.models.generate_content(model=model, contents=prompt, config=config)  # model can't do schemas
        return resp.text or ""

    def _call(self, model: str, prompt) -> str:
        """One answer from one model, retrying when it is overloaded or rate limited."""
        for attempt in Retrying(
            stop=stop_after_attempt(settings.judge_attempts_per_model),
            wait=wait_exponential(multiplier=settings.judge_retry_wait_s, max=30),
            retry=retry_if_exception(_retryable), reraise=True,
        ):
            with attempt:
                return self._generate(model, prompt)

    def _ask(self, prompt) -> str:
        """One answer, moving on to the next configured model when one stays overloaded."""
        last: BaseException | None = None
        for model in (self.model, *settings.gemini_fallback_models):
            try:
                return self._call(model, prompt)
            except Exception as e:
                if not _retryable(e):
                    raise  # e.g. an invalid key: another model won't help
                last = e
        raise last  # every model was unavailable

    def judge(self, moments: list[dict]) -> list[Verdict]:
        wanted = {m["id"] for m in moments}
        got: dict[int, Verdict] = {}
        bad: BadReply | None = None
        for _ in range(settings.judge_reply_attempts):
            remaining = [m for m in moments if m["id"] not in got]
            if not remaining:
                break
            try:  # only the moments still missing are asked for again, so progress is never thrown away
                for v in parse_verdicts(self._ask(build_contents(remaining))):
                    if v.id in wanted:
                        got.setdefault(v.id, v)
            except BadReply as e:
                bad = e
        if not got:
            raise bad or BadReply("the reply didn't contain any of the requested moments")
        return list(got.values())


AUDIO_NOTE = """
Some moments come with an audio recording of those 30 seconds, attached after the list and labelled
with the moment's id. When audio is attached, judge what you HEAR as well as what the text says:
delivery, excitement, laughter, clarity, music, or dead silence. If a moment's transcript is empty,
rely on the audio. A moment with no audio attached is judged from its text alone.
"""

MAX_AUDIO_BYTES = 2_000_000  # a 30 s snippet is ~180 KB; anything far larger is not ours, so is skipped


def load_audio(name: str | None) -> bytes | None:
    """The bytes of a moment's audio snippet, or None (missing, oversized, or outside the folder)."""
    if not name:
        return None
    root = settings.moments_dir.resolve()
    path = (root / name).resolve()
    try:
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def build_prompt(moments: list[dict], with_audio: bool = False) -> str:
    """Only the criteria for types actually present are included, keeping the prompt short."""
    # copy; keeps explicit types; audio_path is a file name on this machine, meaningless to the model
    moments = [{**{k: v for k, v in m.items() if k != "audio_path"}, "type": m.get("type") or "gameplay"}
               for m in moments]
    types = sorted({m["type"] for m in moments})
    criteria = "\n".join(f"- {TYPE_CRITERIA[t]}" for t in types if t in TYPE_CRITERIA)
    prompt = PROMPT.replace("{criteria}", criteria)
    if with_audio:
        prompt = prompt.replace("\nMoments:\n", AUDIO_NOTE + "\nMoments:\n")
    return prompt + json.dumps(moments, ensure_ascii=False)


def build_contents(moments: list[dict]):
    """What is sent to the model: the prompt alone, or the prompt followed by each moment's audio."""
    clips = [(m["id"], data) for m in moments if settings.judge_audio and (data := load_audio(m.get("audio_path")))]
    if not clips:
        return build_prompt(moments)
    from google.genai import types
    parts: list = [build_prompt(moments, with_audio=True)]
    for mid, data in clips:
        parts += [f"Audio of moment {mid}:", types.Part.from_bytes(data=data, mime_type="audio/mpeg")]
    return parts


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)
_OBJECT_START = re.compile(r"\{")  # any object may be a verdict; which keys it has first doesn't matter


def _salvage(text: str) -> list:
    """Every complete verdict object that can be read out of a damaged reply.

    A model occasionally writes an unescaped quote or gets cut off, which makes the whole text
    invalid JSON even though most of the objects in it are fine. Rather than discard them all,
    decode each object separately and skip only the broken ones.
    """
    decoder, found, resume = json.JSONDecoder(), [], 0
    for m in _OBJECT_START.finditer(text):
        if m.start() < resume:
            continue  # inside an object we already read
        try:
            obj, resume = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue  # this one is damaged: skip it, keep going
        if isinstance(obj, dict) and "id" in obj:
            found.append(obj)
        else:
            resume = m.start()  # not a verdict (e.g. a wrapper): look inside it
    return found


def _verdict(d: dict) -> Verdict:
    score = float(d["score"])
    if not math.isfinite(score):
        raise ValueError("score is not a number")
    return Verdict(
        id=int(d["id"]),
        score=max(0.0, min(10.0, score)),
        title=str(d.get("title", ""))[:200],
        hook=str(d.get("hook", ""))[:300],
        caption=str(d.get("caption", "")),
        reason=str(d.get("reason", "")),
    )


def parse_verdicts(raw: str) -> list[Verdict]:
    """Verdicts from a model reply, tolerating fences, wrapper objects and damaged JSON.

    Raises BadReply only if nothing usable is found. Items that are individually malformed are
    skipped, so one bad moment can't cost the rest.
    """
    text = (raw or "").strip()
    fence = _FENCE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
        if isinstance(data, dict):  # some models wrap the list in an object
            data = next((v for v in data.values() if isinstance(v, list)), [])
    except json.JSONDecodeError:
        data = _salvage(text)
    verdicts = []
    for d in data if isinstance(data, list) else []:
        try:
            verdicts.append(_verdict(d))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    if not verdicts:
        raise BadReply(f"couldn't read any verdicts from the reply: {text[:120]!r}")
    return verdicts


def judge_pending(judge: Judge | None = None, video_id: int | None = None, claim_token: str | None = None) -> int:
    """Judge candidates that have no AI score yet (only one video's, if video_id is given).

    Returns how many were scored. A batch the model answered unreadably is skipped and the
    others carry on (its moments stay unscored for a later run); an unavailable API stops the run.
    Raises only if every batch was unreadable.
    """
    judge = judge or GeminiJudge()
    done, unreadable = 0, None
    min_score = runtime.get("judge_min_score")  # editable in the dashboard; read once per run
    with SessionLocal() as db:
        q = select(Candidate).where(Candidate.ai_score.is_(None))
        if video_id is not None:
            q = q.where(Candidate.video_id == video_id)
        todo = list(db.scalars(q))
        for i in range(0, len(todo), settings.judge_batch_size):
            batch = todo[i: i + settings.judge_batch_size]
            by_id = {c.id: c for c in batch}
            payload = [{"id": c.id, "type": c.video.content_type or c.video.streamer.content_type,
                        "audio_path": c.audio_path, "transcript": c.transcript, "chat": c.chat_sample,
                        "signals": {k: round(v, 2) for k, v in c.signals.items()}} for c in batch]
            try:
                verdicts = judge.judge(payload)
            except BadReply as e:
                unreadable = e
                continue
            if claim_token is not None and not db.scalar(  # the model took a while: do we still own this analysis?
                    select(func.count(Video.id)).where(Video.id == video_id, Video.claim_token == claim_token)):
                db.rollback()
                break  # someone else took the video over: write none of this
            for v in verdicts:
                c = by_id.get(v.id)
                if not c:
                    continue
                c.ai_score, c.title, c.hook, c.caption, c.ai_reason = v.score, v.title, v.hook, v.caption, v.reason
                if v.score < min_score:
                    c.review_status = "rejected"
                done += 1
            db.commit()  # commit per batch so a later failure keeps earlier work
    if done == 0 and unreadable:
        raise unreadable
    return done
