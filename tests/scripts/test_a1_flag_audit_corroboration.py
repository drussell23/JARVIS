"""Slice 8 — corroborated-rejects rule for the A1 flag audit.

Run #18 false-red: a CommProtocol INTENT payload carrying
risk_tier='APPROVAL_REQUIRED' (stamped by the Slice-6 attribution gate)
matched semantic_guardian's bare 'APPROVAL_REQUIRED' rejected-marker and
poisoned the family to REJECTED → twelve_flag_audit_passed=false. A REJECT
must be corroborated by the family's own voice on the same line."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "a1_graduation_auditor", _REPO / "scripts" / "a1_graduation_auditor.py",
)
assert _spec is not None and _spec.loader is not None
auditor_mod = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = auditor_mod
_spec.loader.exec_module(auditor_mod)

# The REAL Run-18 poisoning line (truncated as the auditor stores it):
_RUN18_INTENT_LINE = (
    "2026-07-12T00:28:42 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] INTENT op=op-019f5539-f107-74e0 seq=1 payload="
    "{'goal': 'x', 'risk_tier': 'APPROVAL_REQUIRED'}"
)
_GENUINE_GUARD_REJECT_LINE = (
    "2026-07-12T00:29:01 [Ouroboros.Orchestrator] INFO [SemanticGuard] "
    "op=op-x findings=1 pattern=removed_import_still_referenced "
    "risk=APPROVAL_REQUIRED"
)


def _fresh_auditor_families(flags):
    # Same construction pattern as tests/scripts/test_a1_provenance_auditor.py.
    # family_for_flag maps each flag's family needle (e.g. SEMANTIC_GUARDIAN,
    # IRON_GATE) to the signal family under test.
    return auditor_mod.A1GraduationAuditor(
        flags=list(flags),
        strict=True,
        chaos_manifest_path=None,
        lineage_scoping_enabled=False,
    )


def _fresh_auditor():
    # One semantic_guardian-family flag is enough for the Slice 8 tests.
    return _fresh_auditor_families(["JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED"])


def test_intent_payload_line_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )
    assert any(
        "semantic_guardian" in x for x in a.uncorroborated_reject_lines
    )


def test_genuine_guard_rejection_still_poisons(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", raising=False)
    a = _fresh_auditor()
    a._correlate_flag_signal(_GENUINE_GUARD_REJECT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )


def test_kill_switch_restores_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_CORROBORATED_REJECTS", "false")
    a = _fresh_auditor()
    a._correlate_flag_signal(_RUN18_INTENT_LINE)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("semantic_guardian", ())
    )


# ---------------------------------------------------------------------------
# Slice 9 Task 5 — chaos-lineage reject scoping (Run #19 false-red)
#
# The unrelated-reject lane above only activates for resumed_ops audits.
# Single-session audits (no resumed ops) still globally correlated a REJECT
# even when a chaos manifest AND an op-lineage graph are available -- a
# GENUINE, self-corroborated rejection on a background op with nothing to do
# with the audited chaos repair poisoned the flag anyway.
# ---------------------------------------------------------------------------

_RUN19_FULL_OP = "op-019f58a7-f623-756b-bf73-0b4309aaaaaa"

_GENUINE_IRON_GATE_REJECT_UNRELATED_OP = (
    "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
    "Iron Gate — exploration_insufficient: 0/2 (attempt=1) "
    "op=" + _RUN19_FULL_OP
)


def _iron_gate_reject_line(op_token: str) -> str:
    return (
        "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
        "Iron Gate — exploration_insufficient: 0/2 (attempt=1) "
        "op=" + op_token
    )


def _auditor_with_chaos_lineage(chaos_op: str = "op-chaos-1"):
    a = _fresh_auditor_families(["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"])
    # The reject-scoping lane must honor the auditor's master switch (same
    # precedent as the intervention-lock lane) -- turn it ON explicitly here
    # (the shared Slice-8 fixture constructs with it OFF).
    a.lineage_scoping_enabled = True
    a.lineage = auditor_mod.OpLineageGraph(
        ["backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"],
    )
    # Register the chaos op so the lineage is non-empty and scoping is live.
    # Real node-ingestion API is OpLineageGraph.observe_op(op_id, target_files=...).
    a.lineage.observe_op(
        chaos_op,
        target_files=[
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
        ],
    )
    # The unrelated Run-19 background op was OBSERVED by the graph with its
    # own (non-chaos) target files -- a reject may only be excused for an op
    # the graph provably knows.
    a.lineage.observe_op(
        _RUN19_FULL_OP,
        target_files=["backend/core/some/unrelated_module.py"],
    )
    return a


def test_unrelated_op_reject_does_not_poison(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    assert a.lineage.in_chaos_lineage("op-chaos-1") is True
    assert (
        a.lineage.in_chaos_lineage("op-019f58a7-f623-756b-bf73-0b4309aaaaaa")
        is False
    )
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_chaos_lineage_op_reject_still_poisons(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage(
        chaos_op="op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
    )
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_lineage_scoping_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", "false")
    a = _auditor_with_chaos_lineage()
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_resumed_ops_path_unchanged_by_lineage_flag(monkeypatch):
    """Resumed-ops scoping (Slice-6-adjacent, pre-existing) must not be
    touched by the new chaos-lineage lane: with resumed_ops set, an op
    outside that set is unrelated regardless of JARVIS_A1_AUDIT_
    LINEAGE_SCOPED_REJECTS, and chaos-lineage membership is never consulted
    (the resumed-ops branch is mutually exclusive with the chaos-lineage
    branch -- see the ``not self.resumed_ops`` guard)."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage(
        chaos_op="op-019f58a7-f623-756b-bf73-0b4309aaaaaa"
    )
    # Even though the rejecting op IS in the chaos lineage, a resumed-ops
    # scope that excludes it must still mark it unrelated (resumed_ops wins).
    a.resumed_ops = {"op-some-other-resumed-op": {"source": "test"}}
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_unattributable_reject_stays_globally_correlated(monkeypatch):
    """A reject line with no parseable op id fails CLOSED: it still poisons,
    even with chaos-lineage scoping enabled and a live chaos manifest."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    no_op_line = (
        "2026-07-12T16:27:44 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
        "Iron Gate — exploration_insufficient: 0/2 (attempt=1)"
    )
    a._correlate_flag_signal(no_op_line)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


# ---------------------------------------------------------------------------
# Slice 9 review fix — fail-CLOSED node resolution + master-switch gating.
#
# The lane above failed OPEN two ways: (a) in_chaos_lineage's exact
# nodes.get() lookup made a truncated (ctx.op_id[:12]/[:16]) or
# never-observed op id look "outside" the lineage -- a GENUINE reject on the
# audited chaos op itself was excused (false GREEN); (b) the lane ignored the
# auditor's lineage_scoping_enabled master switch that the intervention-lock
# lane honors. A reject may be excused as unrelated ONLY when the op is
# PROVABLY known and PROVABLY outside the chaos lineage.
# ---------------------------------------------------------------------------

_CHAOS_FULL_OP = "op-019f5900-ab12-7cd3-9ef4-0123456789ab"


def test_truncated_reject_id_resolving_to_chaos_op_poisons(monkeypatch):
    """A [:16]-truncated op id that uniquely prefixes the CHAOS op's full
    node id must RESOLVE to it -- the reject is on the audited chaos op
    itself and poisons (the false-GREEN class this fix kills)."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage(chaos_op=_CHAOS_FULL_OP)
    a._correlate_flag_signal(_iron_gate_reject_line(_CHAOS_FULL_OP[:16]))
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_truncated_reject_id_resolving_outside_lineage_excused(monkeypatch):
    """A truncated id that uniquely resolves to a known NON-chaos node is
    provably outside the lineage -> correctly excused. Uses the [:12]
    orchestrator truncation (trailing '-' stripped by _extract_op_id)."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    a._correlate_flag_signal(_iron_gate_reject_line(_RUN19_FULL_OP[:12]))
    assert not any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_unknown_reject_op_fails_closed_and_recorded(monkeypatch):
    """An op id the graph never observed (SSE gap / log-only replay) is
    UNRESOLVED -> NOT excused (poisons, pre-Slice-9 behavior) and the miss
    is recorded in lineage_stitch_failures."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    unknown = "op-ffffffff-dead-beef-0000-111122223333"
    a._correlate_flag_signal(_iron_gate_reject_line(unknown))
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any(
        "flag_reject_op_unresolved" in x and unknown in x
        for x in a.lineage_stitch_failures
    )


def test_ambiguous_truncated_reject_id_fails_closed(monkeypatch):
    """A truncated id that prefixes TWO node keys is AMBIGUOUS -> NOT
    excused (poisons) and recorded in lineage_stitch_failures."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_with_chaos_lineage()
    # A sibling node sharing the Run-19 op's 16-char prefix.
    a.lineage.observe_op(
        "op-019f58a7-f623-9999-8888-777766665555",
        target_files=["backend/core/some/other_unrelated.py"],
    )
    a._correlate_flag_signal(_iron_gate_reject_line(_RUN19_FULL_OP[:16]))
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert any(
        "flag_reject_op_unresolved" in x for x in a.lineage_stitch_failures
    )


def test_master_switch_off_disables_lane_despite_env_flag(monkeypatch):
    """lineage_scoping_enabled=False (constructor / --lineage-scoping) must
    dominate the env flag -- the lane is fully off and every corroborated
    reject poisons, matching the intervention-lock lane's precedent."""
    monkeypatch.setenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", "true")
    a = _auditor_with_chaos_lineage()
    a.lineage_scoping_enabled = False
    a._correlate_flag_signal(_GENUINE_IRON_GATE_REJECT_UNRELATED_OP)
    assert any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )
    assert not a.observed_unrelated_flag_rejects


# ---------------------------------------------------------------------------
# Slice 10 — CommProtocol INTENT lineage stitching (Run #20 false-red).
#
# Run #20 ran in replay_mode=True (log-grep-only, no SSE): unrelated
# background ops never got lineage-graph nodes, so their GENUINE iron_gate
# rejects could not be excused -- resolve_op_id fail-closed correctly, but the
# evidence to stitch those nodes WAS in the debug.log: the op's
# '[CommProtocol] INTENT op=<full-id> seq=1 payload={...}' line carries the
# full op id AND target_files. Teach the log-ingest lane to stitch lineage
# nodes from INTENT lines (fail-soft per line), and make it a PRE-PASS over
# the log so reject correlation is order-independent (false_positive_rejected
# is sticky -- a reject correlated before its op's INTENT line would fail
# closed forever).
# ---------------------------------------------------------------------------

_RUN20_OP_A = "op-019f5a32-80fd-7a59-a84e-62be2e97f179-cau"
_RUN20_OP_B = "op-019f5a32-8108-7bb2-9c01-5f7d2ab90d22-cau"
_UNRELATED_FILE = "backend/core/some/unrelated_module.py"
_CHAOS_FILE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"


def _intent_line(op_id, target_files, seq: int = 1) -> str:
    # Real Run-20 debug.log line shape (Python-literal dict repr payload).
    return (
        "2026-07-12T23:38:39 [backend.core.ouroboros.governance.comm_protocol]"
        " INFO [CommProtocol] INTENT op=%s seq=%d payload={'goal': \"Wave 3"
        " (6) Slice 5b / F1 gra...\", 'target_files': %r,"
        " 'risk_tier': 'SAFE_AUTO'}" % (op_id, seq, list(target_files))
    )


def _auditor_for_intent_stitching(chaos_op: str = "op-chaos-1"):
    a = _fresh_auditor_families(["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"])
    a.lineage_scoping_enabled = True
    a.lineage = auditor_mod.OpLineageGraph([_CHAOS_FILE])
    a.lineage.observe_op(chaos_op, target_files=[_CHAOS_FILE])
    return a


def _iron_gate_poisoned(a) -> bool:
    return any(
        st.false_positive_rejected
        for st in a._by_family.get("iron_gate", ())
    )


def test_intent_stitched_node_excuses_unrelated_reject(monkeypatch):
    """(a) An INTENT line stitches the op's node (non-chaos target_files);
    a later corroborated reject naming the FULL id is excused, with no
    stitch failure recorded."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    a.ingest_log_line(_intent_line(_RUN20_OP_A, [_UNRELATED_FILE]))
    a.ingest_log_line(_iron_gate_reject_line(_RUN20_OP_A))
    assert not _iron_gate_poisoned(a)
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)
    assert not any(
        "flag_reject_op_unresolved" in x for x in a.lineage_stitch_failures
    )


def test_intent_stitched_chaos_op_reject_poisons(monkeypatch):
    """(b) Same, but the INTENT payload's target_files include the chaos
    target -> the op IS the chaos lineage -> the reject poisons."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    a.ingest_log_line(_intent_line(_RUN20_OP_A, [_CHAOS_FILE]))
    a.ingest_log_line(_iron_gate_reject_line(_RUN20_OP_A))
    assert _iron_gate_poisoned(a)


def test_truncated_reject_resolves_to_intent_stitched_node(monkeypatch):
    """(c) A truncated-prefix reject id resolves uniquely to the
    INTENT-stitched node and is excused per its (non-chaos) lineage."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    a.ingest_log_line(_intent_line(_RUN20_OP_A, [_UNRELATED_FILE]))
    a.ingest_log_line(_iron_gate_reject_line(_RUN20_OP_A[:16]))
    assert not _iron_gate_poisoned(a)
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)


def test_unparseable_intent_payload_fails_soft_then_closed(monkeypatch):
    """(d) An INTENT line truncated mid-dict is skipped WITHOUT raising; the
    op stays unresolved -> the reject falls back to fail-closed poison and
    the miss is recorded in lineage_stitch_failures."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    truncated = _intent_line(_RUN20_OP_A, [_UNRELATED_FILE])[:-40]
    assert "payload={" in truncated  # truncation landed mid-dict
    a.ingest_log_line(truncated)  # must NOT raise
    a.ingest_log_line(_iron_gate_reject_line(_RUN20_OP_A))
    assert _iron_gate_poisoned(a)
    assert any(
        "flag_reject_op_unresolved" in x and _RUN20_OP_A in x
        for x in a.lineage_stitch_failures
    )


def test_non_seq1_intent_lines_are_ignored(monkeypatch):
    """Only seq=1 INTENT lines stitch lineage; a seq=2 line does not create
    a node, so the reject stays fail-closed."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    a.ingest_log_line(_intent_line(_RUN20_OP_A, [_UNRELATED_FILE], seq=2))
    a.ingest_log_line(_iron_gate_reject_line(_RUN20_OP_A))
    assert _iron_gate_poisoned(a)


def test_run20_ambiguous_prefix_still_fails_closed(monkeypatch):
    """(e) The Run-20 ambiguity case: two INTENT-stitched ops share the
    12-char prefix op-019f5a32 -- a truncated reject id stays AMBIGUOUS ->
    fail-closed poison + stitch failure."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    a = _auditor_for_intent_stitching()
    a.ingest_log_line(_intent_line(_RUN20_OP_A, [_UNRELATED_FILE]))
    a.ingest_log_line(_intent_line(_RUN20_OP_B, [_UNRELATED_FILE]))
    a.ingest_log_line(_iron_gate_reject_line("op-019f5a32"))
    assert _iron_gate_poisoned(a)
    assert any(
        "flag_reject_op_unresolved" in x for x in a.lineage_stitch_failures
    )


def test_intent_prepass_makes_reject_order_independent(monkeypatch, tmp_path):
    """(3) Order-independence: the streaming lane ingests strictly in file
    order with NO pre-pass and false_positive_rejected is sticky, so a
    reject line appearing BEFORE its op's INTENT line poisons forever.
    prestitch_intent_lineage scans the log for INTENT nodes BEFORE reject
    correlation runs, making the excusal order-independent."""
    monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS", raising=False)
    reject = _iron_gate_reject_line(_RUN20_OP_A)
    intent = _intent_line(_RUN20_OP_A, [_UNRELATED_FILE])
    log = tmp_path / "debug.log"
    log.write_text(reject + "\n" + intent + "\n", encoding="utf-8")

    # Counterfactual: without the pre-pass, reject-before-INTENT poisons.
    a_bare = _auditor_for_intent_stitching()
    for line in log.read_text(encoding="utf-8").splitlines():
        a_bare.ingest_log_line(line)
    assert _iron_gate_poisoned(a_bare)

    # With the pre-pass, the same stream is excused.
    a = _auditor_for_intent_stitching()
    a.prestitch_intent_lineage(str(log))
    for line in log.read_text(encoding="utf-8").splitlines():
        a.ingest_log_line(line)
    assert not _iron_gate_poisoned(a)
    assert any("iron_gate" in x for x in a.observed_unrelated_flag_rejects)
