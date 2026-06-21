# tests/governance/test_epistemic_quarantine.py
from __future__ import annotations
from pathlib import Path
from backend.core.ouroboros.governance import epistemic_quarantine as eq


def _write(p: Path, text: str) -> str:
    p.write_text(text, encoding="utf-8")
    return eq.sha256_of_file(str(p))


def test_atomic_hash_matches_hashlib(tmp_path):
    p = tmp_path / "f.py"
    h = _write(p, "x = 1\n")
    data, digest = eq.atomic_read_and_hash(str(p))
    assert data == b"x = 1\n"
    assert digest == h


def test_atomic_hash_missing_file_returns_empty(tmp_path):
    data, digest = eq.atomic_read_and_hash(str(tmp_path / "nope.py"))
    assert data == b""
    assert digest == ""


def test_quarantine_is_session_scoped(tmp_path):
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale")
    assert led.is_quarantined("a.py") is True
    led2 = eq.QuarantineLedger(path=str(ledger), session_id="S2")
    assert led2.is_quarantined("a.py") is False


def test_quarantine_consult_failopen_on_bad_ledger(tmp_path):
    ledger = tmp_path / "q.jsonl"
    ledger.write_text("{not json\n", encoding="utf-8")
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    assert led.is_quarantined("a.py") is False


def test_reconcile_revalidates_and_drops(tmp_path):
    f = tmp_path / "a.py"
    h = _write(f, "v = 1\n")
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale", root=str(tmp_path), expected_sha=h)
    result = led.reconcile(root=str(tmp_path))
    assert result["revalidated"] == ["a.py"]
    assert result["dropped"] == []


def test_reconcile_drops_when_still_drifted(tmp_path):
    f = tmp_path / "a.py"
    _write(f, "v = 1\n")
    ledger = tmp_path / "q.jsonl"
    led = eq.QuarantineLedger(path=str(ledger), session_id="S1")
    led.quarantine("a.py", reason="stale", root=str(tmp_path), expected_sha="deadbeef")
    result = led.reconcile(root=str(tmp_path))
    assert result["dropped"] == ["a.py"]
    assert result["revalidated"] == []
