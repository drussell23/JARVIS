# -*- coding: utf-8 -*-
"""Tests for the Autonomous Healing Loop added to _execute() in
scripts/sovereign_iac_hypervisor.py.

Scenario coverage:
  1. ready_timeout fails twice, succeeds on attempt 3 -> surgery proceeds, 2 heals fired,
     burn_node called between each failed attempt, sleep (backoff) applied.
  2. All 3 attempts fail -> falls through to existing keep-warm / abort path (rc=5).
  3. Non-resumable (secret) sync failure -> healing loop does NOT fire (already past phases
     1+1b), node is burned by the finally block immediately.

No real GCP/SSH/rsync -- all gcloud/ssh/rsync funnel through patched stubs.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import List

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("sovereign_iac_hypervisor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def iac():
    return _load_module()


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Isolate checkpoint ledger + autopsy logs in a tmp dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_IAC_STATE_PATH", str(tmp_path / "state.json"))
    return tmp_path


@pytest.fixture()
def args(iac):
    """Default arg namespace the tests can mutate."""
    a = iac.build_parser().parse_args([])
    # Disable rescue artifacts so we don't need SSH for that path.
    a.fault_tolerant_obs = False
    return a


# --------------------------------------------------------------------------- #
# Helpers / stubs.
# --------------------------------------------------------------------------- #

def _wire_all_fakes(
    iac, monkeypatch, *,
    provision_results,  # list of (ok, detail) per call
    poll_results,       # list of (ready, reason) per call
    sync_ok=True,
    surgery_verdict="PASS",
    sleep_log=None,
    burn_log=None,
):
    """Wire fakes for the full _execute pipeline.

    provision_results / poll_results are consumed in order (one per call).
    sleep_log and burn_log, if provided, accumulate call args.
    """
    provision_calls = list(provision_results)
    poll_calls = list(poll_results)
    provisioned_nodes: List[str] = []
    burned_nodes: List[str] = []
    slept: List[float] = []

    # Track which nodes got provisioned (non-ok calls still consume an entry).
    def _fake_provision(arg_ns, node, script, **kw):
        provisioned_nodes.append(node)
        ok, detail = provision_calls.pop(0)
        return ok, detail

    def _fake_poll(arg_ns, node, **kw):
        ok, reason = poll_calls.pop(0)
        return ok, reason

    def _fake_burn(arg_ns, node):
        burned_nodes.append(node)

    def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(iac, "provision_sandbox_node", _fake_provision)
    monkeypatch.setattr(iac, "poll_node_ready", _fake_poll)
    monkeypatch.setattr(iac, "burn_node", _fake_burn)
    monkeypatch.setattr(iac, "verify_node_gone", lambda *a, **k: True)
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    monkeypatch.setattr(iac, "rescue_artifacts_before_teardown", lambda *a, **k: None)
    monkeypatch.setattr(iac.time, "sleep", _fake_sleep)

    sync_detail = "SECRET_FAILURE_BURN: secret injection failed" if not sync_ok else "ok"
    monkeypatch.setattr(
        iac, "sync_repos_to_node", lambda *a, **k: (sync_ok, sync_detail)
    )
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_boot", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        iac, "run_remote_surgery",
        lambda *a, **k: (0, [f"VERDICT: {surgery_verdict}\n"], surgery_verdict),
    )

    return provisioned_nodes, burned_nodes, slept


# --------------------------------------------------------------------------- #
# Test 1: transient ready_timeout on attempts 1+2, success on attempt 3.
# --------------------------------------------------------------------------- #
def test_healing_loop_recovers_after_two_ready_timeout_failures(iac, args, monkeypatch):
    """ready_timeout fires on attempts 1 and 2; attempt 3 succeeds.

    Asserts:
    - 3 distinct nodes provisioned (original + 2 replacements).
    - burn_node called exactly 2 times from the healing loop (before each replacement).
    - sleep (backoff) called exactly 2 times.
    - _execute returns 0 (surgery reached PASS).
    - The 2 inter-attempt burns target the timed-out nodes, NOT the final node.
    """
    # Make JARVIS_IAC_HEAL_MAX_ATTEMPTS=3 explicit (also the default).
    monkeypatch.setenv("JARVIS_IAC_HEAL_MAX_ATTEMPTS", "3")
    # Short backoff so the test is instant.
    monkeypatch.setenv("JARVIS_IAC_HEAL_BACKOFF_BASE_S", "0")

    provisioned, burned, slept = _wire_all_fakes(
        iac, monkeypatch,
        provision_results=[
            (True, "ok"),   # attempt 1 provision: succeeds
            (True, "ok"),   # attempt 2 provision: succeeds
            (True, "ok"),   # attempt 3 provision: succeeds
        ],
        poll_results=[
            (False, "ready_timeout"),   # attempt 1 poll: fail
            (False, "ready_timeout"),   # attempt 2 poll: fail
            (True, ""),                 # attempt 3 poll: success
        ],
    )

    rc = iac._execute(args, "sovereign-sandbox-orig", iac.build_startup_script(), [])

    assert rc == 0, f"expected success (rc=0); got rc={rc}"

    # 3 nodes provisioned.
    assert len(provisioned) == 3, f"expected 3 provision attempts; got {provisioned}"
    # First node is the original name; replacements are fresh stamps.
    assert provisioned[0] == "sovereign-sandbox-orig"
    assert provisioned[1] != provisioned[0], "replacement 1 must have a new name"
    assert provisioned[2] != provisioned[1], "replacement 2 must have a new name"

    # 2 healing burns (one per timed-out node) + 1 terminal burn from the finally block.
    # The healing burns target the timed-out nodes, the terminal burn targets the final node.
    assert len(burned) == 3, (
        f"expected 2 heal burns + 1 terminal burn = 3 total; got {burned}"
    )
    assert burned[0] == provisioned[0], "first heal burn must reap attempt-1 node"
    assert burned[1] == provisioned[1], "second heal burn must reap attempt-2 node"
    # Final (terminal) burn targets the successful node.
    assert burned[2] == provisioned[2], "terminal burn targets the successful node"

    # Backoff applied twice (once per heal).
    assert len(slept) == 2, f"expected 2 sleeps (backoffs); got {slept}"


# --------------------------------------------------------------------------- #
# Test 2: all 3 attempts exhaust -> falls through to keep-warm / abort.
# --------------------------------------------------------------------------- #
def test_healing_loop_falls_through_to_abort_after_all_attempts_fail(iac, args, monkeypatch):
    """All 3 attempts time out on docker-ready.

    On the final attempt the healing loop does NOT retry (it's the last attempt).
    _execute returns 5 (existing ready_timeout abort path) and, with the default
    keep_warm_on_failure=True, the node is left warm (no terminal burn).
    """
    monkeypatch.setenv("JARVIS_IAC_HEAL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("JARVIS_IAC_HEAL_BACKOFF_BASE_S", "0")

    provisioned, burned, slept = _wire_all_fakes(
        iac, monkeypatch,
        provision_results=[
            (True, "ok"),
            (True, "ok"),
            (True, "ok"),
        ],
        poll_results=[
            (False, "ready_timeout"),
            (False, "ready_timeout"),
            (False, "ready_timeout"),  # final attempt also fails
        ],
    )

    rc = iac._execute(args, "sovereign-sandbox-all-fail", iac.build_startup_script(), [])

    # Must be the existing "readiness abort" return code.
    assert rc == 5, f"expected rc=5 (readiness abort); got rc={rc}"

    # 3 provisions (one per attempt) + 2 reaps from healing (not the final one).
    assert len(provisioned) == 3
    # 2 burns from the healing loop on attempts 1 and 2.
    # On attempt 3 (final), the healing loop does NOT burn -- it falls through to
    # the existing finally-block keep_warm path which, with keep_warm_on_failure=True,
    # leaves the node warm (no burn).
    assert len(burned) == 2, (
        f"expected 2 heal burns; final attempt falls to keep-warm (no burn); got {burned}"
    )
    # 2 backoff sleeps (attempts 1 and 2 only; no sleep before exiting on attempt 3).
    assert len(slept) == 2


# --------------------------------------------------------------------------- #
# Test 3: non-resumable SECRET failure in sync phase -> no healing retry, burn immediately.
# --------------------------------------------------------------------------- #
def test_non_resumable_secret_failure_does_not_trigger_healing_loop(iac, args, monkeypatch):
    """A SECRET sync failure (SYNC_FAILURE_BURN marker) is non-resumable.

    The healing loop wraps only phases 1+1b.  Once those succeed and we reach
    phase 2 (sync), we are past the healing loop.  A secret failure at sync:
    - Returns rc=6.
    - Sets resumable_failure=False -> finally block BURNS immediately (not keep-warm).
    - burn_node called exactly ONCE (from the finally block, NOT from the healing loop).
    - No backoff sleep fired (no healing retry).
    """
    monkeypatch.setenv("JARVIS_IAC_HEAL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("JARVIS_IAC_HEAL_BACKOFF_BASE_S", "0")

    provisioned, burned, slept = _wire_all_fakes(
        iac, monkeypatch,
        provision_results=[(True, "ok")],  # only one provision attempt needed
        poll_results=[(True, "")],          # ready on first try
        sync_ok=False,                     # SECRET failure in sync
    )

    # Disable keep-warm so the secret-failure burn in the finally fires.
    args.keep_warm_on_failure = False

    rc = iac._execute(args, "sovereign-sandbox-secret", iac.build_startup_script(), [])

    # Secret failure at sync -> return 6.
    assert rc == 6, f"expected rc=6 (sync abort); got rc={rc}"

    # Only 1 provision (no healing retries were needed -- phases 1+1b succeeded).
    assert len(provisioned) == 1, f"expected 1 provision; got {provisioned}"

    # Exactly 1 burn: from the finally block (secret = non-resumable -> burn always).
    assert len(burned) == 1, (
        f"expected 1 burn (finally block, non-resumable); got {burned}"
    )

    # No healing-loop backoff sleeps.
    assert len(slept) == 0, f"expected 0 healing sleeps; got {slept}"
