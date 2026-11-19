"""Settings the owner can change from the dashboard, stored in the database.

The values in config.py / .env are the defaults; a value saved here wins. Each setting has a type
and a range, so a bad value from the UI is refused with a clear message instead of breaking things.
"""
from dataclasses import dataclass

from sqlalchemy import select

from .config import settings
from .db import AppSetting, SessionLocal


@dataclass(frozen=True)
class Spec:
    kind: type            # bool, int or float
    default: object
    low: float | None = None
    high: float | None = None
    label: str = ""
    help: str = ""
    choices: tuple | None = None  # for a text setting: the only allowed words


SPECS: dict[str, Spec] = {
    "auto_mode": Spec(bool, False, label="Automatic mode",
                      help="Check your followed channels on a schedule, analyze new videos and score them, so moments are waiting when you open the app."),
    "auto_interval_minutes": Spec(int, 60, 5, 1440, "Check every (minutes)", "How often automatic mode looks for new videos."),
    "auto_videos_per_cycle": Spec(int, 2, 1, 10, "New videos per check",
                                  "At most this many new videos are analyzed per check, so a busy channel can't flood the laptop."),
    "auto_max_video_minutes": Spec(int, 360, 5, 720, "Skip videos longer than (minutes)",
                                   "Automatic mode ignores very long streams. You can still analyze them yourself."),
    "judge_min_score": Spec(float, settings.judge_min_score, 0, 10, "Auto-reject below (AI score)",
                            "Moments the AI scores under this land in Rejected. You can rescue any of them."),
    "youtube_privacy": Spec(str, "private", label="Privacy of videos uploaded to YouTube", choices=("private", "unlisted", "public"),
                            help="Private is the safe default: check the upload in YouTube Studio, then publish it yourself. (Google also forces uploads from a new, unaudited app to private.)"),
    "delete_working_files": Spec(bool, settings.delete_raw_after_processing, label="Delete working files after analysis",
                                 help="Keeps disk use low: the downloaded audio is removed once a video has been analyzed."),
    "full_download_headroom_gb": Spec(float, settings.full_download_headroom_gb, 0, 50, "Full-download safety margin (GB)",
                                      help="A full-video download is refused unless this much space would still be free afterwards."),
    "shortlist_size": Spec(int, settings.shortlist_size, 5, 50, "Moments per video",
                           "How many candidate moments are picked from each video (applies to videos analyzed from now on)."),
}


def _coerce(key: str, value: object) -> object:
    """The value as the setting's type, or ValueError with a message fit to show the owner."""
    spec = SPECS[key]
    if spec.kind is str:
        if value not in (spec.choices or ()):
            raise ValueError(f"{spec.label}: must be one of {', '.join(spec.choices or ())}")
        return value
    if spec.kind is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{spec.label}: must be on or off")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{spec.label}: must be a number")
    if spec.kind is int and float(value) != int(value):
        raise ValueError(f"{spec.label}: must be a whole number")
    number = spec.kind(value)
    if (spec.low is not None and number < spec.low) or (spec.high is not None and number > spec.high):
        raise ValueError(f"{spec.label}: must be between {spec.low:g} and {spec.high:g}")
    return number


def get(key: str):
    """The current value: what was saved, if valid, else the default."""
    with SessionLocal() as db:
        row = db.get(AppSetting, key)
    if row is not None:
        try:
            return _coerce(key, row.value)
        except ValueError:
            pass  # a bad stored value (edited by hand) falls back to the default rather than breaking things
    return SPECS[key].default


def all_values() -> dict:
    return {k: get(k) for k in SPECS}


def update(changes: dict) -> dict:
    """Validate and save some settings at once (all or nothing). Returns the new values."""
    unknown = sorted(set(changes) - set(SPECS))
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(unknown)}")
    clean = {k: _coerce(k, v) for k, v in changes.items()}  # any bad value refuses the whole change
    with SessionLocal() as db:
        for key, value in clean.items():
            row = db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        db.commit()
    return all_values()


def describe() -> list[dict]:
    """Everything a settings screen needs: key, label, help, type, range, default and current value."""
    return [dict(key=k, label=s.label, help=s.help, type=s.kind.__name__, min=s.low, max=s.high,
                 default=s.default, value=get(k), choices=list(s.choices) if s.choices else None) for k, s in SPECS.items()]


# internal bookkeeping (not user-editable): when automatic mode last ran, and what it did
def get_state(key: str, default=None):
    with SessionLocal() as db:
        row = db.get(AppSetting, f"state_{key}")
    return default if row is None else row.value


def set_state(key: str, value) -> None:
    with SessionLocal() as db:
        row = db.get(AppSetting, f"state_{key}")
        if row is None:
            db.add(AppSetting(key=f"state_{key}", value=value))
        else:
            row.value = value
        db.commit()
