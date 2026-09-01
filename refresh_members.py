"""
Visits each alliance member's page on mightpulse.com and triggers a refresh,
since MightPulse only re-checks a player's live game data when their profile
is actually loaded on the site — the API alone reads whatever is cached.

Run this BEFORE report.py so the API call that follows sees fresh data.

NOTE: the alliance roster's `governor_id` is NOT the same value as the `uid`
used in profile URLs (https://mightpulse.com/player/<uid>). Per MightPulse's
API docs, GET /v1/players/{id}?id_type=uid resolves a player by uid — this
script assumes id_type=governor_id is also supported and uses it to resolve
each roster member's uid via the API (fast, reliable, one call per member).
If that param value turns out to be wrong/unsupported, it falls back to the
roster payload possibly already containing a uid-like field, and finally to
the old search-and-click flow using governor_id / nick_name.
"""

import os
import sys

from playwright.sync_api import sync_playwright

import requests

MIGHTPULSE_API_KEY = os.environ["MIGHTPULSE_API_KEY"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
ALLIANCE_TAG = os.environ.get("ALLIANCE_TAG", "VOX")
API_BASE = "https://api.mightpulse.com/v1"

PAGE_LOAD_TIMEOUT_MS = 20_000
PER_MEMBER_PAUSE_MS = 2_500  # give the backend time to process the refresh

# Keys we'll check, in order, to find the profile uid already sitting in a
# roster member dict (used only if the /players lookup below fails).
UID_CANDIDATE_KEYS = ["uid", "player_id", "profile_id", "profile_uid"]

# id_type values to try, in order, when resolving a roster governor_id to a
# profile uid via GET /v1/players/{id}?id_type=... . Confirmed from docs:
# "uid" is a valid id_type for looking a player up BY their uid, which doesn't
# help us here — what we actually need is the reverse (governor_id -> uid).
# "governor_id" is a guess; adjust once you confirm against the real docs.
PLAYER_LOOKUP_ID_TYPES = ["governor_id"]


def resolve_uid_via_api(governor_id: str) -> str | None:
    """Try to resolve a roster governor_id to a profile uid via the players
    lookup endpoint. Returns None if no configured id_type works, so the
    caller can fall back to other strategies."""
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    for id_type in PLAYER_LOOKUP_ID_TYPES:
        url = f"{API_BASE}/players/{governor_id}"
        try:
            resp = requests.get(url, headers=headers, params={"id_type": id_type}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                uid = data.get("uid") or data.get("id")
                if uid:
                    return str(uid)
            else:
                print(
                    f"DEBUG: players lookup id_type={id_type} for {governor_id} "
                    f"returned {resp.status_code}",
                    file=sys.stderr,
                )
        except requests.RequestException as exc:
            print(f"DEBUG: players lookup id_type={id_type} for {governor_id} failed: {exc}", file=sys.stderr)
    return None


def get_member_ids() -> list[dict]:
    """Get the current roster's governor IDs/names (and uid, if present)."""
    url = f"{API_BASE}/alliances/{KINGDOM_ID}/{ALLIANCE_TAG}"
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    resp = requests.get(url, headers=headers, params={"include": "roster"}, timeout=30)
    resp.raise_for_status()
    members = resp.json()["members"]

    if members:
        print(f"DEBUG: sample member keys: {sorted(members[0].keys())}", file=sys.stderr)

    return members


def extract_uid(member: dict) -> str | None:
    for key in UID_CANDIDATE_KEYS:
        if member.get(key):
            return str(member[key])
    return None


def refresh_by_uid(page, uid: str) -> bool:
    """Navigate straight to the profile page and click refresh."""
    try:
        page.goto(f"https://mightpulse.com/player/{uid}", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

        refresh_button = page.get_by_text("Refresh", exact=False).first
        refresh_button.click(timeout=5000)

        page.wait_for_timeout(PER_MEMBER_PAUSE_MS)
        return True
    except Exception as exc:
        print(f"WARNING: could not refresh uid={uid}: {exc}", file=sys.stderr)
        return False


def refresh_by_search(page, kingdom_id: str, governor_id: str, nick_name: str) -> bool:
    """Fallback: search for a member on the kingdom page and click their refresh control."""
    try:
        page.goto(f"https://mightpulse.com/kingdom/{kingdom_id}", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

        search_box = page.get_by_placeholder("Search by name, governor ID, or keyword")
        search_box.fill(str(governor_id))
        page.wait_for_timeout(1200)

        result = page.locator("text=" + str(governor_id)).first
        result.click(timeout=5000)
        page.wait_for_timeout(800)

        refresh_button = page.get_by_text("Refresh", exact=False).first
        refresh_button.click(timeout=5000)

        page.wait_for_timeout(PER_MEMBER_PAUSE_MS)
        return True
    except Exception as exc:
        print(f"WARNING: could not refresh {nick_name} ({governor_id}) via search: {exc}", file=sys.stderr)
        return False


def main() -> None:
    members = get_member_ids()
    print(f"Refreshing {len(members)} members on mightpulse.com...")

    succeeded, failed = 0, 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for m in members:
            governor_id = m["governor_id"]
            nick_name = m.get("nick_name", "?")

            uid = resolve_uid_via_api(governor_id) or extract_uid(m)
            if uid:
                ok = refresh_by_uid(page, uid)
            else:
                ok = refresh_by_search(page, KINGDOM_ID, governor_id, nick_name)

            succeeded += ok
            failed += not ok

        browser.close()

    print(f"Refresh pass done: {succeeded} succeeded, {failed} failed.")
    # Don't hard-fail the whole run over a few missed refreshes — report.py
    # will just show stale data for those, which is the status quo today.


if __name__ == "__main__":
    main()
