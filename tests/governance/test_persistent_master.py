"""PersistentMaster — regression spine.

Generic signed, out-of-repo master-enable (factored from OCA Slice
3 #0). The ONLY way a master gate can be ON for a Cursor/VS Code
GUI-git subprocess (no shell env). Composes the canonical
roadmap_reader HMAC + the ONE OCA per-machine secret — zero crypto
duplication, fail-closed, NEVER raises.

Coverage:
  * _sanitize_key (injection-free filename token)
  * enable_record_dir/path + env override
  * enable → is_persistently_enabled roundtrip; disable idempotent
  * tamper / flag-key-mismatch / enabled!=True / missing-secret
    / empty-label all → fail-closed
  * independent flag_keys
  * NEVER raises on garbage
  * ledger_sovereignty.master_enabled(): env-true short-circuit
    (no disk) / env-unset+signed-record → True / neither → False
  * AST pin self-validates green
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import persistent_master as pm
from backend.core.ouroboros.governance import ledger_sovereignty as ls


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_PERSISTENT_MASTER_DIR", str(tmp_path / "pm"),
    )
    # Route OCA's single per-machine secret to the throwaway dir.
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.delenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", raising=False)
    yield


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ledger_sovereignty", "ledger_sovereignty"),
        ("JARVIS_LEDGER_SOVEREIGNTY_ENABLED",
         "jarvis_ledger_sovereignty_enabled"),
        ("a/b\\c..d", "a_b_c_d"),
        ("  Mixed-CASE!! ", "mixed_case"),
        ("", "unnamed"),
        ("///", "unnamed"),
    ],
)
def test_sanitize_key(raw, expected):
    assert pm._sanitize_key(raw) == expected


def test_record_path_and_env_override(tmp_path):
    p = pm.enable_record_path("ledger_sovereignty")
    assert p == tmp_path / "pm" / "ledger_sovereignty.json"


def test_enable_roundtrip_and_disable():
    assert pm.is_persistently_enabled("ledger_sovereignty") is False
    assert pm.enable_persistent_master(
        "ledger_sovereignty", "op", now_unix=1000.0,
    )
    assert pm.is_persistently_enabled("ledger_sovereignty") is True
    # Idempotent disable.
    assert pm.disable_persistent_master("ledger_sovereignty")
    assert pm.is_persistently_enabled("ledger_sovereignty") is False
    assert pm.disable_persistent_master("ledger_sovereignty")


def test_tamper_invalidates():
    pm.enable_persistent_master("k", "op", now_unix=1000.0)
    f = pm.enable_record_path("k")
    blob = json.loads(f.read_text())
    blob["record"]["operator_label"] = "attacker"
    f.write_text(json.dumps(blob))
    assert pm.is_persistently_enabled("k") is False


def test_flag_key_mismatch_rejected():
    pm.enable_persistent_master("k", "op", now_unix=1000.0)
    f = pm.enable_record_path("k")
    blob = json.loads(f.read_text())
    # File is k.json but record claims a different flag_key.
    blob["record"]["flag_key"] = "other"
    f.write_text(json.dumps(blob))
    assert pm.is_persistently_enabled("k") is False


def test_enabled_not_true_rejected():
    pm.enable_persistent_master("k", "op", now_unix=1000.0)
    f = pm.enable_record_path("k")
    blob = json.loads(f.read_text())
    blob["record"]["enabled"] = False
    f.write_text(json.dumps(blob))
    assert pm.is_persistently_enabled("k") is False


def test_missing_secret_fails_closed(tmp_path):
    pm.enable_persistent_master("k", "op", now_unix=1000.0)
    Path(tmp_path / "secret").unlink()
    assert pm.is_persistently_enabled("k") is False


def test_empty_label_refused():
    assert pm.enable_persistent_master("k", "  ") is False
    assert pm.is_persistently_enabled("k") is False


def test_independent_flag_keys():
    pm.enable_persistent_master("alpha", "op", now_unix=1000.0)
    assert pm.is_persistently_enabled("alpha") is True
    assert pm.is_persistently_enabled("beta") is False


def test_never_raises_on_garbage(tmp_path):
    # Corrupt record file → False, no raise.
    f = pm.enable_record_path("k")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json")
    assert pm.is_persistently_enabled("k") is False
    assert pm.is_persistently_enabled("///") is False


# --------------------------------------------------------------------------
# ledger_sovereignty.master_enabled() composition
# --------------------------------------------------------------------------


def test_sovereignty_env_true_short_circuits(monkeypatch):
    monkeypatch.setenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", "true")
    # No signed record exists, but env wins (legacy byte-identical).
    assert ls.master_enabled() is True


def test_sovereignty_persistent_record_enables(monkeypatch):
    monkeypatch.delenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", raising=False)
    assert ls.master_enabled() is False
    assert pm.enable_persistent_master(
        "ledger_sovereignty", "op", now_unix=1000.0,
    )
    assert ls.master_enabled() is True


def test_sovereignty_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", raising=False)
    assert ls.master_enabled() is False


# --------------------------------------------------------------------------
# Registration contract
# --------------------------------------------------------------------------


def test_shipped_invariant_self_validates_green():
    invs = pm.register_shipped_invariants()
    assert len(invs) == 1
    src = Path(pm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    violations = invs[0].validate(tree, src)
    assert violations == (), f"not green: {violations}"
