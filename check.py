#!/usr/bin/env python3
"""Watch ACTIVE Communities (Alliance Française de Toronto) for open seats in the
E-TCF CANADA test sessions and alert a Telegram group.

The public site is a React SPA backed by two unauthenticated JSON endpoints. The
dated sessions the user cares about live inside the collapsed "View sub-courses"
dropdown, which is a separate call from the search listing.

Only the standard library is used so the GitHub Actions job needs no install step.
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

ORIGIN = "https://anc.ca.apm.activecommunities.com"
BASE = ORIGIN + "/aftoronto"
SEARCH_PAGE = (
    BASE + "/activity/search?onlineSiteId=0&activity_select_param=0&drop_in=0"
    "&activity_keyword={kw}&viewMode=list&locale=en-US"
)
LIST_API = BASE + "/rest/activities/list?locale=en-US"
SUBS_API = BASE + "/rest/activities/subs/{id}?locale=en-US"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# urgent_message.status_description values that mean "you cannot book this".
# Observed live on this site: "", "Full", "Ended", "Closed", "In progress",
# "Tentative", "Starting soon". The last two stay bookable on purpose - they
# describe timing, not capacity.
BLOCKED_STATUSES = {"full", "ended", "cancelled", "canceled", "closed", "in progress"}
UNLIMITED = -1  # total_open sentinel for uncapped activities

def _ssl_context():
    """A context with a usable CA bundle.

    Ubuntu runners and Homebrew Python work off the system store. The python.org
    macOS build ships with an empty one unless "Install Certificates.command" was
    run, so fall back to certifi and then the system bundle.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0):
        return ctx
    candidates = []
    try:
        import certifi

        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates.append("/etc/ssl/cert.pem")
    for path in candidates:
        if path and os.path.exists(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except OSError:
                continue
            if ctx.cert_store_stats().get("x509_ca", 0):
                return ctx
    return ctx


SSL_CONTEXT = _ssl_context()

DEFAULT_CONFIG = {
    "keyword": "TCF",
    "name_match": "E-TCF CANADA",
    "min_test_date": "1970-01-01",
    "reminder_hours": 6,
    "heartbeat_hour_utc": 13,
    "failure_alert_after": 3,
    "alert_undated": True,
    # Names this runner in every message. Two runners share one Telegram group
    # but keep separate state, so an unlabelled heartbeat cannot tell you WHICH
    # of them is alive - and that is the only question a heartbeat exists to
    # answer. Overridden per runner via the WATCHER_LABEL env var.
    "label": "TCF watcher",
}


# --------------------------------------------------------------------------- #
# Site client
# --------------------------------------------------------------------------- #


class ActiveNet:
    """Minimal client for the ActiveNet activity search endpoints."""

    def __init__(self, keyword, timeout=30):
        self.keyword = keyword
        self.timeout = timeout
        self.referer = SEARCH_PAGE.format(kw=urllib.parse.quote(keyword))
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=SSL_CONTEXT),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.csrf = None

    def bootstrap(self):
        """Load the search page for its csrfToken and JSESSIONID cookie."""
        req = urllib.request.Request(
            self.referer,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            page = resp.read().decode("utf-8", "replace")
        match = re.search(r'csrfToken\s*=\s*"([0-9a-fA-F-]{36})"', page)
        if not match:
            raise RuntimeError(
                "no csrfToken on the search page - the site layout likely changed"
            )
        self.csrf = match.group(1)
        return self.csrf

    def _post(self, url, payload, page_number=1, per_page=20):
        if not self.csrf:
            raise RuntimeError("bootstrap() must run before any API call")
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrf,
            "Referer": self.referer,
            "Origin": ORIGIN,
            "page_info": json.dumps(
                {
                    "order_by": "",
                    "page_number": page_number,
                    "total_records_per_page": per_page,
                }
            ),
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
        meta = doc.get("headers") or {}
        code = meta.get("response_code")
        if code not in (None, "0000"):
            raise RuntimeError(
                "API error %s: %s" % (code, meta.get("response_message"))
            )
        return doc

    def _search_pattern(self):
        return {
            "activity_search_pattern": {
                "skills": [],
                "time_after_str": "",
                "days_of_week": None,
                "activity_select_param": 0,
                "center_ids": [],
                "time_before_str": "",
                "open_spots": None,
                "activity_id": None,
                "activity_category_ids": [],
                "date_before": "",
                "min_age": None,
                "date_after": "",
                "activity_type_ids": [],
                "site_ids": [],
                "for_map": False,
                "geographic_area_ids": [],
                "season_ids": [],
                "activity_department_ids": [],
                "activity_other_category_ids": [],
                "child_season_ids": [],
                "activity_keyword": self.keyword,
                "instructor_ids": [],
                "max_age": None,
                "custom_price_from": "",
                "custom_price_to": "",
            },
            "activity_transfer_pattern": {},
        }

    def search(self):
        """All parent activities matching the keyword, following pagination."""
        items, page = [], 1
        while page <= 25:  # hard stop; this search returns a handful of rows
            doc = self._post(LIST_API, self._search_pattern(), page_number=page)
            items.extend((doc.get("body") or {}).get("activity_items") or [])
            info = (doc.get("headers") or {}).get("page_info") or {}
            if page >= int(info.get("total_page") or 1):
                break
            page += 1
        return items

    def subs(self, parent_id):
        """The dated sessions behind an activity's 'View sub-courses' dropdown."""
        doc = self._post(
            SUBS_API.format(id=parent_id),
            {"sub_activity_ids": "", "activity_transfer_pattern": {}, "open_spots": 0},
        )
        return (doc.get("body") or {}).get("sub_activities") or []


# --------------------------------------------------------------------------- #
# Session model
# --------------------------------------------------------------------------- #


class Session:
    """One bookable, dated test sitting."""

    __slots__ = (
        "id", "number", "name", "date", "date_label", "location", "fee",
        "url", "status", "capacity", "enrolled", "spots", "available", "undated",
    )

    def __init__(self, item):
        self.id = str(item.get("id"))
        self.number = item.get("number") or ""
        self.name = item.get("name") or ""
        self.date = item.get("date_range_start") or ""
        self.date_label = (
            item.get("date_range") or item.get("date_range_description") or self.date
        )
        # Multi-schedule courses return "Select course to view additional dates
        # and times" with no ISO date, so the cutoff cannot be applied to them.
        self.undated = not self.date
        self.location = _label(item.get("location"))
        self.fee = _label(item.get("fee"))
        self.url = _href(item.get("enroll_now")) or item.get("detail_url") or ""
        self.status = ((item.get("urgent_message") or {}).get("status_description") or "").strip()

        self.capacity = _int(item.get("total_open"))
        self.enrolled = _int(item.get("already_enrolled"))
        if self.capacity == UNLIMITED:
            self.spots = None  # uncapped
        else:
            self.spots = max(self.capacity - self.enrolled, 0)

        blocked = self.status.lower() in BLOCKED_STATUSES
        self.available = (not blocked) and (self.spots is None or self.spots > 0)

    @property
    def seats_label(self):
        if self.spots is None:
            return "unlimited spots"
        return "%d of %d spots left" % (self.spots, self.capacity)

    def __repr__(self):
        return "<Session %s %s %s %s %s>" % (
            self.id, self.number, self.date,
            "AVAILABLE" if self.available else "unavailable",
            self.status or "-",
        )


def _label(obj):
    return (obj or {}).get("label") or "" if isinstance(obj, dict) else ""


def _href(obj):
    return (obj or {}).get("href") or "" if isinstance(obj, dict) else ""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect(client, cfg):
    """Every in-window session for the watched course, available or not."""
    needle = cfg["name_match"].casefold()
    cutoff = cfg["min_test_date"]
    found = []
    for parent in client.search():
        if needle not in (parent.get("name") or "").casefold():
            continue
        # A row is a container ("View sub-courses") if EITHER signal says so.
        # They disagree on real rows: E-TEF CANADA reports parent_activity=True
        # with num_of_sub_activities=0, and trusting the count alone made the
        # empty container look like a bookable, dateless session.
        if bool(parent.get("parent_activity")) or _int(parent.get("num_of_sub_activities")) > 0:
            raw = client.subs(parent.get("id"))
        else:
            # Flat activity with no dropdown - treat the row itself as the session.
            raw = [parent]
        for item in raw:
            if item.get("parent_activity"):
                # Containers hold no seats of their own; the price, the date and
                # the Enroll link all live on the children. Never alert on one.
                continue
            session = Session(item)
            if session.undated:
                # No ISO date to compare. Dropping these silently is the one way
                # this watcher could stay quiet while a seat was in fact open,
                # so surface them instead and let the message say so.
                if cfg.get("alert_undated", True):
                    found.append(session)
                continue
            if session.date >= cutoff:  # ISO dates compare correctly as strings
                found.append(session)
    found.sort(key=lambda s: (s.date or "9999-99-99", s.number))
    return found


# --------------------------------------------------------------------------- #
# State and alert decisions
# --------------------------------------------------------------------------- #


def empty_state():
    return {
        "sessions": {},
        "consecutive_failures": 0,
        "failure_alerted": False,
        "last_heartbeat_date": None,
        "last_check": None,
    }


def load_json(path, fallback):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return dict(fallback)
    merged = dict(fallback)
    merged.update(data or {})
    return merged


def save_json(path, data):
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _parse_iso(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def mark_delivered(next_state, alerts, now):
    """Stamp the send time - only after the message has actually gone out.

    Kept separate from decide() on purpose: stamping at decision time records
    alerts that were never delivered, and the next run then suppresses them as
    already-sent. That silently swallowed the very first real alert.
    """
    stamp = now.isoformat()
    for session, _reason in alerts:
        if session.id in next_state:
            next_state[session.id]["last_alert"] = stamp


def decide(sessions, state, cfg, now):
    """Return (alerts, next_sessions_state).

    Alerts fire on new sessions, on full -> open transitions, and as a spaced
    reminder while a session stays open. Never once per poll. The returned state
    carries the PREVIOUS last_alert; call mark_delivered() once sending succeeds.
    """
    alerts = []
    next_state = {}
    reminder = float(cfg["reminder_hours"]) * 3600.0
    previous = state.get("sessions") or {}

    for session in sessions:
        prior = previous.get(session.id) or {}
        entry = {
            "available": session.available,
            "number": session.number,
            "date": session.date,
            "spots": session.spots,
            "status": session.status,
            "last_alert": prior.get("last_alert"),
        }
        if session.available:
            last_alert = _parse_iso(prior.get("last_alert"))
            if not prior:
                reason = "new"
            elif not prior.get("available"):
                reason = "reopened"
            elif last_alert is None:
                reason = "new"
            elif (now - last_alert).total_seconds() >= reminder:
                reason = "reminder"
            else:
                reason = None
            if reason:
                alerts.append((session, reason))
        else:
            entry["last_alert"] = None
        next_state[session.id] = entry

    return alerts, next_state


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


def telegram_send(token, chat_id, text, timeout=30):
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=payload
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # A group silently becomes a supergroup when it grows or is made public,
        # and its chat id changes. Telegram rejects the old id but names the new
        # one - without this the watcher would just stop reaching you.
        try:
            params = (json.loads(detail).get("parameters") or {})
        except ValueError:
            params = {}
        moved = params.get("migrate_to_chat_id")
        if moved:
            raise RuntimeError(
                "the Telegram group became a supergroup and its chat id changed to "
                "%s - update the TELEGRAM_CHAT_ID secret" % moved
            ) from None
        raise RuntimeError("telegram HTTP %s: %s" % (exc.code, detail)) from None
    if not doc.get("ok"):
        raise RuntimeError("telegram rejected the message: %s" % doc)
    return doc


# The first line is all a phone's lock screen previews, so it has to carry the
# course and the date - "a seat opened" alone forces you to open the app to find
# out which sitting it means.
REASON_VERB = {
    "reopened": ("🎉", "Seat open"),
    "new": ("🆕", "New session"),
    "reminder": ("⏰", "Still open"),
}


def format_heading(reason, group):
    emoji, verb = REASON_VERB[reason]
    names = {session.name for session in group}
    course = group[0].name if len(names) == 1 else "%d courses" % len(names)

    dates = []
    for session in group:
        label = "date TBC" if session.undated else session.date_label
        if label not in dates:
            dates.append(label)

    if len(dates) == 1:
        when = dates[0]
        if len(group) > 1:
            when += " (%d sittings)" % len(group)
    else:
        others = len(dates) - 1
        when = "%s +%d more date%s" % (dates[0], others, "" if others == 1 else "s")

    return "%s %s — %s, %s" % (emoji, verb, course, when)


def format_alerts(alerts):
    """One combined message for every session that needs announcing."""
    blocks = []
    for reason in ("reopened", "new", "reminder"):
        group = [session for session, r in alerts if r == reason]
        if not group:
            continue
        lines = ["<b>%s</b>" % html.escape(format_heading(reason, group))]
        # The heading already names the course; repeat it per block only when the
        # group spans more than one, so a single opening does not say it twice.
        repeat_name = len({session.name for session in group}) > 1
        for session in group:
            lines.append("")
            if repeat_name:
                lines.append("<b>%s</b>" % html.escape(session.name))
            lines.append(
                "📅 %s  ·  <code>%s</code>"
                % (html.escape(session.date_label), html.escape(session.number))
            )
            if session.location:
                lines.append("📍 %s" % html.escape(session.location))
            seats = session.seats_label
            if session.fee:
                seats += "  ·  " + session.fee
            lines.append("🎟 %s" % html.escape(seats))
            if session.undated:
                lines.append(
                    "⚠️ No machine-readable date on this listing — check it falls "
                    "on or after your cutoff before enrolling."
                )
            if session.url:
                lines.append('👉 <a href="%s">Enroll now</a>' % html.escape(session.url, quote=True))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_heartbeat(sessions, cfg):
    lines = [
        "💤 <b>%s is alive</b>" % html.escape(cfg.get("label") or "TCF watcher"),
        "",
        "Watching <b>%s</b> for test dates on or after <code>%s</code>."
        % (html.escape(cfg["name_match"]), html.escape(cfg["min_test_date"])),
    ]
    if not sessions:
        lines.append("No sessions listed in that window right now.")
    else:
        lines.append("")
        for session in sessions:
            mark = "🟢" if session.available else "🔴"
            state = session.seats_label if session.available else (session.status or "full")
            lines.append(
                "%s %s — %s (%s)"
                % (mark, html.escape(session.date_label), html.escape(state), html.escape(session.number))
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def fetch_with_retry(cfg, attempts=3):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            client = ActiveNet(cfg["keyword"])
            client.bootstrap()
            return collect(client, cfg)
        except Exception as exc:  # network blips are expected; retry a little
            last = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise last


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    parser.add_argument("--state", default=os.path.join(HERE, "state.json"))
    parser.add_argument("--min-test-date", help="override config.min_test_date")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would happen; send nothing, write nothing",
    )
    parser.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    parser.add_argument("--dump-activity", metavar="ID", help="print raw sub-activity JSON for an id")
    args = parser.parse_args(argv)

    cfg = load_json(args.config, DEFAULT_CONFIG)
    if args.min_test_date:
        cfg["min_test_date"] = args.min_test_date
    # Per-runner overrides: the Mac and the cloud job share config.json but must
    # identify themselves differently and not both shout at the same minute.
    label = os.environ.get("WATCHER_LABEL", "").strip()
    if label:
        cfg["label"] = label
    hour = os.environ.get("HEARTBEAT_HOUR_UTC", "").strip()
    if hour.isdigit():
        cfg["heartbeat_hour_utc"] = int(hour)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if args.test_telegram:
        if not token or not chat_id:
            print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
            return 2
        telegram_send(
            token, chat_id,
            "✅ <b>%s</b> is wired up correctly." % html.escape(cfg.get("label") or "TCF watcher"),
        )
        print("sent test message to chat %s" % chat_id)
        return 0

    if args.dump_activity:
        client = ActiveNet(cfg["keyword"])
        client.bootstrap()
        json.dump(client.subs(args.dump_activity), sys.stdout, indent=2)
        print()
        return 0

    state = load_json(args.state, empty_state())
    now = datetime.now(timezone.utc)

    # --- fetch ---------------------------------------------------------- #
    try:
        sessions = fetch_with_retry(cfg)
    except Exception as exc:
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        state["last_check"] = now.date().isoformat()
        message = "check failed (%d in a row): %s" % (state["consecutive_failures"], exc)
        print(message, file=sys.stderr)
        threshold = int(cfg["failure_alert_after"])
        if (
            state["consecutive_failures"] >= threshold
            and not state.get("failure_alerted")
            and token
            and chat_id
            and not args.dry_run
        ):
            telegram_send(
                token, chat_id,
                "🚨 <b>%s is failing</b>\n\n%d checks in a row failed. Silence "
                "does not mean 'no seats'.\n\n<code>%s</code>"
                % (html.escape(cfg.get("label") or "TCF watcher"),
                   state["consecutive_failures"], html.escape(str(exc))[:500]),
            )
            state["failure_alerted"] = True
        if not args.dry_run:
            save_json(args.state, state)
        return 0  # keep the workflow green so the state commit still runs

    # --- recovered? ------------------------------------------------------ #
    recovered = bool(state.get("failure_alerted"))
    state["consecutive_failures"] = 0
    state["failure_alerted"] = False
    # Date, not timestamp. state.json is committed whenever it changes, so a
    # per-run timestamp forced a commit on every single run - ~105k a year at
    # full cadence. Precise run times live in the Actions run list; all this
    # field needs to answer is "did it successfully check today".
    state["last_check"] = now.date().isoformat()

    alerts, next_sessions = decide(sessions, state, cfg, now)
    state["sessions"] = next_sessions

    # --- heartbeat ------------------------------------------------------- #
    today = now.date().isoformat()
    send_heartbeat = (
        now.hour >= int(cfg["heartbeat_hour_utc"])
        and state.get("last_heartbeat_date") != today
    )
    # --- report ---------------------------------------------------------- #
    for session in sessions:
        print(session)
    print(
        "%d session(s) in window (>= %s), %d alert(s)"
        % (len(sessions), cfg["min_test_date"], len(alerts))
    )

    if args.dry_run:
        if alerts:
            print("\n--- would send ---\n" + format_alerts(alerts))
        if send_heartbeat:
            print("\n--- would send heartbeat ---\n" + format_heartbeat(sessions, cfg))
        return 0

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset; not sending", file=sys.stderr)
        # Availability is recorded, the alert stamps are not, so these alerts are
        # raised again as soon as delivery is possible.
        save_json(args.state, state)
        return 0

    try:
        if recovered:
            telegram_send(
                token, chat_id,
                "✅ <b>%s recovered</b> — checks are succeeding again."
                % html.escape(cfg.get("label") or "TCF watcher"),
            )
        if alerts:
            telegram_send(token, chat_id, format_alerts(alerts))
        if send_heartbeat:
            telegram_send(token, chat_id, format_heartbeat(sessions, cfg))
    except Exception as exc:
        # Deliberately loud and non-zero: the alerting channel itself is broken,
        # so there is no way to tell you from inside it. A red run is the signal.
        print("could not deliver the Telegram message: %s" % exc, file=sys.stderr)
        print("state not saved; this alert retries on the next run", file=sys.stderr)
        return 1

    mark_delivered(state["sessions"], alerts, now)
    if send_heartbeat:
        state["last_heartbeat_date"] = today
    save_json(args.state, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
