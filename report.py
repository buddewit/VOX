"""
MightPulse -> Discord webhook report

Runs once, posts a daily power-progression summary for your Kingshot
alliance to a Discord channel via a webhook, then exits. Designed to be
triggered on a schedule by GitHub Actions (see .github/workflows/daily-report.yml)
rather than run as a long-lived bot.

The alliance-roster endpoint's power/activity fields lag behind reality
(batch-refreshed on MightPulse's side), while the per-player endpoint is
accurate (checked live on request). So this script uses the alliance
endpoint only to get the current member list, then re-fetches each member
individually for accurate power/last-active data.
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
REQUEST_DELAY_SECONDS = 1.1  # stays under the 60/min limit with margin


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


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text())
    return {}


def save_snapshot(members: list[dict]) -> None:
    data = {str(m["governor_id"]): m["power"] for m in members}
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2))


def build_payload(alliance: dict, members: list[dict], previous: dict) -> dict:
    ranked = sorted(members, key=lambda m: m["power"], reverse=True)
    total_power = sum(m["power"] for m in members)  # summed from fresh per-player data, not the (laggy) alliance total

    lines = []
    for m in ranked:
        gid = str(m["governor_id"])
        gain = m["power"] - previous[gid] if gid in previous else None
        gain_str = f"({gain:+,})" if gain is not None else "(new)"
        lines.append(f"{m['nick_name']:<20} {m['power']:>12,} {gain_str}")

    fields = []
    chunk = ""
    chunk_index = 1
    for line in lines:
        if len(chunk) + len(line) + 1 > 1000:
            fields.append({"name": f"Members ({chunk_index})", "value": f"```{chunk}```", "inline": False})
            chunk = ""
            chunk_index += 1
        chunk += line + "\n"
    if chunk:
        fields.append({"name": f"Members ({chunk_index})", "value": f"```{chunk}```", "inline": False})

    embed = {
        "title": f"[{alliance['abbr']}] {alliance['name']} — daily power report",
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
    data = fetch_roster()
    alliance = data["alliance"]
    members = fetch_fresh_members(data["members"])

    previous = load_snapshot()
    payload = build_payload(alliance, members, previous)
    post_to_discord(payload)
    save_snapshot(members)

    print(f"Posted report for [{alliance['abbr']}] — {sum(m['power'] for m in members):,} total power, {len(members)} members")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
