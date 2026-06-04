"""Slice 92 — runtime container-containment verification.

Two-layer security profile (PRD §50.12):
  * Static AST layer: 10.6% passthrough — the run_body_* literal sinks PARSE +
    EXECUTE; no static tree can catch them without FP. Complete (Slice 90/91).
  * Runtime container layer: the zero-trust Docker profile neutralizes their
    CAPABILITIES (exfil / host-write / privilege) at the Linux-VM kernel.

Honesty: the jail does NOT stop a benign sink executing (Popen ls runs); it
stops the ESCAPE. So the metric is "0% of escape attempts succeed", and these
tests assert the arg construction (pure, no Docker) + the harness verdict logic
(mock runner). A live Docker run is a separate, skip-if-absent integration test.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import container_engine as CE
from backend.core.ouroboros.governance.graduation import (
    runtime_adversarial_sweep as R,
)


# --- Phase 1: the hardening profile + opt-in (scoring path untouched) ---

def test_hardened_profile_has_all_zero_trust_flags():
    argv = CE.build_hardened_security_argv()
    joined = " ".join(argv)
    assert "--network none" in joined          # no exfil
    assert "--cap-drop ALL" in joined          # no privileged ops
    assert "no-new-privileges" in joined       # no setuid escalation
    assert "--read-only" in joined             # immutable rootfs
    assert "--pids-limit" in joined            # fork-bomb ceiling


def test_scoring_path_is_unhardened_by_default():
    # CRITICAL: --read-only / --network none would break held-out test exec, so
    # the default scoring argv MUST NOT include them.
    argv = CE.build_docker_argv("img", "echo hi", "/tmp/p")
    j = " ".join(argv)
    assert "--network" not in j
    assert "--read-only" not in j
    assert "--cap-drop" not in j


def test_harden_opt_in_appends_profile():
    argv = CE.build_docker_argv("img", "echo hi", "/tmp/p", harden=True)
    j = " ".join(argv)
    assert "--network none" in j and "--cap-drop ALL" in j and "--read-only" in j
    # still a valid run argv with the image + entrypoint intact
    assert "img" in argv and "run" in argv


def test_probe_argv_is_hardened():
    argv = R.build_probe_argv("python:3-slim", "print(1)")
    j = " ".join(argv)
    assert "--network none" in j and "--cap-drop ALL" in j
    assert "python:3-slim" in argv


# --- Phase 2: harness verdict logic (mock runner — no daemon) ---

def _run(coro):
    return asyncio.run(coro)


def test_blocked_when_escape_marker_absent():
    async def fake_run(argv, timeout):
        # kernel denied the exfil/write → no marker, maybe a stderr error
        return 0, "", "OSError: Network is unreachable"
    rep = _run(R.run_runtime_sweep(_docker_run=fake_run))
    assert rep.escaped_count == 0
    assert rep.blocked_count == len(rep.results)
    assert rep.escape_rate == 0.0
    assert all(r.verdict == "CONTAINMENT_BLOCKED" for r in rep.results)


def test_escaped_when_marker_present():
    async def leaky_run(argv, timeout):
        # simulate a BROKEN jail that let the escape through
        return 0, "JARVIS_ESCAPE_SUCCEEDED\n", ""
    rep = _run(R.run_runtime_sweep(_docker_run=leaky_run))
    assert rep.escaped_count == len(rep.results)
    assert rep.escape_rate == 100.0
    assert all(r.verdict == "ESCAPED" for r in rep.results)


def test_harness_error_never_crashes_sweep():
    async def boom(argv, timeout):
        raise RuntimeError("docker daemon down")
    rep = _run(R.run_runtime_sweep(_docker_run=boom))
    assert all(r.verdict == "HARNESS_ERROR" for r in rep.results)
    # a harness error is NOT an escape (don't false-claim containment either)
    assert rep.escaped_count == 0


def test_report_to_dict_roundtrips():
    async def fake_run(argv, timeout):
        return 0, "", ""
    rep = _run(R.run_runtime_sweep(_docker_run=fake_run))
    d = rep.to_dict()
    assert d["schema_version"] == "runtime_adversarial_sweep.v1"
    assert d["escape_rate"] == 0.0
    assert len(d["results"]) == d["probe_count"]


# --- Phase 2 (integration): real Docker, skip if unavailable ---

@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_containment_blocks_all_escapes():
    """Live: every escape attempt must be neutralized by the hardened jail.
    Skips cleanly in CI without a docker daemon."""
    try:
        rep = _run(R.run_runtime_sweep(image="python:3-slim", timeout_s=40.0))
    except Exception as exc:  # pragma: no cover — daemon/image hiccup
        pytest.skip(f"docker run unavailable: {exc}")
    if any(r.verdict == "HARNESS_ERROR" for r in rep.results):
        pytest.skip("docker daemon/image not runnable in this environment")
    assert rep.escaped_count == 0, (
        f"a real escape got through the hardened jail: {rep.to_dict()}")
