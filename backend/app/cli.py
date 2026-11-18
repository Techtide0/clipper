import argparse

from sqlalchemy import func, select

from . import pipeline
from .db import Candidate, SessionLocal, Streamer, Video, init_db
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
    av = sub.add_parser("add-video", help="analyze one video from a YouTube, Twitch or Kick link")
    av.add_argument("url")
    av.add_argument("--type", dest="content_type", default=None, choices=["gameplay", "news", "commentary"],
                    help="applies to this video only; default is the channel's type (gameplay for a new channel)")
    av.add_argument("--no-process", action="store_true", help="only register the link; analyze later with `process`")
    sc = sub.add_parser("scan", help="find new videos")
    sc.add_argument("--limit", type=int, default=5)
    sub.add_parser("process", help="download + analyze all new videos")
    sub.add_parser("judge", help="AI-score and caption un-judged candidates")
    t = sub.add_parser("tune", help="suggest signal weights from the views you entered")
    mode = t.add_mutually_exclusive_group()  # each is fine alone; together they'd be contradictory
    mode.add_argument("--apply", action="store_true", help="save the suggestion for future analysis")
    mode.add_argument("--reset", action="store_true", help="go back to the configured default weights")
    c = sub.add_parser("candidates", help="show top moments")
    c.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    with SessionLocal() as db:
        if args.cmd == "add":
            try:
                platform = detect_platform(args.channel_url)
            except ValueError as e:
                p.error(str(e))
            existing = db.scalars(select(Streamer).where(Streamer.channel_url == args.channel_url)).first()
            if existing:  # e.g. created earlier by pasting one of its videos: just start following it
                existing.tracked, existing.content_type = True, args.content_type
                db.commit()
                print(f"Now tracking {existing.name} ({existing.platform})")
            else:
                db.add(Streamer(name=args.name, channel_url=args.channel_url, platform=platform,
                                content_type=args.content_type))
                db.commit()
                print(f"Added {args.name} ({platform})")
        elif args.cmd == "add-video":
            try:
                vid, created = pipeline.ingest_url(args.url, args.content_type)
            except (ValueError, ConnectionError) as e:
                p.error(str(e))
            v = db.get(Video, vid)
            print(f"{'Added' if created else 'Already added'}: {v.title} ({v.status})")
            # claim_video is atomic, so this never analyzes a video something else is already running
            if not args.no_process and pipeline.claim_video(vid):
                print("Analyzing (this takes a few minutes for a long video)...")
                pipeline.analyze_and_judge(vid)
                db.refresh(v)
                n = db.scalar(select(func.count(Candidate.id)).where(Candidate.video_id == vid))
                print(f"Status: {v.status}, {n} moments found." + (f" Note: {v.error}" if v.error else ""))
                print("Review them in the dashboard, or run: python -m app.cli candidates")
        elif args.cmd == "streamers":
            for s in db.scalars(select(Streamer)):
                print(s.id, s.name, s.platform, s.content_type, s.channel_url)
        elif args.cmd == "scan":
            failed = 0
            for s in db.scalars(select(Streamer).where(Streamer.tracked)):
                try:
                    print(f"{s.name}: {pipeline.scan_streamer(s.id, args.limit)} new videos")
                except Exception as e:  # one broken channel must not stop the others
                    failed += 1
                    print(f"{s.name}: scan failed ({type(e).__name__}: {e})")
            if failed:
                raise SystemExit(1)  # still scanned everything, but scripts can tell something failed
        elif args.cmd == "process":
            pipeline.process_pending()
        elif args.cmd == "tune":
            from . import tuning
            if args.reset:
                print("Weights reset to defaults." if tuning.reset_weights() else "No saved weights to reset.")
                return
            rep = tuning.analyze(tuning.collect_rows())
            print(f"{rep.n} clips with views.")
            for name, (rho, n) in rep.correlations.items():
                print(f"  {name:9} correlation with views {rho:+.2f}  (on {n} clips)")
            for note in rep.notes:
                print("  note:", note)
            if rep.enough:
                print("current  :", rep.current)
                print("suggested:", rep.suggested)
                if args.apply:
                    tuning.save_weights(rep.suggested)
                    print("Saved. New analysis runs will use these weights (undo with: tune --reset).")
                else:
                    print("Run again with --apply to use the suggestion.")
            elif args.apply:
                print("Not applied: not enough data yet.")
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
