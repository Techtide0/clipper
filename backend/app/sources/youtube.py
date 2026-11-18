from pathlib import Path

import yt_dlp

from .base import Downloaded, Source, VideoInfo


class YouTubeSource(Source):
    platform = "youtube"

    def list_videos(self, channel_url: str, limit: int = 10) -> list[VideoInfo]:
        # Streams tab holds past live streams (VODs) and current lives.
        url = channel_url.rstrip("/")
        if not url.endswith("/streams"):
            url += "/streams"
        opts = {"extract_flat": True, "playlistend": limit, "quiet": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        out = []
        for e in info.get("entries", []) or []:
            if not e or not e.get("id"):
                continue
            out.append(VideoInfo(
                platform_id=e["id"],
                title=e.get("title") or e["id"],
                url=f"https://www.youtube.com/watch?v={e['id']}",
                duration_s=int(e["duration"]) if e.get("duration") else None,
            ))
        return out

    def download(self, video: VideoInfo, out_dir: Path) -> Downloaded:
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / f"{video.platform_id}.%(ext)s"),
            "quiet": True,
            "noprogress": True,
            # 16 kHz mono wav: tiny and all the signal analysis needs
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
            "postprocessor_args": {"extractaudio": ["-ac", "1", "-ar", "16000"]},
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "live_chat"],
            "subtitlesformat": "vtt/best",
            "ignoreerrors": "only_download",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([video.url])
        audio = out_dir / f"{video.platform_id}.wav"
        if not audio.exists():
            raise RuntimeError("audio download failed")
        chat = next(iter(out_dir.glob(f"{video.platform_id}*.live_chat.json")), None)
        caps = next(iter(out_dir.glob(f"{video.platform_id}*.vtt")), None)
        return Downloaded(audio=audio, chat=chat, captions=caps)
