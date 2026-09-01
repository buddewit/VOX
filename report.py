"""
MightPulse -> Discord webhook report

Runs once, posts a power-progression summary for your Kingshot alliance to a
Discord channel via a webhook, then exits. Designed to be triggered on a
schedule by GitHub Actions (see .github/workflows/daily-report.yml) rather
than run as a long-lived bot.

The alliance-roster endpoint's power/activity fields lag behind reality
(batch-refreshed on MightPulse's side), while the per-player endpoint is
accurate (checked live on request). So this script uses the alliance
endpoint only to get the current member list, then re-fetches each member
individually for accurate power/last-active data.

Report contents:
  - Top 20 members by power gained since the previous run (daily).
  - Top 20 members by power gained since the weekly baseline, which resets
    automatically every 7 days.
Each line shows: name, current power, (power gained / % gained).
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

MIGHTPULSE_API_KEY = os.environ["MIGHTPULSE_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
ALLIANCE_TAG = os.environ.get("ALLIANCE_TAG", "VOX")

API_BASE = "https://api.mightpulse.com/v1"
SNAPSHOT_FILE = Path(__file__).parent / "last_snapshot.json"
WEEKLY_SNAPSHOT_FILE = Path(__file__).parent / "weekly_snapshot.json"
REQUEST_DELAY_SECONDS = 1.1  # stays under the 60/min limit with margin
TOP_N = 20
WEEKLY_RESET_DAYS = 7


def api_get(path: str, params: dict | None = None) -> dict:
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


def fetch_roster() -> dict:
    """Alliance totals + member list. Member power/activity here can lag;
    only the member IDs/names are trusted from this call."""
    return api_get(f"/alliances/{KINGDOM_ID}/{ALLIANCE_TAG}", {"include": "info,roster"})


def fetch_fresh_members(members: list[dict]) -> list[dict]:
    """Re-fetch each member individually for accurate, live power/activity."""
    fresh = []
    for i, m in enumerate(members):
        gid = m["governor_id"]
        try:
            data = api_get(f"/players/{gid}", {"include": "base"})
            fresh.append(data["player"])
        except Exception as exc:
            print(f"WARNING: couldn't refresh {m.get('nick_name', gid)} ({gid}): {exc}", file=sys.stderr)
            fresh.append(m)  # fall back to the (stale) roster entry rather than dropping them
        if i < len(members) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)
    return fresh


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
    alliance: dict,
    members: list[dict],
    daily_previous: dict,
    weekly_previous: dict,
    weekly_just_reset: bool,
) -> dict:
    total_power = sum(m["power"] for m in members)  # summed from fresh per-player data, not the (laggy) alliance total

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
        "title": f"[{alliance['abbr']}] {alliance['name']} — power report",
        "description": f"Kingdom {alliance['kid']} · Total power: {total_power:,} · Members: {len(members)}",
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

    data = fetch_roster()
    alliance = data["alliance"]
    members = fetch_fresh_members(data["members"])

    daily_previous = load_snapshot()
    weekly_previous, weekly_just_reset = get_weekly_baseline(members, now)

    payload = build_payload(alliance, members, daily_previous, weekly_previous, weekly_just_reset)
    post_to_discord(payload)

    save_snapshot(members)  # weekly snapshot is only (re)written on reset, inside get_weekly_baseline

    print(f"Posted report for [{alliance['abbr']}] — {sum(m['power'] for m in members):,} total power, {len(members)} members")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
