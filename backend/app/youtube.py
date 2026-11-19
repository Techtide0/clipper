"""Post a cut clip to YouTube, and read its views back so the scoring can learn without typing.

Setup is a one-time job for the owner (a Google Cloud project and an OAuth client file; see
docs/youtube-setup.md). Things worth knowing, all surfaced to the owner in the dashboard:
- Uploads default to **private**; the owner checks them in YouTube Studio and publishes. Google also
  forces uploads from a new, unaudited API project to private whatever is requested.
- YouTube gives an API project a daily allowance; an upload uses a big share of it, so only a few
  uploads fit in a day. When it runs out the clip is marked failed with a plain explanation.
- While the Google project is in "Testing" mode, sign-ins expire after about a week.
"""
import json
import threading
from pathlib import Path

from sqlalchemy import select, update

from . import runtime
from .config import settings
from .db import Candidate, SessionLocal
from .export import PLATFORMS, build_post

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
GAMING = "20"  # YouTube's "Gaming" category
SHORTS = next(p for p in PLATFORMS if p.key == "youtube_shorts")

SETUP_STEPS = [
    "Go to https://console.cloud.google.com and create a project (any name).",
    "In that project, enable the \"YouTube Data API v3\".",
    "Open \"OAuth consent screen\": choose External, fill the required fields, and add your own Google account as a Test user.",
    "Open \"Credentials\" > Create credentials > OAuth client ID > Application type \"Desktop app\".",
    "Download the JSON file and save it exactly as backend\\data\\youtube\\client_secret.json",
    "Come back here and press Connect YouTube, then sign in in the window that opens.",
]


class NotConnected(Exception):
    """YouTube isn't set up or the saved sign-in no longer works."""


_UPLOAD_LOCK = threading.Lock()  # one upload at a time
_CONNECTING = threading.Event()
_last_connect_error: str | None = None


def _dir() -> Path:
    d = settings.data_dir / "youtube"
    d.mkdir(parents=True, exist_ok=True)
    return d


def secrets_path() -> Path:
    return _dir() / "client_secret.json"


def token_path() -> Path:
    return _dir() / "token.json"


def _account_path() -> Path:
    return _dir() / "account.json"


def status() -> dict:
    account = {}
    try:
        account = json.loads(_account_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"configured": secrets_path().is_file(), "connected": token_path().is_file(),
            "connecting": _CONNECTING.is_set(), "channel": account.get("title"),
            "connect_error": _last_connect_error, "privacy": runtime.get("youtube_privacy"),
            "setup_steps": SETUP_STEPS, "secrets_file": str(secrets_path())}


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not token_path().is_file():
        raise NotConnected("YouTube isn't connected yet: open Settings & health and press Connect YouTube")
    creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise NotConnected("the YouTube sign-in has expired: connect again in Settings & health")
        try:
            creds.refresh(Request())
        except Exception as e:
            raise NotConnected("the YouTube sign-in has expired: connect again in Settings & health") from e
        token_path().write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_credentials(), cache_discovery=False)


def connect() -> None:
    """Open Google's sign-in in the browser and save the result. Blocks until the owner finishes (or
    5 minutes pass), so call it from a background thread. Any failure is kept for the dashboard to show."""
    global _last_connect_error
    from google_auth_oauthlib.flow import InstalledAppFlow
    _last_connect_error = None
    if not secrets_path().is_file():
        _last_connect_error = f"the Google client file is missing: save it as {secrets_path()}"
        return
    _CONNECTING.set()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path()), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300)
        token_path().write_text(creds.to_json(), encoding="utf-8")
        try:  # remember which channel this is, to show it
            items = _service().channels().list(part="snippet", mine=True).execute().get("items", [])
            _account_path().write_text(json.dumps({"title": items[0]["snippet"]["title"] if items else None}), encoding="utf-8")
        except Exception:
            pass  # cosmetic only
    except Exception as e:
        _last_connect_error = f"sign-in didn't finish: {type(e).__name__}: {e}"[:300]
    finally:
        _CONNECTING.clear()


def start_connect() -> bool:
    """Begin sign-in in the background. False if one is already open."""
    if _CONNECTING.is_set():
        return False
    threading.Thread(target=connect, name="clipper-youtube-connect", daemon=True).start()
    return True


def disconnect() -> None:
    for p in (token_path(), _account_path()):
        p.unlink(missing_ok=True)


def _friendly(e: Exception) -> str:
    """YouTube's errors are JSON blobs; say what actually happened."""
    reason = ""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(e, HttpError):
            body = json.loads(e.content.decode("utf-8", "ignore"))
            reason = (body.get("error", {}).get("errors") or [{}])[0].get("reason", "") or body.get("error", {}).get("status", "")
    except Exception:
        pass
    known = {
        "quotaExceeded": "YouTube's daily allowance for this app is used up; try again tomorrow",
        "dailyLimitExceeded": "YouTube's daily allowance for this app is used up; try again tomorrow",
        "uploadLimitExceeded": "this YouTube account has hit its upload limit; try again later",
        "forbidden": "YouTube refused the upload (check the account is allowed to upload and the app is set up for it)",
        "insufficientPermissions": "the YouTube sign-in doesn't allow uploading: disconnect and connect again",
        "authError": "the YouTube sign-in has expired: connect again in Settings & health",
        "invalid_grant": "the YouTube sign-in has expired: connect again in Settings & health",
    }
    return known.get(reason) or f"{type(e).__name__}: {e}"[:300]


def _set(cid: int, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(Candidate).where(Candidate.id == cid).values(**values))
        db.commit()


def claim_upload(cid: int) -> bool:
    """Atomically start an upload. Only an approved, cut clip that isn't already up or uploading qualifies."""
    with SessionLocal() as db:
        done = db.execute(update(Candidate).where(
            Candidate.id == cid, Candidate.review_status == "approved", Candidate.render_status == "done",
            Candidate.yt_status.in_(("none", "failed"))).values(yt_status="uploading", yt_progress=0, yt_error=None))
        db.commit()
        return done.rowcount == 1


def upload(cid: int) -> None:
    """Upload the clip's vertical (Shorts-shaped) video. Run after claim_upload succeeded."""
    with SessionLocal() as db:
        c = db.get(Candidate, cid)
        stored, title, hook, caption = c.vertical_path, c.title or "", c.hook or "", c.caption or ""
    try:
        root = settings.clips_dir.resolve()
        path = (root / (stored or "")).resolve()
        if not stored or not path.is_relative_to(root) or not path.is_file():
            raise FileNotFoundError("the clip's file is missing; cut it again")
        post = build_post(SHORTS, title, hook, caption)  # the same title/description/hashtag rules as the export
        tags = [t.lstrip("#") for t in post["text"].split() if t.startswith("#")][:15]
        body = {"snippet": {"title": post["title"] or f"Clip {cid}", "description": post["text"],
                            "categoryId": GAMING, "tags": tags},
                "status": {"privacyStatus": runtime.get("youtube_privacy"), "selfDeclaredMadeForKids": False}}
        from googleapiclient.http import MediaFileUpload
        with _UPLOAD_LOCK:
            request = _service().videos().insert(
                part="snippet,status", body=body,
                media_body=MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
            response = None
            while response is None:
                progress, response = request.next_chunk()
                if progress:
                    _set(cid, yt_progress=min(99, int(progress.progress() * 100)))
        _set(cid, yt_status="uploaded", yt_video_id=response["id"], yt_progress=100, yt_error=None)
    except NotConnected as e:
        _set(cid, yt_status="failed", yt_progress=None, yt_error=str(e)[:500])
    except Exception as e:
        _set(cid, yt_status="failed", yt_progress=None, yt_error=_friendly(e)[:500])


def recover_uploads() -> int:
    """An upload left running by a process that died can't finish: mark it failed so it can be retried."""
    with SessionLocal() as db:
        done = db.execute(update(Candidate).where(Candidate.yt_status == "uploading").values(
            yt_status="failed", yt_progress=None, yt_error="interrupted by a restart; press Post again"))
        db.commit()
        return done.rowcount


def refresh_stats() -> int:
    """Read views and likes of every uploaded clip from YouTube and save them. Returns how many were updated.

    Raises NotConnected if YouTube isn't connected. (Likes can be hidden by the owner; then only views change.)
    """
    with SessionLocal() as db:
        rows = [(c.id, c.yt_video_id) for c in db.scalars(select(Candidate).where(Candidate.yt_video_id.is_not(None)))]
    if not rows:
        return 0
    yt, updated = _service(), 0
    for i in range(0, len(rows), 50):  # the API takes up to 50 ids per call
        chunk = rows[i:i + 50]
        items = yt.videos().list(part="statistics", id=",".join(v for _, v in chunk)).execute().get("items", [])
        stats = {it["id"]: it.get("statistics", {}) for it in items}
        for cid, vid in chunk:
            s = stats.get(vid)
            if not s or "viewCount" not in s:
                continue
            values = {"views": int(s["viewCount"])}
            if "likeCount" in s:
                values["likes"] = int(s["likeCount"])
            _set(cid, **values)
            updated += 1
    return updated
