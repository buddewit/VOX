"""
MightPulse -> Discord Webhook Report (Full Kingdom Scan)
Pings the web profile of EVERY member in the top 100 alliances to force a fresh
backend scrape, then updates API records to track exact gains and losses.

RUN TIME: ~2 to 2.5 hours for ~7,000 members.
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
ALLIANCE_LIMIT = int(os.environ.get("ALLIANCE_LIMIT", "100"))
API_BASE = "https://api.mightpulse.com/v1"
SNAPSHOT_FILE = Path(__file__).parent / "last_snapshot.json"

REPORT_TOP_N = int(os.environ.get("REPORT_TOP_N", "50"))

# Paced rate limit configuration (1.1s = ~54 calls/min)
RATE_LIMIT_PER_MINUTE = int(os.environ.get("MIGHTPULSE_RATE_LIMIT_PER_MIN", "50"))
MAX_WORKERS = int(os.environ.get("MIGHTPULSE_MAX_WORKERS", "4"))
DAILY_CALL_LIMIT = int(os.environ.get("MIGHTPULSE_DAILY_LIMIT", "4800"))


class RateLimiter:
    """Thread-safe sliding-window limiter with an explicit 1.1s min interval."""

    def __init__(self, max_calls: int, period: float, min_interval: float = 1.1):
        self.max_calls = max_calls
        self.period = period
        self.min_interval = min_interval
        self.calls: deque[float] = deque()
        self.last_call = 0.0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.period:
                    self.calls.popleft()

                time_since_last = now - self.last_call
                if len(self.calls) < self.max_calls and time_since_last >= self.min_interval:
                    self.calls.append(now)
                    self.last_call = now
                    return

                sleep_for = max(
                    self.min_interval - time_since_last,
                    (self.period - (now - self.calls[0])) if len(self.calls) >= self.max_calls else 0.01
                )
            time.sleep(max(sleep_for, 0.05))


_rate_limiter = RateLimiter(RATE_LIMIT_PER_MINUTE, 60.0, min_interval=1.1)


def api_get(path: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}

    for attempt in range(3):
        _rate_limiter.acquire()
        resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
        
        if resp.status_code == 429:
            wait_time = (attempt + 1) * 15
            print(f"WARNING: Rate limited (429) on {path}. Waiting {wait_time}s...", file=sys.stderr)
            time.sleep(wait_time)
            continue
            
        if resp.status_code != 200:
            raise RuntimeError(f"MightPulse API {resp.status_code} on {path}: {resp.text}")
        
        return resp.json()

    raise RuntimeError(f"MightPulse API 429 persisted after retries on {path}")


def fetch_top_alliances(kid: str, limit: int = ALLIANCE_LIMIT) -> list[dict]:
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

    raise RuntimeError(f"Couldn't find an alliance list in response for kingdom {kid}.")


def fetch_all_rosters(kid: str, alliance_tags: list[str]) -> tuple[list[dict], list[dict]]:
    alliance_infos: list[dict | None] = [None] * len(alliance_tags)
    members_by_index: list[list[dict]] = [[] for _ in alliance_tags]

    def fetch_one(i: int, tag: str) -> tuple[int, dict | None, list[dict]]:
        try:
            data = api_get(f"/alliances/{kid}/{tag}", {"include": "info,roster"})
            return i, data.get("alliance"), data.get("members", [])
        except Exception as exc:
            print(f"WARNING: Couldn't fetch roster for alliance {tag}: {exc}", file=sys.stderr)
            return i, None, []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_one, i, tag) for i, tag in enumerate(alliance_tags)]
        for future in as_completed(futures):
            i, alliance, members = future.result()
            alliance_infos[i] = alliance
            members_by_index[i] = members

    all_members = [m for members in members_by_index for m in members]
    real_members = [m for m in all_members if m.get("governor_id") is not None]
    
    ok_alliance_infos = [a for a in alliance_infos if a is not None]
    return real_members, ok_alliance_infos


def refresh_entire_kingdom(members: list[dict]) -> list[dict]:
    """Pings the web profile and queries the API for EVERY member in the kingdom."""
    total = len(members)
    print(f"Starting full refresh for {total} members. Estimated time: ~{int(total * 1.1 / 60)} minutes.")

    web_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    fresh: list[dict | None] = [None] * total
    api_calls_made = 0

    def refresh_one(i: int, m: dict) -> tuple[int, dict]:
        nonlocal api_calls_made
        gid = m["governor_id"]
        
        # Step 1: Force backend scrape via Web Ping
        try:
            requests.get(f"https://mightpulse.com/player/{gid}", headers=web_headers, timeout=5)
        except Exception:
            pass  # Non-critical if web ping drops

        # Step 2: Fetch fresh data via API
        try:
            data = api_get(f"/players/{gid}", {"include": "base"})
            player = data.get("player", {})
            if player.get("power") is None:
                return i, m
            return i, player
        except Exception as exc:
            print(f"WARNING: Couldn't refresh {m.get('nick_name', gid)} ({gid}): {exc}", file=sys.stderr)
            return i, m

    # Process members while tracking call bounds
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for i, m in enumerate(members):
            if i >= DAILY_CALL_LIMIT:
                print(f"REACHED SAFETY CAP ({DAILY_CALL_LIMIT} calls). Stopping further refreshes.", file=sys.stderr)
                break
            futures.append(executor.submit(refresh_one, i, m))

        completed_count = 0
        for future in as_completed(futures):
            i, player = future.result()
            fresh[i] = player
            completed_count += 1
            if completed_count % 250 == 0:
                print(f"Progress: {completed_count}/{len(futures)} members refreshed...")

    # Fill unrefreshed tail (if capped) with baseline roster data
    return [fresh[i] if fresh[i] is not None else members[i] for i in range(total)]


def sanitize_power(members: list[dict]) -> list[dict]:
    for m in members:
        if m.get("power") is None:
            m["power"] = 0
    return members


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text())
    return {}


def save_snapshot(members: list[dict]) -> None:
    data = {str(m["governor_id"]): m["power"] for m in members}
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2))


def top_gainers(members: list[dict], previous: dict, top_n: int = REPORT_TOP_N) -> list[dict]:
    gainers = []
    for m in members:
        gid = str(m["governor_id"])
        if gid not in previous:
            continue
        prev_power = previous[gid]
        gain = m["power"] - prev_power
        pct = (gain / prev_power * 100) if prev_power else 0.0
        gainers.append({**m, "gain": gain, "pct": pct})
    
    # Sorts descending: highest positive gains first, biggest drops at bottom
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
    fields = []
    chunk = ""
    idx = 1
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > max_len - 6:
            fields.append({
                "name": f"{label} ({idx})" if idx > 1 else label,
                "value": f"```\n{chunk}```",
                "inline": False
            })
            chunk = ""
            idx += 1
        chunk += line + "\n"
    if chunk:
        fields.append({
            "name": f"{label} ({idx})" if idx > 1 else label,
            "value": f"```\n{chunk}```",
            "inline": False
        })
    return fields


def build_payload(
    kingdom_id: str,
    alliance_count: int,
    members: list[dict],
    daily_previous: dict,
) -> dict:
    total_power = sum(m["power"] for m in members)
    fields = []

    daily_top = top_gainers(members, daily_previous)
    if daily_top:
        fields.extend(chunk_field(f"Top {len(daily_top)} Daily Power Changes", format_gainer_lines(daily_top)))
    else:
        fields.append({"name": "Top Daily Power Changes", "value": "No previous snapshot yet — starting today.", "inline": False})

    embed = {
        "title": f"Kingdom {kingdom_id} — top {alliance_count} alliances power report",
        "description": f"Alliances tracked: {alliance_count} · Members: {len(members)} · Total power: {total_power:,}",
        "color": 0x5865F2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }
    return {"embeds": [embed]}


def post_to_discord(payload: dict) -> None:
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Discord webhook {resp.status_code}: {resp.text}")


def main() -> None:
    top_alliances = fetch_top_alliances(KINGDOM_ID, ALLIANCE_LIMIT)
    alliance_tags = [a["abbr"] for a in top_alliances]
    print(f"Resolved {len(alliance_tags)} top alliances by power in kingdom {KINGDOM_ID}")

    members, alliance_infos = fetch_all_rosters(KINGDOM_ID, alliance_tags)
    members = sanitize_power(members)

    daily_previous = load_snapshot()

    # Full sweep across all members
    members = refresh_entire_kingdom(members)
    members = sanitize_power(members)

    payload = build_payload(KINGDOM_ID, len(alliance_infos), members, daily_previous)
    post_to_discord(payload)

    save_snapshot(members)
    print(f"Posted report — {len(alliance_infos)} alliances, {sum(m['power'] for m in members):,} total power, {len(members)} members")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
