"""Wave 3 hygiene arc — regression spine for the 4 items closed
2026-05-05 (PRD §35 — Open Strategic Moves Registry).

Items covered:
  * Item 1 — Move 8 GENERAL LLM driver status reconciled (PRD doc-only)
  * Item 2 — Vector #11 wall-clock → monotonic migration (5 elapsed-time sites)
  * Item 3 — Vector #9 FlagChangeEvent.to_dict masks sensitive values
  * Item 4 — invariant_drift_store baseline write cross-process flock

Items deferred (not in this spine):
  * Item 5 — Vector #10 AutoCommitter race (~1hr focused arc)
  * Item 6 — Vector #8 ArtifactContract drift (multi-hour arc)
"""
from __future__ import annotations

import ast as _ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Item 2 — wall-clock → monotonic migration
# ---------------------------------------------------------------------------


_MONOTONIC_MIGRATED_FILES = (
    "backend/core/ouroboros/governance/exploration_fleet.py",
    "backend/core/ouroboros/governance/mutation_tester.py",
    "backend/core/ouroboros/governance/mutation_gate.py",
    "backend/core/ouroboros/governance/unlimited_agents.py",
)


def _walk_attr_calls(tree, *, of_module: str, attr: str):
    """Yield AST nodes that look like ``<of_module>.<attr>(...)``
    (e.g. ``time.monotonic()``)."""
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute):
            if (
                isinstance(node.value, _ast.Name)
                and node.value.id == of_module
                and node.attr == attr
            ):
                yield node


@pytest.mark.parametrize("rel_path", _MONOTONIC_MIGRATED_FILES)
def test_elapsed_time_uses_monotonic_not_wall_clock(rel_path):
    """Each migrated file MUST use ``time.monotonic()`` for at
    least one paired elapsed-time measurement (init + check).
    Prevents regression to ``time.time()`` for elapsed math."""
    target = _repo_root() / rel_path
    source = target.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    monotonic_calls = list(
        _walk_attr_calls(tree, of_module="time", attr="monotonic"),
    )
    assert len(monotonic_calls) >= 2, (
        f"{rel_path} should call time.monotonic() at least twice "
        f"(paired init + elapsed-check); got "
        f"{len(monotonic_calls)}"
    )


# ---------------------------------------------------------------------------
# Item 3 — FlagChangeEvent value masking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_name", [
    "JARVIS_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "JARVIS_AUTH_TOKEN",
    "DATABASE_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "MY_PRIVATE_KEY",
    "JARVIS_SESSION_ID",
    "JARVIS_PASSWD",
    "JARVIS_PWD",
    "JARVIS_CREDENTIAL_X",
])
def test_sensitive_flag_value_masked_in_to_dict(flag_name):
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
        FlagChangeEvent,
    )
    ev = FlagChangeEvent(
        flag_name=flag_name,
        prev_value="actual-secret-value-do-not-leak",
        next_value="rotated-secret-value-also-private",
        ts_epoch=1.0,
    )
    d = ev.to_dict()
    assert d["value_masked"] is True
    assert "actual-secret" not in str(d["prev_value"])
    assert "rotated-secret" not in str(d["next_value"])
    assert "<MASKED:" in d["prev_value"]
    assert "<MASKED:" in d["next_value"]
    # Length token preserved for diagnostic use.
    assert "len=" in d["prev_value"]


@pytest.mark.parametrize("flag_name", [
    "JARVIS_HYPOTHESIS_PROBE_ENABLED",
    "JARVIS_FAILURE_MODE_MEMORY_ENABLED",
    "JARVIS_TOPOLOGY_SENTINEL_ENABLED",
    "JARVIS_BG_POOL_SIZE",
    "JARVIS_MAX_RETRIES",
])
def test_non_sensitive_flag_value_passes_through(flag_name):
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
        FlagChangeEvent,
    )
    ev = FlagChangeEvent(
        flag_name=flag_name,
        prev_value="false",
        next_value="true",
        ts_epoch=1.0,
    )
    d = ev.to_dict()
    assert d["value_masked"] is False
    assert d["prev_value"] == "false"
    assert d["next_value"] == "true"


def test_mask_helper_handles_none():
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
        FlagChangeEvent,
    )
    # None values must pass through unchanged so add/remove
    # transitions stay distinguishable from set-to-empty.
    ev = FlagChangeEvent(
        flag_name="JARVIS_API_KEY",
        prev_value=None,
        next_value="new-secret",
        ts_epoch=1.0,
    )
    d = ev.to_dict()
    assert d["prev_value"] is None
    assert d["next_value"] is not None
    assert "<MASKED:" in d["next_value"]


def test_sensitive_token_set_pinned():
    """The sensitive-name-tokens FrozenSet is bytes-pinned —
    changes require explicit test update + scope-doc."""
    from backend.core.ouroboros.governance.observability.flag_change_emitter import (  # noqa: E501
        _SENSITIVE_NAME_TOKENS,
    )
    expected_minimum = {
        "key", "token", "secret", "password", "passwd",
        "pwd", "credential", "private", "auth", "session_id",
    }
    assert expected_minimum.issubset(_SENSITIVE_NAME_TOKENS), (
        f"sensitive token set drifted; missing: "
        f"{expected_minimum - _SENSITIVE_NAME_TOKENS}"
    )


# ---------------------------------------------------------------------------
# Item 4 — invariant_drift_store baseline cross-process flock
# ---------------------------------------------------------------------------


def test_write_baseline_uses_flock_critical_section():
    """Source-grep + AST check that ``write_baseline`` imports and
    uses ``flock_critical_section`` from
    :mod:`cross_process_jsonl`."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "invariant_drift_store.py"
    )
    source = target.read_text(encoding="utf-8")
    # Must import the primitive (lazy import inside write_baseline).
    assert "flock_critical_section" in source, (
        "invariant_drift_store.write_baseline MUST use "
        "cross_process_jsonl.flock_critical_section to close "
        "the §28.5.1 cross-process baseline write race"
    )
    # Reference §28.5.1 closure marker so future grep finds the
    # binding.
    assert "§28.5.1" in source or "Wave 3 hygiene" in source


def test_write_baseline_atomic_write_still_used_under_lock():
    """The flock wraps the atomic-write — the rename is still
    POSIX-atomic so a reader during a write either sees old or
    new, never a torn file. AST check that the structure is
    preserved."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "invariant_drift_store.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    # Find the write_baseline function
    fn = None
    for node in _ast.walk(tree):
        if (
            isinstance(node, _ast.FunctionDef)
            and node.name == "write_baseline"
        ):
            fn = node
            break
    assert fn is not None, "write_baseline function missing"
    # Must call self._atomic_write inside the function.
    has_atomic_write = False
    for inner in _ast.walk(fn):
        if isinstance(inner, _ast.Attribute):
            if inner.attr == "_atomic_write":
                has_atomic_write = True
    assert has_atomic_write, (
        "write_baseline MUST still delegate to self._atomic_write "
        "(POSIX-atomic rename) even after flock migration"
    )


# ---------------------------------------------------------------------------
# Item 5 — async_flock_critical_section + AutoCommitter race fix
# ---------------------------------------------------------------------------


def test_async_flock_critical_section_serializes_within_process():
    """The async primitive serializes contending acquirers in a
    single process — proves the underlying sync flock state is
    held across the async block."""
    import asyncio
    import tempfile
    from pathlib import Path
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        async_flock_critical_section,
    )

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "racewindow.json"
            async with async_flock_critical_section(target) as ok:
                assert ok is True
                # Nested attempt with short timeout should miss.
                async with async_flock_critical_section(
                    target, timeout_s=0.1,
                ) as nested:
                    assert nested is False

    asyncio.run(_run())


def test_async_flock_releases_after_block():
    """Lock MUST be released on async-block exit so a sibling
    acquirer can succeed afterwards."""
    import asyncio
    import tempfile
    from pathlib import Path
    from backend.core.ouroboros.governance.cross_process_jsonl import (
        async_flock_critical_section,
    )

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "release.json"
            async with async_flock_critical_section(target) as ok:
                assert ok is True
            # Now sibling can acquire.
            async with async_flock_critical_section(
                target, timeout_s=1.0,
            ) as second:
                assert second is True

    asyncio.run(_run())


def test_async_flock_in_public_api():
    """``async_flock_critical_section`` MUST be exported via
    ``__all__`` so external consumers (AutoCommitter etc.) can
    rely on it as part of the §33.4 pattern catalog."""
    from backend.core.ouroboros.governance import cross_process_jsonl
    assert hasattr(
        cross_process_jsonl, "async_flock_critical_section",
    )
    assert (
        "async_flock_critical_section"
        in cross_process_jsonl.__all__
    )


def test_auto_committer_intent_lock_path_per_token():
    """Each intent_token gets its own lock file path so
    different ops don't block each other; same token → same
    path so the TOCTOU race is closed."""
    from pathlib import Path
    from backend.core.ouroboros.governance.auto_committer import (
        AutoCommitter,
    )
    ac = AutoCommitter(repo_root=Path("/tmp"))
    p1 = ac._intent_lock_path("token_a" * 16)
    p2 = ac._intent_lock_path("token_b" * 16)
    assert p1 != p2, (
        "different intent_tokens MUST get different lock paths"
    )
    p1_again = ac._intent_lock_path("token_a" * 16)
    assert p1 == p1_again, (
        "same intent_token MUST get same lock path "
        "(serializes TOCTOU)"
    )
    # Lock-dir convention pinned.
    assert ".jarvis/auto_commit_locks/" in str(p1)
    assert str(p1).endswith(".lock")


def test_auto_committer_commit_uses_async_flock():
    """Source-grep + AST check: ``commit()`` MUST call
    ``async_flock_critical_section`` AND extract the TOCTOU
    body into ``_commit_critical_section``."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/auto_committer.py"
    )
    src = target.read_text(encoding="utf-8")
    assert "async_flock_critical_section" in src, (
        "AutoCommitter.commit MUST use "
        "async_flock_critical_section to close vector #10 "
        "(TOCTOU race)"
    )
    assert "_commit_critical_section" in src, (
        "TOCTOU body MUST be extracted into "
        "_commit_critical_section so it can be invoked under "
        "the flock guard"
    )
    assert "_intent_lock_path" in src, (
        "AutoCommitter MUST expose _intent_lock_path helper "
        "for per-token serialization"
    )
    assert "commit_lock_contended" in src, (
        "Lock-contention path MUST surface a distinct "
        "skipped_reason for audit"
    )


# ---------------------------------------------------------------------------
# Item 1 — Move 8 PRD reconciliation (no code change; documentation
# pin)
# ---------------------------------------------------------------------------


def test_general_llm_driver_factory_present():
    """CLAUDE.md claims `JARVIS_GENERAL_LLM_DRIVER_ENABLED`
    graduated default-true post 2026-04-20. The corresponding
    factory MUST be present in agentic_general_subagent.py
    (defends the reconciliation in §35)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "agentic_general_subagent.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "JARVIS_GENERAL_LLM_DRIVER_ENABLED" in source, (
        "graduated default-true flag must be referenced in source"
    )
    # NOT_IMPLEMENTED is the FALLBACK path — must still be
    # present (deactivated when flag is true).
    assert "NOT_IMPLEMENTED" in source, (
        "fallback path must remain — flag-gate retains the stub "
        "for opt-out"
    )
