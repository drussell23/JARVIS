"""Slice 213 — Native Orchestration Kernel (host-side Python launcher).

Replaces the brittle bash launcher whose `set -e` trap silently aborted the
2026-06-10 relaunch before the build (leaving a stale dirty image running —
caught only because the 212 attestation gate kept refusing it). The kernel is
typed, tested, async (asyncio.create_subprocess_exec), and makes POST-LAUNCH
ATTESTATION VERIFICATION part of the launch contract: a launch is not DONE
until the container's stamp MATCHes the pin and the marker code is present.

SCOPE REFUSALS (load-bearing, mirrored in the module docstring):
- HOST-side only. No in-container self-cycling: that requires the docker
  socket inside an autonomous LLM-agent container = root-equivalent host
  escape (same refusal class as the Slice-199 ~/.ssh mount).
- No auto-reload on self-verified patches: S208 detectors are friction, not
  proof. Deploy boundary stays operator merge -> kernel relaunch.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.lifecycle_kernel import (
    LaunchVerdict,
    build_launch_env,
    compute_dirty,
    resolve_commit,
    verify_postlaunch,
)


# ===========================================================================
# A — dirty computation (scoped to dirt that enters the image)
# ===========================================================================

def test_dirty_false_on_clean_tree():
    def run(args):
        assert ":!.jarvis" in args and ":!**/__pycache__" in args
        return 0, ""
    assert compute_dirty(run=run) == "false"


def test_dirty_true_on_real_code_dirt():
    def run(args):
        return 0, " M backend/core/ouroboros/governance/orchestrator.py\n"
    assert compute_dirty(run=run) == "true"


def test_dirty_unknown_on_git_failure():
    def run(args):
        return 128, ""
    assert compute_dirty(run=run) == "unknown"


# ===========================================================================
# B — commit resolution + env construction
# ===========================================================================

def test_resolve_commit():
    def run(args):
        return 0, "5873fbb1a0deadbeef00112233445566778899aa\n"
    assert resolve_commit(run=run).startswith("5873fbb1a0")


def test_resolve_commit_unstamped_on_failure():
    def run(args):
        return 1, ""
    assert resolve_commit(run=run) == "unstamped"


def test_build_launch_env_stamps_and_pins():
    env = build_launch_env(commit="abc123", dirty="false", base={"PATH": "/x"})
    assert env["GIT_COMMIT"] == "abc123"
    assert env["GIT_DIRTY"] == "false"
    assert env["JARVIS_ATTESTATION_EXPECTED_COMMIT"] == "abc123"
    assert env["PATH"] == "/x"          # base preserved
    assert "SOAK_REQUIREMENTS" in env   # oracle default


def test_dirty_tree_refuses_pin():
    """A dirty build must NOT be pinned-and-launched silently — the kernel
    refuses upfront (strict gate would refuse it at boot anyway; failing at
    launch time is the honest, earlier failure)."""
    with pytest.raises(RuntimeError) as ei:
        build_launch_env(commit="abc123", dirty="true", base={})
    assert "dirty" in str(ei.value).lower()


# ===========================================================================
# C — post-launch verification IS the launch contract
# ===========================================================================

def _exec_runner(stamp_commit="abc123", mesh=True):
    async def run(args):
        joined = " ".join(args)
        if ".build_attestation.json" in joined:
            return 0, json.dumps({"commit": stamp_commit, "dirty": "false"})
        if "STRATEGIC IGNITION MESH" in joined:
            return (0, "2") if mesh else (1, "0")
        return 0, ""
    return run


def test_verify_postlaunch_match():
    v = asyncio.run(verify_postlaunch(
        container="c", expected_commit="abc123",
        run=_exec_runner(),
    ))
    assert v is LaunchVerdict.ATTESTED_MATCH


def test_verify_postlaunch_detects_stale_container():
    """THE phantom-deploy class: container stamp != pinned commit."""
    v = asyncio.run(verify_postlaunch(
        container="c", expected_commit="abc123",
        run=_exec_runner(stamp_commit="bb0aab05ef"),
    ))
    assert v is LaunchVerdict.STAMP_MISMATCH


def test_verify_postlaunch_detects_missing_marker():
    v = asyncio.run(verify_postlaunch(
        container="c", expected_commit="abc123",
        run=_exec_runner(mesh=False), marker="STRATEGIC IGNITION MESH",
    ))
    assert v is LaunchVerdict.MARKER_MISSING


def test_verify_postlaunch_unverified_on_exec_failure():
    async def run(args):
        return 125, ""  # container not up
    v = asyncio.run(verify_postlaunch(
        container="c", expected_commit="abc123", run=run,
    ))
    assert v is LaunchVerdict.UNVERIFIED


# ===========================================================================
# D — refusal pins (the lines that must not move)
# ===========================================================================

def test_kernel_never_touches_docker_socket():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "backend" / "core"
           / "ouroboros" / "governance" / "lifecycle_kernel.py").read_text(
        encoding="utf-8")
    assert "docker.sock" not in src
    assert "auto-reload" not in src.lower() or "refus" in src.lower()
    # host-side declaration present
    assert "HOST-side" in src or "host-side" in src
