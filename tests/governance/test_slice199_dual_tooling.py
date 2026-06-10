"""Slice 199 — Sovereign Tooling & Dual-Identity Matrix (HTTPS-token variant).

Slice 198's honest finding: the gitless, gh-less container cannot ship a
proposal — orange-PR correctly stayed fail-closed. This slice makes the
container self-contained and code-shipping WITHOUT mounting host SSH keys:

  * gh + openssh-client layered into the image (git was already present).
  * The container bootstraps its OWN isolated git repo bound to origin over
    HTTPS using the GH_TOKEN already in the env (no ~/.ssh / ~/.config/gh
    bind mount — the token is scoped + the repo is the container's own, which
    is MORE isolated than sharing host identity files, not less).
  * Non-interactive hardening everywhere — GIT_TERMINAL_PROMPT=0 +
    GIT_ASKPASS so a missing/expired credential FAILS CLOSED, never hangs on
    a hidden CLI prompt.
  * orange-PR arming gains a ``gh auth status`` non-interactive check.

Security invariant (grep-pinned): the soak compose does NOT bind-mount the
host's ~/.ssh or ~/.config/gh into the autonomous-agent container.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.m10_autonomous_graduation import (
    gh_auth_status_ok,
    hardened_git_env,
    orange_pr_armed,
)
from backend.core.ouroboros.governance.observability_registry import (
    HEDGE_CONCURRENCY_DISPATCHES,
    _reset_singleton_for_tests,
    get_observability_registry,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
_DOCKER = _REPO_ROOT / "docker"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.setenv(
        "JARVIS_M10_GRADUATION_STATE_PATH", str(tmp_path / "m10_state.json"),
    )
    for var in (
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "JARVIS_ORANGE_PR_ENABLED",
        "JARVIS_OBSERVABILITY_REGISTRY_ENABLED",
        "JARVIS_M10_AUTONOMOUS_GRADUATION_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset_singleton_for_tests()
    yield
    _reset_singleton_for_tests()


def _graduate():
    get_observability_registry().incr(HEDGE_CONCURRENCY_DISPATCHES, 6)


# ===========================================================================
# A — non-interactive hardened git env
# ===========================================================================

def test_hardened_git_env_disables_prompts():
    env = hardened_git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"]  # set to a non-interactive sink
    assert env["GCM_INTERACTIVE"] == "never"


def test_hardened_git_env_inherits_base():
    env = hardened_git_env({"PATH": "/usr/bin", "GH_TOKEN": "x"})
    assert env["PATH"] == "/usr/bin"
    assert env["GH_TOKEN"] == "x"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ===========================================================================
# B — gh auth status non-interactive check
# ===========================================================================

def test_gh_auth_ok_passes_on_success_probe():
    assert gh_auth_status_ok(_probe=lambda: True) is True


def test_gh_auth_ok_fails_closed_on_failure_probe():
    assert gh_auth_status_ok(_probe=lambda: False) is False


def test_gh_auth_ok_fails_closed_on_raise():
    def _boom():
        raise OSError("gh missing")
    assert gh_auth_status_ok(_probe=_boom) is False


# ===========================================================================
# C — orange arming now also requires gh auth
# ===========================================================================

def test_orange_armed_requires_auth(monkeypatch):
    import backend.core.ouroboros.governance.m10_autonomous_graduation as mag
    _graduate()
    monkeypatch.setattr(mag, "orange_pr_assertion_passes", lambda: True)
    monkeypatch.setattr(mag, "gh_auth_status_ok", lambda: False)
    assert orange_pr_armed() is False


def test_orange_armed_when_unlocked_assertion_and_auth(monkeypatch):
    import backend.core.ouroboros.governance.m10_autonomous_graduation as mag
    _graduate()
    monkeypatch.setattr(mag, "orange_pr_assertion_passes", lambda: True)
    monkeypatch.setattr(mag, "gh_auth_status_ok", lambda: True)
    assert orange_pr_armed() is True


# ===========================================================================
# D — image / entrypoint / compose source pins
# ===========================================================================

def test_dockerfile_installs_gh_and_ssh():
    src = (_DOCKER / "Dockerfile.soak").read_text(encoding="utf-8")
    assert "gh" in src  # GitHub CLI
    assert "openssh-client" in src


def test_entrypoint_hardens_non_interactive():
    src = (_DOCKER / "soak_git_entrypoint.sh").read_text(encoding="utf-8")
    assert "GIT_TERMINAL_PROMPT=0" in src
    assert "GCM_INTERACTIVE=never" in src
    assert "gh auth setup-git" in src
    assert "exec python3" in src


def test_entrypoint_uses_https_token_not_host_ssh():
    src = (_DOCKER / "soak_git_entrypoint.sh").read_text(encoding="utf-8")
    # HTTPS origin, token-based; never reads host ssh keys.
    assert "https://github.com/" in src
    assert "/root/.ssh" not in src
    assert "id_rsa" not in src and "id_ed25519" not in src


def test_compose_wires_entrypoint_and_hardening():
    src = (_REPO_ROOT / "docker-compose.dw-cortex-soak.yml").read_text(
        encoding="utf-8",
    )
    assert "soak_git_entrypoint.sh" in src
    assert "GIT_TERMINAL_PROMPT" in src


def test_compose_does_not_mount_host_ssh_or_gh_identity():
    """Security invariant: the autonomous-agent container never receives the
    host's private SSH keys or gh identity. PR shipping is token-over-HTTPS."""
    src = (_REPO_ROOT / "docker-compose.dw-cortex-soak.yml").read_text(
        encoding="utf-8",
    )
    assert ".ssh" not in src
    assert ".config/gh" not in src


# ===========================================================================
# E — doctrine pin
# ===========================================================================

def test_autocommitter_push_uses_hardened_env():
    src = (_GOV / "auto_committer.py").read_text(encoding="utf-8")
    assert "hardened_git_env" in src


def test_boundary_gate_not_weakened():
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src
    assert "soak_git_entrypoint" not in src
