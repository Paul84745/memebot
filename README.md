# Meme bot: posts 5 memes a day to Instagram, free

You do the setup once (about 20 minutes). After that it runs by itself.
Every ~60 days you click one button to renew the Instagram token (see the end).

## Step 1: Instagram
Your Instagram must be a Professional account (Creator is fine). Instagram > Settings > Account type and tools.
No Facebook Page needed.

## Step 2: Gemini key (free, writes the jokes)
Go to aistudio.google.com/apikey > Create API key > copy it.

## Step 3: Instagram token (from your Meta app)
1. developers.facebook.com/apps > open your app.
2. Left menu: **Use cases** > next to the Instagram use case click **Customize**.
3. Click **API setup with Instagram login**.
4. Under **Generate access tokens** click **Add account**, log in to your Instagram, accept.
   (If it says you must accept a tester invite: Instagram app/website > Settings > Apps and websites > Tester invites > Accept.)
5. Click **Generate token** next to your account. Copy two things:
   - the long token  -> this is `IG_ACCESS_TOKEN`
   - the Instagram account ID (a number) -> this is `IG_USER_ID`

## Step 4: GitHub
1. github.com > New repository > name it anything > set it to **Public** > create.
2. Click **uploading an existing file**, drag in everything from this unzipped folder (include the hidden `.github` folder), commit.
   Tip: easier is to drag the whole folder contents from Finder with hidden files shown (Cmd+Shift+.).
3. Settings > Secrets and variables > Actions > New repository secret. Add three:
   `IG_USER_ID`, `IG_ACCESS_TOKEN`, `GEMINI_API_KEY`.

## Step 5: Test
Actions tab > meme-bot > Run workflow. Leave "dry_run" ticked, run it, open the run and download the **preview** file to see a meme.
Run again with dry_run **off** and force **on** to publish one real post. After that it posts by itself.

## Every ~60 days
Repeat Step 3 (click Generate token again), then paste the new token into the `IG_ACCESS_TOKEN` secret.
If posts stop, GitHub emails you about the failed run. That email means: renew the token.

## How it picks what to post
- Each post: the AI writes 12 candidate memes, a harsh judge scores them all ("would a stranger DM this to a friend?"), and only the best one that passes the safety check is posted (minimum score 7/10).
- Learning ranks posts by engagement per 1,000 people reached, with DM sends counted 8x a like. See `INSIGHTS.md`.
- Reels are 7-second loops with the text readable from the first frame.
- Wednesdays and Sundays, the best-timed slot becomes a carousel "meme dump" (8 memes). To preview one: Run workflow with dry_run + dump ticked.
- News: light headlines from the last 24h, freshest first; very fresh stories get a ranking boost.

## Tweaks (optional)
Optional settings (add under `env:` in the workflow): `MIN_FUNNY`, `CANDIDATES`, `DUMP_DAYS` (0=Mon … 6=Sun), `DUMP_SIZE`, `REEL_SECONDS`, `NEWS_MAX_AGE_H`.
- Posts per day, posting hours: top of `bot/main.py`.
- If a template looks wrong, add its name to `EXCLUDE_REGEX` in `bot/main.py`.
