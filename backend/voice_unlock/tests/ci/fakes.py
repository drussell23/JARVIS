from __future__ import annotations

from dataclasses import replace

from backend.voice_unlock.ci.issue_client import Issue


class FakeIssueClient:
    """In-memory IssueClient test double. Records mutating calls so tests
    can assert on them."""

    def __init__(self, issues: list[Issue] | None = None) -> None:
        self.issues: dict[int, Issue] = {i.number: i for i in (issues or [])}
        self.closed: list[tuple[int, str | None]] = []
        self.created: list[int] = []
        self._next_number = (max(self.issues, default=0)) + 1000

    def list_issues(self, state: str = "open") -> list[Issue]:
        if state == "all":
            return list(self.issues.values())
        return [i for i in self.issues.values() if i.state == state]

    def update_issue(self, number, *, title=None, body=None, labels=None, state=None) -> None:
        cur = self.issues[number]
        self.issues[number] = replace(
            cur,
            title=cur.title if title is None else title,
            body=cur.body if body is None else body,
            labels=cur.labels if labels is None else labels,
            state=cur.state if state is None else state,
        )

    def close_issue(self, number, comment=None) -> None:
        self.closed.append((number, comment))
        if number in self.issues:
            self.issues[number] = replace(self.issues[number], state="closed")

    def create_issue(self, title, body, labels) -> int:
        number = self._next_number
        self._next_number += 1
        self.issues[number] = Issue(
            number=number, title=title, body=body,
            user_login="ci-bot", labels=list(labels), state="open",
            created_at="2026-06-14T00:00:00Z",
        )
        self.created.append(number)
        return number
