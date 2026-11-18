import argparse

from sqlalchemy import select

from . import pipeline
from .db import Candidate, SessionLocal, Streamer, init_db


def fmt(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def main() -> None:
    init_db()
    p = argparse.ArgumentParser(prog="clipper")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="track a streamer")
    a.add_argument("name")
    a.add_argument("channel_url")
    sub.add_parser("streamers")
    sc = sub.add_parser("scan", help="find new videos")
    sc.add_argument("--limit", type=int, default=5)
    sub.add_parser("process", help="download + analyze all new videos")
    c = sub.add_parser("candidates", help="show top moments")
    c.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    with SessionLocal() as db:
        if args.cmd == "add":
            db.add(Streamer(name=args.name, channel_url=args.channel_url))
            db.commit()
            print(f"Added {args.name}")
        elif args.cmd == "streamers":
            for s in db.scalars(select(Streamer)):
                print(s.id, s.name, s.channel_url)
        elif args.cmd == "scan":
            for s in db.scalars(select(Streamer)):
                print(f"{s.name}: {pipeline.scan_streamer(s.id, args.limit)} new videos")
        elif args.cmd == "process":
            pipeline.process_pending()
        elif args.cmd == "candidates":
            q = select(Candidate).order_by(Candidate.score.desc()).limit(args.top)
            for cand in db.scalars(q):
                v = cand.video
                print(f"{cand.score:5.2f}  {fmt(cand.start_s)}-{fmt(cand.end_s)}  "
                      f"{v.title[:50]}  {v.url}&t={int(cand.start_s)}s")


if __name__ == "__main__":
    main()
