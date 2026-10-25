"""Turns a meme image into a short looping Reel with original, royalty-free music.

The music is synthesised here from scratch (no samples, no copyrighted material), so it can
never trigger a copyright claim or get the account muted/banned. Several moods are available
and the bot learns which one performs best.
"""
import os
import random
import subprocess
import wave
from pathlib import Path

import numpy as np
import requests
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
    "chill": ["chill", "lofi", "relax"],
    "upbeat": ["happy", "upbeat", "funk"],
    "bouncy": ["funny", "quirky", "playful"],
    "hype": ["energetic", "dance", "electronic"],
    "sad": ["sad", "piano", "melancholy"],
    "wholesome": ["acoustic", "ukulele", "cheerful"],
    "awkward": ["quirky", "comedy", "silly"],
    "dramatic": ["dramatic", "cinematic", "suspense"],
}


def real_track(mood, workdir):
    """A real produced track (CC0/public-domain, via Openverse) trimmed to the reel length."""
    workdir = Path(workdir)
    pool = []
    queries = MOOD_QUERIES.get(mood, ["upbeat"])[:]
    random.shuffle(queries)
    for q in queries + ["music"]:
        for lic in ("cc0,pdm",):
            r = requests.get("https://api.openverse.org/v1/audio/", timeout=30,
                             params={"q": q, "license": lic, "page_size": 20},
                             headers={"User-Agent": "meme-bot/1.0"})
            res = r.json().get("results", []) if r.ok else []
            print(f"::warning::openverse q={q!r} lic={lic} status={r.status_code}: {len(res)} results")
            pool = [x for x in res if (x.get("duration") or 0) >= 25000 and x.get("url")]
            if pool:
                break
        if pool:
            break
    random.shuffle(pool)
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
            start = max(0, min(item["duration"] / 1000 * 0.25, item["duration"] / 1000 - DURATION - 1))
            out = workdir / "music.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.1f}", "-i", str(raw),
                            "-t", str(DURATION), "-ac", "2", "-ar", "44100",
                            "-af", f"afade=t=in:d=0.3,afade=t=out:st={DURATION - 0.6}:d=0.6,loudnorm=I=-14",
                            str(out)], check=True, timeout=120)
            print(f"::notice::real track used for {mood}: {item.get('title')!r} ({item.get('license')})")
            return out
        except Exception as e:
            print(f"::warning::track failed ({str(e)[:200]}), trying next")
    raise RuntimeError("no usable real track")


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


def make_reel(img, track, out_mp4, workdir):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    frame = workdir / "frame.png"
    wav = workdir / "music.wav"
    vertical_frame(img).save(frame)
    try:
        wav = real_track(track, workdir)
    except Exception as e:
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
