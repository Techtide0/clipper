import argparse

from sqlalchemy import func, select

from . import pipeline
from .config import settings
from .db import Candidate, SessionLocal, Streamer, Video, init_db


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
    rc = sub.add_parser("recover", help="free videos whose analysis stalled (its worker died) so they can be retried")
    rc.add_argument("--all", action="store_true",
                    help="free every in-progress video, even a recent one: only if you know nothing is running")
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
                sid, created = pipeline.register_streamer(args.name, args.channel_url, args.content_type)
            except ValueError as e:
                p.error(str(e))
            s = db.get(Streamer, sid)
            print(f"{'Added' if created else 'Now tracking'} {s.name} ({s.platform})")
        elif args.cmd == "add-video":
            try:
                vid, created = pipeline.ingest_url(args.url, args.content_type)
            except (ValueError, ConnectionError) as e:
                p.error(str(e))
            v = db.get(Video, vid)
            print(f"{'Added' if created else 'Already added'}: {v.title} ({v.status})")
            if args.no_process:
                return
            # claim_video is atomic, so this never analyzes a video something else is already running
            token = pipeline.claim_video(vid, content_type=args.content_type)
            if not token:
                db.expire_all()  # what it is now, not what it was when we first looked
                now = db.get(Video, vid)
                if now is None:
                    print("Not started: the video was removed in the meantime.")
                    return
                print(f"Not started: it is {now.status}. If its analysis died, free it with: "
                      f"python -m app.cli recover   (it only frees claims idle for {int(settings.claim_stale_s)}s+; "
                      "add --all if you know nothing is running)")
                return
            print("Analyzing (this takes a few minutes for a long video)...")
            pipeline.analyze_and_judge(vid, token)
            db.refresh(v)
            n = db.scalar(select(func.count(Candidate.id)).where(Candidate.video_id == vid))
            if v.status == "failed":
                print(f"Analysis failed: {v.error}")
                raise SystemExit(1)
            print(f"Status: {v.status}, {n} moments found." + (f" Note: {v.error}" if v.error else ""))
            print("Review them in the dashboard, or run: python -m app.cli candidates")
        elif args.cmd == "recover":
            n = pipeline.recover_stale(0 if args.all else None)
            print(f"Freed {n} stalled video(s); paste/retry them to analyze again." if n
                  else "Nothing to recover: no analysis has stalled.")
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
            judge_error = None
            try:
                print(f"Judged {judge_pending()} moments")
            except Exception as e:
                judge_error = e
            try:  # even after a partial run: videos that are fully scored now lose their old warning
                cleared = pipeline.refresh_scoring_notes()
                if cleared:
                    print(f"{cleared} video(s) are now fully scored.")
            except Exception as e:
                if judge_error is None:
                    raise  # nothing else went wrong: this is the error
                print(f"(also couldn't refresh the scoring notes: {type(e).__name__}: {e})")
            if judge_error is not None:
                raise judge_error
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
