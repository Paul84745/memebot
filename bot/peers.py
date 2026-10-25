"""Learns from other public meme pages (Instagram business discovery: public captions,
likes, comments, timestamps). Only reads public data; never copies anyone's content."""
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Lemmy is an open, Reddit-like network with a public JSON API (no login, scraping-free).
COMMUNITIES = [("lemmy.world", "memes"), ("lemmy.world", "me_irl"), ("lemmy.world", "wholesomememes"),
               ("lemmy.ml", "memes"), ("lemmy.world", "dankmemes"), ("lemmy.world", "programmerhumor"),
               ("lemmy.world", "funny")]
REFRESH_DAYS = 2
REDDIT_SUBS = ["memes", "dankmemes", "me_irl", "wholesomememes", "funny", "memeeconomy"]


def _reddit_rows():
    """Official Reddit API (free app credentials). Returns {sub: [rows]} or {} if no keys."""
    import os
    import requests
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return {}
    ua = "meme-bot/1.0 by plqmemes"
    tok = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
                        data={"grant_type": "client_credentials"}, headers={"User-Agent": ua}, timeout=25)
    tok.raise_for_status()
    token = tok.json()["access_token"]
    out = {}
    for sub in REDDIT_SUBS:
        try:
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/top", params={"t": "week", "limit": 60, "raw_json": 1},
                             headers={"User-Agent": ua, "Authorization": f"bearer {token}"}, timeout=25)
            r.raise_for_status()
            rows = []
            for k in r.json()["data"]["children"]:
                d = k["data"]
                if d.get("over_18") or not (d.get("title") or "").strip():
                    continue
                rows.append({"u": sub, "er": d["score"] + 3 * d.get("num_comments", 0),
                             "cap": d["title"][:220], "t": "", "type": "IMAGE"})
            out[f"r/{sub}"] = rows
        except Exception as e:
            print(f"::warning::reddit r/{sub} failed: {str(e)[:150]}")
    return out


def refresh(state, graph, ig_user_id, now):
    """Instagram's API cannot read other accounts, so 'what memes work right now' comes from
    this week's top posts in big public meme communities, scored relative to each community."""
    import requests
    st = state.setdefault("peers", {})
    last = st.get("fetched_at")
    if last and now - datetime.fromisoformat(last) < timedelta(days=REFRESH_DAYS):
        return
    posts, ok, err = [], [], ""
    for host, name in COMMUNITIES:
        try:
            r = requests.get(f"https://{host}/api/v3/post/list",
                             params={"community_name": f"{name}@{host}" if host != "lemmy.world" or True else name,
                                     "sort": "TopWeek", "limit": 50, "type_": "All"},
                             headers={"User-Agent": "meme-bot/1.0"}, timeout=25)
            r.raise_for_status()
            kids = r.json()["posts"]
        except Exception as e:
            err = f"{name}@{host}: {str(e)[:120]}"
            continue
        rows = []
        for k in kids:
            post, c = k["post"], k["counts"]
            title = (post.get("name") or "").strip()
            if post.get("nsfw") or not title or k.get("community", {}).get("nsfw"):
                continue
            rows.append({"u": name, "er": max(c.get("score", 0), 0) + 3 * c.get("comments", 0),
                         "cap": title[:220], "t": post.get("published", ""), "type": "IMAGE"})
        if len(rows) >= 10:
            avg = statistics.mean(x["er"] for x in rows) or 1
            for x in rows:
                x["rel"] = round(x["er"] / avg, 3)
            posts += rows
            ok.append(f"{name}@{host}")
    try:
        for name, rows in _reddit_rows().items():
            if len(rows) >= 10:
                avg = statistics.mean(x["er"] for x in rows) or 1
                for x in rows:
                    x["rel"] = round(x["er"] / avg, 3)
                posts += rows
                ok.append(name)
    except Exception as e:
        err = f"reddit: {str(e)[:150]}"
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
    lines = [f"## Learned from this week's top meme posts (public meme communities) ({st['n']} posts from {len(st['ok'])} pages)"]
    for title, key in (("Opening style", "by_start"), ("Caption length", "by_len")):
        d = st.get(key) or {}
        if d:
            lines.append(f"- {title}: " + ", ".join(f"{k} x{v}" for k, v in
                         sorted(d.items(), key=lambda kv: kv[1], reverse=True)))
    return lines + [""]
