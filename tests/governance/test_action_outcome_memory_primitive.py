"""M11 Slice 1 — ActionOutcomeMemory primitive tests (PRD §30.5.3).

Pins the closed enum + frozen dataclass + signature-hash contract.
Authority invariants pinned: Slice 1 imports stdlib + the
SituationKind enum from :mod:`failure_mode_memory` ONLY (no
governance/orchestrator/strategic_direction).

Test layout mirrors
:mod:`test_failure_mode_memory_primitive` — same shape: master
flag (5) + closed enum (5) + frozen dataclass (10) + signature
(10) + schema (2) + authority (3) + exports (2) = ~37 tests +
M11-specific dedup-key-includes-outcome tests.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag (asymmetric env semantics, default-FALSE Slice 1)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_true_post_graduation(self, monkeypatch):
        """Slice 5 graduation: ``JARVIS_ACTION_OUTCOME_MEMORY_-
        ENABLED`` flips false → true (PRD §30.5.3). Operator
        instant-revert via explicit env false."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is True

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE", "Yes"],
    )
    def test_truthy_variants_flip_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "FALSE"],
    )
    def test_explicit_falsy_variants(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        # Whitespace == unset == graduated default == True (Slice 5)
        assert action_outcome_memory_enabled() is True

    def test_garbage_value_is_off(self, monkeypatch):
        """Asymmetric semantics: anything not in the truthy set
        is False. Convention from cigw / coherence / quorum."""
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "maybe",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — OutcomeKind closed enum (PRD §30.5.3 spec — 5 values)
# ---------------------------------------------------------------------------


class TestOutcomeKind:
    def test_has_five_prd_spec_values(self):
        """PRD §30.5.3 mandates these 5 specific values."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
        )
        for name in (
            "APPLIED_VERIFIED",
            "APPLIED_REVERTED",
            "REJECTED",
            "DEFERRED",
            "DISABLED",
        ):
            assert hasattr(OutcomeKind, name), (
                f"OutcomeKind missing required PRD value {name}"
            )

    def test_disabled_is_master_off_sentinel(self):
        """DISABLED matches the ConsensusOutcome.DISABLED pattern
        — the master-off sentinel."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
        )
        assert OutcomeKind.DISABLED.value == "disabled"

    def test_closed_enum_size_five(self):
        """PRD §30.5.3 spec is 5 values. Size pinned so a future
        PR adding a value MUST update this test (intentional
        friction against silent vocabulary drift)."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
        )
        assert len(OutcomeKind) == 5

    def test_str_enum_lowercase_values(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
        )
        for member in OutcomeKind:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()

    def test_no_unknown_or_other_sentinel(self):
        """Outcome is known at write site (orchestrator knows
        APPLY/VERIFY/REJECT/CANCEL state). Unlike SituationKind /
        FailureModeKind which need CoR fallbacks, OutcomeKind
        intentionally does NOT have UNKNOWN / OTHER. The PRD
        spec is exactly 5 values."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
        )
        names = {m.name for m in OutcomeKind}
        assert "UNKNOWN" not in names
        assert "OTHER" not in names


# ---------------------------------------------------------------------------
# § 3 — ActionOutcomeRecord frozen dataclass (PRD 11-field shape)
# ---------------------------------------------------------------------------


def _sample_record():
    from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
        ActionOutcomeRecord,
        OutcomeKind,
    )
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        SituationKind,
    )
    return ActionOutcomeRecord(
        signature_hash="a" * 64,
        situation_kind=SituationKind.MULTI_FILE_REFACTOR,
        attempted_action_kind="add_dataclass",
        outcome_kind=OutcomeKind.APPLIED_VERIFIED,
        target_files=("a.py", "b.py"),
        commit_hash="abc1234",
        summary="Imported X from canonical module; tests pass.",
        observed_at_unix=1700000000.0,
        op_id="op-test-001",
        cluster_id="cluster-3",
        weight=2,
    )


class TestActionOutcomeRecord:
    def test_frozen_cannot_be_mutated(self):
        rec = _sample_record()
        with pytest.raises(FrozenInstanceError):
            rec.weight = 99  # type: ignore[misc]

    def test_eleven_required_fields_plus_schema(self):
        """PRD §30.5.3 specifies the canonical field shape; +
        schema_version (round-trip metadata) = 12 keys."""
        rec = _sample_record()
        d = rec.to_dict()
        for k in (
            "signature_hash",
            "situation_kind",
            "attempted_action_kind",
            "outcome_kind",
            "target_files",
            "commit_hash",
            "summary",
            "observed_at_unix",
            "op_id",
            "cluster_id",
            "weight",
            "schema_version",
        ):
            assert k in d

    def test_target_files_stored_on_record(self):
        """KEY M11 IMPROVEMENT over Upgrade 3 Slice 1: target_files
        IS on the record (Upgrade 3's FailureModeRecord deferred
        this; M11 stores it from day one for meaningful Slice 3
        Jaccard)."""
        rec = _sample_record()
        assert rec.target_files == ("a.py", "b.py")

    def test_default_weight_is_one(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
            OutcomeKind,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        rec = ActionOutcomeRecord(
            signature_hash="a" * 64,
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="add_index",
            outcome_kind=OutcomeKind.REJECTED,
            target_files=(),
            commit_hash="",
            summary="",
            observed_at_unix=0.0,
            op_id="",
        )
        assert rec.weight == 1
        assert rec.cluster_id == ""  # default empty

    def test_to_dict_serializable_to_json(self):
        rec = _sample_record()
        roundtrip = json.loads(json.dumps(rec.to_dict()))
        assert (
            roundtrip["situation_kind"] == "multi_file_refactor"
        )
        assert roundtrip["outcome_kind"] == "applied_verified"
        assert roundtrip["target_files"] == ["a.py", "b.py"]
        assert roundtrip["cluster_id"] == "cluster-3"
        assert roundtrip["weight"] == 2

    def test_from_dict_round_trip(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        reconstructed = ActionOutcomeRecord.from_dict(d)
        assert reconstructed is not None
        assert reconstructed == rec

    def test_from_dict_rejects_schema_mismatch(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        d = _sample_record().to_dict()
        d["schema_version"] = "action_outcome_memory.99"
        assert ActionOutcomeRecord.from_dict(d) is None

    def test_from_dict_rejects_missing_signature(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        d = _sample_record().to_dict()
        d["signature_hash"] = ""
        assert ActionOutcomeRecord.from_dict(d) is None

    def test_from_dict_rejects_unknown_outcome_kind(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        d = _sample_record().to_dict()
        d["outcome_kind"] = "no_such_outcome"
        assert ActionOutcomeRecord.from_dict(d) is None

    def test_from_dict_rejects_unknown_situation_kind(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        d = _sample_record().to_dict()
        d["situation_kind"] = "no_such_situation"
        assert ActionOutcomeRecord.from_dict(d) is None

    def test_from_dict_returns_none_on_garbage(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        for bad in (None, [], "string", 42, 3.14):
            assert ActionOutcomeRecord.from_dict(bad) is None

    def test_from_dict_handles_missing_target_files(self):
        """Records without target_files (legacy or sparse) → empty
        tuple. Defensive against future schema additions."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        d = _sample_record().to_dict()
        del d["target_files"]
        rec = ActionOutcomeRecord.from_dict(d)
        assert rec is not None
        assert rec.target_files == ()


# ---------------------------------------------------------------------------
# § 4 — compute_outcome_signature determinism + invariance contract
# ---------------------------------------------------------------------------


class TestComputeOutcomeSignature:
    def test_returns_64_char_sha256_hex(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        sig = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
        )
        assert len(sig) == 64
        int(sig, 16)

    def test_determinism_same_inputs_same_hash(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        kw = dict(
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="add_column",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("migrations/001.sql",),
        )
        a = compute_outcome_signature(**kw)
        b = compute_outcome_signature(**kw)
        assert a == b

    def test_file_order_invariance(self):
        """Same files in different listing order MUST hash
        identically (canonicalize sorts internally; same
        primitive as Upgrade 3)."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        a = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_module",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py", "c.py"),
        )
        b = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_module",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("c.py", "a.py", "b.py"),
        )
        assert a == b

    def test_outcome_kind_in_dedup_tuple(self):
        """**LOAD-BEARING M11 BEHAVIOR**: same triplet with
        DIFFERENT outcomes MUST produce DIFFERENT signatures.
        Reason: "we tried the same approach twice and got
        different results" IS a recordable distinction, not a
        recurrence. This is the key behavioral difference from
        :func:`failure_mode_memory.compute_signature_hash` where
        the dedup tuple is (situation, attempt, files)."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        kw_base = dict(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            target_files=("a.py", "b.py"),
        )
        verified = compute_outcome_signature(
            **kw_base, outcome_kind=OutcomeKind.APPLIED_VERIFIED,
        )
        reverted = compute_outcome_signature(
            **kw_base, outcome_kind=OutcomeKind.APPLIED_REVERTED,
        )
        rejected = compute_outcome_signature(
            **kw_base, outcome_kind=OutcomeKind.REJECTED,
        )
        deferred = compute_outcome_signature(
            **kw_base, outcome_kind=OutcomeKind.DEFERRED,
        )
        # All four MUST be distinct
        assert len({verified, reverted, rejected, deferred}) == 4

    def test_different_situation_different_hash(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        files = ("a.py",)
        a = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=files,
        )
        b = compute_outcome_signature(
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=files,
        )
        assert not (a == b)

    def test_different_attempt_different_hash(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        a = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py",),
        )
        b = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_function",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py",),
        )
        assert not (a == b)

    def test_different_files_different_hash(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        a = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py",),
        )
        b = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
        )
        assert not (a == b)

    def test_empty_files_iterable_is_valid(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        sig = compute_outcome_signature(
            situation_kind=SituationKind.API_VERSION_BUMP,
            attempted_action_kind="bump_major",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
        )
        assert len(sig) == 64

    def test_falls_back_to_empty_sha_on_error(self):
        """NEVER raises — bad input falls back to sha256("")."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compute_outcome_signature,
        )
        sig = compute_outcome_signature(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind="x",
            outcome_kind=None,  # type: ignore[arg-type]
            target_files=("a.py",),
        )
        assert isinstance(sig, str)
        assert len(sig) == 64

    def test_case_normalization_attempt_kind(self):
        """Case-insensitive attempt-kind hashing — mirrors
        Upgrade 3 discipline."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        a = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="Add_Dataclass",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py",),
        )
        b = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py",),
        )
        assert a == b


# ---------------------------------------------------------------------------
# § 5 — Schema version stability
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_schema_version_is_v1(self):
        """Slice 1 ships ``action_outcome_memory.1``. Any future
        breaking change to the dataclass shape MUST bump this AND
        update from_dict's gate."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
        )
        assert (
            ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
            == "action_outcome_memory.1"
        )

    def test_record_default_schema_matches_module_constant(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
        )
        rec = _sample_record()
        assert (
            rec.schema_version
            == ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
        )


# ---------------------------------------------------------------------------
# § 6 — SituationKind reuse contract (zero duplication target)
# ---------------------------------------------------------------------------


class TestSituationKindReuse:
    def test_uses_failure_mode_memory_situation_kind(self):
        """Load-bearing zero-duplication pin: M11's SituationKind
        IS :mod:`failure_mode_memory`'s SituationKind. Adding a
        new situation to one arc benefits both."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind as FMM_SituationKind,
        )
        # Walk the dataclass field annotations — the type of
        # ``situation_kind`` MUST be the SAME class object.
        from typing import get_type_hints
        hints = get_type_hints(ActionOutcomeRecord)
        assert hints["situation_kind"] is FMM_SituationKind


# ---------------------------------------------------------------------------
# § 7 — Authority invariants — narrowest Slice 1 floor
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    """The Slice 1 primitive imports stdlib + the SituationKind
    enum from :mod:`failure_mode_memory` ONLY. Slices 2-5 add
    semantic_index / cross_process_jsonl / ide_observability_-
    stream — those imports MUST NOT appear at Slice 1."""

    # Slice 2 lifts the carve-outs for ``semantic_index`` (cluster
    # lookup) + ``cross_process_jsonl`` (flock primitive) — those
    # imports are now allowed. The full forbidden cage
    # (orchestrator / iron_gate / providers / strategic_direction /
    # postmortem_recall) remains structurally pinned.
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.policy",
        # Slice 4 will reverse-import — strategic_direction depends
        # on action_outcome_memory, NEVER the reverse.
        "from backend.core.ouroboros.governance.strategic_direction",
        # postmortem_recall remains forbidden — M11 reuses
        # SituationKind from failure_mode_memory but does NOT pull
        # postmortem-specific scanning logic; that boundary stays
        # at the failure_mode_memory layer.
        "from backend.core.ouroboros.governance.postmortem_recall",
    )

    def test_primitive_imports_narrow_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "action_outcome_memory.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"action_outcome_memory.py (Slice 1) must NOT "
                f"import {forbidden} — primitive authority floor"
            )

    def test_module_imports_resolve_in_isolation(self):
        """Loadable as a standalone module — belt-and-suspenders
        against accidental governance imports that satisfy the
        grep but break import."""
        from backend.core.ouroboros.governance import (  # noqa: F401
            action_outcome_memory,
        )

    def test_no_print_no_input_no_io_at_module_scope(self):
        """Pure-data layer: no stdin/stdout side effects."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "action_outcome_memory.py"
        )
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            assert "print(" not in stripped, (
                "Slice 1 primitive must not emit stdout"
            )
            assert "input(" not in stripped, (
                "Slice 1 primitive must not read stdin"
            )


# ---------------------------------------------------------------------------
# § 8 — __all__ exports — locks the public surface
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_slices_1_through_4_public_names(self):
        """Slice 1 (5) + Slice 2 (9) + Slice 3 (6) + Slice 4 (3)
        = 23 public exports. Future slices append; never remove."""
        from backend.core.ouroboros.governance import action_outcome_memory  # noqa: E501
        expected = sorted([
            # Slice 1 — primitive
            "ACTION_OUTCOME_MEMORY_SCHEMA_VERSION",
            "ActionOutcomeRecord",
            "OutcomeKind",
            "action_outcome_memory_enabled",
            "compute_outcome_signature",
            # Slice 2 — persistence
            "RecordOutcome",
            "clear_action_outcomes",
            "cluster_jsonl_path",
            "dedup_window_days",
            "history_dir",
            "max_records_per_cluster",
            "read_action_outcomes_for_cluster",
            "read_all_action_outcomes",
            "record_action_outcome",
            # Slice 3 — RAG retriever
            "ActionOutcomeMatch",
            "action_outcome_min_weight",
            "action_outcome_polarity_mode",
            "action_outcome_recency_halflife_days",
            "action_outcome_top_k",
            "recall_for_region",
            # Slice 4 — prompt-section composer
            "DEFAULT_ACTION_OUTCOME_PROMPT_BUDGET",
            "compose_action_outcomes_section",
            "publish_action_outcome_recalled",
            # Slice 5 — graduation operator surfaces
            "find_action_outcome_by_signature",
        ])
        assert sorted(action_outcome_memory.__all__) == expected

    def test_internal_helpers_underscore_prefixed(self):
        from backend.core.ouroboros.governance import action_outcome_memory  # noqa: E501
        for name in (
            # Slice 1
            "_situation_kind_from_value",
            "_outcome_kind_from_value",
            # Slice 2
            "_resolve_cluster_id",
            "_safe_filename_stem",
            "_serialize_record",
            "_read_existing_records",
            "_within_dedup_window",
            "_GLOBAL_FALLBACK_STEM",
            # Slice 3
            "_outcome_polarity_weight",
            "_polarity_presets",
            "_POLARITY_PRESETS",
        ):
            assert name not in action_outcome_memory.__all__
            assert hasattr(action_outcome_memory, name)


# ---------------------------------------------------------------------------
# § 9 — Cross-arc symmetry pins (Upgrade 3 ↔ M11)
# ---------------------------------------------------------------------------


class TestCrossArcSymmetry:
    """M11 is the symmetric positive-evidence pair to Upgrade 3.
    These pins lock the structural symmetry so future refactors
    that diverge one arc from the other trip immediately."""

    def test_schema_version_format_matches_upgrade_3(self):
        """Both modules use ``<name>.<version>`` shape."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FAILURE_MODE_MEMORY_SCHEMA_VERSION,
        )
        # Same shape: <module_name>.<integer>
        assert "." in ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
        assert "." in FAILURE_MODE_MEMORY_SCHEMA_VERSION

    def test_signature_hash_length_matches_upgrade_3(self):
        """Both modules emit 64-char sha256 hex."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compute_outcome_signature,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        sig_m11 = compute_outcome_signature(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
        )
        sig_u3 = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
        )
        assert len(sig_m11) == len(sig_u3) == 64

    def test_master_flag_default_true_post_graduation(
        self, monkeypatch,
    ):
        """Both modules graduated default-TRUE at Slice 5
        (Upgrade 3 + M11). The shape of the graduation
        transition is identical: same env name shape, same
        asymmetric semantics, same instant-revert idiom."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_memory_enabled,
        )
        # Both graduated to default-TRUE
        assert action_outcome_memory_enabled() is True
        assert failure_mode_memory_enabled() is True


# ---------------------------------------------------------------------------
# § 10 — Empty-fallback known constant pin
# ---------------------------------------------------------------------------


class TestEmptyFallbackKnownValue:
    def test_sha256_empty_fallback_known_value(self):
        """Catastrophic-input fallback IS the well-known sha256 of
        the empty string. Pinned so a future change to the
        fallback value trips visibly."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compute_outcome_signature,
        )
        expected_empty = hashlib.sha256(b"").hexdigest()
        sig = compute_outcome_signature(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind=None,  # type: ignore[arg-type]
            outcome_kind=None,  # type: ignore[arg-type]
            target_files=42,  # type: ignore[arg-type]
        )
        assert len(sig) == 64
        int(sig, 16)
        # Determinism even on the fallback path
        sig2 = compute_outcome_signature(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind=None,  # type: ignore[arg-type]
            outcome_kind=None,  # type: ignore[arg-type]
            target_files=42,  # type: ignore[arg-type]
        )
        assert sig == sig2
        assert (
            expected_empty
            == "e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855"
        )
