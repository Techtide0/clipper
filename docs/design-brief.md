# Clipper: design brief

A description of the application as it exists today, written so a UI/UX design can be made for it.
Everything under "What exists" is built and working. Everything under "Not built yet" is planned and
needs design too, so the new interface doesn't have to be redone when it arrives.

---

## 1. What it is, in one paragraph

Clipper is a local web app (it runs on the owner's laptop and opens in the browser) that finds good
moments in **GTA VI** videos and streams, cuts them into short vertical and widescreen clips, writes a
title, hook and caption for each, and leaves the owner with one real job: **look at each suggested
moment and say yes or no**. Approved moments become downloadable clips ready to post on YouTube Shorts,
Instagram Reels, TikTok and Facebook.

The goal behind the product: **about 30 minutes a day** of the owner's time, fitted around studies.
The app should do the boring 95% (finding, scoring, cutting) and make the decision step fast.

## 2. Who uses it

- **One person**, the owner. No accounts, no teams, no sign-in. Everything is on their machine.
- A student who also makes content: comfortable with web apps and social platforms, **not** a developer.
  They should never need a terminal to do their daily routine.
- Works in short sessions (the 30 minutes), usually on a laptop, possibly with a modest graphics chip.
- Posts three kinds of content: **gameplay** clips, **news** (announcements, leaks, trailers) and
  **commentary** (opinion and breakdowns). The AI judges each kind by different standards.

## 3. The core loop (what the whole design should serve)

```
  Find a video  ->  App analyzes it  ->  Owner reviews moments  ->  Approved ones are cut
  (paste a link,    (a few minutes,      (the main job: yes /       (automatic, ~3 min each)
   or a followed     in the background)   no / tweak)                        |
   channel)                                                                   v
                                         Enter views later  <-  Post it  <-  Download + copy caption
                                         (teaches the scoring)  (by hand)
```

The review step is where the owner spends nearly all their time. **It must be the fastest, calmest,
most obvious part of the interface.**

## 4. What exists today (one page, three areas)

The current interface is a single page with a link box on top and a list of moment cards underneath.
It works but was built for function, not for use; this is what a designer is replacing.

### 4.1 "Analyze a video" box (top of page)
- A text field for a **YouTube, Twitch or Kick video link** and a **type** picker
  (channel default / gameplay / news / commentary) and an **Analyze** button.
- Instant feedback: success message ("Added ... analysis takes a few minutes"), or a plain-English
  error (for example: it's a channel link not a video; the stream hasn't finished; unsupported site;
  network problem, try again).
- Pasting the same link twice is harmless ("already added").

### 4.2 The recent-videos list (under the box, the 6 most recent)
Each row: a **status badge**, the title, the channel, the number of moments found, any note or error,
and a **Full video** control.

Video statuses a designer must show:

| Status | Meaning for the owner |
|---|---|
| `new` | Registered, waiting. Nothing is running. |
| `queued` | About to be analyzed. |
| `processing` | Being analyzed right now (downloading audio, finding moments, AI scoring). |
| `analyzed` | Done. Moments are ready to review. |
| `failed` | Something went wrong; the reason is shown and it can be retried by pasting the link again. |

Possible **notes** on an analyzed video: "AI scoring incomplete (...); run judge again later" (some
moments weren't scored; they still appear, just unscored).

**Full video** control (optional, because the file can be several GB):
`Full video (1:15)` button -> `waiting to download...` -> `downloading 42%` -> `Save full video (212 MB)`
plus `Delete copy`, or `failed: <reason>` plus `Retry`. It refuses if the disk is too full, and says so.

### 4.3 The review area: tabs and moment cards
Tabs: **To review**, **Approved**, **Rejected**. A moment moves between them as the owner decides.
On the Approved tab, a **Render all approved (N)** button cuts everything still waiting.

A **moment card** is the heart of the product. It contains:

1. **Preview** (left)
   - Before cutting: a still image with "Preview at 10:00". Clicking loads the real YouTube player at
     that moment. (It is lazy on purpose: 25 live players on one page overloads the browser.)
   - For Twitch/Kick videos there is no embedded preview, only "Open the video" and the time to jump to.
   - After cutting: the **finished vertical clip playing in the card**.
2. **Badges and context**: **AI score** (0-10), the video type, the "signal" strength, the source video
   title, and the time range (for example 11:41-12:11).
3. **AI reason**: one sentence on why it scored that way.
4. **Editable fields**: Title, Hook (the first line a viewer should see or hear), Caption (with
   hashtags), Start and End in seconds (trim).
5. **Actions**: Save changes (appears when something was edited), **Approve**, **Reject**,
   Back to review, **Cut clip / Re-cut**, **Export for platforms**.
6. **After cutting**: **Download 9:16**, **Download 16:9**, **Copy caption**, with a note on which
   suits which platform.
7. **Export panel** (after "Export for platforms"): one row per platform (YouTube Shorts, YouTube
   16:9, Instagram Reels, TikTok, Facebook Reels) with a download of that platform's video and a
   **Copy text** button for that platform's title and caption, trimmed to its limits. Shows warnings
   (for example a clip longer than a platform allows).
8. **Results after posting** (approved clips only): **Views** and **Likes** number fields and a
   Save results button. This feeds the scoring so it learns what performs.

Moment states a designer must show:

| Review state | Cut state | What it means |
|---|---|---|
| pending | none | Suggested by the app; the owner hasn't decided. |
| pending / approved | rendering | The clip is being cut ("cutting the clip..."); takes about 3 minutes, one at a time. |
| pending / approved | done | Cut. Preview plays the real clip; downloads appear. |
| approved | failed | The cut failed; shows the reason; Cut clip retries. |
| rejected | none | Hidden away. The app auto-rejects anything the AI scores below 6, so the Rejected tab is a real place the owner should be able to rescue things from. |

## 5. How things behave (facts that shape the UX)

**Timings, measured on a real laptop**
- Analyzing a video: about 1 minute for a 25-minute video; about 5 minutes for a 3-hour stream.
- AI scoring: 1-2 minutes for 25 moments.
- Cutting one clip into both formats: **about 3 minutes** (it downloads just that 30 seconds, then
  encodes twice). Clips are cut **one at a time**, so ten approved clips is about half an hour.
- A full-video download depends on length and connection: a real 15-minute video took about
  2 minutes and was about 210 MB at 1080p (so a 3-hour stream is several GB).
- So: **most waiting happens in the background and the interface must make waiting feel safe**: clear
  progress, never a frozen button, and the ability to leave and come back.

**Counts**
- A video usually yields up to **25 candidate moments**, each about **30 seconds**.
- Moments scoring under **6/10** are auto-rejected, so typically only a handful (for example 5 of 17)
  land in "To review".

**Things that go wrong (and must have good messages)**
- The AI service is often overloaded; the app retries and switches models, but sometimes some moments
  stay unscored. The owner should see "scored 20 of 25" not an error.
- A video can fail to download; a stream that is still live or not yet started is refused.
- Chat-based signals only work for some YouTube videos; Twitch and Kick currently have none.
- The browser may be closed while work is running; the server keeps going. If the **server** is
  stopped mid-work, stalled items become "failed, retry" when it starts again.

**Known quality limits a designer should not hide**
- The AI reads **text only** (transcript and chat); it cannot hear or see. On videos without
  captions its scores are weaker. (An audio-aware version is the planned fix.)
- The 9:16 clip currently shows the whole widescreen frame in the middle with a blurred background
  above and below, so the picture looks small on a phone. A "fill the screen" crop is planned.
- No subtitles are burned into the video yet.

## 6. Not built yet (needs design too)

> **Historical baseline.** This brief describes the app as it was before the redesign. The dashboard now has
> Channels, Settings & health, Review, Ready to post, Videos and Performance screens. What is still genuinely
> outstanding: burned-in subtitles and posting to TikTok/Instagram/Facebook. The list below is the original one,
> where Channels were command-line only, Settings were configuration-file based and Fit / Fill was planned.

These exist only as command-line tools or not at all. A designer should treat them as real screens.

1. **Channels / followed streamers**: add a streamer, set its type (gameplay/news/commentary), see its
   recent videos, start or stop following. *Today: command line only.*
2. **Automatic mode**: the app watches followed channels, analyzes new videos, scores them and cuts
   approved ones without being asked, so the owner just opens it and reviews. *The biggest step toward
   the 30-minutes-a-day goal.*
3. **Settings**: auto-reject threshold (6), how many moments per video (25), signal weights, the AI
   model, disk and cleanup preferences. *Today: a config file.*
4. **Videos page**: a full list of every video (the current page shows only 6), with delete,
   re-analyze, full-video download, disk usage.
5. **Publishing**: post straight to YouTube Shorts (first), later others. Needs a "connect your
   account" flow, a schedule, and post status.
6. **Performance and learning**: a view of how clips performed and what the app learned
   ("chat spikes predict your views; loudness doesn't"), with a button to apply the suggested weights.
   *Today: a command-line command that suggests weights once enough clips have views.*
7. **First-run and health**: is the server running, is the AI key set, is ffmpeg installed, how much
   disk is free. *Today the owner finds out from errors.*

## 7. Design goals and principles

1. **Review speed above everything.** One moment at a time, large preview, big Approve/Reject, and
   keyboard shortcuts (for example A approve, R reject, E edit, arrow keys to move). The owner should be
   able to clear a day's moments in minutes.
2. **Show the AI's reasoning, not just its score**: the reason, the score, and the transcript text
   behind it (the data exists but the current screen does not show the transcript).
3. **Make background work visible and calm.** Every long task has a plain status, a progress hint and
   an expected duration. Nothing should look stuck.
4. **Nothing is lost by a mistake.** Reject is reversible; deleting a full video says what it frees and
   that clips are unaffected; trimming an approved clip warns that it will be re-cut.
5. **Errors are sentences, not codes**, always with the next step ("try again", "free up space").
6. **Honest about limits** (section 5), without being alarming.
7. **Dark and light themes** (the current page follows the system setting).
8. **Responsive**, because the review step may happen on a phone/tablet over the same network, even
   though everything runs on the laptop.
9. **Accessible**: keyboard operable, visible focus, labels on every field, status not conveyed by
   colour alone.

## 8. Screens to design (suggested)

| Screen | Purpose | Key content and actions |
|---|---|---|
| **Today / Inbox** (home) | The daily 30 minutes | Count of moments waiting; "Start reviewing"; what's processing; what failed; clips ready to post. |
| **Review** (one moment at a time) | The core job | Large preview, AI score and reason, transcript, title/hook/caption, trim, Approve / Reject / Skip, shortcuts, progress "7 of 12". |
| **Ready to post** | Hand-off | Approved and cut clips: Download 9:16 / 16:9, Copy caption, per-platform export, "mark as posted", enter views later. |
| **Videos** | Everything analyzed | List with status, moments, disk used, full-video download/delete, re-analyze. Add-a-link box lives here and on Today. |
| **Channels** | Who to follow | Add/remove streamers, type, last scan, auto mode on/off. |
| **Performance** | Learning loop | Views per clip, what predicts success, suggested weights, apply/reset. |
| **Settings & health** | Setup | Threshold, limits, AI key status, disk, connected accounts. |

## 9. Interactions and microcopy to get right

- **Link box**: accept a paste, validate on the spot, name what it understood ("YouTube video, 15:03,
  IGN"), then confirm.
- **Approve** triggers the cut automatically; the button should say what happens ("Approve and cut").
- **Cut clip** on a pending moment lets the owner judge the real clip before deciding.
- **Render all approved** explains that clips are cut one after another and how long it will take.
- **Copy caption** confirms with a brief "Copied", and falls back to a selectable text box if the
  browser blocks copying.
- **Empty states**: nothing to review yet ("Paste a link to get started"), nothing approved, nothing
  rejected, nothing processing.
- **Counts and ETAs**: "3 clips cutting, about 9 minutes left".

## 10. Technical notes for implementation

- Front end: React (Vite). Talks to a local server at `/api/...` (proxied in development).
- Server: Python (FastAPI) on port 8000; data in a local database and a `data/` folder
  (clips, exports, optional full downloads).
- Useful endpoints already available:
  - `POST /videos/from-url`, `GET /videos`, `GET /videos/{id}`
  - `POST /videos/{id}/full-download`, `GET` / `DELETE /videos/{id}/full`
  - `GET /candidates?status=pending|approved|rejected`, `PATCH /candidates/{id}`
  - `POST /candidates/{id}/render`, `POST /candidates/render-approved`
  - `GET /candidates/{id}/download/vertical|wide`
  - `POST` / `GET /candidates/{id}/export`
- The page currently **polls** every 4 seconds while something is running. A smoother approach
  (live updates) is possible if the design needs it.
- Clip previews play from `/clips/...`; downloads are served as attachments named from the title.
