"""Slice 3 #3 spine — SSE commit_authority_decision_recorded +
GET /observability/commit-authority wiring.

Pins:
  * EVENT_TYPE_COMMIT_AUTHORITY_DECISION_RECORDED value +
    membership in _VALID_EVENT_TYPES (the stream-pin convention)
  * publish_commit_authority_decision: disabled / non-mapping /
    happy / never-raises (mirrors publish_git_index_anomaly)
  * archive.record() emits the SSE (single source — every parked
    record auto-publishes), fail-silent
  * AST pin: the GET route is registered in
    IDEObservabilityRouter.register_routes; the handler composes
    commit_authority_archive.recent (no parallel projection)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    ide_observability_stream as stream,
)
from backend.core.ouroboros.governance import (
    commit_authority_archive as ca,
)
from backend.core.ouroboros.governance import ide_observability as ido


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ARCHIVE_PATH",
        str(tmp_path / "a.jsonl"),
    )
    ca.reset_default_archive_for_tests()
    yield
    ca.reset_default_archive_for_tests()


def test_event_type_value_and_membership():
    assert (
        stream.EVENT_TYPE_COMMIT_AUTHORITY_DECISION_RECORDED
        == "commit_authority_decision_recorded"
    )
    assert (
        stream.EVENT_TYPE_COMMIT_AUTHORITY_DECISION_RECORDED
        in stream._VALID_EVENT_TYPES
    )


def test_publish_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: False)
    assert stream.publish_commit_authority_decision({"ref": "c-1"}) is None


def test_publish_non_mapping_none(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)
    assert stream.publish_commit_authority_decision("x") is None  # type: ignore[arg-type]


def test_publish_happy(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)
    seen = {}

    class _B:
        def publish(self, et, oid, pl):
            seen.update(et=et, oid=oid, pl=pl)
            return "evt-9"

    monkeypatch.setattr(stream, "get_default_broker", lambda: _B())
    out = stream.publish_commit_authority_decision(
        {"ref": "c-1", "kind": "grant_issue"}
    )
    assert out == "evt-9"
    assert seen["et"] == "commit_authority_decision_recorded"
    assert seen["oid"] == "commit_authority"
    assert seen["pl"]["kind"] == "grant_issue"


def test_publish_never_raises(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)

    def _boom():
        raise RuntimeError("x")

    monkeypatch.setattr(stream, "get_default_broker", _boom)
    assert stream.publish_commit_authority_decision({"ref": "c"}) is None


def test_archive_record_emits_sse(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    fired = []
    monkeypatch.setattr(
        stream, "publish_commit_authority_decision",
        lambda d: fired.append(d),
    )
    rec = ca.record(kind="grant_issue", detail={"grant_id": "g1"})
    assert rec is not None
    assert len(fired) == 1
    assert fired[0]["ref"] == rec.ref
    assert fired[0]["kind"] == "grant_issue"


def test_archive_sse_failure_never_breaks_ring(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")

    def _boom(_d):
        raise RuntimeError("sse exploded")

    monkeypatch.setattr(
        stream, "publish_commit_authority_decision", _boom,
    )
    # Ring insert must still succeed despite the SSE seam blowing up.
    rec = ca.record(kind="revoke", detail={})
    assert rec is not None and rec.ref == "c-1"


def test_ast_pin_route_registered_and_composes_recent():
    src = Path(ido.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # route string present
    assert '"/observability/commit-authority"' in src
    # handler exists + composes commit_authority_archive.recent
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_handle_commit_authority"
        ),
        None,
    )
    assert fn is not None, "_handle_commit_authority missing"
    body = ast.unparse(fn)
    assert "commit_authority_archive" in body
    assert "recent" in body
