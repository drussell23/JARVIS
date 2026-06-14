from __future__ import annotations

from backend.voice_unlock.ci.issue_client import Issue
from backend.voice_unlock.ci import ledger
from backend.voice_unlock.ci.ledger_reporter import LedgerContext, find_or_update_ledger
from backend.voice_unlock.tests.ci.fakes import FakeIssueClient


def _ctx(status="failure"):
    return LedgerContext(
        run_url="https://github.com/o/r/actions/runs/77",
        timestamp_iso="2026-06-14T04:00:00Z",
        status=status,
        detail="Track A logic failure",
    )


def test_creates_ledger_when_absent():
    client = FakeIssueClient([])
    number = find_or_update_ledger(client, _ctx())
    assert number in client.issues
    created = client.issues[number]
    assert created.title == ledger.LEDGER_TITLE
    assert ledger.LEDGER_LABEL in created.labels
    assert ledger.LEDGER_MARKER in created.body
    assert len(client.created) == 1


def test_updates_existing_open_ledger_without_creating():
    existing = Issue(number=42, title=ledger.LEDGER_TITLE, body=ledger.LEDGER_MARKER,
                     user_login="ci-bot", labels=[ledger.LEDGER_LABEL],
                     state="open", created_at="2026-06-10T00:00:00Z")
    client = FakeIssueClient([existing])
    number = find_or_update_ledger(client, _ctx())
    assert number == 42
    assert client.created == []
    assert "runs/77" in client.issues[42].body


def test_reopens_closed_ledger_on_new_failure():
    closed = Issue(number=42, title=ledger.LEDGER_TITLE, body=ledger.LEDGER_MARKER,
                   user_login="ci-bot", labels=[ledger.LEDGER_LABEL],
                   state="closed", created_at="2026-06-10T00:00:00Z")
    client = FakeIssueClient([closed])
    find_or_update_ledger(client, _ctx(status="failure"))
    assert client.issues[42].state == "open"
    assert client.created == []


def test_finds_ledger_by_marker_even_without_label():
    marked = Issue(number=7, title="anything", body=f"x {ledger.LEDGER_MARKER} y",
                   user_login="ci-bot", labels=[], state="open",
                   created_at="2026-06-10T00:00:00Z")
    client = FakeIssueClient([marked])
    number = find_or_update_ledger(client, _ctx())
    assert number == 7
    assert client.created == []


from backend.voice_unlock.ci.ledger_reporter import context_from_env


def test_context_from_env_builds_run_url_and_status():
    env = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "555",
        "LEDGER_STATUS": "failure",
        "LEDGER_DETAIL": "Track A reject-case regression",
    }
    ctx = context_from_env(env, now_iso="2026-06-14T04:00:00Z")
    assert ctx.run_url == "https://github.com/o/r/actions/runs/555"
    assert ctx.status == "failure"
    assert ctx.detail == "Track A reject-case regression"
    assert ctx.timestamp_iso == "2026-06-14T04:00:00Z"


def test_context_from_env_defaults_status_passing():
    ctx = context_from_env({"GITHUB_RUN_ID": "1"}, now_iso="2026-06-14T04:00:00Z")
    assert ctx.status == "passing"


def test_passing_status_does_not_reopen_closed_ledger():
    # A passing run must NOT resurrect a closed ledger (only failures reopen).
    closed = Issue(number=42, title=ledger.LEDGER_TITLE, body=ledger.LEDGER_MARKER,
                   user_login="ci-bot", labels=[ledger.LEDGER_LABEL],
                   state="closed", created_at="2026-06-10T00:00:00Z")
    client = FakeIssueClient([closed])
    number = find_or_update_ledger(client, _ctx(status="passing"))
    assert number == 42
    assert client.issues[42].state == "closed"
    assert client.created == []
