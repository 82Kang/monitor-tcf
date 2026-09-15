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

### Containers vs. real sittings

The listing shows the course name **twice**: once as a collapsed container, and
once as the dated sitting inside it. Only the inner row is bookable — it is the
one carrying the price, the date and the Enroll link. Every one of the 283
container rows on this site has an empty date.

A row is treated as a container when `parent_activity` is true **or**
`num_of_sub_activities > 0`. Both signals are needed because they disagree on
real data: the sibling `E-TEF CANADA` products report `parent_activity: true`
alongside `num_of_sub_activities: 0`. Rows flagged `parent_activity` are never
reported as sessions, wherever they appear.

### Is a seat open?

A session is **available** when at least one seat is free
(`total_open − already_enrolled > 0`; `total_open: -1` means uncapped) **and**
its status tag is not a blocking one.

`urgent_message.status_description` doubles as an urgency banner, so the set of
values is open-ended. Observed live:

| Tag | Bookable? |
|---|---|
| `""` (no tag) | yes |
| `Full` | no |
| `Ended` | no |
| `Closed` | no |
| `In progress` | no |
| `Cancelled` | no |
| `Tentative` | yes — describes confirmation, not capacity |
| `Starting soon` | yes — describes timing, not capacity |
| `1 space(s) left` | yes — an urgency banner |

Only the blocking list is enumerated; **anything unrecognised counts as
bookable**. A whitelist of known-good tags would silently swallow a real opening
the first time the site invented a new banner.

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
| `alert_undated` | Some listings carry no machine-readable date, showing "Select course to view additional dates and times" instead. There is nothing for `min_test_date` to compare against, so `true` alerts on them anyway with a warning, rather than dropping them in silence. Set `false` to ignore them — at the risk of hearing nothing if a dateless sitting is the one that opens. |

Note that `min_test_date` is applied to a session's **own** date whenever the site
supplies one; no status tag bypasses it. Dateless rows are the only exception,
and they are always flagged as such in the message.

Edit and push; the next scheduled run picks it up.

## Setup

**1. Telegram bot + group**

Message `@BotFather` → `/newbot` → copy the token. Create a group and add the
bot to it. Then:

```
python3 tg_setup.py
```

It prompts for the token with a hidden input (so it never reaches your shell
history), checks the token is valid, warns if a webhook is swallowing updates,
lists every chat the bot can see with its id, and offers to send a test message.

If it finds no chats, the usual cause is privacy mode: bots only receive messages
that start with `/`, so send `/start@yourbotname` in the group and re-run.

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
