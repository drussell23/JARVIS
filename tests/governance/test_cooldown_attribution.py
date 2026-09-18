"""The brake measures the target, not the machine.

Measured in `bt-2026-09-18-033008`: every test-synthesis goal died
`_schema_invalid:diff_source_unreadable` — a diff demanded against a file the
goal existed to CREATE — and each failure escalated a cooldown on the target.
Test files that were never handed a runnable request reached
`consecutive_failures: 9`, and the ledger is persistent, so they stayed
suppressed long after the pipeline defect was fixed.

The cause was a state swallow with two halves:

* `record_terminal` ACCEPTED an `outcome` and only logged it, so the reason
  died at the write site;
* `_goal_verdict` read the row back, took the op id, and reported
  "goal unsatisfied" for every terminal — a provider outage and a model that
  cannot write the file were the same sentence.
"""
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.governance import terminal_reason as TR
from backend.core.ouroboros.governance.autonomy.sentinel_loop import SentinelLoop


# --------------------------------------------------------------------------
# The predicate — composed from the existing taxonomy, not a second one
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "_schema_invalid:diff_source_unreadable:tests/test_x.py",
    "gcp-jprime_schema_invalid:candidates_empty",
    "not_admitted:exception",
    "superseded_on_resume",
])
def test_a_pipeline_fault_is_not_the_targets_fault(code):
    """THE regression. The op never got a fair attempt."""
    assert TR.classify_terminal_reason(code) is TR.TerminalReasonClass.PIPELINE_CONTRACT_FAULT
    assert TR.is_target_attributable(code) is False


@pytest.mark.parametrize("code", [
    "all_providers_exhausted",
    "circuit_breaker_tripped:terminal_quota",
    "wall_clock_cap",
    "budget_floor_breached",
    "cooldown_cancelled_shutdown",
])
def test_facts_about_the_run_never_cool_a_target(code):
    assert TR.is_target_attributable(code) is False


@pytest.mark.parametrize("code", [
    "exploration_insufficient: 0/2",
    "ascii_gate_failed",
    "adversarial_reviewer_rejected",
])
def test_a_gate_rejecting_a_real_generation_IS_the_targets_fault(code):
    """The model produced something and a gate refused it. That is the AI
    capability failure the brake exists for."""
    assert TR.is_target_attributable(code) is True


@pytest.mark.parametrize("code", ["", None, "totally_unrecognized_xyz", 42])
def test_unknown_fails_TOWARD_the_brake(code):
    """Deliberate. If an unclassified reason exempted a target, any new
    failure string would silently disable the only thing stopping the loop
    spinning on one file."""
    assert TR.is_target_attributable(code) is True


def test_a_pipeline_fault_is_not_reflexive_healing_eligible():
    """There is no model output to feed back — the op never produced one."""
    assert TR.is_reflexive_healing_eligible("schema_invalid:diff_source_unreadable") is False


def test_the_new_class_did_not_capture_the_gate_rules():
    """Ordering matters: the pipeline rules sit before the gate rules, so a
    gate rejection must still classify as one."""
    assert TR.classify_terminal_reason("iron_gate_blocked") is \
        TR.TerminalReasonClass.STRUCTURAL_GATE_REJECTION


# --------------------------------------------------------------------------
# The Sentinel — one decision point, and it never raises
# --------------------------------------------------------------------------

class _Ledger:
    def __init__(self):
        self.cooled = []

    def record_failure(self, target, *, reason=""):
        self.cooled.append((target, reason))

    def record_success(self, target):
        pass


def _loop(tmp_path, ledger):
    return SentinelLoop(
        repo_root=tmp_path, dispatch=lambda **kw: "op", cooldown=ledger,
    )


def test_a_pipeline_fault_does_not_escalate_the_cooldown(tmp_path, caplog):
    led = _Ledger()
    loop = _loop(tmp_path, led)
    with caplog.at_level("WARNING"):
        cooled = loop._cool_if_attributable(
            "tests/test_x.py", "failed",
            "op op-01a0b28e terminal: gcp-jprime_schema_invalid:"
            "diff_source_unreadable:tests/test_x.py",
        )
    assert cooled is False
    assert led.cooled == []
    assert "NOT cooled" in " ".join(r.getMessage() for r in caplog.records)


def test_a_real_target_failure_still_cools(tmp_path):
    """The brake keeps its teeth."""
    led = _Ledger()
    cooled = _loop(tmp_path, led)._cool_if_attributable(
        "tests/test_x.py", "failed", "op op-1 terminal: ascii_gate_failed",
    )
    assert cooled is True
    assert led.cooled and led.cooled[0][0] == "tests/test_x.py"


def test_an_unattributable_reason_still_cools(tmp_path):
    led = _Ledger()
    assert _loop(tmp_path, led)._cool_if_attributable(
        "tests/test_x.py", "failed", "op op-1 terminal, goal unsatisfied",
    ) is True
    assert len(led.cooled) == 1


def test_a_brake_that_raises_is_worse_than_one_that_over_cools(tmp_path):
    """If attribution itself breaks, cool — never propagate into the loop."""
    class _Exploding:
        def __init__(self):
            self.cooled = []

        def record_failure(self, target, *, reason=""):
            self.cooled.append(target)

    led = _Exploding()
    loop = _loop(tmp_path, led)
    # A detail that cannot be classified (raises inside the predicate).
    assert loop._cool_if_attributable("t.py", "failed", object()) is True
    assert led.cooled == ["t.py"]


def test_every_failure_exit_goes_through_the_one_decision_point(tmp_path):
    """Three exits, one rule. A guard copied three times is how two of them
    come to disagree."""
    import inspect

    src = inspect.getsource(SentinelLoop)
    body = src.split("def _cool_if_attributable")[0] + \
        src.split("def _cool_if_attributable")[1].split("    # -- seams")[1]
    assert "self._cooldown.record_failure(" not in body
    assert src.count("self._cool_if_attributable(") == 3


# --------------------------------------------------------------------------
# The ledger — the reason must survive the round trip, the chain must not move
# --------------------------------------------------------------------------

def test_the_reason_survives_the_ledger_round_trip(tmp_path, monkeypatch):
    """`record_terminal` used to take `outcome` and only LOG it. That is where
    the cause died."""
    import asyncio

    from backend.core.ouroboros.governance import goal_reconciliation_ledger as L

    p = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("JARVIS_GOAL_RECONCILIATION_LEDGER_PATH", str(p))
    monkeypatch.setenv("JARVIS_GOAL_RECONCILIATION_ENABLED", "true")

    # The secret is passed EXPLICITLY on both sides. Rows are MAC'd, and a
    # test that leans on the ambient `.env` reads back zero records and looks
    # like a data-loss bug — that false alarm has already cost this project an
    # investigation once.
    secret = "test-roadmap-secret"
    asyncio.run(L.record_terminal(
        goal_id="g-1", op_id="op-1",
        outcome="gcp-jprime_schema_invalid:diff_source_unreadable:tests/test_x.py",
        path=p, secret=secret,
    ))
    recs = [r for r in (L.read_records(path=p, secret=secret) or ())
            if r.event == L.ReconciliationEvent.TERMINAL.value]
    assert recs, "no terminal row was written"
    assert "diff_source_unreadable" in recs[-1].terminal_reason


def test_the_reason_is_OUTSIDE_the_maced_payload(tmp_path, monkeypatch):
    """Adding a field to the MAC'd payload would invalidate every row already
    on disk. The chain covers `_PAYLOAD_FIELDS`; the reason must not be one."""
    from backend.core.ouroboros.governance import goal_reconciliation_ledger as L

    assert "terminal_reason" not in L._PAYLOAD_FIELDS
    assert "session" not in L._PAYLOAD_FIELDS      # the precedent it follows


def test_a_row_with_no_reason_is_byte_identical_to_a_pre_existing_one(tmp_path):
    """Old rows must read back unchanged, and a reasonless row must not start
    emitting a new key."""
    from backend.core.ouroboros.governance import goal_reconciliation_ledger as L

    rec = L._make_record(
        event=L.ReconciliationEvent.DISPATCHED, goal_id="g", goal_digest_hex="",
        commit_sha="", op_id="op", prev_hash="x", secret=None,
    )
    assert "terminal_reason" not in rec.to_dict()


def test_an_old_row_without_the_field_still_reads(tmp_path, monkeypatch):
    """Forward compatibility in the direction that matters: 435 rows predate
    this field."""
    import json

    from backend.core.ouroboros.governance import goal_reconciliation_ledger as L

    secret = "test-roadmap-secret"
    rec = L._make_record(
        event=L.ReconciliationEvent.TERMINAL, goal_id="g", goal_digest_hex="",
        commit_sha="", op_id="op", prev_hash=L._genesis(), secret=secret,
        terminal_reason="something",
    )
    row = rec.to_dict()
    row.pop("terminal_reason", None)          # a row written before the field
    p = tmp_path / "old.jsonl"
    p.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    out = L.read_records(path=p, secret=secret)
    assert len(out) == 1, "a pre-existing row must still read"
    assert out[0].terminal_reason == ""
    assert out[0].op_id == "op"               # the MAC'd payload is intact


# --------------------------------------------------------------------------
# The gap the VRAM finding exposed in this very fix
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "background_accepted:background_dw_blocked_by_topology:Catalog-driven",
    "dw_severed_queued:topology_block:Catalog-driven (Phase 12)",
    "speculative_deferred:blocked_by_topology:Catalog-driven",
])
def test_an_empty_provider_catalog_is_not_the_targets_fault(code):
    """The single largest failure class in bt-2026-09-18-034951 — 42 of 54
    failed passes, every one with zero tokens generated — and the first
    version of this fix left all 42 cooling their targets.

    A file is not harder to test because a provider catalog was empty when
    its turn came.
    """
    assert TR.classify_terminal_reason(code) is \
        TR.TerminalReasonClass.PROVIDER_EXHAUSTION
    assert TR.is_target_attributable(code) is False


@pytest.mark.parametrize("code", [
    "[SYSTEM: DEFERRED_DUE_TO_MEMORY_PRESSURE]",
    "deferred_due_to_memory_pressure: 20.0 GiB exceeds the 7.9 GiB free",
    "boot_recovery_missing_provenance",
])
def test_a_busy_accelerator_is_not_the_targets_fault(code):
    """The op never reached the model. Cooling the target would suppress a
    blameless file because the card was full for one instant."""
    assert TR.is_target_attributable(code) is False


def test_closing_the_gap_did_not_silence_the_brake():
    """The rules were ADDED, not the default inverted — which is what the
    design of this module calls for. A real target failure still cools."""
    assert TR.is_target_attributable("ascii_gate_failed") is True
    assert TR.is_target_attributable("exploration_insufficient: 0/2") is True
    assert TR.is_target_attributable("some_unclassified_new_string") is True
