"""Tests for backend.core.managed_mode — managed-mode contract utilities.

TDD: written before implementation to verify all public API contracts.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
import time

import pytest


# ---------------------------------------------------------------------------
# TestManagedModeFlags
# ---------------------------------------------------------------------------


class TestManagedModeFlags:
    """JARVIS_ROOT_MANAGED env-var detection."""

    def test_root_managed_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ROOT_MANAGED", raising=False)
        from backend.core.managed_mode import is_root_managed

        assert is_root_managed() is False

    def test_root_managed_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_MANAGED", "true")
        from backend.core.managed_mode import is_root_managed

        assert is_root_managed() is True

    def test_root_managed_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_MANAGED", "TRUE")
        from backend.core.managed_mode import is_root_managed

        assert is_root_managed() is True


# ---------------------------------------------------------------------------
# TestExitCodes
# ---------------------------------------------------------------------------


class TestExitCodes:
    """Well-known exit code constants."""

    def test_exit_code_constants(self):
        from backend.core.managed_mode import (
            EXIT_CLEAN,
            EXIT_CONFIG_ERROR,
            EXIT_CONTRACT_MISMATCH,
            EXIT_DEPENDENCY_FAILURE,
            EXIT_RUNTIME_FATAL,
        )

        assert EXIT_CLEAN == 0
        assert EXIT_CONFIG_ERROR == 100
        assert EXIT_CONTRACT_MISMATCH == 101
        assert EXIT_DEPENDENCY_FAILURE == 200
        assert EXIT_RUNTIME_FATAL == 300


# ---------------------------------------------------------------------------
# TestExecFingerprint
# ---------------------------------------------------------------------------


class TestExecFingerprint:
    """SHA-256-based exec fingerprint."""

    def test_deterministic(self):
        from backend.core.managed_mode import compute_exec_fingerprint

        fp1 = compute_exec_fingerprint("/usr/bin/python3", ["main.py", "--flag"])
        fp2 = compute_exec_fingerprint("/usr/bin/python3", ["main.py", "--flag"])
        assert fp1 == fp2
        assert fp1.startswith("sha256:")
        # 16 hex chars after prefix
        assert len(fp1) == len("sha256:") + 16

    def test_different_inputs(self):
        from backend.core.managed_mode import compute_exec_fingerprint

        fp1 = compute_exec_fingerprint("/usr/bin/python3", ["main.py"])
        fp2 = compute_exec_fingerprint("/usr/bin/python3.11", ["main.py"])
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# TestCapabilityHash
# ---------------------------------------------------------------------------


class TestCapabilityHash:
    """Deterministic JSON-based capability hashing."""

    def test_deterministic(self):
        from backend.core.managed_mode import compute_capability_hash

        caps = {"voice": True, "vision": False}
        h1 = compute_capability_hash(caps)
        h2 = compute_capability_hash(caps)
        assert h1 == h2

    def test_order_independent(self):
        from backend.core.managed_mode import compute_capability_hash

        h1 = compute_capability_hash({"b": 2, "a": 1})
        h2 = compute_capability_hash({"a": 1, "b": 2})
        assert h1 == h2


# ---------------------------------------------------------------------------
# TestHMACAuth
# ---------------------------------------------------------------------------


class TestHMACAuth:
    """HMAC-based control-plane auth header."""

    def test_build_and_verify(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth

        session = "sess-abc-123"
        secret = "s3cr3t"
        header = build_hmac_auth(session, secret)
        assert verify_hmac_auth(header, session, secret) is True

    def test_reject_wrong_secret(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth

        session = "sess-abc-123"
        header = build_hmac_auth(session, "correct-secret")
        assert verify_hmac_auth(header, session, "wrong-secret") is False

    def test_reject_wrong_session(self):
        from backend.core.managed_mode import build_hmac_auth, verify_hmac_auth

        secret = "s3cr3t"
        header = build_hmac_auth("session-A", secret)
        assert verify_hmac_auth(header, "session-B", secret) is False

    def test_reject_expired(self):
        from backend.core.managed_mode import verify_hmac_auth

        session = "sess-abc-123"
        secret = "s3cr3t"
        # Manually build a header with a timestamp 60 seconds in the past
        old_ts = str(time.time() - 60.0)
        nonce = "deadbeef"
        msg = f"{old_ts}:{nonce}:{session}".encode()
        sig = hmac_mod.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        header = f"{old_ts}:{nonce}:{sig}"
        # Default tolerance is 30s, so 60s-old header should be rejected
        assert verify_hmac_auth(header, session, secret, tolerance_s=30.0) is False


# ---------------------------------------------------------------------------
# TestHealthEnvelope
# ---------------------------------------------------------------------------


class TestHealthEnvelope:
    """build_health_envelope: enrichment in managed mode, passthrough in standalone."""

    def test_build_envelope(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "sess-xyz-789")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "prime")
        from backend.core.managed_mode import build_health_envelope

        base = {"status": "ok", "custom_key": 42}
        env = build_health_envelope(base, readiness="ready")

        # Original fields preserved
        assert env["status"] == "ok"
        assert env["custom_key"] == 42
        # Enrichment fields
        assert env["liveness"] == "up"
        assert env["readiness"] == "ready"
        assert env["session_id"] == "sess-xyz-789"
        assert env["subsystem_role"] == "prime"
        assert env["schema_version"] == "1.0.0"
        assert "pid" in env
        assert "start_time_ns" in env
        assert "exec_fingerprint" in env
        assert "observed_at_ns" in env
        assert "wall_time_utc" in env
        # drain_id absent when not provided
        assert env.get("drain_id") is None

    def test_drain_id_included(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ROOT_SESSION_ID", "sess-xyz-789")
        monkeypatch.setenv("JARVIS_SUBSYSTEM_ROLE", "reactor")
        from backend.core.managed_mode import build_health_envelope

        env = build_health_envelope({}, readiness="draining", drain_id="drain-001")
        assert env["drain_id"] == "drain-001"

    def test_no_enrichment_without_session(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ROOT_SESSION_ID", raising=False)
        from backend.core.managed_mode import build_health_envelope

        base = {"status": "ok"}
        env = build_health_envelope(base, readiness="ready")
        # Should be the base response unchanged — no enrichment keys
        assert env == {"status": "ok"}
        assert "liveness" not in env
