# tests/unit/core/reactive_state/test_wave1_integration.py
"""Wave 1 integration -- policy + audit + observability end-to-end."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.reactive_state import (
    ReactiveStateStore,
    WriteStatus,
    build_ownership_registry,
    build_schema_registry,
)
from backend.core.reactive_state.audit import AuditLog, AuditSeverity
from backend.core.reactive_state.policy import build_default_policy_engine


@pytest.fixture
def store(tmp_path: Path) -> ReactiveStateStore:
    audit = AuditLog()
    s = ReactiveStateStore(
        journal_path=tmp_path / "w1_int.db",
        epoch=1,
        session_id="w1-integration",
        ownership_registry=build_ownership_registry(),
        schema_registry=build_schema_registry(),
        policy_engine=build_default_policy_engine(),
        audit_log=audit,
    )
    s.open()
    s.initialize_defaults()
    yield s
    s.close()


class TestWave1Integration:
    def test_full_gcp_activation_sequence(self, store: ReactiveStateStore) -> None:
        """Correct sequence: set IP -> set offload -> set hollow."""
        # Step 1: Set IP
        ip = store.read("gcp.node_ip")
        r1 = store.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip.version, writer="gcp_controller")
        assert r1.status == WriteStatus.OK

        # Step 2: Set offload (now allowed -- IP is set)
        offload = store.read("gcp.offload_active")
        r2 = store.write(key="gcp.offload_active", value=True, expected_version=offload.version, writer="gcp_controller")
        assert r2.status == WriteStatus.OK

        # Step 3: Set hollow (now allowed -- offload is active)
        hollow = store.read("hollow.client_active")
        r3 = store.write(key="hollow.client_active", value=True, expected_version=hollow.version, writer="gcp_controller")
        assert r3.status == WriteStatus.OK

    def test_wrong_sequence_rejected(self, store: ReactiveStateStore) -> None:
        """Out-of-order: set hollow without offload -> POLICY_REJECTED."""
        hollow = store.read("hollow.client_active")
        r = store.write(key="hollow.client_active", value=True, expected_version=hollow.version, writer="gcp_controller")
        assert r.status == WriteStatus.POLICY_REJECTED

        # Rejection counted
        stats = store.rejection_stats()
        assert ("hollow.client_active", "POLICY_REJECTED") in stats

    def test_replay_audit_detects_inconsistency(self, tmp_path: Path) -> None:
        """Write consistent state, tamper journal, reopen -> audit finds error."""
        import json
        import sqlite3

        db_path = tmp_path / "replay_audit.db"
        audit1 = AuditLog()
        s1 = ReactiveStateStore(
            journal_path=db_path, epoch=1, session_id="s1",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
            audit_log=audit1,
        )
        s1.open()
        s1.initialize_defaults()
        # Set IP and offload correctly
        ip = s1.read("gcp.node_ip")
        s1.write(key="gcp.node_ip", value="10.0.0.1", expected_version=ip.version, writer="gcp_controller")
        offload = s1.read("gcp.offload_active")
        s1.write(key="gcp.offload_active", value=True, expected_version=offload.version, writer="gcp_controller")
        s1.close()

        # Tamper: clear IP in journal
        conn = sqlite3.connect(str(db_path))
        max_rev = conn.execute("SELECT MAX(global_revision) FROM state_journal").fetchone()[0]
        conn.execute(
            "INSERT INTO state_journal (global_revision, key, value, previous_value, "
            "version, epoch, writer, writer_session_id, origin, consistency_group, "
            "timestamp_unix_ms, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (max_rev + 1, "gcp.node_ip", json.dumps(""), json.dumps("10.0.0.1"),
             3, 1, "gcp_controller", "tamper", "explicit", None, 0, "tampered"),
        )
        conn.commit()
        conn.close()

        # Reopen with new audit log
        audit2 = AuditLog()
        s2 = ReactiveStateStore(
            journal_path=db_path, epoch=2, session_id="s2",
            ownership_registry=build_ownership_registry(),
            schema_registry=build_schema_registry(),
            policy_engine=build_default_policy_engine(),
            audit_log=audit2,
        )
        s2.open()
        assert audit2.has_critical_findings() is True
        s2.close()
