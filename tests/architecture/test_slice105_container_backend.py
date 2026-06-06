"""Slice 105 — Containerized Sandbox Backend (the Linux/Docker bridge).

Phase-4 verification. The Docker API is mocked (an injectable async runner) so the
matrix is deterministic with NO real Docker — asserting the FSM formats the hardened
payload correctly, parses isolated output, and handles container timeouts/crashes.
A real-Docker integration test is gated on Docker actually responding (skipped here,
runs on a Docker host) and proves the kernel guarantee (network egress denied).
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile

import pytest

from backend.core.ouroboros.governance import container_sandbox as CS
from backend.core.ouroboros.governance.runtime_sandbox import (
    ContainmentBreach,
    ContainmentPolicy,
)


class _FakeDocker:
    """Injectable async stand-in for container_engine._real_docker_run."""

    def __init__(self, rc=0, out="ok\n", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.argv = None
        self.timeout_s = None

    async def __call__(self, argv, timeout_s):
        self.argv = list(argv)
        self.timeout_s = timeout_s
        return self.rc, self.out, self.err


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "container")
    yield


# === Hardened payload formatting (composes Slice 92's zero-trust profile) ===

def test_hardened_argv_has_full_zero_trust_profile():
    argv = CS.build_container_argv(
        "print('hi')", worktree="/tmp/wt", image="python:3.11-slim",
        policy=ContainmentPolicy(as_bytes=536870912),
    )
    s = " ".join(argv)
    assert "--network none" in s          # no egress
    assert "--cap-drop ALL" in s          # no capabilities
    assert "no-new-privileges" in s       # no privilege escalation
    assert "--read-only" in s             # immutable rootfs
    assert "--pids-limit" in s            # no fork bomb
    assert "--rm" in s                    # ephemeral
    assert "-v /tmp/wt:/work:rw" in s     # worktree = sole writable mount
    assert "-w /work" in s
    assert argv[-3:] == ["-I", "-c", "print('hi')"]   # isolated python, payload inline


def test_optional_strict_seccomp_profile_is_injected(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_SECCOMP_PROFILE", "/etc/jarvis/strict.json")
    argv = CS.build_container_argv("print(1)", worktree="/tmp/wt")
    assert "seccomp=/etc/jarvis/strict.json" in " ".join(argv)


# === Output parsing + breach mapping (mocked Docker API) ====================

def test_clean_container_run_parses_output():
    fake = _FakeDocker(rc=0, out="HELLO\n", err="")
    r = asyncio.run(CS.run_in_container("print('HELLO')", worktree=tempfile.mkdtemp(),
                                        docker_run=fake))
    assert r.ok is True
    assert r.breach is ContainmentBreach.NONE
    assert "HELLO" in r.stdout
    assert r.platform == "linux-container"
    assert "network_egress_denied" in r.guarantees


def test_container_timeout_is_a_breach():
    fake = _FakeDocker(rc=124, out="", err="docker run exceeded 5.0s")
    r = asyncio.run(CS.run_in_container("\nwhile True: pass", worktree=tempfile.mkdtemp(),
                                        policy=ContainmentPolicy(timeout_s=5), docker_run=fake))
    assert r.ok is False
    assert r.breach is ContainmentBreach.TIMEOUT


def test_container_nonzero_is_a_contained_breach():
    fake = _FakeDocker(rc=1, out="", err="Traceback ...")
    r = asyncio.run(CS.run_in_container("raise SystemExit(1)", worktree=tempfile.mkdtemp(),
                                        docker_run=fake))
    assert r.ok is False
    assert r.breach is ContainmentBreach.NONZERO_EXIT


def test_container_signal_kill_is_a_breach():
    fake = _FakeDocker(rc=-9, out="", err="")
    r = asyncio.run(CS.run_in_container("x", worktree=tempfile.mkdtemp(), docker_run=fake))
    assert r.breach is ContainmentBreach.SIGNAL_KILLED


def test_disabled_master_yields_disabled(monkeypatch):
    monkeypatch.delenv("JARVIS_RUNTIME_SANDBOX_ENABLED", raising=False)
    fake = _FakeDocker()
    r = asyncio.run(CS.run_in_container("print(1)", worktree=tempfile.mkdtemp(), docker_run=fake))
    assert r.breach is ContainmentBreach.DISABLED
    assert fake.argv is None   # never even built/ran the container


# === The live wire: run_payload_contained routing + fallback ================

def test_wire_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "local")  # not container
    out = asyncio.run(CS.run_payload_contained("print(1)", worktree=tempfile.mkdtemp()))
    assert out is None   # caller runs the legacy path


def test_wire_returns_result_when_enabled():
    fake = _FakeDocker(rc=0, out="WIRED\n")
    out = asyncio.run(CS.run_payload_contained("print('WIRED')", worktree=tempfile.mkdtemp(),
                                               docker_run=fake))
    assert out is not None and out.ok and "WIRED" in out.stdout


def test_wire_surfaces_breach_for_graceful_fallback():
    fake = _FakeDocker(rc=124, out="", err="timeout")
    out = asyncio.run(CS.run_payload_contained("loop", worktree=tempfile.mkdtemp(), docker_run=fake))
    assert out is not None and out.ok is False and out.breach is ContainmentBreach.TIMEOUT


def test_backend_selection_logic(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "container")
    assert CS.containerized_sandbox_enabled() is True
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "local")
    assert CS.containerized_sandbox_enabled() is False
    monkeypatch.delenv("JARVIS_RUNTIME_SANDBOX_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_BACKEND", "container")
    assert CS.containerized_sandbox_enabled() is False  # master off dominates


# === Real-Docker integration (skipped unless the daemon is responding) ======

def _docker_responsive() -> bool:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _docker_responsive(), reason="Docker daemon not responding (dev host)")
def test_real_container_denies_network_egress(tmp_path):
    # The REAL kernel-containment proof: a payload attempting network egress
    # CANNOT reach the network (--network none), even though it executes.
    code = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    sys.stdout.write('EGRESS_OK')\n"
        "except Exception:\n"
        "    sys.stdout.write('EGRESS_BLOCKED')\n"
    )
    r = asyncio.run(CS.run_in_container(code, worktree=str(tmp_path),
                                        policy=ContainmentPolicy(timeout_s=30)))
    assert "EGRESS_BLOCKED" in r.stdout
    assert "EGRESS_OK" not in r.stdout
