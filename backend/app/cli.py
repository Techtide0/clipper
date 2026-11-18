import argparse

from sqlalchemy import select

from . import pipeline
from .db import Candidate, SessionLocal, Streamer, init_db
from .sources import detect_platform


def fmt(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def main() -> None:
    init_db()
    p = argparse.ArgumentParser(prog="clipper")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="track a streamer")
    a.add_argument("name")
    a.add_argument("channel_url")
    a.add_argument("--type", dest="content_type", default="gameplay",
                   choices=["gameplay", "news", "commentary"])
    sub.add_parser("streamers")
    sc = sub.add_parser("scan", help="find new videos")
    sc.add_argument("--limit", type=int, default=5)
    sub.add_parser("process", help="download + analyze all new videos")
    sub.add_parser("judge", help="AI-score and caption un-judged candidates")
    c = sub.add_parser("candidates", help="show top moments")
    c.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    with SessionLocal() as db:
        if args.cmd == "add":
            try:
                platform = detect_platform(args.channel_url)
            except ValueError as e:
                p.error(str(e))
            db.add(Streamer(name=args.name, channel_url=args.channel_url, platform=platform,
                            content_type=args.content_type))
            db.commit()
            print(f"Added {args.name} ({platform})")
        elif args.cmd == "streamers":
            for s in db.scalars(select(Streamer)):
                print(s.id, s.name, s.platform, s.content_type, s.channel_url)
        elif args.cmd == "scan":
            for s in db.scalars(select(Streamer)):
                print(f"{s.name}: {pipeline.scan_streamer(s.id, args.limit)} new videos")
        elif args.cmd == "process":
            pipeline.process_pending()
        elif args.cmd == "judge":
            from .judge import judge_pending
            print(f"Judged {judge_pending()} moments")
        elif args.cmd == "candidates":
            rank = Candidate.ai_score.desc().nulls_last(), Candidate.score.desc()
            q = select(Candidate).where(Candidate.review_status != "rejected").order_by(*rank).limit(args.top)
            for cand in db.scalars(q):
                v = cand.video
                ai = f"AI {cand.ai_score:4.1f}" if cand.ai_score is not None else "AI  -- "
                # only YouTube URLs take a &t= offset; for other sites the start time is printed instead
                where = (f"{v.url}&t={int(cand.start_s)}s" if v.streamer.platform == "youtube"
                         else f"{v.url.split('#')[0]}  (at {fmt(cand.start_s)})")
                print(f"{ai}  sig {cand.score:4.2f}  {fmt(cand.start_s)}-{fmt(cand.end_s)}  "
                      f"{cand.title or v.title[:50]}\n        {where}")


if __name__ == "__main__":
    main()
