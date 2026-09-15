# How This System Works

A complete walk through the TCF seat watcher: the website it scrapes, the Telegram
bot that alerts you, the GitHub Actions that run it, how secrets stay secret, and
the handful of genuinely hard problems hiding underneath.

Every example is from **this** system — real IDs, real values, real bugs we hit.

---

## Table of contents

| Part | What it covers |
|---|---|
| [0. The one-minute version](#0-the-one-minute-version) | The whole thing in a paragraph and a diagram |
| [1. Why this shape](#1-why-this-shape) | Push vs pull, and why you can't just "subscribe" |
| [2. The data source](#2-the-data-source) | SPAs, CSRF, cookies, reverse-engineering an API |
| [3. Telegram bots, completely](#3-telegram-bots-completely) | What a bot *is*, tokens, chat IDs, polling vs webhooks |
| [4. Security](#4-security) | Sealed-box crypto, threat modelling, what we got wrong |
| [5. GitHub Actions](#5-github-actions) | Runners, triggers, cron, permissions, billing |
| [6. State: the hard part](#6-state-the-hard-part) | Why this is the bit that actually breaks |
| [7. Deployment](#7-deployment) | What "deploy" means with no build step |
| [8. Observability](#8-observability) | Why silence is the enemy |
| [9. One poll, traced end to end](#9-one-poll-traced-end-to-end) | Every byte, in order |
| [10. Glossary](#10-glossary) | Every piece of jargon, defined |
| [11. Exercises](#11-exercises) | Things to try, to make it stick |

---

## 0. The one-minute version

Every 5 minutes, a temporary Linux machine somewhere in Microsoft's cloud wakes up,
downloads your repository, runs a ~600-line Python script, and disappears. The
script asks Alliance Française's booking system whether any E-TCF CANADA test
sitting on or after 15 November 2026 has a free seat. If one does — **and it hasn't
already told you about that exact seat** — it sends a message to your Telegram
group. Then it writes down what it saw, commits that note back into the repository,
and the machine is destroyed.

```
                    ┌─────────────────────────────────────────────┐
                    │  GitHub Actions                             │
                    │  (a scheduler + a disposable Linux VM)      │
                    │                                             │
   every ~5 min ───►│   1. clone repo      ┌──────────────┐       │
                    │   2. run check.py ──►│   check.py   │       │
                    │   3. commit state    └──────┬───────┘       │
                    │   4. destroy VM             │               │
                    └─────────────────────────────┼───────────────┘
                                                  │
                        ┌─────────────────────────┴──────────────────┐
                        │                                            │
                        ▼ (read: is a seat free?)                    ▼ (write: tell me)
        ┌───────────────────────────────┐              ┌──────────────────────────┐
        │  ActiveNet / ActiveCommunities│              │   Telegram Bot API       │
        │  anc.ca.apm.activecommunities │              │   api.telegram.org       │
        │  .com/aftoronto               │              │                          │
        │                               │              │   ──► your group         │
        │  (Alliance Française Toronto) │              │       "Tcf_chase"        │
        └───────────────────────────────┘              └──────────────────────────┘
```

Three independent parties. Two of them don't know this system exists. That framing
matters, and it's the subject of the next section.

---

## 1. Why this shape

### The fundamental constraint: nobody will tell you

The ideal design would be: Alliance Française notifies you when a seat frees up.
That's a **push** model — the source of truth initiates contact.

They don't offer that. No webhook, no RSS, no email alert, no public API contract.
So you're forced into a **pull** model: you repeatedly ask "anything yet?" and
compare the answer to last time.

| Model | Who initiates | Latency | Cost | Needs cooperation? |
|---|---|---|---|---|
| **Push** | The data source | Instant | Near zero | **Yes** — they must build it |
| **Pull (polling)** | You | Up to one poll interval | One request per poll, forever | No |

This single constraint explains almost every design decision downstream:

- You need something that runs **on a schedule**, forever → GitHub Actions
- You need to **remember** what you saw last time, or you'll re-announce the same
  seat every poll → `state.json`
- You need somewhere to **send** the news that reaches your phone → Telegram
- Polling has a **latency floor** equal to your interval → the 5-minute cron, and
  the accepted 5–30 minute reality

### Why polling is harder than it looks

A naive poller is four lines:

```python
while True:
    if seat_is_free():
        send_telegram("seat free!")
    sleep(300)
```

This is wrong in at least five ways, and every one of them bit this project:

1. **It spams.** A seat stays free for hours → 12 messages an hour, forever.
2. **It has no memory across runs.** On Actions the process dies every run, so
   `while True` doesn't even exist — state must be *stored somewhere*.
3. **Silence is ambiguous.** If `seat_is_free()` throws every time, you get the
   same silence as "no seats". You'd never know.
4. **It conflates "is it open" with "should I tell you".** Those are different
   questions. The second depends on history.
5. **Delivery can fail.** If `send_telegram` fails after you've recorded "told
   him", the message is lost forever. (This exact bug happened — see
   [Part 6](#6-state-the-hard-part).)

Everything complicated in `check.py` exists to solve one of those five.

---

## 2. The data source

### Server-rendered pages vs single-page applications

Two eras of web app, and they need completely different scraping strategies.

**Server-rendered** (the old way): you request a URL, the server builds the
complete HTML — prices, dates, everything — and sends it. To scrape, you download
the HTML and parse it.

```
GET /courses  ──►  server queries DB, renders HTML  ──►  <div>September 21, 2026</div>
```

**Single-page application (SPA)** (the modern way): you request a URL and get a
nearly empty HTML shell plus a large JavaScript bundle. The JavaScript then runs
*in your browser*, makes its own requests to JSON endpoints, and constructs the
page dynamically.

```
GET /activity/search ──► shell HTML + app.index.js (11.8 MB)
                              │
                              └─► browser runs JS ──► POST /rest/activities/list
                                                           │
                                                      JSON response
                                                           │
                                                    JS builds the DOM
```

ActiveNet is firmly the second kind. Downloading the search page HTML gives you
**no course data at all** — it isn't there yet. Two ways forward:

| Approach | How | Cost |
|---|---|---|
| **Headless browser** (Selenium, Playwright) | Run a real browser, let the JS execute, read the resulting DOM | ~300 MB of dependencies, seconds per run, fragile to layout changes |
| **Call the JSON API directly** | Skip the browser; make the same requests the JS makes | A few HTTP calls, milliseconds, stable |

We took the second. It's faster, dependency-free (pure Python standard library),
and — crucially — **more stable**, because JSON field names change far less often
than CSS classes and DOM structure.

### How you find a hidden API

This is a genuinely useful skill. The process:

1. **Open browser DevTools → Network tab**, filter to `Fetch/XHR`. Interact with
   the page. You see every API call the JS makes, with full URLs, headers, request
   bodies, and responses.

2. **Read the JavaScript bundle** when DevTools isn't available or the call is
   conditional. This is what I did for the sub-courses dropdown. The bundle is
   minified — variable names crushed to single letters — but *string literals
   survive minification*. Searching `app.index.405ba947.js` for `sub_activities`
   found:

   ```js
   c.getSubActivitiesById = (0,n.createAPI)(n.HttpMethod.POST, "".concat(i,"/subs/{{activityId}}"))
   ```

   `i` is the activities base path, so the endpoint is
   `POST /rest/activities/subs/{activityId}`. That one line is why the collapsed
   dropdown works.

**Key insight:** minifiers rename *variables*, never *string contents*. URLs, API
paths, and field names are all strings. They're always readable.

### The three requests

```
① GET  /activity/search?...&activity_keyword=TCF
       └─► HTML containing: csrfToken = "2f253ae2-6a9d-..."
       └─► Set-Cookie: JSESSIONID=...

② POST /rest/activities/list?locale=en-US
       Headers: X-CSRF-Token, Cookie: JSESSIONID, page_info
       Body:    {"activity_search_pattern": {..., "activity_keyword": "TCF"}}
       └─► parent rows (the collapsed containers)

③ POST /rest/activities/subs/129936?locale=en-US
       Headers: same
       Body:    {"sub_activity_ids": "", "activity_transfer_pattern": {}, "open_spots": 0}
       └─► the actual dated sittings
```

Request ① exists **purely** to obtain credentials for ② and ③.

### CSRF tokens: what and why

**CSRF** = Cross-Site Request Forgery. The attack it prevents:

> You're logged into `yourbank.com`. You visit `evil.com`. Its HTML contains
> `<img src="https://yourbank.com/transfer?to=attacker&amount=1000">`. Your browser
> **automatically attaches your bank cookies** to that request, because cookies are
> sent based on *destination*, not on who initiated it. The bank sees a valid
> authenticated request and transfers the money.

The defence: require a secret value that the attacker **cannot read**. The server
embeds a random token in the page, and every state-changing request must echo it
back. `evil.com` can *cause* requests to yourbank.com, but the
**same-origin policy** prevents it from *reading* yourbank.com's HTML — so it can
never learn the token.

```
       ┌──────────────────────────────────────────────────────┐
       │ evil.com CAN make your browser send a request        │  ← forgery works
       │ evil.com CANNOT read the response, or the page       │  ← so it can't get the token
       └──────────────────────────────────────────────────────┘
```

Our script isn't an attacker — but the server can't tell the difference, so we play
by the same rules: fetch the page, read the token, include it as `X-CSRF-Token`.

**Why a fresh token every run?** Tokens are tied to a server-side session and
expire. Caching one would work for a while and then start failing mysteriously.
Fetching a new one costs ~77 KB and one round trip — worth it for never having to
debug "it worked yesterday".

### Session cookies

`JSESSIONID` is a Java servlet session identifier. The server keeps a chunk of
memory keyed by that ID. The CSRF token is validated *against that session*, so the
cookie and the token are a matched pair — you need both, from the same request ①.

In `check.py` this is handled by:

```python
self.jar = http.cookiejar.CookieJar()
self.opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    urllib.request.HTTPCookieProcessor(self.jar),
)
```

`HTTPCookieProcessor` automatically stores `Set-Cookie` headers from responses and
replays them on subsequent requests through the same opener — exactly what a
browser does.

### The shape of the data: containers vs sittings

This caused a real bug, so it's worth understanding precisely.

```
E-TCF CANADA - 4 modules          ← CONTAINER (parent_activity: true)
│                                    no date, no bookable seat
│                                    exists only to group things
│
├── SCTCFC210926-OV                ← SITTING (parent_activity: false)
│   September 21, 2026                has the date, the $400, the Enroll link
│   2/6 enrolled                      THIS is what you book
│
└── SCTCFC210926.2-OV              ← SITTING
    September 21, 2026
    1/6 enrolled
```

**All 283 container rows on that site have an empty date.** So a naive "skip rows
with no date" rule accidentally worked — until it didn't.

The trap: I decided "is this a container?" by checking
`num_of_sub_activities > 0`. But four rows on the site report
`parent_activity: true` **with** `num_of_sub_activities: 0` — including three
`E-TEF CANADA` products, the sibling of your course. Those fell through to the
"treat it as a standalone course" branch, became dateless sessions, and would have
been announced by the dateless-safety-net.

The fix uses **both** signals:

```python
if bool(parent.get("parent_activity")) or _int(parent.get("num_of_sub_activities")) > 0:
    raw = client.subs(parent.get("id"))   # it's a container: expand it
else:
    raw = [parent]                        # genuinely standalone

for item in raw:
    if item.get("parent_activity"):
        continue    # never bookable, wherever it appears
```

**General lesson:** when an API gives you two signals for the same fact, check
whether they ever disagree on real data. Here they disagreed on 4 rows out of 290 —
1.4%, and exactly the rows that mattered.

### Open-world field values

`urgent_message.status_description` is the little tag on the blue box. Values seen
live on this site:

| Value | Meaning | Bookable? |
|---|---|---|
| `""` | No tag | ✅ |
| `Full` | Capacity reached | ❌ |
| `Ended` | Already finished | ❌ |
| `Closed` | Registration shut | ❌ |
| `In progress` | Currently running | ❌ |
| `Cancelled` | Called off | ❌ |
| `Tentative` | Not yet confirmed | ✅ (timing, not capacity) |
| `Starting soon` | Begins shortly | ✅ (timing, not capacity) |
| `1 space(s) left` | Urgency banner | ✅ |

That last one is the important one. `1 space(s) left` is not a status, it's
**marketing copy** — the site reuses this field for urgency messaging. That means
the set of possible values is **open**: new strings can appear at any time.

So the code uses a **blocklist**, not an allowlist:

```python
BLOCKED_STATUSES = {"full", "ended", "cancelled", "canceled", "closed", "in progress"}
blocked = self.status.lower() in BLOCKED_STATUSES
self.available = (not blocked) and (self.spots is None or self.spots > 0)
```

Anything unrecognised is treated as **bookable**. Think about the two possible
mistakes:

| Design | New unknown status appears | Consequence |
|---|---|---|
| **Allowlist** (only known-good statuses alert) | Treated as not-bookable | **Silent miss.** You never hear about a real seat. |
| **Blocklist** (only known-bad statuses suppress) | Treated as bookable | **False alarm.** You check and find nothing. |

A false alarm costs you ten seconds. A silent miss costs you the seat. The
asymmetry decides the design.

Notice this pattern repeating: the dateless-session handling, the blocklist, the
failure alerting — all the same principle. **When you must be wrong, be wrong in
the direction that's loud rather than silent.**

---

## 3. Telegram bots, completely

### What a bot actually is

A Telegram bot is **a user account that a program controls over HTTP**. Not a
plugin, not an integration, not a special protocol — an account.

It has a username (`@Tcf_chase_bot`), a profile, can be added to groups, and appears
in members lists. The only differences from a human account:

- It logs in with a **token** instead of a phone number and SMS code
- It can't initiate conversations (see below)
- It has restricted visibility in groups by default (privacy mode)
- It cannot be online/offline, can't see other users' phone numbers, etc.

### Two APIs: Bot API vs MTProto

Telegram exposes two entirely different interfaces:

| | **MTProto** | **Bot API** |
|---|---|---|
| What it is | Telegram's native binary protocol | An HTTPS wrapper Telegram runs for you |
| Used by | Real clients (the iPad app), userbots | Bots |
| Transport | Custom binary over TCP | Plain HTTPS + JSON |
| Auth | Phone + session keys | A bot token |
| Complexity | High — custom crypto, layers, TL schema | Trivial — it's just HTTP |

We use the **Bot API**. It's a REST-ish service at `api.telegram.org` that
translates HTTPS calls into MTProto on your behalf. This is why you can operate a
bot with nothing but `curl`.

### The token

```
8629747607:AAF3iWi4cpHXYAl8zF8GJOE_Cgkj23k8dfo
└────┬───┘ └──────────────┬────────────────────┘
  bot ID          auth string (35 chars)
 (public)               (SECRET)
```

- **Bot ID** — the numeric account ID. Public; it appears in messages the bot sends.
- **Auth string** — the actual credential.

The token is **both identity and authentication**. There's no separate username and
password, no OAuth flow, no refresh token, no expiry. Possession of that string
**is** being the bot. This is why leaking it matters, and why revocation
(`/revoke` in BotFather) is the only remedy — you can't "change the password"
because the token *is* the password.

### The HTTP interface

Every method is a URL:

```
https://api.telegram.org/bot<TOKEN>/<METHOD>
```

Note the token is embedded in the **path**, not a header. Consequence worth
internalising: **the token ends up in URLs**, so it can leak into server access
logs, browser history, proxy logs, and error messages that include the URL. That's
why `check.py` is careful never to print a raw request URL on failure.

Methods we use:

| Method | Purpose |
|---|---|
| `getMe` | Validate the token; returns the bot's identity |
| `getUpdates` | Fetch incoming messages (used only by `tg_setup.py`) |
| `sendMessage` | Send a message — the only one the watcher needs |
| `getWebhookInfo` | Check whether a webhook is stealing updates |
| `deleteWebhook` | Remove one |

A minimal send:

```python
urllib.parse.urlencode({
    "chat_id": "-5472319913",
    "text": "<b>Seat open</b>",
    "parse_mode": "HTML",
    "disable_web_page_preview": "true",
})
# POSTed to https://api.telegram.org/bot<TOKEN>/sendMessage
```

Every response has the same envelope:

```json
{"ok": true,  "result": {...}}
{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}
```

**Always check `ok`.** An HTTP 200 with `"ok": false` is a failure. Code that only
checks the HTTP status will silently swallow real errors.

### Chat IDs

Every conversation has a numeric ID, and **the sign and magnitude tell you the
type**:

| Chat type | ID shape | Example |
|---|---|---|
| Private (DM with a user) | Positive | `123456789` |
| Basic group | Negative, small | `-5472319913` ← **yours** |
| Supergroup / channel | Negative, `-100` prefix | `-1001234567890` |

Yours is a **basic group**. That matters, because of migration:

### The supergroup migration trap

Telegram has two group types. A **basic group** is limited (200 members, no admin
tools, no public link). A **supergroup** is the scalable kind.

Telegram **silently upgrades** a basic group to a supergroup when you make it
public, exceed the member limit, or use certain admin features. On upgrade, the
**chat ID changes** — `-5472319913` would become something like
`-1005472319913`.

Your bot would then be sending to an ID that no longer exists. Telegram returns:

```json
{
  "ok": false,
  "error_code": 400,
  "description": "Bad Request: group chat was upgraded to a supergroup chat",
  "parameters": {"migrate_to_chat_id": -1005472319913}
}
```

The failure mode without handling: **alerts just stop, silently**, and you assume
no seats are available. This is the worst possible failure for a watcher.

`check.py` handles it explicitly:

```python
moved = params.get("migrate_to_chat_id")
if moved:
    raise RuntimeError(
        "the Telegram group became a supergroup and its chat id changed to "
        "%s - update the TELEGRAM_CHAT_ID secret" % moved
    )
```

...and the run exits non-zero, which turns it red on GitHub and triggers GitHub's
failure email. **Note the design:** we cannot notify you through Telegram, because
Telegram is exactly what's broken. So the alert has to travel through a
*different* channel. That's not incidental — it's a rule:

> **A monitoring system must never depend solely on the channel it monitors.**

### Privacy mode — why your `/start` seemed to vanish

By default, bots in groups run in **privacy mode**. A bot in privacy mode receives
only:

- Messages beginning with `/` (commands)
- Replies to the bot's own messages
- Messages that @mention the bot
- Service messages (someone joined, the bot was added, etc.)

Everything else is invisible to it. This exists so you can add a weather bot to a
group without it reading every private conversation.

This is why a plain "hello" gives you an empty `getUpdates` and it looks broken.
And it's why `tg_setup.py` tells you to send `/start@Tcf_chase_bot` — the explicit
`@username` removes any doubt about which bot a command is addressed to when
several are present.

You can disable it via BotFather `/setprivacy` → Disable, but note: **the change
only takes effect after you remove and re-add the bot to existing groups.**

### Why a bot can't message you first

Telegram enforces: **a bot may only send to a chat it has been contacted from.** A
user must tap Start, or the bot must be added to the group. There's no way to
message an arbitrary user ID.

This is anti-spam, and it's why the setup dance exists at all. It's also why the
chat ID is *discovered* rather than *configured* — you can't know it until contact
has happened.

### Two models for receiving: polling vs webhooks

Our watcher only **sends**, so it needs neither. But `tg_setup.py` needs to
*receive* (to learn your chat ID), and understanding both is worth it.

**Long polling (`getUpdates`)** — you ask, the server holds the connection open:

```
client                          telegram
  │── getUpdates?timeout=25 ──────►│
  │                                │  (holds the connection, no data yet)
  │                                │  ... 12 seconds pass ...
  │                                │  a message arrives!
  │◄──── [update_id: 501, ...] ────│
  │── getUpdates?offset=502 ──────►│   ← offset=502 CONFIRMS 501, deleting it
```

The `offset` parameter is the subtle part. Updates stay queued on Telegram's server
until you confirm them by calling `getUpdates` with an offset **greater than** the
update's ID. Until then, repeated calls return the same updates.

This gives **at-least-once** delivery: if your process crashes after handling an
update but before confirming it, you'll see it again. Your handler must be
**idempotent** (safe to run twice).

`tg_setup.py` exploits this deliberately:

```python
if wait:
    offset = batch[-1]["update_id"] + 1
```

In `--wait` mode it advances the offset to keep polling for new things. In one-shot
mode it **never** passes an offset — so it doesn't consume anything, and a re-run
sees the same updates rather than coming back mysteriously empty.

**Webhooks** — Telegram POSTs to your HTTPS URL when something happens:

```
telegram ──── POST https://you.example.com/hook ────► your server
```

| | Long polling | Webhooks |
|---|---|---|
| Needs a public HTTPS endpoint | No | **Yes** (valid cert required) |
| Latency | Near-instant with long poll | Instant |
| Works behind NAT / on a laptop | Yes | No |
| Resource cost | An open connection | Only when events occur |
| Scales to high volume | Poorly | Well |

**They are mutually exclusive.** If a webhook is set, `getUpdates` returns HTTP
409 Conflict. This is a classic "my bot receives nothing" cause, which is why
`tg_setup.py` checks `getWebhookInfo` before anything else.

### Formatting and `parse_mode`

We send `parse_mode: HTML`, which permits a small tag subset: `<b>`, `<i>`,
`<u>`, `<s>`, `<code>`, `<pre>`, `<a href>`, `<blockquote>`.

**This makes escaping mandatory.** A course name containing `&` or `<` would break
the message — or worse, be interpreted as markup. Hence:

```python
lines.append("📅 %s  ·  <code>%s</code>"
             % (html.escape(session.date_label), html.escape(session.number)))
```

You can see this working in the enrol URL: `?wishlist_id=0&locale=en-US` is sent as
`?wishlist_id=0&amp;locale=en-US`. Telegram decodes it back to `&` when rendering.
Forget the escape and Telegram rejects the whole message with a 400.

### Rate limits

| Scope | Limit |
|---|---|
| Overall | ~30 messages/second |
| Per group | ~20 messages/minute |
| Bulk broadcast | Telegram suggests ≤ 30/second |

We're nowhere near these — but they explain a design choice. `format_alerts()`
builds **one combined message** listing every new opening rather than one message
per session. With two sittings that's politeness; with twenty it would be the
difference between working and being throttled.

### Why a group beats a list of DMs

`TELEGRAM_CHAT_ID` is a single value pointing at a group. To add a watcher, you
invite them to the group — **no code change, no redeploy, no config edit**. The
alternative (a list of user IDs) would require each person to `/start` the bot,
you to collect their ID, and a config change per person.

This is a general architectural idea: **make the fan-out someone else's problem.**
Telegram already knows how to deliver one message to N people. Don't rebuild that.

---

## 4. Security

### Where the token lives, at every moment

```
① BotFather generates it
       │
       ▼
② Your clipboard / Telegram app
       │
       ├─────────────► ✗ this chat transcript      (LEAKED — revoked)
       ├─────────────► ✗ fish shell history         (LEAKED — cleared)
       │
       ▼
③ GitHub web form (over TLS)
       │
       ▼  encrypted IN YOUR BROWSER before upload
④ GitHub's encrypted secret store
       │
       ▼  decrypted only inside a running job
⑤ Environment variable TELEGRAM_BOT_TOKEN on an ephemeral VM
       │
       ▼
⑥ URL path in an HTTPS request to api.telegram.org
       │
       ▼
⑦ VM destroyed; token gone
```

Stages ④–⑦ are sound. Stage ② is where humans leak credentials, and it's where we
leaked it — twice. More on that below.

### How GitHub secrets are actually encrypted

This is not "GitHub stores your password in a database". It's public-key
cryptography, and the plaintext **never reaches GitHub's servers**.

Each repository has an X25519 **public key**. When you set a secret:

1. The client (browser or `gh`) fetches that public key
2. It encrypts your value using a **libsodium sealed box**
3. It uploads only the resulting ciphertext

A **sealed box** works like this: the sender generates a throwaway keypair,
performs a Diffie–Hellman exchange with the recipient's public key to derive a
shared symmetric key, encrypts with XSalsa20-Poly1305, and discards their private
key. The result:

```
  Anyone with the PUBLIC key  ──►  can encrypt
  Only the PRIVATE key holder ──►  can decrypt
  Even the SENDER             ──►  cannot decrypt afterwards
```

That last property is the point of "sealed". Once you've set a secret, **you**
can't read it back either — which is exactly what you observed: `gh secret list`
shows names and dates only.

`gh` says so explicitly in its own help text:

> *Secret values are locally encrypted before being sent to GitHub.*

### The permission model

| Who / what | Can read the secret? | Why |
|---|---|---|
| Anyone browsing the public repo | ❌ | It isn't in the repo |
| Anyone cloning or forking | ❌ | It isn't in git at all |
| A workflow run in **your** repo | ✅ | Injected as an env var at run time |
| A workflow from a **fork's pull request** | ❌ | GitHub deliberately withholds it |
| You, later | ❌ | Write-only; overwrite, never read |
| Someone with **write access** to the repo | ✅ | They can add a workflow that prints it |

That fork rule is the one that makes public repos safe. Without it, anyone could
open a PR containing `- run: echo $TELEGRAM_BOT_TOKEN` and read your secret from
the public logs. GitHub blocks it at the platform level.

Our workflows are aligned with this:

```yaml
# watch.yml  — has the secrets
on:
  schedule: [...]
  workflow_dispatch:     # ← no pull_request trigger at all

# tests.yml  — runs on PRs
on:
  push: {...}
  pull_request:          # ← but uses no secrets
```

The workflow holding secrets **cannot be triggered by an outsider**. The one
outsiders can trigger **has no secrets to leak**. That's not luck; it's the design.

### Log masking

GitHub scans workflow logs and replaces any exact occurrence of a secret with
`***`. You saw this:

```
env:
  TELEGRAM_BOT_TOKEN:
  TELEGRAM_CHAT_ID: ***
```

It is a **safety net, not a guarantee**. It only matches the exact string — if your
code base64-encodes the token, or prints it one character per line, masking won't
catch it. So `check.py` is written never to print the value in the first place.
Defence in depth: the code doesn't leak it, *and* the platform would mask it if it
did.

### Threat model: what actually happens if the token leaks

Be precise about blast radius rather than vaguely alarmed.

| An attacker with the bot token **can** | An attacker **cannot** |
|---|---|
| Send messages as `@Tcf_chase_bot` | Access your Telegram account |
| Read messages in groups the bot is in (subject to privacy mode) | Read your other chats |
| Delete the bot's own messages | Remove the bot or change its owner |
| Spam your group until you remove the bot | Touch your GitHub, email, or anything else |

So: **contained, annoying, not catastrophic.** But there's no upside to leaving a
leaked credential live, and revocation takes 20 seconds — so revoke.

### What went wrong here, and the right response

The token was pasted into this conversation, and later typed as a shell argument.
Both are extremely common ways credentials leak. The instructive part is the
**response pattern**:

1. **Recognise the exposure.** A transcript, a shell history file, a log, a
   screenshot, a commit — all count.
2. **Rotate immediately.** BotFather `/revoke` invalidates the old token the moment
   it issues a new one. Rotation, not concealment, is the fix. Deleting the message
   doesn't help — you must assume it was read.
3. **Clean up the secondary copies** (fish history, in this case) — not because it
   fixes the leak, but because stale credentials lying around cause confusion later.
4. **Fix the process so it can't recur.** Hence `tg_setup.py` reading via `getpass`,
   and using the GitHub web form rather than a command line argument.

Note step 4. A leak is usually a *process* failure, not a *person* failure. "Be more
careful" is not a fix. "Make the careless path impossible" is.

### Why the repo being public is fine

Everything in it is inert:

| File | Public? | Sensitive? |
|---|---|---|
| `check.py`, `tg_setup.py` | Yes | No — logic only |
| `config.json` | Yes | No — a date and a course name |
| `state.json` | Yes | No — session IDs, dates, seat counts |
| `.github/workflows/*.yml` | Yes | No — references secrets by *name* |
| Bot token, chat ID | **No** | Held in the encrypted secret store |

One thing I removed before the first push: `resources/` contained a **photo of your
laptop screen** showing browser tabs and your room. Not a credential, but personal,
permanent, and indexed by GitHub code search. Because it was already in all 11
commits, `git rm` wouldn't have been enough — history had to be rewritten with
`git filter-branch`, the backup refs deleted, and the objects garbage-collected.

> **Rule:** it is trivial to add something to a public repo, and genuinely hard to
> remove it. Decide *before* the first push.

---

## 5. GitHub Actions

### The mental model

GitHub Actions is **event-driven compute attached to a repository**. Something
happens → GitHub starts a fresh virtual machine → runs your commands → destroys it.

The hierarchy:

```
Event            "the clock hit */5" or "someone pushed" or "a button was clicked"
  └── Workflow   one .yml file in .github/workflows/
        └── Job  a unit that gets its own fresh VM  (ours: "check")
              └── Step   one command or one reusable Action
```

Key property: **the VM is ephemeral**. It's created for the job and destroyed
after. Nothing you write to disk survives. This single fact is why
[state management](#6-state-the-hard-part) is the hardest part of the system.

### Runners

A **runner** is the machine executing a job. `runs-on: ubuntu-latest` requests a
GitHub-hosted Ubuntu VM: 4 CPU, 16 GB RAM, ~14 GB SSD, pre-loaded with Python,
Node, Docker, git, and the `gh` CLI.

Startup costs ~10–20 seconds. Our runs take 9–15 seconds total, so **most of the
elapsed time is the machine booting**, not our script working.

### `watch.yml`, line by line

```yaml
name: watch-tcf                    # shown in the Actions UI
```

```yaml
on:
  schedule:
    - cron: '*/5 * * * *'          # every 5 minutes, ALWAYS UTC
  workflow_dispatch:               # adds the manual "Run workflow" button
```

`workflow_dispatch` is the reason we could test immediately instead of waiting for
cron. Always add it to a scheduled workflow — debugging without it is miserable.

```yaml
permissions:
  contents: write                  # allow the job to push commits
```

Every job gets an automatic `GITHUB_TOKEN` — a temporary credential minted for that
run and **revoked when the job ends**. `permissions:` controls what it may do.
Default is read-only; we need `contents: write` to commit `state.json`.

This is **least privilege**: the token can write to this repo's contents and
nothing else — not issues, not packages, not other repos.

```yaml
concurrency:
  group: watch-tcf
  cancel-in-progress: false
```

A **concurrency group** ensures only one run executes at a time; others queue.
Without it, two overlapping runs could both try to commit `state.json` and one
would clobber the other. `cancel-in-progress: false` means "queue them", not
"cancel the older one" — safer when a job is mid-commit.

```yaml
      - uses: actions/checkout@v4
```

An **Action** is a reusable package of steps. `actions/checkout` clones your
repository into the runner. Without it the VM is empty — it does **not**
automatically contain your code.

```yaml
      - name: Check for open seats
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python3 check.py
```

`${{ secrets.X }}` is resolved by GitHub at run time; the decrypted value is
injected as an environment variable. It exists only in that process, on that VM,
for those few seconds.

```yaml
      - name: Persist state if it changed
        run: |
          if git diff --quiet -- state.json; then
            echo "state unchanged; nothing to commit"
            exit 0
          fi
          ...
          for attempt in 1 2 3; do
            if git push; then exit 0; fi
            git pull --rebase --autostash
          done
```

Three deliberate details:

1. **Commit only on change.** At 288 runs/day, committing unconditionally would be
   ~105,000 commits a year. Only committing real changes keeps it to a handful.
2. **Retry with rebase.** If another run pushed first, `git push` is rejected.
   Rebase onto their work and retry rather than failing.
3. **Exit non-zero after 3 attempts** so a persistent problem turns the run red.

### cron, in detail

```
┌───────── minute      (0-59)
│ ┌─────── hour        (0-23)
│ │ ┌───── day of month(1-31)
│ │ │ ┌─── month       (1-12)
│ │ │ │ ┌─ day of week (0-6, Sunday = 0)
│ │ │ │ │
* * * * *
```

`*/5 * * * *` = "every minute divisible by 5, every hour, every day" → :00, :05,
:10, …

**Actions cron is always UTC.** There is no timezone option. This is why the
heartbeat setting is named `heartbeat_hour_utc` — the name encodes the constraint
so you can't misread it later.

### Why 5 minutes is the floor, and why it's really 5–30

GitHub documents a **minimum interval of 5 minutes**. Shorter expressions like
`*/1` are silently not honoured.

More importantly, scheduled runs are explicitly **best-effort**. Delays of 5–30
minutes are common, worst at the top of each hour — because that's when everyone's
`0 * * * *` jobs fire at once and the queue backs up.

**It is worse than "delayed".** Under load GitHub does not queue a scheduled run
and get to it late — it **skips it entirely**. No run appears, no error is raised,
nothing is emailed. From the outside it is indistinguishable from the watcher
deciding there was nothing to report.

We measured this on this very repo. With `cron: '*/5 * * * *'`:

```
06:40 UTC   workflow deployed
12:02 UTC   first scheduled run           ← 5.5 hours later
15:20 UTC   still the ONLY scheduled run  ← ~100 expected, 1 delivered
```

The cause is that `*/5` fires at :00, :05, :10 … which is precisely when every
other `*/5` and hourly job on GitHub fires. You are queuing behind the entire
platform at exactly the busiest instant.

The fix costs nothing — keep the cadence, move off the boundary:

```yaml
- cron: '3,8,13,18,23,28,33,38,43,48,53,58 * * * *'
```

Twelve fires an hour, same 5-minute spacing, landing at :03, :08, :13 … where
there is far less contention.

Practical takeaways:

- **Never** rely on Actions cron for precise timing
- **Never schedule on a round boundary.** Prefer `7 * * * *` to `0 * * * *`, and
  an explicit minute list to `*/5`
- Treat a missing run as normal, not as an error — design for gaps
- If you need a genuine guarantee, Actions is the wrong tool. Cloudflare Workers
  cron does 1-minute intervals reliably; an always-on VM does anything

This is the main tradeoff in the whole system, and the one worth revisiting if the
watcher ever misses something that mattered.

### Billing, and why the repo must be public

| Repo type | Actions minutes |
|---|---|
| **Public** | Free, unlimited, on standard runners |
| **Private (free tier)** | 2,000 minutes/month |

Our schedule: 288 runs/day × ~15s ≈ 72 min/day ≈ **2,200 min/month** — already over
the private allowance, and billing rounds each run up to a whole minute, which makes
it far worse (288 min/day ≈ 8,600/month).

Public isn't a preference here, it's a requirement of the chosen cadence. Which is
why [Part 4](#4-security) had to be airtight first.

### The 60-day auto-disable

GitHub **disables scheduled workflows after 60 days of repository inactivity**. A
watcher that silently stops after two months would be worse than useless.

Our defence falls out of the design for free: the **daily heartbeat** writes
`last_heartbeat_date` into `state.json`, which the workflow commits. A commit is
repository activity. So the thing that proves the watcher is alive to *you* also
keeps it alive on *GitHub*.

Good design often looks like this — one mechanism satisfying two requirements that
seemed unrelated.

---

## 6. State: the hard part

### Why state is needed at all

The VM is destroyed after every run. Nothing persists. But answering
*"should I tell the user?"* requires knowing what you told them last time.

```
Poll 1:  seat open  → previously? nothing known  → TELL THEM
Poll 2:  seat open  → previously? already told   → stay quiet
Poll 3:  seat open  → previously? told 6 h ago   → remind
Poll 4:  full       → record it                  → stay quiet
Poll 5:  seat open  → previously? it was full    → TELL THEM ("reopened")
```

Without memory you get poll 1's behaviour every time: **288 identical messages a
day**.

### Where to put it

| Option | How | Verdict |
|---|---|---|
| **Actions cache** | `actions/cache` | Evicted after 7 days unused; keys are immutable so you need rolling keys. Fiddly. |
| **Artifacts** | `upload/download-artifact` | Meant for build outputs; awkward for read-modify-write. |
| **External DB** | Redis, S3, Firebase | Works, but adds a service, credentials, and cost for one small JSON file. |
| **Commit to the repo** ✅ | `git commit state.json` | Durable, versioned, human-readable, free, *and* counts as repo activity. |

We commit. The bonus is real: `state.json` is a complete audit log. You can
`git log -p state.json` and watch every seat change over time.

The cost is commit noise, mitigated by only committing on change.

### The bug we hit — and why it's a classic

This is the most valuable lesson in the project, because it's a famous class of
distributed-systems bug that people rediscover constantly.

**Original code:**

```python
def decide(sessions, state, cfg, now):
    ...
    if reason:
        alerts.append((session, reason))
        entry["last_alert"] = now.isoformat()   # ← recorded as sent
```

`decide()` stamped "we told them" **at the moment of deciding**, before anything was
sent. Then:

```python
if not token or not chat_id:
    print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset; not sending")
    save_json(args.state, state)     # ← saves the "already told them" stamp!
    return 0
```

What actually happened, in order:

```
06:40  run #1   token not yet set
                → finds 2 open sittings
                → decide() stamps last_alert = 06:40:07
                → "not sending"  (no token)
                → saves state WITH the stamps
                → commits them to GitHub

06:44  run #2   token now set
                → finds the same 2 sittings
                → decide() sees last_alert = 06:40, only 4 minutes ago
                → "already told him, and the 6 h reminder isn't due"
                → 0 alerts

       YOU:     receive nothing, and would have received nothing
                until 12:40 — six hours later.
```

The general shape of this bug:

> **Recording that an action happened, before the action has actually happened.**

You'll meet it as: marking an email sent before SMTP accepts it; acknowledging a
queue message before processing it; committing a database transaction before the
downstream call returns; marking a webhook delivered before it's been received.

**The fix — separate deciding from recording:**

```python
def decide(...):
    """...The returned state carries the PREVIOUS last_alert;
    call mark_delivered() once sending succeeds."""
    # no stamping here

def mark_delivered(next_state, alerts, now):
    """Stamp the send time - only after the message has actually gone out."""
    stamp = now.isoformat()
    for session, _reason in alerts:
        if session.id in next_state:
            next_state[session.id]["last_alert"] = stamp
```

and in `main()`:

```python
try:
    telegram_send(...)              # ① actually send
except Exception as exc:
    print("could not deliver...")
    return 1                        # ② state NOT saved → retried next run

mark_delivered(state["sessions"], alerts, now)   # ③ only now record it
save_json(args.state, state)
```

### At-least-once vs at-most-once

Every delivery system picks one. You cannot have both.

| Guarantee | Means | Failure mode | Record when? |
|---|---|---|---|
| **At-most-once** | Never duplicated | Can be **lost** | *Before* acting |
| **At-least-once** | Never lost | Can be **duplicated** | *After* acting |
| Exactly-once | Both | Impossible over an unreliable network¹ | — |

¹ It can be *approximated* with idempotency keys and deduplication, but the
underlying delivery is still at-least-once.

Our original code accidentally implemented **at-most-once** — and lost the message.
For a seat watcher, losing an alert is the whole failure; getting a duplicate costs
you nothing. So **at-least-once is obviously correct here**, and now that's what it
does: if anything fails, state isn't saved, so the next run retries.

Ask this of any notification system you build: *which of these two did I choose,
and did I choose it on purpose?*

### The state file

```json
{
  "sessions": {
    "129937": {
      "available": true,
      "date": "2026-09-21",
      "last_alert": "2026-09-15T06:47:05.722915+00:00",
      "number": "SCTCFC210926-OV",
      "spots": 4,
      "status": ""
    }
  },
  "consecutive_failures": 0,
  "failure_alerted": false,
  "last_heartbeat_date": null,
  "last_check": "2026-09-15T06:47:05.722915+00:00"
}
```

| Field | Job |
|---|---|
| `available` | Detects full → open transitions |
| `last_alert` | Suppresses repeats; drives the 6-hour reminder. **Only set after delivery.** |
| `consecutive_failures` | Counts toward the "watcher is broken" alert |
| `failure_alerted` | Ensures the broken-alert fires once, not every run |
| `last_heartbeat_date` | One heartbeat per day |
| `last_check` | Human-readable "is it alive" without reading logs |

Sessions outside the date window are simply **absent**. That's why raising
`min_test_date` to 2026-11-15 emptied the file — and why lowering it later makes
those sessions look brand new, producing a burst of alerts. Correct behaviour, but
worth knowing before it surprises you.

---

## 7. Deployment

### There is no build step

Modern "deployment" often means compile → bundle → containerise → push image →
roll out. None of that here:

```
git push  ──►  GitHub stores the files  ──►  next run clones them  ──►  done
```

Each run does `actions/checkout@v4`, which clones the repository **at that moment**.
So whatever is on `main` when the run starts is what executes.

Consequences:

- **`git push` IS the deployment.** No separate step.
- **Changing `config.json` needs no redeploy.** The next run reads the new value.
  Editing `min_test_date` in the GitHub web UI and clicking Commit is a complete
  deployment.
- **Rollback is `git revert`.** The next run picks up the reverted code.
- **There are no versions or environments.** One branch, one live thing.

This works because the workload is tiny and dependency-free. `check.py` imports only
the Python standard library — no `pip install`, so no lockfile, no dependency
resolution, nothing to break between runs. That was a deliberate choice: at 288 runs
a day, a `pip install` step would be 288 chances a day for a package registry
hiccup to break your watcher.

### Config vs code

```
check.py       — HOW to decide    (logic; changing it needs thought + tests)
config.json    — WHAT to look for (data; changing it is routine)
secrets        — WHO to tell      (credentials; never in the repo)
```

Separating these means changing your cutoff date is a one-line data edit anyone can
do in a browser, not a code change. Keeping `min_test_date` out of `check.py` is
what makes that possible.

### What deployment looked like here

```bash
gh repo create monitor-tcf --public --source=. --remote=origin --push
# → https://github.com/82Kang/monitor-tcf

gh secret set TELEGRAM_CHAT_ID --body "-5472319913"
# (the token went in via the web UI, never a command line)

gh workflow run watch.yml     # don't wait for cron; prove it now
gh run view <id> --log        # verify from the logs, not from hope
```

That last pair matters. **Deployment isn't finished when the push succeeds** — it's
finished when you've watched a real run do the real thing. Our first "successful"
run was green while silently doing nothing, because the token was missing. Green
means "the process exited 0", not "it worked".

---

## 8. Observability

### The core problem: silence is ambiguous

A watcher that says nothing could mean:

- ✅ No seats available (working correctly)
- ❌ The script is crashing
- ❌ The website changed its API
- ❌ The Telegram token was revoked
- ❌ GitHub disabled the schedule after 60 days
- ❌ The group became a supergroup and the chat ID changed

**All six look identical from your phone.** Everything below exists to break that
tie.

### Three independent signals

```
                     ┌──► Telegram   "watcher is failing" after 3 consecutive errors
  something breaks ──┼──► GitHub UI  the run turns red in the Actions list
                     └──► Email      GitHub emails you on scheduled-workflow failure
```

They're **independent on purpose**. If Telegram is the thing that's broken, signals
2 and 3 still reach you. Never let your only alarm depend on the system it's
watching.

### The heartbeat

Once a day at 13:00 UTC:

```
💤 TCF watcher is alive

Watching E-TCF CANADA for test dates on or after 2026-11-15.
No sessions listed in that window right now.
```

This converts silence from *ambiguous* to *meaningful*. If the heartbeat arrives,
silence definitively means "no seats". If it stops, something is wrong — and you
find out within a day rather than whenever you next think to check.

It also, as noted, keeps the schedule alive past GitHub's 60-day rule.

### Failure alerting with hysteresis

```python
state["consecutive_failures"] += 1
if (state["consecutive_failures"] >= threshold      # 3 in a row
        and not state.get("failure_alerted")):      # and not already shouting
    telegram_send(... "🚨 TCF watcher is failing" ...)
    state["failure_alerted"] = True
```

Two deliberate behaviours:

- **Threshold of 3** — a single network blip is not an incident. Requiring three
  consecutive failures (~15 minutes) filters transient noise.
- **`failure_alerted` latch** — alert once on the way down, then stay quiet. Without
  it, a two-day outage means 576 identical panic messages. When it recovers you get
  one "✅ recovered" message.

This pattern — threshold + latch — is what every real alerting system does, and
it's why PagerDuty exists.

### Exit codes are a signal

```python
return 0   # site fetch failed → stay green, retry next run, state still commits
return 1   # Telegram delivery failed → go RED, because we can't tell you any other way
```

A deliberate distinction. A flaky website is expected and self-healing — going red
every time would train you to ignore red. A broken notification channel is
different: it's the one failure the system **cannot report through itself**, so it
escalates to the only channels left.

---

## 9. One poll, traced end to end

Everything above, in sequence, for a single run.

```
T+0.0s   GitHub's scheduler fires the '*/5' cron for watch-tcf
         (in practice, T+0 to T+30min later — best-effort)

T+0.1s   Concurrency check: is another watch-tcf run active?
         No → proceed.  Yes → queue.

T+2s     A fresh ubuntu-latest VM boots

T+12s    actions/checkout@v4 clones 82Kang/monitor-tcf at main
         The VM now has check.py, config.json, state.json

T+13s    GitHub decrypts TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
         and injects them as environment variables

T+13s    python3 check.py starts
         │
         ├─ load config.json  → min_test_date = "2026-11-15"
         ├─ load state.json   → what we knew last time
         │
         ├─ ① GET /activity/search?activity_keyword=TCF
         │     ← HTML + Set-Cookie: JSESSIONID
         │     regex out: csrfToken = "2f253ae2-..."
         │
         ├─ ② POST /rest/activities/list
         │     headers: X-CSRF-Token, Cookie, page_info
         │     ← [ {id:129936, name:"E-TCF CANADA - 4 modules",
         │          parent_activity:true, num_of_sub_activities:2}, ... ]
         │     filter by name_match "E-TCF CANADA"
         │
         ├─ ③ POST /rest/activities/subs/129936
         │     ← [ {id:129937, number:"SCTCFC210926-OV",
         │          date_range_start:"2026-09-21",
         │          total_open:6, already_enrolled:2,
         │          urgent_message:{status_description:""}}, ... ]
         │
         ├─ classify each sitting:
         │     status ""             → not blocked
         │     6 - 2 = 4 spots       → > 0
         │     available = True
         │     date "2026-09-21" >= "2026-11-15"?  NO  → dropped
         │
         ├─ collect() returns []          (0 sessions in window)
         ├─ decide()  returns ([], {})    (0 alerts)
         │
         ├─ heartbeat due? hour 6 < 13 → no
         │
         └─ save state.json, exit 0

T+24s    git diff --quiet state.json → unchanged → no commit

T+25s    Job succeeds. VM destroyed. Everything on it is gone.

         Next run in ~5 minutes, from a completely clean machine.
```

Now imagine the seat *is* in window and open. The only differences:

```
         ├─ collect() returns [Session 129937, Session 129938]
         ├─ decide()  returns ([(129937,"new"), (129938,"new")], {...})
         │     because state has no record of them
         │
         ├─ format_alerts() builds ONE combined message
         │     "🆕 New session — E-TCF CANADA - 4 modules, September 21, 2026 (2 sittings)"
         │
         ├─ POST https://api.telegram.org/bot<TOKEN>/sendMessage
         │     ← {"ok": true, ...}          ← delivery CONFIRMED
         │
         ├─ mark_delivered()  ← only now is last_alert stamped
         └─ save state.json

T+24s    git diff → CHANGED → commit + push
         "state: 2026-09-15T06:47Z"
```

And if `sendMessage` had returned `{"ok": false}`? An exception, `return 1`, state
**not** saved — so the next run tries again. At-least-once, by construction.

---

## 10. Glossary

**Action** — A reusable, packaged step for GitHub Actions, e.g. `actions/checkout`.
Confusingly, "GitHub Actions" (the product) and "an Action" (a package) are
different things.

**At-least-once / at-most-once** — Delivery guarantees. At-least-once may duplicate
but never loses; at-most-once may lose but never duplicates. You must pick one.

**Bot API** — Telegram's HTTPS+JSON interface for bots, at `api.telegram.org`. The
simple alternative to MTProto.

**Chat ID** — Numeric identifier for a Telegram conversation. Positive = private
chat, negative = group, `-100…` prefix = supergroup or channel.

**Concurrency group** — A GitHub Actions label ensuring only one run with that label
executes at a time.

**cron** — Five-field time expression (`minute hour day month weekday`). In GitHub
Actions it is always interpreted as UTC.

**CSRF (Cross-Site Request Forgery)** — An attack where a malicious site causes your
browser to make authenticated requests to another site. Defended against with a
secret token the attacker can't read.

**Ephemeral** — Created for one use then destroyed. GitHub runners are ephemeral,
which is why nothing written to disk survives.

**`GITHUB_TOKEN`** — A short-lived credential GitHub mints for each workflow run.
Scoped by `permissions:` and revoked when the job ends.

**Hysteresis / latch** — Requiring N consecutive failures before alerting, and
alerting only once until recovery. Prevents alarm spam.

**Idempotent** — Safe to perform more than once with the same result. Necessary
whenever delivery is at-least-once.

**Least privilege** — Grant only the permissions actually required. `contents: write`
rather than blanket write access.

**Long polling** — Client requests, server holds the connection open until data
arrives or a timeout elapses. Near-instant delivery without needing a public
endpoint.

**Minification** — Shrinking JavaScript by renaming variables and stripping
whitespace. **String literals are preserved**, which is why API paths remain
discoverable in a minified bundle.

**MTProto** — Telegram's native binary protocol, used by real clients. Bots use the
simpler Bot API instead.

**Poll / polling** — Repeatedly asking "has it changed?" The fallback when the data
source offers no push mechanism.

**Privacy mode** — Default Telegram setting where a bot in a group sees only
commands, replies to itself, mentions, and service messages.

**Runner** — The VM that executes a GitHub Actions job.

**Same-origin policy** — Browser rule preventing a page from reading responses from
a different origin. The foundation that makes CSRF tokens effective.

**Sealed box** — libsodium construction where anyone with the public key can
encrypt, only the private key holder can decrypt, and **even the sender cannot
decrypt afterwards**. How GitHub secrets work.

**SPA (Single-Page Application)** — A web app that ships a JS bundle and builds the
page client-side from API calls. Its HTML contains no data.

**Supergroup** — Telegram's scalable group type. Basic groups auto-upgrade to
supergroups, **changing the chat ID** in the process.

**Webhook** — Inverted API call: the service POSTs to *your* URL when an event
occurs. Requires a public HTTPS endpoint.

**`workflow_dispatch`** — Trigger that adds a manual "Run workflow" button.
Essential for testing scheduled workflows.

---

## 11. Exercises

Ways to make this concrete. Roughly increasing difficulty.

**1. Watch a real poll.**
Go to the [Actions tab](https://github.com/82Kang/monitor-tcf/actions), open the
newest run, expand "Check for open seats". Match every line to the trace in
[Part 9](#9-one-poll-traced-end-to-end).

**2. Prove the cutoff yourself.**
```bash
python3 check.py --dry-run --min-test-date 2020-01-01   # finds the Sept 21 sittings
python3 check.py --dry-run --min-test-date 2027-01-01   # finds nothing
```
`--dry-run` sends nothing and writes nothing, so it's always safe.

**3. See the raw API.**
```bash
python3 check.py --dump-activity 129936
```
Compare that JSON to the rendered page. Find `total_open`, `already_enrolled`, and
`urgent_message.status_description` in both.

**4. Read the state history.**
```bash
git log -p --follow state.json
```
Every seat change the watcher has ever observed, as a diff.

**5. Break it deliberately.**
Change `name_match` in `config.json` to `"NONSENSE"`, push, trigger a run. Watch it
find zero sessions. Revert. This teaches you what a *correct* empty result looks
like, so you can distinguish it from a broken one.

**6. Cause a real failure.**
Temporarily set `TELEGRAM_CHAT_ID` to `-1`. Trigger a run. Watch it go **red**,
observe the error, and confirm `state.json` was **not** committed — proving the
at-least-once retry. Then set it back.

**7. Reason about it before testing.**
If Alliance Française posts a sitting on 20 November with 1 of 8 seats taken, and
the run happens at 14:00 UTC — exactly which messages do you receive, and what does
`state.json` look like afterwards? Work it out from
[Part 6](#6-state-the-hard-part), then verify against the code.

---

## The five ideas worth keeping

Strip away the specifics and this is what generalises:

1. **When you must be wrong, be wrong loudly.** Unknown status → assume bookable.
   Dateless session → alert anyway. Site unreachable → say so. Silent failure is the
   only unrecoverable kind.

2. **Never record that something happened until it has happened.** Stamping
   `last_alert` before `sendMessage` succeeded cost us the first real alert. This bug
   has a thousand disguises.

3. **A monitor must not depend solely on what it monitors.** If Telegram breaks, the
   alarm has to travel by GitHub and email instead.

4. **Silence needs a meaning.** Without a heartbeat, "no messages" is indistinguishable
   from "it died three weeks ago".

5. **Decide what's public before the first push.** Adding to a public repo takes a
   second; removing it takes history rewriting — and you must assume it was already
   copied.
