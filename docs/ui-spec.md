# Clipper: complete UI inventory

Every page, region, button, field, status and message in the product, written so it can be handed to a
designer or a design tool. The *why* and the user's goals are in [design-brief.md](design-brief.md).

> **Status update:** several things marked PROPOSED below have since been built in a first, plain version:
> the **Today** line (as a summary above the link box), **Channels**, **Settings & health** (health checks, automatic
> mode, editable settings, YouTube connection, Performance), per-video **Re-analyze / Delete / Score the rest**,
> the **vertical framing** choice (Fit / Fill), **Approve starts the cut**, **Post to YouTube** and automatic views.
> The page now has top navigation: **Review · Channels · Settings & health** (address `#channels`, `#settings`).
> **Current screens:** Today, Review (one moment at a time, with shortcuts), Ready to post, Videos, Channels,
> Performance, Settings & health (all built from `Clipper design.dc.html`). The inventory below is **historical**:
> it describes the original single page and is kept as a record of the starting point.
> **Still planned:** burned-in subtitles and posting to TikTok/Instagram/Facebook. YouTube posting exists but has
> only been tested against a stand-in for YouTube, not the real service.

Legend used throughout:
- **BUILT** = exists and works today (the labels below are the exact words on screen).
- **PROPOSED** = not built yet; described so it can be designed now. Each says what the server already
  supports and what would need building.

---

## 0. Paste-ready design prompt

> Design the web interface for **Clipper**, a single-user tool that runs on the owner's laptop and turns
> GTA VI videos into short social clips. The owner (a busy student, 30 minutes a day, not a developer)
> pastes a YouTube/Twitch/Kick link or follows channels; the app analyzes each video in the background,
> picks up to 25 thirty-second "moments", scores each 0-10 with an AI (with a one-line reason, a title,
> a hook and a caption with hashtags), and cuts approved moments into a 9:16 and a 16:9 clip. The owner's
> real job is **reviewing moments: approve, reject, or tweak the title/hook/caption/trim**, then
> downloading clips and copy-pasting captions to post on YouTube Shorts, Instagram Reels, TikTok and
> Facebook. Later they enter each clip's views so the scoring learns.
> **The review step must be the fastest and calmest part**: one moment at a time, big preview, big
> Approve/Reject, keyboard shortcuts, AI score and reason always visible. Background work (analysis about
> 1-5 min, AI scoring 1-2 min, cutting about 3 min per clip, one at a time) must feel safe: clear
> statuses, progress, never a frozen button. Errors are plain sentences with a next step. Light and dark
> themes, responsive down to a phone, fully keyboard-accessible. Screens: **Today** (inbox),
> **Review**, **Ready to post**, **Videos**, **Channels**, **Performance**, **Settings & health**.
> Tone: calm, direct, a little gaming-flavoured but not noisy. Use the real data and copy in this
> document.

---

## 1. Global

### 1.1 Page frame (BUILT)
- One page, content column max width **1100 px**, centred, 16 px padding.
- Title: **"Clipper — review"** (H1, 22 px).
- Order top to bottom: title -> *Analyze a video* panel -> tabs -> (bulk bar) -> (messages) -> moment cards.
- Follows the system light/dark setting (no manual toggle yet; PROPOSED: add one).

### 1.2 Visual tokens in use today (a starting point, not a requirement)
| Token | Light | Dark | Used for |
|---|---|---|---|
| background | `#f6f7f9` | `#14161a` | page |
| panel | `#ffffff` | `#1d2026` | cards, buttons |
| text | `#14171c` | `#e8eaed` | body |
| muted | `#6a7280` | `#98a1ae` | helper text, labels |
| line | `#e2e5ea` | `#2c313a` | borders |
| accent | `#3b5bdb` | same | primary buttons, active tab |
| good | `#2f9e44` | same | Approve |
| bad | `#e03131` | same | Reject, errors |
| warn | `#f08c00` | same | (defined, unused) |
- Type: system sans, 15 px body, 12 px labels, 13 px helper text. Radii 8 px (controls) and 12 px (cards).

### 1.3 Server connection banner (BUILT)
If the server can't be reached, a red line replaces the content:
**"Can't reach the backend (<reason>). Is it running on port 8000?"**
PROPOSED: a persistent connection indicator, with a friendlier first-run explanation.

---

## 2. Region: "Analyze a video" panel (BUILT, top of page)

Purpose: send one video to the pipeline by pasting its link.

| Element | Exact text | Notes |
|---|---|---|
| Label | **Analyze a video: paste a YouTube, Twitch or Kick link** | |
| Link field | placeholder `https://www.youtube.com/watch?v=…` | single line; Enter submits |
| Type picker | options: **type: channel default**, **gameplay**, **news**, **commentary** | decides how the AI judges this video |
| Button | **Analyze** (while sending: **Reading link…**) | disabled while sending or if the field is empty |

Behaviour: link is read in a few seconds; analysis then runs in the background. The field clears on
success unless the owner already typed something new meanwhile.

Messages below the panel:
| Situation | Exact text | Style |
|---|---|---|
| Queued | `Added "<title>". Analysis takes a few minutes; its moments will appear under "To review".` | muted |
| Already added | `"<title>" is already added (<status>).` | muted |
| Empty link | `paste a video link` | error |
| Not a web link | `that doesn't look like a web link (it must start with http:// or https://)` | error |
| Unsupported site | `unsupported site: '<url>' (supported: kick, twitch, youtube)` | error |
| Channel/playlist | `that link is a channel or playlist, not a single video (to track a whole channel, add it as a streamer instead)` | error |
| Stream not over | `that stream hasn't finished yet; try again once it has ended` | error |
| Unreadable | `couldn't read that link: <reason from the site>` | error |
| No usable video | `couldn't find a usable video at that link` | error |
| Network problem | `couldn't reach the site to read that link; try again in a moment` | error |
| Status can't be read (after 3 tries) | `Can't read video status (<reason>). Still retrying.` | error |

---

## 3. Region: recent videos list (BUILT, under the panel)

Shows the **6 most recent videos** (the server can return up to 100). One row each:

`[status badge]  <video title>  · <channel> · <N moments, only when analyzed>  · <note or error>   [Full video control]`

### 3.1 Video status badge
| Badge text | Meaning | Suggested treatment |
|---|---|---|
| `new` | Registered, waiting; nothing running (it needs the command-line `process`, or pasting again) | neutral |
| `queued` | About to be analyzed | in progress |
| `processing` | Being analyzed now | in progress, animated |
| `analyzed` | Done; moments are ready | success |
| `failed` | Went wrong; reason shown; retry by pasting the link again | error |

The page **polls every 4 seconds** while any video is `queued`/`processing` or a full download is
running, and reloads the review list automatically when one finishes.

### 3.2 Notes and errors shown on a row
- `AI scoring incomplete (<ErrorType>); run judge again later` (some moments unscored)
- `AI scoring incomplete (N moments unscored); run judge again later`
- `AI scoring couldn't be verified; run judge again later`
- `its analysis stopped unexpectedly; paste the link again or run recover, then retry`
- Failure reason from the pipeline, for example `RuntimeError: audio download failed`

### 3.3 "Full video" control (BUILT; opt-in because a full video can be several GB)
| State | What is shown | Buttons |
|---|---|---|
| none | **⬇ Full video (1:15)** (length as h:mm). Tooltip: "Downloads the whole original video (up to 1080p). A long stream can be several GB." | the button starts it |
| queued | badge **waiting to download…** | none |
| downloading | badge **downloading 42%** (progress never goes backwards, max 99% until finished) | none |
| done | **⬇ Save full video (210 MB)** (a download link) | **Delete copy** (tooltip: "Deletes the downloaded file to free disk space. Your clips are unaffected.") |
| failed | red reason, for example `OSError: not enough disk space: about 12.8 GB needed (including a 2 GB safety margin), 5.0 GB free` | **Retry** |

The saved file is named from the video title, for example
`GTA-6-PS5-Pro-Gameplay---15-Minutes-of-The-First-Mission.mp4`.

Server messages that can appear: `the full video is already downloading or downloaded`,
`the file is gone; download it again`, `there is no downloaded copy`.

---

## 4. Region: tabs and bulk bar (BUILT)

### 4.1 Tabs
Three buttons: **To review** (default), **Approved**, **Rejected**. The active tab is filled with the
accent colour. A moment moves between tabs when its decision changes. (PROPOSED: show a count on each
tab, for example "To review (5)".)

### 4.2 Bulk bar (Approved tab only, when something still needs cutting)
- Button: **Render all approved (N)**, N = approved moments not yet cut (or whose cut failed).
- Helper text: *They are cut one after another, so this can take a while.*
- Error: `Couldn't start the renders: <reason>`.

### 4.3 List states
| State | Text |
|---|---|
| loading | `Loading…` |
| empty | `Nothing here yet.` (PROPOSED: tab-specific empty states, see section 9) |

---

## 5. Component: moment card (BUILT)

A two-column card (stacks on narrow screens): **preview on the left**, **details and actions on the right**.
This is the core of the product.

### 5.1 Left column: preview
Exactly one of these shows:

| When | What |
|---|---|
| Clip has been cut | A **vertical video player** with controls, then the downloads row (5.4) |
| Video is YouTube, not cut | A still image of the video with a button **▶ Preview at 10:00**; clicking loads the real YouTube player at that moment (autoplay, plays only the moment) |
| Video is Twitch or Kick, not cut | A dashed box: *No embedded preview for twitch.* + link **Open the video** + *Jump to 10:00* |

(The preview is click-to-load on purpose: dozens of live players on one page overload the browser.)

### 5.2 Right column, top: badges and context
- Badge **AI 7.0** (the 0-10 score; **AI —** if not scored yet)
- Badge with the video type: **gameplay** / **news** / **commentary**
- Badge **signal 0.44** (how strong the audio/chat/keyword signals were; a technical number: consider hiding it behind a "details" toggle)
- Muted text: `<source video title> · 11:41–12:11`
- Below: the **AI reason**, one sentence, for example *"The action is focused and provides a clear objective, making it engaging for viewers."*

### 5.3 Right column: editable fields
| Label | Control | Limits | Notes |
|---|---|---|---|
| **Title** | single line | up to 200 characters stored; AI aims for 70 | |
| **Hook** | single line | up to 300 stored; AI aims for 100 | the first line a viewer should see/hear |
| **Caption** | multi-line (3 rows) | no hard limit; each platform trims on export | includes the hashtags |
| **Start (seconds)** | number | at least 0 | trim |
| **End (seconds)** | number | clip must be at least 3 seconds long | trim |

Error from the server on a bad trim: `clip must be at least 3 seconds and start at or after 0`.
(PROPOSED: show times as mm:ss, add a visual trim bar over a thumbnail strip, and show the
**transcript** of the moment. The transcript is already available from the server but is not displayed.)

### 5.4 Downloads row (shown once the clip is cut)
| Control | Exact text | Result |
|---|---|---|
| Link (primary style) | **⬇ Download 9:16** | saves `<title>-9x16.mp4` |
| Link | **⬇ Download 16:9** | saves `<title>-16x9.mp4` |
| Button | **Copy caption** | copies title + blank line + caption; falls back to a selectable text box with *"Couldn't copy automatically. Select and copy this text:"* |
| Helper | *9:16 for Shorts, Reels and TikTok · 16:9 for YouTube* | |

### 5.5 Action row (buttons appear or hide by state)
| Button | Visible when | Disabled when | What it does |
|---|---|---|---|
| **Save changes** (primary) | any field was edited | while busy | saves title/hook/caption/trim; if an approved clip's trim changed, it is re-cut automatically |
| **Approve** (green) | not already approved | while busy | marks approved **and cuts the clip automatically** |
| **Reject** (red) | not already rejected | while busy | moves it to Rejected (never cut) |
| **Back to review** | not pending | while busy | returns it to To review |
| **✂ Cut clip** / **Re-cut** | not rejected and not currently cutting | while busy or there are unsaved edits | cuts (or re-cuts) without approving |
| *badge* **cutting the clip…** | while cutting | | cuts take about 3 minutes, one at a time |
| **Export for platforms** / **Re-export** (primary) | approved **and** cut | while busy or unsaved edits | builds a folder per platform (5.6) |

Cut failure shows: `Render failed: <reason>` (red). Other errors show as a red line under the buttons.
Server messages: `already rendering`, `this clip hasn't been rendered yet`,
`the clip's file is missing; render it again`.

### 5.6 Export panel (after "Export for platforms")
One row per platform: **name**, **⬇ video** (download that platform's file), **Copy text**
(that platform's title + caption, trimmed to its rules), and a red warning if the clip breaks a rule
(for example `clip is 75s, over the 60s limit`; that 60 s is Clipper's own limit for the vertical formats,
not YouTube's rule: YouTube decides Shorts eligibility itself, and a longer vertical upload may still qualify on a
standard channel). Footer: *Files are also saved in the exports folder
inside the backend's data directory.*

| Platform row | Video | Title | Text limit | Hashtag limit | Max length |
|---|---|---|---|---|---|
| YouTube Shorts | 9:16 | up to 100 chars | 5000 | 15 (adds `#Shorts`) | 60 s (Clipper's own export limit; see note) |
| YouTube (16:9) | 16:9 | up to 100 chars | 5000 | 15 | 12 hours |
| Instagram Reels | 9:16 | none (hook leads the caption) | 2200 | 30 | 60 s |
| TikTok | 9:16 | none (hook leads the caption) | 2200 | 10 | 60 s |
| Facebook Reels | 9:16 | none (hook leads the caption) | 2200 | 30 | 60 s |

The hashtag limit covers all hashtags in the final text, including any in the hook.
Export errors: `clip must be approved and rendered before export`, `not exported yet`.
Lookup error: `Couldn't check for an earlier export (<reason>)` with a **Retry** button.

### 5.7 "Results after posting" (approved moments only)
Heading **Results after posting**; helper: *Enter these once the clip has been up a while. They teach
the scoring (run `tune`).* (PROPOSED: replace the jargon with *"They help the app learn what works."*
and add a **Performance** screen.)
- **Views** (number, whole, 0 or more) and **Likes** (same). **Save results** appears when changed.
- Emptying a field clears the saved number. Error: `Views must be a whole number, 0 or more`
  (or `Likes ...`).

---

## 6. Catalogues

### 6.1 Moment states
| Decision (tab) | Cut state | Meaning |
|---|---|---|
| pending | none | Suggested by the app, not cut |
| pending / approved | rendering | Being cut now |
| pending / approved | done | Cut; plays in the card; downloads available |
| pending / approved | failed | Cut failed; reason shown; Cut clip retries |
| rejected | none | Hidden. **The app auto-rejects anything the AI scores below 6**, so this tab holds real candidates the owner may want to rescue |

### 6.2 Everything a moment carries (the data available to design with)
`id`, source video (title, link, platform), type, start and end time, AI score, "signal" score and its
three parts (**audio** loudness, **chat** activity, **keywords** in the transcript), title, hook,
caption, AI reason, **transcript** (the words spoken in the moment), decision, cut state and error,
clip file links (vertical and wide), views, likes.

### 6.3 Every button, in one list
Analyze · ▶ Preview at mm:ss · Save changes · Approve · Reject · Back to review · ✂ Cut clip · Re-cut ·
⬇ Download 9:16 · ⬇ Download 16:9 · Copy caption · Export for platforms · Re-export · ⬇ video (per
platform) · Copy text (per platform) · Save results · Render all approved (N) · ⬇ Full video ·
⬇ Save full video · Delete copy · Retry (full download) · Retry (export lookup) · Open the video ·
tabs: To review / Approved / Rejected.

---

## 7. Realistic sample data (use this in mockups)

**Videos**
| Title | Channel | Platform | Length | Status | Moments |
|---|---|---|---|---|---|
| GTA 6 PS5 Pro Gameplay - 15 Minutes of The First Mission | IGN | YouTube | 14:51 | analyzed | 17 |
| GTA 6 Live AMA With Our Reviewer Who Played 120hrs | IGN | YouTube | 3:05:16 | processing | - |
| GTAVI Countdown | Twitch | Twitch | 8:21:02 | queued | - |

**Moments** (scores, titles and hooks are real, from the first video above; text marked * is
illustrative, written to fill the gaps, so replace it if you want only real content)
| AI | Title | Hook | Caption | Reason |
|---|---|---|---|---|
| 8.0 | GTA VI Intense Hostage Rescue | Come untie me, man! You going to get me out of this or what? | A tense rescue in a dark barn. Can they get out in time? #GTA6 #Gameplay #Rescue * | Clear stakes and strong dialogue make this a good standalone clip. * |
| 7.0 | GTA VI Trailer Heist | They got the dash in the trailer, man. Knock this down! | The heist is on and things are getting messy. Grab the cash and get out! #GTA6 #Heist #Gaming #GTA6Gameplay | The action is focused and provides a clear objective, making it engaging for viewers. |
| 6.0 | GTA VI Moral Dilemma | They got Donnie. I don't want to get involved with those guys, man. | Do you help or walk away? #GTA6 #Story * | Good character moment but little visual payoff. * |
| 5.0 | GTA VI Delivery Mission | (auto-rejected: under 6) | | |

Times: moments are about 30 seconds long, for example 10:00-10:30 and 11:41-12:11.

---

## 8. Key flows, step by step

**A. Daily review (the 30 minutes)**
1. Open the app. See what's waiting (PROPOSED: Today screen; today: the To review tab).
2. For each moment: watch the preview, read AI score and reason, optionally edit title/hook/caption/trim.
3. **Approve** (it starts cutting) or **Reject**.
4. When cuts finish, go to Approved: **Download 9:16**, **Copy caption**, post on the platform by hand.
5. Days later: enter **Views** and **Likes**.

**B. Add a video**: paste link -> choose type (optional) -> Analyze -> see the row go
queued -> processing -> analyzed -> moments appear in To review.

**C. Post a clip**: Approved tab -> Download 9:16 -> Copy caption -> upload on the platform ->
(later) Export for platforms for per-platform text and limits.

**D. Get a whole video**: on its row, **⬇ Full video** -> watch % -> **⬇ Save full video** -> optionally
**Delete copy** to free space.

**E. Rescue an auto-rejected moment**: Rejected tab -> **Back to review** (or **Cut clip** to see the real clip first).

**F. Recover from a failure**: a `failed` video -> paste its link again; a failed cut -> **Cut clip**;
a failed full download -> **Retry**.

---

## 9. Screens proposed in the original brief (since built, except direct posting)

For each: purpose, content, actions, states, and what the server supports today.

### 9.1 Today (home / inbox)
- **Purpose:** start the daily session in one glance.
- **Content:** big number **"5 moments to review"** with a **Start reviewing** button; row of status
  tiles: *Analyzing (2)*, *Cutting (1, about 3 min left)*, *Ready to post (4)*, *Needs attention (1)*;
  a compact **Analyze a video** box; recent videos.
- **States:** first run (*"Paste a link to get started"*), all clear (*"You're all caught up"*), something failed.
- **Server:** counts can be built from existing endpoints (moments by decision, videos by status).

### 9.2 Review (one moment at a time)
- **Purpose:** the core job, as fast as possible.
- **Layout idea:** large preview on the left (vertical 9:16 if cut, otherwise the YouTube moment),
  details on the right: AI score and reason, **transcript** of the moment, title / hook / caption,
  trim control, then very large **Approve** and **Reject**, plus **Skip** and **Edit**.
- **Header:** progress *"7 of 12"*, source video title, type badge.
- **Shortcuts (proposed):** `A` approve, `R` reject, `S` skip, `E` edit, `←` `→` previous/next, `Space` play/pause, `?` help.
- **States:** loading, nothing to review, finished (*"All reviewed. 6 approved, 4 rejected. Cuts will be ready in about 12 minutes."*).
- **Server:** everything needed exists, including the transcript.

### 9.3 Ready to post
- **Purpose:** the hand-off to the platforms.
- **Content:** a list or grid of approved, cut clips with a small vertical player, **Download 9:16**,
  **Download 16:9**, **Copy caption**, and per-platform export (**Export for platforms**). Filters:
  *Not posted yet / Posted / All*. A **Mark as posted** action and, after a few days, a **Results** prompt.
- **States:** clip still cutting (progress), cut failed (reason + retry), nothing ready.
- **Server:** downloads, caption copy, export, "Mark as posted" and posted dates all exist now.

### 9.3b Posting directly (YouTube built, untested against the real service; Schedule not built)
- **Purpose:** publish without leaving the app. Start with YouTube Shorts.
- **Content:** **Connect YouTube account** (one-time sign-in), then **Post now** / **Schedule** per clip,
  and a status per clip (*Uploading… / Posted / Failed: reason*). Respect daily upload limits.
- **Server:** not built; needs a Google sign-in setup by the owner.

### 9.4 Videos
- **Purpose:** every video, not just the latest six.
- **Content:** table or cards with title, channel, platform, length, status, number of moments, scored
  vs unscored, disk used (clips and any full copy), and per-row **Full video**, **Delete copy**,
  **Re-analyze**, **Remove**; a **Paste a link** box; filters by status, channel and platform.
- **States:** empty, failed rows with reasons, a banner when disk is low.
- **Server:** list and full-video controls exist (up to 100). **Re-analyze, remove and disk-usage totals need building.**

### 9.5 Channels
- **Purpose:** choose who the app follows automatically.
- **Content:** list of followed streamers/channels with platform, **type** (gameplay/news/commentary),
  last checked, videos found; **Add a channel** (paste a channel URL + choose type); **Follow / Stop
  following**; a **Check now** button; and an **Automatic mode** switch (*"Check every hour and analyze new videos"*).
- **States:** none followed yet, a check that failed (reason), a channel found by two spellings (merged).
- **Server:** add, list and scan exist. **Stop following, last-checked time and automatic mode need building.**

### 9.6 Performance (the learning loop)
- **Purpose:** show what actually performed and let the app improve its picks.
- **Content:** table of posted clips with views and likes; **Enter views** inline; a short plain
  explanation such as *"Clips with lots of chat activity got 2.4x more views"*; the signals compared
  (**Audio**, **Chat**, **Keywords**, **AI score**) with a correlation indicator; current weights vs
  suggested; **Apply suggestion** and **Reset to defaults**.
- **Rules to explain in the UI:** nothing is suggested until at least **15 clips** have views; each
  signal needs about 10 clips that have it; weights stay between 0.1 and 3; enter views after a similar
  waiting period for every clip.
- **Server:** the analysis exists as a command-line command only; **endpoints need building**.

### 9.7 Settings & health
- **Purpose:** set it up once and know it's working.
- **Health checks:** server running; AI key set and working (and which model is in use, with
  *"using the backup model"* when the main one is busy); video tools installed (ffmpeg, yt-dlp);
  free disk space; storage used by clips, exports and full downloads.
- **Settings:** auto-reject threshold (default **6**), moments per video (**25**), moment length
  (**30 s**), minimum spacing (**45 s**), how much the AI/chat/keyword/loudness signals count, whether to
  delete working files after analysis (default on), full-download safety margin (**2 GB**).
- **Actions:** **Clean up old files**, **Recover stalled items**.
- **Server:** settings live in a config file today; **a settings API and health endpoint need building.**

---

## 10. Empty, loading, error and success states to design (all screens)

| Situation | Suggested message |
|---|---|
| First ever visit | *"Paste a YouTube, Twitch or Kick link to find your first clips."* |
| Nothing to review | *"You're all caught up. New moments appear here when a video finishes analyzing."* |
| Nothing approved | *"Approved moments show up here, ready to download."* |
| Nothing rejected | *"Moments the AI scores under 6 land here. You can rescue any of them."* |
| Analysis running | *"Analyzing — about 3 minutes left. You can close this page; it keeps going."* |
| Scoring partly failed | *"Scored 20 of 25. The AI service was busy; the rest can be scored later."* (with a **Score the rest** button) |
| Cut queued behind others | *"Waiting to be cut — 2 ahead of this one."* |
| Server unreachable | *"Can't reach the app's server. Make sure it's running, then try again."* |
| Success toasts | *"Copied"*, *"Saved"*, *"Approved — cutting now"*, *"Clip ready to download"* |

---

## 11. Responsive, accessibility and polish

- **Breakpoints:** the card stacks to one column at **720 px**; design phone (review on the couch),
  tablet and laptop.
- **Touch:** minimum 44 px targets; Approve/Reject within thumb reach on a phone.
- **Keyboard:** everything reachable; visible focus ring; shortcut help.
- **Screen readers:** every field has a label (already done); previews need descriptive names;
  statuses must not rely on colour alone (use icon + text).
- **Motion:** gentle progress indicators for background work; respect "reduce motion".
- **Theme:** light and dark, plus a manual switch.
- **Performance:** a page can hold dozens of moments; keep previews click-to-load, lazy-load images.

---

## 12. Constraints a design should respect

- Everything runs on one laptop for one user: no sign-in screens, no sharing.
- **Cuts are slow (about 3 minutes each) and run one at a time**: design for waiting, batch approval
  and "come back later". Never block the review flow while a cut runs.
- The AI is **text-only** today and sometimes unavailable; scores can be missing. Always handle "unscored".
- Long jobs survive closing the browser; they do **not** survive stopping the server (they become
  "failed, retry").
- The server is at `http://localhost:8000`; the interface at `http://localhost:5173` in development.
