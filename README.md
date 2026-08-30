# MightPulse daily report (GitHub Actions + webhook)

Posts a daily power-progression report for your Kingshot alliance to a
Discord channel, using the MightPulse API and a Discord webhook. Runs once a
day as a scheduled GitHub Actions job — no server to keep online.

## What it does

Once a day, GitHub Actions:
1. Checks out this repo and runs `report.py`
2. That script calls `GET /v1/alliances/{kingdom_id}/{tag}?include=info,roster`
   on MightPulse
3. Compares each member's power to the previous run's snapshot
   (`last_snapshot.json`, committed to the repo)
4. Posts an embed to your Discord channel via webhook: alliance totals + each
   member's power and gain since last run, sorted highest power first
5. Commits the updated snapshot back to the repo so tomorrow's run has
   something to diff against

## Setup

1. **Create a Discord webhook**
   - Channel Settings -> Integrations -> Webhooks -> New Webhook
   - Copy the Webhook URL

2. **Get a MightPulse API key**
   - https://api.mightpulse.com -> sign in with Discord -> Create API key
   - Copy it immediately (`kss_...`, shown once)

3. **Push this folder to a GitHub repo** (public or private both work; a
   private repo gets 2,000 free Actions minutes/month, plenty for this)

4. **Add secrets and variables** in the repo:
   Settings -> Secrets and variables -> Actions
   - **Secrets** tab: `MIGHTPULSE_API_KEY`, `DISCORD_WEBHOOK_URL`
   - **Variables** tab: `KINGDOM_ID` (e.g. `2423`), `ALLIANCE_TAG` (e.g. `VOX`)

5. **That's it.** The workflow in
   `.github/workflows/daily-report.yml` runs on a cron schedule
   (`0 6 * * *` = 06:00 UTC daily) and can also be triggered manually from
   the Actions tab -> "Daily MightPulse report" -> "Run workflow", which is
   the fastest way to confirm it works before waiting for the schedule.

## Adjusting the schedule

Edit the `cron` line in `.github/workflows/daily-report.yml`. Cron is always
UTC on GitHub Actions regardless of your local timezone. E.g. `0 20 * * *`
for 20:00 UTC.

## Local testing (optional)

```
cp .env.example .env   # fill in the values
pip install -r requirements.txt
export $(cat .env | xargs) && python report.py
```

## Notes

- **Rate limits**: MightPulse allows 60 req/min and 5,000/day per key; one
  run uses a single request.
- **Data freshness**: MightPulse data can be up to 60 minutes old.
- **First run**: there's no previous snapshot yet, so every member shows
  "(new)" instead of a gain — normal, day two onward will show real deltas.
- **Multiple alliances**: duplicate the workflow file with different
  `KINGDOM_ID`/`ALLIANCE_TAG` variables and a separate webhook if you want
  more than one report.
