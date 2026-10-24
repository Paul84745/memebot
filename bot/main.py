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
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state.json"
IMG_DIR = ROOT / "images"
OUT_DIR = ROOT / "out"

GRAPH = "https://graph.instagram.com/v26.0"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
POSTS_PER_DAY = int(os.environ.get("POSTS_PER_DAY", "4"))
# Posting windows in UTC (covers European morning through US afternoon/evening).
WINDOWS_UTC = [(7, 10), (11, 14), (15, 18), (19, 22)]
MAX_LATE = timedelta(hours=3)  # skip a slot rather than post in a burst after an outage

THEMES = [
    "work and office life", "being tired and sleep", "being broke and money",
    "dating and relationships", "food and snacks", "phone and social media addiction",
    "gym and health goals", "adulting and responsibilities", "procrastination",
    "friends and group chats", "technology and AI", "pets", "weekends and Mondays",
    "school and studying", "travel and being late",
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
    slots = []
    for i in range(POSTS_PER_DAY):
        a, b = WINDOWS_UTC[i % len(WINDOWS_UTC)]
        minute = random.randint(a * 60, b * 60 - 1)
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
    return (m.get("like_count", 0) + 3 * m.get("comments_count", 0)
            + 5 * m.get("shares", 0) + 4 * m.get("saved", 0))


def finalized(history):
    return [h for h in history if h.get("final") and h.get("score") is not None]


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


def pick_candidates(templates, history):
    recent = [h["template_id"] for h in history[-8:]]
    by_tpl = defaultdict(list)
    for h in finalized(history):
        by_tpl[h["template_id"]].append(h["score"])
    ranked = sorted(by_tpl, key=lambda t: statistics.mean(by_tpl[t]), reverse=True)
    ids = {t["id"]: t for t in templates}
    best = [ids[t] for t in ranked if t in ids and t not in recent[-3:]][:4]
    pool = [t for t in templates if t["id"] not in recent and t not in best]
    random.shuffle(pool)
    return best + pool[: 12 - len(best)]


def fetch_metrics(media_id):
    m = {}
    fields = graph("GET", media_id, fields="like_count,comments_count")
    m["like_count"] = fields.get("like_count", 0)
    m["comments_count"] = fields.get("comments_count", 0)
    try:
        ins = graph("GET", f"{media_id}/insights", metric="reach,shares,saved")
        for d in ins.get("data", []):
            m[d["name"]] = d["values"][0]["value"]
    except Exception as e:  # insights are optional
        print(f"insights unavailable for {media_id}: {e}")
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
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_LOW_AND_ABOVE"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }
    for attempt in range(4):
        r = requests.post(url, params={"key": env("GEMINI_API_KEY")}, json=body, timeout=90)
        if r.status_code in (429, 500, 503):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        if not cands or not cands[0].get("content", {}).get("parts"):
            raise RuntimeError("Gemini returned no content (probably safety-blocked)")
        return cands[0]["content"]["parts"][0]["text"]
    raise RuntimeError("Gemini unavailable")


def moderate(meme, template_name):
    prompt = (
        "You are a strict Instagram community-guidelines reviewer for a mainstream, "
        "brand-safe meme page. Review this meme. Mark it UNSAFE if it contains or implies: "
        "politics, religion, hate or stereotypes about any group, sexual content, drugs, "
        "violence, self-harm, profanity, real people, tragedy, medical claims, or anything "
        "that could get an account restricted. When in doubt, UNSAFE.\n\n"
        f"Template: {template_name}\nTop text: {meme['top']}\nBottom text: {meme['bottom']}\n"
        f"Caption: {meme['caption']}\nHashtags: {' '.join(meme['hashtags'])}\n\n"
        'Reply JSON only: {"safe": true or false, "reason": "short reason"}'
    )
    try:
        verdict = json.loads(gemini(prompt, temperature=0.0))
        return bool(verdict.get("safe")) is True
    except Exception as e:
        print(f"moderation failed, treating as unsafe: {e}")
        return False


def fetch_templates():
    r = requests.get("https://api.imgflip.com/get_memes", timeout=30)
    r.raise_for_status()
    memes = r.json()["data"]["memes"]
    return [
        m for m in memes
        if m["box_count"] == 2
        and 0.8 <= m["width"] / m["height"] <= 1.91
        and not EXCLUDE_TEMPLATES.search(m["name"])
    ]


def generate(state):
    templates = fetch_templates()
    by_id = {t["id"]: t for t in templates}
    history = state["history"]
    theme = pick_theme(history)
    cands = pick_candidates(templates, history)
    cand_ids = {c["id"] for c in cands}
    recent = [f"{h['top']} / {h['bottom']}" for h in history[-15:]]
    listing = "\n".join(f"{c['id']} | {c['name']}" for c in cands)

    prompt = (
        "You run a huge, hilarious Instagram meme page. Write ONE original meme.\n\n"
        f"Theme: {theme}\n\n"
        "Pick the best-fitting template from this list (id | name). Each is a single "
        "image with text on the top and the bottom:\n"
        f"{listing}\n\n"
        "Humor rules:\n"
        "- Hyper-relatable, specific, understood in two seconds. The kind of meme people "
        "send to a friend with 'this is literally you'.\n"
        "- Setup on top, punchline on bottom, each under 12 words. Respect the template's "
        "famous meaning.\n"
        "- Do not reuse or paraphrase these recent jokes: " + json.dumps(recent) + "\n\n"
        "Hard safety rules (an account ban is the worst outcome):\n"
        "- No politics, religion, race, ethnicity, nationality, gender, sexuality, "
        "disability or tragedy jokes. No insults aimed at groups.\n"
        "- No sex, nudity, drugs, violence, self-harm, profanity, real people's names or "
        "brand attacks.\n"
        "- Mild, friendly, self-deprecating humor only.\n\n"
        "Caption: one short line that adds to the joke, sometimes ending with a soft share "
        "hook like 'Send this to the friend who does this'. Never beg for likes or follows. "
        "Give 3 to 5 relevant hashtags.\n\n"
        'Return JSON only: {"template_id": "...", "top": "...", "bottom": "...", '
        '"caption": "...", "hashtags": ["#..."]}'
    )

    for attempt in range(4):
        try:
            data = json.loads(gemini(prompt))
            if isinstance(data, list):
                data = data[0]
            tid = str(data["template_id"])
            top, bottom = data["top"].strip(), data["bottom"].strip()
            caption = data["caption"].strip()
            tags = []
            for h in data["hashtags"][:5]:
                h = "#" + re.sub(r"[^\w]", "", h)
                if len(h) > 2:
                    tags.append(h)
        except Exception as e:
            print(f"bad generation ({e}), retrying")
            continue
        if tid not in cand_ids or not top or not bottom or len(top) > 90 or len(bottom) > 90 \
                or len(caption) > 200 or len(tags) < 3:
            print("generation failed validation, retrying")
            continue
        if f"{top} / {bottom}" in recent:
            continue
        everything = " ".join([top, bottom, caption, *tags])
        if BLOCKED.search(everything):
            print("local blocklist hit, retrying")
            continue
        meme = {"template_id": tid, "top": top, "bottom": bottom,
                "caption": caption, "hashtags": tags, "theme": theme}
        if not moderate(meme, by_id[tid]["name"]):
            print("moderator rejected, retrying")
            continue
        return meme, by_id[tid]
    raise RuntimeError("Could not produce a safe meme after several attempts")


# ------------------------------------------------------------------------ rendering

def load_font(size):
    for p in FONT_PATHS:
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


def fit_text(text, max_w, max_h, start):
    size = start
    while True:
        font = load_font(size)
        lines = wrap(text, font, max_w)
        lh = size * 1.12
        fits = len(lines) * lh <= max_h and all(font.getlength(l) <= max_w for l in lines)
        if fits or size <= 18:
            return font, size, lines, lh
        size -= 2


def render(meme, template):
    raw = requests.get(template["url"], timeout=30).content
    img = Image.open(BytesIO(raw)).convert("RGB")
    w, h = img.size
    img = img.resize((1080, round(h * 1080 / w)), Image.LANCZOS)
    W, H = img.size
    draw = ImageDraw.Draw(img)
    max_w, max_h, margin = int(W * 0.92), int(H * 0.30), int(H * 0.025)
    for text, where in ((meme["top"], "top"), (meme["bottom"], "bottom")):
        font, size, lines, lh = fit_text(text.upper(), max_w, max_h, int(W * 0.095))
        y = margin if where == "top" else H - margin - lh * len(lines)
        for line in lines:
            draw.text((W / 2, y), line, font=font, fill="white",
                      stroke_width=max(2, size // 12), stroke_fill="black", anchor="ma")
            y += lh
    return img


# ------------------------------------------------------------------------ Instagram

def graph(method, path, **params):
    params["access_token"] = env("IG_ACCESS_TOKEN")
    kwargs = {"params": params} if method == "GET" else {"data": params}
    r = requests.request(method, f"{GRAPH}/{path}", timeout=60, **kwargs)
    if not r.ok:
        raise RuntimeError(f"Instagram API {r.status_code}: {r.text[:500]}")
    return r.json()


def host_image(path):
    if not os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError("Publishing only works inside GitHub Actions (needs a public image URL)")
    commit_push(f"bot: add {path.name}")
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/images/{path.name}"
    for _ in range(20):
        if requests.get(url, timeout=15, stream=True).status_code == 200:
            return url
        time.sleep(3)
    raise RuntimeError("Hosted image never became reachable (is the repo public?)")


def publish(image_url, caption):
    ig = env("IG_USER_ID")
    cid = graph("POST", f"{ig}/media", image_url=image_url, caption=caption)["id"]
    for _ in range(30):
        status = graph("GET", cid, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container status: {status}")
        time.sleep(3)
    return graph("POST", f"{ig}/media_publish", creation_id=cid)["id"]


def cleanup_images():
    for f in IMG_DIR.glob("*.jpg"):
        try:
            if time.time() - int(f.name.split("_")[0]) > 3 * 86400:
                f.unlink()
        except ValueError:
            pass


# ---------------------------------------------------------------------------- main

def run_post(state, slot, dry):
    meme, template = generate(state)
    print(json.dumps(meme, indent=1), "| template:", template["name"])
    img = render(meme, template)
    if dry:
        OUT_DIR.mkdir(exist_ok=True)
        img.save(OUT_DIR / "preview.jpg", quality=92)
        (OUT_DIR / "preview.json").write_text(json.dumps(meme, indent=1))
        print("Dry run: preview saved to out/preview.jpg")
        return
    IMG_DIR.mkdir(exist_ok=True)
    path = IMG_DIR / f"{int(time.time())}_{template['id']}.jpg"
    img.save(path, quality=92)
    url = host_image(path)
    caption = meme["caption"] + "\n\n" + " ".join(meme["hashtags"])
    media_id = publish(url, caption)
    slot["status"] = "done"
    state["history"].append({
        "media_id": media_id,
        "posted_at": now_utc().isoformat(),
        "template_id": template["id"],
        "template_name": template["name"],
        "theme": meme["theme"],
        "top": meme["top"],
        "bottom": meme["bottom"],
    })
    print(f"Posted {media_id}")


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    force = os.environ.get("FORCE") == "1"
    now = now_utc()
    state = load_state()
    try:
        plan_day(state, now)
        if not dry:
            refresh_metrics(state, now)
            cleanup_images()
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
    finally:
        if not dry:
            save_state(state)
            commit_push("bot: state update")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)  # failed run => GitHub emails you automatically
