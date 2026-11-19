# Posting to YouTube: one-time setup

Clipper can upload a clip to your YouTube channel with one click, and read its views back. Google
requires you to create a small "app" for that (free, about 10 minutes). Only you can do this part.

## Steps

1. Open https://console.cloud.google.com and sign in with the Google account that owns your YouTube channel.
2. Create a **new project** (any name, for example "Clipper").
3. Go to **APIs & Services > Library**, search for **YouTube Data API v3** and press **Enable**.
4. Go to **APIs & Services > OAuth consent screen**:
   - User type: **External**. Fill the required fields (app name, your email).
   - Under **Test users**, **add your own Google account**. (Without this, sign-in is refused.)
5. Go to **APIs & Services > Credentials > Create credentials > OAuth client ID**:
   - Application type: **Desktop app**. Create it, then **Download JSON**.
6. Save that file exactly as `backend\data\youtube\client_secret.json` (create the `youtube` folder if needed).
7. In Clipper open **Settings & health > YouTube** and press **Connect YouTube**. Sign in in the window that opens
   and allow access. (Google will warn the app is unverified: choose **Advanced > Go to Clipper (unsafe)**. It is
   your own app, running on your own computer.)

## What to expect

- **Uploads are private by default.** Google also forces videos uploaded by a brand-new, unaudited app to stay
  private whatever you ask for. So the flow is: Post to YouTube, then open the video in YouTube Studio, check it,
  and press Publish yourself. (To remove that limit, Google runs a compliance audit for apps; not needed for personal use.)
- **A daily allowance.** YouTube gives each app a daily quota and an upload uses a large part of it, so only a few
  uploads fit in a day. If it runs out, the clip shows a plain message and you can retry tomorrow.
- **Sign-in lasts about a week** while the Google project is in "Testing" mode. When it expires, press Connect again.
- **Views come back automatically** (every automatic check, or press **Fetch views now**). They feed the
  Performance section, so the scoring can learn without you typing numbers.
- Nothing is ever uploaded without you pressing **Post to YouTube** on a clip you approved and cut.

## Honest status

This was built and tested against a stand-in for YouTube's API (upload, progress, errors, quota messages,
view counts), but **not yet against the real YouTube**, because that needs your Google sign-in. The first real
upload is the real test: if anything behaves differently from the description above, the clip shows the exact error.
