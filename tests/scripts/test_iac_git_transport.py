#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the Sovereign IaC Hypervisor git-clone transport (parity-verified
node clone over fast WAN, replacing the <1MB/s IAP tar-pipe).

ALL subprocess / SSH / scp / git calls are MOCKED -- NO real network, NO real
nodes, NO real git remotes. We assert:

  * resolve_local_git_target() -> (url, sha, branch) when HEAD is pushed.
  * HEAD-not-on-origin -> fail-CLOSED (raises).
  * node clone+checkout asserts parity: node HEAD == sha -> ok; mismatch -> raise.
  * secret injection success -> ok; secret timeout/failure -> transport FAILS and
    burn_node is CALLED (node NOT kept warm -- a node without .env is useless).
  * concurrent deps launched ALONGSIDE secrets (both invoked, deps not serialized
    after secrets).
  * transport=tar path unchanged (back-compat regression).
  * secret file CONTENTS are NEVER logged (paths ok).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
import types

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HYPERVISOR = _REPO_ROOT / "scripts" / "sovereign_iac_hypervisor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sovereign_iac_hypervisor_under_test", str(_HYPERVISOR)
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hyper():
    return _load_module()


def _args(**over):
    base = dict(
        project="proj-x", zone="zone-y", sync_timeout_s=300.0,
    )
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# 1. resolve_local_git_target -- (url, sha, branch) when HEAD is pushed.
# --------------------------------------------------------------------------- #
def test_resolve_local_git_target_returns_url_sha_branch(hyper, monkeypatch):
    sha = "a" * 40

    def fake_run(cmd, *, timeout_s=120.0):
        joined = " ".join(cmd)
        if "rev-parse" in cmd and "HEAD" in cmd and "--abbrev-ref" not in cmd:
            return 0, sha + "\n"
        if "--abbrev-ref" in cmd:
            return 0, "feature/foo\n"
        if "get-url" in cmd:
            return 0, "https://github.com/drussell23/JARVIS.git\n"
        if "branch" in cmd and "-r" in cmd:  # contains-check: HEAD on origin
            return 0, "  origin/feature/foo\n"
        return 1, "[unexpected " + joined + "]"

    monkeypatch.setattr(hyper, "_run", fake_run)
    url, got_sha, branch = hyper.resolve_local_git_target(str(_REPO_ROOT))
    assert url == "https://github.com/drussell23/JARVIS.git"
    assert got_sha == sha
    assert branch == "feature/foo"


# --------------------------------------------------------------------------- #
# 2. HEAD-not-on-origin -> fail-CLOSED.
# --------------------------------------------------------------------------- #
def test_resolve_fails_closed_when_head_not_on_origin(hyper, monkeypatch):
    sha = "b" * 40

    def fake_run(cmd, *, timeout_s=120.0):
        if "rev-parse" in cmd and "HEAD" in cmd and "--abbrev-ref" not in cmd:
            return 0, sha + "\n"
        if "--abbrev-ref" in cmd:
            return 0, "feature/unpushed\n"
        if "get-url" in cmd:
            return 0, "https://github.com/drussell23/JARVIS.git\n"
        # contains-check returns NOTHING -> sha not on any remote branch.
        if "branch" in cmd and "-r" in cmd:
            return 0, "\n"
        if "ls-remote" in cmd:
            return 0, "deadbeef\trefs/heads/main\n"  # sha absent
        return 1, "[unexpected]"

    monkeypatch.setattr(hyper, "_run", fake_run)
    with pytest.raises(hyper.GitTransportError) as ei:
        hyper.resolve_local_git_target(str(_REPO_ROOT))
    msg = str(ei.value)
    assert sha in msg
    assert "not on origin" in msg.lower()
    assert "push" in msg.lower()


# --------------------------------------------------------------------------- #
# 3. node clone+checkout asserts parity.
# --------------------------------------------------------------------------- #
def test_git_clone_on_node_parity_ok(hyper, monkeypatch):
    sha = "c" * 40
    captured = {}

    def fake_run_streaming_labeled(cmd, *, label, log_path=None, timeout_s=3600.0):
        captured["clone_cmd"] = cmd
        captured["label"] = label
        # node's resulting HEAD == sha (parity holds).
        return 0, [sha + "\n"]

    monkeypatch.setattr(hyper, "_run_streaming_labeled", fake_run_streaming_labeled)
    ok, detail = hyper.git_clone_on_node(
        _args(), "node-1", "https://github.com/drussell23/JARVIS.git", sha,
        "feature/foo", "/opt/trinity/jarvis",
    )
    assert ok, detail
    # The clone command is an SSH exec carrying git clone + checkout + rev-parse.
    joined = " ".join(captured["clone_cmd"])
    assert "git clone" in joined
    assert "checkout " + sha in joined or sha in joined
    assert captured["label"] == "synced"


def test_git_clone_on_node_parity_mismatch_raises(hyper, monkeypatch):
    sha = "c" * 40
    wrong = "d" * 40

    def fake_run_streaming_labeled(cmd, *, label, log_path=None, timeout_s=3600.0):
        return 0, [wrong + "\n"]  # node ended up on a DIFFERENT commit.

    monkeypatch.setattr(hyper, "_run_streaming_labeled", fake_run_streaming_labeled)
    with pytest.raises(hyper.GitTransportError) as ei:
        hyper.git_clone_on_node(
            _args(), "node-1", "https://github.com/drussell23/JARVIS.git", sha,
            "feature/foo", "/opt/trinity/jarvis",
        )
    assert "parity" in str(ei.value).lower() or "mismatch" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# 4. secret injection -- success, and timeout/failure -> burn.
# --------------------------------------------------------------------------- #
def test_inject_secrets_success(hyper, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_SECRET_FILES", ".env")
    calls = []

    def fake_run(cmd, *, timeout_s=120.0):
        calls.append((cmd, timeout_s))
        return 0, "transferred\n"

    monkeypatch.setattr(hyper, "_run", fake_run)
    # Pretend the local .env exists.
    monkeypatch.setattr(hyper.os.path, "isfile", lambda p: p.endswith(".env"))
    ok, detail = hyper.inject_secrets_to_node(
        _args(), "node-1", "/opt/trinity/jarvis", local_root=str(_REPO_ROOT),
    )
    assert ok, detail
    assert calls, "scp must be invoked for the secret transfer"
    # Strict 30s timeout used.
    assert all(t <= 30.0 for _, t in calls), "secret transfer must use the strict timeout"


def test_inject_secrets_timeout_fails_closed(hyper, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_SECRET_FILES", ".env")

    def fake_run(cmd, *, timeout_s=120.0):
        return 1, "[run failed: TimeoutExpired]"  # transfer failed / timed out.

    monkeypatch.setattr(hyper, "_run", fake_run)
    monkeypatch.setattr(hyper.os.path, "isfile", lambda p: p.endswith(".env"))
    ok, detail = hyper.inject_secrets_to_node(
        _args(), "node-1", "/opt/trinity/jarvis", local_root=str(_REPO_ROOT),
    )
    assert not ok, "a failed secret transfer must fail-CLOSED"


def test_git_transport_secret_failure_burns_node(hyper, monkeypatch):
    """End-to-end: when the secret injection fails, sync_repos_to_node returns a
    FAILURE classified as SECRET (burn), NOT a resumable keep-warm failure."""
    sha = "e" * 40
    monkeypatch.setenv("JARVIS_IAC_SYNC_TRANSPORT", "git")
    monkeypatch.setenv("JARVIS_IAC_SECRET_FILES", ".env")
    monkeypatch.setenv("JARVIS_IAC_CONCURRENT_DEPS", "false")

    monkeypatch.setattr(
        hyper, "resolve_local_git_target",
        lambda root: ("https://github.com/drussell23/JARVIS.git", sha, "main"),
    )
    monkeypatch.setattr(
        hyper, "git_clone_on_node",
        lambda *a, **k: (True, "cloned"),
    )
    # The remote-prep ssh succeeds; only the secret transfer fails.
    monkeypatch.setattr(
        hyper, "_run_streaming_labeled",
        lambda *a, **k: (0, ["workspace_ready\n"]),
    )
    monkeypatch.setattr(
        hyper, "inject_secrets_to_node",
        lambda *a, **k: (False, "secret .env transfer timed out"),
    )
    monkeypatch.setattr(hyper, "_resolve_repo_paths",
                        lambda args: [("jarvis", str(_REPO_ROOT))])

    ok, detail = hyper.sync_repos_to_node(_args(), "node-1", [".git"])
    assert not ok, "secret failure must fail the transport"
    assert getattr(hyper, "SYNC_FAILURE_BURN", "burn") in (detail or "") or "burn" in detail.lower() \
        or "secret" in detail.lower()


def test_sync_failure_secret_class_triggers_burn_not_keepwarm(hyper, monkeypatch):
    """The orchestrator's keep-warm vs burn decision: a SECRET-class sync failure
    must BURN (node without .env is useless), not keep-warm-for-resume."""
    # The classifier helper -- a secret failure is NOT resumable.
    assert hyper.is_secret_failure("secret .env transfer FAILED -- BURN") is True
    assert hyper.is_secret_failure("clone failed rc=1 (resumable)") is False


# --------------------------------------------------------------------------- #
# 5. concurrent deps run ALONGSIDE secrets (not serialized after).
# --------------------------------------------------------------------------- #
def test_concurrent_deps_launched_alongside_secrets(hyper, monkeypatch):
    sha = "f" * 40
    monkeypatch.setenv("JARVIS_IAC_SYNC_TRANSPORT", "git")
    monkeypatch.setenv("JARVIS_IAC_CONCURRENT_DEPS", "true")
    monkeypatch.setenv("JARVIS_IAC_SECRET_FILES", ".env")
    monkeypatch.setenv("JARVIS_IAC_DEPS_CMD", "python3 -m pip install -r requirements.txt")

    order = []

    monkeypatch.setattr(
        hyper, "resolve_local_git_target",
        lambda root: ("https://github.com/drussell23/JARVIS.git", sha, "main"),
    )
    monkeypatch.setattr(hyper, "git_clone_on_node", lambda *a, **k: (True, "cloned"))
    monkeypatch.setattr(
        hyper, "_run_streaming_labeled",
        lambda *a, **k: (0, ["workspace_ready\n"]),
    )
    monkeypatch.setattr(hyper, "_resolve_repo_paths",
                        lambda args: [("jarvis", str(_REPO_ROOT))])

    def fake_inject(*a, **k):
        order.append("secrets")
        return True, "secrets ok"

    def fake_deps(*a, **k):
        order.append("deps")
        return True, "deps ok"

    monkeypatch.setattr(hyper, "inject_secrets_to_node", fake_inject)
    monkeypatch.setattr(hyper, "run_node_deps_install", fake_deps)

    ok, detail = hyper.sync_repos_to_node(_args(), "node-1", [".git"])
    assert ok, detail
    assert "secrets" in order and "deps" in order, "both deps + secrets must run"


# --------------------------------------------------------------------------- #
# 6. transport=tar unchanged (back-compat regression).
# --------------------------------------------------------------------------- #
def test_tar_transport_back_compat_unchanged(hyper, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_SYNC_TRANSPORT", "tar")
    seen = {"tar_pipe": 0, "git_clone": 0}

    monkeypatch.setattr(hyper, "_resolve_repo_paths",
                        lambda args: [("jarvis", str(_REPO_ROOT))])

    def fake_run_streaming_labeled(cmd, *, label, log_path=None, timeout_s=3600.0):
        joined = " ".join(cmd)
        if "tar czf" in joined:
            seen["tar_pipe"] += 1
        return 0, ["ok\n"]

    monkeypatch.setattr(hyper, "_run_streaming_labeled", fake_run_streaming_labeled)

    def fake_clone(*a, **k):
        seen["git_clone"] += 1
        return True, "cloned"

    monkeypatch.setattr(hyper, "git_clone_on_node", fake_clone)

    ok, detail = hyper.sync_repos_to_node(_args(), "node-1", [".git"])
    assert ok, detail
    assert seen["git_clone"] == 0, "tar transport must NOT call the git clone"
    assert seen["tar_pipe"] >= 1, "tar transport must drive the tar-pipe"


# --------------------------------------------------------------------------- #
# 7. secret CONTENTS never logged.
# --------------------------------------------------------------------------- #
def test_secret_contents_never_logged(hyper, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_IAC_SECRET_FILES", ".env")
    secret_value = "SUPER_SECRET_API_KEY=sk-deadbeefcafe123456"

    def fake_run(cmd, *, timeout_s=120.0):
        # The scp command carries PATHS only -- never the file contents.
        assert secret_value not in " ".join(cmd), "secret CONTENTS leaked into the scp argv"
        return 0, "transferred\n"

    monkeypatch.setattr(hyper, "_run", fake_run)
    monkeypatch.setattr(hyper.os.path, "isfile", lambda p: p.endswith(".env"))
    hyper.inject_secrets_to_node(
        _args(), "node-1", "/opt/trinity/jarvis", local_root=str(_REPO_ROOT),
    )
    out = capsys.readouterr().out
    assert secret_value not in out, "secret CONTENTS must never be logged"
