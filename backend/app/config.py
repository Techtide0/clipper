from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    gemini_api_key: str = ""
    data_dir: Path = ROOT / "data"
    shortlist_size: int = 25
    clip_window_s: int = 30          # length of each candidate moment
    min_gap_s: int = 45              # candidates must be this far apart
    # relative weights of each signal in the final score
    w_audio: float = 1.0
    w_chat: float = 1.5
    w_keywords: float = 0.8
    delete_raw_after_processing: bool = True

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'clipper.db'}"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.raw_dir.mkdir(parents=True, exist_ok=True)
