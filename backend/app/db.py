from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings

engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Streamer(Base):
    __tablename__ = "streamers"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), default="youtube")
    name: Mapped[str] = mapped_column(String(100))
    channel_url: Mapped[str] = mapped_column(String(300), unique=True)
    # gameplay | news | commentary: changes how the AI judge scores this streamer's moments
    content_type: Mapped[str] = mapped_column(String(20), default="gameplay")
    videos: Mapped[list["Video"]] = relationship(back_populates="streamer")


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[int] = mapped_column(primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id"))
    platform_id: Mapped[str] = mapped_column(String(50), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(300))
    duration_s: Mapped[int | None] = mapped_column(default=None)
    # new -> downloaded -> analyzed | failed
    status: Mapped[str] = mapped_column(String(20), default="new")
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    streamer: Mapped[Streamer] = relationship(back_populates="videos")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="video")


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"))
    start_s: Mapped[float]
    end_s: Mapped[float]
    score: Mapped[float]
    signals: Mapped[dict] = mapped_column(JSON, default=dict)  # per-signal breakdown
    # text context for the AI judge (captured during analysis, since raw files get deleted)
    transcript: Mapped[str] = mapped_column(Text, default="")
    chat_sample: Mapped[str] = mapped_column(Text, default="")
    # Stage 2 fills these in
    ai_score: Mapped[float | None] = mapped_column(default=None)
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    hook: Mapped[str | None] = mapped_column(String(300), default=None)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    ai_reason: Mapped[str | None] = mapped_column(Text, default=None)
    # results you enter after posting the clip; the tuner learns from these
    views: Mapped[int | None] = mapped_column(default=None)
    likes: Mapped[int | None] = mapped_column(default=None)
    # pending -> approved | rejected (Stage 3 dashboard)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    # rendering: none -> rendering -> done | failed
    render_status: Mapped[str] = mapped_column(String(20), default="none")
    render_error: Mapped[str | None] = mapped_column(String(500), default=None)
    vertical_path: Mapped[str | None] = mapped_column(String(300), default=None)  # 9:16, relative to clips dir
    wide_path: Mapped[str | None] = mapped_column(String(300), default=None)      # 16:9
    video: Mapped[Video] = relationship(back_populates="candidates")


def _sql_literal(v: object) -> str | None:
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return None


def add_missing_columns(eng=engine) -> None:
    """Lightweight migration: add any model column an older database file doesn't have yet.

    New columns get the model's scalar default (e.g. content_type -> 'gameplay'), so existing
    rows keep working and no records are lost.
    """
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                default = _sql_literal(col.default.arg) if col.default is not None and col.default.is_scalar else None
                clause = f" DEFAULT {default}" if default is not None else ""
                conn.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(eng.dialect)}{clause}'))


def repair_streamer_platforms(eng=engine) -> None:
    """Upgrade rows saved by older versions.

    1. Streamers added before platform detection all said 'youtube', even for Twitch/Kick URLs.
       (Videos need no repair: the pipeline reads the platform from their streamer.)
    2. Twitch/Kick video ids are now stored with a 'twitch-' / 'kick-' prefix, so an older
       unprefixed row is renamed; otherwise a scan would see its VOD as new and analyze it twice.
    """
    from .sources import SOURCES, detect_platform  # local import: sources doesn't import this module
    with eng.begin() as conn:
        for sid, url, platform in conn.execute(text("SELECT id, channel_url, platform FROM streamers")).all():
            try:
                detected = detect_platform(url)
            except ValueError:
                continue  # unknown site: leave the row as it is
            if detected != platform:
                # guarded by the url we judged, so an edit made meanwhile is never overwritten
                conn.execute(text("UPDATE streamers SET platform = :p WHERE id = :i AND channel_url = :u"),
                             {"p": detected, "i": sid, "u": url})
        for key, source in SOURCES.items():
            if source.id_prefix:
                conn.execute(text(
                    "UPDATE videos SET platform_id = :pre || platform_id "
                    "WHERE substr(platform_id, 1, :n) != :pre AND streamer_id IN "
                    "(SELECT id FROM streamers WHERE platform = :key)"),
                    {"pre": source.id_prefix, "n": len(source.id_prefix), "key": key})


def init_db(eng=engine) -> None:
    Base.metadata.create_all(eng)
    add_missing_columns(eng)
    repair_streamer_platforms(eng)
