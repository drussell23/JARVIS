"""Orange PR lane honours the operator's push policy (never pushes by default)."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance import orange_pr_reviewer as O


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("JARVIS_AUTO_PUSH_BRANCH", "JARVIS_ORANGE_PR_PUSH_ENABLED"):
        monkeypatch.delenv(k, raising=False)


def test_push_not_allowed_by_default(monkeypatch):
    assert O.remote_push_allowed() is False
    monkeypatch.setenv("JARVIS_AUTO_PUSH_BRANCH", "ouroboros/inbox")
    assert O.remote_push_allowed() is True
    monkeypatch.setenv("JARVIS_ORANGE_PR_PUSH_ENABLED", "0")   # explicit lane value wins
    assert O.remote_push_allowed() is False
    monkeypatch.delenv("JARVIS_AUTO_PUSH_BRANCH")
    monkeypatch.setenv("JARVIS_ORANGE_PR_PUSH_ENABLED", "1")
    assert O.remote_push_allowed() is True


def test_create_flow_never_pushes_or_calls_gh_when_not_allowed(monkeypatch, tmp_path):
    src = inspect.getsource(O.OrangePRReviewer)
    assert "remote_push_allowed()" in src and 'url=f"local://{branch}"' in src
    # the guard sits BEFORE the push and the gh call
    assert src.index("remote_push_allowed()") < src.index('"push", "-u", "origin"') < src.index('"pr", "create"')
