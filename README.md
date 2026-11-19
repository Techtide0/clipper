# Clipper

Finds the best moments in GTA VI videos and streams, scores them with AI, cuts them into 9:16 and
16:9 clips, and leaves you with one job: approve or reject. Runs on your own laptop.

## Quick start (Windows)

1. **Install once:** Python 3.12, Node.js, and ffmpeg (`winget install Gyan.FFmpeg`).
2. Double-click **`setup.bat`**. It installs everything and creates `backend\.env`.
3. Open `backend\.env` and paste your Gemini key after `GEMINI_API_KEY=`
   (free at https://aistudio.google.com/apikey).
4. Double-click **`start.bat`**. Your browser opens at http://localhost:8000. Leave the black window open while you use it.

## Using it

| You want to | Do this |
|---|---|
| Analyze one video | Paste its YouTube/Twitch/Kick link in the box at the top of **Review** and press Analyze |
| Follow a channel | **Channels** page: paste the channel link. Turn on **Automatic mode** in **Settings & health** and new videos are found, analyzed and scored for you |
| Decide on moments | **Review**: Approve (it cuts the clip automatically) or Reject |
| Get clips to post | **Approved** tab: Download 9:16 / 16:9 and Copy caption |
| Get the whole video | The **Full video** button on a video's row |
| Post to YouTube | One-time setup in `docs/youtube-setup.md`, then **Post to YouTube** on an approved, cut clip (uploads private; you publish in YouTube Studio) |
| Check everything works | **Settings & health** |

The app never posts anything for you and never approves anything by itself.

## Where things are

- `backend/` Python server (FastAPI), `frontend/` dashboard (React), `docs/` design notes.
- Your data lives in `backend/data/` (database, clips, exports, downloads). Back that folder up if it matters to you.
- `docs/ui-spec.md` and `docs/design-brief.md` describe every screen, for designing a new look.

## Developing

```
cd backend
.venv\Scripts\python -m pytest -q                              # tests
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --reload
cd ..\frontend
npm run dev                                                    # dashboard with live reload on :5173
```

## If something goes wrong

- **Settings & health** shows what's missing (ffmpeg, AI key, disk space) and how to fix it.
- A video or clip stuck on "processing"/"cutting" after a crash is freed automatically the next
  time the app starts. From the command line: `python -m app.cli recover`.
- Only analyze content you have the right to use. Clips of other creators' videos can be taken down:
  get permission and add your own commentary.
