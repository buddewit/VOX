"""
MightPulse -> Discord webhook report

Runs once, posts a kingdom-wide power-progression summary to a Discord
channel via a webhook, then exits. Designed to be triggered on a schedule
by GitHub Actions (see .github/workflows/daily-report.yml) rather than run
as a long-lived bot.

Scope: the top ALLIANCE_LIMIT alliances (by power) in KINGDOM_ID, resolved
fresh each run via /kingdoms/{kid}/ranks?board=alliance_power — there is no
longer a single fixed ALLIANCE_TAG. Every one of those alliances' rosters is
pulled via /alliances/{kid}/{tag}?include=roster.

Per MightPulse's docs, player and alliance responses share the same
freshness model on paper (each up to 60 min old, refreshed per-section on
request). In practice, roster power has been observed to lag behind a
direct per-player check — confirmed by comparing a report against live
per-player data — so per-player verification is real and necessary, not
optional.

Verifying every member individually doesn't scale to 100 alliances though
(potentially thousands of members vs. a 5,000/day call cap). So instead:
roster data is used to compute a *provisional* gain ranking first, then only
the top VERIFY_CANDIDATE_POOL provisional gainers (daily and weekly pools,
unioned) get a real per-player refresh, and the final top-20 lists are
ranked from that verified subset. This assumes the true top 20 gainers are
within the wider candidate pool even using stale roster numbers — a
reasonable bet with a 5x-or-more pool, but not a guarantee. Widen
VERIFY_CANDIDATE_POOL if you suspect it's cutting anyone real gainers.

Report contents:
  - Top 20 members by power gained since the previous run (daily).
  - Top 20 members by power gained since the weekly baseline, which resets
    automatically every 7 days.
Each line shows: name, current power, (power gained / % gained).
"""

import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

MIGHTPULSE_API_KEY = os.environ["MIGHTPULSE_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
ALLIANCE_LIMIT = int(os.environ.get("ALLIANCE_LIMIT", "50"))  # top N alliances by power; API caps this at 100

API_BASE = "https://api.mightpulse.com/v1"
SNAPSHOT_FILE = Path(__file__).parent / "last_snapshot.json"
WEEKLY_SNAPSHOT_FILE = Path(__file__).parent / "weekly_snapshot.json"
TOP_N = 20
WEEKLY_RESET_DAYS = 7

# How many provisional top-gainers (per period: daily, weekly) get verified
# with a real per-player call before final ranking. Bounds verification cost
# to roughly 2x this number per run, regardless of kingdom size. Must be
# comfortably larger than TOP_N so the true top 20 isn't cut by stale
# roster-based provisional ranking.
VERIFY_CANDIDATE_POOL = int(os.environ.get("VERIFY_CANDIDATE_POOL", str(TOP_N * 5)))

# Per-player refetching is now done concurrently instead of one-request-at-a-
# time. RATE_LIMIT_PER_MINUTE caps total calls across all workers so we stay
# under MightPulse's 60/min limit (with margin); MAX_WORKERS controls how
# many requests can be in flight at once, which is what actually hides
# network latency. Both are overridable via env vars if needed.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("MIGHTPULSE_RATE_LIMIT_PER_MIN", "55"))
MAX_WORKERS = int(os.environ.get("MIGHTPULSE_MAX_WORKERS", "8"))


class RateLimiter:
    """Thread-safe sliding-window limiter: blocks callers so that no more
    than `max_calls` acquisitions happen in any rolling `period` seconds,
    no matter how many threads are calling it."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.period - (now - self.calls[0])
            time.sleep(max(sleep_for, 0.01))


_rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE, 60.0)


def api_get(path: str, params: dict | None = None) -> dict:
    _rate_limiter.acquire()
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}

    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if resp.status_code == 429:
        # brief backoff and one retry if we ever bump the rate limit
        time.sleep(5)
        resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"MightPulse API {resp.status_code} on {path}: {resp.text}")
    return resp.json()


def fetch_top_alliances(kid: str, limit: int = ALLIANCE_LIMIT) -> list[dict]:
    """Top alliances in this kingdom by power, already ranked by the API.
    Each entry has at least aid, abbr, name, score per the docs.

    The docs don't spell out the top-level key holding the list for this
    endpoint. Guessing key names one at a time proved fragile in practice
    (missed the actual "boards" key first try), so instead this looks
    structurally for a list of dicts shaped like a documented alliance-board
    entry — has an "abbr" field — searching top-level values first, then one
    level of nesting. Fails loudly with the real shape if nothing matches."""
    data = api_get(f"/kingdoms/{kid}/ranks", {"board": "alliance_power", "limit": limit})

    def looks_like_alliance_list(value) -> bool:
        return (
            isinstance(value, list)
            and len(value) > 0
            and all(isinstance(item, dict) and "abbr" in item for item in value)
        )

    for value in data.values():
        if looks_like_alliance_list(value):
            return value

    for value in data.values():
        if isinstance(value, dict):
            for nested in value.values():
                if looks_like_alliance_list(nested):
                    return nested

    raise RuntimeError(
        f"Couldn't find an alliance list (dicts with 'abbr') in /kingdoms/{kid}/ranks "
        f"response — got top-level keys {sorted(data.keys())} with types "
        f"{[(k, type(v).__name__) for k, v in data.items()]}. Inspect the real payload "
        f"and fix fetch_top_alliances()."
    )


def fetch_all_rosters(kid: str, alliance_tags: list[str]) -> tuple[list[dict], list[dict]]:
    """Fetch every listed alliance's roster concurrently (up to MAX_WORKERS
    at a time, paced by the shared RateLimiter). Returns (all_members,
    alliance_infos) — a flat member list across all alliances, plus each
    alliance's own info dict (for a total-power/member-count summary)."""
    alliance_infos: list[dict | None] = [None] * len(alliance_tags)
    members_by_index: list[list[dict]] = [[] for _ in alliance_tags]

    def fetch_one(i: int, tag: str) -> tuple[int, dict | None, list[dict]]:
        try:
            data = api_get(f"/alliances/{kid}/{tag}", {"include": "info,roster"})
            return i, data["alliance"], data["members"]
        except Exception as exc:
            print(f"WARNING: couldn't fetch roster for alliance {tag}: {exc}", file=sys.stderr)
            return i, None, []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_one, i, tag) for i, tag in enumerate(alliance_tags)]
        for future in as_completed(futures):
            i, alliance, members = future.result()
            alliance_infos[i] = alliance
            members_by_index[i] = members

    all_members = [m for members in members_by_index for m in members]
    ok_alliance_infos = [a for a in alliance_infos if a is not None]
    return all_members, ok_alliance_infos


def refresh_members_individually(members: list[dict]) -> list[dict]:
    """Re-fetch each given member individually for live, accurate
    power/activity — confirmed necessary (roster power has been observed to
    lag in practice). The caller scopes `members` to a bounded candidate
    pool rather than passing everyone, since at kingdom scale (thousands of
    members across 100 alliances) verifying every member every run isn't
    feasible under the daily call cap."""
    fresh: list[dict | None] = [None] * len(members)

    def fetch_one(i: int, m: dict) -> tuple[int, dict]:
        gid = m["governor_id"]
        try:
            data = api_get(f"/players/{gid}", {"include": "base"})
            player = data["player"]
            if player.get("power") is None:
                # Verification should never make the data worse than roster
                # already had — a deleted/banned/degenerate account can come
                # back with power: null. Keep the roster estimate instead.
                print(
                    f"WARNING: verified data for {m.get('nick_name', gid)} ({gid}) "
                    f"had no power value — keeping roster estimate instead",
                    file=sys.stderr,
                )
                return i, m
            return i, player
        except Exception as exc:
            print(f"WARNING: couldn't refresh {m.get('nick_name', gid)} ({gid}): {exc}", file=sys.stderr)
            return i, m  # fall back to the (stale) roster entry rather than dropping them

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_one, i, m) for i, m in enumerate(members)]
        for future in as_completed(futures):
            i, player = future.result()
            fresh[i] = player

    return fresh


def provisional_gain_candidates(members: list[dict], previous: dict, pool_size: int) -> list[dict]:
    """Rank members by gain using whatever power we currently have (roster
    data, possibly stale) and return the top `pool_size`. This is a
    shortlist for verification, not the final ranking — only members with a
    previous data point can be ranked at all, same as top_gainers()."""
    candidates = [m for m in members if str(m["governor_id"]) in previous]
    candidates.sort(
        key=lambda m: m["power"] - previous[str(m["governor_id"])],
        reverse=True,
    )
    return candidates[:pool_size]


def apply_verified(members: list[dict], verified: list[dict]) -> list[dict]:
    """Replace entries in `members` with their verified counterparts (by
    governor_id), leaving everyone else's roster data untouched."""
    verified_by_gid = {str(m["governor_id"]): m for m in verified}
    return [verified_by_gid.get(str(m["governor_id"]), m) for m in members]


def sanitize_power(members: list[dict]) -> list[dict]:
    """Last-line safety net: a null/missing power value from either the
    roster or a per-player refresh (e.g. a deleted/banned account) should
    never crash the whole report. Treat it as 0 and log it loudly rather
    than masking it — a report that silently drops someone is worse than
    one that shows them at 0 and lets you go investigate."""
    for m in members:
        if m.get("power") is None:
            gid = m.get("governor_id", "?")
            print(f"WARNING: {m.get('nick_name', gid)} ({gid}) has no power value — treating as 0", file=sys.stderr)
            m["power"] = 0
    return members


# ---------------------------------------------------------------------------
# Daily snapshot (previous run -> now)
# ---------------------------------------------------------------------------

def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text())
    return {}


def save_snapshot(members: list[dict]) -> None:
    data = {str(m["governor_id"]): m["power"] for m in members}
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Weekly snapshot (baseline that resets every WEEKLY_RESET_DAYS days)
# ---------------------------------------------------------------------------

def load_weekly_snapshot() -> dict:
    if WEEKLY_SNAPSHOT_FILE.exists():
        return json.loads(WEEKLY_SNAPSHOT_FILE.read_text())
    return {}


def save_weekly_snapshot(members: list[dict], baseline_date: datetime) -> None:
    data = {
        "baseline_date": baseline_date.isoformat(),
        "powers": {str(m["governor_id"]): m["power"] for m in members},
    }
    WEEKLY_SNAPSHOT_FILE.write_text(json.dumps(data, indent=2))


def get_weekly_baseline(members: list[dict], now: datetime) -> tuple[dict, bool]:
    """Returns (baseline_powers, just_reset). If no baseline exists yet, or
    the existing one is >= WEEKLY_RESET_DAYS old, today's powers become the
    new baseline and just_reset is True (nothing to compare against yet)."""
    snap = load_weekly_snapshot()
    if not snap:
        save_weekly_snapshot(members, now)
        return {}, True

    baseline_date = datetime.fromisoformat(snap["baseline_date"])
    if (now - baseline_date).days >= WEEKLY_RESET_DAYS:
        save_weekly_snapshot(members, now)
        return {}, True

    return snap["powers"], False


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def top_gainers(members: list[dict], previous: dict, top_n: int = TOP_N) -> list[dict]:
    """Members with a previous data point, ranked by power gained (desc)."""
    gainers = []
    for m in members:
        gid = str(m["governor_id"])
        if gid not in previous:
            continue  # can't rank a gain we have no baseline for
        prev_power = previous[gid]
        gain = m["power"] - prev_power
        pct = (gain / prev_power * 100) if prev_power else 0.0
        gainers.append({**m, "gain": gain, "pct": pct})
    gainers.sort(key=lambda m: m["gain"], reverse=True)
    return gainers[:top_n]


def format_gainer_lines(gainers: list[dict]) -> str:
    lines = []
    for m in gainers:
        lines.append(
            f"{m['nick_name']:<20} {m['power']:>12,}  ({m['gain']:+,} / {m['pct']:+.1f}%)"
        )
    return "\n".join(lines)


def chunk_field(label: str, text: str, max_len: int = 1000) -> list[dict]:
    """Split text into <=max_len-char Discord embed fields (code blocks)."""
    fields = []
    chunk = ""
    idx = 1
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > max_len - 6:  # leave room for ``` fences
            fields.append({"name": f"{label} ({idx})" if idx > 1 else label,
                            "value": f"```{chunk}```", "inline": False})
            chunk = ""
            idx += 1
        chunk += line + "\n"
    if chunk:
        fields.append({"name": f"{label} ({idx})" if idx > 1 else label,
                        "value": f"```{chunk}```", "inline": False})
    return fields


def build_payload(
    kingdom_id: str,
    alliance_count: int,
    members: list[dict],
    daily_previous: dict,
    weekly_previous: dict,
    weekly_just_reset: bool,
) -> dict:
    total_power = sum(m["power"] for m in members)

    fields = []

    daily_top = top_gainers(members, daily_previous)
    if daily_top:
        fields.extend(chunk_field(f"Top {len(daily_top)} Daily Gainers", format_gainer_lines(daily_top)))
    else:
        fields.append({"name": "Top Daily Gainers", "value": "No previous snapshot yet — starting today.", "inline": False})

    if weekly_just_reset:
        fields.append({"name": "Top Weekly Gainers", "value": "Weekly tracking reset today — gains will show starting next run.", "inline": False})
    else:
        weekly_top = top_gainers(members, weekly_previous)
        if weekly_top:
            fields.extend(chunk_field(f"Top {len(weekly_top)} Weekly Gainers (7d)", format_gainer_lines(weekly_top)))
        else:
            fields.append({"name": "Top Weekly Gainers (7d)", "value": "No comparable data yet.", "inline": False})

    embed = {
        "title": f"Kingdom {kingdom_id} — top {alliance_count} alliances power report",
        "description": f"Alliances tracked: {alliance_count} · Members: {len(members)} · Total power: {total_power:,}",
        "color": 0x5865F2,  # Discord blurple
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }
    return {"embeds": [embed]}


def post_to_discord(payload: dict) -> None:
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Discord webhook {resp.status_code}: {resp.text}")


def main() -> None:
    now = datetime.now(timezone.utc)

    top_alliances = fetch_top_alliances(KINGDOM_ID, ALLIANCE_LIMIT)
    alliance_tags = [a["abbr"] for a in top_alliances]
    print(f"Resolved {len(alliance_tags)} top alliances by power in kingdom {KINGDOM_ID}")

    members, alliance_infos = fetch_all_rosters(KINGDOM_ID, alliance_tags)
    members = sanitize_power(members)

    daily_previous = load_snapshot()
    weekly_previous, weekly_just_reset = get_weekly_baseline(members, now)

    # Shortlist provisional gainers (roster data, possibly stale) for daily
    # and weekly separately, union them, and only verify that bounded set
    # individually — see module docstring for why we don't verify everyone.
    candidate_pool: dict[str, dict] = {}
    for m in provisional_gain_candidates(members, daily_previous, VERIFY_CANDIDATE_POOL):
        candidate_pool[str(m["governor_id"])] = m
    if not weekly_just_reset:
        for m in provisional_gain_candidates(members, weekly_previous, VERIFY_CANDIDATE_POOL):
            candidate_pool[str(m["governor_id"])] = m

    if candidate_pool:
        verified = refresh_members_individually(list(candidate_pool.values()))
        members = apply_verified(members, verified)
        members = sanitize_power(members)
        print(f"Verified {len(verified)} provisional-gainer candidates individually")

    payload = build_payload(KINGDOM_ID, len(alliance_infos), members, daily_previous, weekly_previous, weekly_just_reset)
    post_to_discord(payload)

    save_snapshot(members)  # weekly snapshot is only (re)written on reset, inside get_weekly_baseline

    print(f"Posted report — {len(alliance_infos)} alliances, {sum(m['power'] for m in members):,} total power, {len(members)} members")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
      
