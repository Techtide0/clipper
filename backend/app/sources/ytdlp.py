"""Shared yt-dlp logic. YouTube, Twitch and Kick differ only in a few small hooks."""
import json
import multiprocessing
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from .base import Downloaded, Source, VideoInfo, VideoMeta


def _is_network_failure(err: BaseException) -> bool:
    """True if a yt-dlp failure was our connection (DNS, timeout, refused, proxy) and not the link.

    yt-dlp wraps the real cause in extractor-level errors, sometimes several layers deep, so this
    walks the whole cause chain. An HTTPError anywhere in it means the site answered and the link
    is the problem (a 404, say), so that wins and the failure is not treated as a network one.
    """
    from yt_dlp.networking.exceptions import HTTPError, TransportError
    seen: list[BaseException] = []
    stack = [err.exc_info[1] if getattr(err, "exc_info", None) else None, err]
    while stack and len(seen) < 16:
        cur = stack.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        stack += [getattr(cur, "cause", None), cur.__cause__, cur.__context__]
    if any(isinstance(e, HTTPError) for e in seen):
        return False
    return any(isinstance(e, (TransportError, OSError)) for e in seen)


def _valid_http_url(u: object) -> bool:
    """An http(s) URL with a real host (not just a string that starts with 'http')."""
    if not isinstance(u, str):
        return False
    try:
        p = urlparse(u)
        p.port  # raises ValueError for a malformed explicit port such as ':abc' or ':99999'
        return p.scheme in ("http", "https") and bool(p.hostname)
    except ValueError:
        return False


class YtDlpSource(Source):
    # Prepended to platform ids so ids from different sites can never collide in the database
    # or in the raw-download folder. Empty for YouTube, whose ids are already distinctive.
    id_prefix = ""
    # yt-dlp extractor-name patterns this platform may use (see _opts); empty = no restriction
    extractor_names: tuple[str, ...] = ()

    def videos_url(self, channel_url: str) -> str:
        """The page that lists a channel's past broadcasts."""
        return channel_url

    def extra_download_opts(self) -> dict:
        return {}

    def fetch_chat(self, video: VideoInfo, out_dir: Path) -> list[tuple[float, str]] | None:
        return None

    def find_captions(self, video: VideoInfo, out_dir: Path) -> Path | None:
        return next(iter(out_dir.glob(f"{video.platform_id}*.vtt")), None)

    def entry_url(self, e: dict) -> str | None:
        """The video URL for a flat listing entry, or None to skip it.

        Only valid absolute http(s) URLs are kept: they are stored, opened in the dashboard and
        fed back to yt-dlp, so an opaque id or relative path would break all three.
        """
        return next((u for u in (e.get("url"), e.get("webpage_url")) if _valid_http_url(u)), None)

    def channel_urls(self, info: dict) -> tuple[str, ...]:
        """Every valid channel URL the metadata gives, best first."""
        return tuple(u for u in (info.get("channel_url"), info.get("uploader_url")) if _valid_http_url(u))

    def _opts(self, **opts) -> dict:
        """yt-dlp options common to every call this source makes."""
        if self.extractor_names:
            # Only this platform's extractors may run. Without it, a URL on an allowed host that
            # redirects elsewhere falls through to yt-dlp's generic extractor, which follows the
            # redirect and accepts whatever it finds on the other host.
            opts["allowed_extractors"] = list(self.extractor_names)
        return opts

    def fetch_video(self, url: str) -> VideoMeta:
        opts = self._opts(
            quiet=True, noplaylist=True, skip_download=True,
            # A channel or playlist link must be recognised and refused quickly. Without these,
            # yt-dlp fully loads every video in it (thousands, for a big channel) before answering.
            extract_flat="in_playlist", playlistend=1,
            socket_timeout=20,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            if _is_network_failure(e):
                # our connection, not their link: callers treat this as "try again", not "bad link"
                raise ConnectionError("couldn't reach the site (network problem); try again in a moment") from e
            # yt-dlp's message is verbose; keep the first line, which says what went wrong
            raise ValueError(f"couldn't read that link: {str(e).splitlines()[0][:200]}") from e
        if not info:
            raise ValueError("couldn't read that link")
        if info.get("entries") is not None or info.get("_type") in ("playlist", "multi_video"):
            raise ValueError("that link is a channel or playlist, not a single video "
                             "(to track a whole channel, add it as a streamer instead)")
        if info.get("live_status") in ("is_live", "is_upcoming"):
            raise ValueError("that stream hasn't finished yet; try again once it has ended")
        vid, page = info.get("id"), info.get("webpage_url")
        if not vid or not _valid_http_url(page):
            raise ValueError("couldn't find a usable video at that link")
        return VideoMeta(
            video=VideoInfo(
                platform_id=f"{self.id_prefix}{vid}",
                title=info.get("title") or str(vid),
                url=page,
                duration_s=int(info["duration"]) if info.get("duration") else None,
            ),
            channel_name=info.get("uploader") or info.get("channel"),
            channel_urls=self.channel_urls(info),
        )

    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]:
        opts = self._opts(extract_flat=True, playlistend=limit, quiet=True, skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(self.videos_url(channel_url), download=False)
        out = []
        for e in info.get("entries", []) or []:
            if not e or not e.get("id"):
                continue
            # a stream that hasn't started or hasn't ended can't be downloaded as a finished
            # recording; it is picked up by a later scan once it is over
            if e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            url = self.entry_url(e)
            if not url:
                continue
            out.append(VideoInfo(
                platform_id=f"{self.id_prefix}{e['id']}",
                title=e.get("title") or str(e["id"]),
                url=url,
                duration_s=int(e["duration"]) if e.get("duration") else None,
            ))
        return out

    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded:
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = self._opts(
            format="bestaudio/best",
            outtmpl=str(out_dir / f"{video.platform_id}.%(ext)s"),
            quiet=True,
            noprogress=True,
            # 16 kHz mono wav: tiny and all the signal analysis needs
            postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
            postprocessor_args={"extractaudio": ["-ac", "1", "-ar", "16000"]},
            **self.extra_download_opts(),
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video.url])
        audio = out_dir / f"{video.platform_id}.wav"
        if not audio.exists():
            raise RuntimeError("audio download failed")
        # chat and captions are optional extras: a failure in either must never lose the audio
        try:
            chat = self.fetch_chat(video, out_dir)
        except Exception:
            chat = None
        try:
            captions = self.find_captions(video, out_dir)
        except Exception:
            captions = None
        return Downloaded(audio=audio, chat_messages=chat, captions=captions)


# --- chat replay through the chat-downloader library, in a killable child process -----------

def _chat_worker(url: str, out_path: str) -> None:
    """Runs in a child process: stream messages to a file as JSON lines."""
    from chat_downloader import ChatDownloader
    chat = ChatDownloader().get_chat(url, message_types=["text_message"])
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            for m in chat:
                t = m.get("time_in_seconds")
                if t is not None and t >= 0:
                    f.write(json.dumps([float(t), str(m.get("message") or "")]) + "\n")
                    f.flush()
    finally:
        close = getattr(chat, "close", None)
        if close:
            close()


def _worker_entry(target, args: tuple, out_path: str, finished_at) -> None:
    """Child-process wrapper: run the worker, then record when it finished (only on success)."""
    target(*args, out_path)
    finished_at.value = time.monotonic()


def run_chat_process(target, args: tuple, timeout_s: float, idle_s: float) -> list[tuple[float, str]] | None:
    """Run `target(*args, out_path)` in a child process and read the JSON lines it writes.

    Returns None if the whole run exceeds timeout_s, if the output stops growing for idle_s,
    or if the child fails. The child is terminated and reaped in every case, so nothing keeps
    running or holding messages after we give up. This matters because chat-downloader retries
    a broken API for minutes inside its own code, where no in-process check could interrupt it.
    A partial replay is discarded on purpose: chat covering only the start of a stream would
    make the rest look quiet and skew the scoring.

    Clocks: everything uses time.monotonic(), which wall-clock changes (NTP, daylight saving,
    manual edits) can't disturb. The child records its own monotonic finish time in a shared
    value, so a replay that finished on time is accepted even if we only notice later, and one
    that finished after the deadline is rejected. (Monotonic time is shared by all processes on
    the same machine on Windows and Linux, which is what makes comparing these values valid.)
    """
    ctx = multiprocessing.get_context("spawn")
    finished_at = ctx.Value("d", 0.0)  # stays 0.0 unless the worker runs to completion
    with tempfile.TemporaryDirectory(prefix="clipper_chat_") as tmp:
        out = Path(tmp) / "chat.jsonl"
        proc = ctx.Process(target=_worker_entry, args=(target, args, str(out), finished_at), daemon=True)
        deadline = time.monotonic() + timeout_s
        proc.start()
        last_growth, size = time.monotonic(), 0
        try:
            while proc.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                proc.join(min(0.2, remaining))  # never sleep past the total deadline
                if not proc.is_alive():
                    break
                now = time.monotonic()  # sampled *before* reading the file, so growth is never credited late
                cur = out.stat().st_size if out.exists() else 0
                if cur != size:
                    size, last_growth = cur, now
                elif now - last_growth > idle_s:
                    return None
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        if proc.exitcode != 0 or not out.exists():
            return None
        # completion must have been recorded by the worker itself, and within the deadline
        if not (0.0 < finished_at.value <= deadline):
            return None
        try:
            with out.open(encoding="utf-8") as f:  # line by line: replays can be large
                return [(float(t), str(text)) for t, text in (json.loads(line) for line in f if line.strip())]
        except (OSError, ValueError, TypeError):
            return None


def chat_via_chat_downloader(url: str, timeout_s: float, idle_s: float = 90) -> list[tuple[float, str]] | None:
    """Chat replay for a VOD URL, or None if unavailable (unsupported site, broken API, too slow)."""
    return run_chat_process(_chat_worker, (url,), timeout_s, idle_s)
