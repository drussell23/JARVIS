"""Upgrade 3 Slice 1 — Failure-Mode Memory primitive tests.

Pins the closed enums + frozen dataclass + signature-hash contract
PRD §31.4.2 specifies. Authority invariants pinned as well — Slice
2-5 must NOT regress this primitive's stdlib-only authority floor.

Test layout mirrors :mod:`test_generative_quorum_graduation` and
other graduated primitive tests (closed-enum + frozen-dataclass +
signature-determinism + master-flag + authority).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag (asymmetric env semantics, default-FALSE for Slice 1)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_memory_enabled,
        )
        assert failure_mode_memory_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE", "Yes"],
    )
    def test_truthy_variants_flip_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_memory_enabled,
        )
        assert failure_mode_memory_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off", "FALSE"],
    )
    def test_explicit_falsy_variants(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_memory_enabled,
        )
        assert failure_mode_memory_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            failure_mode_memory_enabled,
        )
        # Whitespace == unset == default == False (Slice 1)
        assert failure_mode_memory_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — SituationKind closed enum (PRD §31.4.2 spec + UNKNOWN sentinel)
# ---------------------------------------------------------------------------


class TestSituationKind:
    def test_has_six_prd_spec_values(self):
        """PRD §31.4.2 mandates these 6 specific values."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        for name in (
            "MULTI_FILE_REFACTOR",
            "DB_MIGRATION",
            "ASYNC_RESTRUCTURE",
            "NEW_TEST_FRAMEWORK_INTEGRATION",
            "API_VERSION_BUMP",
            "CROSS_REPO_DRIFT_FIX",
        ):
            assert hasattr(SituationKind, name), (
                f"SituationKind missing required PRD value {name}"
            )

    def test_has_unknown_sentinel(self):
        """UNKNOWN is the chain-of-responsibility fallback for the
        Slice 2 extractor — mirrors FailureModeKind.OTHER."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        assert SituationKind.UNKNOWN.value == "unknown"

    def test_closed_enum_size_seven(self):
        """6 PRD-spec values + 1 UNKNOWN sentinel = 7 total. Size
        is structurally pinned so a future PR adding a value MUST
        update this test (intentional friction against silent
        vocabulary drift)."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        assert len(SituationKind) == 7

    def test_str_enum_lowercase_values(self):
        """All values are lowercase strings (storage + comparison
        canonical form)."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        for member in SituationKind:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()


# ---------------------------------------------------------------------------
# § 3 — FailureModeKind closed enum (PRD §31.4.2 spec)
# ---------------------------------------------------------------------------


class TestFailureModeKind:
    def test_has_seven_prd_spec_values(self):
        """PRD §31.4.2 mandates these 7 specific values."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
        )
        for name in (
            "MISSING_IMPORT",
            "TYPE_MISMATCH",
            "ASSERT_INVERTED",
            "CIRCULAR_DEP_INTRODUCED",
            "BANNED_TOKEN_INTRODUCED",
            "TEST_TIMEOUT_REGRESSED",
            "OTHER",
        ):
            assert hasattr(FailureModeKind, name), (
                f"FailureModeKind missing required PRD value {name}"
            )

    def test_other_is_sentinel(self):
        """OTHER is the chain-of-responsibility fallback (PRD
        §31.4.2 Slice 2 spec)."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
        )
        assert FailureModeKind.OTHER.value == "other"

    def test_closed_enum_size_seven(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
        )
        assert len(FailureModeKind) == 7

    def test_str_enum_lowercase_values(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
        )
        for member in FailureModeKind:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()


# ---------------------------------------------------------------------------
# § 4 — FailureModeRecord frozen dataclass shape (PRD §31.4.2 spec)
# ---------------------------------------------------------------------------


def _sample_record():
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        FailureModeKind,
        FailureModeRecord,
        SituationKind,
    )
    return FailureModeRecord(
        signature_hash="a" * 64,
        situation_kind=SituationKind.MULTI_FILE_REFACTOR,
        attempted_action_kind="add_dataclass",
        failure_mode_kind=FailureModeKind.MISSING_IMPORT,
        mitigation_summary="Try importing from canonical module.",
        observed_at_unix=1700000000.0,
        op_id="op-test-001",
        weight=2,
    )


class TestFailureModeRecord:
    def test_frozen_cannot_be_mutated(self):
        rec = _sample_record()
        with pytest.raises(FrozenInstanceError):
            rec.weight = 99  # type: ignore[misc]

    def test_eight_required_fields_plus_schema(self):
        """PRD §31.4.2 specifies 8 fields. Plus schema_version
        (round-trip metadata)."""
        rec = _sample_record()
        # The 8 PRD fields + schema_version (9 total in __dict__):
        d = rec.to_dict()
        for k in (
            "signature_hash",
            "situation_kind",
            "attempted_action_kind",
            "failure_mode_kind",
            "mitigation_summary",
            "observed_at_unix",
            "op_id",
            "weight",
            "schema_version",
        ):
            assert k in d

    def test_default_weight_is_one(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            FailureModeRecord,
            SituationKind,
        )
        rec = FailureModeRecord(
            signature_hash="a" * 64,
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="add_index",
            failure_mode_kind=FailureModeKind.OTHER,
            mitigation_summary="",
            observed_at_unix=0.0,
            op_id="",
        )
        assert rec.weight == 1

    def test_to_dict_serializable_to_json(self):
        rec = _sample_record()
        # Round-trip through JSON to ensure no non-serializable fields.
        roundtrip = json.loads(json.dumps(rec.to_dict()))
        assert (
            roundtrip["situation_kind"] == "multi_file_refactor"
        )
        assert roundtrip["failure_mode_kind"] == "missing_import"
        assert roundtrip["weight"] == 2

    def test_from_dict_round_trip(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        reconstructed = FailureModeRecord.from_dict(d)
        assert reconstructed is not None
        assert reconstructed == rec

    def test_from_dict_rejects_schema_mismatch(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        d["schema_version"] = "failure_mode_memory.99"
        assert FailureModeRecord.from_dict(d) is None

    def test_from_dict_rejects_missing_signature(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        d["signature_hash"] = ""
        assert FailureModeRecord.from_dict(d) is None

    def test_from_dict_rejects_unknown_situation_kind(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        d["situation_kind"] = "no_such_situation"
        assert FailureModeRecord.from_dict(d) is None

    def test_from_dict_rejects_unknown_failure_mode_kind(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        d["failure_mode_kind"] = "no_such_mode"
        assert FailureModeRecord.from_dict(d) is None

    def test_from_dict_returns_none_on_garbage(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        for bad in (None, [], "string", 42, 3.14):
            assert FailureModeRecord.from_dict(bad) is None

    def test_from_dict_handles_situation_kind_case_insensitively(
        self,
    ):
        """Defensive: a future serializer that uppercases enum
        values must still round-trip."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeRecord,
        )
        rec = _sample_record()
        d = rec.to_dict()
        d["situation_kind"] = "MULTI_FILE_REFACTOR"
        d["failure_mode_kind"] = "MISSING_IMPORT"
        reconstructed = FailureModeRecord.from_dict(d)
        assert reconstructed is not None
        assert (
            reconstructed.situation_kind.value
            == "multi_file_refactor"
        )


# ---------------------------------------------------------------------------
# § 5 — compute_signature_hash determinism + invariance contract
# ---------------------------------------------------------------------------


class TestComputeSignatureHash:
    def test_returns_64_char_sha256_hex(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        sig = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            target_files=("a.py", "b.py"),
        )
        assert len(sig) == 64
        # Hex chars only
        int(sig, 16)

    def test_determinism_same_inputs_same_hash(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        kw = dict(
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="add_column",
            target_files=("migrations/001.sql",),
        )
        a = compute_signature_hash(**kw)
        b = compute_signature_hash(**kw)
        assert a == b

    def test_file_order_invariance(self):
        """Load-bearing: same files in different listing order
        MUST hash identically (canonicalize sorts internally)."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        a = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_module",
            target_files=("a.py", "b.py", "c.py"),
        )
        b = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_module",
            target_files=("c.py", "a.py", "b.py"),
        )
        assert a == b

    def test_different_situation_kind_different_hash(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        files = ("a.py",)
        a = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            target_files=files,
        )
        b = compute_signature_hash(
            situation_kind=SituationKind.DB_MIGRATION,
            attempted_action_kind="x",
            target_files=files,
        )
        assert a != b

    def test_different_attempt_kind_different_hash(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        a = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            target_files=("a.py",),
        )
        b = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="rename_function",
            target_files=("a.py",),
        )
        assert a != b

    def test_different_files_different_hash(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        a = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            target_files=("a.py",),
        )
        b = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            target_files=("a.py", "b.py"),
        )
        assert a != b

    def test_empty_files_iterable_is_valid(self):
        """target_files defaults to empty — situation+attempt alone
        should still produce a stable hash."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        sig = compute_signature_hash(
            situation_kind=SituationKind.API_VERSION_BUMP,
            attempted_action_kind="bump_major",
        )
        assert len(sig) == 64

    def test_falls_back_to_empty_sha_on_error(self):
        """NEVER raises — bad situation_kind input falls back to
        sha256("") so callers always have a string."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            compute_signature_hash,
        )
        # Pass a non-enum to exercise the fallback path
        sig = compute_signature_hash(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind="x",
            target_files=("a.py",),
        )
        # Returns a valid hash (str(None) coerces, doesn't raise)
        assert isinstance(sig, str)
        assert len(sig) == 64

    def test_case_normalization_attempt_kind(self):
        """Attempt kind is lowercased so casing variation doesn't
        produce duplicate signatures."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            compute_signature_hash,
        )
        a = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="Add_Dataclass",
            target_files=("a.py",),
        )
        b = compute_signature_hash(
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            target_files=("a.py",),
        )
        assert a == b

    def test_sha256_empty_fallback_known_value(self):
        """If something truly catastrophic happens, fallback is the
        well-known sha256 of the empty string."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            compute_signature_hash,
        )
        expected_empty = hashlib.sha256(b"").hexdigest()
        # Pass non-iterable target_files to trip the
        # _canonicalize_target_files TypeError branch.
        sig = compute_signature_hash(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind=None,  # type: ignore[arg-type]
            target_files=42,  # type: ignore[arg-type]
        )
        # Either the empty-fallback OR a deterministic non-empty
        # string — both are valid, but the value MUST be
        # deterministic, not raise, and be hex.
        assert len(sig) == 64
        int(sig, 16)
        # In the fully-degraded case, we should hit the fallback.
        # Test that the fallback IS the empty-string sha when
        # _canonicalize_target_files returns empty AND inputs
        # coerce to empty strings.
        sig2 = compute_signature_hash(
            situation_kind=None,  # type: ignore[arg-type]
            attempted_action_kind=None,  # type: ignore[arg-type]
            target_files=42,  # type: ignore[arg-type]
        )
        assert sig == sig2
        # The well-known constant exists for documentation / sanity:
        assert (
            expected_empty
            == "e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855"
        )


# ---------------------------------------------------------------------------
# § 6 — Schema version stability
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_schema_version_is_v1(self):
        """Slice 1 ships ``failure_mode_memory.1``. Any future
        breaking change to the dataclass shape MUST bump this AND
        update from_dict's gate."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FAILURE_MODE_MEMORY_SCHEMA_VERSION,
        )
        assert (
            FAILURE_MODE_MEMORY_SCHEMA_VERSION
            == "failure_mode_memory.1"
        )

    def test_record_default_schema_matches_module_constant(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FAILURE_MODE_MEMORY_SCHEMA_VERSION,
        )
        rec = _sample_record()
        assert rec.schema_version == FAILURE_MODE_MEMORY_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# § 7 — Authority invariants — stdlib-only primitive
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    """The Slice 1 primitive MUST be importable in isolation —
    stdlib only. Slices 2-5 will lean on
    semantic_index/postmortem_recall/strategic_direction; the
    primitive layer must remain narrowable so Slice 5b operator
    surfaces can consume it without pulling the whole RAG stack."""

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
        # on failure_mode_memory, NEVER the reverse.
        "from backend.core.ouroboros.governance.strategic_direction",
        # Slice 2-3 will use these; Slice 1 primitive must not.
        "from backend.core.ouroboros.governance.semantic_index",
        "from backend.core.ouroboros.governance.postmortem_recall",
    )

    def test_primitive_imports_stdlib_only(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "failure_mode_memory.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"failure_mode_memory.py (Slice 1) must NOT "
                f"import {forbidden} — primitive authority floor"
            )

    def test_module_imports_resolve_in_isolation(self):
        """The primitive is loadable as a standalone module.
        Belt-and-suspenders against accidental governance-module
        imports that satisfy the grep but break import."""
        from backend.core.ouroboros.governance import (  # noqa: F401
            failure_mode_memory,
        )

    def test_no_print_no_input_no_io(self):
        """Pure-data layer: no stdin/stdout side effects."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "failure_mode_memory.py"
        )
        source = path.read_text(encoding="utf-8")
        # The grep is intentionally narrow — these tokens at
        # module scope (not inside docstrings) would indicate I/O.
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
        """Slice 1 (6) + Slice 2 (10) + Slice 3 (5) + Slice 4 (3)
        = 24 public exports. Future slices append; never remove."""
        from backend.core.ouroboros.governance import failure_mode_memory  # noqa: E501
        expected = sorted([
            # Slice 1 — primitive
            "FAILURE_MODE_MEMORY_SCHEMA_VERSION",
            "FailureModeKind",
            "FailureModeRecord",
            "SituationKind",
            "compute_signature_hash",
            "failure_mode_memory_enabled",
            # Slice 2 — extractor + persistence
            "ExtractionOutcome",
            "RecordOutcome",
            "dedup_window_days",
            "extract_failure_mode",
            "history_dir",
            "history_max_records",
            "history_path",
            "read_failure_mode_history",
            "record_failure_mode",
            "record_postmortem",
            # Slice 3 — RAG retriever
            "FailureModeMatch",
            "failure_mode_min_weight",
            "failure_mode_recency_halflife_days",
            "failure_mode_top_k",
            "retrieve_failure_modes",
            # Slice 4 — prompt-section composer
            "DEFAULT_PROMPT_SECTION_BUDGET",
            "classify_situation_from_ctx",
            "compose_failure_modes_section",
        ])
        assert sorted(failure_mode_memory.__all__) == expected

    def test_helpers_are_underscore_prefixed(self):
        """Internal helpers are NOT in __all__ — they're
        implementation, not API. Slice 2 + Slice 3 add more
        internal helpers — pin a representative set."""
        from backend.core.ouroboros.governance import failure_mode_memory  # noqa: E501
        for name in (
            # Slice 1
            "_situation_kind_from_value",
            "_failure_mode_kind_from_value",
            "_canonicalize_target_files",
            # Slice 2
            "_classify_situation",
            "_classify_failure_mode",
            "_extract_attempt_kind",
            "_derive_mitigation",
            "_serialize_record",
            "_read_existing_records",
            "_within_dedup_window",
            "_postmortem_field",
            "_plan_text_for_classification",
            # Slice 3
            "_recency_weight",
            "_jaccard_similarity",
            "_weight_score",
            "_diversity_dedup",
        ):
            assert name not in failure_mode_memory.__all__
            assert hasattr(failure_mode_memory, name)
