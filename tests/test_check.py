"""Tests over payloads captured verbatim from the live ActiveNet API.

tests/fixtures/sessions.json holds one real activity item per status the site was
observed to emit, so the classifier is exercised against the actual shapes rather
than hand-written guesses.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import check  # noqa: E402

FIXTURES = json.load(
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sessions.json"))
)

CFG = dict(check.DEFAULT_CONFIG, name_match="E-TCF CANADA", min_test_date="2026-09-14")
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def s(key):
    return check.Session(FIXTURES[key])


# --------------------------- classification --------------------------------- #


def test_full_session_is_not_available():
    full = s("Full")
    assert full.status == "Full"
    assert (full.enrolled, full.capacity) == (5, 5)
    assert full.spots == 0
    assert full.available is False


def test_open_session_is_available_with_seat_count():
    open_one = s("OPEN")
    assert open_one.status == ""
    assert open_one.spots == open_one.capacity - open_one.enrolled > 0
    assert open_one.available is True
    assert "spots left" in open_one.seats_label


def test_ended_closed_and_in_progress_are_blocked():
    for key in ("Ended", "Closed", "In progress"):
        assert s(key).available is False, key


def test_starting_soon_stays_bookable():
    # "Starting soon" describes timing, not capacity - it must not suppress an alert.
    session = s("Starting soon")
    assert session.status == "Starting soon"
    assert session.available is True


def test_unlimited_capacity_has_no_seat_maths():
    unlimited = s("UNLIMITED")
    assert unlimited.capacity == check.UNLIMITED
    assert unlimited.spots is None
    assert unlimited.available is True
    assert unlimited.seats_label == "unlimited spots"


def test_zero_seats_left_without_a_full_tag_is_still_unavailable():
    raw = dict(FIXTURES["OPEN"], total_open=6, already_enrolled=6)
    raw["urgent_message"] = {"status_description": ""}
    assert check.Session(raw).available is False


def test_undated_sessions_are_flagged_not_dropped():
    assert s("Ended").undated is True
    assert s("OPEN").undated is False


# ------------------------------- filtering ---------------------------------- #


class FakeClient:
    """Stands in for ActiveNet: one parent with the given sub-activities."""

    def __init__(self, subs, name="E-TCF CANADA - 4 modules"):
        self._subs = subs
        self._name = name

    def search(self):
        return [{"id": 1, "name": self._name, "num_of_sub_activities": len(self._subs)}]

    def subs(self, parent_id):
        return self._subs


def test_cutoff_excludes_earlier_test_dates():
    dated = dict(FIXTURES["OPEN"])  # 2026-11-02
    cfg = dict(CFG, min_test_date="2026-12-01")
    assert check.collect(FakeClient([dated]), cfg) == []

    cfg = dict(CFG, min_test_date="2026-11-02")  # boundary is inclusive
    assert len(check.collect(FakeClient([dated]), cfg)) == 1


def test_name_filter_excludes_other_courses():
    client = FakeClient([dict(FIXTURES["OPEN"])], name="TCF Preparation")
    assert check.collect(client, CFG) == []


def test_undated_session_survives_the_cutoff_when_enabled():
    undated = dict(FIXTURES["Starting soon"])
    assert len(check.collect(FakeClient([undated]), dict(CFG, alert_undated=True))) == 1
    assert check.collect(FakeClient([undated]), dict(CFG, alert_undated=False)) == []


def test_activity_without_subs_is_treated_as_its_own_session():
    flat = dict(FIXTURES["OPEN"], name="E-TCF CANADA - 4 modules")
    flat["num_of_sub_activities"] = 0

    class Flat(FakeClient):
        def search(self):
            return [flat]

        def subs(self, parent_id):  # must never be called
            raise AssertionError("subs() should not be called for a flat activity")

    assert len(check.collect(Flat([]), CFG)) == 1


# ---------------------------- alert decisions ------------------------------- #


def state_with(session_id, available, last_alert=None):
    return {
        "sessions": {
            session_id: {"available": available, "last_alert": last_alert}
        }
    }


def test_new_session_alerts_once():
    session = s("OPEN")
    alerts, next_state = check.decide([session], {"sessions": {}}, CFG, NOW)
    assert [r for _, r in alerts] == ["new"]
    assert next_state[session.id]["last_alert"] == NOW.isoformat()


def test_full_to_open_transition_alerts():
    session = s("OPEN")
    alerts, _ = check.decide([session], state_with(session.id, False), CFG, NOW)
    assert [r for _, r in alerts] == ["reopened"]


def test_no_repeat_alert_while_it_stays_open():
    session = s("OPEN")
    prior = state_with(session.id, True, last_alert=(NOW - timedelta(minutes=20)).isoformat())
    alerts, _ = check.decide([session], prior, CFG, NOW)
    assert alerts == []


def test_reminder_after_the_configured_gap():
    session = s("OPEN")
    prior = state_with(session.id, True, last_alert=(NOW - timedelta(hours=7)).isoformat())
    alerts, _ = check.decide([session], prior, dict(CFG, reminder_hours=6), NOW)
    assert [r for _, r in alerts] == ["reminder"]


def test_full_session_clears_its_alert_timestamp_so_reopening_fires():
    full = s("Full")
    prior = state_with(full.id, True, last_alert=NOW.isoformat())
    alerts, next_state = check.decide([full], prior, CFG, NOW)
    assert alerts == []
    assert next_state[full.id]["available"] is False
    assert next_state[full.id]["last_alert"] is None


# ------------------------------- messages ----------------------------------- #


def test_message_contains_the_actionable_details():
    session = s("OPEN")
    text = check.format_alerts([(session, "reopened")])
    assert session.number in text
    assert session.date_label in text
    assert "spots left" in text
    assert session.url in text.replace("&amp;", "&")


def test_undated_message_carries_the_warning():
    text = check.format_alerts([(s("Starting soon"), "new")])
    assert "No machine-readable date" in text


def test_one_message_covers_every_session():
    text = check.format_alerts([(s("OPEN"), "new"), (s("Starting soon"), "new")])
    assert text.count("🎟") == 2


def test_ampersands_in_urls_are_escaped_for_telegram_html():
    text = check.format_alerts([(s("OPEN"), "new")])
    assert "wishlist_id=0&amp;locale" in text or "&" not in s("OPEN").url


# --------------------------- failure handling ------------------------------- #


def _paths(tmp_path, **cfg_overrides):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(dict(CFG, **cfg_overrides)))
    state = tmp_path / "state.json"
    state.write_text(json.dumps(check.empty_state()))
    return ["--config", str(cfg), "--state", str(state)], state


def _boom(cfg, attempts=3):
    raise RuntimeError("network is down")


def test_failures_alert_once_at_the_threshold_then_stay_quiet(tmp_path, monkeypatch):
    argv, state = _paths(tmp_path, failure_alert_after=2)
    sent = []
    monkeypatch.setattr(check, "fetch_with_retry", _boom)
    monkeypatch.setattr(check, "telegram_send", lambda t, c, text, **kw: sent.append(text))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")

    assert check.main(argv) == 0            # 1st failure: below threshold
    assert sent == []
    assert check.main(argv) == 0            # 2nd: fires
    assert len(sent) == 1 and "failing" in sent[0]
    assert check.main(argv) == 0            # 3rd: no repeat spam
    assert len(sent) == 1
    assert json.loads(state.read_text())["consecutive_failures"] == 3


def test_exit_code_stays_zero_so_the_state_commit_still_runs(tmp_path, monkeypatch):
    argv, _ = _paths(tmp_path)
    monkeypatch.setattr(check, "fetch_with_retry", _boom)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert check.main(argv) == 0


def test_recovery_is_announced_after_a_failure_alert(tmp_path, monkeypatch):
    argv, state = _paths(tmp_path, failure_alert_after=1)
    sent = []
    monkeypatch.setattr(check, "telegram_send", lambda t, c, text, **kw: sent.append(text))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")

    monkeypatch.setattr(check, "fetch_with_retry", _boom)
    check.main(argv)
    assert "failing" in sent[0]

    monkeypatch.setattr(check, "fetch_with_retry", lambda cfg, attempts=3: [])
    check.main(argv)
    assert any("recovered" in text for text in sent)
    saved = json.loads(state.read_text())
    assert saved["consecutive_failures"] == 0 and saved["failure_alerted"] is False


def test_dry_run_never_sends_or_writes(tmp_path, monkeypatch):
    argv, state = _paths(tmp_path)
    before = state.read_text()
    monkeypatch.setattr(check, "fetch_with_retry", lambda cfg, attempts=3: [s("OPEN")])
    monkeypatch.setattr(
        check, "telegram_send",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run must not send")),
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    assert check.main(argv + ["--dry-run"]) == 0
    assert state.read_text() == before


# ------------------------ parent containers are not seats -------------------- #
#
# The listing shows "E-TCF CANADA - 4 modules" twice: once as a collapsed
# container and once as the real dated sitting inside it, which is the row that
# carries the price and the Enroll link. Only the inner one is bookable.

CONTAINER = {
    "id": 129936,
    "name": "E-TCF CANADA - 4 modules",
    "parent_activity": True,
    "num_of_sub_activities": 2,
    "date_range_start": "",
    "date_range": "",
    "total_open": 6,
    "already_enrolled": 3,
    "urgent_message": {"status_description": ""},
    "fee": {"label": "$400.00"},
}


def test_container_row_is_never_reported_as_a_session():
    inner = dict(FIXTURES["OPEN"], name="E-TCF CANADA - 4 modules")

    class Listing:
        def search(self):
            return [dict(CONTAINER)]

        def subs(self, parent_id):
            return [inner]

    got = check.collect(Listing(), CFG)
    assert [x.id for x in got] == [str(inner["id"])]


def test_container_claiming_zero_subs_is_still_expanded_not_booked():
    # Real shape on this site: E-TEF CANADA rows report parent_activity=True
    # alongside num_of_sub_activities=0. Reading the count alone turned the
    # container itself into a dateless "available" session.
    empty = dict(CONTAINER, num_of_sub_activities=0)

    class Listing:
        def __init__(self):
            self.expanded = False

        def search(self):
            return [empty]

        def subs(self, parent_id):
            self.expanded = True
            return []

    listing = Listing()
    assert check.collect(listing, CFG) == []
    assert listing.expanded, "the container must be expanded, not treated as a seat"


def test_a_container_nested_in_sub_results_is_skipped():
    class Listing:
        def search(self):
            return [dict(CONTAINER)]

        def subs(self, parent_id):
            return [dict(CONTAINER, id=999), dict(FIXTURES["OPEN"])]

    got = check.collect(Listing(), CFG)
    assert [x.id for x in got] == [str(FIXTURES["OPEN"]["id"])]


def test_genuinely_flat_activity_is_still_booked_directly():
    flat = dict(FIXTURES["OPEN"], name="E-TCF CANADA - 4 modules")
    flat["parent_activity"] = False
    flat["num_of_sub_activities"] = 0

    class Listing:
        def search(self):
            return [flat]

        def subs(self, parent_id):
            raise AssertionError("a flat activity must not be expanded")

    assert len(check.collect(Listing(), CFG)) == 1
