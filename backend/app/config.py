from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"   # set GEMINI_MODEL in .env to switch (retired/busy models return 404/503)
    judge_batch_size: int = 10               # moments per API call (keeps free-tier usage low)
    scheduler_enabled: bool = True           # allow the background scheduler (automatic mode) to run at all
    hardware_encoding: bool = True          # use Intel Quick Sync for cutting when this machine has it
    judge_audio: bool = True                # send each moment's audio to the AI (it can then hear, not just read)
    # If the main model is overloaded (it often is, on the free tier) these are tried in order.
    gemini_fallback_models: list[str] = ["gemini-3.1-flash-lite"]
    judge_attempts_per_model: int = Field(2, ge=1)   # tries on one model before moving to the next
    judge_retry_wait_s: float = Field(3.0, ge=0)     # base wait between tries (doubles each time)
    judge_reply_attempts: int = Field(3, ge=1)       # times to re-ask for moments a reply left out or garbled
    judge_min_score: float = 6.0             # 0-10; below this a moment is auto-rejected
    data_dir: Path = ROOT / "data"
    shortlist_size: int = 25
    clip_window_s: int = 30          # length of each candidate moment
    min_gap_s: int = 45              # candidates must be this far apart
    # relative weights of each signal in the final score
    w_audio: float = 1.0
    w_chat: float = 1.5
    w_keywords: float = 0.8
    delete_raw_after_processing: bool = True
    # a full-video download is refused unless this much space would be left free after it (GB)
    full_download_headroom_gb: float = Field(2.0, ge=0)
    # give up on a chat replay that takes longer than this, or that delivers nothing for
    # chat_idle_timeout_s (a broken API); both must be positive, finite numbers
    # A running analysis refreshes its claim every heartbeat_s; a claim not refreshed for
    # claim_stale_s is treated as abandoned (the worker died) and may be recovered.
    heartbeat_s: float = Field(30, gt=0, allow_inf_nan=False)
    claim_stale_s: float = Field(180, gt=0, allow_inf_nan=False)
    chat_timeout_s: float = Field(900, gt=0, allow_inf_nan=False)
    chat_idle_timeout_s: float = Field(90, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _heartbeat_before_stale(self):
        # a live worker must refresh its claim well before it can look abandoned
        if self.heartbeat_s * 2 > self.claim_stale_s:
            raise ValueError("heartbeat_s must be at most half of claim_stale_s, or a live analysis could be recovered")
        return self

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'clipper.db'}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def weights_path(self) -> Path:
        return self.data_dir / "weights.json"  # written by `clipper tune --apply`

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def moments_dir(self) -> Path:
        return self.data_dir / "moments"  # a short audio snippet per moment, so the AI can hear it

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"  # full-length videos you asked for


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.raw_dir.mkdir(parents=True, exist_ok=True)
settings.clips_dir.mkdir(parents=True, exist_ok=True)
settings.exports_dir.mkdir(parents=True, exist_ok=True)
settings.downloads_dir.mkdir(parents=True, exist_ok=True)
settings.moments_dir.mkdir(parents=True, exist_ok=True)
