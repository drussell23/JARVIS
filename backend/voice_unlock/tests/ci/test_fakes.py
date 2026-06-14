from __future__ import annotations

from backend.voice_unlock.ci.issue_client import Issue
from backend.voice_unlock.tests.ci.fakes import FakeIssueClient


def _issue(number: int, **kw) -> Issue:
    base = dict(
        number=number, title="t", body="b", user_login="github-actions",
        labels=["unlock"], state="open", created_at="2026-06-01T00:00:00Z",
    )
    base.update(kw)
    return Issue(**base)


def test_fake_lists_filters_by_state():
    client = FakeIssueClient([_issue(1, state="open"), _issue(2, state="closed")])
    assert {i.number for i in client.list_issues(state="open")} == {1}
    assert {i.number for i in client.list_issues(state="all")} == {1, 2}


def test_fake_update_close_create_record_calls():
    client = FakeIssueClient([_issue(1)])
    client.update_issue(1, body="new", state="open")
    client.close_issue(2, comment="dup")
    new_num = client.create_issue("title", "body", ["unlock-ci-ledger"])
    assert client.issues[1].body == "new"
    assert client.closed == [(2, "dup")]
    assert client.created and client.issues[new_num].title == "title"
