"""Stand-ins for the chat-downloader worker, run in child processes by the tests.

Each takes the output path as its last argument, like app.sources.ytdlp._chat_worker.
"""
import json
import time


def _write(f, t, text):
    f.write(json.dumps([t, text]) + "\n")
    f.flush()


def ok(out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        _write(f, 90.0, "replay at 90 seconds")
        _write(f, 91.5, "second")


def slow_but_finishes(out_path):
    """Pauses (shorter than the idle limit) between messages, then completes."""
    with open(out_path, "w", encoding="utf-8") as f:
        _write(f, 1.0, "a")
        time.sleep(1.0)
        _write(f, 2.0, "b")


def hang_before_any_message(out_path):
    time.sleep(120)  # like chat-downloader retrying a broken API internally


def endless(out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        while True:
            _write(f, 1.0, "x")
            time.sleep(0.05)


def explode(out_path):
    raise KeyError("data")
