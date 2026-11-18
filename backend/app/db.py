from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine
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
    # pending -> approved | rejected (Stage 3 dashboard)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    video: Mapped[Video] = relationship(back_populates="candidates")


def init_db() -> None:
    Base.metadata.create_all(engine)
