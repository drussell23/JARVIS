"""Slice 14 — post-8b durability probe + value-gate honesty riders (RED).

Run-23 final red: the auditor's ``fsm_classify_to_applied`` requires the
literal ``[Slice74Probe] LEDGER_TERMINAL ... written=True`` (auditor
:620-635), but every probe for a mutating op fired ``written=False`` — the
``written`` boolean is the LEDGER DEDUP result, and the terminal APPLIED
row is always a duplicate of change_engine's earlier row. Meanwhile the
mutation had demonstrably landed on the operator tree (abbddeec24).

Fix (mandates 1+3): RE-EMIT the SAME probe schema strictly AFTER Phase 8b
(AutoCommit + WorkspacePromoter) fully resolves — no sleeps, no new logging
sequence — with ``written`` = the true durable state: a commit hash exists
AND promotion either succeeded or was legitimately not attempted (master
off / same root / no net change). Mandate 4: an attempted-but-failed or
aborted promotion re-emits ``written=False``.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from backend.core.ouroboros.governance.orchestrator import Orchestrator

_REPO = Path(__file__).resolve().parents[2]


def _outcome(attempted, promoted, state):
    return SimpleNamespace(attempted=attempted, promoted=promoted, state=state)


class TestDurabilityTruthTable:
    def _durable(self, committed_hash, promo):
        return Orchestrator._terminal_durability(committed_hash, promo)

    def test_committed_and_promoted_is_durable(self):
        assert self._durable("abc123", _outcome(True, True, "promoted")) is True

    def test_committed_promotion_master_off_is_durable(self):
        # Legacy posture: the workspace commit IS the durable artifact.
        assert self._durable("abc123", _outcome(False, False, "disabled")) is True

    def test_committed_same_root_noop_is_durable(self):
        assert self._durable(
            "abc123", _outcome(False, False, "noop_same_root"),
        ) is True

    def test_promotion_attempted_but_refused_is_not_durable(self):
        # Mandate 4: refused/aborted promotion → written=False, absolutely.
        for state in ("target_dirty", "conflict_aborted", "live_work_active",
                      "no_commit", "git_failure"):
            assert self._durable(
                "abc123", _outcome(True, False, state),
            ) is False, state

    def test_no_commit_hash_is_not_durable(self):
        assert self._durable(None, _outcome(True, True, "promoted")) is False
        assert self._durable("", None) is False

    def test_probe_line_matches_auditor_regex(self):
        """DRY (mandate 3): the re-emitted line must satisfy the auditor's
        OWN compiled pattern — same schema, no new logging sequence."""
        spec = importlib.util.spec_from_file_location(
            "a1_auditor_for_probe",
            _REPO / "scripts" / "a1_graduation_auditor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("a1_auditor_for_probe", mod)
        spec.loader.exec_module(mod)
        auditor_pattern = None
        for name in dir(mod):
            val = getattr(mod, name)
            if isinstance(val, re.Pattern) and "LEDGER_TERMINAL" in val.pattern:
                auditor_pattern = val
                break
        assert auditor_pattern is not None
        sample = Orchestrator._render_durability_probe(
            "op-019f5e7f-0326-7ebf-a9ee-e359bf390400-sig", True,
        )
        m = auditor_pattern.search(sample)
        assert m is not None, sample
        assert m.group("written") == "True"
        assert m.group("state") == "applied"


class TestPost8bWiring:
    RUNNER = (
        _REPO
        / "backend/core/ouroboros/governance/phase_runners/slice4b_runner.py"
    )
    ORCH = _REPO / "backend/core/ouroboros/governance/orchestrator.py"

    def _promo_block(self, path):
        src = path.read_text()
        i = src.index("Phase 8b-p: Workspace promotion")
        j = src.index("Phase 8b2", i)
        return src[i:j]

    def test_runner_emits_probe_after_8b(self):
        block = self._promo_block(self.RUNNER)
        assert "_emit_terminal_durability_probe" in block, (
            "the live Slice4bRunner must re-emit the durability probe "
            "strictly after AutoCommit + promotion resolve (mandate 1)"
        )

    def test_inline_twin_emits_probe_after_8b(self):
        block = self._promo_block(self.ORCH)
        assert "_emit_terminal_durability_probe" in block, (
            "the inline orchestrator twin must carry the same re-emission "
            "(T5 lesson — both paths)"
        )

    def test_fail_branch_emits_written_false(self):
        """Mandate 4: the promotion-refused branch must re-emit BEFORE the
        fail return, registering written=False explicitly."""
        for path in (self.RUNNER, self.ORCH):
            block = self._promo_block(path)
            fail_idx = block.index("promotion_failed")
            assert "_emit_terminal_durability_probe" in block[:fail_idx] or \
                block.count("_emit_terminal_durability_probe") >= 2, (
                    f"{path.name}: refused promotion must still emit the "
                    "probe (written=False) — silence is the Run-23 class"
                )


class TestValueGateVerdictLogging:
    ORCH = _REPO / "backend/core/ouroboros/governance/orchestrator.py"

    def test_pass_through_is_never_silent(self):
        """Mandate 2 (Run-23 catch): the gate evaluated the noise op and
        passed it WITHOUT A TRACE. Every evaluation must log its verdict
        and per-file reasoning at DEBUG."""
        src = self.ORCH.read_text()
        i = src.index("evaluate_candidate_value")
        window = src[i:i + 4000]
        assert "[ValueGate] verdict" in window, (
            "the seam must log verdict + per-file detail for EVERY "
            "evaluation, including substantive/indeterminate pass-throughs"
        )
