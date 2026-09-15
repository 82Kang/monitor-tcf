# monitor-tcf

Watches Alliance Française de Toronto's booking site for open seats in the
**E-TCF CANADA – 4 modules** test and pushes a Telegram message to a group chat
when one appears.

## How it works

The booking page (`anc.ca.apm.activecommunities.com/aftoronto`) is a React SPA on
ACTIVE Communities. The dated sittings live inside the collapsed *"View
sub-courses"* dropdown, which is a second API call — so `check.py` talks to the
JSON endpoints directly rather than scraping HTML:

1. `GET /activity/search?...` — only to harvest the `csrfToken` and `JSESSIONID`
   cookie. No login needed.
2. `POST /rest/activities/list` — the parent rows for the keyword.
3. `POST /rest/activities/subs/{parentId}` — the dated sessions in the dropdown.

A session counts as **available** when `urgent_message.status_description` is not
one of `Full / Ended / Closed / In progress / Cancelled` **and** at least one seat
is free (`total_open − already_enrolled > 0`; `total_open: -1` means uncapped).
`Tentative` and `Starting soon` describe timing, not capacity, so they stay
bookable.

Standard library only — the Actions job needs no install step.

## Configuration — `config.json`

| Key | Meaning |
|---|---|
| `min_test_date` | **The setting you'll change most.** Only sessions whose test date is on or after this ISO date can alert. |
| `name_match` | Substring a course name must contain. Keeps the unrelated "TCF Preparation" licence out. |
| `keyword` | Search term sent to the site. |
| `reminder_hours` | While a session stays open, re-send at most this often. |
| `heartbeat_hour_utc` | Hour for the daily "still watching" message. |
| `failure_alert_after` | Consecutive failures before shouting that the watcher is broken. |
| `alert_undated` | Some listings return "Select course to view additional dates" with no machine-readable date. `true` alerts on them anyway (flagged) rather than dropping them silently. |

Edit and push; the next scheduled run picks it up.

## Setup

**1. Telegram bot + group**

```
# Message @BotFather -> /newbot -> copy the token
# Create a group, add the bot to it, post any message in the group, then:
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
# take result[].message.chat.id  (a group id is negative, e.g. -1001234567890)
```

Anyone invited to the group gets the alerts — no code change needed.

**2. Repo + secrets**

```
gh repo create monitor-tcf --public --source=. --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

The repo must be **public**: a `*/5` schedule uses ~3 h of runner time a day,
well past the 2,000 min/month private free tier. Nothing secret is committed.

## Local use

```
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...

python3 check.py --test-telegram              # prove the bot reaches the group
python3 check.py --dry-run                    # print findings, send/write nothing
python3 check.py --dry-run --min-test-date 2027-01-01
python3 check.py --dump-activity 129936       # raw sub-activity JSON
python3 -m pytest tests/ -q
```

## Alerting rules

Alerts fire on a **new session**, on a **full → open** transition, and as a spaced
reminder every `reminder_hours` while a session stays open — never once per poll.
`state.json` is committed back by the workflow only when something changes, which
keeps history small and doubles as the repository activity that stops GitHub
auto-disabling scheduled workflows after 60 days.

If a check fails `failure_alert_after` times in a row you get a "watcher is
failing" message, then a recovery message — so silence always means "no seats",
never "the script died".

## Known limitation

GitHub's `schedule` trigger has a 5-minute floor and is best-effort: runs are
commonly delayed 5–30 minutes, worst at the top of the hour. With ~6 seats per
session this can lose a race against someone refreshing by hand; it is reliable
for learning that a **new cohort was posted**. For a true 3-minute cadence the
same `check.py` runs unchanged under macOS `launchd` or a small VM.
