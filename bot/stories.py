"""Daily interactive Instagram Stories.

Instagram's API cannot add real poll or quiz stickers to a Story (only the app can), so these
Stories ask for the answer in the replies instead: "reply 1 or 2", "rate it 1-10", "A or B?",
"caption this". Replies land in the page's DMs and count as interaction, which keeps the page
at the front of followers' Story bar and feed. The bot learns which kind of Story works best.
"""
import os
import random
import re
import statistics
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFilter

W, H = 1080, 1920
# Instagram's own UI covers roughly the top 220px (profile bar) and bottom 280px (reply box).
TOP, BOTTOM = 230, 1630
HANDLE = os.environ.get("PAGE_HANDLE", "@plqmemes")

TYPES = {
    "faceoff": "two recent memes from the page: which one is funnier? reply 1 or 2",
    "rate": "a brand-new meme: rate it 1-10 in the replies",
    "wyr": "an unhinged 'this or that' question: reply A or B",
    "caption_this": "a reaction photo without a caption: reply with your caption",
}

ACCENTS = [(255, 214, 0), (0, 230, 160), (255, 92, 140), (90, 170, 255), (255, 140, 40)]


# ------------------------------------------------------------------------- learning

def score_story(m):
    """Story score per 1,000 people reached. Follows weigh most, then shares and replies.
    (Instagram reports replies as 0 for pages in Europe, so the other signals carry it there.)"""
    reach = m.get("reach")
    if not reach:
        return None
    raw = (20 * (m.get("follows") or 0) + 8 * (m.get("shares") or 0)
           + 4 * (m.get("replies") or 0) + 2 * (m.get("profile_visits") or 0))
    return round(1000 * raw / max(reach, 30), 1)


def done_stories(history):
    return [h for h in history if h.get("kind") == "story" and h.get("final")
            and h.get("score") is not None]


def pick_type(history):
    done = done_stories(history)
    keys = list(TYPES)
    if len(done) < 6 or random.random() < 0.3:
        return random.choice(keys)
    by = {}
    for h in done:
        by.setdefault(h.get("story_type"), []).append(h["score"])
    overall = statistics.mean(h["score"] for h in done)
    w = [(statistics.mean(by[k]) if len(by.get(k, [])) >= 2 else overall) + 1 for k in keys]
    return random.choices(keys, w)[0]


def report(history):
    done = done_stories(history)
    if not done:
        return []
    by = {}
    for h in done:
        by.setdefault(h.get("story_type") or "?", []).append(h)
    lines = [f"## Stories ({len(done)} measured)"]
    for k, hs in sorted(by.items(), key=lambda kv: statistics.mean(x["score"] for x in kv[1]), reverse=True):
        lines.append(f"- {k}: score {statistics.mean(x['score'] for x in hs):.1f}, "
                     f"reach {statistics.mean(x.get('reach') or 0 for x in hs):.0f}, "
                     f"follows {sum(x.get('follows') or 0 for x in hs)} ({len(hs)} stories)")
    return lines + [""]


# ------------------------------------------------------------------------- drawing

def _bg(accent):
    """Dark vertical gradient tinted with the accent colour."""
    top, bot = (16, 16, 22), tuple(int(c * 0.28) + 10 for c in accent)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    return img


def _blur_bg(img):
    bg = img.copy().resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(45))
    return Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.68)


def _text_block(draw, text, ctx, y, size, fill, bold=True, max_w=W - 140, max_lines=3, gap=1.18):
    """Centred text; shrinks until it fits in max_lines. Returns the y below the block."""
    while True:
        font = ctx.load_font(size, bold=bold)
        lines = ctx.wrap(text, font, max_w)
        if len(lines) <= max_lines or size <= 34:
            break
        size -= 4
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((W - w) / 2, y), line, font=font, fill=fill)
        y += round(size * gap)
    return y


def _pill(draw, text, ctx, y, accent, size=58):
    """The call-to-action button near the bottom ('reply 1 or 2')."""
    font = ctx.load_font(size, bold=True)
    tw = draw.textlength(text, font=font)
    pad_x, pad_y = 48, 26
    x0 = (W - tw) / 2 - pad_x
    draw.rounded_rectangle([x0, y, x0 + tw + 2 * pad_x, y + size + 2 * pad_y], radius=60, fill=accent)
    draw.text(((W - tw) / 2, y + pad_y - size * 0.08), text, font=font, fill=(15, 15, 15))


def _handle(draw, ctx):
    font = ctx.load_font(34, bold=True)
    w = draw.textlength(HANDLE, font=font)
    draw.text(((W - w) / 2, TOP - 10), HANDLE, font=font, fill=(200, 200, 200))


def _fit(img, max_w, max_h):
    s = min(max_w / img.width, max_h / img.height)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def _badge(draw, ctx, x, y, label, accent):
    r = 46
    draw.ellipse([x - r, y - r, x + r, y + r], fill=accent, outline=(15, 15, 15), width=5)
    font = ctx.load_font(56, bold=True)
    w = draw.textlength(label, font=font)
    draw.text((x - w / 2, y - 34), label, font=font, fill=(15, 15, 15))


# ------------------------------------------------------------------------- the four types

def _faceoff(state, ctx, accent):
    now = datetime.now(timezone.utc)
    recent = [h for h in state["history"]
              if h.get("kind") in ("reel", "image") and h.get("text") and str(h.get("template_id", "")).isdigit()
              and now - datetime.fromisoformat(h["posted_at"]) < timedelta(hours=72)]
    by_id = {t["id"]: t for t in ctx.fetch_templates()}
    recent = [h for h in recent if h["template_id"] in by_id]
    recent.sort(key=lambda h: (h.get("funny") or 0, h["posted_at"]), reverse=True)
    pair, used = [], set()
    for h in recent:
        if h["template_id"] not in used:
            pair.append(h)
            used.add(h["template_id"])
        if len(pair) == 2:
            break
    if len(pair) < 2:
        return None
    random.shuffle(pair)
    img = _bg(accent)
    d = ImageDraw.Draw(img)
    _handle(d, ctx)
    y = _text_block(d, "which one is funnier?", ctx, TOP + 60, 74, (255, 255, 255)) + 25
    slot_h = (BOTTOM - 150 - y - 30) // 2
    for i, h in enumerate(pair):
        meme = ctx.render({"text": h["text"]}, by_id[h["template_id"]], dark=False, max_total=760)
        m = _fit(meme, W - 190, slot_h)
        x = (W - m.width) // 2
        img.paste(m, (x, y))
        _badge(d, ctx, x - 8, y + 8, str(i + 1), accent)
        y += m.height + 30
    _pill(d, "reply 1 or 2", ctx, BOTTOM - 120, accent)
    return img, {"text": " || ".join(h["text"] for h in pair), "template_id": "story"}


def _rate(state, ctx, accent):
    items = ctx.make_memes(state, want=1, n_cands=8)
    if not items:
        return None
    meme, tpl = items[0]
    m_img = ctx.render(meme, tpl, dark=False)
    img = _blur_bg(m_img)
    d = ImageDraw.Draw(img)
    _handle(d, ctx)
    y = _text_block(d, "rate this 1-10", ctx, TOP + 60, 80, (255, 255, 255))
    m = _fit(m_img, W - 120, BOTTOM - 170 - y - 20)
    img.paste(m, ((W - m.width) // 2, y + 10))
    _pill(d, "reply with your number", ctx, BOTTOM - 120, accent)
    return img, {"text": meme["text"], "template_id": tpl["id"], "news": meme.get("news")}


WYR_PROMPT = """You write Instagram Story questions for an EDGY meme page (followers aged 16-30).
Write 8 different "this or that" / "would you rather" questions that people can't resist
answering. Both options must be painful, cursed or funny so choosing is hard, and hyper-specific
everyday situations (texting, money, school, work, gym, family, dating, sleep, food, group chats).
Dark, crude and cringe humor is welcome; suggestive is fine, explicit is not.
Hard lines: no slurs, nothing demeaning a group (race, religion, nationality, sexuality, gender
identity, disability), no real people's names, no politics, no self-harm, violence or hard drugs,
nothing sexual involving minors.
Keep "q" under 8 words (e.g. "would you rather", "pick one", "which is worse") and each option
under 11 words, lowercase casual voice.
Avoid repeating these recent ones: {recent}
Return JSON only: {{"questions": [{{"q": "...", "a": "...", "b": "..."}}]}}"""


def _wyr(state, ctx, accent):
    recent = [h["text"] for h in state["history"][-60:] if h.get("story_type") == "wyr"][-12:]
    try:
        qs = ctx.items(ctx.llm(WYR_PROMPT.format(recent=recent), temperature=1.0, max_tokens=1500), "questions")
    except Exception as e:
        print(f"wyr generation failed: {e}")
        return None
    qs = [q for q in qs if all(isinstance(q.get(k), str) and 2 <= len(q[k]) <= 90 for k in "qab")]
    qs = [q for q in qs if not ctx.BLOCKED.search(" ".join((q["q"], q["a"], q["b"])))]
    ok = safe_texts(ctx, [f"{q['q']}: (A) {q['a']} or (B) {q['b']}" for q in qs])
    qs = [q for q, good in zip(qs, ok) if good]
    if not qs:
        return None
    q = random.choice(qs)
    img = _bg(accent)
    d = ImageDraw.Draw(img)
    _handle(d, ctx)
    y = _text_block(d, q["q"].rstrip("?") + "?", ctx, TOP + 90, 84, (255, 255, 255))
    box_h = 420
    gap = 70
    y0 = max(y + 60, (TOP + BOTTOM - 2 * box_h - gap) // 2)
    for i, (label, opt) in enumerate((("A", q["a"]), ("B", q["b"]))):
        by = y0 + i * (box_h + gap)
        d.rounded_rectangle([70, by, W - 70, by + box_h], radius=40, fill=(255, 255, 255))
        fy = by + box_h // 2 - 60
        _text_block(d, opt, ctx, fy, 58, (15, 15, 15), max_w=W - 260, max_lines=3)
        _badge(d, ctx, 70 + 10, by + 10, label, accent)
        if i == 0:
            font = ctx.load_font(44, bold=True)
            tw = d.textlength("or", font=font)
            d.text(((W - tw) / 2, by + box_h + gap / 2 - 26), "or", font=font, fill=(220, 220, 220))
    _pill(d, "reply A or B", ctx, BOTTOM - 120, accent)
    return img, {"text": f"{q['q']} | A: {q['a']} | B: {q['b']}", "template_id": "story"}


def _caption_this(state, ctx, accent):
    used = {h.get("template_id") for h in state["history"][-20:]}
    tpls = sorted(ctx.fetch_templates(), key=lambda t: float(t.get("captions") or 0), reverse=True)[:50]
    tpls = [t for t in tpls if t["id"] not in used] or tpls
    tpl = random.choice(tpls)
    photo = ctx.fetch_image(tpl["url"])
    img = _blur_bg(photo)
    d = ImageDraw.Draw(img)
    _handle(d, ctx)
    y = _text_block(d, "caption this", ctx, TOP + 60, 88, (255, 255, 255))
    p = _fit(photo, W - 120, BOTTOM - 180 - y - 20)
    img.paste(p, ((W - p.width) // 2, y + 10))
    _pill(d, "reply with your caption", ctx, BOTTOM - 120, accent)
    return img, {"text": f"caption this: {tpl['name']}", "template_id": tpl["id"]}


BUILDERS = {"faceoff": _faceoff, "rate": _rate, "wyr": _wyr, "caption_this": _caption_this}


def make(state, kind, ctx):
    """Returns (kind, image, meta). Falls back to another type if the chosen one can't be made."""
    order = [kind] + [k for k in ("rate", "caption_this", "wyr", "faceoff") if k != kind]
    for k in order:
        try:
            out = BUILDERS[k](state, ctx, random.choice(ACCENTS))
        except Exception as e:
            print(f"::warning::story type {k} failed: {str(e)[:200]}")
            out = None
        if out:
            if k != kind:
                print(f"story: {kind} not possible right now, made {k} instead")
            return k, out[0].convert("RGB"), out[1]
    raise RuntimeError("could not make any story")


def safe_texts(ctx, texts):
    """Ban-risk check for plain texts (same bar as the meme moderator). Failure = unsafe."""
    if not texts:
        return []
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    prompt = (
        "You review Instagram Story questions for an edgy meme page aimed at young adults. For EACH "
        "decide if it would realistically get the account BANNED (not merely edgy). Dark humor, crude "
        "jokes, innuendo and mild profanity are SAFE. UNSAFE only if it has: slurs; demeaning a group "
        "(race, ethnicity, religion, nationality, sexuality, gender identity, disability); explicit "
        "sexual description; a real person's name or politics; glorified violence, self-harm or hard "
        "drugs; anything sexual involving minors.\n\n" + listing + "\n\n"
        'Reply JSON only: {"results": [{"i": 0, "safe": true}, ...]}')
    verdict = [False] * len(texts)
    try:
        for it in ctx.items(ctx.llm(prompt, temperature=0.0, max_tokens=1200), "results"):
            i = int(it.get("i", -1))
            if 0 <= i < len(texts):
                verdict[i] = it.get("safe") is True
    except Exception as e:
        print(f"story moderation failed, treating as unsafe: {e}")
    return verdict


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()
