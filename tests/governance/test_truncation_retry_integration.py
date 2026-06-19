# tests/governance/test_truncation_retry_integration.py
from __future__ import annotations
import dataclasses


def test_full_chain_diff_capable(monkeypatch):
    """truncation failure -> directive(force_diff) -> stamp -> provider override allows diff."""
    monkeypatch.setenv("JARVIS_TRUNCATION_RETRY_ENABLED", "true")
    from backend.core.ouroboros.governance.truncation_retry import (
        truncation_retry_enabled, is_truncation_failure,
        build_truncation_retry_directive, stamp_retry_directive, apply_retry_overrides)

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        force_diff_on_retry: bool = False
        retry_max_tokens_override: int = 0

    err = "doubleword-397b_schema_invalid:all_candidates_syntax_error"
    assert truncation_retry_enabled() and is_truncation_failure(err)
    d = build_truncation_retry_directive(diff_capable=True, current_max_tokens=8192)
    ctx2 = stamp_retry_directive(_Ctx(), d)
    assert ctx2.force_diff_on_retry is True
    ff, mt = apply_retry_overrides(ctx=ctx2, schema_capability="full_content_and_diff",
                                   force_full=True, max_tokens=8192)
    assert ff is False                      # diff-capable retry -> diff allowed end to end


def test_full_chain_full_only_bumps_tokens(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUNCATION_RETRY_ENABLED", "true")
    from backend.core.ouroboros.governance.truncation_retry import (
        build_truncation_retry_directive, stamp_retry_directive, apply_retry_overrides)

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        force_diff_on_retry: bool = False
        retry_max_tokens_override: int = 0

    d = build_truncation_retry_directive(diff_capable=False, current_max_tokens=8192)
    ctx2 = stamp_retry_directive(_Ctx(), d)
    ff, mt = apply_retry_overrides(ctx=ctx2, schema_capability="full_content_only",
                                   force_full=True, max_tokens=8192)
    assert ff is True and mt > 8192          # full-only -> can't diff, but more headroom


def test_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUNCATION_RETRY_ENABLED", "false")
    from backend.core.ouroboros.governance.truncation_retry import truncation_retry_enabled
    assert truncation_retry_enabled() is False


def test_real_operationcontext_advance_preserves_truncation_fields():
    """Closes the recurring 'wiring verified only by inspection' gap: stamp the two
    flags on a REAL OperationContext, advance to GENERATE_RETRY, and assert they
    survive advance() (i.e. _retry_ctx_kwargs can't clobber them)."""
    import dataclasses
    from datetime import datetime, timezone
    from backend.core.ouroboros.governance.op_context import (
        OperationContext, OperationPhase)
    now = datetime.now(timezone.utc)
    ctx = OperationContext(
        op_id="op-trunc-test", created_at=now, phase=OperationPhase.GENERATE,
        phase_entered_at=now, context_hash="h0", previous_hash="",
        target_files=("a.py",),
    )
    stamped = dataclasses.replace(
        ctx, force_diff_on_retry=True, retry_max_tokens_override=16384)
    advanced = stamped.advance(OperationPhase.GENERATE_RETRY)
    assert advanced.force_diff_on_retry is True
    assert advanced.retry_max_tokens_override == 16384
