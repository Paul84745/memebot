"""Matches the Reels' music to what is trending on Reels/TikTok right now.

Instagram's API can't attach songs from Instagram's music library ("music tagging is only
available for original audio"), and putting the actual copyrighted trending songs in the video
gets Reels muted or the page flagged. So instead the bot follows the STYLE of what is trending -
the genre and the edit (sped up, slowed + reverb) - using royalty-free music, and learns which
styles bring the most shares and follows.

Every few days it reads this week's headlines about trending sounds, has the AI turn them into
music styles, and keeps a list of those. If that fails it falls back to styles that are always
popular on Reels.
"""
import random
import re
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import requests

REFRESH_DAYS = 3
MOODS = ["chill", "upbeat", "bouncy", "hype", "sad", "wholesome", "awkward", "dramatic"]
FX = ["none", "sped_up", "slowed_reverb"]
QUERIES = [
    '"trending audio" instagram reels',
    '"trending sounds" tiktok',
    '"viral song" tiktok',
    '"trending songs" reels',
    "tiktok sound trend this week",
]

# Evergreen Reels styles, used until/unless fresh trend data is available.
DEFAULT_STYLES = [
    {"query": "phonk", "moods": ["hype", "dramatic"], "fx": "none"},
    {"query": "funk", "moods": ["upbeat", "bouncy", "hype"], "fx": "sped_up"},
    {"query": "lofi", "moods": ["chill", "sad", "wholesome"], "fx": "slowed_reverb"},
    {"query": "piano", "moods": ["sad", "dramatic", "wholesome"], "fx": "slowed_reverb"},
    {"query": "electronic", "moods": ["hype", "upbeat"], "fx": "sped_up"},
    {"query": "jazz", "moods": ["awkward", "bouncy", "chill"], "fx": "none"},
    {"query": "orchestral", "moods": ["dramatic"], "fx": "none"},
    {"query": "acoustic", "moods": ["wholesome", "chill"], "fx": "none"},
]

PROMPT = """Below are this week's news headlines about songs and sounds trending on TikTok and
Instagram Reels. Work out the MUSIC STYLES that are trending (genre, tempo, production, edit) -
not specific songs or artists. For each style give:
- "query": a 1-2 word search term for a royalty-free music library, a plain genre or instrument
  (examples: phonk, jersey club, afrobeats, house, drill, rnb, synthwave, piano, orchestral, funk,
  lofi, country, rock, reggaeton, ambient). Never a song title or artist name.
- "moods": which of these meme moods it suits: {moods}
- "fx": "sped_up" if the trend is sped-up versions, "slowed_reverb" if slowed + reverb, else "none".
Give 4 to 8 styles, most clearly trending first. If the headlines say little, give the styles that
are most popular on Reels right now in general.

Headlines:
{headlines}

Return JSON only: {{"styles": [{{"query": "...", "moods": ["..."], "fx": "none"}}]}}"""


def _headlines():
    out, seen = [], set()
    for q in QUERIES:
        url = (f"https://news.google.com/rss/search?q={quote_plus(q + ' when:7d')}"
               "&hl=en-US&gl=US&ceid=US:en")
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (meme-bot)"})
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"trend feed failed: {e}")
            continue
        for it in root.iter("item"):
            t = re.sub(r"\s+-\s+[^-]{2,40}$", "", (it.findtext("title") or "").strip())
            if 15 <= len(t) <= 200 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out[:45]


def _valid(styles):
    good = []
    for s in styles:
        q = re.sub(r"[^a-z0-9 &-]", "", str(s.get("query", "")).lower()).strip()
        moods = [m for m in (s.get("moods") or []) if m in MOODS]
        fx = s.get("fx") if s.get("fx") in FX else "none"
        if 2 <= len(q) <= 24 and len(q.split()) <= 2 and moods and q not in {g["query"] for g in good}:
            good.append({"query": q, "moods": moods, "fx": fx})
    return good[:8]


def refresh(state, llm, items, now):
    st = state.get("trends") or {}
    if st.get("fetched_at") and now - datetime.fromisoformat(st["fetched_at"]) < timedelta(days=REFRESH_DAYS):
        return
    heads = _headlines()
    styles = []
    if len(heads) >= 5:
        try:
            raw = llm(PROMPT.format(moods=", ".join(MOODS), headlines="\n".join(f"- {h}" for h in heads)),
                      temperature=0.3, max_tokens=1200)
            styles = _valid(items(raw, "styles"))
        except Exception as e:
            print(f"::warning::trend extraction failed: {str(e)[:200]}")
    if len(styles) >= 3:
        # keep a couple of evergreen styles in the mix so every mood always has an option
        have = {s["query"] for s in styles}
        styles += [s for s in DEFAULT_STYLES if s["query"] not in have][:3]
        source = f"{len(heads)} headlines"
    else:
        styles, source = (st.get("styles") or DEFAULT_STYLES), "evergreen defaults"
    state["trends"] = {"fetched_at": now.isoformat(), "styles": styles, "source": source}
    print(f"::notice::trending music styles ({source}): "
          + ", ".join(f"{s['query']}{'' if s['fx'] == 'none' else ' ' + s['fx']}" for s in styles))


def pick_style(state, mood, history):
    """A trending style that suits the meme's mood. About 1 in 5 Reels use plain mood music
    instead, so the bot can keep measuring whether following trends actually pays off."""
    styles = (state.get("trends") or {}).get("styles") or DEFAULT_STYLES
    fits = [s for s in styles if mood in s["moods"]]
    if not fits or random.random() < 0.2:
        return None
    done = [h for h in history if h.get("final") and h.get("score") is not None and h.get("music_style")]
    if len(done) < 8 or random.random() < 0.3:
        # earlier entries are the most clearly trending ones; favour them a little
        return random.choices(fits, [1.0 / (1 + 0.3 * i) for i in range(len(fits))])[0]
    by = {}
    for h in done:
        by.setdefault(h["music_style"], []).append(h["score"])
    overall = statistics.mean(h["score"] for h in done)
    w = [(statistics.mean(by[s["query"]]) if len(by.get(s["query"], [])) >= 2 else overall) + 1 for s in fits]
    return random.choices(fits, w)[0]


def report(state, done):
    lines = []
    st = state.get("trends") or {}
    if st.get("styles"):
        lines += [f"## Trending music styles right now ({st.get('source')}, {st['fetched_at'][:10]})",
                  "- " + ", ".join(f"{s['query']}{'' if s['fx'] == 'none' else ' (' + s['fx'].replace('_', ' ') + ')'}"
                                   for s in st["styles"])]
    by = {}
    for h in done:
        if h.get("kind") == "reel" and h.get("music"):
            by.setdefault(h.get("music_style") or "plain mood music", []).append(h["score"])
    if by:
        lines += ["- results by sound style:"] + [
            f"  - {k}: avg {statistics.mean(v):.1f} ({len(v)} posts)"
            for k, v in sorted(by.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)]
    return lines + ([""] if lines else [])
