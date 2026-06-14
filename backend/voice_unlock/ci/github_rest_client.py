from __future__ import annotations

import http.client
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from backend.voice_unlock.ci.issue_client import Issue

_API_ROOT = "https://api.github.com"
# Page size for collection endpoints. Deliberately modest: large GitHub issue
# pages (100 issues × full bodies ≈ 390 KB) intermittently truncate mid-read on
# some networks/TLS stacks, raising http.client.IncompleteRead. Smaller pages
# keep each response well under that fragile threshold.
_PER_PAGE = 50
# Runaway guard for pagination — far above any real repo's issue count, never an
# expected stopping point (an empty page stops the loop first).
_MAX_PAGES = 200

# Transient transport failures worth retrying for idempotent reads. IncompleteRead
# is the observed failure (body delivered short of Content-Length); the rest cover
# dropped/reset/timed-out connections. NOT included: urllib.error.HTTPError — a 4xx
# is a real answer, not a blip.
_TRANSIENT_READ_ERRORS = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionError,
    TimeoutError,
)

# The opener returns decoded GitHub JSON — a list[dict] for collection endpoints,
# a dict for single-resource endpoints. Genuinely dynamic, so typed as Any.
Opener = Callable[[urllib.request.Request], Any]


def _read_with_retry(do_call: Callable[[], Any], *, idempotent: bool,
                     attempts: int = 4, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Call ``do_call()``, retrying transient transport failures for idempotent
    requests with exponential backoff. Non-idempotent requests (POST/PATCH) are
    never retried — a retry of a mutation that actually landed server-side but
    failed on read would double-apply it."""
    for n in range(attempts):
        try:
            return do_call()
        except _TRANSIENT_READ_ERRORS:
            if not idempotent or n == attempts - 1:
                raise
            sleep(min(0.5 * (2 ** n), 4.0))
    raise RuntimeError("unreachable")  # pragma: no cover


def _default_opener(request: urllib.request.Request) -> Any:
    idempotent = request.get_method() == "GET"

    def _call() -> Any:
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310 (trusted host)
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    return _read_with_retry(_call, idempotent=idempotent)


class GitHubRestClient:
    """Production IssueClient over GitHub REST v3. The HTTP `opener` is injected
    so the request/response shaping is unit-testable without network."""

    def __init__(self, *, token: str, repo: str, opener: Opener | None = None) -> None:
        self._token = token
        self._repo = repo  # "owner/name"
        self._opener = opener or _default_opener

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{_API_ROOT}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url=url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "jarvis-unlock-ci-ledger")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return self._opener(req)

    @staticmethod
    def _to_issue(raw: dict) -> Issue:
        return Issue(
            number=raw["number"],
            title=raw.get("title", ""),
            body=raw.get("body") or "",
            user_login=(raw.get("user") or {}).get("login", ""),
            labels=[l["name"] for l in raw.get("labels", [])],
            state=raw.get("state", "open"),
            created_at=raw.get("created_at", ""),
        )

    def list_issues(self, state: str = "open") -> list[Issue]:
        issues: list[Issue] = []
        # Page until an EMPTY page. We deliberately do NOT stop on a short page:
        # GitHub may return fewer than per_page items on a non-final page, and a
        # `len(batch) < per_page` break would silently drop everything after it.
        # _MAX_PAGES is a runaway guard, not an expected stopping point.
        for page in range(1, _MAX_PAGES + 1):
            qs = urllib.parse.urlencode({"state": state, "per_page": _PER_PAGE, "page": page})
            batch = self._request("GET", f"/repos/{self._repo}/issues?{qs}")
            if not batch:
                break
            for raw in batch:
                if "pull_request" in raw:
                    continue
                issues.append(self._to_issue(raw))
        return issues

    def update_issue(self, number, *, title=None, body=None, labels=None, state=None) -> None:
        payload: dict = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if state is not None:
            payload["state"] = state
        if payload:
            self._request("PATCH", f"/repos/{self._repo}/issues/{number}", payload)

    def close_issue(self, number, comment=None) -> None:
        if comment:
            self._request("POST", f"/repos/{self._repo}/issues/{number}/comments", {"body": comment})
        self._request("PATCH", f"/repos/{self._repo}/issues/{number}", {"state": "closed"})

    def create_issue(self, title, body, labels) -> int:
        raw = self._request("POST", f"/repos/{self._repo}/issues",
                            {"title": title, "body": body, "labels": list(labels)})
        return raw["number"]
