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
# Item 6 — Vector #8 Versioned Artifact Contract
# ---------------------------------------------------------------------------


def test_versioned_artifact_substrate_authority_asymmetry():
    """The substrate module MUST stay pure (stdlib + typing +
    dataclasses ONLY)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/meta/"
        "versioned_artifact.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"versioned_artifact.py MUST NOT "
                        f"import {module!r}"
                    )


def test_verify_artifact_schema_accepts_match():
    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    payload = {"schema_version": "foo.1", "data": 42}
    v = verify_artifact_schema(payload, expected_schema="foo.1")
    assert v.accepted is True
    assert v.actual_schema == "foo.1"
    assert v.is_legacy is False


def test_verify_artifact_schema_accepts_legacy():
    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    payload = {"schema_version": "foo.0"}
    v = verify_artifact_schema(
        payload,
        expected_schema="foo.1",
        allowed_legacy=["foo.0"],
    )
    assert v.accepted is True
    assert v.is_legacy is True
    assert "legacy_schema_accepted" in v.diagnostic


def test_verify_artifact_schema_rejects_drift():
    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    payload = {"schema_version": "wrong.0"}
    v = verify_artifact_schema(payload, expected_schema="foo.1")
    assert v.accepted is False
    assert "schema_drift" in v.diagnostic


def test_verify_artifact_schema_missing():
    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    v = verify_artifact_schema({}, expected_schema="foo.1")
    assert v.accepted is False
    assert "missing" in v.diagnostic


def test_verify_artifact_schema_object_attribute():
    """Helper accepts objects with .schema_version attribute,
    not just dicts."""
    from backend.core.ouroboros.governance.meta.versioned_artifact import (  # noqa: E501
        verify_artifact_schema,
    )
    class Stub:
        schema_version = "foo.1"
    v = verify_artifact_schema(Stub(), expected_schema="foo.1")
    assert v.accepted is True


def test_rollback_artifact_has_schema_version():
    from backend.core.ouroboros.governance.change_engine import (
        RollbackArtifact, ROLLBACK_ARTIFACT_SCHEMA_VERSION,
    )
    ra = RollbackArtifact(
        original_content="x", snapshot_hash="h",
    )
    assert ra.schema_version == "rollback_artifact.1"
    assert (
        ROLLBACK_ARTIFACT_SCHEMA_VERSION == "rollback_artifact.1"
    )


def test_rollback_artifact_round_trip():
    from backend.core.ouroboros.governance.change_engine import (
        RollbackArtifact,
    )
    ra = RollbackArtifact(
        original_content="hello\nworld",
        snapshot_hash="sha256_abc",
        existed=False,
    )
    d = ra.to_dict()
    ra2 = RollbackArtifact.from_dict(d)
    assert ra == ra2


def test_rollback_artifact_from_dict_defensive():
    from backend.core.ouroboros.governance.change_engine import (
        RollbackArtifact,
    )
    # Garbage input → None, no exception.
    assert RollbackArtifact.from_dict("not a dict") is None  # type: ignore
    assert RollbackArtifact.from_dict(None) is None  # type: ignore


def test_saga_ledger_artifact_has_schema_version():
    from backend.core.ouroboros.governance.saga.saga_types import (
        SagaLedgerArtifact,
        SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION,
    )
    sla = SagaLedgerArtifact(
        saga_id="s", op_id="op", event="prepare", repo="*",
        original_ref="HEAD", original_sha="aaa", base_sha="bbb",
        saga_branch="b", promoted_sha="",
        promote_order_index=-1, rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=False,
        skipped_no_diff=False, timestamp_ns=0,
    )
    assert (
        sla.schema_version == "saga_ledger_artifact.1"
    )
    assert (
        SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION
        == "saga_ledger_artifact.1"
    )


def test_saga_ledger_artifact_round_trip():
    from backend.core.ouroboros.governance.saga.saga_types import (
        SagaLedgerArtifact,
    )
    sla = SagaLedgerArtifact(
        saga_id="s1", op_id="op1", event="apply_repo",
        repo="r1", original_ref="HEAD", original_sha="a",
        base_sha="b", saga_branch="branch", promoted_sha="c",
        promote_order_index=0, rollback_reason="",
        partial_promote_boundary_repo="",
        kept_forensics_branches=True, skipped_no_diff=False,
        timestamp_ns=12345,
    )
    d = sla.to_dict()
    sla2 = SagaLedgerArtifact.from_dict(d)
    assert sla == sla2


def test_work_unit_ledger_artifact_round_trip():
    from backend.core.ouroboros.governance.saga.saga_types import (
        WorkUnitLedgerArtifact,
        WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION,
    )
    wula = WorkUnitLedgerArtifact(
        graph_id="g", unit_id="u", repo="r", state="running",
        barrier_id="b", causal_trace_id="t", timestamp_ns=1,
    )
    assert (
        wula.schema_version == "work_unit_ledger_artifact.1"
    )
    assert (
        WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION
        == "work_unit_ledger_artifact.1"
    )
    d = wula.to_dict()
    wula2 = WorkUnitLedgerArtifact.from_dict(d)
    assert wula == wula2


def test_artifact_pins_auto_registered():
    """The substrate module's authority-asymmetry pin auto-
    discovers via register_shipped_invariants."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    pin_names = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    assert (
        "versioned_artifact_authority_asymmetry" in pin_names
    )


def test_three_artifacts_use_canonical_constants():
    """All 3 *Artifact classes MUST reference their
    module-level *_ARTIFACT_SCHEMA_VERSION constant via
    field default — not inline literals — so future bumps
    require a single edit."""
    targets = (
        (
            _repo_root() / "backend/core/ouroboros/governance"
            / "change_engine.py",
            "ROLLBACK_ARTIFACT_SCHEMA_VERSION",
        ),
        (
            _repo_root() / "backend/core/ouroboros/governance"
            / "saga/saga_types.py",
            "SAGA_LEDGER_ARTIFACT_SCHEMA_VERSION",
        ),
        (
            _repo_root() / "backend/core/ouroboros/governance"
            / "saga/saga_types.py",
            "WORK_UNIT_LEDGER_ARTIFACT_SCHEMA_VERSION",
        ),
    )
    for path, const_name in targets:
        text = path.read_text(encoding="utf-8")
        assert f"{const_name}:" in text or f"{const_name} =" in text, (
            f"{path.name}: missing canonical constant "
            f"{const_name}"
        )
        # Constant must appear as a default value somewhere.
        assert text.count(const_name) >= 2, (
            f"{path.name}: {const_name} should appear at "
            f"least twice (constant declaration + field default)"
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


# ---------------------------------------------------------------------------
# Item 7 — §35 #6 / §3.6.3 #5 mask-discipline consumer-chain pin
# (Vector #9 broader closure, 2026-05-09)
# ---------------------------------------------------------------------------
#
# Wave 3 v2.25 closed the substrate: FlagChangeEvent.to_dict() masks
# credential-shaped values. But the dataclass STILL holds raw secrets
# in `prev_value` / `next_value` — a future consumer reading those
# fields directly bypasses the Wave 3 mask and leaks the raw value.
#
# The §35 #6 / §3.6.3 #5 closure: AST regression sweep that walks
# backend/, detects every `.prev_value` / `.next_value` attribute
# access + every `getattr(*, "prev_value"|"next_value")` call, and
# asserts the receiver file is on a canonical bytes-pinned allowlist.
#
# This pin enforces the substrate→consumer chain at lint-time: any
# new consumer must either compose `to_dict()` (Wave 3 mask) OR be
# explicitly added to the allowlist with reviewer attention.
#
# IMPORTANT: scoped to backend/ only. Test files inherently access
# these fields to validate substrate behavior — that's expected and
# allowed.


# Bytes-pinned canonical allowlist of files that may legitimately
# read FlagChangeEvent.prev_value / .next_value as raw values:
#
#   * flag_change_emitter.py — substrate that owns FlagChangeEvent
#     and the masking. All raw access here is internal (property
#     methods, comparison helpers, to_dict masking branch).
#   * sse_bridge.py — uses ``getattr(event, "prev_value", None)``
#     and ``getattr(event, "next_value", None)`` to extract values
#     and IMMEDIATELY pipes them through ``_mask_flag_value`` before
#     SSE publish. The values never reach a consumer in raw form.
#
# Drift requires reviewer attention + explicit allowlist update.
_FLAG_VALUE_ACCESS_ALLOWLIST = frozenset({
    "core/ouroboros/governance/observability/flag_change_emitter.py",
    "core/ouroboros/governance/observability/sse_bridge.py",
})


def _walk_flag_value_access_sites(
    backend_root: Path,
) -> list:
    """Find every `.prev_value` / `.next_value` Attribute access
    + every `getattr(*, "prev_value"|"next_value")` Call across
    backend/. Returns list of (rel_path, kind, lineno) tuples.

    Skipped: __pycache__, venv/, third-party.
    """
    violations = []
    for py in backend_root.rglob("*.py"):
        try:
            rel = py.relative_to(backend_root).as_posix()
        except ValueError:
            continue
        if (
            "__pycache__" in rel
            or rel.startswith("venv/")
            or rel.startswith("env/")
        ):
            continue
        try:
            src = py.read_text(encoding="utf-8")
            tree = _ast.parse(src)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in _ast.walk(tree):
            # Direct attribute access: e.prev_value / e.next_value
            if (
                isinstance(node, _ast.Attribute)
                and node.attr in ("prev_value", "next_value")
            ):
                violations.append(
                    (rel, "Attribute", node.lineno),
                )
            # getattr(event, "prev_value", ...) string-attr access
            if (
                isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], _ast.Constant)
                and node.args[1].value
                in ("prev_value", "next_value")
            ):
                violations.append(
                    (rel, "getattr", node.lineno),
                )
    return violations


def test_mask_discipline_consumer_chain_pinned():
    """§35 #6 / §3.6.3 #5: regression sweep against future
    consumers that bypass FlagChangeEvent.to_dict() and read
    raw .prev_value / .next_value fields directly.

    Wave 3 v2.25 closed the SUBSTRATE (to_dict masks sensitive
    flag names). This pin closes the LATENT vector: any future
    consumer doing ``e.prev_value`` on a credential-shaped flag
    would leak the raw secret because the dataclass fields hold
    raw values; only the to_dict() projection masks.
    """
    backend = _repo_root() / "backend"
    sites = _walk_flag_value_access_sites(backend)
    out_of_allowlist = [
        s for s in sites if s[0] not in _FLAG_VALUE_ACCESS_ALLOWLIST
    ]
    assert not out_of_allowlist, (
        "Vector #9 mask-discipline violation: file(s) read raw "
        "FlagChangeEvent.prev_value/next_value outside the "
        "canonical allowlist. Either compose `event.to_dict()` "
        "(which masks credential-shaped flags) OR add the file "
        "to _FLAG_VALUE_ACCESS_ALLOWLIST with explicit reviewer "
        f"attention. Violations: {out_of_allowlist}"
    )


def test_allowlist_files_actually_use_the_fields():
    """Companion regression: prove the allowlist is not stale —
    each entry must still actually read the fields. If a file
    no longer accesses the fields, it should be removed from
    the allowlist (lest a future malicious-leak path reuse the
    file's allowed status)."""
    backend = _repo_root() / "backend"
    sites = _walk_flag_value_access_sites(backend)
    sites_by_file = {s[0] for s in sites}
    stale = _FLAG_VALUE_ACCESS_ALLOWLIST - sites_by_file
    assert not stale, (
        f"Allowlist contains stale entries (no longer accessing "
        f"the fields): {stale}. Remove from "
        "_FLAG_VALUE_ACCESS_ALLOWLIST."
    )


def test_allowlist_size_pinned():
    """Bytes-pin: the allowlist size is exactly 2 today. Adding
    a NEW consumer requires both (a) updating the allowlist AND
    (b) updating this size assertion — forces reviewer attention
    on the safety policy."""
    assert len(_FLAG_VALUE_ACCESS_ALLOWLIST) == 2, (
        "Allowlist size drifted from canonical 2-entry value. "
        "Adding a new consumer is permitted but requires explicit "
        "reviewer attention on the masking policy. Update both "
        "_FLAG_VALUE_ACCESS_ALLOWLIST and this size assertion."
    )


def test_substrate_uses_mask_helper():
    """Defense-in-depth: the substrate file (flag_change_emitter)
    MUST use ``_mask_value()`` in its ``to_dict()`` projection.
    Bytes-pin against accidental removal of the masking branch."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "observability/flag_change_emitter.py"
    )
    source = target.read_text(encoding="utf-8")
    # The masking helper exists.
    assert "def _mask_value(" in source
    # The sensitive-name detector exists.
    assert "def _is_sensitive_flag(" in source
    # to_dict invokes the mask path.
    assert "_mask_value(self.prev_value)" in source
    assert "_mask_value(self.next_value)" in source
    # The "value_masked" boolean field is exposed so consumers
    # can audit the decision.
    assert '"value_masked"' in source


def test_sse_bridge_pipes_through_mask_helper():
    """Defense-in-depth: sse_bridge.publish_flag_change_event
    MUST pipe ``getattr(event, "prev_value")`` through its
    own ``_mask_flag_value`` helper before SSE publish (the
    canonical consumer-side double-mask)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "observability/sse_bridge.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "_mask_flag_value" in source
    # Bridge wrapper composes both: getattr extraction + mask.
    assert 'getattr(event, "prev_value"' in source
    assert 'getattr(event, "next_value"' in source
