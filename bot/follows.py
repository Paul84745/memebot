"""Works out how many followers each post brought in.

Instagram reports follows per post only for feed photos, carousels and Stories, not for Reels.
So on every run the bot notes the page's follower count and how many people each recent post
has reached. The followers gained between two runs are shared out across the posts by how many
NEW people each one reached in that time (a post nobody new saw can't have brought follows).
It is an estimate, but over dozens of posts it shows clearly which jokes, formats and sounds
turn viewers into followers - not just likers.
"""
from datetime import datetime, timedelta

WINDOW_H = 48          # posts are attributed follows for their first 48 hours (when they're finalised)
MIN_GAP = timedelta(minutes=15)
MAX_GAP = timedelta(hours=12)  # longer gaps mix in too much other traffic; skip attributing those


def _ts(s):
    return datetime.fromisoformat(s)


def _reach(graph, media_id):
    try:
        data = graph("GET", f"{media_id}/insights", metric="reach").get("data", [])
        return int(data[0]["values"][0]["value"]) if data else None
    except Exception:
        return None  # e.g. a Story with fewer than 5 viewers, or a post that was deleted


def track(state, graph, ig_user_id, now):
    st = state.get("follow_track") or {}
    if st.get("t") and now - _ts(st["t"]) < MIN_GAP:
        return
    try:
        followers = int(graph("GET", ig_user_id, fields="followers_count")["followers_count"])
    except Exception as e:
        print(f"::warning::follower count unavailable: {str(e)[:200]}")
        return
    recent = [h for h in state["history"] if h.get("media_id")
              and now - _ts(h["posted_at"]) < timedelta(hours=WINDOW_H)]
    prev_reach = st.get("reach", {})
    reach = {}
    for h in recent:
        r = _reach(graph, h["media_id"])
        if r is None:
            r = prev_reach.get(h["media_id"])  # keep the last known value so deltas stay correct
        if r is not None:
            reach[h["media_id"]] = r

    if st.get("followers") is not None and now - _ts(st["t"]) <= MAX_GAP:
        gained = followers - st["followers"]
        prev_t = _ts(st["t"])
        deltas = {}
        for h in recent:
            mid = h["media_id"]
            if mid not in reach:
                continue
            before = prev_reach.get(mid)
            if before is None:
                if _ts(h["posted_at"]) < prev_t:
                    continue  # existed last time but wasn't measured: can't tell what's new
                before = 0
            if reach[mid] > before:
                deltas[mid] = reach[mid] - before
        total = sum(deltas.values())
        if gained and total > 0:
            by_id = {h["media_id"]: h for h in recent}
            for mid, dr in deltas.items():
                h = by_id[mid]
                h["follows_attr"] = round(h.get("follows_attr", 0.0) + gained * dr / total, 3)
            print(f"followers {st['followers']} -> {followers} ({gained:+d}) shared over {len(deltas)} posts")

    state["follow_track"] = {"t": now.isoformat(), "followers": followers, "reach": reach}
    hist = state.setdefault("follower_log", [])
    if not hist or hist[-1][0][:10] != now.isoformat()[:10]:
        hist.append([now.isoformat(), followers])  # one data point per day for the insights file
    state["follower_log"] = hist[-120:]


def follows_for(h, metrics):
    """Final follows for a post: Instagram's own number when it gives one, else the estimate."""
    if metrics.get("follows") is not None:
        return metrics["follows"], "instagram"
    if "follows_attr" in h:
        return round(h["follows_attr"], 1), "estimated"
    return None, None


def report(state, done):
    lines = []
    log = state.get("follower_log") or []
    if len(log) >= 2:
        first, last = log[max(0, len(log) - 8)], log[-1]
        lines += ["## Followers", f"- now {last[1]} ({last[1] - first[1]:+d} since {first[0][:10]})"]
    fp = [h for h in done if h.get("follows_per_1k") is not None]
    if fp:
        if not lines:
            lines.append("## Followers")
        avg = sum(h["follows_per_1k"] for h in fp) / len(fp)
        lines.append(f"- new followers per 1,000 people reached: {avg:.2f} on average ({len(fp)} posts)")
        best = sorted(fp, key=lambda h: h["follows_per_1k"], reverse=True)[:3]
        lines += ["- posts that brought the most followers:"] + [
            f"  - ({h['follows_per_1k']:.2f}/1k, {h.get('follows')} follows) {h.get('text')}" for h in best]
    return lines + ([""] if lines else [])
