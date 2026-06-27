"""Tests for sandbox_exec.py — ephemeral Trinity-Docker bash/pytest, fail-closed.

Verifies:
  (a) With sandbox enabled + injected fake docker_run: argv contains --network/none
      and sandbox_run_bash returns ok=True, denied=False.
  (b) With sandbox disabled: sandbox_run_bash returns denied=True, ok=False —
      NEVER falls through to unsandboxed host execution (fail-closed contract).
"""
from __future__ import annotations
import asyncio
import os

import pytest

import backend.core.ouroboros.governance.sandbox_exec as sx


def test_bash_runs_in_container_via_injected_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "true")

    async def fake_docker(argv, timeout):
        assert "--network" in argv and "none" in argv  # air-gap enforced
        return (0, "ok", "")

    r = asyncio.run(
        sx.sandbox_run_bash("ls", worktree=str(tmp_path), docker_run=fake_docker)
    )
    assert r.ok and not r.denied


def test_fail_closed_when_sandbox_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "false")

    r = asyncio.run(
        sx.sandbox_run_bash("ls", worktree=str(tmp_path))
    )
    assert r.denied and not r.ok  # NEVER runs unsandboxed


def test_run_tests_fail_closed_when_sandbox_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "false")

    r = asyncio.run(
        sx.sandbox_run_tests(["tests/"], worktree=str(tmp_path))
    )
    assert r.denied and not r.ok  # fail-closed for run_tests vector too


def test_run_tests_in_container_via_injected_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "true")

    async def fake_docker(argv, timeout):
        assert "--network" in argv and "none" in argv  # air-gap enforced
        # Return minimal pytest summary output so the parser produces ok=True.
        return (0, "1 passed in 0.01s", "")

    r = asyncio.run(
        sx.sandbox_run_tests(["tests/"], worktree=str(tmp_path), docker_run=fake_docker)
    )
    assert r.ok and not r.denied


def test_bash_fail_closed_on_spawn_failed_breach(monkeypatch, tmp_path):
    """Verify sandbox_run_bash denies when breach=SPAWN_FAILED (fail-closed)."""
    from backend.core.ouroboros.governance.container_sandbox import ContainmentBreach
    from dataclasses import dataclass

    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "true")

    @dataclass(frozen=True)
    class FakeContainerResult:
        breach: ContainmentBreach
        ok: bool
        stdout: str
        stderr: str
        returncode: int
        diagnostic: str

    async def fake_run_in_container(*args, **kwargs):
        # Return a result with breach=SPAWN_FAILED to trigger deny path
        return FakeContainerResult(
            breach=ContainmentBreach.SPAWN_FAILED,
            ok=False,
            stdout="",
            stderr="Docker spawn failed",
            returncode=None,
            diagnostic="Docker spawn failed",
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.container_sandbox.run_in_container",
        fake_run_in_container,
    )

    r = asyncio.run(
        sx.sandbox_run_bash("ls", worktree=str(tmp_path))
    )
    assert r.denied and not r.ok  # SPAWN_FAILED → deny


def test_run_tests_fail_closed_on_spawn_failed_breach(monkeypatch, tmp_path):
    """Verify sandbox_run_tests denies when breach=SPAWN_FAILED (fail-closed)."""
    from backend.core.ouroboros.governance.container_sandbox import ContainmentBreach
    from dataclasses import dataclass

    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "true")

    @dataclass(frozen=True)
    class FakePytestResult:
        breach: ContainmentBreach
        ok: bool
        diagnostic: str
        returncode: int

    async def fake_run_pytest_in_container(*args, **kwargs):
        # Return a result with breach=SPAWN_FAILED to trigger deny path
        return FakePytestResult(
            breach=ContainmentBreach.SPAWN_FAILED,
            ok=False,
            diagnostic="Docker spawn failed",
            returncode=None,
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.container_sandbox.run_pytest_in_container",
        fake_run_pytest_in_container,
    )

    r = asyncio.run(
        sx.sandbox_run_tests(["tests/"], worktree=str(tmp_path))
    )
    assert r.denied and not r.ok  # SPAWN_FAILED → deny
