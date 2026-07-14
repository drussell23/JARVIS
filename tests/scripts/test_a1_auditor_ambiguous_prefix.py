"""Slice 11 Task 5 — two-sided UUIDv7 flag-audit fix (RED first).

Run-21 false-red: the Iron Gate reject line logged ``ctx.op_id[:12]`` ->
``op=op-019f5a91-`` — truncated exactly at the UUIDv7 same-millisecond
boundary, ambiguous between two background ops born 27ms apart. W4's
``resolve_op_id`` correctly failed closed and poisoned 3 iron_gate flags,
while the SAME rejection's full-id sibling log line was excused.

Fix, both sides of the contract:
  (emit)  audit-keyed REJECT lines log the FULL op id — pinned by a sweep
          over the _FAMILY_SIGNALS rejected-marker emit sites in
          orchestrator.py;
  (audit) ``resolve_op_candidates`` + the bulletproof rule: an ambiguous
          prefix is excusable ONLY when EVERY candidate deterministically
          resolves outside the chaos lineage; any in-lineage candidate or
          an empty candidate set poisons exactly as before (fail-closed
          preserved).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "a1_graduation_auditor", _REPO / "scripts" / "a1_graduation_auditor.py",
)
assert _spec is not None and _spec.loader is not None
auditor_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("a1_graduation_auditor", auditor_mod)
_spec.loader.exec_module(auditor_mod)

CHAOS_FILE = "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py"
FULL_A = "op-019f5a91-5b4e-752e-bf0f-dbf0693db4e6-cau"
FULL_B = "op-019f5a91-5b69-7da4-a200-b6e1e2770e0e-cau"
CHAOS_OP = "op-019f5a92-e0b4-71d9-9796-18d4a243e95d-sig"

# The REAL Run-21 poisoning line (op id truncated by the [:12] emit).
RUN21_TRUNCATED_REJECT = (
    "2026-07-13T01:22:16 [Ouroboros.Orchestrator] WARNING [Orchestrator] "
    "Iron Gate — exploration_insufficient: 0/2 (attempt=1 cumulative, "
    "preloaded=0) for op=op-019f5a91-"
)


def _auditor_with_lineage(*, chaos_op_in_prefix: bool = False):
    a = auditor_mod.A1GraduationAuditor(
        flags=["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"],
        strict=True,
        chaos_manifest_path=None,
        lineage_scoping_enabled=True,
    )
    a.lineage.chaos_files = [CHAOS_FILE]
    a.lineage.observe_op(FULL_A, target_files=["requirements.txt"])
    if chaos_op_in_prefix:
        # Make the SECOND prefix-sharing op a chaos-lineage member.
        a.lineage.observe_op(FULL_B, target_files=[CHAOS_FILE])
    else:
        a.lineage.observe_op(FULL_B, target_files=["docs/x.md"])
    a.lineage.observe_op(CHAOS_OP, target_files=[CHAOS_FILE])
    return a


def _iron_gate_states(a):
    return list(a._by_family.get("iron_gate", ()))


class TestResolveOpCandidates:
    def test_exact_match(self):
        g = auditor_mod.OpLineageGraph([CHAOS_FILE])
        g.observe_op(FULL_A)
        assert g.resolve_op_candidates(FULL_A) == (FULL_A,)

    def test_unique_prefix(self):
        g = auditor_mod.OpLineageGraph([CHAOS_FILE])
        g.observe_op(FULL_A)
        assert g.resolve_op_candidates("op-019f5a91-5b4e") == (FULL_A,)

    def test_ambiguous_prefix_returns_all_sorted(self):
        g = auditor_mod.OpLineageGraph([CHAOS_FILE])
        g.observe_op(FULL_B)
        g.observe_op(FULL_A)
        assert g.resolve_op_candidates("op-019f5a91") == tuple(
            sorted([FULL_A, FULL_B])
        )

    def test_too_short_and_unknown_are_empty(self):
        g = auditor_mod.OpLineageGraph([CHAOS_FILE])
        g.observe_op(FULL_A)
        assert g.resolve_op_candidates("op-01") == ()
        assert g.resolve_op_candidates("op-ffffffff") == ()
        assert g.resolve_op_candidates(None) == ()


class TestAmbiguousPrefixExcusal:
    def test_all_candidates_outside_lineage_excused(self, monkeypatch):
        """THE Run-21 case: both prefix-sharing ops are background ops
        outside the chaos lineage -> the reject is provably unrelated no
        matter which one emitted it -> excused, flags stay green."""
        monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS",
                           raising=False)
        a = _auditor_with_lineage(chaos_op_in_prefix=False)
        a._correlate_flag_signal(RUN21_TRUNCATED_REJECT)
        assert not any(
            st.false_positive_rejected for st in _iron_gate_states(a)
        ), "ambiguous-but-all-outside must not poison (Run-21 false-red)"
        assert any(
            "iron_gate" in x and "ambiguous_all_outside" in x
            for x in a.observed_unrelated_flag_rejects
        ), "the excusal must be recorded, never silently dropped (§7)"
        assert not any(
            "flag_reject_op_unresolved" in x
            for x in a.lineage_stitch_failures
        )

    def test_any_in_lineage_candidate_still_poisons(self, monkeypatch):
        """Fail-closed preserved: if the truncated id COULD denote a
        chaos-lineage op, the reject poisons exactly as before."""
        monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS",
                           raising=False)
        a = _auditor_with_lineage(chaos_op_in_prefix=True)
        a._correlate_flag_signal(RUN21_TRUNCATED_REJECT)
        assert any(
            st.false_positive_rejected for st in _iron_gate_states(a)
        )
        assert any(
            "iron_gate" in x for x in a.lineage_stitch_failures
        ), "the ambiguity must still be recorded when it poisons"

    def test_zero_candidates_still_poisons(self, monkeypatch):
        monkeypatch.delenv("JARVIS_A1_AUDIT_LINEAGE_SCOPED_REJECTS",
                           raising=False)
        a = auditor_mod.A1GraduationAuditor(
            flags=["JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED"],
            strict=True,
            chaos_manifest_path=None,
            lineage_scoping_enabled=True,
        )
        a.lineage.chaos_files = [CHAOS_FILE]
        a.lineage.observe_op(CHAOS_OP, target_files=[CHAOS_FILE])
        a._correlate_flag_signal(RUN21_TRUNCATED_REJECT)
        assert any(
            st.false_positive_rejected for st in _iron_gate_states(a)
        )
        assert any(
            "flag_reject_op_unresolved" in x
            for x in a.lineage_stitch_failures
        )


class TestEmitSideFullOpIds:
    ORCH = _REPO / "backend/core/ouroboros/governance/orchestrator.py"

    def test_iron_gate_reject_logs_full_op_id(self):
        src = self.ORCH.read_text()
        i = src.index("Iron Gate — exploration_insufficient:")
        window = src[i:i + 600]
        assert "ctx.op_id[:12]" not in window and \
            "ctx.op_id[:16]" not in window, (
                "the audit-keyed Iron Gate reject line must log the FULL "
                "op id — [:12] cuts at the UUIDv7 same-millisecond boundary "
                "(Run-21 ambiguous-prefix false-red)"
            )

    def test_no_audit_keyed_reject_marker_emits_truncated_ids(self):
        """Sweep: every logger call whose format string carries a
        _FAMILY_SIGNALS rejected marker must not truncate op ids — across
        orchestrator.py AND every extracted phase runner (Run-22 caught
        generate_runner.py carrying its own [:12] copy of the Iron Gate
        emit; the Slice-6 T5 lesson yet again: the extracted runner IS the
        live path)."""
        markers = (
            "exploration_insufficient",
            "ExplorationInsufficientError",
            "mutation_budget_exhausted",
            "risk_tier_floor_block",
        )
        targets = [self.ORCH] + sorted(
            (_REPO / "backend/core/ouroboros/governance/phase_runners")
            .glob("*.py")
        )
        offenders = []
        for path in targets:
            src = path.read_text()
            for m in markers:
                for hit in re.finditer(re.escape(m), src):
                    window = src[hit.start():hit.start() + 600]
                    if "op_id[:12]" in window or "op_id[:16]" in window:
                        line_no = src[:hit.start()].count("\n") + 1
                        offenders.append(f"{path.name}:{line_no} ({m})")
        assert not offenders, (
            "audit-keyed reject emit sites still truncating op ids: "
            f"{offenders}"
        )
