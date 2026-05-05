"""Upgrade 2 Slice 4 — decision_drift_detected SSE tests
(PRD §31.3).

Pins:
  § 1 — Event vocabulary constant
  § 2 — Publisher signature + master-off no-op contract
  § 3 — Replay job fires SSE per drift entry (integration)
  § 4 — Replay job DOES NOT fire SSE on clean records
  § 5 — Replay job's exit code authoritative even when SSE
        publish fails (best-effort isolation)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path),
    )
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_REPLAY_ENABLED", "true",
    )


def _clean_record(rid="rec-1"):
    return {
        "record_id": rid, "session_id": "s",
        "op_id": "op", "phase": "ROUTE",
        "kind": "route_selection", "ordinal": 0,
        "inputs_hash": "h",
        "output_repr": '{"a":1,"b":2}',  # canonical
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": "decision_record.1",
    }


def _drifted_record():
    rec = _clean_record(rid="drifted-1")
    # Whitespace makes it non-canonical
    rec["output_repr"] = '{"a": 1, "b": 2}'
    return rec


def _write(tmp_path, sid, records):
    d = tmp_path / sid
    d.mkdir(parents=True, exist_ok=True)
    path = d / "decisions.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
    )
    return path


# ---------------------------------------------------------------------------
# § 1 — Event vocabulary
# ---------------------------------------------------------------------------


class TestEventVocabulary:
    def test_event_constant_present(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_DECISION_DRIFT_DETECTED,
        )
        assert (
            EVENT_TYPE_DECISION_DRIFT_DETECTED
            == "decision_drift_detected"
        )


# ---------------------------------------------------------------------------
# § 2 — Publisher contract
# ---------------------------------------------------------------------------


class TestPublisherContract:
    def test_publish_helper_callable(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_decision_drift_event,
        )
        # Stream master-off → returns None silently; never
        # raises. Verifies the function accepts all expected
        # kwargs.
        result = publish_decision_drift_event(
            session_id="test",
            record_index=42,
            drift_kind="output_repr_non_canonical",
            record_id="rec-1",
            expected='{"a":1}',
            actual='{"a": 1}',
            detail="whitespace mismatch",
            ts_unix=1.0,
        )
        assert result is None or isinstance(result, str)

    def test_publish_handles_missing_optional_strings(
        self,
    ):
        """Empty strings + missing optional fields handled
        defensively."""
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_decision_drift_event,
        )
        result = publish_decision_drift_event(
            session_id="",
            record_index=0,
            drift_kind="parse_error",
            record_id="",
            expected="",
            actual="",
            detail="",
            ts_unix=0.0,
        )
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# § 3 — Replay job fires SSE per drift entry
# ---------------------------------------------------------------------------


class TestReplayFiresSSE:
    def test_fires_one_event_per_drift_entry(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        sid = "drift-session"
        # 2 drifted records → 2 SSE events expected
        _write(tmp_path, sid, [
            _drifted_record(),
            _drifted_record(),
        ])
        # Mock the publisher; capture invocations
        publish_mock = MagicMock(return_value="event-id")
        from backend.core.ouroboros.governance import (
            ide_observability_stream,
        )
        monkeypatch.setattr(
            ide_observability_stream,
            "publish_decision_drift_event",
            publish_mock,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        summary = replay_session_consistency(sid)
        # 2 drift entries detected
        assert len(summary.drift_entries) == 2
        # 2 SSE events published
        assert publish_mock.call_count == 2

    def test_event_payload_carries_session_and_kind(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        sid = "payload-session"
        _write(tmp_path, sid, [_drifted_record()])
        publish_mock = MagicMock(return_value=None)
        from backend.core.ouroboros.governance import (
            ide_observability_stream,
        )
        monkeypatch.setattr(
            ide_observability_stream,
            "publish_decision_drift_event",
            publish_mock,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        replay_session_consistency(sid)
        assert publish_mock.called
        kwargs = publish_mock.call_args.kwargs
        assert kwargs["session_id"] == sid
        assert (
            kwargs["drift_kind"]
            == "output_repr_non_canonical"
        )
        assert kwargs["record_id"] == "drifted-1"
        # Bounded fields present
        assert "expected" in kwargs
        assert "actual" in kwargs
        assert "detail" in kwargs
        # ts_unix is a non-zero monotonic-clock-ish epoch
        assert kwargs["ts_unix"] > 0


# ---------------------------------------------------------------------------
# § 4 — No SSE on clean records
# ---------------------------------------------------------------------------


class TestNoSSEOnCleanRecords:
    def test_clean_session_produces_no_events(
        self, monkeypatch, tmp_path,
    ):
        _setup_ledger(monkeypatch, tmp_path)
        sid = "clean-session"
        _write(tmp_path, sid, [
            _clean_record("r-1"),
            _clean_record("r-2"),
            _clean_record("r-3"),
        ])
        publish_mock = MagicMock()
        from backend.core.ouroboros.governance import (
            ide_observability_stream,
        )
        monkeypatch.setattr(
            ide_observability_stream,
            "publish_decision_drift_event",
            publish_mock,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        summary = replay_session_consistency(sid)
        assert summary.exit_code == 0
        assert len(summary.drift_entries) == 0
        # Zero SSE events fired
        assert not publish_mock.called


# ---------------------------------------------------------------------------
# § 5 — Best-effort isolation — publish failure can't break
#       the replay job
# ---------------------------------------------------------------------------


class TestPublishFailureIsolation:
    def test_exit_code_authoritative_when_publish_raises(
        self, monkeypatch, tmp_path,
    ):
        """Publisher raising MUST NOT propagate — replay's
        exit_code + drift_entries are authoritative."""
        _setup_ledger(monkeypatch, tmp_path)
        sid = "raise-session"
        _write(tmp_path, sid, [_drifted_record()])
        # Publisher that raises
        from backend.core.ouroboros.governance import (
            ide_observability_stream,
        )
        def _broken_publish(**_kw):
            raise RuntimeError("synthetic SSE failure")
        monkeypatch.setattr(
            ide_observability_stream,
            "publish_decision_drift_event",
            _broken_publish,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        # MUST NOT raise — exception isolated
        summary = replay_session_consistency(sid)
        assert summary.exit_code == 1
        assert len(summary.drift_entries) == 1


# ---------------------------------------------------------------------------
# § 6 — Authority floor (replay module — no eager broker import)
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    def test_replay_lazy_imports_publisher(self):
        """The replay module MUST lazy-import the publisher so
        the broker stays out of replay's import graph at module
        load. Pinned by source-grep — top-level lines must NOT
        contain the SSE module import."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "determinism" / "replay_determinism.py"
        )
        source = path.read_text(encoding="utf-8")
        # Lazy import is INSIDE replay_session_consistency, not
        # at module top — verify no top-level import line
        for line in source.splitlines():
            assert not line.startswith(
                "from backend.core.ouroboros.governance"
                ".ide_observability_stream",
            ), (
                "replay_determinism.py must lazy-import the "
                "SSE publisher inside the function body — "
                "NOT at module top"
            )
        # And the lazy import IS present
        assert "publish_decision_drift_event" in source
        assert (
            "ide_observability_stream"
            in source
        )
