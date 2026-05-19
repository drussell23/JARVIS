"""Slice 3 #1+#2 spine — /commit REPL + CommitAuthorityArchive.

Pins:
  * commit_repl auto-discovery contract (verb basename rule,
    dispatch_commit_command, ok/text/matched, NEVER raises,
    matched=False fall-through)
  * /commit grant refuses empty-branch whole-repo; defaults
    --branch to current; status/recent/help/revoke/enable
  * archive: closed c-N ring, monotonic refs, drop-oldest,
    master-gated default-FALSE, durable flock ledger, recent()
  * AST pins: repl naming-cage (file→verb→dispatcher), archive
    authority-asymmetry + closed taxonomy + canonical flock,
    no `or "ide"` regression anywhere in the new modules
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import commit_repl as cr
from backend.core.ouroboros.governance import (
    commit_authority_archive as ca,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ARCHIVE_PATH",
        str(tmp_path / "archive.jsonl"),
    )
    ca.reset_default_archive_for_tests()
    yield
    ca.reset_default_archive_for_tests()


# --------------------------------------------------------------------------
# commit_repl auto-discovery contract
# --------------------------------------------------------------------------


def test_non_commit_line_falls_through():
    r = cr.dispatch_commit_command("/mode status")
    assert r.matched is False and r.ok is False


def test_bare_and_help_match():
    assert cr.dispatch_commit_command("/commit").matched is True
    h = cr.dispatch_commit_command("/commit help")
    assert h.ok and "Operator Commit Authority" in h.text


def test_unknown_subcommand():
    r = cr.dispatch_commit_command("/commit frobnicate")
    assert r.matched is True and r.ok is False
    assert "unknown subcommand" in r.text


def test_status_never_raises_and_reports():
    r = cr.dispatch_commit_command("/commit status")
    assert r.matched is True
    assert "master_enabled" in r.text or "unavailable" in r.text


def test_grant_refuses_empty_branch(monkeypatch):
    # Force branch resolution to fail → must refuse, not issue a
    # whole-repo grant.
    monkeypatch.setattr(cr, "_current_branch", lambda _r: "")
    r = cr.dispatch_commit_command("/commit grant")
    assert r.ok is False
    assert "Refusing an empty whole-repo grant" in r.text


def test_grant_bad_minutes(monkeypatch):
    monkeypatch.setattr(cr, "_current_branch", lambda _r: "feat")
    r = cr.dispatch_commit_command("/commit grant --minutes abc")
    assert r.ok is False and "bad --minutes" in r.text


def test_revoke_requires_target():
    r = cr.dispatch_commit_command("/commit revoke")
    assert r.ok is False and "--id" in r.text


def test_parse_error_is_graceful():
    r = cr.dispatch_commit_command('/commit grant --label "unterminated')
    assert r.matched is True and r.ok is False
    assert "parse error" in r.text


# --------------------------------------------------------------------------
# CommitAuthorityArchive
# --------------------------------------------------------------------------


def test_archive_master_off_is_noop(monkeypatch):
    monkeypatch.delenv(ca.MASTER_FLAG_ENV_VAR, raising=False)
    assert ca.record(kind="grant_issue", detail={"x": 1}) is None
    assert ca.recent(10) == []


def test_archive_records_cN_monotonic(monkeypatch, tmp_path):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    r1 = ca.record(kind="grant_issue", detail={"grant_id": "g1"})
    r2 = ca.record(kind="revoke", detail={"all": True})
    assert r1.ref == "c-1" and r2.ref == "c-2"
    rec = ca.recent(10)
    assert [x["ref"] for x in rec] == ["c-1", "c-2"]
    assert rec[0]["kind"] == "grant_issue"
    # Durable ledger written via canonical flock primitive.
    led = Path(tmp_path / "archive.jsonl")
    assert led.exists()
    lines = [json.loads(x) for x in led.read_text().splitlines() if x]
    assert {l["ref"] for l in lines} == {"c-1", "c-2"}


def test_archive_unknown_kind_skipped(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    assert ca.record(kind="not_a_kind", detail={}) is None
    assert ca.recent(10) == []


def test_archive_drop_oldest(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    arch = ca.BoundedCommitAuthorityArchive(capacity=2)
    arch.record(kind="grant_issue", detail={})
    arch.record(kind="revoke", detail={})
    arch.record(kind="consume", detail={})
    refs = [r["ref"] for r in arch.recent(10)]
    assert refs == ["c-2", "c-3"]  # c-1 evicted, refs never reused
    assert arch.lookup("c-1") is None


def test_archive_closed_taxonomy():
    assert {k.value for k in ca.CommitAuthorityEventKind} == {
        "grant_issue", "revoke", "verify_verdict",
        "consume", "bypass_suspected", "enable",
    }


def test_record_roundtrip_frozen():
    monkeypatch_env = ca.CommitAuthorityRecord(
        ref="c-9", kind="enable", detail={"label": "x"},
        inserted_at=1.0,
    )
    d = monkeypatch_env.to_dict()
    assert d["ref"] == "c-9" and d["kind"] == "enable"
    with pytest.raises(Exception):
        monkeypatch_env.ref = "c-10"  # frozen


# --------------------------------------------------------------------------
# AST pins
# --------------------------------------------------------------------------


def test_ast_pin_repl_naming_cage():
    src = Path(cr.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # file commit_repl.py → verb /commit → dispatch_commit_command
    assert any(
        isinstance(n, ast.FunctionDef)
        and n.name == "dispatch_commit_command"
        for n in ast.walk(tree)
    ), "naming-cage: dispatch_commit_command must exist"
    assert '"/commit"' in src or "'/commit'" in src
    # authority asymmetry — no decision-side imports
    for bad in (
        "orchestrator", "iron_gate", "candidate_generator",
        "change_engine", "semantic_guardian", "urgency_router",
    ):
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                assert bad not in (n.module or ""), (
                    f"commit_repl must not import {bad}"
                )


def test_ast_pin_archive_invariant_self_validates():
    invs = ca.register_shipped_invariants()
    assert len(invs) == 1
    src = Path(ca.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert invs[0].validate(tree, src) == ()


def test_ast_pin_no_hardcoded_ide_default_regression():
    # Slice 3 modules must not reintroduce the `or "ide"` bug.
    for mod in (cr, ca):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(
                node.op, ast.Or
            ):
                for v in node.values:
                    assert not (
                        isinstance(v, ast.Constant)
                        and v.value == "ide"
                    ), f"{mod.__name__}: `or \"ide\"` regression"


def test_register_flags_two_seeds():
    seen = []

    class _R:
        def register(self, s):
            seen.append(s.name)

    assert ca.register_flags(_R()) == 2
    assert ca.MASTER_FLAG_ENV_VAR in seen
    assert ca.ARCHIVE_SIZE_ENV_VAR in seen
