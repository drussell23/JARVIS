"""Orange PR lane honours the operator's push policy (never pushes by default)."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance import orange_pr_reviewer as O


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("JARVIS_AUTO_PUSH_BRANCH", "JARVIS_ORANGE_PR_PUSH_ENABLED",
              "JARVIS_REMOTE_PUSH_AIRGAP"):
        monkeypatch.delenv(k, raising=False)


def test_push_not_allowed_by_default():
    assert O.remote_push_allowed() is False


def test_the_airgap_outranks_every_lane_declaration(monkeypatch):
    """2026-09-08: the lane's declarations are no longer sufficient on their
    own. The air-gap (``remote_push_guard``) is consulted first and nothing
    here lifts it — which is the whole reason it exists, since these very
    declarations were what let 22 branches reach origin."""
    monkeypatch.setenv("JARVIS_AUTO_PUSH_BRANCH", "ouroboros/inbox")
    assert O.remote_push_allowed() is False
    monkeypatch.setenv("JARVIS_ORANGE_PR_PUSH_ENABLED", "1")
    assert O.remote_push_allowed() is False


def test_lane_composition_still_holds_once_the_airgap_is_down(monkeypatch):
    """With the operator's explicit ``AIRGAP=false``, the original lane
    precedence is unchanged: a declared branch allows, an explicit lane value
    wins over it either way."""
    monkeypatch.setenv("JARVIS_REMOTE_PUSH_AIRGAP", "false")
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
