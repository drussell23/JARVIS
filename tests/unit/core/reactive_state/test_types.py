"""Tests for the reactive state store core data types.

Covers frozen-ness, field accessibility, enum completeness, and
WriteResult construction patterns for both success and rejection cases.
"""
from __future__ import annotations

import time

import pytest

from backend.core.reactive_state.types import (
    JournalEntry,
    StateEntry,
    WriteRejection,
    WriteResult,
    WriteStatus,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_state_entry(**overrides) -> StateEntry:
    defaults = dict(
        key="gcp.vm_ready",
        value=True,
        version=1,
        epoch=0,
        writer="supervisor",
        origin="explicit",
        updated_at_mono=time.monotonic(),
        updated_at_unix_ms=int(time.time() * 1000),
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def _make_journal_entry(**overrides) -> JournalEntry:
    defaults = dict(
        global_revision=42,
        key="gcp.vm_ready",
        value=True,
        previous_value=False,
        version=2,
        epoch=0,
        writer="supervisor",
        writer_session_id="sess-abc-123",
        origin="explicit",
        consistency_group=None,
        timestamp_unix_ms=int(time.time() * 1000),
        checksum="sha256:deadbeef",
    )
    defaults.update(overrides)
    return JournalEntry(**defaults)


def _make_write_rejection(**overrides) -> WriteRejection:
    defaults = dict(
        key="gcp.vm_ready",
        writer="rogue-writer",
        writer_session_id="sess-xyz-789",
        reason=WriteStatus.OWNERSHIP_REJECTED,
        epoch=0,
        attempted_version=3,
        current_version=2,
        global_revision_at_reject=41,
        timestamp_mono=time.monotonic(),
    )
    defaults.update(overrides)
    return WriteRejection(**defaults)


# ── TestStateEntry ─────────────────────────────────────────────────────


class TestStateEntry:
    """StateEntry is a frozen dataclass with accessible fields."""

    def test_state_entry_is_frozen(self):
        entry = _make_state_entry()
        with pytest.raises(AttributeError):
            entry.key = "something_else"  # type: ignore[misc]

    def test_state_entry_fields_accessible(self):
        now_mono = time.monotonic()
        now_ms = int(time.time() * 1000)
        entry = _make_state_entry(
            key="audio.active",
            value=False,
            version=7,
            epoch=2,
            writer="voice_orchestrator",
            origin="derived",
            updated_at_mono=now_mono,
            updated_at_unix_ms=now_ms,
        )
        assert entry.key == "audio.active"
        assert entry.value is False
        assert entry.version == 7
        assert entry.epoch == 2
        assert entry.writer == "voice_orchestrator"
        assert entry.origin == "derived"
        assert entry.updated_at_mono == now_mono
        assert entry.updated_at_unix_ms == now_ms


# ── TestWriteResult ────────────────────────────────────────────────────


class TestWriteResult:
    """WriteResult carries either an entry (OK) or a rejection."""

    def test_write_result_ok_has_entry_no_rejection(self):
        entry = _make_state_entry()
        result = WriteResult(status=WriteStatus.OK, entry=entry)
        assert result.status == WriteStatus.OK
        assert result.entry is entry
        assert result.rejection is None

    def test_write_result_rejection_has_rejection_no_entry(self):
        rejection = _make_write_rejection()
        result = WriteResult(
            status=WriteStatus.OWNERSHIP_REJECTED,
            rejection=rejection,
        )
        assert result.status == WriteStatus.OWNERSHIP_REJECTED
        assert result.entry is None
        assert result.rejection is rejection


# ── TestWriteStatus ────────────────────────────────────────────────────


class TestWriteStatus:
    """All six WriteStatus enum values must exist."""

    def test_all_write_status_values_exist(self):
        expected = {
            "OK",
            "VERSION_CONFLICT",
            "OWNERSHIP_REJECTED",
            "SCHEMA_INVALID",
            "EPOCH_STALE",
            "POLICY_REJECTED",
        }
        actual = {member.name for member in WriteStatus}
        assert actual == expected

    def test_write_status_is_str_enum(self):
        assert isinstance(WriteStatus.OK, str)
        assert WriteStatus.OK == "OK"
        assert WriteStatus.VERSION_CONFLICT == "VERSION_CONFLICT"


# ── TestJournalEntry ───────────────────────────────────────────────────


class TestJournalEntry:
    """JournalEntry is a frozen dataclass."""

    def test_journal_entry_is_frozen(self):
        entry = _make_journal_entry()
        with pytest.raises(AttributeError):
            entry.key = "something_else"  # type: ignore[misc]

    def test_journal_entry_fields_accessible(self):
        entry = _make_journal_entry(
            global_revision=99,
            key="prime.endpoint",
            value="https://new.endpoint",
            previous_value="https://old.endpoint",
            version=5,
            epoch=1,
            writer="prime_router",
            writer_session_id="sess-prime-001",
            origin="explicit",
            consistency_group="prime_config",
            timestamp_unix_ms=1700000000000,
            checksum="sha256:cafebabe",
        )
        assert entry.global_revision == 99
        assert entry.key == "prime.endpoint"
        assert entry.value == "https://new.endpoint"
        assert entry.previous_value == "https://old.endpoint"
        assert entry.version == 5
        assert entry.epoch == 1
        assert entry.writer == "prime_router"
        assert entry.writer_session_id == "sess-prime-001"
        assert entry.origin == "explicit"
        assert entry.consistency_group == "prime_config"
        assert entry.timestamp_unix_ms == 1700000000000
        assert entry.checksum == "sha256:cafebabe"


# ── TestWriteRejection ────────────────────────────────────────────────


class TestWriteRejection:
    """WriteRejection is a frozen dataclass."""

    def test_write_rejection_is_frozen(self):
        rejection = _make_write_rejection()
        with pytest.raises(AttributeError):
            rejection.key = "something_else"  # type: ignore[misc]
