from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Issue:
    """A minimal projection of a GitHub issue used by the CI ledger tooling."""

    number: int
    title: str
    body: str
    user_login: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"  # "open" | "closed"
    created_at: str = ""


@runtime_checkable
class IssueClient(Protocol):
    """Seam over GitHub issue operations. Real adapter hits the REST API;
    the test double records calls. No concrete client is imported by the
    pure ledger/selection logic."""

    def list_issues(self, state: str = "open") -> list[Issue]:
        """state in {"open", "closed", "all"}."""
        ...

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> None:
        ...

    def close_issue(self, number: int, comment: str | None = None) -> None:
        ...

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """Returns the new issue number."""
        ...
