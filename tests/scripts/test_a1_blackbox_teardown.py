"""Tests for the Black Box checksum-gated teardown in the A1 harness orchestrator.

All subprocess + network are mocked -- no real scp/ssh, no node, zero spend.
Proves the absolute data-preservation contract:

  * a MATCHING pulled sha256 -> teardown AUTHORIZED;
  * a MISMATCH -> teardown REFUSED + node HELD + HELD_NODE.txt written;
  * a pull-failure after the bounded retries -> HELD (never burned);
  * the chaos-revert still runs in finally regardless of the checksum outcome;
  * the DW-primary pre-launch assertion REFUSES launch when
    JARVIS_PROVIDER_CLAUDE_DISABLED != true OR no JARVIS_DW_PRIMARY_OVERRIDE.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "a1_live_fire_chaos_harness.py"
_spec = importlib.util.spec_from_file_location("a1_live_fire_chaos_harness", _SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["a1_live_fire_chaos_harness"] = harness
_spec.loader.exec_module(harness)


# ===========================================================================
# DW-primary pre-launch assertion.
# ===========================================================================


def test_dw_primary_assertion_passes_when_pinned():
    env = {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
        "JARVIS_DW_PRIMARY_OVERRIDE": "openai/gpt-oss-120b",
    }
    ok, reason = harness.assert_dw_primary(env)
    assert ok is True
    assert reason == ""


def test_dw_primary_assertion_refuses_when_claude_not_disabled():
    env = {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "false",
        "JARVIS_DW_PRIMARY_OVERRIDE": "openai/gpt-oss-120b",
    }
    ok, reason = harness.assert_dw_primary(env)
    assert ok is False
    assert "CLAUDE" in reason.upper() or "claude" in reason.lower()


def test_dw_primary_assertion_refuses_when_no_dw_override():
    env = {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
        "JARVIS_DW_PRIMARY_OVERRIDE": "",
    }
    ok, reason = harness.assert_dw_primary(env)
    assert ok is False
    assert "DW" in reason.upper()


def test_compose_env_satisfies_dw_primary_assertion(monkeypatch):
    # The Linux overlay sets both -> composition must survive the assertion.
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    env = harness.compose_env()
    ok, reason = harness.assert_dw_primary(env)
    assert ok is True, "compose_env must satisfy the DW-primary assertion: %s" % reason


def test_execute_on_node_refuses_launch_without_dw_primary(monkeypatch, capsys):
    # When the composed env does NOT pin DW-primary, --execute-on-node REFUSES.
    monkeypatch.setattr(harness, "compose_env", lambda **kw: {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "false",  # NOT disabled
        "JARVIS_DW_PRIMARY_OVERRIDE": "openai/gpt-oss-120b",
    })
    # Ensure build_live_run is never reached (refusal happens first).
    built = []
    monkeypatch.setattr(harness, "build_live_run", lambda args: built.append(1))
    rc = harness.main(["--execute-on-node"])
    assert rc != 0, "must refuse to launch when DW-primary not pinned"
    assert built == [], "the run must not be built when the assertion fails"
    out = capsys.readouterr().out
    assert "REFUSED" in out and "DW-primary" in out


# ===========================================================================
# Checksum-gated teardown: the BlackBoxTeardown decision.
# ===========================================================================


class FakeTransport:
    """A fake IAP transport that records bundle/pull/verify/teardown calls and
    returns scripted results so the decision FSM can be exercised offline."""

    def __init__(self, *, node_sha, pulled_sha_sequence, pull_ok_sequence=None):
        self.node_sha = node_sha
        self.pulled_sha_sequence = list(pulled_sha_sequence)
        self.pull_ok_sequence = list(pull_ok_sequence) if pull_ok_sequence is not None else None
        self.calls = []

    def bundle_on_node(self, *, run_id, out_dir):
        self.calls.append(("bundle", run_id))
        return {"archive": "%s/black_box_%s.tar.gz" % (out_dir, run_id), "sha256": self.node_sha}

    def pull_archive(self, *, node_archive, node_sha_path, local_dir):
        self.calls.append(("pull", local_dir))
        if self.pull_ok_sequence is not None:
            ok = self.pull_ok_sequence.pop(0) if self.pull_ok_sequence else False
            if not ok:
                return None  # pull failed
        # Return the locally-recomputed sha (scripted).
        return self.pulled_sha_sequence.pop(0) if self.pulled_sha_sequence else None

    def teardown(self, *, node):
        self.calls.append(("teardown", node))
        return True


def _make_decider(tmp_path, transport, *, retries=3):
    return harness.BlackBoxTeardownDecider(
        run_id="run-X",
        node="sovereign-node-1",
        autopsy_root=str(tmp_path / "autopsies"),
        transport=transport,
        pull_retries=retries,
    )


def test_matching_sha_authorizes_teardown(tmp_path):
    sha = "a" * 64
    transport = FakeTransport(node_sha=sha, pulled_sha_sequence=[sha])
    decider = _make_decider(tmp_path, transport)
    decision = decider.run()
    assert decision.authorized is True, "matching sha must authorize teardown"
    assert decision.held is False
    assert ("teardown", "sovereign-node-1") in transport.calls


def test_mismatch_holds_node_and_writes_held_file(tmp_path):
    transport = FakeTransport(node_sha="a" * 64, pulled_sha_sequence=["b" * 64])
    decider = _make_decider(tmp_path, transport)
    decision = decider.run()
    assert decision.authorized is False, "a sha MISMATCH must NOT authorize teardown"
    assert decision.held is True, "the node must be HELD on a mismatch"
    # teardown must NOT have been called (the node is NOT burned).
    assert ("teardown", "sovereign-node-1") not in transport.calls
    # A HELD_NODE.txt with the manual-extract command is written.
    held = Path(decision.held_node_file)
    assert held.exists()
    body = held.read_text()
    assert "gcloud compute ssh" in body
    assert "sovereign-node-1" in body


def test_pull_failure_after_retries_holds_never_burns(tmp_path):
    # Every pull attempt fails -> after the bounded retries the node is HELD.
    transport = FakeTransport(
        node_sha="a" * 64, pulled_sha_sequence=[],
        pull_ok_sequence=[False, False, False],
    )
    decider = _make_decider(tmp_path, transport, retries=3)
    decision = decider.run()
    assert decision.authorized is False
    assert decision.held is True, "pull-failure after retries must HOLD the node"
    assert ("teardown", "sovereign-node-1") not in transport.calls
    # Exactly the bounded number of pull attempts were made.
    pulls = [c for c in transport.calls if c[0] == "pull"]
    assert len(pulls) == 3


def test_retry_then_match_authorizes(tmp_path):
    # First pull fails, second succeeds with a matching sha -> AUTHORIZED.
    sha = "c" * 64
    transport = FakeTransport(
        node_sha=sha, pulled_sha_sequence=[sha],
        pull_ok_sequence=[False, True],
    )
    decider = _make_decider(tmp_path, transport, retries=3)
    decision = decider.run()
    assert decision.authorized is True
    assert decision.held is False
    assert ("teardown", "sovereign-node-1") in transport.calls


def test_pull_retries_reads_env(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_BLACKBOX_PULL_RETRIES", "7")
    assert harness.blackbox_pull_retries() == 7
    monkeypatch.delenv("JARVIS_A1_BLACKBOX_PULL_RETRIES", raising=False)
    assert harness.blackbox_pull_retries() == 3  # default


# ===========================================================================
# The remote failure path composes Black-Box BEFORE the teardown + still reverts.
# ===========================================================================


class FakeChaosRevert:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append("status")
        return {"active": False}

    def list_candidates(self):
        self.calls.append("list_candidates")
        return 2

    def inject(self, seed):
        self.calls.append(("inject", seed))
        return True

    def revert(self):
        self.calls.append("revert")
        return True


def test_blackbox_failure_path_holds_on_mismatch_and_reverts(tmp_path, monkeypatch):
    # On a FAILED verdict the orchestrator runs Black-Box; a mismatch HOLDS the
    # node (no teardown) but chaos is STILL reverted in finally.
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("failed run\n")

    transport = FakeTransport(node_sha="a" * 64, pulled_sha_sequence=["b" * 64])

    class FakeSoak:
        def launch(self, env, run_dir):
            return harness.SoakHandle(debug_log=str(debug_log), session_dir=run_dir, proc=None)

        def stop(self):
            pass

    class FakeAuditor:
        def watch(self, *, base, log_file, timeout_s, verdict_out):
            Path(verdict_out).write_text('{"proven": false}')
            return {"proven": False, "failure_locus": "audit:timeout"}

    chaos = FakeChaosRevert()
    decisions = []

    def _fake_blackbox_teardown(*, run_id, node, autopsy_root, debug_log, verdict, chaos_manifest):
        decider = harness.BlackBoxTeardownDecider(
            run_id=run_id, node=node, autopsy_root=autopsy_root,
            transport=transport, pull_retries=2,
        )
        d = decider.run()
        decisions.append(d)
        return d

    run = harness.HarnessRun(
        run_id="run-RF",
        run_root=str(tmp_path / "runs"),
        autopsy_root=str(tmp_path / "autopsies"),
        cost_cap=0.0,
        wall_seconds=10,
        seed=1,
        sse_base="http://127.0.0.1:8099",
        chaos=chaos,
        soak=FakeSoak(),
        auditor=FakeAuditor(),
        blackbox_node="sovereign-node-RF",
        blackbox_teardown_fn=_fake_blackbox_teardown,
    )
    rc = run.execute()
    assert rc != 0
    # Black-Box teardown decision ran on the failure path.
    assert decisions, "the Black-Box teardown decision must run on a failed verdict"
    assert decisions[0].held is True, "a mismatch must HOLD the node"
    assert decisions[0].authorized is False
    # No teardown call -> node not burned.
    assert ("teardown", "sovereign-node-RF") not in transport.calls
    # Chaos STILL reverted in finally regardless.
    assert "revert" in chaos.calls
