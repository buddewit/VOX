"""
MightPulse -> Discord webhook report

Runs once, posts a daily power-progression summary for your Kingshot
alliance to a Discord channel via a webhook, then exits. Designed to be
triggered on a schedule by GitHub Actions (see .github/workflows/daily-report.yml)
rather than run as a long-lived bot.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

MIGHTPULSE_API_KEY = os.environ["kss_ykbr0QiJT5OXGNh1aQkGE7R2BpqxXWMXtPmEQ0TYgWc"]
DISCORD_WEBHOOK_URL = os.environ["https://discord.com/api/webhooks/1543629501001498795/nytly6oh70miibm0a8zBmzcauDxefiCTKVDZqclJ3OK635maMk04AslsOriPuu7Zoa_2"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
ALLIANCE_TAG = os.environ.get("ALLIANCE_TAG", "VOX")

API_BASE = "https://api.mightpulse.com/v1"
SNAPSHOT_FILE = Path(__file__).parent / "last_snapshot.json"


def fetch_roster() -> dict:
    url = f"{API_BASE}/alliances/{KINGDOM_ID}/{ALLIANCE_TAG}"
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    params = {"include": "info,roster"}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"MightPulse API {resp.status_code}: {resp.text}")
    return resp.json()


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text())
    return {}


def save_snapshot(members: list[dict]) -> None:
    data = {str(m["governor_id"]): m["power"] for m in members}
    SNAPSHOT_FILE.write_text(json.dumps(data, indent=2))


def build_payload(alliance: dict, members: list[dict], previous: dict) -> dict:
    ranked = sorted(members, key=lambda m: m["power"], reverse=True)

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
        "description": f"Kingdom {alliance['kid']} · Total power: {alliance['power']:,} · Members: {alliance['count']}",
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
    members = data["members"]

    previous = load_snapshot()
    payload = build_payload(alliance, members, previous)
    post_to_discord(payload)
    save_snapshot(members)

    print(f"Posted report for [{alliance['abbr']}] — {alliance['power']:,} total power, {alliance['count']} members")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
