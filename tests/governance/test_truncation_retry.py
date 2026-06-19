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
