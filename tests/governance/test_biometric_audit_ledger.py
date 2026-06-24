"""Tests for the hash-chained Immutable Audit Ledger.

Every authorization attempt (AUTHORIZED + REJECTED) appends an
immutable, hash-chained record binding voice-print -> AST mutation.
Tampering with any past record breaks the chain (verifiable).
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.command_node import (
    biometric_audit_ledger as al,
)


def _record(**overrides):
    base = {
        "pr_id": "PR-1",
        "target_repo": "jarvis",
        "ast_mutation_id": "ast-1",
        "blast_radius_hash": "br-1",
        "challenge_nonce": "n" * 64,
        "voiceprint_id": "owner",
        "ecapa_score": 0.95,
        "antispoof_verdict": True,
        "freshness_ok": True,
        "decision": "AUTHORIZED",
        "audio_sha256": "a" * 64,
    }
    base.update(overrides)
    return base


def _ledger(tmp_path):
    return al.BiometricAuditLedger(path=tmp_path / "command_node_audit.jsonl")


def test_append_and_verify_chain(tmp_path):
    led = _ledger(tmp_path)
    led.append(_record(decision="AUTHORIZED"))
    led.append(_record(decision="REJECTED", pr_id="PR-2"))
    assert led.verify_chain() is True


def test_append_returns_record_hash(tmp_path):
    led = _ledger(tmp_path)
    r1 = led.append(_record())
    assert r1["record_hash"]
    assert len(r1["record_hash"]) == 64  # sha256 hex
    assert r1["prev_hash"] == al.GENESIS_HASH


def test_chain_links_prev_hash(tmp_path):
    led = _ledger(tmp_path)
    r1 = led.append(_record(pr_id="PR-1"))
    r2 = led.append(_record(pr_id="PR-2"))
    assert r2["prev_hash"] == r1["record_hash"]


def test_tampered_record_breaks_chain(tmp_path):
    path = tmp_path / "command_node_audit.jsonl"
    led = al.BiometricAuditLedger(path=path)
    led.append(_record(pr_id="PR-1", ecapa_score=0.40))
    led.append(_record(pr_id="PR-2"))
    assert led.verify_chain() is True

    # Tamper: rewrite the first record's score (simulate a forged
    # "pass" after the fact) WITHOUT recomputing the chain.
    lines = path.read_text().splitlines()
    rec0 = json.loads(lines[0])
    rec0["ecapa_score"] = 0.99  # forge a higher score
    lines[0] = json.dumps(rec0, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    led2 = al.BiometricAuditLedger(path=path)
    assert led2.verify_chain() is False


def test_tampered_record_hash_breaks_chain(tmp_path):
    path = tmp_path / "command_node_audit.jsonl"
    led = al.BiometricAuditLedger(path=path)
    led.append(_record(pr_id="PR-1"))
    led.append(_record(pr_id="PR-2"))

    lines = path.read_text().splitlines()
    rec1 = json.loads(lines[1])
    rec1["record_hash"] = "f" * 64  # forge the chain pointer
    lines[1] = json.dumps(rec1, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    led2 = al.BiometricAuditLedger(path=path)
    assert led2.verify_chain() is False


def test_deleted_middle_record_breaks_chain(tmp_path):
    path = tmp_path / "command_node_audit.jsonl"
    led = al.BiometricAuditLedger(path=path)
    led.append(_record(pr_id="PR-1"))
    led.append(_record(pr_id="PR-2"))
    led.append(_record(pr_id="PR-3"))

    lines = path.read_text().splitlines()
    del lines[1]  # excise the middle record
    path.write_text("\n".join(lines) + "\n")

    led2 = al.BiometricAuditLedger(path=path)
    assert led2.verify_chain() is False


def test_empty_ledger_verifies(tmp_path):
    led = _ledger(tmp_path)
    assert led.verify_chain() is True


def test_record_carries_ts(tmp_path):
    led = _ledger(tmp_path)
    r = led.append(_record())
    assert "ts" in r and r["ts"]


def test_append_failure_does_not_raise(tmp_path):
    # Point the ledger at a path whose parent is a file -> mkdir fails.
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    led = al.BiometricAuditLedger(path=bad_parent / "sub" / "audit.jsonl")
    # Fail-soft: returns a record dict (with hash computed) but the
    # write loudly logs and does not raise.
    r = led.append(_record())
    assert r is not None  # never raises; record still computed


def test_audio_sha256_present_no_raw_audio(tmp_path):
    led = _ledger(tmp_path)
    led.append(_record(audio_sha256="b" * 64))
    path = tmp_path / "command_node_audit.jsonl"
    content = path.read_text()
    assert ("b" * 64) in content
    # Sanity: the record schema has no 'audio' field at all.
    rec = json.loads(content.splitlines()[0])
    assert "audio" not in rec
    assert "audio_sha256" in rec


# ===========================================================================
# M2 -- AuditWriteError and raise_on_write_failure tests
# ===========================================================================


def test_append_raise_on_write_failure_raises_on_bad_path(tmp_path):
    """M2: raise_on_write_failure=True on a bad path raises AuditWriteError."""
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    led = al.BiometricAuditLedger(path=bad_parent / "sub" / "audit.jsonl")
    with pytest.raises(al.AuditWriteError):
        led.append(_record(), raise_on_write_failure=True)


def test_append_raise_on_write_failure_false_does_not_raise_on_bad_path(tmp_path):
    """M2: raise_on_write_failure=False (default) on a bad path does NOT raise
    (preserves original fail-soft behavior)."""
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    led = al.BiometricAuditLedger(path=bad_parent / "sub" / "audit.jsonl")
    # Must not raise; returns a record dict.
    r = led.append(_record(), raise_on_write_failure=False)
    assert r is not None


def test_append_raise_on_write_failure_true_succeeds_on_valid_path(tmp_path):
    """M2: raise_on_write_failure=True on a valid path succeeds and returns
    a record with record_hash."""
    led = _ledger(tmp_path)
    r = led.append(_record(), raise_on_write_failure=True)
    assert r is not None
    assert r["record_hash"]
    assert led.verify_chain() is True


def test_audit_write_error_is_exported():
    """M2: AuditWriteError is in __all__ and importable from the module."""
    assert hasattr(al, "AuditWriteError")
    assert al.AuditWriteError.__name__ == "AuditWriteError"
    assert issubclass(al.AuditWriteError, Exception)
