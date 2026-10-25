#!/usr/bin/env python3
"""Autonomous Instagram meme poster. Runs on GitHub Actions every ~20 minutes.

Each run: plan today's random post times -> collect metrics for old posts ->
if a post slot is due, generate a meme, render it, host it, publish it.
"""
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

import peers
import reel

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state.json"
IMG_DIR = ROOT / "images"
OUT_DIR = ROOT / "out"

GRAPH = "https://graph.instagram.com/v26.0"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
POSTS_PER_DAY = int(os.environ.get("POSTS_PER_DAY", "5"))
# Posting windows in UTC (covers European morning through US afternoon/evening).
WINDOWS_UTC = [(6, 9), (9, 12), (12, 15), (15, 18), (18, 22)]
MAX_LATE = timedelta(hours=3)  # skip a slot rather than post in a burst after an outage

THEMES = [
    "being tired and sleep", "being broke and money", "friends and group chats",
    "school and studying", "work and bad bosses", "gym and diets", "food and cravings",
    "being late and plans falling through", "phone battery and screen time",
    "procrastination", "weekends and Mondays", "texting and replies",
    "family at home", "gaming", "sports and watching games with friends",
]

# Multi-panel templates look wrong with simple top/bottom captions. Tune via EXCLUDE_REGEX.
EXCLUDE_TEMPLATES = re.compile(
    os.environ.get(
        "EXCLUDE_REGEX",
        r"drake|button|boyfriend|brain|panel|yelling|pointing|tuxedo|pooh|bernie|"
        r"expanding|\bvs\b|comparison|exit|offer|wojak|squid|trump|biden|putin|obama",
    ),
    re.I,
)

# Last-resort local filter on top of Gemini's safety settings and the moderator pass.
BLOCKED = re.compile(
    r"nazi|hitler|rape|suicide|kill (my|your)self|porn|nsfw|n[i1]gg|f[a@]gg?ot|retard|"
    r"trump|biden|putin|election|abortion|gaza|israel|ukraine|palestin|isis|terror|"
    r"cocaine|heroin|sex|nude|boob|dick|cum\b|fuck|shit|bitch",
    re.I,
)

FONT_PATHS = [
    "/Library/Fonts/Impact.ttf",
    "C:\\Windows\\Fonts\\impact.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required secret/env var: {name}")
    return value


def now_utc():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"schedule": {"date": None, "slots": []}, "history": []}


def save_state(state):
    state["history"] = state["history"][-300:]
    STATE_FILE.write_text(json.dumps(state, indent=1))


def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def commit_push(message):
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git("config", "user.name", "meme-bot")
    git("config", "user.email", "meme-bot@users.noreply.github.com")
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for _ in range(3):
        try:
            git("pull", "--rebase", "-q")
            git("push", "-q")
            return
        except subprocess.CalledProcessError:
            time.sleep(5)
    raise RuntimeError("git push failed")


# ------------------------------------------------------------------------ schedule

def plan_day(state, now):
    today = now.strftime("%Y-%m-%d")
    if state["schedule"].get("date") == today:
        return
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    hs = hour_stats(state)
    used, slots = set(), []
    for i in range(POSTS_PER_DAY):
        a, b = WINDOWS_UTC[i % len(WINDOWS_UTC)]
        minute = random.randint(a * 60, b * 60 - 1)
        if hs and random.random() < 0.7:
            free = [blk for blk in hs if blk not in used]
            if free:
                blk = random.choices(free, [max(0.2, hs[x]) for x in free])[0]
                minute = random.randint(blk * 120, blk * 120 + 119)
                used.add(blk)
        slots.append({
            "t": (midnight + timedelta(minutes=minute)).isoformat(),
            "status": "pending",
            "attempts": 0,
        })
    slots.sort(key=lambda s: s["t"])
    state["schedule"] = {"date": today, "slots": slots}


def next_due_slot(state, now):
    for slot in state["schedule"]["slots"]:
        if slot["status"] != "pending":
            continue
        t = datetime.fromisoformat(slot["t"])
        if t > now:
            continue
        if now - t > MAX_LATE:
            slot["status"] = "skipped"
            continue
        return slot
    return None


# ------------------------------------------------------------------------- learning

def score_of(m):
    """Engagement per 1,000 people reached, with DM sends weighted heaviest: sends are what
    push a post to non-followers. Returns None when reach is unknown (kept out of learning,
    so every learned score is on the same scale)."""
    reach = m.get("reach")
    if not reach:
        return None
    raw = (8 * m.get("shares", 0) + 3 * m.get("saved", 0)
           + 2 * m.get("comments_count", 0) + m.get("like_count", 0))
    return round(1000 * raw / max(reach, 100), 1)


def finalized(history, kinds=("reel", "image", None)):
    """Measured single-meme posts. Carousel dumps are tracked separately so they don't skew
    what the bot learns about individual jokes, photos and posting times."""
    return [h for h in history if h.get("final") and h.get("score") is not None
            and h.get("kind") in kinds]


def pick_theme(history):
    done = finalized(history)
    if len(done) < 8 or random.random() < 0.3:
        return random.choice(THEMES)
    by_theme = defaultdict(list)
    for h in done:
        by_theme[h["theme"]].append(h["score"])
    overall = statistics.mean(h["score"] for h in done)
    weights = [(statistics.mean(by_theme[t]) if t in by_theme else overall) + 1 for t in THEMES]
    return random.choices(THEMES, weights)[0]


def pick_candidates(templates, history, n=12):
    recent = [h["template_id"] for h in history[-8:]]
    by_tpl = defaultdict(list)
    for h in finalized(history):
        by_tpl[h["template_id"]].append(h["score"])
    ranked = sorted(by_tpl, key=lambda t: statistics.mean(by_tpl[t]), reverse=True)
    ids = {t["id"]: t for t in templates}
    best = [ids[t] for t in ranked if t in ids and t not in recent[-3:]][:4]
    pool = [t for t in templates if t["id"] not in recent and t not in best]
    random.shuffle(pool)
    return best + pool[: n - len(best)]


# ----------------------------------------------------------- learning from results

FORMATS = {
    "when_you": "starts with 'when you ...'",
    "me_when": "starts with 'me when ...' or 'me ...'",
    "how_me_bro": "starts with 'how me and bro gon be ...'",
    "pov": "starts with 'POV: ...'",
    "other": "any other casual caption style",
}


def classify_format(text):
    t = text.lower().strip()
    if t.startswith("when"):
        return "when_you"
    if t.startswith("how me") or t.startswith("how we"):
        return "how_me_bro"
    if t.startswith("pov"):
        return "pov"
    if t.startswith("me ") or t.startswith("me,"):
        return "me_when"
    return "other"


def _weighted_by(history, key):
    done = [h for h in finalized(history) if h.get(key)]
    if len(done) < 8 or random.random() < 0.3:
        return None
    by = defaultdict(list)
    for h in done:
        by[h[key]].append(h["score"])
    overall = statistics.mean(h["score"] for h in done)
    return {k: (statistics.mean(v) if len(v) >= 2 else overall) + 1 for k, v in by.items()}


def pick_format(history):
    w = _weighted_by([dict(h, format=h.get("format") or classify_format(h.get("text", "")))
                      for h in history if h.get("text")], "format")
    keys = list(FORMATS)
    if not w:
        return random.choice(keys[:4])
    return random.choices(keys, [w.get(k, statistics.mean(w.values())) for k in keys])[0]


def pick_music(history):
    keys = list(reel.TRACKS)
    w = _weighted_by(history, "music")
    if not w:
        return random.choice(keys)
    avg = statistics.mean(w.values())
    return random.choices(keys, [w.get(k, avg) for k in keys])[0]


def top_examples(history, n=5):
    done = [h for h in finalized(history) if h.get("text")]
    done.sort(key=lambda h: h["score"], reverse=True)
    return [h["text"] for h in done[:n]] if len(done) >= 6 else []


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("+0000", "+00:00"))


def import_history(state):
    """One-time: read the page's existing posts so posting-time learning starts informed."""
    if state.get("imported_done"):
        return
    items, after = [], None
    try:
        for _ in range(4):
            params = {"fields": "id,timestamp,like_count,comments_count", "limit": 50}
            if after:
                params["after"] = after
            data = graph("GET", f"{env('IG_USER_ID')}/media", **params)
            for m in data.get("data", []):
                items.append({"t": m["timestamp"],
                              "score": m.get("like_count", 0) + 3 * m.get("comments_count", 0)})
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not after or not data.get("paging", {}).get("next"):
                break
    except Exception as e:
        print(f"could not import existing posts: {e}")
        return
    state["imported"] = items
    state["imported_done"] = True
    print(f"Imported {len(items)} existing posts for time learning")


def hour_stats(state):
    """Average relative performance per 2-hour UTC block, from imported + bot posts."""
    groups = []
    imp = state.get("imported", [])
    bot = [{"t": h["posted_at"], "score": h["score"]} for h in finalized(state["history"])]
    for g in (imp, bot):
        if len(g) >= 5:
            avg = statistics.mean(x["score"] for x in g) or 1
            groups.append([(parse_ts(x["t"]).hour // 2, x["score"] / avg) for x in g])
    samples = [x for g in groups for x in g]
    samples += [tuple(x) for x in state.get("peers", {}).get("hour_samples", [])]
    if len(samples) < 15:
        return None
    by = defaultdict(list)
    for blk, rel in samples:
        by[blk].append(rel)
    overall = statistics.mean(r for _, r in samples)
    return {b: (statistics.mean(by[b]) if len(by.get(b, [])) >= 2 else overall) for b in range(3, 12)}


def write_insights(state):
    done = finalized(state["history"])
    lines = ["# What the bot has learned", "",
             f"Posts measured so far: {len(done)} (plus {len(state.get('imported', []))} older page posts)", "",
             "Score = engagement per 1,000 people reached, with DM sends counted 8x a like.", ""]
    sp = [h["sends_per_1k"] for h in done if h.get("sends_per_1k") is not None]
    if sp:
        lines += ["## Sends", f"- average: {statistics.mean(sp):.2f} sends per 1,000 reached "
                  f"(last 10 posts: {statistics.mean(sp[-10:]):.2f})", ""]
    dumps = finalized(state["history"], kinds=("carousel",))
    if dumps and done:
        lines += ["## Meme dumps vs single memes",
                  f"- dumps: avg {statistics.mean(h['score'] for h in dumps):.1f} ({len(dumps)} posts)",
                  f"- single: avg {statistics.mean(h['score'] for h in done):.1f} ({len(done)} posts)", ""]
    lines += peers.report(state)
    hs = hour_stats(state)
    if hs:
        best = sorted(hs, key=hs.get, reverse=True)[:3]
        lines += ["## Best posting times (UTC)"] + [f"- {b*2}:00-{b*2+2}:00 (score x{hs[b]:.2f})" for b in best] + [""]
    if len(done) >= 4:
        for title, key in (("Best themes", "theme"), ("Best photos", "template_name")):
            by = defaultdict(list)
            for h in done:
                by[h.get(key) or "?"].append(h["score"])
            top = sorted(by.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)[:5]
            lines += [f"## {title}"] + [f"- {k}: avg {statistics.mean(v):.1f} ({len(v)} posts)" for k, v in top] + [""]
        by = defaultdict(list)
        for h in done:
            by[h.get("format") or classify_format(h.get("text", ""))].append(h["score"])
        lines += ["## Best formats"] + [f"- {k}: avg {statistics.mean(v):.1f} ({len(v)} posts)"
                  for k, v in sorted(by.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)] + [""]
        by = defaultdict(list)
        for h in done:
            if h.get("music"):
                by[h["music"]].append(h["score"])
        if by:
            lines += ["## Best music"] + [f"- {k}: avg {statistics.mean(v):.1f} ({len(v)} posts)"
                      for k, v in sorted(by.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)] + [""]
        nw = [h["score"] for h in done if h.get("news")]
        pl = [h["score"] for h in done if not h.get("news")]
        if nw and pl:
            lines += ["## News memes vs normal", f"- news: avg {statistics.mean(nw):.1f} ({len(nw)} posts)",
                      f"- normal: avg {statistics.mean(pl):.1f} ({len(pl)} posts)", ""]
        lines += ["## Top posts"] + [f"- ({h['score']}) {h.get('text')}" for h in
                  sorted(done, key=lambda h: h["score"], reverse=True)[:5]]
    else:
        lines.append("Not enough measured posts yet (needs a few days).")
    (ROOT / "INSIGHTS.md").write_text("\n".join(lines) + "\n")


def fetch_metrics(media_id):
    m = {}
    fields = graph("GET", media_id, fields="like_count,comments_count")
    m["like_count"] = fields.get("like_count", 0)
    m["comments_count"] = fields.get("comments_count", 0)
    for metrics in ("reach,shares,saved,views", "reach,shares,saved"):
        try:
            ins = graph("GET", f"{media_id}/insights", metric=metrics)
            for d in ins.get("data", []):
                m[d["name"]] = d["values"][0]["value"]
            break
        except Exception as e:  # insights are optional
            print(f"insights ({metrics}) unavailable for {media_id}: {e}")
    if m.get("reach"):
        m["sends_per_1k"] = round(1000 * m.get("shares", 0) / m["reach"], 2)
    return m


def refresh_metrics(state, now):
    checked = 0
    for h in state["history"]:
        if h.get("final") or not h.get("media_id") or checked >= 8:
            continue
        if now - datetime.fromisoformat(h["posted_at"]) < timedelta(hours=48):
            continue
        try:
            m = fetch_metrics(h["media_id"])
        except Exception as e:
            print(f"metrics failed for {h['media_id']}: {e}")
            continue
        h.update(m)
        h["score"] = score_of(m)
        h["final"] = True
        checked += 1
        print(f"final score {h['score']} for {h['template_name']} / {h['theme']}")


# -------------------------------------------------------------------------- Gemini

def gemini(prompt, temperature=1.0):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    safety = [
        {"category": c, "threshold": "BLOCK_LOW_AND_ABOVE"}
        for c in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        )
    ]
    use_thinking_off = True
    for attempt in range(7):
        cfg = {"temperature": temperature, "responseMimeType": "application/json"}
        if use_thinking_off:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": cfg, "safetySettings": safety}
        try:
            r = requests.post(url, params={"key": env("GEMINI_API_KEY")}, json=body, timeout=240)
        except requests.exceptions.RequestException as e:
            print(f"Gemini network error: {e}")
            time.sleep(10)
            continue
        if r.status_code == 400 and use_thinking_off:
            use_thinking_off = False  # this model does not accept that setting
            continue
        if r.status_code in (429, 500, 503):
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        if not cands or not cands[0].get("content", {}).get("parts"):
            raise RuntimeError("Gemini returned no content (probably safety-blocked)")
        return cands[0]["content"]["parts"][0]["text"]
    raise RuntimeError("Gemini unavailable")


def claude(prompt, temperature=1.0, max_tokens=1000):
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    body = {"model": model, "max_tokens": max_tokens, "temperature": min(temperature, 1.0),
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": env("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(5):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers=headers,
                              json=body, timeout=60)
        except requests.exceptions.RequestException as e:
            print(f"Claude network error: {e}")
            time.sleep(5)
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(5 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if not m:
            raise RuntimeError("Claude returned no JSON")
        return m.group(0)
    raise RuntimeError("Claude unavailable")


GROQ_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b",
               "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
_groq_ok = {"model": None}


def groq(prompt, temperature=1.0):
    models = [os.environ["GROQ_MODEL"]] if os.environ.get("GROQ_MODEL") else GROQ_MODELS
    if _groq_ok["model"]:
        models = [_groq_ok["model"]] + [m for m in models if m != _groq_ok["model"]]
    headers = {"Authorization": f"Bearer {env('GROQ_API_KEY')}"}
    last = None
    for model in models:
        body = {"model": model, "temperature": min(temperature, 1.0),
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}]}
        for attempt in range(4):
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  headers=headers, json=body, timeout=60)
            except requests.exceptions.RequestException as e:
                last = f"network error: {e}"
                time.sleep(5)
                continue
            if r.status_code in (429, 500, 502, 503):
                last = f"{model}: {r.status_code}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code in (400, 403, 404):
                last = f"{model}: {r.status_code} {r.text[:200]}"
                print(f"Groq model {model} not usable ({r.status_code}), trying next")
                break
            if not r.ok:
                raise RuntimeError(f"Groq API {r.status_code}: {r.text[:300]}")
            _groq_ok["model"] = model
            return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError(f"Groq unavailable ({last})")


def llm(prompt, temperature=1.0, max_tokens=1000):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return claude(prompt, temperature, max_tokens)
    if os.environ.get("GROQ_API_KEY"):
        return groq(prompt, temperature)
    return gemini(prompt, temperature)


def _items(raw, key):
    """Parse an LLM JSON reply into a list of dicts, accepting {key: [...]}, [...] or {...}."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get(key, data)
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def moderate_many(memes, by_id):
    """One safety review for a batch of memes. Returns a list of booleans (failure = unsafe)."""
    if not memes:
        return []
    listing = "\n".join(
        f"{i}. image: {by_id[m['template_id']]['name']} | caption on image: {m['text']} | "
        f"post caption: {m['caption']} | hashtags: {' '.join(m['hashtags'])}"
        for i, m in enumerate(memes))
    prompt = (
        "You review memes for an Instagram meme page aimed at young adults. For EACH meme decide "
        "if it could realistically get the account restricted. Mark it UNSAFE only if it clearly "
        "contains: sexual content or innuendo, hate or stereotypes about a group, politics or "
        "religion, violence or self-harm, drugs, slurs or strong profanity, body-shaming, or a "
        "real named person. Everyday relatable humor, mild slang (bro, nah, fr), self-deprecating "
        "jokes and friendly teasing are SAFE.\n\n" + listing + "\n\n"
        'Reply JSON only: {"results": [{"i": 0, "safe": true, "reason": "short"}, ...]} '
        "with one entry per meme."
    )
    verdict = [False] * len(memes)
    try:
        for it in _items(llm(prompt, temperature=0.0, max_tokens=2500), "results"):
            i = int(it.get("i", -1))
            if 0 <= i < len(memes):
                verdict[i] = it.get("safe") is True
                if not verdict[i]:
                    print(f"moderator rejected: {memes[i]['text']!r} -> {it.get('reason')}")
    except Exception as e:
        print(f"moderation failed, treating batch as unsafe: {e}")
    return verdict


def judge(memes, by_id):
    """Harsh comparative comedy judge. Sets meme['funny'] (1-10) and meme['send'] (bool)."""
    listing = "\n".join(f"{i}. [photo: {by_id[m['template_id']]['name']}] {m['text']}"
                        for i, m in enumerate(memes))
    prompt = (
        "You are the harshest comedy editor of a meme page with millions of followers aged 16-30. "
        "Below are candidate memes (a caption above a reaction photo). Only the best get posted.\n\n"
        "For each one ask: would a stranger scrolling past stop, laugh out loud, and SEND it to a "
        "friend in DMs ('this is literally you')? Score 1-10: 5 = meh, 7 = would send to a friend, "
        "9 = would go viral. Hard cap at 4 if the joke needs explaining, the situation is generic "
        "or vague, there is no clear punchline, the slang is forced, it sounds corporate, or the "
        "photo's mood doesn't match the feeling. Reward a hyper-specific detail everyone has "
        "lived, an unexpected but true twist, and a perfect photo/caption match. Most candidates "
        "should score below 7; be honest, not generous.\n\n" + listing + "\n\n"
        'Reply JSON only: {"scores": [{"i": 0, "score": 6, "send": false}, ...]} '
        "with one entry per candidate. \"send\" = true only if you would personally DM it to a friend."
    )
    for m in memes:
        m["funny"], m["send"] = 0, False
    try:
        for it in _items(llm(prompt, temperature=0.0, max_tokens=3000), "scores"):
            i = int(it.get("i", -1))
            if 0 <= i < len(memes):
                memes[i]["funny"] = int(round(float(it.get("score", 0))))
                memes[i]["send"] = it.get("send") is True
    except Exception as e:
        print(f"judging failed: {e}")


def rank(m):
    """Funniness first; small boosts for 'would DM it' and for very fresh news."""
    bonus = 0.5 if m.get("send") else 0.0
    age = m.get("news_age_h")
    if age is not None:
        bonus += 1.0 if age <= 6 else 0.5 if age <= 12 else 0.0
    return m.get("funny", 0) + bonus


MIN_FUNNY = int(os.environ.get("MIN_FUNNY", "7"))
CANDIDATES = int(os.environ.get("CANDIDATES", "12"))


def fetch_templates():
    r = requests.get("https://api.imgflip.com/get_memes", timeout=30)
    r.raise_for_status()
    memes = r.json()["data"]["memes"]
    return [
        m for m in memes
        if m["box_count"] <= 2
        and 0.6 <= m["width"] / m["height"] <= 1.91
        and not EXCLUDE_TEMPLATES.search(m["name"])
    ]



# ------------------------------------------------------------------------------ news

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=weird+OR+bizarre+OR+viral+OR+unexpected+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=funny+OR+hilarious+OR+%22goes+viral%22+when:1d&hl=en-GB&gl=GB&ceid=GB:en",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://www.reddit.com/r/nottheonion/.rss",
]
NEWS_MAX_AGE_H = float(os.environ.get("NEWS_MAX_AGE_H", "24"))
ATOM = "{http://www.w3.org/2005/Atom}"
# News is only used when it is light-hearted; anything heavy is dropped before the AI sees it.
NEWS_AVOID = re.compile(
    r"war|kill|dead|death|die[sd]?\b|murder|attack|shoot|bomb|missile|troops|army|terror|"
    r"crash|victim|abuse|assault|arrest|court|trial|prison|jail|police|hostage|"
    r"president|minister|election|vote|government|senate|congress|parliament|tariff|"
    r"trump|biden|putin|israel|gaza|ukraine|russia|china|iran|hamas|"
    r"cancer|virus|outbreak|hospital|disease|suicide|rape|sex|nude|porn|drug|"
    r"flood|earthquake|hurricane|wildfire|tragedy|funeral|collapse",
    re.I,
)


def _age_hours(text, now):
    if not text:
        return None
    try:
        t = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600)


def fetch_news(state, limit=12):
    """Light headlines from the last day, freshest first. Each: {"title", "age_h"}."""
    used = set(state.get("news_used", []))
    now = now_utc()
    items, seen = [], set()
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (meme-bot)"})
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"news feed failed ({url[:50]}): {e}")
            continue
        entries = [(it.findtext("title"), it.findtext("pubDate")) for it in root.iter("item")]
        entries += [(e.findtext(f"{ATOM}title"), e.findtext(f"{ATOM}updated") or e.findtext(f"{ATOM}published"))
                    for e in root.iter(f"{ATOM}entry")]  # reddit serves Atom, not RSS
        for raw_title, date in entries:
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", (raw_title or "").strip())
            if not (20 <= len(title) <= 160) or title in seen or title in used:
                continue
            if NEWS_AVOID.search(title) or BLOCKED.search(title):
                continue
            age = _age_hours(date, now)
            if age is not None and age > NEWS_MAX_AGE_H:
                continue
            seen.add(title)
            items.append({"title": title, "age_h": age})
    # freshest first; undated items go after dated ones, in random order
    random.shuffle(items)
    items.sort(key=lambda x: x["age_h"] if x["age_h"] is not None else NEWS_MAX_AGE_H)
    fresh = sum(1 for x in items if x["age_h"] is not None and x["age_h"] <= 6)
    print(f"::notice::{len(items)} usable light news headlines ({fresh} from the last 6h)")
    return items[:limit]


def news_probability(history):
    done = finalized(history)
    news = [h["score"] for h in done if h.get("news")]
    plain = [h["score"] for h in done if not h.get("news")]
    # The judge picks news memes only when they beat the normal ones, so news is tried often.
    if len(news) >= 4 and len(plain) >= 4:
        ratio = statistics.mean(news) / max(statistics.mean(plain), 1)
        return min(0.85, max(0.2, 0.5 * ratio))
    return 0.6


# ------------------------------------------------------------------------ generation

PEER_HINT = ""


def build_prompt(n, themes, fmt, tops, listing, recent, headlines):
    news_block = ""
    if headlines:
        def age(h):
            return f" ({h['age_h']:.0f}h ago)" if h["age_h"] is not None else ""
        k = max(2, n // 3)
        news_block = (
            f"NEWS: below are today's light news headlines, freshest first. Make up to {k} of the "
            "memes about these stories (fresh ones matter most: a meme about something that "
            "happened a few hours ago spreads much further). Only use a headline if it is "
            "genuinely funny or very relatable; keep it recognisable as the story, but never name "
            "real people, politicians, countries in conflict or brands being attacked, and skip "
            "anything sad, political, violent or about a tragedy. Set \"news_index\" to that "
            "headline's number; for normal memes set it to null.\n"
            + "\n".join(f"{i}. {h['title']}{age(h)}" for i, h in enumerate(headlines)) + "\n\n")
    return PEER_HINT + (
        "You run a viral Instagram meme page for people in their teens and twenties. "
        f"Write {n} DIFFERENT original meme candidates. A harsh editor will post only the best "
        "one, so make every candidate a genuinely different situation and punchline.\n\n"
        + news_block +
        f"Themes to spread the candidates over: {json.dumps(themes)}\n"
        f"Caption style for at least half of them: {FORMATS[fmt]}\n\n"
        + ("These are this page's most-shared memes so far; aim for the same kind of humor and "
           "structure but with completely new situations: " + json.dumps(tops) + "\n\n"
           if tops else "") +
        "Format: a single caption that sits ABOVE a reaction photo. Use formats like "
        "'When you ...', 'How me and bro gon be ... after ...', 'Me when ...', 'POV: ...'. "
        "Casual texting voice (words like bro, nah, fr, lowkey are fine; lowercase is fine). "
        "Under 22 words. Hyper-specific and relatable: friends, school, work, gym, food, "
        "money, sleep, phones, being late, group chats.\n\n"
        "What matters most is SENDS: the meme people DM to a friend with 'this is literally "
        "you' / 'this is us'. That means a moment almost everyone has lived but nobody says out "
        "loud, with one specific detail that makes it feel personal. The joke must land in one "
        "read, without explanation.\n\n"
        "Examples of the voice and rhythm to match (write something NEW, never copy these):\n"
        "- when you tell yourself you're going to bed early and it's already 3am\n"
        "- how me and bro gon be posted up at the pizza place after we hear the first slice is free\n"
        "- me explaining to my mom that the 'quick nap' was 6 hours\n"
        "- when the group chat has 200 messages and the plan is still 'we'll see'\n"
        "- when you check your bank account after saying 'it's only $5' twelve times\n"
        "- POV: you said you'd start the assignment 'in 10 minutes' at 2pm and it's now midnight\n"
        "- when the teacher says 'pick your own groups' and everyone looks at you\n"
        "- me pretending to listen to the whole gym story from the guy who just started going\n"
        "Rules for quality: ONE clear situation, a specific detail, an obvious punchline or "
        "relatable pain, and the reaction photo's mood must match the feeling. Never write "
        "about AI, technology trends, or abstract ideas - only real everyday situations "
        "(or the news story).\n\n"
        "Pick each meme's reaction photo from this list (id | name) whose face or mood best fits "
        "the punchline; use different photos across candidates:\n"
        f"{listing}\n\n"
        "Do not reuse or paraphrase these recent jokes: " + json.dumps(recent) + "\n\n"
        "Hard safety rules (an account ban is the worst outcome):\n"
        "- No sexual jokes or innuendo, no body-shaming, no insults about anyone's looks.\n"
        "- No politics, religion, race, ethnicity, nationality, gender, sexuality, disability "
        "or tragedy jokes. No insults aimed at groups.\n"
        "- No nudity, drugs, violence, self-harm, profanity, real people's names or brand attacks.\n"
        "- Friendly, self-deprecating, wholesome-edgy humor only.\n\n"
        "Post caption: one short line that adds to the joke, sometimes ending with a soft share "
        "hook like 'send this to your bro'. Never beg for likes or follows. "
        "Give 3 to 5 relevant hashtags.\n\n"
        "Also pick the background music mood that makes the viewer feel the joke's emotion most "
        "strongly. Options: " + ", ".join(reel.TRACKS) + " (chill=lazy/tired, upbeat=happy win, "
        "bouncy=playful/funny, hype=excited/chaos, sad=disappointment/loss, wholesome=sweet/friends, "
        "awkward=cringe/embarrassed, dramatic=tension/betrayal/dread).\n\n"
        'Return JSON only: {"memes": [{"template_id": "...", "theme": "one of the themes", '
        '"text": "...", "caption": "...", "hashtags": ["#..."], "mood": "...", '
        '"news_index": null}, ...]}'
    )


def parse_candidates(raw, cand_ids, recent, themes, headlines):
    out, seen = [], set()
    for data in _items(raw, "memes"):
        try:
            tid = str(data["template_id"])
            text = str(data["text"]).strip()
            caption = str(data["caption"]).strip()
            tags = []
            for h in list(data["hashtags"])[:5]:
                h = "#" + re.sub(r"[^\w]", "", str(h))
                if len(h) > 2:
                    tags.append(h)
        except Exception:
            continue
        if tid not in cand_ids or not text or len(text) > 170 or len(caption) > 200 \
                or len(tags) < 3 or text in recent or text.lower() in seen:
            continue
        if BLOCKED.search(" ".join([text, caption, *tags])):
            print(f"local blocklist hit: {text!r}")
            continue
        seen.add(text.lower())
        theme = data.get("theme") if data.get("theme") in themes else themes[0]
        mood = str(data.get("mood", "")).lower().strip()
        meme = {"template_id": tid, "text": text, "caption": caption, "hashtags": tags,
                "theme": theme, "mood": mood if mood in reel.TRACKS else None}
        ni = data.get("news_index")
        if isinstance(ni, int) and 0 <= ni < len(headlines):
            meme["news"] = headlines[ni]["title"]
            meme["news_age_h"] = headlines[ni]["age_h"]
            meme["theme"] = "news"
        out.append(meme)
    return out


def make_memes(state, want=1, n_cands=CANDIDATES, min_score=MIN_FUNNY, floor=5):
    """Generate a batch of candidates, judge them, moderate the best, return up to `want`
    (meme, template) pairs, best first. Never returns anything below `floor`."""
    started = time.time()
    templates = fetch_templates()
    by_id = {t["id"]: t for t in templates}
    history = state["history"]
    tops = top_examples(history)
    recent = [h.get("text") or f"{h.get('top', '')} / {h.get('bottom', '')}" for h in history[-15:]]
    headlines = fetch_news(state) if random.random() < news_probability(history) else []
    picked, spare = [], []
    for rnd in range(3):
        if time.time() - started > 720 or len(picked) >= want:
            break
        themes = []
        while len(themes) < min(3, len(THEMES)):
            t = pick_theme(history)
            if t not in themes:
                themes.append(t)
        fmt = pick_format(history)
        cands = pick_candidates(templates, history, n=20)
        cand_ids = {c["id"] for c in cands}
        listing = "\n".join(f"{c['id']} | {c['name']}" for c in cands)
        prompt = build_prompt(n_cands, themes, fmt, tops, listing, recent, headlines)
        try:
            raw = llm(prompt, temperature=1.0, max_tokens=6000)
        except Exception as e:
            print(f"generation failed ({e}), retrying")
            continue
        used = {m["text"].lower() for m in picked + spare}
        memes = [m for m in parse_candidates(raw, cand_ids, recent, themes, headlines)
                 if m["text"].lower() not in used]
        print(f"round {rnd + 1}: {len(memes)} valid candidates")
        if not memes:
            continue
        judge(memes, by_id)
        memes.sort(key=rank, reverse=True)
        for m in memes:
            flag = (" [news %sh]" % (f"{m['news_age_h']:.0f}" if m.get("news_age_h") is not None else "?")
                    if m.get("news") else "")
            print(f"  {m['funny']:>2}{' send' if m['send'] else '     '}{flag}  {m['text']}")
        top = [m for m in memes if m["funny"] >= floor][: want - len(picked) + 3]
        tpls = {m["template_id"] for m in picked}
        for m, ok in zip(top, moderate_many(top, by_id)):
            if not ok:
                continue
            if m["funny"] >= min_score and len(picked) < want and \
                    (want == 1 or m["template_id"] not in tpls):
                picked.append(m)
                tpls.add(m["template_id"])
            else:
                spare.append(m)
    if len(picked) < want:  # nothing reached the bar: fall back to the best safe ones above floor
        tpls = {m["template_id"] for m in picked}
        for m in sorted(spare, key=rank, reverse=True):
            if len(picked) >= want:
                break
            if want == 1 or m["template_id"] not in tpls:
                picked.append(m)
                tpls.add(m["template_id"])
    picked.sort(key=rank, reverse=True)
    return [(m, by_id[m["template_id"]]) for m in picked]


# ------------------------------------------------------------------------ rendering

BOLD_FONTS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
REG_FONTS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def load_font(size, bold=False):
    for p in (BOLD_FONTS if bold else REG_FONTS):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def wrap(text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def render(meme, template, dark=None):
    """Caption bar on top, reaction photo underneath (the style of the page).
    Big text so it reads instantly on a phone in the first second."""
    raw = requests.get(template["url"], timeout=30).content
    photo = Image.open(BytesIO(raw)).convert("RGB")
    W = 1080
    photo = photo.resize((W, round(photo.height * W / photo.width)), Image.LANCZOS)

    if dark is None:
        dark = random.random() < 0.35
    bg, fg = ((0, 0, 0), (255, 255, 255)) if dark else ((255, 255, 255), (0, 0, 0))
    pad = 44
    size = 68
    while True:
        font = load_font(size, bold=dark)
        lines = wrap(meme["text"], font, W - 2 * pad)
        if len(lines) <= 4 or size <= 40:
            break
        size -= 4
    lh = round(size * 1.24)
    bar_h = len(lines) * lh + 2 * pad - round(size * 0.2)

    max_total = 1350  # Instagram's tallest allowed ratio (4:5)
    if bar_h + photo.height > max_total:
        keep = max_total - bar_h
        top = (photo.height - keep) // 2
        photo = photo.crop((0, top, W, top + keep))

    canvas = Image.new("RGB", (W, bar_h + photo.height), bg)
    draw = ImageDraw.Draw(canvas)
    y = pad
    for line in lines:
        draw.text((pad, y), line, font=font, fill=fg)
        y += lh
    canvas.paste(photo, (0, bar_h))
    return canvas


def to_portrait(img, dark):
    """Carousel slides must share one shape: centre each meme on a 1080x1350 (4:5) card."""
    W, H = 1080, 1350
    if img.height > H:
        img = img.resize((round(img.width * H / img.height), H), Image.LANCZOS)
    card = Image.new("RGB", (W, H), (0, 0, 0) if dark else (255, 255, 255))
    card.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return card


# ------------------------------------------------------------------------ Instagram

def graph(method, path, **params):
    params["access_token"] = env("IG_ACCESS_TOKEN")
    kwargs = {"params": params} if method == "GET" else {"data": params}
    r = requests.request(method, f"{GRAPH}/{path}", timeout=60, **kwargs)
    if not r.ok:
        raise RuntimeError(f"Instagram API {r.status_code}: {r.text[:500]}")
    return r.json()


def host_files(paths):
    """Push files to the public repo (one commit) and return their raw URLs."""
    if not os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError("Publishing only works inside GitHub Actions (needs a public URL)")
    commit_push(f"bot: add {len(paths)} file(s)")
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    urls = []
    for path in paths:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/images/{path.name}"
        for _ in range(20):
            if requests.get(url, timeout=15, stream=True).status_code == 200:
                break
            time.sleep(3)
        else:
            raise RuntimeError("Hosted file never became reachable (is the repo public?)")
        urls.append(url)
    return urls


def host_file(path):
    return host_files([path])[0]


def wait_container(cid, video=False):
    for _ in range(60 if video else 30):
        status = graph("GET", cid, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container status: {status}")
        time.sleep(5 if video else 3)
    raise RuntimeError("Instagram container never finished processing")


def publish(media_url, caption, video=False):
    ig = env("IG_USER_ID")
    if video:
        params = dict(media_type="REELS", video_url=media_url, caption=caption, share_to_feed="true")
    else:
        params = dict(image_url=media_url, caption=caption)
    cid = graph("POST", f"{ig}/media", **params)["id"]
    wait_container(cid, video)
    return graph("POST", f"{ig}/media_publish", creation_id=cid)["id"]


def publish_carousel(urls, caption):
    ig = env("IG_USER_ID")
    kids = [graph("POST", f"{ig}/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    for k in kids:
        wait_container(k)
    cid = graph("POST", f"{ig}/media", media_type="CAROUSEL",
                children=",".join(kids), caption=caption)["id"]
    wait_container(cid)
    return graph("POST", f"{ig}/media_publish", creation_id=cid)["id"]


def cleanup_images():
    for f in [*IMG_DIR.glob("*.jpg"), *IMG_DIR.glob("*.mp4")]:
        try:
            if time.time() - int(f.name.split("_")[0]) > 3 * 86400:
                f.unlink()
        except ValueError:
            pass


# ------------------------------------------------------------------------ meme dumps

DUMP_DAYS = {int(d) for d in os.environ.get("DUMP_DAYS", "2,6").split(",") if d.strip()}  # Wed, Sun
DUMP_SIZE = int(os.environ.get("DUMP_SIZE", "8"))
DUMP_MIN = 5
DUMP_CAPTIONS = [
    "meme dump 🧃 which one is you? (1-{n})",
    "weekly meme dump. comment the number that's literally you 💀",
    "meme dump 📦 send #{k} to the friend it's about",
    "dump time. which one hit too close to home? (1-{n})",
    "meme dump 🗑️ tag the friend who is #{k}",
]


def ensure_dump(state, now):
    """On dump days, turn the day's best-timed remaining slot into a carousel meme dump."""
    sched = state["schedule"]
    if sched.get("dump_checked") == sched.get("date"):
        return
    sched["dump_checked"] = sched.get("date")
    if now.weekday() not in DUMP_DAYS or DUMP_SIZE < 2:
        return
    pending = [s for s in sched["slots"]
               if s["status"] == "pending" and datetime.fromisoformat(s["t"]) > now]
    if not pending:
        return
    hs = hour_stats(state)
    if hs:
        target = max(pending, key=lambda s: hs.get(datetime.fromisoformat(s["t"]).hour // 2, 0))
    else:
        evening = [s for s in pending if 17 <= datetime.fromisoformat(s["t"]).hour <= 21]
        target = evening[0] if evening else pending[-1]
    target["kind"] = "dump"
    print(f"Meme dump planned for {target['t']}")


def dump_caption(items):
    n = len(items)
    line = random.choice(DUMP_CAPTIONS).format(n=n, k=random.randint(1, n))
    counts = Counter(t for m, _ in items for t in m["hashtags"] if t.lower() != "#memedump")
    tags = ["#memedump"] + [t for t, _ in counts.most_common(4)]
    return line + "\n\n" + " ".join(tags)


def run_dump(state, slot, dry):
    items = make_memes(state, want=DUMP_SIZE, n_cands=16, min_score=MIN_FUNNY,
                       floor=max(5, MIN_FUNNY - 1))
    if len(items) < DUMP_MIN:
        raise RuntimeError(f"only {len(items)} memes were good enough for a dump")
    dark = random.random() < 0.35
    imgs = [to_portrait(render(m, t, dark=dark), dark) for m, t in items]
    caption = dump_caption(items)
    for m, t in items:
        print(f"  dump item ({m['funny']}): {m['text']} | {t['name']}")
    if dry:
        OUT_DIR.mkdir(exist_ok=True)
        for i, img in enumerate(imgs, 1):
            img.save(OUT_DIR / f"dump_{i}.jpg", quality=92)
        (OUT_DIR / "dump.json").write_text(json.dumps(
            {"caption": caption, "memes": [m for m, _ in items]}, indent=1))
        print(f"Dry run: {len(imgs)}-slide meme dump saved to out/")
        return
    IMG_DIR.mkdir(exist_ok=True)
    stamp = int(time.time())
    paths = []
    for i, img in enumerate(imgs):
        p = IMG_DIR / f"{stamp}_dump{i}.jpg"
        img.save(p, quality=92)
        paths.append(p)
    media_id = publish_carousel(host_files(paths), caption)
    slot["status"] = "done"
    for m, _ in items:
        if m.get("news"):
            state.setdefault("news_used", []).append(m["news"])
    state["news_used"] = state.get("news_used", [])[-200:]
    state["history"].append({
        "media_id": media_id,
        "posted_at": now_utc().isoformat(),
        "template_id": "dump",
        "template_name": "meme dump",
        "theme": "meme dump",
        "text": " | ".join(m["text"] for m, _ in items),
        "kind": "carousel",
        "slides": len(items),
        "funny": round(statistics.mean(m["funny"] for m, _ in items), 1),
    })
    print(f"Posted meme dump {media_id} ({len(items)} slides)")


# ---------------------------------------------------------------------------- main

def run_post(state, slot, dry):
    if slot.get("kind") == "dump" or os.environ.get("DUMP") == "1":
        try:
            return run_dump(state, slot, dry)
        except Exception as e:
            print(f"::warning::meme dump failed ({e}); posting a single meme instead")
    items = make_memes(state, want=1)
    if not items:
        raise RuntimeError("Could not produce a safe, funny meme in time")
    meme, template = items[0]
    print(json.dumps(meme, indent=1), "| template:", template["name"])
    img = render(meme, template)
    music = meme.get("mood") or pick_music(state["history"])
    if dry:
        OUT_DIR.mkdir(exist_ok=True)
        img.save(OUT_DIR / "preview.jpg", quality=92)
        (OUT_DIR / "preview.json").write_text(json.dumps(meme, indent=1))
        reel.make_reel(img, music, OUT_DIR / "preview.mp4", tempfile.mkdtemp())
        print(f"Dry run: preview saved to out/ (music: {music})")
        return
    IMG_DIR.mkdir(exist_ok=True)
    stamp = f"{int(time.time())}_{template['id']}"
    caption = meme["caption"] + "\n\n" + " ".join(meme["hashtags"])
    media_id = None
    try:
        mp4 = reel.make_reel(img, music, IMG_DIR / f"{stamp}.mp4", tempfile.mkdtemp())
        media_id = publish(host_file(mp4), caption, video=True)
        kind = "reel"
    except Exception as e:  # never lose a post slot because of the video step
        print(f"Reel failed ({e}); posting as a normal image instead")
        path = IMG_DIR / f"{stamp}.jpg"
        img.save(path, quality=92)
        media_id = publish(host_file(path), caption)
        kind, music = "image", None
    slot["status"] = "done"
    if meme.get("news"):
        state.setdefault("news_used", []).append(meme["news"])
        state["news_used"] = state["news_used"][-200:]
    state["history"].append({
        "media_id": media_id,
        "posted_at": now_utc().isoformat(),
        "template_id": template["id"],
        "template_name": template["name"],
        "theme": meme["theme"],
        "text": meme["text"],
        "format": classify_format(meme["text"]),
        "kind": kind,
        "music": music,
        "news": bool(meme.get("news")),
        "news_age_h": meme.get("news_age_h"),
        "funny": meme.get("funny"),
    })
    print(f"Posted {media_id}")


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE") == "1"
    now = now_utc()
    state = load_state()
    try:
        _run(state, now, dry, force)
    except Exception as e:
        print(f"::error::{type(e).__name__}: {str(e)[:400]}")
        raise
    finally:
        if not dry:
            save_state(state)
            commit_push("bot: state update")


def _run(state, now, dry, force):
    if True:
        if not dry:
            import_history(state)
        plan_day(state, now)
        global PEER_HINT
        try:
            peers.refresh(state, graph, env("IG_USER_ID"), now)
        except Exception as e:
            print(f"::warning::peer learning failed: {e}")
        PEER_HINT = peers.hint(state)
        if not dry:
            ensure_dump(state, now)
            refresh_metrics(state, now)
            cleanup_images()
            write_insights(state)
        if dry:
            run_post(state, {}, dry=True)
            return
        if force:
            slot = {"status": "pending", "attempts": 0}
        else:
            slot = next_due_slot(state, now)
        if slot is None:
            print("Nothing due.")
            return
        slot["attempts"] = slot.get("attempts", 0) + 1
        if slot["attempts"] > 3:
            slot["status"] = "skipped"
            print("Slot skipped after 3 failed attempts.")
            return
        run_post(state, slot, dry=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)  # failed run => GitHub emails you automatically
