"""
Visits each alliance member's page on mightpulse.com and triggers a refresh,
since MightPulse only re-checks a player's live game data when their profile
is actually loaded on the site — the API alone reads whatever is cached.

Run this BEFORE report.py so the API call that follows sees fresh data.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

import requests

MIGHTPULSE_API_KEY = os.environ["MIGHTPULSE_API_KEY"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
ALLIANCE_TAG = os.environ.get("ALLIANCE_TAG", "VOX")
API_BASE = "https://api.mightpulse.com/v1"

PAGE_LOAD_TIMEOUT_MS = 20_000
PER_MEMBER_PAUSE_MS = 2_500  # give the backend time to process the refresh


def get_member_ids() -> list[dict]:
    """Just to get the current roster's governor IDs/names — no fresh power data needed here."""
    url = f"{API_BASE}/alliances/{KINGDOM_ID}/{ALLIANCE_TAG}"
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    resp = requests.get(url, headers=headers, params={"include": "roster"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["members"]


def refresh_member(page, governor_id: str, nick_name: str) -> bool:
    """Search for a member and click their refresh control. Returns success."""
    try:
        search_box = page.get_by_placeholder("Search by name, governor ID, or keyword")
        search_box.fill(str(governor_id))
        page.wait_for_timeout(1200)

        # Click the first matching search result
        result = page.locator("text=" + str(governor_id)).first
        result.click(timeout=5000)
        page.wait_for_timeout(800)

        # Click the refresh control on the player panel
        refresh_button = page.get_by_text("Refresh", exact=False).first
        refresh_button.click(timeout=5000)

        page.wait_for_timeout(PER_MEMBER_PAUSE_MS)
        return True
    except Exception as exc:
        print(f"WARNING: could not refresh {nick_name} ({governor_id}): {exc}", file=sys.stderr)
        return False


def main() -> None:
    members = get_member_ids()
    print(f"Refreshing {len(members)} members on mightpulse.com...")

    succeeded, failed = 0, 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"https://mightpulse.com/kingdom/{KINGDOM_ID}", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

        for m in members:
            ok = refresh_member(page, m["governor_id"], m.get("nick_name", "?"))
            succeeded += ok
            failed += not ok

        browser.close()

    print(f"Refresh pass done: {succeeded} succeeded, {failed} failed.")
    # Don't hard-fail the whole run over a few missed refreshes — report.py
    # will just show stale data for those, which is the status quo today.


if __name__ == "__main__":
    main()
