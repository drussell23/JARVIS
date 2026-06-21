"""Sovereign GitOps Identity Matrix — graduation workspace tests (2026-06-21).

Autonomous, authorized, branch-capable git workspace for the [SOVEREIGN GRADUATION]
PR proposer — no manual git clone / git config."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.graduation.graduation_workspace import (
    authed_remote_url,
    ensure_clean_workspace,
    git_identity_ready,
    workspace_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("JARVIS_GRADUATION_WORKSPACE_ENABLED", "JARVIS_GRADUATION_GIT_REMOTE",
              "GH_TOKEN", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
              "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)


def test_enabled_default_true():
    assert workspace_enabled() is True


def test_enabled_off(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_WORKSPACE_ENABLED", "0")
    assert workspace_enabled() is False


def test_authed_remote_from_bare(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("JARVIS_GRADUATION_GIT_REMOTE", "github.com/owner/repo.git")
    assert authed_remote_url() == "https://x-access-token:tok@github.com/owner/repo.git"


def test_authed_remote_from_full_https(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("JARVIS_GRADUATION_GIT_REMOTE", "https://github.com/owner/repo.git")
    assert authed_remote_url() == "https://x-access-token:tok@github.com/owner/repo.git"


def test_authed_remote_strips_existing_creds(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("JARVIS_GRADUATION_GIT_REMOTE", "x-access-token:old@github.com/owner/repo.git")
    assert authed_remote_url() == "https://x-access-token:tok@github.com/owner/repo.git"


def test_authed_remote_empty_without_token(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_GIT_REMOTE", "github.com/owner/repo.git")
    assert authed_remote_url() == ""


def test_authed_remote_empty_without_remote(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    assert authed_remote_url() == ""


def test_identity_ready_true(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ouroboros")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "o@jarvis.ai")
    assert git_identity_ready() is True


def test_identity_ready_false_when_absent():
    assert git_identity_ready() is False


def test_ensure_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_WORKSPACE_ENABLED", "0")
    ok, detail = ensure_clean_workspace("/whatever")
    assert ok is True and detail == "disabled"


def test_ensure_no_repo_root():
    ok, detail = ensure_clean_workspace("")
    assert ok is False and detail == "no_repo_root"


def test_ensure_fails_without_remote_or_token(monkeypatch):
    # enabled but no remote/token → structured fail, NEVER raises
    ok, detail = ensure_clean_workspace("/tmp/some_ws")
    assert ok is False and detail == "remote_or_token_unset"
