"""Slice 123 Phase 1 — Boot-Recovery Provenance Quarantine.

Proves the quarantine engine sequesters unvouched recovery ops off the hot path
WITHOUT ever breaking recovery (best-effort, gated, never raises).
"""

from __future__ import annotations

import json

from backend.core.ouroboros.governance import boot_recovery_quarantine as Q


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED", raising=False)
    assert Q.quarantine_enabled() is False
    monkeypatch.setenv("JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED", "1")
    assert Q.quarantine_enabled() is True


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_QUARANTINE_DIR", str(tmp_path))
    out = Q.quarantine_op("op-1", {"target_file": "x"}, "boot_recovery_missing_provenance")
    assert out is None
    assert list(tmp_path.iterdir()) == []  # nothing written when off


class TestQuarantine:
    def _enable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_QUARANTINE_DIR", str(tmp_path))

    def test_op_payload_is_sequestered(self, tmp_path, monkeypatch):
        self._enable(tmp_path, monkeypatch)
        out = Q.quarantine_op(
            "op-abc", {"recovery_attempt_id": "r1", "stale": "data"},
            "boot_recovery_missing_provenance", now=1700.0,
        )
        assert out is not None
        p = tmp_path / "1700_op-abc.json"
        assert p.exists()
        rec = json.loads(p.read_text())
        assert rec["op_id"] == "op-abc"
        assert rec["reason"] == "boot_recovery_missing_provenance"
        assert rec["payload"]["recovery_attempt_id"] == "r1"
        assert rec["schema_version"] == "quarantine.1"

    def test_filename_is_sanitized(self, tmp_path, monkeypatch):
        self._enable(tmp_path, monkeypatch)
        out = Q.quarantine_op("op/../../etc/passwd", {}, "r", now=1.0)
        assert out is not None
        # No path traversal — the slashes are scrubbed.
        written = list(tmp_path.iterdir())
        assert len(written) == 1
        assert written[0].parent == tmp_path
        assert "/" not in written[0].name.replace(str(tmp_path), "")

    def test_non_jsonable_payload_degrades_to_repr(self, tmp_path, monkeypatch):
        self._enable(tmp_path, monkeypatch)

        class Weird:
            def __repr__(self):
                return "WEIRD_OBJ"

        out = Q.quarantine_op("op-x", {"obj": Weird(), "n": 1}, "r", now=2.0)
        rec = json.loads((tmp_path / "2_op-x.json").read_text())
        assert rec["payload"]["obj"] == "WEIRD_OBJ"  # coerced, not crashed
        assert rec["payload"]["n"] == 1

    def test_never_raises_on_bad_dir(self, monkeypatch):
        # Point the dir at an unwritable location — must return None, not raise.
        monkeypatch.setenv("JARVIS_BOOT_RECOVERY_QUARANTINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_QUARANTINE_DIR", "/proc/cannot/write/here")
        out = Q.quarantine_op("op-1", {"a": 1}, "r")
        assert out is None  # swallowed, recovery continues

    def test_list_quarantined_roundtrip(self, tmp_path, monkeypatch):
        self._enable(tmp_path, monkeypatch)
        Q.quarantine_op("op-1", {"k": 1}, "r", now=10.0)
        Q.quarantine_op("op-2", {"k": 2}, "r", now=20.0)
        listed = Q.list_quarantined()
        assert len(listed) == 2
        ids = {r["op_id"] for r in listed}
        assert ids == {"op-1", "op-2"}
