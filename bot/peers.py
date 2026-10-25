"""Learns from other public meme pages (Instagram business discovery: public captions,
likes, comments, timestamps). Only reads public data; never copies anyone's content."""
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PEERS = ["9gag", "memezar", "sarcasm_only", "pubity", "ladbible", "betches",
                 "tank.sinatra", "funnyhumor", "memes", "dankmemes"]
REFRESH_DAYS = 3


def usernames():
    f = Path(__file__).with_name("peers.txt")
    if f.exists():
        names = [x.strip().lstrip("@") for x in f.read_text().splitlines()
                 if x.strip() and not x.startswith("#")]
        if names:
            return names
    return DEFAULT_PEERS


def refresh(state, graph, ig_user_id, now):
    st = state.setdefault("peers", {})
    last = st.get("fetched_at")
    if last and now - datetime.fromisoformat(last) < timedelta(days=REFRESH_DAYS):
        return
    posts, ok, failed = [], [], []
    for u in usernames():
        try:
            data = graph("GET", ig_user_id, fields=(
                f"business_discovery.username({u})"
                "{followers_count,media.limit(40){caption,like_count,comments_count,timestamp,media_type}}"))
            bd = data["business_discovery"]
            foll = max(bd.get("followers_count") or 1, 1)
            media = bd.get("media", {}).get("data", [])
        except Exception as e:
            failed.append(u)
            err = str(e)[:160]
            continue
        rows = []
        for m in media:
            likes = m.get("like_count")
            if likes is None:
                continue
            er = (likes + 3 * (m.get("comments_count") or 0)) / foll * 1000
            rows.append({"u": u, "er": er, "cap": (m.get("caption") or "")[:220],
                         "t": m["timestamp"], "type": m.get("media_type")})
        if len(rows) >= 5:
            avg = statistics.mean(r["er"] for r in rows) or 1
            for r in rows:
                r["rel"] = round(r["er"] / avg, 3)
            posts += rows
            ok.append(u)
    print(f"::warning::peers: {len(ok)} accounts readable {ok}, {len(failed)} failed "
          f"{failed[:6]}{' last error: ' + err if failed else ''}")
    if not posts:
        st["fetched_at"] = now.isoformat()
        st["ok"] = []
        return
    st.update({
        "fetched_at": now.isoformat(), "ok": ok, "n": len(posts),
        "hour_samples": [(datetime.fromisoformat(p["t"].replace("+0000", "+00:00")).hour // 2, p["rel"])
                         for p in posts],
        "best": sorted(posts, key=lambda p: p["rel"], reverse=True)[:25],
        "worst": sorted(posts, key=lambda p: p["rel"])[:15],
        "by_type": {k: round(statistics.mean(p["rel"] for p in posts if p["type"] == k), 2)
                    for k in {p["type"] for p in posts}},
        "by_tags": _bucket(posts, lambda p: _tagbucket(len(re.findall(r"#\w+", p["cap"])))),
        "by_len": _bucket(posts, lambda p: _lenbucket(len(p["cap"]))),
    })


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
    s = "LESSONS FROM OTHER BIG MEME PAGES (study the style, never copy; write something new):\n"
    if best:
        s += "Their top-performing post captions: " + " | ".join(best) + "\n"
    if worst:
        s += "Their worst-performing post captions (avoid this kind): " + " | ".join(worst) + "\n"
    return s + "\n"


def report(state):
    st = state.get("peers") or {}
    if not st.get("n"):
        return []
    lines = [f"## Learned from other meme pages ({st['n']} posts from {len(st['ok'])} pages)"]
    for title, key in (("Post type", "by_type"), ("Hashtags", "by_tags"), ("Caption length", "by_len")):
        d = st.get(key) or {}
        if d:
            lines.append(f"- {title}: " + ", ".join(f"{k} x{v}" for k, v in
                         sorted(d.items(), key=lambda kv: kv[1], reverse=True)))
    return lines + [""]
