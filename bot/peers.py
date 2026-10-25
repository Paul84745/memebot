"""Learns from other public meme pages (Instagram business discovery: public captions,
likes, comments, timestamps). Only reads public data; never copies anyone's content."""
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUBS = ["memes", "dankmemes", "me_irl", "wholesomememes", "funny", "memeeconomy"]
REFRESH_DAYS = 2


def refresh(state, graph, ig_user_id, now):
    """Instagram's API (Instagram Login) cannot read other accounts, so the largest open source
    of 'what memes work right now' is used instead: this week's top posts on Reddit meme
    communities, scored relative to each community's own average."""
    import requests
    st = state.setdefault("peers", {})
    last = st.get("fetched_at")
    if last and now - datetime.fromisoformat(last) < timedelta(days=REFRESH_DAYS):
        return
    posts, ok, err = [], [], ""
    for sub in SUBS:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json",
                             params={"t": "week", "limit": 60, "raw_json": 1},
                             headers={"User-Agent": "Mozilla/5.0 (compatible; meme-bot/1.0)"}, timeout=20)
            r.raise_for_status()
            kids = [k["data"] for k in r.json()["data"]["children"]]
        except Exception as e:
            err = f"{sub}: {str(e)[:120]}"
            continue
        rows = []
        for k in kids:
            if k.get("over_18") or not (k.get("title") or "").strip():
                continue
            rows.append({"u": sub, "er": k["score"] + 3 * k.get("num_comments", 0),
                         "cap": k["title"][:220], "t": datetime.fromtimestamp(
                             k["created_utc"], timezone.utc).isoformat(), "type": "IMAGE"})
        if len(rows) >= 10:
            avg = statistics.mean(x["er"] for x in rows) or 1
            for x in rows:
                x["rel"] = round(x["er"] / avg, 3)
            posts += rows
            ok.append(sub)
    print(f"::warning::peers: {len(ok)} communities readable {ok}" + (f"; last error {err}" if err else ""))
    st["fetched_at"] = now.isoformat()
    if not posts:
        return
    st.update({
        "ok": ok, "n": len(posts),
        "best": sorted(posts, key=lambda p: p["rel"], reverse=True)[:25],
        "worst": sorted(posts, key=lambda p: p["rel"])[:15],
        "by_len": _bucket(posts, lambda p: _lenbucket(len(p["cap"]))),
        "by_start": _bucket(posts, lambda p: _startbucket(p["cap"])),
    })


def _startbucket(cap):
    c = cap.lower().strip()
    for k in ("when ", "me ", "pov", "how ", "nobody", "my "):
        if c.startswith(k):
            return f"starts with '{k.strip()}'"
    return "other opening"


def _tagbucket(n):
    return "0 hashtags" if n == 0 else "1-3 hashtags" if n <= 3 else "4-8 hashtags" if n <= 8 else "9+ hashtags"


def _lenbucket(n):
    return "very short caption" if n < 30 else "short caption" if n < 90 else "long caption"


def _bucket(posts, fn):
    by = defaultdict(list)
    for p in posts:
        by[fn(p)].append(p["rel"])
    return {k: round(statistics.mean(v), 2) for k, v in by.items() if len(v) >= 5}


def hint(state):
    """Prompt text describing what works / flops on other meme pages (empty if no data)."""
    st = state.get("peers") or {}
    if not st.get("best"):
        return ""
    best = [p["cap"].replace("\n", " ")[:120] for p in st["best"][:8] if p["cap"].strip()]
    worst = [p["cap"].replace("\n", " ")[:120] for p in st["worst"][:5] if p["cap"].strip()]
    s = "LESSONS FROM THIS WEEK'S TOP MEMES ACROSS THE INTERNET (study the style, never copy; write something new):\n"
    if best:
        s += "Top-performing meme titles: " + " | ".join(best) + "\n"
    if worst:
        s += "Worst-performing meme titles (avoid this kind): " + " | ".join(worst) + "\n"
    return s + "\n"


def report(state):
    st = state.get("peers") or {}
    if not st.get("n"):
        return []
    lines = [f"## Learned from this week's top meme posts (Reddit meme communities) ({st['n']} posts from {len(st['ok'])} pages)"]
    for title, key in (("Opening style", "by_start"), ("Caption length", "by_len")):
        d = st.get(key) or {}
        if d:
            lines.append(f"- {title}: " + ", ".join(f"{k} x{v}" for k, v in
                         sorted(d.items(), key=lambda kv: kv[1], reverse=True)))
    return lines + [""]
