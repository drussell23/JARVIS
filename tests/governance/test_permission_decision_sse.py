"""Regression spine for Venom V2 Slice 3 — SSE event
``permission_decision_recorded``.

Pins the load-bearing SSE-bridge invariants:

* ``EVENT_TYPE_PERMISSION_DECISION_RECORDED`` is registered in
  the canonical ``_VALID_EVENT_TYPES`` frozenset (drift here
  silently drops every publish — the broker's pre-publish
  validation rejects unregistered types).
* ``maybe_record_decision`` fires the canonical
  ``publish_task_event`` AFTER the archive's ``record()`` write
  succeeds — and ONLY when both master flags compose to ``true``.
* The publish path NEVER raises into the policy substrate's
  control flow (the producer-bridge is best-effort by contract).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_PERMISSION_DECISION_RECORDED,
    _VALID_EVENT_TYPES,
    get_default_broker,
    reset_default_broker,
)
from backend.core.ouroboros.governance.permission_decision_archive import (
    MASTER_FLAG_ENV_VAR,
    maybe_record_decision,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.tool_permission import (
    AggregatePermissionDecision,
    TOOL_PERMISSION_SCHEMA_VERSION,
    ToolPermissionDecision,
)


_STREAM_FLAG = "JARVIS_IDE_STREAM_ENABLED"
_ARCHIVE_SRC = Path(
    inspect.getfile(maybe_record_decision),
).read_text(encoding="utf-8")


def _make_decision(
    *,
    tool_name: str = "read_file",
    op_id: str = "op-A",
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
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Each test starts with master-on archive + master-on stream
    + fresh singletons. Per-test overrides are explicit."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(_STREAM_FLAG, "true")
    reset_default_archive_for_tests()
    reset_default_broker()
    yield
    reset_default_archive_for_tests()
    reset_default_broker()


# ---------------------------------------------------------------------------
# Event-type registration — the canonical broker contract
# ---------------------------------------------------------------------------


def test_event_type_string_is_canonical():
    """The constant value MUST be the canonical literal — IDE
    consumers + AST greps key off this exact string."""
    assert (
        EVENT_TYPE_PERMISSION_DECISION_RECORDED
        == "permission_decision_recorded"
    )


def test_event_type_registered_in_valid_set():
    """The broker rejects publishes for any event_type NOT in
    ``_VALID_EVENT_TYPES``. Drift here silently drops every
    Venom V2 SSE event without a runtime error."""
    assert (
        EVENT_TYPE_PERMISSION_DECISION_RECORDED
        in _VALID_EVENT_TYPES
    )


# ---------------------------------------------------------------------------
# Producer-bridge contract — both gates must be ON to publish
# ---------------------------------------------------------------------------


def test_publish_fires_when_archive_and_stream_both_enabled():
    """Happy path: archive on + stream on → exactly one publish
    per record."""
    broker = get_default_broker()
    pre = broker.published_count
    maybe_record_decision(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(),
    )
    post = broker.published_count
    assert post == pre + 1, (
        f"expected exactly 1 publish, got {post - pre}"
    )


def test_publish_silenced_when_stream_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Stream master flag explicitly off → publish is no-op
    (record still archived; publish is the only operation that
    silences). Both load-bearing: archive stays authoritative,
    SSE is best-effort."""
    monkeypatch.setenv(_STREAM_FLAG, "false")
    reset_default_broker()  # re-read flag state
    broker = get_default_broker()
    pre = broker.published_count
    maybe_record_decision(
        op_id="op-B", tool_name="write_file",
        decision=_make_decision(tool_name="write_file"),
    )
    post = broker.published_count
    assert post == pre, (
        f"stream-disabled should be no-op: delta={post - pre}"
    )


def test_publish_silenced_when_archive_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """Archive master off → record() short-circuits early; no
    record + no publish."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    reset_default_broker()
    broker = get_default_broker()
    pre = broker.published_count
    maybe_record_decision(
        op_id="op-C", tool_name="bash",
        decision=_make_decision(),
    )
    post = broker.published_count
    assert post == pre


def test_published_payload_carries_canonical_projection():
    """The payload published to the SSE broker MUST be the
    archive's ``DecisionRecord.to_dict()`` projection — IDE
    consumers depend on the canonical schema_version + ref +
    decision_value fields."""
    broker = get_default_broker()
    maybe_record_decision(
        op_id="op-X", tool_name="bash",
        decision=_make_decision(
            tool_name="bash", op_id="op-X",
            value=ToolPermissionDecision.DENY,
            detail="dangerous",
        ),
    )
    last = list(broker._history)[-1]
    assert last.event_type == EVENT_TYPE_PERMISSION_DECISION_RECORDED
    assert last.op_id == "op-X"
    pl = last.payload
    assert pl["ref"].startswith("p-")
    assert pl["op_id"] == "op-X"
    assert pl["tool_name"] == "bash"
    assert pl["decision_value"] == "deny"
    assert (
        pl["schema_version"]
        == "permission_decision_archive.v1"
    )
    # Inner projection composes tool_permission's to_dict — drift
    # here would silently change the IDE consumer's schema.
    inner = pl["decision"]
    assert inner["decision"] == "deny"
    assert inner["detail"] == "dangerous"
    assert (
        inner["schema_version"] == TOOL_PERMISSION_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# Best-effort contract — broker exception MUST NOT raise into policy
# ---------------------------------------------------------------------------


def test_broker_exception_does_not_propagate():
    """If publish_task_event raises (broker unavailable, queue
    overflow, etc.) the producer-bridge MUST swallow + the
    archive write MUST still succeed. NEVER raises into the
    policy path is the load-bearing contract."""
    with patch(
        "backend.core.ouroboros.governance.ide_observability_stream.publish_task_event"
    ) as mock_pub:
        mock_pub.side_effect = RuntimeError("broker exploded")
        # Should not raise.
        maybe_record_decision(
            op_id="op-D", tool_name="read_file",
            decision=_make_decision(),
        )
    # Archive write still succeeded.
    from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
        get_default_archive,
    )
    assert len(get_default_archive()) == 1


# ---------------------------------------------------------------------------
# AST pins — load-bearing structural invariants
# ---------------------------------------------------------------------------


def test_ast_pin_event_type_constant_present():
    """The constant MUST be defined as a module-level Assign
    in the ide_observability_stream substrate. Drift here breaks
    the canonical-import path that downstream consumers use."""
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    src = Path(
        inspect.getfile(ios),
    ).read_text(encoding="utf-8")
    assert (
        'EVENT_TYPE_PERMISSION_DECISION_RECORDED = (\n'
        '    "permission_decision_recorded"\n'
        ')'
    ) in src or (
        'EVENT_TYPE_PERMISSION_DECISION_RECORDED = '
        '"permission_decision_recorded"'
    ) in src, (
        "EVENT_TYPE_PERMISSION_DECISION_RECORDED constant must "
        "appear at module scope with the canonical literal"
    )


def test_ast_pin_event_type_in_valid_set_literal():
    """The constant MUST appear in the literal frozenset
    expression. Drift here lets the constant exist but the
    broker reject every publish silently — same failure mode
    as v2.84's POSTURE_OBSERVER_DEGRADED gap."""
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    src = Path(
        inspect.getfile(ios),
    ).read_text(encoding="utf-8")
    # Locate the frozenset block.
    idx = src.index("_VALID_EVENT_TYPES = frozenset({")
    end = src.index("})", idx)
    block = src[idx:end]
    assert "EVENT_TYPE_PERMISSION_DECISION_RECORDED" in block, (
        "EVENT_TYPE_PERMISSION_DECISION_RECORDED MUST appear in "
        "the _VALID_EVENT_TYPES literal frozenset — drift here "
        "silently drops every Venom V2 SSE publish"
    )


def test_ast_pin_archive_invokes_publish_task_event():
    """``maybe_record_decision`` MUST invoke
    ``publish_task_event`` AFTER ``archive.record()`` succeeds.
    Bytes-pinned to anchor the producer-bridge wiring."""
    # Locate maybe_record_decision body.
    tree = ast.parse(_ARCHIVE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "maybe_record_decision"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_ARCHIVE_SRC, fn)
    assert src is not None
    assert "publish_task_event" in src, (
        "maybe_record_decision MUST invoke publish_task_event "
        "as the SSE producer-bridge"
    )
    assert "EVENT_TYPE_PERMISSION_DECISION_RECORDED" in src, (
        "must use the canonical EVENT_TYPE_* import (not a raw "
        "string literal)"
    )
    # Positional invariant: the publish call MUST come AFTER the
    # archive.record() call (writing before would publish a stale
    # ref / phantom record).
    record_idx = src.index("archive.record(")
    publish_idx = src.index("publish_task_event(")
    assert record_idx < publish_idx, (
        "publish_task_event must fire AFTER archive.record() — "
        "load-bearing positional invariant"
    )


def test_ast_pin_publish_wrapped_defensively():
    """The publish call MUST be inside a try/except so a broker
    exception doesn't raise into the policy substrate. This
    pin enforces the best-effort contract structurally."""
    tree = ast.parse(_ARCHIVE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "maybe_record_decision"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_ARCHIVE_SRC, fn)
    assert src is not None
    publish_idx = src.index("publish_task_event(")
    pre = src[:publish_idx]
    post = src[publish_idx:]
    # The publish call must be inside a try block (track-back
    # search for `try:` declaration).
    last_try_idx = pre.rfind("try:")
    last_except_idx = pre.rfind("except")
    assert last_try_idx > last_except_idx, (
        "publish_task_event must be inside a try/except — "
        "broker exception MUST NOT propagate"
    )
    assert "except" in post, (
        "must have an except handler after the publish call"
    )
