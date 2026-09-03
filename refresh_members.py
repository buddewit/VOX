"""
Visits each member's page on mightpulse.com (across the top ALLIANCE_LIMIT
alliances in the kingdom, not just one) and triggers a refresh, since
MightPulse only re-checks a player's live game data when their profile is
actually loaded on the site — the API alone reads whatever is cached.

Run this BEFORE report.py so the API call that follows sees fresh data.

Scope: resolves the top ALLIANCE_LIMIT alliances by power fresh each run via
/kingdoms/{kid}/ranks?board=alliance_power, then pulls every one of those
alliances' rosters via /alliances/{kid}/{tag}?include=roster and visits every
member across all of them. At kingdom scale (thousands of members) this is a
genuine full sweep and will take a couple of hours — that's expected and
accepted, not a bug to optimize away.

NOTE: the alliance roster's `governor_id` is NOT the same value as the `uid`
used in profile URLs (https://mightpulse.com/player/<uid>). Per MightPulse's
API docs, GET /v1/players/{id}?id_type=uid resolves a player by uid — this
script assumes id_type=governor_id is also supported and uses it to resolve
each roster member's uid via the API (fast, reliable, one call per member).
If that param value turns out to be wrong/unsupported, it falls back to the
roster payload possibly already containing a uid-like field, and finally to
the old search-and-click flow using governor_id / nick_name.

Member visits run concurrently (multiple browser tabs at once, each in its
own isolated context) instead of one at a time, and the uid-resolution API
calls are paced/retried the same way report.py's api_get is. CONCURRENCY and
the API rate limit are tunable via env vars below.
"""

import asyncio
import os
import sys
import time
from collections import deque

import requests
from playwright.async_api import Browser, Page, async_playwright

MIGHTPULSE_API_KEY = os.environ["MIGHTPULSE_API_KEY"]
KINGDOM_ID = os.environ.get("KINGDOM_ID", "2423")
# Top N alliances (by power) to sweep. Replaces the old single ALLIANCE_TAG.
ALLIANCE_LIMIT = int(os.environ.get("ALLIANCE_LIMIT", "50"))
API_BASE = "https://api.mightpulse.com/v1"

PAGE_LOAD_TIMEOUT_MS = 20_000
PER_MEMBER_PAUSE_MS = 2_500  # give the backend time to process the refresh

# How many profile pages to have open/loading at once, and how many
# uid-resolution API calls per minute to allow across all of them.
CONCURRENCY = int(os.environ.get("MIGHTPULSE_REFRESH_CONCURRENCY", "5"))
RATE_LIMIT_PER_MINUTE = int(os.environ.get("MIGHTPULSE_REFRESH_RATE_LIMIT_PER_MIN", "55"))

# Keys we'll check, in order, to find the profile uid already sitting in a
# roster member dict (used only if the /players lookup below fails).
UID_CANDIDATE_KEYS = ["uid", "player_id", "profile_id", "profile_uid"]

# id_type values to try, in order, when resolving a roster governor_id to a
# profile uid via GET /v1/players/{id}?id_type=... . Confirmed from docs:
# "uid" is a valid id_type for looking a player up BY their uid, which doesn't
# help us here — what we actually need is the reverse (governor_id -> uid).
# "governor_id" is a guess; adjust once you confirm against the real docs.
PLAYER_LOOKUP_ID_TYPES = ["governor_id"]


class AsyncRateLimiter:
    """Sliding-window limiter for coroutines: at most `max_calls` acquisitions
    in any rolling `period` seconds, no matter how many tasks share it."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                sleep_for = self.period - (now - self.calls[0])
            await asyncio.sleep(max(sleep_for, 0.01))


async def resolve_uid_via_api(governor_id: str, rate_limiter: AsyncRateLimiter) -> str | None:
    """Try to resolve a roster governor_id to a profile uid via the players
    lookup endpoint. Returns None if no configured id_type works, so the
    caller can fall back to other strategies.

    The actual HTTP call is blocking (requests), so it runs in a thread via
    asyncio.to_thread — that keeps the event loop free for the browser pages
    while still being paced by the shared rate limiter."""
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}

    def _get(id_type: str):
        url = f"{API_BASE}/players/{governor_id}"
        resp = requests.get(url, headers=headers, params={"id_type": id_type}, timeout=15)
        if resp.status_code == 429:
            time.sleep(5)
            resp = requests.get(url, headers=headers, params={"id_type": id_type}, timeout=15)
        return resp

    for id_type in PLAYER_LOOKUP_ID_TYPES:
        await rate_limiter.acquire()
        try:
            resp = await asyncio.to_thread(_get, id_type)
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


def fetch_top_alliances(kid: str, limit: int) -> list[dict]:
    """Top alliances in this kingdom by power, already ranked by the API.
    Each entry has at least aid, abbr, name, score per the docs.

    The docs don't spell out the top-level key holding the list for this
    endpoint, so this looks structurally for a list of dicts shaped like a
    documented alliance-board entry (has an "abbr" field) — top-level values
    first, then one level of nesting. Fails loudly with the real shape if
    nothing matches."""
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    resp = requests.get(
        f"{API_BASE}/kingdoms/{kid}/ranks",
        headers=headers,
        params={"board": "alliance_power", "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

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
        f"response — got top-level keys {sorted(data.keys())}. Inspect the real payload "
        f"and fix fetch_top_alliances()."
    )


def get_member_ids(alliance_tags: list[str]) -> list[dict]:
    """Get the current roster's governor IDs/names (and uid, if present)
    across every given alliance, flattened into one list."""
    headers = {"Authorization": f"Bearer {MIGHTPULSE_API_KEY}"}
    all_members: list[dict] = []

    for tag in alliance_tags:
        url = f"{API_BASE}/alliances/{KINGDOM_ID}/{tag}"
        try:
            resp = requests.get(url, headers=headers, params={"include": "roster"}, timeout=30)
            resp.raise_for_status()
            members = resp.json()["members"]
        except Exception as exc:
            print(f"WARNING: couldn't fetch roster for alliance {tag}: {exc}", file=sys.stderr)
            continue
        all_members.extend(members)

    if all_members:
        print(f"DEBUG: sample member keys: {sorted(all_members[0].keys())}", file=sys.stderr)

    return all_members


def extract_uid(member: dict) -> str | None:
    for key in UID_CANDIDATE_KEYS:
        if member.get(key):
            return str(member[key])
    return None


async def refresh_by_uid(page: Page, uid: str) -> str:
    """Navigate to the profile page — the page load itself triggers the
    live-data refresh, no button click needed.

    Returns "visited" or "error".
    """
    try:
        await page.goto(f"https://mightpulse.com/player/{uid}", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(PER_MEMBER_PAUSE_MS)
        return "visited"
    except Exception as exc:
        print(f"WARNING: could not visit uid={uid}: {exc}", file=sys.stderr)
        return "error"


async def refresh_by_search(page: Page, kingdom_id: str, governor_id: str, nick_name: str) -> str:
    """Fallback: search for a member on the kingdom page and open their
    profile — the page load itself triggers the refresh, no click needed.

    Returns "visited", "not_found", or "error".
    """
    try:
        await page.goto(f"https://mightpulse.com/kingdom/{kingdom_id}", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

        search_box = page.get_by_placeholder("Search by name, governor ID, or keyword")
        await search_box.fill(str(governor_id))
        await page.wait_for_timeout(1200)

        result = page.locator("text=" + str(governor_id)).first
        if await result.count() == 0:
            print(f"WARNING: no search result for {nick_name} ({governor_id})", file=sys.stderr)
            return "not_found"

        await result.click(timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(PER_MEMBER_PAUSE_MS)
        return "visited"
    except Exception as exc:
        print(f"WARNING: could not visit {nick_name} ({governor_id}) via search: {exc}", file=sys.stderr)
        return "error"


async def process_member(
    m: dict,
    browser: Browser,
    semaphore: asyncio.Semaphore,
    rate_limiter: AsyncRateLimiter,
) -> str:
    governor_id = m["governor_id"]
    nick_name = m.get("nick_name", "?")

    # Resolve the uid BEFORE taking a browser slot, so API latency doesn't
    # hog a concurrency slot that could be loading another member's page.
    uid = await resolve_uid_via_api(governor_id, rate_limiter) or extract_uid(m)

    async with semaphore:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            if uid:
                result = await refresh_by_uid(page, uid)
            else:
                result = await refresh_by_search(page, KINGDOM_ID, governor_id, nick_name)
        finally:
            await context.close()

    return result


async def main_async() -> None:
    top_alliances = fetch_top_alliances(KINGDOM_ID, ALLIANCE_LIMIT)
    alliance_tags = [a["abbr"] for a in top_alliances]
    print(f"Resolved {len(alliance_tags)} top alliances by power in kingdom {KINGDOM_ID}")

    members = get_member_ids(alliance_tags)
    print(
        f"Refreshing {len(members)} members across {len(alliance_tags)} alliances "
        f"on mightpulse.com (concurrency={CONCURRENCY})..."
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    rate_limiter = AsyncRateLimiter(RATE_LIMIT_PER_MINUTE, 60.0)
    tally = {"visited": 0, "not_found": 0, "error": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            tasks = [process_member(m, browser, semaphore, rate_limiter) for m in members]
            for result in await asyncio.gather(*tasks):
                tally[result] = tally.get(result, 0) + 1
        finally:
            await browser.close()

    print(
        f"Refresh pass done: {tally['visited']} visited, "
        f"{tally['not_found']} not found, "
        f"{tally['error']} errored."
    )
    # Don't hard-fail the whole run over a few missed visits — report.py
    # will just show stale data for those, which is the status quo today.


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
