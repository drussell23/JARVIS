"""M11 Slice 4 — Prompt-section composer + StrategicDirection
injection tests (PRD §30.5.3 Slice 4).

Pins:
  * compose_action_outcomes_section rendering contract (header,
    intro, bullet shape, 4-component score surface, commit-hash
    provenance, char budget, empty-on-empty-matches)
  * StrategicDirection._render_action_outcomes_section integration
    (None ctx -> empty / disabled -> empty / no matches -> empty
    / matches present -> section appended LAST)
  * Section ordering (after Upgrade 3 failure-modes block)
  * format_for_prompt backward compat
  * SSE event publish on injection
  * Authority direction (lazy import preserved; M11 module is
    NOT imported at module scope in strategic_direction.py)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
    )


def _make_match(
    *, outcome=None, attempt="add_dataclass", weight=3,
    recency=1.0, polarity=1.0, commit="abc1234",
):
    from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
        ActionOutcomeMatch,
        ActionOutcomeRecord,
        OutcomeKind,
    )
    from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
        SituationKind,
    )
    out = outcome or OutcomeKind.APPLIED_VERIFIED
    rec = ActionOutcomeRecord(
        signature_hash=("a" * 64),
        situation_kind=SituationKind.MULTI_FILE_REFACTOR,
        attempted_action_kind=attempt,
        outcome_kind=out,
        target_files=("a.py", "b.py"),
        commit_hash=commit if (
            out is OutcomeKind.APPLIED_VERIFIED
        ) else "",
        summary="touched X to Y; tests pass.",
        observed_at_unix=time.time(),
        op_id="op-test",
        cluster_id="42",
        weight=weight,
    )
    return ActionOutcomeMatch(
        record=rec,
        recency_score=recency,
        jaccard_score=1.0,
        weight_score=0.578,
        polarity_score=polarity,
        combined_score=recency * 1.0 * 0.578 * polarity,
    )


# ---------------------------------------------------------------------------
# § 1 — compose_action_outcomes_section rendering contract
# ---------------------------------------------------------------------------


class TestComposeSection:
    def test_empty_matches_returns_empty(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        assert compose_action_outcomes_section([]) == ""

    def test_zero_budget_returns_empty(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        assert (
            compose_action_outcomes_section(
                [_make_match()], max_chars=0,
            )
            == ""
        )

    def test_negative_budget_returns_empty(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        assert (
            compose_action_outcomes_section(
                [_make_match()], max_chars=-100,
            )
            == ""
        )

    def test_section_has_canonical_header(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        section = compose_action_outcomes_section(
            [_make_match()],
        )
        assert section.startswith(
            "## Recent Region Outcomes"
        )

    def test_section_has_intro_text(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        section = compose_action_outcomes_section(
            [_make_match()],
        )
        assert "PRD §30.5.3" in section
        assert "outcome-polarity" in section.lower()

    def test_section_includes_outcome_attempt_summary(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compose_action_outcomes_section,
        )
        section = compose_action_outcomes_section(
            [_make_match(
                outcome=OutcomeKind.APPLIED_VERIFIED,
                attempt="add_dataclass",
            )],
        )
        assert "applied_verified" in section
        assert "add_dataclass" in section
        assert "touched X to Y" in section

    def test_per_component_scores_surfaced(self):
        """Operator-explainability: model sees WHY each match was
        chosen — all 4 score components in the rendered line."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        section = compose_action_outcomes_section(
            [_make_match(weight=5, recency=0.78, polarity=0.7)],
        )
        assert "weight=5" in section
        assert "recency=0.78" in section
        assert "polarity=0.70" in section

    def test_commit_hash_surfaced_for_verified_only(self):
        """commit_hash provenance shows for APPLIED_VERIFIED;
        empty for other outcomes (no stable commit ref)."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            compose_action_outcomes_section,
        )
        verified_section = compose_action_outcomes_section(
            [_make_match(
                outcome=OutcomeKind.APPLIED_VERIFIED,
                commit="abc12345",
            )],
        )
        assert "commit=`abc12345`" in verified_section
        # REJECTED has no commit
        rejected_section = compose_action_outcomes_section(
            [_make_match(outcome=OutcomeKind.REJECTED)],
        )
        assert "commit=" not in rejected_section

    def test_budget_cap_truncates_extra_matches(self):
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            compose_action_outcomes_section,
        )
        matches = [_make_match() for _ in range(50)]
        section = compose_action_outcomes_section(
            matches, max_chars=900,
        )
        assert section != ""
        assert len(section) <= 900

    def test_default_budget_is_four_kb(self):
        """PRD §30.5.3: 4KB cap — slightly larger than Upgrade
        3's 3KB because outcome lines carry richer context."""
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            DEFAULT_ACTION_OUTCOME_PROMPT_BUDGET,
        )
        assert DEFAULT_ACTION_OUTCOME_PROMPT_BUDGET == 4000


# ---------------------------------------------------------------------------
# § 2 — StrategicDirection integration
# ---------------------------------------------------------------------------


def _make_loaded_service(tmp_path):
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    svc = StrategicDirectionService(project_root=tmp_path)
    svc._digest = "Test digest content."  # noqa: SLF001
    svc._loaded = True  # noqa: SLF001
    return svc


class TestStrategicDirectionIntegration:
    def test_no_ctx_no_action_outcomes_section(
        self, monkeypatch, tmp_path,
    ):
        """Backward compat: no kwargs -> no section."""
        _enable(monkeypatch, tmp_path)
        svc = _make_loaded_service(tmp_path)
        body = svc.format_for_prompt()
        assert body
        assert "Recent Region Outcomes" not in body

    def test_disabled_master_no_section(
        self, monkeypatch, tmp_path,
    ):
        """Master-flag-off → no section even when ctx provided."""
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        svc = _make_loaded_service(tmp_path)
        body = svc.format_for_prompt(
            target_files=("a.py", "b.py"),
            plan={"approach": "refactor"},
        )
        assert "Recent Region Outcomes" not in body

    def test_no_matches_no_section(
        self, monkeypatch, tmp_path,
    ):
        """Master-on but empty history → no section emitted."""
        _enable(monkeypatch, tmp_path)
        svc = _make_loaded_service(tmp_path)
        body = svc.format_for_prompt(
            target_files=("a.py", "b.py"),
            plan={"approach": "refactor"},
        )
        assert "Recent Region Outcomes" not in body

    def test_matches_inject_section(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end: plant outcomes, verify section appears."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
            OutcomeKind,
            record_action_outcome,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        record_action_outcome(ActionOutcomeRecord(
            signature_hash="a" * 64,
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="add_dataclass",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
            commit_hash="abc1234",
            summary="Imported from canonical module",
            observed_at_unix=time.time(),
            op_id="op-1",
            cluster_id="42",
            weight=3,
        ))
        svc = _make_loaded_service(tmp_path)
        body = svc.format_for_prompt(
            target_files=("a.py", "b.py"),
            plan={"approach": "refactor"},
        )
        assert "Recent Region Outcomes" in body
        assert "applied_verified" in body
        assert "Imported from canonical module" in body

    def test_section_appended_LAST(
        self, monkeypatch, tmp_path,
    ):
        """**Load-bearing ordering**: action-outcomes appears
        AFTER the failure-modes block (and after Strategic
        Direction header) — recency-of-attention favors what's
        closest to the model's first-attempt generation."""
        # Enable BOTH arcs so both sections can render
        _enable(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "true",
        )
        # Plant both kinds of records
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
            OutcomeKind,
            record_action_outcome,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            FailureModeKind,
            FailureModeRecord,
            SituationKind,
            record_failure_mode,
        )
        # Use separate temp dirs so the two arcs don't collide
        # via env-knob defaults — explicit override.
        fmm_dir = tmp_path / "fmm"
        aom_dir = tmp_path / "aom"
        monkeypatch.setenv(
            "JARVIS_FAILURE_MODE_HISTORY_DIR", str(fmm_dir),
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(aom_dir),
        )
        record_failure_mode(FailureModeRecord(
            signature_hash="f" * 64,
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="bad_attempt",
            failure_mode_kind=FailureModeKind.MISSING_IMPORT,
            mitigation_summary="check imports first",
            observed_at_unix=time.time(),
            op_id="op-fail",
            weight=3,
        ))
        record_action_outcome(ActionOutcomeRecord(
            signature_hash="a" * 64,
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="good_attempt",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
            commit_hash="abc1234",
            summary="this approach worked",
            observed_at_unix=time.time(),
            op_id="op-good",
            cluster_id="42",
            weight=3,
        ))
        svc = _make_loaded_service(tmp_path)
        body = svc.format_for_prompt(
            target_files=("a.py", "b.py"),
            plan={"approach": "refactor"},
        )
        sd_idx = body.find("## Strategic Direction")
        fm_idx = body.find(
            "## Prior Failure Modes for This Situation",
        )
        ao_idx = body.find("## Recent Region Outcomes")
        assert sd_idx >= 0
        assert fm_idx > sd_idx
        # Action outcomes MUST come AFTER failure modes
        assert ao_idx > fm_idx

    def test_only_target_files_required(
        self, monkeypatch, tmp_path,
    ):
        """Unlike Upgrade 3 Slice 4 which required BOTH
        target_files + plan, M11 needs only target_files (recall
        is region-keyed). target_files=None still suppresses."""
        _enable(monkeypatch, tmp_path)
        svc = _make_loaded_service(tmp_path)
        # target_files=None → no section
        body_none = svc.format_for_prompt(
            target_files=None,
            plan={"approach": "refactor"},
        )
        assert "Recent Region Outcomes" not in body_none

    def test_plan_not_required_for_action_outcomes(
        self, monkeypatch, tmp_path,
    ):
        """target_files alone (no plan) is sufficient for the
        M11 path. Plant a record + call without plan; the M11
        section should still render even though Upgrade 3 won't."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            ActionOutcomeRecord,
            OutcomeKind,
            record_action_outcome,
        )
        from backend.core.ouroboros.governance.failure_mode_memory import (  # noqa: E501
            SituationKind,
        )
        record_action_outcome(ActionOutcomeRecord(
            signature_hash="a" * 64,
            situation_kind=SituationKind.MULTI_FILE_REFACTOR,
            attempted_action_kind="x",
            outcome_kind=OutcomeKind.APPLIED_VERIFIED,
            target_files=("a.py", "b.py"),
            commit_hash="",
            summary="worked",
            observed_at_unix=time.time(),
            op_id="op-1",
            cluster_id="42",
            weight=3,
        ))
        svc = _make_loaded_service(tmp_path)
        # Pass target_files but plan=None → M11 fires;
        # Upgrade 3 won't (it needs both).
        body = svc.format_for_prompt(
            target_files=("a.py", "b.py"),
            plan=None,
        )
        assert "Recent Region Outcomes" in body
        # Upgrade 3's section absent (no plan)
        assert (
            "Prior Failure Modes for This Situation"
            not in body
        )


# ---------------------------------------------------------------------------
# § 3 — _render_action_outcomes_section in isolation
# ---------------------------------------------------------------------------


class TestRenderActionOutcomesSection:
    def test_none_target_files_returns_empty(self):
        from backend.core.ouroboros.governance.strategic_direction import (  # noqa: E501
            StrategicDirectionService,
        )
        result = (
            StrategicDirectionService._render_action_outcomes_section(  # noqa: E501, SLF001
                target_files=None,
            )
        )
        assert result == ""

    def test_empty_target_files_with_no_history(
        self, monkeypatch, tmp_path,
    ):
        """target_files=() (empty but provided) is valid input;
        no records → empty section."""
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.strategic_direction import (  # noqa: E501
            StrategicDirectionService,
        )
        result = (
            StrategicDirectionService._render_action_outcomes_section(  # noqa: E501, SLF001
                target_files=(),
            )
        )
        assert result == ""

    def test_master_off_returns_empty(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.strategic_direction import (  # noqa: E501
            StrategicDirectionService,
        )
        result = (
            StrategicDirectionService._render_action_outcomes_section(  # noqa: E501, SLF001
                target_files=("a.py", "b.py"),
            )
        )
        assert result == ""


# ---------------------------------------------------------------------------
# § 4 — Authority direction (lazy import preserved)
# ---------------------------------------------------------------------------


class TestDependencyDirection:
    """The authority direction is strategic_direction →
    action_outcome_memory, NEVER the reverse. M11 module's AST
    pins (Slice 1 + Slice 5 graduation) enforce that
    action_outcome_memory does NOT import strategic_direction.
    This test pins the COMPLEMENTARY direction: strategic_-
    direction lazy-imports action_outcome_memory only INSIDE
    the render method (NOT at module scope) so a missing
    action_outcome_memory cannot break import of
    strategic_direction."""

    def test_strategic_direction_imports_aom_lazily(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "strategic_direction.py"
        )
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "action_outcome_memory" in line and (
                line.startswith("from ")
                or line.startswith("import ")
            ):
                pytest.fail(
                    f"strategic_direction must lazy-import "
                    f"action_outcome_memory inside the render "
                    f"method, not at module scope. Found "
                    f"top-level import: {line!r}"
                )


# ---------------------------------------------------------------------------
# § 5 — SSE event publish on injection
# ---------------------------------------------------------------------------


class TestSSEPublishHook:
    def test_sse_publisher_master_off_returns_none(
        self, monkeypatch,
    ):
        """Slice 5 graduated default-true; force off to test the
        master-off short-circuit path. publish returns None
        silently when master is off."""
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            publish_action_outcome_recalled,
        )
        result = publish_action_outcome_recalled(
            op_id="op-test",
            match_count=3,
            top_outcome_kind="applied_verified",
            top_signature="abc" * 21 + "x",
            top_weight=5,
        )
        assert result is None

    def test_sse_event_constant_registered(self):
        """The new event constant exists in the
        ide_observability_stream registry (master validated set)."""
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ACTION_OUTCOME_RECALLED_AT_GENERATE,
        )
        assert (
            EVENT_TYPE_ACTION_OUTCOME_RECALLED_AT_GENERATE
            == "action_outcome_recalled_at_generate"
        )
