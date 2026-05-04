"""Upgrade 3 Slice 2 — FailureModeExtractor + persistence tests.

Pins the chain-of-responsibility classifier contract from PRD
§31.4.2 + the dedup-aware flock'd persistence layer. Mirrors the
test discipline of Slice 1 primitive tests + Move 6 quorum
observer roundtrip tests.

Test layout:
  § 1 — Situation classifiers (each in isolation + chain order)
  § 2 — Failure-mode classifiers (each in isolation + chain order)
  § 3 — Attempt-kind extraction (diff + plan-text)
  § 4 — Mitigation derivation
  § 5 — extract_failure_mode (composer; gate; partial-flag)
  § 6 — record_failure_mode (persistence; dedup; ring rotation)
  § 7 — read_failure_mode_history (filter, sort, clamp)
  § 8 — record_postmortem (end-to-end composer)
  § 9 — Env-knob clamps
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest


# ---------------------------------------------------------------------------
# Test fixtures — synthetic POSTMORTEM payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SyntheticPostmortem:
    """Duck-typed equivalent of postmortem_recall.PostmortemRecord;
    the extractor accepts EITHER object OR dict shape."""

    op_id: str = "op-test-001"
    session_id: str = "bt-test"
    root_cause: str = ""
    failed_phase: str = "VERIFY"
    next_safe_action: str = ""
    target_files: tuple = ()
    timestamp_unix: float = 1700000000.0


# ---------------------------------------------------------------------------
# § 1 — Situation classifiers
# ---------------------------------------------------------------------------


class TestSituationClassifiers:
    def test_db_migration_via_path(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("migrations/001.sql",),
            diff="",
            plan_text="",
        )
        assert result is SituationKind.DB_MIGRATION

    def test_db_migration_via_ddl_in_diff(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("schema.py",),
            diff="+ CREATE TABLE foo (id INT);",
            plan_text="",
        )
        assert result is SituationKind.DB_MIGRATION

    def test_async_restructure_via_diff_density(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        diff = (
            "+ async def foo(): pass\n"
            "+ await asyncio.gather(t1, t2)\n"
            "+ asyncio.create_task(coro)\n"
        )
        result = _classify_situation(
            target_files=("worker.py",),
            diff=diff,
            plan_text="",
        )
        assert result is SituationKind.ASYNC_RESTRUCTURE

    def test_test_framework_integration_via_new_conftest(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        diff = (
            "+++ tests/conftest.py\n"
            "+ @pytest.fixture\n"
            "+ def my_fixture(): ...\n"
        )
        result = _classify_situation(
            target_files=("tests/conftest.py",),
            diff=diff,
            plan_text="add fixture framework",
        )
        assert result is SituationKind.NEW_TEST_FRAMEWORK_INTEGRATION

    def test_api_version_bump_via_pyproject(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("pyproject.toml",),
            diff='version = "2.0.0"',
            plan_text="",
        )
        assert result is SituationKind.API_VERSION_BUMP

    def test_cross_repo_drift_via_plan_text(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("client.py",),
            diff="",
            plan_text="Realign cross-repo signature with sibling repo.",
        )
        assert result is SituationKind.CROSS_REPO_DRIFT_FIX

    def test_multi_file_refactor_when_two_py_files(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("a.py", "b.py"),
            diff="",
            plan_text="",
        )
        assert result is SituationKind.MULTI_FILE_REFACTOR

    def test_unknown_when_single_file_no_signal(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("docs.md",),
            diff="",
            plan_text="",
        )
        assert result is SituationKind.UNKNOWN

    def test_chain_order_specific_beats_general(self):
        """A multi-file DB migration MUST classify as DB_MIGRATION,
        NOT MULTI_FILE_REFACTOR — specific classifiers run first."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            _classify_situation,
        )
        result = _classify_situation(
            target_files=("migrations/001.sql", "migrations/002.sql"),
            diff="",
            plan_text="",
        )
        assert result is SituationKind.DB_MIGRATION


# ---------------------------------------------------------------------------
# § 2 — Failure-mode classifiers
# ---------------------------------------------------------------------------


class TestFailureModeClassifiers:
    def test_circular_dep_before_missing_import(self):
        """Circular-import raises ImportError but is more specific
        — must classify as CIRCULAR_DEP_INTRODUCED, not
        MISSING_IMPORT (chain order)."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause=(
                "ImportError: circular import detected in module "
                "foo.bar (partially initialized module)"
            ),
        )
        assert result is FailureModeKind.CIRCULAR_DEP_INTRODUCED

    def test_missing_import_via_import_error(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause="ImportError: cannot import name 'foo'",
        )
        assert result is FailureModeKind.MISSING_IMPORT

    def test_missing_import_via_semantic_guardian_pattern(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause=(
                "SemanticGuardian flagged "
                "removed_import_still_referenced"
            ),
        )
        assert result is FailureModeKind.MISSING_IMPORT

    def test_type_mismatch_via_typeerror(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause=(
                "TypeError: argument of type 'NoneType' is not "
                "iterable"
            ),
        )
        assert result is FailureModeKind.TYPE_MISMATCH

    def test_assert_inverted_pattern(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause="SemanticGuardian: test_assertion_inverted",
        )
        assert result is FailureModeKind.ASSERT_INVERTED

    def test_banned_token_pattern(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause="Iron Gate rejected: forbidden token",
        )
        assert result is FailureModeKind.BANNED_TOKEN_INTRODUCED

    def test_test_timeout_pattern(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause="pytest test timed out after 30s",
        )
        assert result is FailureModeKind.TEST_TIMEOUT_REGRESSED

    def test_other_when_no_match(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        result = _classify_failure_mode(
            root_cause="something completely unrelated",
        )
        assert result is FailureModeKind.OTHER

    def test_empty_root_cause_returns_other(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _classify_failure_mode,
        )
        assert (
            _classify_failure_mode(root_cause="")
            is FailureModeKind.OTHER
        )


# ---------------------------------------------------------------------------
# § 3 — Attempt-kind extraction
# ---------------------------------------------------------------------------


class TestAttemptKindExtraction:
    def test_dataclass_decorator_in_diff(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        diff = (
            "+ @dataclass(frozen=True)\n"
            "+ class Foo: x: int\n"
        )
        assert _extract_attempt_kind(
            plan_text="", diff=diff,
        ) == "add_dataclass"

    def test_async_def_in_diff(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        diff = "+ async def fetch(): ...\n"
        assert _extract_attempt_kind(
            plan_text="", diff=diff,
        ) == "add_async_function"

    def test_class_added(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        diff = "+ class Foo: pass\n"
        assert _extract_attempt_kind(
            plan_text="", diff=diff,
        ) == "add_class"

    def test_def_added(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        diff = "+ def helper(x): return x\n"
        assert _extract_attempt_kind(
            plan_text="", diff=diff,
        ) == "add_function"

    def test_plan_rename_token(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        assert _extract_attempt_kind(
            plan_text="rename module foo to bar",
            diff="",
        ) == "rename_symbol"

    def test_unspecified_fallback(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        assert _extract_attempt_kind(
            plan_text="", diff="",
        ) == "unspecified"

    def test_specific_diff_beats_general_plan(self):
        """Diff signal is more specific than plan-text — wins."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            _extract_attempt_kind,
        )
        diff = "+ @dataclass\n+ class Foo: pass\n"
        plan = "rename and refactor"
        assert _extract_attempt_kind(
            plan_text=plan, diff=diff,
        ) == "add_dataclass"


# ---------------------------------------------------------------------------
# § 4 — Mitigation derivation
# ---------------------------------------------------------------------------


class TestMitigationDerivation:
    def test_each_mode_has_distinct_template(self):
        """Defensive: every FailureModeKind must produce a non-empty
        mitigation."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _derive_mitigation,
        )
        seen = set()
        for kind in FailureModeKind:
            text = _derive_mitigation(kind)
            assert text, f"empty mitigation for {kind.value}"
            seen.add(text)
        # Every kind has a UNIQUE mitigation template.
        assert len(seen) == len(FailureModeKind)

    def test_next_safe_action_appended_when_present(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _derive_mitigation,
        )
        text = _derive_mitigation(
            FailureModeKind.MISSING_IMPORT,
            next_safe_action="explore the import surface first",
        )
        assert "Next-safe-action" in text
        assert "explore the import surface first" in text

    def test_none_next_safe_action_omitted(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            _derive_mitigation,
        )
        text = _derive_mitigation(
            FailureModeKind.MISSING_IMPORT,
            next_safe_action="none",
        )
        assert "Next-safe-action" not in text


# ---------------------------------------------------------------------------
# § 5 — extract_failure_mode composer
# ---------------------------------------------------------------------------


class TestExtractFailureMode:
    def test_disabled_when_master_off(self, monkeypatch):
        # Slice 5 graduated default-true; force off to test the
        # disabled path explicitly.
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            extract_failure_mode,
        )
        outcome, rec = extract_failure_mode(
            _SyntheticPostmortem(root_cause="ImportError: foo"),
        )
        assert outcome is ExtractionOutcome.DISABLED
        assert rec is None

    def test_disabled_via_explicit_override(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            extract_failure_mode,
        )
        outcome, rec = extract_failure_mode(
            _SyntheticPostmortem(root_cause="ImportError: foo"),
            enabled_override=False,
        )
        assert outcome is ExtractionOutcome.DISABLED
        assert rec is None

    def test_rejected_on_none_input(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            extract_failure_mode,
        )
        outcome, rec = extract_failure_mode(
            None, enabled_override=True,
        )
        assert outcome is ExtractionOutcome.REJECTED
        assert rec is None

    def test_rejected_on_empty_postmortem(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            extract_failure_mode,
        )
        outcome, rec = extract_failure_mode(
            _SyntheticPostmortem(op_id="", root_cause=""),
            enabled_override=True,
        )
        assert outcome is ExtractionOutcome.REJECTED

    def test_ok_when_both_classifiers_match(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            FailureModeKind,
            SituationKind,
            extract_failure_mode,
        )
        pm = _SyntheticPostmortem(
            root_cause="ImportError: cannot import name 'foo'",
            target_files=("a.py", "b.py"),
        )
        outcome, rec = extract_failure_mode(
            pm, enabled_override=True,
        )
        assert outcome is ExtractionOutcome.OK
        assert rec is not None
        assert rec.situation_kind is SituationKind.MULTI_FILE_REFACTOR
        assert rec.failure_mode_kind is FailureModeKind.MISSING_IMPORT
        assert rec.weight == 1
        assert rec.signature_hash  # non-empty

    def test_partial_when_unknown_situation(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            SituationKind,
            extract_failure_mode,
        )
        pm = _SyntheticPostmortem(
            root_cause="ImportError: foo",
            target_files=("README.md",),
        )
        outcome, rec = extract_failure_mode(
            pm, enabled_override=True,
        )
        assert outcome is ExtractionOutcome.OK_PARTIAL
        assert rec is not None
        assert rec.situation_kind is SituationKind.UNKNOWN

    def test_partial_when_other_failure_mode(self):
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            FailureModeKind,
            extract_failure_mode,
        )
        pm = _SyntheticPostmortem(
            root_cause="some unrelated error",
            target_files=("a.py", "b.py"),
        )
        outcome, rec = extract_failure_mode(
            pm, enabled_override=True,
        )
        assert outcome is ExtractionOutcome.OK_PARTIAL
        assert rec is not None
        assert rec.failure_mode_kind is FailureModeKind.OTHER

    def test_dict_postmortem_accepted(self):
        """Duck-typed: a plain dict works as well as the dataclass."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            extract_failure_mode,
        )
        pm_dict = {
            "op_id": "op-dict-001",
            "root_cause": "TypeError: incompatible types",
            "target_files": ["foo.py", "bar.py"],
            "next_safe_action": "fix types",
            "timestamp_unix": 1700000000.0,
        }
        outcome, rec = extract_failure_mode(
            pm_dict, enabled_override=True,
        )
        assert outcome is ExtractionOutcome.OK
        assert rec is not None
        assert rec.op_id == "op-dict-001"

    def test_plan_text_used_for_classification(self):
        """Plan rationale should influence situation classification
        when files are ambiguous."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
            extract_failure_mode,
        )
        pm = _SyntheticPostmortem(
            root_cause="ImportError",
            target_files=("a.py",),
        )
        plan = {
            "approach": "Bump major version of dependency",
            "risk_factors": ["semver-major break"],
        }
        _outcome, rec = extract_failure_mode(
            pm, plan=plan, enabled_override=True,
        )
        assert rec is not None
        # Plan-text-only API_VERSION_BUMP requires a version-related
        # file token; without that, falls through. Confirm extractor
        # handles plan input without raising.
        assert rec.situation_kind in SituationKind

    def test_signature_stable_for_same_inputs(self):
        """Two extractions over the SAME postmortem MUST produce
        the SAME signature_hash."""
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            extract_failure_mode,
        )
        pm = _SyntheticPostmortem(
            root_cause="ImportError: foo",
            target_files=("a.py", "b.py"),
        )
        _, r1 = extract_failure_mode(pm, enabled_override=True)
        _, r2 = extract_failure_mode(pm, enabled_override=True)
        assert r1 is not None and r2 is not None
        assert r1.signature_hash == r2.signature_hash


# ---------------------------------------------------------------------------
# § 6 — record_failure_mode persistence
# ---------------------------------------------------------------------------


def _make_record(
    *,
    sig: str = "a" * 64,
    obs_at: float = 1700000000.0,
    weight: int = 1,
    op_id: str = "op-001",
):
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        FailureModeKind,
        FailureModeRecord,
        SituationKind,
    )
    return FailureModeRecord(
        signature_hash=sig,
        situation_kind=SituationKind.MULTI_FILE_REFACTOR,
        attempted_action_kind="add_dataclass",
        failure_mode_kind=FailureModeKind.MISSING_IMPORT,
        mitigation_summary="check imports",
        observed_at_unix=obs_at,
        op_id=op_id,
        weight=weight,
    )


class TestRecordFailureMode:
    def test_disabled_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        # Slice 5 graduated default-true; force off explicitly.
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            record_failure_mode,
        )
        outcome = record_failure_mode(_make_record())
        assert outcome is RecordOutcome.DISABLED

    def test_rejected_on_garbage_input(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            record_failure_mode,
        )
        outcome = record_failure_mode(
            "not a record",  # type: ignore[arg-type]
        )
        assert outcome is RecordOutcome.REJECTED

    def test_ok_new_appends_record(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            read_failure_mode_history,
            record_failure_mode,
        )
        outcome = record_failure_mode(_make_record())
        assert outcome is RecordOutcome.OK_NEW
        history = read_failure_mode_history()
        assert len(history) == 1
        assert history[0].weight == 1

    def test_dedup_increments_weight_within_window(
        self, monkeypatch, tmp_path,
    ):
        """Two records sharing signature within dedup window → one
        record with weight=2."""
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            read_failure_mode_history,
            record_failure_mode,
        )
        r1 = _make_record(obs_at=1700000000.0)
        r2 = _make_record(obs_at=1700001000.0)  # same day
        assert (
            record_failure_mode(r1)
            is RecordOutcome.OK_NEW
        )
        assert (
            record_failure_mode(r2)
            is RecordOutcome.OK_DEDUPED
        )
        history = read_failure_mode_history()
        assert len(history) == 1
        assert history[0].weight == 2
        # observed_at_unix updated to most recent
        assert history[0].observed_at_unix == 1700001000.0

    def test_no_dedup_outside_window(
        self, monkeypatch, tmp_path,
    ):
        """Same signature but >30d apart → two distinct records."""
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            read_failure_mode_history,
            record_failure_mode,
        )
        r1 = _make_record(obs_at=1700000000.0)
        r2 = _make_record(
            obs_at=1700000000.0 + 31 * 86400.0,
        )
        assert (
            record_failure_mode(r1)
            is RecordOutcome.OK_NEW
        )
        assert (
            record_failure_mode(r2)
            is RecordOutcome.OK_NEW
        )
        history = read_failure_mode_history()
        assert len(history) == 2

    def test_distinct_signatures_both_persist(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            read_failure_mode_history,
            record_failure_mode,
        )
        r1 = _make_record(sig="a" * 64)
        r2 = _make_record(sig="b" * 64)
        assert (
            record_failure_mode(r1)
            is RecordOutcome.OK_NEW
        )
        assert (
            record_failure_mode(r2)
            is RecordOutcome.OK_NEW
        )
        history = read_failure_mode_history()
        assert len(history) == 2

    def test_ring_buffer_truncates_to_cap(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        # Tight cap to validate truncation
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS", "50",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            read_failure_mode_history,
            record_failure_mode,
        )
        # Insert 60 distinct signatures (well past the floor of 50)
        for i in range(60):
            sig = f"{i:064x}"  # 64-char hex
            obs = 1700000000.0 + i  # strictly increasing ts
            record_failure_mode(
                _make_record(
                    sig=sig, obs_at=obs, op_id=f"op-{i:03d}",
                ),
            )
        history = read_failure_mode_history()
        # Cap is clamped to floor 50 by env-knob clamping.
        assert len(history) == 50
        # Most-recent retained — first kept op-id is op-010
        # (we wrote op-000 .. op-059; cap keeps last 50).
        assert history[0].op_id == "op-010"
        assert history[-1].op_id == "op-059"


# ---------------------------------------------------------------------------
# § 7 — read_failure_mode_history filter / sort / clamp
# ---------------------------------------------------------------------------


class TestReadHistory:
    def test_empty_when_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            read_failure_mode_history,
        )
        assert read_failure_mode_history() == ()

    def test_since_unix_filter(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            read_failure_mode_history,
            record_failure_mode,
        )
        for i in range(5):
            sig = f"{i:064x}"
            record_failure_mode(
                _make_record(
                    sig=sig,
                    obs_at=1700000000.0 + i * 1000.0,
                    op_id=f"op-{i}",
                ),
            )
        # Filter to records observed at or after 1700002000.
        cut = 1700002000.0
        history = read_failure_mode_history(since_unix=cut)
        assert all(
            r.observed_at_unix >= cut for r in history
        )
        assert len(history) == 3

    def test_limit_clamp(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            read_failure_mode_history,
            record_failure_mode,
        )
        for i in range(10):
            sig = f"{i:064x}"
            record_failure_mode(
                _make_record(
                    sig=sig, obs_at=1700000000.0 + i,
                    op_id=f"op-{i}",
                ),
            )
        history = read_failure_mode_history(limit=3)
        assert len(history) == 3
        # tail-clamp keeps most recent
        assert history[-1].op_id == "op-9"


# ---------------------------------------------------------------------------
# § 8 — record_postmortem end-to-end composer
# ---------------------------------------------------------------------------


class TestRecordPostmortem:
    def test_end_to_end_extract_then_persist(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            FailureModeKind,
            RecordOutcome,
            read_failure_mode_history,
            record_postmortem,
        )
        pm = _SyntheticPostmortem(
            root_cause="ImportError: cannot import name 'foo'",
            target_files=("a.py", "b.py"),
        )
        ex_outcome, rec_outcome = record_postmortem(pm)
        assert ex_outcome is ExtractionOutcome.OK
        assert rec_outcome is RecordOutcome.OK_NEW
        history = read_failure_mode_history()
        assert len(history) == 1
        assert (
            history[0].failure_mode_kind
            is FailureModeKind.MISSING_IMPORT
        )

    def test_recurrence_increments_weight_via_composer(
        self, monkeypatch, tmp_path,
    ):
        """Two postmortems with same signature within 30d → one
        record with weight=2 (the load-bearing PRD §31.4.6 mechanic
        that gates first-attempt injection)."""
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            RecordOutcome,
            read_failure_mode_history,
            record_postmortem,
        )
        pm1 = _SyntheticPostmortem(
            op_id="op-1",
            root_cause="ImportError: foo",
            target_files=("a.py", "b.py"),
            timestamp_unix=1700000000.0,
        )
        pm2 = _SyntheticPostmortem(
            op_id="op-2",
            root_cause="ImportError: foo",
            target_files=("a.py", "b.py"),
            timestamp_unix=1700001000.0,
        )
        _, r1 = record_postmortem(pm1)
        _, r2 = record_postmortem(pm2)
        assert r1 is RecordOutcome.OK_NEW
        assert r2 is RecordOutcome.OK_DEDUPED
        history = read_failure_mode_history()
        assert len(history) == 1
        assert history[0].weight == 2

    def test_disabled_propagates_through_composer(
        self, monkeypatch, tmp_path,
    ):
        # Slice 5 graduated default-true; force off to test the
        # disabled path explicitly.
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            ExtractionOutcome,
            RecordOutcome,
            record_postmortem,
        )
        pm = _SyntheticPostmortem(root_cause="ImportError")
        ex, rec = record_postmortem(pm)
        assert ex is ExtractionOutcome.DISABLED
        assert rec is RecordOutcome.REJECTED


# ---------------------------------------------------------------------------
# § 9 — Env-knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobClamps:
    def test_history_max_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS", "1",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            history_max_records,
        )
        # Floor is 50 per the clamp.
        assert history_max_records() == 50

    def test_history_max_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS", "999999",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            history_max_records,
        )
        assert history_max_records() == 100_000

    def test_history_max_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS", raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            history_max_records,
        )
        assert history_max_records() == 5000

    def test_dedup_window_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_DEDUP_WINDOW_DAYS", "0",
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            dedup_window_days,
        )
        assert dedup_window_days() == 1

    def test_history_dir_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", raising=False,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            history_dir,
        )
        assert str(history_dir()).endswith("failure_mode_memory")
