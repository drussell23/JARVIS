from __future__ import annotations

from backend.voice_unlock.ci.issue_client import Issue
from backend.voice_unlock.ci import ledger
from backend.voice_unlock.ci.purge_phantom_issues import run_purge
from backend.voice_unlock.tests.ci.fakes import FakeIssueClient


def _phantom(number: int) -> Issue:
    return Issue(number=number, title=ledger.PHANTOM_TITLE, body="auto",
                 user_login="github-actions",
                 labels=["bug", "critical", "unlock", "automated-test"],
                 state="open", created_at=f"2026-05-{number:02d}T00:00:00Z")


def test_dry_run_changes_nothing_but_reports_plan():
    client = FakeIssueClient([_phantom(1), _phantom(9), _phantom(4)])
    result = run_purge(client, execute=False, timestamp_iso="2026-06-14T00:00:00Z")
    assert result.ledger_number == 9
    assert sorted(result.closed_numbers) == [1, 4]
    assert client.closed == []
    assert all(i.state == "open" for i in client.issues.values())
    assert client.issues[9].title == ledger.PHANTOM_TITLE


def test_execute_converts_newest_and_closes_rest():
    client = FakeIssueClient([_phantom(1), _phantom(9), _phantom(4)])
    result = run_purge(client, execute=True, timestamp_iso="2026-06-14T00:00:00Z")
    assert result.ledger_number == 9
    assert client.issues[9].title == ledger.LEDGER_TITLE
    assert ledger.LEDGER_LABEL in client.issues[9].labels
    assert ledger.LEDGER_MARKER in client.issues[9].body
    assert {n for n, _ in client.closed} == {1, 4}
    assert client.issues[1].state == "closed" and client.issues[4].state == "closed"


def test_execute_is_idempotent_second_run_noop():
    client = FakeIssueClient([_phantom(1), _phantom(9), _phantom(4)])
    run_purge(client, execute=True, timestamp_iso="2026-06-14T00:00:00Z")
    client.closed.clear()
    result2 = run_purge(client, execute=True, timestamp_iso="2026-06-14T00:00:00Z")
    assert result2.ledger_number == 9
    assert result2.closed_numbers == []
    assert client.closed == []
