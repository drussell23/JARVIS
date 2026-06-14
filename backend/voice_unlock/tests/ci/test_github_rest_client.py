from __future__ import annotations

import json

from backend.voice_unlock.ci.github_rest_client import GitHubRestClient


class _RecordingOpener:
    """Captures Requests and returns canned JSON, so no network is touched."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (method, url, headers, payload)

    def __call__(self, request):
        payload = None
        if request.data:
            payload = json.loads(request.data.decode("utf-8"))
        self.calls.append((request.get_method(), request.full_url, dict(request.headers), payload))
        return self._responses.pop(0)


def test_list_issues_builds_paginated_get_and_parses():
    page1 = [{"number": 1, "title": "a", "body": "", "user": {"login": "github-actions"},
              "labels": [{"name": "unlock"}], "state": "open", "created_at": "2026-05-01T00:00:00Z"}]
    opener = _RecordingOpener([page1, []])
    client = GitHubRestClient(token="tok", repo="o/r", opener=opener)
    issues = client.list_issues(state="open")
    assert [i.number for i in issues] == [1]
    assert issues[0].user_login == "github-actions"
    assert issues[0].labels == ["unlock"]
    method, url, headers, _ = opener.calls[0]
    assert method == "GET"
    assert "/repos/o/r/issues" in url and "state=open" in url
    assert headers["Authorization"] == "Bearer tok"


def test_close_issue_posts_comment_then_patches_state():
    opener = _RecordingOpener([{}, {}])
    client = GitHubRestClient(token="tok", repo="o/r", opener=opener)
    client.close_issue(5, comment="superseded by ledger")
    methods = [c[0] for c in opener.calls]
    urls = [c[1] for c in opener.calls]
    assert methods == ["POST", "PATCH"]
    assert "/issues/5/comments" in urls[0]
    assert "/issues/5" in urls[1]
    assert opener.calls[1][3] == {"state": "closed"}


def test_create_issue_posts_and_returns_number():
    opener = _RecordingOpener([{"number": 321}])
    client = GitHubRestClient(token="tok", repo="o/r", opener=opener)
    num = client.create_issue("title", "body", ["unlock-ci-ledger"])
    assert num == 321
    method, url, _, payload = opener.calls[0]
    assert method == "POST" and url.endswith("/repos/o/r/issues")
    assert payload == {"title": "title", "body": "body", "labels": ["unlock-ci-ledger"]}


import http.client
import pytest
from backend.voice_unlock.ci.github_rest_client import _read_with_retry


def test_read_with_retry_recovers_from_transient_incomplete_read():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.IncompleteRead(b"partial")
        return [{"ok": True}]

    out = _read_with_retry(flaky, idempotent=True, attempts=4, sleep=lambda s: None)
    assert out == [{"ok": True}]
    assert calls["n"] == 3


def test_read_with_retry_does_not_retry_non_idempotent():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise http.client.IncompleteRead(b"partial")

    with pytest.raises(http.client.IncompleteRead):
        _read_with_retry(flaky, idempotent=False, attempts=4, sleep=lambda s: None)
    assert calls["n"] == 1  # mutations must never be retried


def test_read_with_retry_raises_after_exhausting_attempts():
    def always_fail():
        raise http.client.IncompleteRead(b"partial")

    with pytest.raises(http.client.IncompleteRead):
        _read_with_retry(always_fail, idempotent=True, attempts=3, sleep=lambda s: None)


def test_list_issues_does_not_stop_on_short_non_final_page():
    # GitHub may return a page SHORTER than per_page that is NOT the last page.
    # Pagination must continue until an EMPTY page, or items silently drop
    # (this is the bug that left issue #58863 open after the first purge).
    def issue(n):
        return {"number": n, "title": "t", "body": "", "user": {"login": "u"},
                "labels": [], "state": "open", "created_at": "2026-01-01T00:00:00Z"}
    opener = _RecordingOpener([[issue(1)], [issue(2)], []])  # short, then more, then empty
    client = GitHubRestClient(token="tok", repo="o/r", opener=opener)
    issues = client.list_issues(state="open")
    assert [i.number for i in issues] == [1, 2]
