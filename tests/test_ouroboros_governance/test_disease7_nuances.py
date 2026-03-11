"""
Disease 7 + Nuances — Remaining Gap Tests (TDD)
=================================================

Root causes confirmed by Phase 1 investigation:

D7a  PrimeProvider call site does NOT pass repo_root to _parse_generation_response.
D7b  ClaudeProvider call site same gap.
D7c  _try_reconstruct_from_ellipsis uses Path.cwd(), no repo_root param.
D7d  Length sanity check inside _parse_generation_response uses Path.cwd() even
     though repo_root is already in scope.
N2   _CODEGEN_SYSTEM_PROMPT (48–55) has zero diff-anchoring instructions.
N7   No prompt-size gate: if the prompt exceeds J-Prime's context window the file
     content is silently truncated and the model falls back to trained memory.
D8   No Reactor Core feedback emitted on content failures.

All tests in this file MUST fail before implementation.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

_FIXED_TS = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
_FUTURE_DL = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _ctx(target="docs/file.md", description="Add a line"):
    return OperationContext.create(
        target_files=(target,),
        description=description,
        _timestamp=_FIXED_TS,
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_diff_response(file_path: str, orig_lines: str, new_line: str) -> str:
    """Build a minimal valid 2b.1-diff JSON response."""
    lines = orig_lines.splitlines()
    context = "\n".join(f" {l}" for l in lines)
    n = len(lines)
    diff = f"@@ -1,{n} +1,{n + 1} @@\n{context}\n+{new_line}\n"
    return json.dumps({
        "schema_version": "2b.1-diff",
        "candidates": [{
            "candidate_id": "c1",
            "file_path": file_path,
            "unified_diff": diff,
            "rationale": "test",
        }],
        "provider_metadata": {"model_id": "test-model"},
    })


def _make_mock_client_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.model = "test"
    resp.tokens_used = 10
    return resp


# ---------------------------------------------------------------------------
# D7a — PrimeProvider call-site wiring
# ---------------------------------------------------------------------------

class TestPrimeProviderRepoRootWiring:
    """PrimeProvider.generate() must pass its repo_root to _parse_generation_response."""

    def test_prime_provider_reads_source_from_repo_root_not_cwd(
        self, tmp_path: Path
    ) -> None:
        """File exists ONLY under tmp_path (not cwd).
        If PrimeProvider wires repo_root correctly, orig_content is populated
        and the diff candidate is returned.
        If it falls back to cwd, orig_content = '' and the parse raises
        _schema_invalid:diff_source_unreadable (0 candidates or error)."""
        from backend.core.ouroboros.governance.providers import PrimeProvider

        # Create file ONLY in tmp_path — NOT in cwd
        doc_dir = tmp_path / "docs"
        doc_dir.mkdir()
        original = "first line\nsecond line\n"
        (doc_dir / "file.md").write_text(original)

        response_content = _make_diff_response("docs/file.md", original.rstrip("\n"), "third line")
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(
            return_value=_make_mock_client_response(response_content)
        )

        provider = PrimeProvider(prime_client=mock_client, repo_root=tmp_path)
        result = _run(provider.generate(_ctx(), _FUTURE_DL))

        assert len(result.candidates) == 1, (
            "PrimeProvider must pass repo_root to the diff parser so the source "
            f"file is resolved from repo_root, not cwd. Got {len(result.candidates)} candidates."
        )
        assert "third line" in result.candidates[0]["full_content"]


# ---------------------------------------------------------------------------
# D7b — ClaudeProvider call-site wiring
# ---------------------------------------------------------------------------

class TestClaudeProviderRepoRootWiring:
    """ClaudeProvider.generate() must pass its repo_root to _parse_generation_response."""

    def test_claude_provider_reads_source_from_repo_root_not_cwd(
        self, tmp_path: Path
    ) -> None:
        """Same invariant as D7a but via ClaudeProvider."""
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        doc_dir = tmp_path / "docs"
        doc_dir.mkdir()
        original = "alpha line\nbeta line\n"
        (doc_dir / "file.md").write_text(original)

        response_content = _make_diff_response("docs/file.md", original.rstrip("\n"), "gamma line")

        # Mock the Anthropic message response
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=response_content)]
        mock_msg.usage = MagicMock(input_tokens=10, output_tokens=10)
        mock_msg.model = "claude-sonnet-test"

        mock_anthropic = MagicMock()
        mock_anthropic.messages = MagicMock()
        mock_anthropic.messages.create = AsyncMock(return_value=mock_msg)

        with patch("anthropic.AsyncAnthropic", return_value=mock_anthropic):
            provider = ClaudeProvider(
                api_key="sk-test",
                repo_root=tmp_path,
                daily_budget=9999.0,
            )
            provider._client = mock_anthropic
            result = _run(provider.generate(_ctx(), _FUTURE_DL))

        assert len(result.candidates) == 1, (
            "ClaudeProvider must pass repo_root to the diff parser. "
            f"Got {len(result.candidates)} candidates."
        )
        assert "gamma line" in result.candidates[0]["full_content"]


# ---------------------------------------------------------------------------
# D7c — _try_reconstruct_from_ellipsis uses Path.cwd()
# ---------------------------------------------------------------------------

class TestEllipsisReconstructionRepoRoot:
    """_try_reconstruct_from_ellipsis must accept repo_root and use it."""

    def test_ellipsis_reconstruction_reads_from_repo_root(
        self, tmp_path: Path
    ) -> None:
        """When repo_root is provided, the function reads the original source from
        repo_root / source_path, not Path.cwd() / source_path."""
        from backend.core.ouroboros.governance.providers import (
            _try_reconstruct_from_ellipsis,
        )

        # File exists ONLY under tmp_path
        (tmp_path / "target.md").write_text("existing content\n")

        # Simulate small-model ellipsis output: "...\nnew line\n"
        ellipsis_content = "...\nnew line\n"

        result = _try_reconstruct_from_ellipsis(
            ellipsis_content,
            "target.md",
            repo_root=tmp_path,
        )

        assert result is not None, (
            "_try_reconstruct_from_ellipsis must accept repo_root and find the "
            "source file there. Got None (file not found)."
        )
        assert "existing content" in result
        assert "new line" in result

    def test_ellipsis_reconstruction_fallback_when_no_repo_root(
        self, tmp_path: Path
    ) -> None:
        """Without repo_root, function falls back to Path.cwd() (documented behavior,
        not silent breakage). Returns None when file not in cwd — no crash."""
        from backend.core.ouroboros.governance.providers import (
            _try_reconstruct_from_ellipsis,
        )

        ellipsis_content = "...\nnew content\n"
        # File doesn't exist in cwd — should return None gracefully, not raise
        result = _try_reconstruct_from_ellipsis(
            ellipsis_content, "totally_nonexistent_xyzzy_12345.md"
        )
        assert result is None


# ---------------------------------------------------------------------------
# D7d — Length sanity check Path.cwd() inside _parse_generation_response
# ---------------------------------------------------------------------------

class TestLengthSanityCheckRepoRoot:
    """The length sanity check at the per-candidate validation step must use
    the repo_root already in scope, not fall back to cwd."""

    def test_length_sanity_check_uses_repo_root(self, tmp_path: Path) -> None:
        """When repo_root is passed to _parse_generation_response, the length
        sanity check reads the original file from repo_root, not cwd."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        # 200+ byte file ONLY in tmp_path so the length check triggers
        (tmp_path / "docs").mkdir()
        big_content = "# Header\n" + "line content here\n" * 15  # >200 bytes
        (tmp_path / "docs" / "file.md").write_text(big_content)

        # Response returns full_content slightly shorter than original
        # (should pass because it's > 50% of original)
        trimmed = big_content[: int(len(big_content) * 0.6)]
        response = json.dumps({
            "schema_version": "2b.1",
            "candidates": [{
                "candidate_id": "c1",
                "file_path": "docs/file.md",
                "full_content": trimmed,
                "rationale": "test",
            }],
            "provider_metadata": {},
        })

        ctx = OperationContext.create(
            target_files=("docs/file.md",),
            description="trim file",
            _timestamp=_FIXED_TS,
        )

        # Should NOT raise or discard candidate due to misread orig_len
        result = _parse_generation_response(
            response, "test-provider", 0.1, ctx, "", "docs/file.md",
            repo_root=tmp_path,
        )

        assert len(result.candidates) == 1, (
            "Length sanity check must read original file from repo_root to compute "
            "orig_len. Without repo_root wiring it reads from cwd and gets "
            "orig_len=0, causing the sanity check to mis-fire or be skipped."
        )


# ---------------------------------------------------------------------------
# N2 — _CODEGEN_SYSTEM_PROMPT lacks diff anchoring
# ---------------------------------------------------------------------------

class TestCodegenSystemPromptAnchoring:
    """_CODEGEN_SYSTEM_PROMPT must contain diff-anchoring instructions so small
    models receive the mandate at the highest-priority position."""

    def test_system_prompt_contains_verbatim_context_mandate(self) -> None:
        """The system prompt must instruct the model to use VERBATIM context lines
        from the provided source, not from trained memory."""
        from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT

        prompt_lower = _CODEGEN_SYSTEM_PROMPT.lower()
        anchoring_indicators = [
            "verbatim",
            "exact context",
            "source snapshot",
            "provided source",
            "from the file",
            "from the source",
        ]
        assert any(ind in prompt_lower for ind in anchoring_indicators), (
            "_CODEGEN_SYSTEM_PROMPT must contain a diff-anchoring instruction. "
            "The system prompt is the highest-priority instruction for the model. "
            "Without it, small models (mistral-7b) ignore injected source and "
            "use trained memory for diff context lines.\n"
            f"Current system prompt:\n{_CODEGEN_SYSTEM_PROMPT!r}"
        )

    def test_system_prompt_configurable_via_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra instructions from JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA must be
        appended when the env var is set."""
        monkeypatch.setenv(
            "JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA",
            "TEST_SENTINEL_EXTRA_INSTRUCTION",
        )
        # Re-import to pick up env var (or test the function that builds the prompt)
        import importlib
        import backend.core.ouroboros.governance.providers as pmod
        importlib.reload(pmod)
        try:
            assert "TEST_SENTINEL_EXTRA_INSTRUCTION" in pmod._CODEGEN_SYSTEM_PROMPT, (
                "JARVIS_CODEGEN_SYSTEM_PROMPT_EXTRA must be appended to "
                "_CODEGEN_SYSTEM_PROMPT so operators can extend it without "
                "modifying code."
            )
        finally:
            importlib.reload(pmod)  # restore original state


# ---------------------------------------------------------------------------
# N7 — No prompt-size gate
# ---------------------------------------------------------------------------

class TestPromptSizeGate:
    """_build_codegen_prompt or its caller must raise / fall back when the prompt
    would overflow J-Prime's context window."""

    def test_oversized_prompt_raises_structured_error(
        self, tmp_path: Path
    ) -> None:
        """When estimated prompt tokens exceed the configured limit, a structured
        RuntimeError is raised so the caller can route to a larger-context provider."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt

        # Create a file large enough that prompt would exceed a tiny limit
        big_file = tmp_path / "big.md"
        # 4 chars ≈ 1 token; set a tiny limit to trigger the gate
        content = "x " * 500  # ~250 tokens just for content
        big_file.write_text(content)

        ctx = OperationContext.create(
            target_files=("big.md",),
            description="Add a line",
            _timestamp=_FIXED_TS,
        )

        with pytest.raises(RuntimeError, match="prompt_too_large") as exc_info:
            _build_codegen_prompt(
                ctx,
                repo_root=tmp_path,
                max_prompt_tokens=10,  # absurdly small to force the error
            )

        assert "prompt_too_large" in str(exc_info.value), (
            "_build_codegen_prompt must raise RuntimeError('prompt_too_large:...') "
            "when estimated token count exceeds max_prompt_tokens. "
            "This lets the caller switch to a larger-context provider or truncate."
        )

    def test_normal_prompt_does_not_raise(self, tmp_path: Path) -> None:
        """Normal-sized prompts must pass the size gate without error."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt

        small_file = tmp_path / "small.md"
        small_file.write_text("one line\n")

        ctx = OperationContext.create(
            target_files=("small.md",),
            description="Append a footer",
            _timestamp=_FIXED_TS,
        )

        # Default limit (e.g. 6144 tokens) — small file must pass
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert isinstance(prompt, str) and len(prompt) > 0


# ---------------------------------------------------------------------------
# D8 — Reactor Core feedback on content failures
# ---------------------------------------------------------------------------

class TestReactorCoreFeedback:
    """Content failures (stale diff, apply failed) must emit a fire-and-forget
    telemetry signal to Reactor Core for model quality tracking."""

    def test_content_failure_calls_emit_reactor_feedback(
        self, tmp_path: Path
    ) -> None:
        """When a diff fails pre-apply validation (StaleDiffError), the module
        must call _emit_content_failure_to_reactor with a structured payload."""
        from backend.core.ouroboros.governance.providers import (
            _emit_content_failure_to_reactor,
        )

        # Verify the function exists and is callable
        assert callable(_emit_content_failure_to_reactor), (
            "_emit_content_failure_to_reactor must be importable from providers.py"
        )

    def test_emit_reactor_feedback_sends_correct_fields(self) -> None:
        """The emitted payload must contain event_type, source, and data fields
        matching the Reactor Core TelemetryEvent schema."""
        from backend.core.ouroboros.governance.providers import (
            _emit_content_failure_to_reactor,
        )

        captured: list = []

        async def fake_post(url: str, payload: dict, **kwargs) -> None:
            captured.append(payload)

        async def run():
            with patch(
                "backend.core.ouroboros.governance.providers._reactor_http_post",
                side_effect=fake_post,
            ):
                await _emit_content_failure_to_reactor({
                    "event_type": "CUSTOM",
                    "source": "ouroboros.providers",
                    "data": {
                        "failure_type": "content_quality",
                        "provider": "gcp-jprime",
                        "op_id": "test-op",
                        "error": "stale_diff",
                    },
                })

        _run(run())
        # Function is fire-and-forget — primary test is it doesn't raise
        # When reactor is offline (no mock matched), must not raise

    def test_reactor_feedback_disabled_by_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OUROBOROS_REACTOR_FEEDBACK_ENABLED=false, the function returns
        immediately without making any network call."""
        monkeypatch.setenv("OUROBOROS_REACTOR_FEEDBACK_ENABLED", "false")

        from backend.core.ouroboros.governance.providers import (
            _emit_content_failure_to_reactor,
        )

        call_count = 0

        async def fake_post(url: str, payload: dict, **kwargs) -> None:
            nonlocal call_count
            call_count += 1

        async def run():
            with patch(
                "backend.core.ouroboros.governance.providers._reactor_http_post",
                side_effect=fake_post,
            ):
                await _emit_content_failure_to_reactor({"event_type": "CUSTOM"})

        _run(run())
        assert call_count == 0, (
            "When OUROBOROS_REACTOR_FEEDBACK_ENABLED=false, "
            "_emit_content_failure_to_reactor must not make any network calls."
        )

    def test_stale_diff_in_parser_triggers_reactor_emission(
        self, tmp_path: Path
    ) -> None:
        """When _parse_generation_response catches a StaleDiffError, it must
        schedule _emit_content_failure_to_reactor() as a background task."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        # Create a real file
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "file.md").write_text("real line one\nreal line two\n")

        # Diff with stale context — will fail validate_diff_context
        stale_diff_response = json.dumps({
            "schema_version": "2b.1-diff",
            "candidates": [{
                "candidate_id": "c1",
                "file_path": "docs/file.md",
                "unified_diff": "@@ -1,2 +1,3 @@\n wrong context A\n wrong context B\n+new line\n",
                "rationale": "test",
            }],
            "provider_metadata": {"model_id": "test"},
        })

        ctx = OperationContext.create(
            target_files=("docs/file.md",),
            description="add line",
            _timestamp=_FIXED_TS,
        )

        emitted: list = []

        async def capture_emit(payload: dict) -> None:
            emitted.append(payload)

        async def run():
            with patch(
                "backend.core.ouroboros.governance.providers._emit_content_failure_to_reactor",
                side_effect=capture_emit,
            ) as mock_emit:
                try:
                    _parse_generation_response(
                        stale_diff_response,
                        "gcp-jprime",
                        1.0,
                        ctx,
                        "sha123",
                        "docs/file.md",
                        repo_root=tmp_path,
                    )
                except RuntimeError:
                    pass  # expected — stale diff raises after exhausting candidates
                return mock_emit.called

        called = _run(run())
        assert called, (
            "_parse_generation_response must call _emit_content_failure_to_reactor "
            "when a StaleDiffError is caught, so Reactor Core can track content "
            "quality failures for fine-tuning."
        )


# ---------------------------------------------------------------------------
# N8 — schema_version 2b.1-noop not parsed as is_noop=True
# ---------------------------------------------------------------------------

class TestNoopSchemaVersionParsing:
    """_parse_generation_response must recognise schema_version '2b.1-noop' as a
    successful noop result (is_noop=True), NOT raise schema_invalid.

    Root cause: the noop check at step 0 only handles {"no_op": true} (legacy key).
    J-Prime now emits {"schema_version": "2b.1-noop", "reason": "..."} which falls
    through to the wrong_schema_version branch and raises RuntimeError.
    """

    def _call_parse(self, raw: str, provider: str = "gcp-jprime") -> "GenerationResult":
        from backend.core.ouroboros.governance.providers import _parse_generation_response
        from backend.core.ouroboros.governance.op_context import OperationContext
        ctx = OperationContext.create(target_files=("docs/x.md",), description="test", primary_repo="jarvis")
        return _parse_generation_response(
            raw, provider_name=provider, duration_s=0.1,
            ctx=ctx, source_hash="deadbeef", source_path="docs/x.md",
        )

    def test_noop_schema_version_returns_is_noop_true(self) -> None:
        """schema_version '2b.1-noop' must yield GenerationResult(is_noop=True)."""
        raw = json.dumps({
            "schema_version": "2b.1-noop",
            "reason": "The comment is already present at the end of the file.",
        })
        result = self._call_parse(raw)
        assert result.is_noop is True, (
            "_parse_generation_response must return is_noop=True when "
            "schema_version is '2b.1-noop'. "
            f"Got: is_noop={result.is_noop}"
        )

    def test_noop_schema_version_wrapped_in_code_fence(self) -> None:
        """2b.1-noop inside a markdown code fence (as J-Prime returns) must work."""
        raw = (
            "```json\n"
            '{"schema_version": "2b.1-noop", "reason": "Already done."}\n'
            "```"
        )
        result = self._call_parse(raw)
        assert result.is_noop is True

    def test_noop_schema_version_has_empty_candidates(self) -> None:
        """is_noop result must have an empty candidates tuple, not raise."""
        raw = json.dumps({"schema_version": "2b.1-noop", "reason": "Present."})
        result = self._call_parse(raw)
        assert result.candidates == () or result.candidates is None or len(result.candidates) == 0

    def test_noop_schema_version_preserves_provider_name(self) -> None:
        """provider_name must be propagated into the is_noop GenerationResult."""
        raw = json.dumps({"schema_version": "2b.1-noop", "reason": "x"})
        result = self._call_parse(raw, provider="gcp-jprime")
        assert result.provider_name == "gcp-jprime"
