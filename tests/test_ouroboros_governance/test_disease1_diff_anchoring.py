"""
Disease 1 — Diff Anchoring Tests (TDD)
=======================================

Root disease: J-Prime generates diffs from trained memory, not injected content.

These tests define the exact contract that must hold BEFORE any implementation.
They MUST fail when first run.

Fix surfaces under test
-----------------------
D1  Pre-apply diff validation gate:
      Validate context lines against actual file BEFORE attempting apply.
      If context lines don't match → StaleDiffError (structured), not ValueError.

D2  Idempotency detection:
      Check if the requested change is already present before calling any model.
      Already present → return no-op GenerationResult immediately.

D3  No-op response schema:
      The parser must handle `{"no_op": true, "reason": "..."}` without crashing.
      No-op → GenerationResult.is_noop == True.

D4  Strengthened diff prompt instruction:
      _build_codegen_prompt must instruct the model to:
        (a) Use verbatim context lines from the provided source
        (b) Return no_op if change is already present
        (c) Include source_sha256 field in the diff response

D5  FailbackFSM failure classification:
      diff_apply_failed (content failure) must NOT trigger FSM state change.
      Only infrastructure failures (timeout, connection error) trigger FSM.

D6  Source path resolution uses repo_root not cwd:
      In the 2b.1-diff branch, source file must be read from repo_root / path,
      not Path.cwd() / path.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
_FUTURE_DL = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _ctx(
    *,
    target_files: Tuple[str, ...] = ("docs/file.md",),
    description: str = "Append a line",
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        _timestamp=_FIXED_TS,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# D1 — Pre-apply diff validation gate (StaleDiffGuard)
# ---------------------------------------------------------------------------

class TestStaleDiffGuard:
    """Pre-apply validation must catch stale context BEFORE attempting apply."""

    def test_stale_diff_raises_structured_error(self) -> None:
        """A diff whose context lines don't match the actual file raises
        StaleDiffError with structured fields before any file mutation."""
        from backend.core.ouroboros.governance.providers import (
            StaleDiffError,
            validate_diff_context,
        )

        original = "line one\nline two\nline three\n"
        # Diff expects context "line X\nline Y" which is NOT in original
        stale_diff = "@@ -1,2 +1,3 @@\n line X\n line Y\n+new line\n"

        with pytest.raises(StaleDiffError) as exc_info:
            validate_diff_context(original, stale_diff)

        err = exc_info.value
        # Must carry structured info
        assert hasattr(err, "expected_context"), "StaleDiffError must have expected_context"
        assert hasattr(err, "actual_lines"), "StaleDiffError must have actual_lines"
        assert hasattr(err, "hunk_line"), "StaleDiffError must have hunk_line"

    def test_valid_diff_passes_guard(self) -> None:
        """A diff whose context lines exactly match the file passes without error."""
        from backend.core.ouroboros.governance.providers import validate_diff_context

        original = "line one\nline two\nline three\n"
        valid_diff = "@@ -1,2 +1,3 @@\n line one\n line two\n+new line\n"

        # Must not raise
        validate_diff_context(original, valid_diff)

    def test_append_hunk_at_eof_passes_guard(self) -> None:
        """Hunk appending at end of file (context = last lines) must pass."""
        from backend.core.ouroboros.governance.providers import validate_diff_context

        original = "first\nsecond\nthird\n"
        # Context = last 2 lines, appending after them
        append_diff = "@@ -2,2 +2,3 @@\n second\n third\n+fourth\n"

        validate_diff_context(original, append_diff)

    def test_stale_diff_does_not_mutate_file(self, tmp_path: Path) -> None:
        """validate_diff_context must be a pure check — it must NOT mutate the file."""
        from backend.core.ouroboros.governance.providers import (
            StaleDiffError,
            validate_diff_context,
        )

        file = tmp_path / "doc.md"
        content = "alpha\nbeta\ngamma\n"
        file.write_text(content)

        stale_diff = "@@ -1,2 +1,3 @@\n wrong\n context\n+new\n"
        with pytest.raises(StaleDiffError):
            validate_diff_context(content, stale_diff)

        # File untouched
        assert file.read_text() == content


# ---------------------------------------------------------------------------
# D2 — Idempotency detection (pre-generation check)
# ---------------------------------------------------------------------------

class TestIdempotencyDetection:
    """Change already present → no-op result without calling any model."""

    def test_detects_exact_line_already_present(self, tmp_path: Path) -> None:
        """If the exact target string already appears in the file,
        is_change_needed returns False."""
        from backend.core.ouroboros.governance.providers import is_change_needed

        file = tmp_path / "doc.md"
        file.write_text("# Title\n<!-- monitored by Ouroboros -->\n")

        assert is_change_needed(file, "<!-- monitored by Ouroboros -->") is False

    def test_detects_absent_change_as_needed(self, tmp_path: Path) -> None:
        """If the target string is not in the file, is_change_needed returns True."""
        from backend.core.ouroboros.governance.providers import is_change_needed

        file = tmp_path / "doc.md"
        file.write_text("# Title\nsome content\n")

        assert is_change_needed(file, "<!-- monitored by Ouroboros -->") is True

    def test_missing_file_treats_as_needed(self, tmp_path: Path) -> None:
        """Non-existent file → change is needed (will be created)."""
        from backend.core.ouroboros.governance.providers import is_change_needed

        non_existent = tmp_path / "missing.md"
        assert is_change_needed(non_existent, "anything") is True

    def test_partial_line_match_does_not_count(self, tmp_path: Path) -> None:
        """A substring match on a different line does not suppress the change."""
        from backend.core.ouroboros.governance.providers import is_change_needed

        file = tmp_path / "doc.md"
        # "monitored" is present but NOT the full sentinel line
        file.write_text("# monitored systems\nsome content\n")

        # The sentinel is the full line — partial substring should still require change
        assert is_change_needed(file, "<!-- monitored by Ouroboros -->") is True


# ---------------------------------------------------------------------------
# D3 — No-op response schema handling
# ---------------------------------------------------------------------------

class TestNoOpResponseSchema:
    """Parser must handle {"no_op": true} without crashing."""

    def test_noop_response_parsed_to_noop_result(self) -> None:
        """A model response of {"no_op": true, "reason": "..."} must produce
        a GenerationResult with is_noop=True."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"no_op": True, "reason": "Change already present"})
        result = _parse_generation_response(
            raw, "test-provider", 0.1, _ctx(), "sha123", "docs/file.md"
        )

        assert isinstance(result, GenerationResult)
        assert getattr(result, "is_noop", False) is True, (
            "GenerationResult must have is_noop=True for no_op response"
        )

    def test_noop_result_has_no_candidates(self) -> None:
        """A no-op result carries zero candidates (nothing to apply)."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"no_op": True, "reason": "Already present"})
        result = _parse_generation_response(
            raw, "test-provider", 0.1, _ctx(), "sha123", "docs/file.md"
        )

        assert len(result.candidates) == 0, "no_op result must have zero candidates"

    def test_noop_without_reason_field_still_parses(self) -> None:
        """{"no_op": true} without reason field is still valid."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"no_op": True})
        result = _parse_generation_response(
            raw, "test-provider", 0.0, _ctx(), "", ""
        )
        assert getattr(result, "is_noop", False) is True


# ---------------------------------------------------------------------------
# D4 — Strengthened diff prompt instructions
# ---------------------------------------------------------------------------

class TestStrengthendedDiffPrompt:
    """_build_codegen_prompt must contain anchoring + idempotency instructions."""

    def _build_prompt(self, tmp_path: Path, content: str = "alpha\nbeta\n") -> str:
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt

        target = tmp_path / "doc.md"
        target.write_text(content)
        ctx = OperationContext.create(
            target_files=("doc.md",),
            description="Append a footer line",
            _timestamp=_FIXED_TS,
        )
        return _build_codegen_prompt(ctx, repo_root=tmp_path)

    def test_prompt_instructs_verbatim_context(self, tmp_path: Path) -> None:
        """The diff schema instruction must tell the model to use verbatim
        context lines from the Source Snapshot, not from trained memory."""
        prompt = self._build_prompt(tmp_path)
        assert any(
            phrase in prompt
            for phrase in [
                "verbatim",
                "exact context",
                "copy.*source",
                "Source Snapshot",
                "provided source",
            ]
        ), (
            "Prompt must instruct model to ground diff context in the provided "
            "source snapshot, not trained memory. "
            f"Actual prompt excerpt:\n{prompt[prompt.find('Output Schema'):prompt.find('Output Schema')+800]}"
        )

    def test_prompt_instructs_no_op_for_already_present(self, tmp_path: Path) -> None:
        """The prompt must tell the model: if the change is already present,
        return {\"no_op\": true} instead of a diff."""
        prompt = self._build_prompt(tmp_path)
        assert "no_op" in prompt, (
            "Prompt must include no_op instruction so the model can signal "
            "idempotency without generating a stale diff."
        )

    def test_prompt_requires_source_sha256_in_diff_response(self, tmp_path: Path) -> None:
        """The diff response schema must include a source_sha256 field so the
        parser can detect hash mismatches before attempting apply."""
        prompt = self._build_prompt(tmp_path)
        assert "source_sha256" in prompt, (
            "Diff response schema must require source_sha256 so the model "
            "echoes back what version of the file it generated against."
        )

    def test_prompt_includes_sha256_of_actual_file(self, tmp_path: Path) -> None:
        """SHA-256 of the target file must appear in the Source Snapshot header."""
        import hashlib

        content = "alpha\nbeta\n"
        expected_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        prompt = self._build_prompt(tmp_path, content)

        assert expected_hash in prompt, (
            "Full SHA-256 prefix must appear in the Source Snapshot section "
            "so the model knows exactly which file version it's working from."
        )


# ---------------------------------------------------------------------------
# D5 — FailbackFSM failure classification
# ---------------------------------------------------------------------------

class TestFailbackFSMFailureClassification:
    """Content failures must NOT trigger FSM infrastructure state transitions."""

    def test_diff_apply_failure_does_not_change_fsm_state(self) -> None:
        """A diff_apply_failed error is a CONTENT failure, not an infra failure.
        FSM must stay in PRIMARY_READY."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
            FailbackState,
        )

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.provider_name = "gcp-jprime"
        fallback.provider_name = "claude-api"

        primary.generate.side_effect = RuntimeError(
            "gcp-jprime_schema_invalid:diff_apply_failed_all_candidates"
        )
        fallback.generate.return_value = MagicMock(spec=GenerationResult, candidates=[])

        gen = CandidateGenerator(primary=primary, fallback=fallback)

        # Primary fails with content error
        _run(gen.generate(_ctx(), _FUTURE_DL))

        # FSM must stay PRIMARY_READY for content failures
        assert gen.fsm.state == FailbackState.PRIMARY_READY, (
            f"Content failure should NOT change FSM state. "
            f"Got: {gen.fsm.state}. Expected: PRIMARY_READY."
        )

    def test_infrastructure_failure_triggers_fsm_fallback(self) -> None:
        """A timeout/connection error IS an infra failure → FSM → FALLBACK_ACTIVE."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
            FailbackState,
        )

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.provider_name = "gcp-jprime"
        fallback.provider_name = "claude-api"

        # Simulate infrastructure failure (connection refused / timeout)
        primary.generate.side_effect = RuntimeError("gcp-jprime_connection_refused")
        fallback.generate.return_value = MagicMock(spec=GenerationResult, candidates=[])

        gen = CandidateGenerator(primary=primary, fallback=fallback)
        _run(gen.generate(_ctx(), _FUTURE_DL))

        assert gen.fsm.state == FailbackState.FALLBACK_ACTIVE, (
            f"Infrastructure failure must push FSM to FALLBACK_ACTIVE. "
            f"Got: {gen.fsm.state}"
        )

    def test_content_failure_counter_increments_separately(self) -> None:
        """Content failures must be tracked separately from infra failures
        so the FSM probe logic can distinguish them."""
        from backend.core.ouroboros.governance.candidate_generator import (
            CandidateGenerator,
        )

        primary = AsyncMock()
        fallback = AsyncMock()
        primary.provider_name = "gcp-jprime"
        fallback.provider_name = "claude-api"
        primary.generate.side_effect = RuntimeError(
            "gcp-jprime_schema_invalid:diff_apply_failed_all_candidates"
        )
        fallback.generate.return_value = MagicMock(spec=GenerationResult, candidates=[])

        gen = CandidateGenerator(primary=primary, fallback=fallback)
        _run(gen.generate(_ctx(), _FUTURE_DL))

        assert gen.fsm.content_failure_count >= 1, (
            "CandidateGenerator.fsm must track content_failure_count separately."
        )


# ---------------------------------------------------------------------------
# D6 — Source path resolution uses repo_root not cwd
# ---------------------------------------------------------------------------

class TestSourcePathResolution:
    """Diff parsing must resolve relative paths from repo_root, not cwd."""

    def test_diff_parse_reads_from_repo_root_not_cwd(self, tmp_path: Path) -> None:
        """When repo_root != cwd, the 2b.1-diff branch must read the source file
        from repo_root / source_path, not Path.cwd() / source_path."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        # Create file ONLY under tmp_path (not cwd)
        doc = tmp_path / "docs" / "file.md"
        doc.parent.mkdir(parents=True)
        original_content = "first line\nsecond line\n"
        doc.write_text(original_content)

        # Valid diff against the real content
        valid_diff_response = json.dumps({
            "schema_version": "2b.1-diff",
            "candidates": [{
                "candidate_id": "c1",
                "file_path": "docs/file.md",
                "unified_diff": "@@ -1,2 +1,3 @@\n first line\n second line\n+third line\n",
                "rationale": "Added third line",
            }],
            "provider_metadata": {"model_id": "jarvis-prime"},
        })

        ctx = OperationContext.create(
            target_files=("docs/file.md",),
            description="Add third line",
            _timestamp=_FIXED_TS,
        )

        # Must succeed because it reads from tmp_path, not cwd
        result = _parse_generation_response(
            valid_diff_response,
            "gcp-jprime",
            1.0,
            ctx,
            "sha123",
            "docs/file.md",
            repo_root=tmp_path,   # <-- explicit repo_root passed
        )

        assert len(result.candidates) == 1
        assert "third line" in result.candidates[0]["full_content"]

    def test_diff_parse_without_repo_root_falls_back_gracefully(self, tmp_path: Path) -> None:
        """When repo_root is not passed, behavior is documented and not a silent
        empty-file bug. Either it uses cwd correctly or raises a clear error."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        # A diff that would fail because the source doesn't exist at cwd
        diff_response = json.dumps({
            "schema_version": "2b.1-diff",
            "candidates": [{
                "candidate_id": "c1",
                "file_path": "totally/nonexistent/file.md",
                "unified_diff": "@@ -1,1 +1,2 @@\n context\n+new\n",
                "rationale": "test",
            }],
            "provider_metadata": {},
        })

        ctx = OperationContext.create(
            target_files=("totally/nonexistent/file.md",),
            description="test",
            _timestamp=_FIXED_TS,
        )

        # Must NOT silently succeed with empty orig_content (which would corrupt)
        with pytest.raises(Exception):
            _parse_generation_response(
                diff_response, "gcp-jprime", 1.0, ctx, "", "totally/nonexistent/file.md"
            )
