"""Regression spine for the Ledger Sovereignty substrate (Slice 1).

The substrate is the foundation for the P1 Containment arc — Slice
2 wires :class:`WorktreeManager.create` to stamp the marker and
:class:`AutoCommitter` to assert against it before any ``git
commit``. This file proves the substrate is correct in isolation
so Slice 2 wiring can compose it confidently.

Coverage axes:

  * §33.1 master-FALSE default + master-ON enable
  * :class:`OwnershipRecord` shape (frozen + roundtrip + schema)
  * :func:`marker_path` purity (no FS touch)
  * :func:`mark_owned` write semantics (atomic, parent mkdir,
    NEVER-raises on failure)
  * :func:`read_ownership` read semantics (missing / unreadable /
    malformed JSON / non-dict / unknown-key forward-compat)
  * :func:`assert_ledger_sovereignty` 4 branches:
      - master-OFF → no-op
      - master-ON + no marker → raises typed
      - master-ON + valid marker → returns
      - master-ON + session_id mismatch → raises typed with both
        IDs surfaced
  * 3 AST pins validate against current source
  * Authority asymmetry (no forbidden imports)
"""
from __future__ import annotations

import ast as _ast
import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.ledger_sovereignty import (
    LEDGER_SOVEREIGNTY_SCHEMA_VERSION,
    LedgerSovereigntyError,
    OwnershipRecord,
    assert_ledger_sovereignty,
    is_owned,
    mark_owned,
    marker_path,
    master_enabled,
    read_ownership,
    register_shipped_invariants,
)


_MASTER_FLAG = "JARVIS_LEDGER_SOVEREIGNTY_ENABLED"


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> Iterator[None]:
    monkeypatch.delenv(_MASTER_FLAG, raising=False)
    # master_enabled() is now `env OR persistent_master signed
    # record`. These tests assert the ENV-only contract, so the
    # persistent input must be isolated (point its dir at an empty
    # tmp path → no record → env-only behavior). The persistent
    # path has its own suite: test_persistent_master.py.
    monkeypatch.setenv(
        "JARVIS_PERSISTENT_MASTER_DIR", str(tmp_path / "pm_iso"),
    )
    yield


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MASTER_FLAG, "true")


# ---------------------------------------------------------------------------
# Master gate (§33.1)
# ---------------------------------------------------------------------------


class TestMasterGate:
    def test_master_default_false(self):
        assert master_enabled() is False

    def test_master_on(self, monkeypatch):
        _enable(monkeypatch)
        assert master_enabled() is True

    def test_master_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(_MASTER_FLAG, "TRUE")
        assert master_enabled() is True

    def test_master_garbage_falls_back_to_false(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_MASTER_FLAG, "yes-please")
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# OwnershipRecord shape
# ---------------------------------------------------------------------------


class TestOwnershipRecord:
    def test_carries_schema_version(self):
        r = OwnershipRecord(
            session_id="bt-x", branch_name="ouroboros/auto/x",
            creator_pid=123,
        )
        assert r.schema_version == (
            LEDGER_SOVEREIGNTY_SCHEMA_VERSION
        )

    def test_is_frozen(self):
        r = OwnershipRecord(
            session_id="bt-x", branch_name="ouroboros/auto/x",
            creator_pid=123,
        )
        with pytest.raises(Exception):
            r.session_id = "other"  # type: ignore[misc]

    def test_to_dict_roundtrip(self):
        r = OwnershipRecord(
            session_id="bt-roundtrip",
            branch_name="ouroboros/auto/round",
            creator_pid=999, created_at=1700000000.0,
        )
        back = OwnershipRecord.from_dict(r.to_dict())
        assert back == r

    def test_from_dict_ignores_unknown_keys(self):
        # Forward-compat: Slice 2+ may add fields; older readers
        # must not break.
        back = OwnershipRecord.from_dict({
            "session_id": "fwd",
            "branch_name": "ouroboros/auto/fwd",
            "creator_pid": 1,
            "created_at": 1.0,
            "future_field_x": "ignored",
        })
        assert back.session_id == "fwd"

    def test_from_dict_missing_schema_version_defaults(self):
        # Legacy markers predating schema_version stay readable.
        back = OwnershipRecord.from_dict({
            "session_id": "old",
            "branch_name": "b",
            "creator_pid": 1,
        })
        assert back.schema_version == (
            LEDGER_SOVEREIGNTY_SCHEMA_VERSION
        )


# ---------------------------------------------------------------------------
# marker_path (pure function)
# ---------------------------------------------------------------------------


class TestMarkerPath:
    def test_does_not_touch_filesystem(self, tmp_path):
        # Computing marker_path on a nonexistent root must not
        # create or touch anything.
        bogus = tmp_path / "does-not-exist"
        result = marker_path(bogus)
        assert result == (
            bogus / ".jarvis" / "ledger_ownership.json"
        )
        assert not bogus.exists()

    def test_uses_jarvis_subdirectory(self, tmp_path):
        result = marker_path(tmp_path)
        assert ".jarvis" in result.parts
        assert result.name == "ledger_ownership.json"


# ---------------------------------------------------------------------------
# mark_owned (write side)
# ---------------------------------------------------------------------------


class TestMarkOwned:
    def test_writes_marker_and_returns_record(self, tmp_path):
        rec = mark_owned(
            tmp_path,
            session_id="bt-s1",
            branch_name="ouroboros/auto/s1",
        )
        assert rec is not None
        assert rec.session_id == "bt-s1"
        assert marker_path(tmp_path).exists()

    def test_creates_jarvis_parent_dir(self, tmp_path):
        target_root = tmp_path / "fresh-worktree"
        target_root.mkdir()
        # No .jarvis/ yet — mark_owned must create it.
        assert not (target_root / ".jarvis").exists()
        rec = mark_owned(
            target_root, session_id="s", branch_name="b",
        )
        assert rec is not None
        assert (target_root / ".jarvis").is_dir()

    def test_payload_is_json_parseable(self, tmp_path):
        mark_owned(
            tmp_path,
            session_id="bt-json", branch_name="bb",
            creator_pid=42,
        )
        payload = json.loads(
            marker_path(tmp_path).read_text(encoding="utf-8")
        )
        assert payload["session_id"] == "bt-json"
        assert payload["branch_name"] == "bb"
        assert payload["creator_pid"] == 42
        assert payload["schema_version"] == (
            LEDGER_SOVEREIGNTY_SCHEMA_VERSION
        )

    def test_uses_os_pid_when_creator_pid_omitted(self, tmp_path):
        rec = mark_owned(
            tmp_path, session_id="s", branch_name="b",
        )
        assert rec is not None
        assert rec.creator_pid == os.getpid()

    def test_write_failure_returns_none(self, tmp_path):
        # Nonexistent root — parent.mkdir handles, but write to
        # a path that resolves to a directory will fail.
        bad = tmp_path / "is_a_dir"
        bad.mkdir()
        # Pre-create the target as a directory so the file write
        # fails.
        (bad / ".jarvis").mkdir()
        (bad / ".jarvis" / "ledger_ownership.json").mkdir()
        result = mark_owned(
            bad, session_id="s", branch_name="b",
        )
        assert result is None  # NEVER raises, returns None

    def test_atomic_write_no_torn_read(self, tmp_path):
        """Marker write goes through ``os.replace`` from a tmp
        sibling — a concurrent reader either sees the previous
        marker (or no marker) or the fully-written new marker,
        never a partial."""
        mark_owned(tmp_path, session_id="v1", branch_name="b1")
        rec1 = read_ownership(tmp_path)
        assert rec1 and rec1.session_id == "v1"
        # Re-mark — should overwrite atomically.
        mark_owned(tmp_path, session_id="v2", branch_name="b2")
        rec2 = read_ownership(tmp_path)
        assert rec2 and rec2.session_id == "v2"
        # No stray .tmp file left behind.
        tmps = list(
            (tmp_path / ".jarvis").glob("*.tmp")
        )
        assert tmps == []


# ---------------------------------------------------------------------------
# read_ownership (read side)
# ---------------------------------------------------------------------------


class TestReadOwnership:
    def test_missing_returns_none(self, tmp_path):
        assert read_ownership(tmp_path) is None

    def test_after_write_returns_record(self, tmp_path):
        mark_owned(
            tmp_path, session_id="s", branch_name="b",
        )
        rec = read_ownership(tmp_path)
        assert rec is not None
        assert rec.session_id == "s"

    def test_unreadable_file_returns_none(self, tmp_path):
        # Create a directory at the marker path so read fails.
        target = marker_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        assert read_ownership(tmp_path) is None

    def test_invalid_json_returns_none(self, tmp_path):
        target = marker_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json at all", encoding="utf-8")
        assert read_ownership(tmp_path) is None

    def test_non_dict_payload_returns_none(self, tmp_path):
        target = marker_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_ownership(tmp_path) is None

    def test_is_owned_convenience(self, tmp_path):
        assert is_owned(tmp_path) is False
        mark_owned(
            tmp_path, session_id="s", branch_name="b",
        )
        assert is_owned(tmp_path) is True


# ---------------------------------------------------------------------------
# assert_ledger_sovereignty — the structural boundary
# ---------------------------------------------------------------------------


class TestAssertLedgerSovereignty:
    def test_master_off_is_noop_on_unowned_path(self, tmp_path):
        # Default — master OFF. Even an unowned path must NOT
        # raise; this is the byte-identical pre-substrate path.
        assert_ledger_sovereignty(tmp_path)  # no raise

    def test_master_off_is_noop_with_session_id(self, tmp_path):
        # Even with expected_session_id supplied, master-OFF stays
        # a no-op.
        assert_ledger_sovereignty(
            tmp_path, expected_session_id="anything",
        )

    def test_master_on_no_marker_raises_typed(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        with pytest.raises(LedgerSovereigntyError) as exc_info:
            assert_ledger_sovereignty(tmp_path)
        assert exc_info.value.path == tmp_path
        assert "no ownership marker" in exc_info.value.reason

    def test_master_on_valid_marker_passes(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        mark_owned(
            tmp_path, session_id="s", branch_name="b",
        )
        assert_ledger_sovereignty(tmp_path)  # no raise

    def test_master_on_session_id_mismatch_raises(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        mark_owned(
            tmp_path,
            session_id="actual-session",
            branch_name="b",
        )
        with pytest.raises(LedgerSovereigntyError) as exc_info:
            assert_ledger_sovereignty(
                tmp_path,
                expected_session_id="expected-session",
            )
        err = exc_info.value
        assert err.expected_session_id == "expected-session"
        assert err.actual_session_id == "actual-session"
        assert "session_id mismatch" in err.reason

    def test_master_on_session_id_match_passes(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        mark_owned(
            tmp_path, session_id="match", branch_name="b",
        )
        assert_ledger_sovereignty(
            tmp_path, expected_session_id="match",
        )

    def test_master_on_malformed_marker_raises(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        # Write bad JSON — read_ownership returns None →
        # assertion sees "no marker" → raises.
        target = marker_path(tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("garbage", encoding="utf-8")
        with pytest.raises(LedgerSovereigntyError):
            assert_ledger_sovereignty(tmp_path)

    def test_error_carries_structured_fields(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        try:
            assert_ledger_sovereignty(tmp_path)
            pytest.fail("expected raise")
        except LedgerSovereigntyError as err:
            # Structured attributes — telemetry can read them
            # without parsing the message.
            assert hasattr(err, "path")
            assert hasattr(err, "reason")
            assert hasattr(err, "expected_session_id")
            assert hasattr(err, "actual_session_id")

    def test_error_is_runtimeerror_subclass(self):
        # Catch-by-RuntimeError fallback path — existing defenders
        # must still see it.
        assert issubclass(
            LedgerSovereigntyError, RuntimeError,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_returns_three_pins(self):
        pins = register_shipped_invariants()
        names = {p.invariant_name for p in pins}
        assert names == {
            "ledger_sovereignty_master_default_false",
            "ledger_sovereignty_authority_asymmetry",
            "ledger_sovereignty_assert_raises_typed",
        }

    def test_all_pins_pass_on_current_source(self):
        pins = register_shipped_invariants()
        src = Path(
            "backend/core/ouroboros/governance/"
            "ledger_sovereignty.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for pin in pins:
            violations = pin.validate(tree, src)
            assert violations == (), (
                f"{pin.invariant_name} drift: {violations}"
            )

    def test_authority_asymmetry_no_forbidden_imports(self):
        # Belt-and-suspenders: re-run the asymmetry check
        # directly here so a future drift of the pin itself can't
        # silently hide a violation.
        src = Path(
            "backend/core/ouroboros/governance/"
            "ledger_sovereignty.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        forbidden = {
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            (
                "backend.core.ouroboros.governance.policy_engine"
            ),
            "backend.core.ouroboros.governance.change_engine",
            (
                "backend.core.ouroboros.governance."
                "candidate_generator"
            ),
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.worktree_manager",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden, (
                    f"forbidden import: {mod}"
                )
