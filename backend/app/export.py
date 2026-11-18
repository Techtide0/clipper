"""Stage 4 export: one ready-to-upload folder per platform, with the right video and post text.

Platform limits live in PLATFORMS so they're easy to update when the platforms change them.
Durations are deliberately conservative (60s for vertical) so a clip is accepted everywhere.
"""
import json
import re
import shutil
from dataclasses import dataclass

from .config import settings
from .db import Candidate, SessionLocal


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    orientation: str        # "vertical" (9:16) or "wide" (16:9)
    max_seconds: int
    title_max: int          # 0 = platform has no separate title
    text_max: int           # description / caption length limit
    max_hashtags: int
    extra_tags: tuple[str, ...] = ()


PLATFORMS = [
    Platform("youtube_shorts", "YouTube Shorts", "vertical", 60, 100, 5000, 15, ("#Shorts",)),
    Platform("youtube_video", "YouTube (16:9)", "wide", 12 * 3600, 100, 5000, 15),
    Platform("instagram_reels", "Instagram Reels", "vertical", 60, 0, 2200, 30),
    Platform("tiktok", "TikTok", "vertical", 60, 0, 2200, 10),
    Platform("facebook_reels", "Facebook Reels", "vertical", 60, 0, 2200, 30),
]

_TAG = re.compile(r"#\w+")
# only a hashtag block at the very end counts as "the tags"; inline text like "#1 pick" is left alone
_TRAILING_TAGS = re.compile(r"(?:^|\s)((?:#\w+\s*)+)$")


def split_caption(caption: str) -> tuple[str, list[str]]:
    """Separate the sentence(s) from the trailing hashtags the AI appended."""
    m = _TRAILING_TAGS.search(caption)
    if m:
        tags, body = list(dict.fromkeys(_TAG.findall(m.group(1)))), caption[: m.start()]
    else:
        tags, body = [], caption
    return re.sub(r"\s+", " ", body).strip(), tags


def limit_hashtags(text: str, n: int) -> str:
    """Keep only the first n hashtags anywhere in the text; drop the rest."""
    count = 0

    def keep(m: re.Match) -> str:
        nonlocal count
        count += 1
        return m.group(0) if count <= n else ""

    out = _TAG.sub(keep, text)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def build_post(p: Platform, title: str, hook: str, caption: str) -> dict:
    """Post text trimmed to the platform's limits. The hashtag limit covers all text in the post."""
    body, tags = split_caption(caption)
    tag_line = " ".join(dict.fromkeys([*p.extra_tags, *tags]))

    def clip(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"

    if p.title_max:
        post_title = clip(title or hook, p.title_max)
        text = "\n\n".join(x for x in (body, tag_line) if x)
    else:  # no separate title: lead with the hook inside the caption
        post_title = ""
        lead = hook or title
        text = "\n\n".join(x for x in (lead, body if body != lead else "", tag_line) if x)
    text = clip(limit_hashtags(text, p.max_hashtags), p.text_max)
    return {"title": post_title, "text": text}


def _build_bundle(c: Candidate, dest, duration: float) -> dict:
    manifest = {"candidate_id": c.id, "duration_s": round(duration, 1), "platforms": []}
    for p in PLATFORMS:
        src = settings.clips_dir / (c.vertical_path if p.orientation == "vertical" else c.wide_path)
        folder = dest / p.key
        folder.mkdir(parents=True)
        # a real copy, not a hard link: re-rendering rewrites the source file in place and
        # a link would silently change an already-exported video
        shutil.copy2(src, folder / f"clip_{c.id}.mp4")
        post = build_post(p, c.title or "", c.hook or "", c.caption or "")
        lines = [f"TITLE: {post['title']}\n\n"] if post["title"] else []
        (folder / "post.txt").write_text("".join(lines) + post["text"] + "\n", encoding="utf-8")
        warning = f"clip is {duration:.0f}s, over the {p.max_seconds}s limit" if duration > p.max_seconds else None
        manifest["platforms"].append({
            "key": p.key, "label": p.label, "video": f"{c.id}/{p.key}/clip_{c.id}.mp4",
            "title": post["title"], "text": post["text"], "warning": warning,
        })
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def export_candidate(candidate_id: int) -> dict:
    """Build data/exports/<id>/<platform>/ folders. Returns a manifest describing them.

    The bundle is built in a staging folder and swapped in only when complete, so a failed
    re-export never destroys the previous good one.
    """
    with SessionLocal() as db:
        c = db.get(Candidate, candidate_id)
        if c is None:
            raise LookupError("no such candidate")
        if c.review_status != "approved" or c.render_status != "done":
            raise ValueError("clip must be approved and rendered before export")
        duration = c.end_s - c.start_s
        root = settings.exports_dir / str(c.id)
        stage = settings.exports_dir / f".{c.id}.staging"
        old = settings.exports_dir / f".{c.id}.old"
        for leftover in (stage, old):
            shutil.rmtree(leftover, ignore_errors=True)
        try:
            manifest = _build_bundle(c, stage, duration)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        if root.exists():
            root.rename(old)
        stage.rename(root)
        shutil.rmtree(old, ignore_errors=True)
        return manifest
