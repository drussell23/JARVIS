# tests/governance/test_truncation_retry.py
from __future__ import annotations


def test_truncation_retry_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_TRUNCATION_RETRY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.truncation_retry import truncation_retry_enabled
    assert truncation_retry_enabled() is False
    monkeypatch.setenv("JARVIS_TRUNCATION_RETRY_ENABLED", "true")
    assert truncation_retry_enabled() is True


def test_is_truncation_failure_matches_syntax_and_placeholder():
    from backend.core.ouroboros.governance.truncation_retry import is_truncation_failure
    assert is_truncation_failure("gcp-jprime_schema_invalid:all_candidates_syntax_error") is True
    assert is_truncation_failure("doubleword-397b_schema_invalid:all_candidates_syntax_error") is True
    assert is_truncation_failure("candidate contains placeholder text") is True
    assert is_truncation_failure("all_providers_exhausted:terminal_quota") is False
    assert is_truncation_failure("some_other_error") is False
    assert is_truncation_failure("") is False
    assert is_truncation_failure(None) is False  # robust to None


def test_directive_diff_capable_forces_diff():
    from backend.core.ouroboros.governance.truncation_retry import build_truncation_retry_directive
    d = build_truncation_retry_directive(diff_capable=True, current_max_tokens=8192)
    assert d.force_diff is True
    assert "diff" in d.feedback.lower()
    assert d.new_max_tokens >= 8192          # never lowers


def test_directive_full_only_bumps_tokens_no_diff():
    from backend.core.ouroboros.governance.truncation_retry import build_truncation_retry_directive
    d = build_truncation_retry_directive(diff_capable=False, current_max_tokens=8192)
    assert d.force_diff is False
    assert d.new_max_tokens > 8192           # bumped to give headroom
    # feedback must forbid elisions explicitly
    assert "..." in d.feedback or "no elision" in d.feedback.lower() or "complete file" in d.feedback.lower()


def test_directive_token_bump_respects_ceiling(monkeypatch):
    from backend.core.ouroboros.governance.truncation_retry import build_truncation_retry_directive
    monkeypatch.setenv("JARVIS_TRUNCATION_RETRY_TOKEN_CEILING", "16384")
    d = build_truncation_retry_directive(diff_capable=False, current_max_tokens=12000)
    assert d.new_max_tokens <= 16384         # min(2x, ceiling)


def test_stamp_directive_sets_context_flags():
    import dataclasses
    from backend.core.ouroboros.governance.truncation_retry import (
        stamp_retry_directive, RetryDirective)

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        force_diff_on_retry: bool = False
        retry_max_tokens_override: int = 0

    out = stamp_retry_directive(_Ctx(), RetryDirective(force_diff=True, new_max_tokens=16384, feedback="x"))
    assert out.force_diff_on_retry is True
    assert out.retry_max_tokens_override == 16384


def test_stamp_directive_failsoft_on_non_dataclass():
    from backend.core.ouroboros.governance.truncation_retry import (
        stamp_retry_directive, RetryDirective)
    obj = object()
    assert stamp_retry_directive(obj, RetryDirective(False, 1, "x")) is obj


def test_real_operation_context_has_truncation_fields():
    from backend.core.ouroboros.governance.op_context import OperationContext
    f = OperationContext.__dataclass_fields__
    assert "force_diff_on_retry" in f and f["force_diff_on_retry"].default is False
    assert "retry_max_tokens_override" in f and f["retry_max_tokens_override"].default == 0
