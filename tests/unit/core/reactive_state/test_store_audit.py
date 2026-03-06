"""Tests for post-replay invariant audit wiring in ReactiveStateStore.

Verifies that when an ``AuditLog`` is supplied to the store, the
``post_replay_invariant_audit`` function runs after journal replay and
records its findings into the audit log.

4 tests.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.manifest import (
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.policy import build_default_policy_engine
from backend.core.reactive_state.store import ReactiveStateStore


# ── Helpers ──────────────────────────────────────────────────────────


def _make_store(tmp_path: Path, *, epoch: int = 1, audit_log=None):
    return ReactiveStateStore(
        journal_path=tmp_path / "audit.db",
        epoch=epoch,
        session_id=f"audit-test-{epoch}",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
        audit_log=audit_log,
    )


# ── Tests ────────────────────────────────────────────────────────────


class TestStoreAudit:
    """Post-replay invariant audit integration with the store."""

    def test_clean_replay_no_findings(self, tmp_path: Path) -> None:
        """Write valid defaults, close, reopen with audit_log -- no critical findings."""
        # First session: write some valid state (defaults only)
        s1 = _make_store(tmp_path, epoch=1)
        s1.open()
        s1.initialize_defaults()
        s1.close()

        # Second session: reopen with an audit log
        audit = AuditLog()
        s2 = _make_store(tmp_path, epoch=2, audit_log=audit)
        s2.open()
        try:
            assert not audit.has_critical_findings()
        finally:
            s2.close()

    def test_inconsistent_replay_produces_findings(
        self, tmp_path: Path
    ) -> None:
        """Tamper journal to create cross-key inconsistency -- audit_log has critical findings."""
        db_path = tmp_path / "audit.db"

        # First session: set IP and activate offload (valid state)
        s1 = ReactiveStateStore(
            journal_path=db_path,
            epoch=1,
            session_id="audit-test-1",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
        )
        s1.open()
        s1.initialize_defaults()

        # Set IP so offload can be activated
        ip_entry = s1.read("gcp.node_ip")
        assert ip_entry is not None
        s1.write(
            key="gcp.node_ip",
            value="10.0.0.1",
            expected_version=ip_entry.version,
            writer="gcp_controller",
        )

        # Activate offload
        offload_entry = s1.read("gcp.offload_active")
        assert offload_entry is not None
        s1.write(
            key="gcp.offload_active",
            value=True,
            expected_version=offload_entry.version,
            writer="gcp_controller",
        )
        s1.close()

        # Tamper: insert a journal entry that clears the IP
        conn = sqlite3.connect(str(db_path))
        max_rev = conn.execute(
            "SELECT MAX(global_revision) FROM state_journal"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO state_journal (global_revision, key, value, previous_value, "
            "version, epoch, writer, writer_session_id, origin, consistency_group, "
            "timestamp_unix_ms, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                max_rev + 1,
                "gcp.node_ip",
                json.dumps(""),
                json.dumps("10.0.0.1"),
                3,
                1,
                "gcp_controller",
                "tamper",
                "explicit",
                None,
                0,
                "tampered",
            ),
        )
        conn.commit()
        conn.close()

        # Second session: reopen with audit log -- should detect inconsistency
        audit = AuditLog()
        s2 = ReactiveStateStore(
            journal_path=db_path,
            epoch=2,
            session_id="audit-test-2",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
            audit_log=audit,
        )
        s2.open()
        try:
            assert audit.has_critical_findings()
            findings = audit.findings
            # Should have at least one ERROR about gcp.offload_active + empty IP
            error_findings = [
                f for f in findings if f.severity == AuditSeverity.ERROR
            ]
            assert len(error_findings) >= 1
            assert any(
                "gcp.node_ip" in f.message or "gcp.offload_active" in f.key
                for f in error_findings
            )
        finally:
            s2.close()

    def test_no_audit_log_skips_audit(self, tmp_path: Path) -> None:
        """Store without audit_log opens and closes without crash."""
        s = _make_store(tmp_path, epoch=1)
        s.open()
        s.initialize_defaults()
        s.close()

        # Reopen without audit_log (None default)
        s2 = _make_store(tmp_path, epoch=2)
        s2.open()
        try:
            # Should work fine -- no audit_log, no crash
            entry = s2.read("gcp.offload_active")
            assert entry is not None
        finally:
            s2.close()

    def test_audit_log_accessible(self, tmp_path: Path) -> None:
        """store.audit_log returns the same AuditLog instance passed to constructor."""
        audit = AuditLog()
        s = _make_store(tmp_path, epoch=1, audit_log=audit)
        s.open()
        try:
            assert s.audit_log is audit
        finally:
            s.close()
