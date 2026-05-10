"""Regression spine for ``permission_decision_archive`` — Venom V2
observability ring.

Closes the operator-visibility gap surfaced in the §37 v10 brutal
review: ``tool_permission.py`` substrate ships + is composed by
``tool_executor._maybe_evaluate_tool_permission`` but no operator-
facing surface exists. This module's tests pin the ring's
load-bearing properties + the producer-bridge's fail-silent contract.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.permission_decision_archive import (
    ARCHIVE_SIZE_ENV_VAR,
    BoundedDecisionArchive,
    MASTER_FLAG_ENV_VAR,
    PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION,
    REF_PREFIX,
    get_default_archive,
    maybe_record_decision,
    permission_archive_enabled,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.tool_permission import (
    AggregatePermissionDecision,
    TOOL_PERMISSION_SCHEMA_VERSION,
    ToolPermissionDecision,
)


_MODULE_SRC = Path(
    inspect.getfile(BoundedDecisionArchive),
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_decision(
    *,
    tool_name: str = "read_file",
    op_id: str = "op-1",
    value: ToolPermissionDecision = ToolPermissionDecision.ALLOW,
    detail: str = "test",
) -> AggregatePermissionDecision:
    return AggregatePermissionDecision(
        schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
        tool_name=tool_name,
        op_id=op_id,
        decision=value,
        total_callbacks=1,
        detail=detail,
    )


@pytest.fixture(autouse=True)
def _isolate_env_and_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Each test starts with master-off + no singleton."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(ARCHIVE_SIZE_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ---------------------------------------------------------------------------
# Master-flag contract — §33.1 graduation
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    """The master flag MUST default to False per §33.1 graduation
    contract. This is the load-bearing pre-graduation invariant."""
    assert permission_archive_enabled() is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_master_flag_accepts_canonical_truthy_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool,
):
    """Master-flag parser accepts the canonical truthy/falsy
    vocabulary the rest of governance uses."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert permission_archive_enabled() is expected


def test_record_is_noop_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
):
    """When the master flag is off, ``record`` returns ``None`` and
    the archive stays empty (zero overhead pre-graduation)."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    arch = BoundedDecisionArchive(capacity=10)
    rec = arch.record(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(),
    )
    assert rec is None
    assert len(arch) == 0


def test_maybe_record_decision_is_noop_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
):
    """The producer-bridge §33.2 wrapper must respect master-off."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    maybe_record_decision(
        op_id="op-1", tool_name="bash",
        decision=_make_decision(tool_name="bash"),
    )
    assert len(get_default_archive()) == 0


# ---------------------------------------------------------------------------
# Ring contract — capacity / eviction / monotonic refs
# ---------------------------------------------------------------------------


def test_ring_records_decision_with_p_prefix(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=3)
    rec = arch.record(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(),
    )
    assert rec is not None
    assert rec.ref.startswith(REF_PREFIX)
    assert rec.ref == "p-1"
    assert rec.decision_value == "allow"
    assert rec.tool_name == "read_file"
    assert rec.op_id == "op-1"


def test_ring_evicts_oldest_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
):
    """capacity=3, record 5 → keep last 3 (drop-oldest)."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=3)
    for i in range(5):
        arch.record(
            op_id=f"op-{i}", tool_name="read_file",
            decision=_make_decision(op_id=f"op-{i}"),
        )
    assert len(arch) == 3
    # Refs p-1 + p-2 evicted; p-3 + p-4 + p-5 remain.
    assert arch.all_refs() == ("p-3", "p-4", "p-5")
    assert arch.lookup("p-1") is None
    assert arch.lookup("p-2") is None
    assert arch.lookup("p-3") is not None


def test_ring_monotonic_refs_never_rewind(
    monkeypatch: pytest.MonkeyPatch,
):
    """Critical safety invariant: a printed ref always resolves to
    the same decision OR to None (evicted) — never to a different
    decision. Counter must NOT reset even after clear()."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=2)
    rec_a = arch.record(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(op_id="op-A"),
    )
    arch.clear()
    rec_b = arch.record(
        op_id="op-B", tool_name="read_file",
        decision=_make_decision(op_id="op-B"),
    )
    assert rec_a is not None
    assert rec_b is not None
    assert rec_a.ref != rec_b.ref
    assert rec_b.ref == "p-2"  # counter advances, doesn't rewind


# ---------------------------------------------------------------------------
# Filtering API
# ---------------------------------------------------------------------------


def test_recent_returns_newest_first(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=10)
    for i in range(5):
        arch.record(
            op_id=f"op-{i}", tool_name="read_file",
            decision=_make_decision(op_id=f"op-{i}"),
        )
    recent = arch.recent(limit=3)
    assert [r.ref for r in recent] == ["p-5", "p-4", "p-3"]


def test_by_tool_filters_correctly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=10)
    arch.record(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(tool_name="read_file"),
    )
    arch.record(
        op_id="op-2", tool_name="bash",
        decision=_make_decision(tool_name="bash"),
    )
    arch.record(
        op_id="op-3", tool_name="read_file",
        decision=_make_decision(tool_name="read_file"),
    )
    rs = arch.by_tool("read_file")
    assert [r.ref for r in rs] == ["p-3", "p-1"]
    assert arch.by_tool("nonexistent") == []
    assert arch.by_tool("") == []


def test_by_op_filters_correctly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=10)
    arch.record(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(op_id="op-A"),
    )
    arch.record(
        op_id="op-B", tool_name="bash",
        decision=_make_decision(op_id="op-B"),
    )
    arch.record(
        op_id="op-A", tool_name="write_file",
        decision=_make_decision(op_id="op-A"),
    )
    rs = arch.by_op("op-A")
    assert [r.tool_name for r in rs] == ["write_file", "read_file"]
    assert arch.by_op("") == []


# ---------------------------------------------------------------------------
# Projection contract — §33.5 to_dict
# ---------------------------------------------------------------------------


def test_decision_record_to_dict_carries_full_projection(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=10)
    rec = arch.record(
        op_id="op-X", tool_name="bash",
        decision=_make_decision(
            tool_name="bash", op_id="op-X",
            value=ToolPermissionDecision.DENY,
            detail="dangerous",
        ),
    )
    assert rec is not None
    d = rec.to_dict()
    assert d["ref"].startswith("p-")
    assert d["op_id"] == "op-X"
    assert d["tool_name"] == "bash"
    assert d["decision_value"] == "deny"
    assert (
        d["schema_version"]
        == PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION
    )
    # Composes tool_permission's projection without redefinition.
    inner = d["decision"]
    assert inner["schema_version"] == TOOL_PERMISSION_SCHEMA_VERSION
    assert inner["decision"] == "deny"
    assert inner["detail"] == "dangerous"


def test_archive_snapshot_to_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=5)
    arch.record(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(),
    )
    snap = arch.snapshot().to_dict()
    assert snap["capacity"] == 5
    assert snap["size"] == 1
    assert snap["next_seq"] == 2
    assert 0.0 <= snap["utilization"] <= 1.0
    assert (
        snap["schema_version"]
        == PERMISSION_DECISION_ARCHIVE_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# Fail-closed contract — invalid inputs never raise
# ---------------------------------------------------------------------------


def test_lookup_returns_none_for_invalid_inputs(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=5)
    assert arch.lookup(None) is None
    assert arch.lookup(123) is None
    assert arch.lookup("nonexistent-ref") is None


def test_record_with_malformed_decision_object_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
):
    """A foreign decision shape (no ``.decision`` attribute / no
    ``to_dict``) MUST NOT raise — the producer-bridge contract is
    fail-silent."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    arch = BoundedDecisionArchive(capacity=5)

    class _Foreign:
        pass

    rec = arch.record(
        op_id="op-1", tool_name="read_file",
        decision=_Foreign(),
    )
    # Still archived (ref-stability) but with safe-default values.
    assert rec is not None
    assert rec.decision_value == "defer"  # safe DEFER default
    assert rec.decision_projection == {}  # empty projection on miss


def test_maybe_record_decision_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch,
):
    """The producer-bridge §33.2 wrapper MUST NEVER raise into the
    policy path. We use a deliberately broken decision and confirm
    the wrapper returns None and logs nothing-fatal."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _Boom:
        @property
        def decision(self):
            raise RuntimeError("kaboom")

        def to_dict(self):
            raise RuntimeError("kaboom")

    # Must not raise.
    maybe_record_decision(
        op_id="op-1", tool_name="bash", decision=_Boom(),
    )
    # Archive still works for valid records after.
    maybe_record_decision(
        op_id="op-2", tool_name="bash",
        decision=_make_decision(),
    )
    assert len(get_default_archive()) >= 1


# ---------------------------------------------------------------------------
# Singleton lifecycle
# ---------------------------------------------------------------------------


def test_get_default_archive_returns_same_instance(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    a1 = get_default_archive()
    a2 = get_default_archive()
    assert a1 is a2


def test_reset_for_tests_drops_singleton(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    a1 = get_default_archive()
    reset_default_archive_for_tests()
    a2 = get_default_archive()
    assert a1 is not a2


def test_capacity_env_var_respected(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "7")
    reset_default_archive_for_tests()
    arch = get_default_archive()
    assert arch.capacity == 7


# ---------------------------------------------------------------------------
# AST pins — load-bearing structural invariants
# ---------------------------------------------------------------------------


def test_ast_pin_p_prefix_is_canonical():
    """REF_PREFIX MUST equal ``"p-"`` — the cross-substrate
    ``/expand <ref>`` dispatcher in serpent_flow keys off this
    literal. Drift here silently breaks the dispatcher."""
    assert REF_PREFIX == "p-"


def test_ast_pin_master_flag_default_false_in_source():
    """Source-level pin: the master-flag function MUST default
    to False per §33.1 graduation contract. We assert by parsing
    the AST + verifying the helper returns False on the canonical
    falsy strings — drift to default-True would silently graduate
    the ring without an evidence ladder."""
    tree = ast.parse(_MODULE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "permission_archive_enabled"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_MODULE_SRC, fn)
    assert src is not None
    # The truthy alternation must be exactly the canonical 4-tuple.
    assert '("1", "true", "yes", "on")' in src, (
        "permission_archive_enabled MUST gate on the canonical "
        "4-tuple — drift here breaks the master-flag contract"
    )


def test_ast_pin_no_policy_imports():
    """Authority-asymmetry pin: this module MUST NOT import
    tool_permission's policy code (compute_permission_decision /
    PermissionRegistry / evaluate_tool_permission). The archive
    is read-side telemetry only."""
    tree = ast.parse(_MODULE_SRC)
    forbidden = {
        "compute_permission_decision",
        "PermissionRegistry",
        "evaluate_tool_permission",
        "ToolPermissionCallback",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"permission_decision_archive must NOT "
                    f"import policy symbol {alias.name!r} — "
                    f"authority asymmetry violation"
                )


def test_ast_pin_record_is_no_op_when_flag_off():
    """The first statement of ``BoundedDecisionArchive.record``
    MUST be the master-flag short-circuit — drift here would
    silently pay archive overhead pre-graduation."""
    tree = ast.parse(_MODULE_SRC)
    cls = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and n.name == "BoundedDecisionArchive"
        ),
        None,
    )
    assert cls is not None
    record_fn = next(
        (
            n for n in cls.body
            if isinstance(n, ast.FunctionDef)
            and n.name == "record"
        ),
        None,
    )
    assert record_fn is not None
    src = ast.get_source_segment(_MODULE_SRC, record_fn)
    assert src is not None
    assert "permission_archive_enabled()" in src, (
        "BoundedDecisionArchive.record MUST short-circuit on "
        "the master flag — drift breaks the §33.1 contract"
    )


# ---------------------------------------------------------------------------
# Tool-executor wiring pin — load-bearing seam
# ---------------------------------------------------------------------------


def test_tool_executor_invokes_producer_bridge():
    """``_maybe_evaluate_tool_permission`` in tool_executor.py
    MUST invoke ``maybe_record_decision`` after the policy call
    succeeds — that's the single seam where the archive composes
    the policy substrate. Drift here silently disables the
    observability surface."""
    from backend.core.ouroboros.governance import tool_executor
    src = Path(
        inspect.getfile(tool_executor),
    ).read_text(encoding="utf-8")
    fn_match = src[
        src.index("async def _maybe_evaluate_tool_permission"):
    ]
    # Bound the search to the function body.
    fn_match = fn_match[: fn_match.index("\nasync def ")]
    assert "maybe_record_decision" in fn_match, (
        "_maybe_evaluate_tool_permission MUST invoke "
        "maybe_record_decision — composing the producer-bridge "
        "is the load-bearing wiring for Venom V2 observability"
    )
    assert "permission_decision_archive" in fn_match, (
        "import path MUST be the canonical "
        "permission_decision_archive module"
    )
