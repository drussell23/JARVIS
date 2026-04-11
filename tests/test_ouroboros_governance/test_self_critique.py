"""Tests for the Self-Critique Engine (Phase 3a).

The critique engine runs after a successful VERIFY + auto-commit and
evaluates the applied diff against the original goal via a cheap DW
call. Low ratings become FEEDBACK memories; high ratings reinforce
file reputation in the MemoryEngine.

These tests lock in:
  * CritiqueResult dataclass invariants (clamping, frozen-ness)
  * parse_critique_json: good path, malformed, missing fields, bounds
  * build_critique_prompt: includes all required sections, truncates diffs
  * collect_op_diff: commit path + working-tree fallback + git failure
  * CritiqueEngine gating: env disable, trivial skip, empty description,
    empty diff
  * Provider fallback: primary success, primary fail → fallback success,
    both fail → skip
  * Memory writeback: poor path → UserPreferenceStore; excellent path →
    MemoryEngine reputation
  * Telemetry counters update correctly
  * Non-blocking guarantees: provider exceptions, writeback exceptions,
    parse errors all return valid results without raising

All tests avoid network and disk beyond a tmp git repo.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.self_critique import (
    ClaudeCritiqueProvider,
    CritiqueEngine,
    CritiqueRequest,
    CritiqueResult,
    DoublewordCritiqueProvider,
    _extract_json_block,
    build_critique_prompt,
    collect_op_diff,
    is_self_critique_enabled,
    parse_critique_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_critique_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure each test starts with critique env vars at defaults."""
    for key in list(__import__("os").environ.keys()):
        if key.startswith("JARVIS_CRITIQUE_") or key == "JARVIS_SELF_CRITIQUE_ENABLED":
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Init a throwaway git repo with one committed file, return its root."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=tmp_path, check=True,
    )
    (tmp_path / "foo.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "foo.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


@dataclass
class _FakeProvider:
    """Scriptable fake CritiqueProvider for tests."""

    name: str = "stub"
    response: str = "{}"
    fail_with: Optional[Exception] = None
    calls: List[CritiqueRequest] = field(default_factory=list)

    async def critique(self, request: CritiqueRequest) -> str:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return self.response


# ---------------------------------------------------------------------------
# TestCritiqueResult
# ---------------------------------------------------------------------------


class TestCritiqueResult:
    def test_frozen(self):
        result = CritiqueResult(
            op_id="op-1", rating=3, rationale="ok", matches_goal=True,
            completeness=3, concerns=(), provider_name="stub",
            schema_version="critique.1", duration_s=0.1, cost_usd=0.0,
            raw_response="", parse_ok=True,
        )
        with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
            result.rating = 5  # type: ignore[misc]

    def test_is_poor_uses_env_threshold(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_CRITIQUE_POOR_THRESHOLD", "3")
        r = CritiqueResult(
            op_id="op", rating=3, rationale="", matches_goal=True,
            completeness=3, concerns=(), provider_name="",
            schema_version="critique.1", duration_s=0.0, cost_usd=0.0,
            raw_response="", parse_ok=True,
        )
        assert r.is_poor is True

    def test_is_excellent(self):
        r = CritiqueResult(
            op_id="op", rating=5, rationale="", matches_goal=True,
            completeness=5, concerns=(), provider_name="",
            schema_version="critique.1", duration_s=0.0, cost_usd=0.0,
            raw_response="", parse_ok=True,
        )
        assert r.is_excellent is True

    def test_to_dict_contains_all_fields(self):
        r = CritiqueResult(
            op_id="op-42", rating=4, rationale="solid", matches_goal=True,
            completeness=4, concerns=("minor nit",), provider_name="doubleword",
            schema_version="critique.1", duration_s=0.55, cost_usd=0.001,
            raw_response="raw", parse_ok=True,
        )
        d = r.to_dict()
        assert d["op_id"] == "op-42"
        assert d["rating"] == 4
        assert d["concerns"] == ["minor nit"]
        assert d["duration_s"] == 0.55
        assert "parse_ok" in d


# ---------------------------------------------------------------------------
# TestJSONExtraction
# ---------------------------------------------------------------------------


class TestJSONExtraction:
    def test_plain_json_object(self):
        assert _extract_json_block('{"a": 1}') == '{"a": 1}'

    def test_fenced_json_block(self):
        raw = '```json\n{"rating": 5}\n```'
        assert _extract_json_block(raw) == '{"rating": 5}'

    def test_fenced_without_language(self):
        raw = '```\n{"rating": 3}\n```'
        assert _extract_json_block(raw) == '{"rating": 3}'

    def test_embedded_in_prose(self):
        raw = "Here is my analysis:\n{\"rating\": 2}\nHope that helps."
        block = _extract_json_block(raw)
        assert block is not None
        assert '"rating": 2' in block

    def test_empty_returns_none(self):
        assert _extract_json_block("") is None

    def test_no_json_returns_none(self):
        assert _extract_json_block("no json here at all") is None


# ---------------------------------------------------------------------------
# TestParseCritiqueJSON
# ---------------------------------------------------------------------------


class TestParseCritiqueJSON:
    def test_happy_path(self):
        raw = json.dumps({
            "rating": 4,
            "matches_goal": True,
            "completeness": 4,
            "rationale": "Looks good",
            "concerns": ["minor naming"],
        })
        data, ok = parse_critique_json(raw, op_id="op-1")
        assert ok is True
        assert data["rating"] == 4
        assert data["matches_goal"] is True
        assert data["completeness"] == 4
        assert data["rationale"] == "Looks good"
        assert data["concerns"] == ["minor naming"]

    def test_rating_clamped_high(self):
        raw = json.dumps({"rating": 99, "rationale": "x"})
        data, ok = parse_critique_json(raw, op_id="op-1")
        assert ok is True
        assert data["rating"] == 5

    def test_rating_clamped_low(self):
        raw = json.dumps({"rating": -5, "rationale": "x"})
        data, ok = parse_critique_json(raw, op_id="op-1")
        assert data["rating"] == 1

    def test_rating_non_integer_fallback(self):
        raw = json.dumps({"rating": "garbage", "rationale": "x"})
        data, ok = parse_critique_json(raw, op_id="op-1")
        assert ok is True
        assert data["rating"] == 3  # fallback

    def test_missing_fields_defaults(self):
        raw = "{}"
        data, ok = parse_critique_json(raw, op_id="op-1")
        assert ok is True
        assert data["rating"] == 3
        assert data["completeness"] == 3
        assert data["matches_goal"] is True
        assert data["rationale"] == "(no rationale provided)"

    def test_malformed_json_returns_defaults(self):
        data, ok = parse_critique_json("not valid json {", op_id="op-1")
        assert ok is False
        assert data["rating"] == 3
        assert "parse_failure" in data["rationale"]

    def test_non_dict_returns_defaults(self):
        data, ok = parse_critique_json(json.dumps([1, 2, 3]), op_id="op-1")
        assert ok is False

    def test_concerns_truncated_to_10(self):
        concerns = [f"concern {i}" for i in range(25)]
        raw = json.dumps({"rating": 3, "rationale": "x", "concerns": concerns})
        data, _ = parse_critique_json(raw, op_id="op-1")
        assert len(data["concerns"]) == 10

    def test_rationale_truncated(self):
        raw = json.dumps({"rating": 3, "rationale": "x" * 5000})
        data, _ = parse_critique_json(raw, op_id="op-1")
        assert len(data["rationale"]) <= 800


# ---------------------------------------------------------------------------
# TestBuildCritiquePrompt
# ---------------------------------------------------------------------------


class TestBuildCritiquePrompt:
    def test_contains_all_sections(self):
        req = CritiqueRequest(
            op_id="op-1",
            goal="Fix the bug in login flow",
            diff="- old\n+ new\n",
            risk_tier="notify_apply",
            target_files=("auth/login.py",),
            test_summary="10/10 tests passed",
            deadline_s=30.0,
        )
        prompt = build_critique_prompt(req)
        assert "## Original Goal" in prompt
        assert "Fix the bug in login flow" in prompt
        assert "## Risk Tier" in prompt
        assert "notify_apply" in prompt
        assert "## Target Files" in prompt
        assert "auth/login.py" in prompt
        assert "## Test Summary" in prompt
        assert "10/10 tests passed" in prompt
        assert "## Applied Diff" in prompt
        assert "rating" in prompt.lower()

    def test_diff_truncation(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_CRITIQUE_MAX_DIFF_CHARS", "200")
        big_diff = "+ line\n" * 200  # ~1400 chars
        req = CritiqueRequest(
            op_id="op", goal="g", diff=big_diff, risk_tier="low",
            target_files=("f.py",), test_summary="", deadline_s=30.0,
        )
        prompt = build_critique_prompt(req)
        assert "diff truncated" in prompt

    def test_no_target_files(self):
        req = CritiqueRequest(
            op_id="op", goal="g", diff="+ x", risk_tier="low",
            target_files=(), test_summary="", deadline_s=30.0,
        )
        prompt = build_critique_prompt(req)
        assert "(none declared)" in prompt


# ---------------------------------------------------------------------------
# TestCollectOpDiff
# ---------------------------------------------------------------------------


class TestCollectOpDiff:
    def test_working_tree_diff(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 2\n")
        diff = collect_op_diff(
            tmp_git_repo, commit_hash=None, target_files=("foo.py",),
        )
        assert "foo.py" in diff
        assert "-    return 1" in diff
        assert "+    return 2" in diff

    def test_committed_diff(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 99\n")
        subprocess.run(["git", "add", "foo.py"], cwd=tmp_git_repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "bump"],
            cwd=tmp_git_repo, check=True,
        )
        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        )
        head = head_proc.stdout.strip()
        diff = collect_op_diff(
            tmp_git_repo, commit_hash=head, target_files=("foo.py",),
        )
        assert "+    return 99" in diff

    def test_nonexistent_repo_returns_empty(self, tmp_path: Path):
        diff = collect_op_diff(
            tmp_path / "nope", commit_hash=None, target_files=("x.py",),
        )
        assert diff == ""


# ---------------------------------------------------------------------------
# TestCritiqueEngineGating
# ---------------------------------------------------------------------------


class TestCritiqueEngineGating:
    @pytest.mark.asyncio
    async def test_disabled_via_env_skips(
        self, tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("JARVIS_SELF_CRITIQUE_ENABLED", "false")
        provider = _FakeProvider()
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="fix bug", target_files=("foo.py",),
            risk_tier="notify_apply",
        )
        assert result.skip_reason == "disabled"
        assert len(provider.calls) == 0

    @pytest.mark.asyncio
    async def test_trivial_tier_skipped(self, tmp_git_repo: Path):
        provider = _FakeProvider()
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="bump version", target_files=("foo.py",),
            risk_tier="trivial",
        )
        assert result.skip_reason == "skip_tier:trivial"
        assert len(provider.calls) == 0

    @pytest.mark.asyncio
    async def test_skip_trivial_env_false_does_not_skip(
        self, tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With JARVIS_CRITIQUE_SKIP_TRIVIAL=false, trivial ops are evaluated."""
        monkeypatch.setenv("JARVIS_CRITIQUE_SKIP_TRIVIAL", "false")
        # Make a working-tree diff so diff collection succeeds.
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({"rating": 5, "rationale": "good"})
        )
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="bump", target_files=("foo.py",),
            risk_tier="trivial",
        )
        assert result.skip_reason is None
        assert result.rating == 5

    @pytest.mark.asyncio
    async def test_empty_description_skipped(self, tmp_git_repo: Path):
        provider = _FakeProvider()
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="", target_files=("foo.py",),
            risk_tier="notify_apply",
        )
        assert result.skip_reason == "empty_description"

    @pytest.mark.asyncio
    async def test_empty_diff_skipped(self, tmp_git_repo: Path):
        """No working-tree changes → no diff → skip."""
        provider = _FakeProvider()
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="do stuff", target_files=("foo.py",),
            risk_tier="notify_apply",
        )
        assert result.skip_reason == "empty_diff"
        assert len(provider.calls) == 0


# ---------------------------------------------------------------------------
# TestCritiqueEngineHappyPath
# ---------------------------------------------------------------------------


class TestCritiqueEngineHappyPath:
    @pytest.mark.asyncio
    async def test_excellent_rating_flows(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({
                "rating": 5,
                "rationale": "Perfect",
                "matches_goal": True,
                "completeness": 5,
                "concerns": [],
            })
        )
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="fix the answer",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.rating == 5
        assert result.matches_goal is True
        assert result.parse_ok is True
        assert result.provider_name == "stub"
        assert result.skip_reason is None
        assert len(provider.calls) == 1
        assert provider.calls[0].goal == "fix the answer"

    @pytest.mark.asyncio
    async def test_poor_rating_writeback(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 999\n")
        provider = _FakeProvider(
            response=json.dumps({
                "rating": 1,
                "rationale": "Wrong approach entirely",
                "matches_goal": False,
                "completeness": 1,
                "concerns": ["uses magic number"],
            })
        )
        mock_store = MagicMock()
        mock_store.record_critique_failure = MagicMock()
        engine = CritiqueEngine(
            provider=provider, repo_root=tmp_git_repo,
            user_preference_store=mock_store,
        )
        result = await engine.critique_op(
            op_id="op-1", description="fix auth",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.rating == 1
        mock_store.record_critique_failure.assert_called_once()
        kwargs = mock_store.record_critique_failure.call_args.kwargs
        assert kwargs["rating"] == 1
        assert kwargs["op_id"] == "op-1"
        assert "uses magic number" in kwargs["concerns"]

    @pytest.mark.asyncio
    async def test_excellent_updates_memory_reputation(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({"rating": 5, "rationale": "Perfect"})
        )
        mock_memory = MagicMock()
        mock_memory._update_file_reputation = MagicMock()
        engine = CritiqueEngine(
            provider=provider, repo_root=tmp_git_repo,
            memory_engine=mock_memory,
        )
        await engine.critique_op(
            op_id="op-1", description="fix auth",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        mock_memory._update_file_reputation.assert_called_once()
        call_args = mock_memory._update_file_reputation.call_args
        assert call_args.kwargs.get("success") is True

    @pytest.mark.asyncio
    async def test_middle_rating_no_writeback(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({"rating": 3, "rationale": "Acceptable"})
        )
        mock_store = MagicMock()
        mock_memory = MagicMock()
        engine = CritiqueEngine(
            provider=provider, repo_root=tmp_git_repo,
            user_preference_store=mock_store, memory_engine=mock_memory,
        )
        result = await engine.critique_op(
            op_id="op-1", description="tweak",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.rating == 3
        assert result.is_poor is False
        assert result.is_excellent is False
        mock_store.record_critique_failure.assert_not_called()
        mock_memory._update_file_reputation.assert_not_called()


# ---------------------------------------------------------------------------
# TestCritiqueEngineFailureHandling
# ---------------------------------------------------------------------------


class TestCritiqueEngineFailureHandling:
    @pytest.mark.asyncio
    async def test_provider_exception_returns_skip(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(fail_with=RuntimeError("boom"))
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.skip_reason is not None
        assert "provider_failed" in result.skip_reason
        assert engine.stats()["fail_count"] == 1

    @pytest.mark.asyncio
    async def test_provider_timeout_returns_skip(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(fail_with=asyncio.TimeoutError())
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.skip_reason is not None
        assert "timeout" in result.skip_reason

    @pytest.mark.asyncio
    async def test_fallback_provider_used_on_primary_failure(
        self, tmp_git_repo: Path
    ):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        primary = _FakeProvider(name="primary", fail_with=RuntimeError("down"))
        fallback = _FakeProvider(
            name="fallback",
            response=json.dumps({"rating": 4, "rationale": "from fallback"}),
        )
        engine = CritiqueEngine(
            provider=primary, fallback_provider=fallback,
            repo_root=tmp_git_repo,
        )
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.rating == 4
        assert result.provider_name == "fallback"
        assert len(fallback.calls) == 1

    @pytest.mark.asyncio
    async def test_both_providers_fail_returns_skip(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        primary = _FakeProvider(name="primary", fail_with=RuntimeError("a"))
        fallback = _FakeProvider(name="fallback", fail_with=RuntimeError("b"))
        engine = CritiqueEngine(
            provider=primary, fallback_provider=fallback,
            repo_root=tmp_git_repo,
        )
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.skip_reason is not None
        assert engine.stats()["fail_count"] == 1

    @pytest.mark.asyncio
    async def test_malformed_json_still_returns_valid_result(
        self, tmp_git_repo: Path
    ):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(response="absolutely not json {{{")
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        # Parse failed, but result is still valid
        assert result.parse_ok is False
        assert result.rating == 3  # safe default
        assert result.skip_reason is None

    @pytest.mark.asyncio
    async def test_parse_failure_no_writeback(self, tmp_git_repo: Path):
        """Even if the default rating=3 would not trigger writeback anyway,
        parse failures must also skip writeback on poor-rating edge cases."""
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(response="garbage")
        mock_store = MagicMock()
        engine = CritiqueEngine(
            provider=provider, repo_root=tmp_git_repo,
            user_preference_store=mock_store,
        )
        await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        mock_store.record_critique_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_writeback_exception_swallowed(self, tmp_git_repo: Path):
        """Store raising an exception must not fail the critique."""
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({"rating": 1, "rationale": "bad"})
        )
        mock_store = MagicMock()
        mock_store.record_critique_failure = MagicMock(
            side_effect=RuntimeError("disk full")
        )
        engine = CritiqueEngine(
            provider=provider, repo_root=tmp_git_repo,
            user_preference_store=mock_store,
        )
        # Must NOT raise
        result = await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert result.rating == 1


# ---------------------------------------------------------------------------
# TestCritiqueEngineStats
# ---------------------------------------------------------------------------


class TestCritiqueEngineStats:
    @pytest.mark.asyncio
    async def test_stats_track_counts(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            response=json.dumps({"rating": 5, "rationale": "ok"})
        )
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        assert engine.stats()["total_critiques"] == 0
        await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        stats = engine.stats()
        assert stats["total_critiques"] == 1
        assert stats["excellent_count"] == 1
        assert stats["poor_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_track_skips(self, tmp_git_repo: Path):
        provider = _FakeProvider()
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        # trivial tier → skip
        await engine.critique_op(
            op_id="op-1", description="x",
            target_files=("foo.py",), risk_tier="trivial",
        )
        assert engine.stats()["skip_count"] == 1

    @pytest.mark.asyncio
    async def test_cost_accumulates(self, tmp_git_repo: Path):
        (tmp_git_repo / "foo.py").write_text("def foo():\n    return 42\n")
        provider = _FakeProvider(
            name="doubleword",
            response=json.dumps({"rating": 4, "rationale": "ok"}),
        )
        engine = CritiqueEngine(provider=provider, repo_root=tmp_git_repo)
        await engine.critique_op(
            op_id="op-1", description="fix",
            target_files=("foo.py",), risk_tier="notify_apply",
        )
        assert engine.stats()["cumulative_cost_usd"] > 0.0


# ---------------------------------------------------------------------------
# TestIsSelfCritiqueEnabled
# ---------------------------------------------------------------------------


class TestIsSelfCritiqueEnabled:
    def test_default_enabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("JARVIS_SELF_CRITIQUE_ENABLED", raising=False)
        assert is_self_critique_enabled() is True

    def test_disabled_via_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_SELF_CRITIQUE_ENABLED", "false")
        assert is_self_critique_enabled() is False

    @pytest.mark.parametrize("value,expected", [
        ("true", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
    ])
    def test_env_parsing(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ):
        monkeypatch.setenv("JARVIS_SELF_CRITIQUE_ENABLED", value)
        assert is_self_critique_enabled() is expected


# ---------------------------------------------------------------------------
# TestDoublewordCritiqueProvider
# ---------------------------------------------------------------------------


class TestDoublewordCritiqueProvider:
    @pytest.mark.asyncio
    async def test_provider_calls_prompt_only(self):
        mock_dw = MagicMock()
        mock_dw.prompt_only = MagicMock()
        async def _async_prompt_only(**kwargs):
            return json.dumps({"rating": 4, "rationale": "ok"})
        mock_dw.prompt_only = _async_prompt_only
        provider = DoublewordCritiqueProvider(mock_dw, max_tokens=256)
        request = CritiqueRequest(
            op_id="op-1", goal="fix bug", diff="+ y",
            risk_tier="notify_apply", target_files=("foo.py",),
            test_summary="ok", deadline_s=30.0,
        )
        raw = await provider.critique(request)
        assert '"rating": 4' in raw

    @pytest.mark.asyncio
    async def test_provider_timeout_propagates(self):
        mock_dw = MagicMock()
        async def _slow(**kwargs):
            await asyncio.sleep(5.0)
            return "late"
        mock_dw.prompt_only = _slow
        provider = DoublewordCritiqueProvider(mock_dw)
        request = CritiqueRequest(
            op_id="op", goal="g", diff="+ x", risk_tier="low",
            target_files=(), test_summary="", deadline_s=0.05,
        )
        with pytest.raises(asyncio.TimeoutError):
            await provider.critique(request)


# ---------------------------------------------------------------------------
# TestRecordCritiqueFailure (UserPreferenceStore integration)
# ---------------------------------------------------------------------------


class TestRecordCritiqueFailureIntegration:
    """Exercises the real UserPreferenceStore.record_critique_failure method."""

    def test_record_critique_failure_creates_feedback(self, tmp_path: Path):
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType,
            UserPreferenceStore,
        )
        store = UserPreferenceStore(
            project_root=tmp_path, auto_register_protected_paths=False
        )
        memory = store.record_critique_failure(
            op_id="op-1",
            description="Fix auth middleware",
            target_files=["backend/auth.py"],
            rating=1,
            rationale="Wrong approach — breaks session tracking",
            concerns=("session tokens not persisted", "missing retry logic"),
        )
        assert memory is not None
        assert memory.type is MemoryType.FEEDBACK
        assert "critique_poor_" in memory.name
        assert "1/5" in memory.description
        assert "rating_1" in memory.tags

    def test_record_critique_failure_empty_rationale_returns_none(
        self, tmp_path: Path
    ):
        from backend.core.ouroboros.governance.user_preference_memory import (
            UserPreferenceStore,
        )
        store = UserPreferenceStore(
            project_root=tmp_path, auto_register_protected_paths=False
        )
        memory = store.record_critique_failure(
            op_id="op-1", description="Fix stuff",
            target_files=["x.py"], rating=1, rationale="",
        )
        assert memory is None

    def test_record_critique_failure_dedups_on_repeat(self, tmp_path: Path):
        from backend.core.ouroboros.governance.user_preference_memory import (
            UserPreferenceStore,
        )
        store = UserPreferenceStore(
            project_root=tmp_path, auto_register_protected_paths=False
        )
        first = store.record_critique_failure(
            op_id="op-1", description="Fix auth",
            target_files=["auth.py"], rating=1, rationale="first",
        )
        second = store.record_critique_failure(
            op_id="op-2", description="Fix auth",
            target_files=["auth.py"], rating=2, rationale="second",
        )
        assert first is not None and second is not None
        # Same memory id because description is identical
        assert first.id == second.id
        assert second.why.startswith("second")
