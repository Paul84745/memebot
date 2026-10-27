"""Turns a meme image into a short looping Reel with original, royalty-free music.

The music is synthesised here from scratch (no samples, no copyrighted material), so it can
never trigger a copyright claim or get the account muted/banned. Several moods are available
and the bot learns which one performs best.
"""
import os
import random
import re
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import requests
from urllib.parse import quote
from PIL import Image, ImageFilter

SR = 44100
DURATION = int(os.environ.get("REEL_SECONDS", "7"))  # short loops get rewatched; rewatches count as views

# name -> (bpm, root MIDI note, chord offsets per bar, scale for the melody, style)
TRACKS = {
    "chill":    dict(bpm=78,  root=57, bars=[(0, 3, 7, 10), (5, 8, 12, 15), (3, 7, 10, 14), (7, 10, 14, 17)],
                     scale=[0, 3, 5, 7, 10, 12], wave="soft", drums="lofi"),
    "upbeat":   dict(bpm=116, root=60, bars=[(0, 4, 7), (7, 11, 14), (9, 12, 16), (5, 9, 12)],
                     scale=[0, 2, 4, 7, 9, 12], wave="pluck", drums="four"),
    "bouncy":   dict(bpm=104, root=55, bars=[(0, 4, 7), (5, 9, 12), (0, 4, 7), (7, 11, 14)],
                     scale=[0, 2, 4, 5, 7, 9, 12], wave="pluck", drums="swing"),
    "sad":      dict(bpm=62,  root=57, bars=[(0, 3, 7), (-4, 0, 3), (-7, -4, 0), (-2, 2, 5)],
                     scale=[0, 3, 5, 7, 10], wave="soft", drums="none"),
    "wholesome": dict(bpm=84, root=60, bars=[(0, 4, 7), (9, 12, 16), (5, 9, 12), (7, 11, 14)],
                     scale=[0, 2, 4, 7, 9, 12], wave="soft", drums="none"),
    "awkward":  dict(bpm=90,  root=56, bars=[(0, 3, 6), (1, 4, 7), (0, 3, 6), (-1, 2, 5)],
                     scale=[0, 1, 3, 6, 7], wave="pluck", drums="slow"),
    "hype":     dict(bpm=128, root=62, bars=[(0, 4, 7), (5, 9, 12), (7, 11, 14), (0, 4, 7)],
                     scale=[0, 2, 4, 7, 9, 12], wave="pluck", drums="four"),
    "dramatic": dict(bpm=70,  root=48, bars=[(0, 3, 7), (-4, 0, 3), (-2, 2, 5), (-5, -2, 2)],
                     scale=[0, 3, 5, 7, 8, 12], wave="soft", drums="slow"),
}


def _midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def _tone(freq, dur, kind):
    t = np.arange(int(SR * dur)) / SR
    if kind == "pluck":
        env = np.exp(-t * 7)
        s = (np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(4 * np.pi * freq * t)
             + 0.15 * np.sin(6 * np.pi * freq * t))
    else:  # soft pad / keys
        env = np.minimum(t / 0.03, 1) * np.exp(-t * 2.2)
        s = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t + 0.5)
    return s * env


def _drum(kind, length):
    t = np.arange(int(SR * length)) / SR
    if kind == "kick":
        f = 120 * np.exp(-t * 25) + 45
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 11)
    if kind == "hat":
        rng = np.random.default_rng(7)
        return rng.standard_normal(len(t)) * np.exp(-t * 70) * 0.5
    rng = np.random.default_rng(3)  # snare
    return (rng.standard_normal(len(t)) * np.exp(-t * 22) * 0.7
            + np.sin(2 * np.pi * 190 * t) * np.exp(-t * 28) * 0.4)


def _add(buf, sig, start, gain):
    i = int(start * SR)
    if i >= len(buf):
        return
    sig = sig[: len(buf) - i]
    buf[i:i + len(sig)] += sig * gain


def synth(name, out_wav, seconds=DURATION, seed=0):
    cfg = TRACKS[name]
    rng = np.random.default_rng(seed)
    beat = 60.0 / cfg["bpm"]
    buf = np.zeros(int(SR * (seconds + 1)))
    n_beats = int(seconds / beat) + 2
    for b in range(n_beats):
        bar = (b // 4) % len(cfg["bars"])
        chord = cfg["bars"][bar]
        t0 = b * beat
        # bass on every beat 1 & 3, chord stabs on beat 1
        if b % 4 in (0, 2):
            _add(buf, _tone(_midi(cfg["root"] + chord[0] - 12), beat * 1.6, "soft"), t0, 0.55)
        if b % 4 == 0:
            for n in chord:
                _add(buf, _tone(_midi(cfg["root"] + n), beat * 3.6, cfg["wave"]), t0, 0.16)
        # melody: two eighth notes per beat, sparse, from the scale
        for k in range(2):
            if rng.random() < 0.6:
                note = cfg["root"] + 12 + int(rng.choice(cfg["scale"]))
                _add(buf, _tone(_midi(note), beat * 0.9, cfg["wave"]), t0 + k * beat / 2, 0.14)
        d = cfg["drums"]
        if d in ("four", "swing"):
            _add(buf, _drum("kick", 0.3), t0, 0.7)
            _add(buf, _drum("hat", 0.1), t0 + beat / 2, 0.35)
            if b % 2 == 1:
                _add(buf, _drum("snare", 0.25), t0, 0.5)
        elif d == "lofi":
            if b % 4 in (0, 2):
                _add(buf, _drum("kick", 0.3), t0, 0.6)
            if b % 4 in (1, 3):
                _add(buf, _drum("snare", 0.25), t0, 0.35)
            _add(buf, _drum("hat", 0.1), t0 + beat / 2, 0.25)
        elif d == "slow" and b % 4 == 0:
            _add(buf, _drum("kick", 0.4), t0, 0.8)
    buf = buf[: int(SR * seconds)]
    # gentle low-pass for a warmer sound, then fades so the loop restarts cleanly
    k = np.ones(4) / 4
    buf = np.convolve(buf, k, mode="same")
    fade = int(SR * 0.25)
    buf[:int(SR * 0.02)] *= np.linspace(0, 1, int(SR * 0.02))
    buf[-fade:] *= np.linspace(1, 0, fade)
    peak = np.max(np.abs(buf)) or 1
    buf = buf / peak * 0.85
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((buf * 32767).astype(np.int16).tobytes())


MOOD_QUERIES = {
    "chill": ["lofi", "chill", "lofi hip hop"],
    "upbeat": ["happy", "upbeat", "funk", "pop"],
    "bouncy": ["funny", "quirky", "playful"],
    "hype": ["phonk", "trap", "energetic", "brazilian funk", "dance"],
    "sad": ["sad", "piano", "melancholy"],
    "wholesome": ["acoustic", "ukulele", "cheerful"],
    "awkward": ["quirky", "comedy", "silly"],
    "dramatic": ["dramatic", "cinematic", "suspense"],
}


FX_FILTERS = {
    # the two edits that dominate trending Reels audio
    "sped_up": "asetrate=44100*1.22,aresample=44100,",
    "slowed_reverb": "asetrate=44100*0.86,aresample=44100,aecho=0.8:0.82:70|140:0.32|0.2,",
    "none": "",
}
LAST = {}  # details of the last track used, for the bot's learning log


SEARCH_LOG = []


_MODE = {"category": True}


def _search(q):
    """Openverse search for songs. If the music-category search keeps timing out, switch to a
    plain search and keep only song-length results (sound effects are short)."""
    if _MODE.get("down"):
        return []
    params = {"q": q, "license": "cc0,pdm,by", "page_size": 20}
    if _MODE["category"]:
        params["category"] = "music"
    t0 = time.time()
    try:
        r = requests.get("https://api.openverse.org/v1/audio/", timeout=25, params=params,
                         headers={"User-Agent": "meme-bot/1.0"})
    except requests.exceptions.RequestException as e:
        SEARCH_LOG.append(f"{q}:{'cat' if _MODE['category'] else 'plain'} ERR {type(e).__name__}")
        if _MODE["category"]:
            _MODE["category"] = False
            return _search(q)
        _MODE["down"] = True  # Openverse unreachable: don't spend minutes retrying every query
        return []
    res = r.json().get("results", []) if r.ok else []
    if not _MODE["category"]:
        res = [x for x in res if (x.get("duration") or 0) >= 60000]
    # real songs only: long enough to have a proper section, never a one-shot sound effect
    pool = [x for x in res if (x.get("duration") or 0) >= 30000 and x.get("url")
            and not re.search(r"\b(kick|snare|hat|loop|sfx|fx|sample|one ?shot|drum hit)\b",
                              (x.get("title") or "").lower())]
    SEARCH_LOG.append(f"{q}:{'cat' if _MODE['category'] else 'plain'} {r.status_code} "
                      f"{len(res)}/{len(pool)} {time.time() - t0:.0f}s")
    print(f"openverse q={q!r}: {len(res)} results, {len(pool)} usable songs")
    # public-domain first (no credit needed), then CC BY (credited in the post caption)
    random.shuffle(pool)
    pool.sort(key=lambda x: x.get("license") not in ("cc0", "pdm"))
    return pool


def _ia_length(v):
    """Internet Archive file length: '183.42' seconds or 'm:ss' / 'h:mm:ss'."""
    try:
        parts = [float(x) for x in str(v).split(":")]
    except ValueError:
        return 0
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return sec


def _ia_license(url):
    u = (url or "").lower()
    if "publicdomain" in u or "/zero/" in u:
        return "pdm"
    if re.search(r"/licenses/by/\d", u):
        return "by"
    return None  # NC / ND / SA and unlicensed music are never used


def _archive_search(q, want=5):
    """Second music source: the Internet Archive's netlabel collection (free CC music by
    independent labels, no key needed). Only public-domain and CC BY tracks."""
    if _MODE.get("ia_down"):
        return []
    t0 = time.time()
    try:
        r = requests.get("https://archive.org/advancedsearch.php", timeout=20, params={
            "q": f"({q}) AND mediatype:(audio) AND collection:(netlabels)",
            "fl[]": ["identifier", "title", "creator", "licenseurl"],
            "sort[]": "downloads desc", "rows": 60, "output": "json"},
            headers={"User-Agent": "meme-bot/1.0"})
        docs = r.json().get("response", {}).get("docs", []) if r.ok else []
    except Exception as e:
        SEARCH_LOG.append(f"{q}:archive ERR {type(e).__name__}")
        _MODE["ia_down"] = True
        return []
    docs = [d for d in docs if _ia_license(d.get("licenseurl"))]
    # mostly the popular releases (better produced), with some variety
    docs = docs[:12] + random.sample(docs[12:], min(6, len(docs[12:])))
    random.shuffle(docs)
    pool = []
    for d in docs:
        if len(pool) >= want or time.time() - t0 > 60:
            break
        try:
            meta = requests.get(f"https://archive.org/metadata/{d['identifier']}", timeout=15,
                                headers={"User-Agent": "meme-bot/1.0"}).json()
        except Exception:
            continue
        files = [f for f in meta.get("files", []) if str(f.get("name", "")).lower().endswith(".mp3")
                 and 60 <= _ia_length(f.get("length")) <= 900
                 and int(f.get("size") or 0) < 25 * 1024 * 1024]
        if not files:
            continue
        f = random.choice(files)
        creator = d.get("creator")
        if isinstance(creator, list):
            creator = creator[0] if creator else None
        pool.append({"title": f.get("title") or re.sub(r"\.mp3$", "", f["name"], flags=re.I),
                     "creator": f.get("creator") or creator,
                     "url": f"https://archive.org/download/{d['identifier']}/{quote(f['name'])}",
                     "duration": _ia_length(f.get("length")) * 1000,
                     "license": _ia_license(d.get("licenseurl")), "source": "archive.org"})
    SEARCH_LOG.append(f"{q}:archive {r.status_code} {len(docs)}/{len(pool)} {time.time() - t0:.0f}s")
    print(f"archive.org q={q!r}: {len(docs)} licensed releases, {len(pool)} usable songs")
    pool.sort(key=lambda x: x.get("license") != "pdm")
    return pool


def real_track(mood, workdir, style=None):
    """A real produced song via Openverse, or the Internet Archive when Openverse is down
    (public domain, or CC BY with credit in the caption),
    trimmed to the reel length.
    With a trend style, searches that genre first and applies its edit (sped up / slowed)."""
    workdir = Path(workdir)
    queries = MOOD_QUERIES.get(mood, ["upbeat"])[:]
    random.shuffle(queries)
    if style:
        queries = [style["query"]] + queries
    fx = (style or {}).get("fx", "none")
    pool, used_q = [], None
    for q in queries + ["music"]:
        pool = _search(q)
        if not pool and _MODE.get("down"):
            pool = _archive_search(q)  # Openverse unreachable: use the Internet Archive instead
        if pool:
            used_q = q
            break
    if style and used_q != style["query"]:
        fx = "none"  # the trend genre had nothing usable; don't apply its edit to another genre
    for item in pool[:5]:
        try:
            raw = workdir / "src_audio"
            with requests.get(item["url"], timeout=60, stream=True) as resp:
                resp.raise_for_status()
                size = 0
                with open(raw, "wb") as f:
                    for chunk in resp.iter_content(1 << 16):
                        size += len(chunk)
                        if size > 25 * 1024 * 1024:
                            raise RuntimeError("too big")
                        f.write(chunk)
            need = DURATION * 1.3 + 1
            start = max(0, min(item["duration"] / 1000 * 0.25, item["duration"] / 1000 - need))
            out = workdir / "music.wav"
            af = (FX_FILTERS.get(fx, "") + f"afade=t=in:d=0.3,afade=t=out:st={DURATION - 0.6}:d=0.6,loudnorm=I=-14")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.1f}", "-i", str(raw),
                            "-af", af, "-t", str(DURATION), "-ac", "2", "-ar", "44100", str(out)],
                           check=True, timeout=120)
            LAST.clear()
            LAST.update(title=item.get("title"), query=used_q, fx=fx,
                        style=style["query"] if style and used_q == style["query"] else None,
                        credit=(f"music: {item.get('title')} by {item.get('creator') or 'unknown'} (CC BY)"
                                if item.get("license") == "by" else None))
            print(f"::notice::track for {mood}: {item.get('title')!r} ({item.get('license')}, "
                  f"{item.get('source', 'openverse')}, "
                  f"search {used_q!r}, edit {fx})")
            return out
        except Exception as e:
            print(f"::warning::track failed ({str(e)[:200]}), trying next")
    raise RuntimeError("no usable real track; searches: " + "; ".join(SEARCH_LOG[-6:]))


def vertical_frame(img):
    """Place the meme on a blurred 9:16 backdrop (1080x1920)."""
    W, H = 1080, 1920
    bg = img.copy()
    bg = bg.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    dim = Image.new("RGB", (W, H), (0, 0, 0))
    bg = Image.blend(bg, dim, 0.45)
    scale = min(W / img.width, 1300 / img.height)
    fg = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2 - 60))
    return bg


def make_reel(img, track, out_mp4, workdir, style=None):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    frame = workdir / "frame.png"
    wav = workdir / "music.wav"
    vertical_frame(img).save(frame)
    try:
        wav = real_track(track, workdir, style)
    except Exception as e:
        LAST.clear()
        LAST.update(title="built-in synth", query=None, fx="none", style=None)
        print(f"::warning::real music unavailable ({str(e)[:300]}); using built-in synth")
        synth(track, wav)
    frames = DURATION * 30
    # Text is fully readable from frame 1 (no intro); a very slow push-in keeps it alive
    # without a visible jump when the loop restarts.
    vf = (f"zoompan=z='min(1+0.0002*on,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s=1080x1920:fps=30,format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(frame), "-i", str(wav),
           "-vf", vf, "-t", str(DURATION), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart",
           "-shortest", str(out_mp4)]
    subprocess.run(cmd, check=True, timeout=300)
    return out_mp4
