"""Slice 3 #4+#5 spine — AutoCommitter unified gate + post-commit hook.

#4 pins (AST — the behavioral path needs a full git repo + many
preconditions; the load-bearing invariants are structural):
  * auto_committer composes operator_commit_authority.verify_pre_commit
  * the channel is the LITERAL "autonomous" (never env, never
    resolve_commit_channel — an autonomous committer must not be
    env-trickable into an operator channel)
  * verdict → skipped_reason mapping present

#5 behavioral + AST:
  * verified-marker write / read-and-clear (one-shot)
  * cmd_hook_post_commit: no marker → bypass_suspected archived;
    fresh marker + oneshot → consume; OCA-off → no-op; always
    returns 0; NEVER raises
  * post-commit subcommand wired into the parser + dispatch
"""
from __future__ import annotations

import ast
import json
import subprocess
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import auto_committer as ac
from backend.core.ouroboros.governance import commit_authority_cli as cli
from backend.core.ouroboros.governance import (
    commit_authority_archive as ca,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ARCHIVE_PATH",
        str(tmp_path / "a.jsonl"),
    )
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    ca.reset_default_archive_for_tests()
    yield
    ca.reset_default_archive_for_tests()


# --------------------------------------------------------------------------
# #4 — AutoCommitter unified gate (AST invariants)
# --------------------------------------------------------------------------


def test_ast_autocommitter_uses_literal_autonomous_channel():
    src = Path(ac.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find the verify_pre_commit call; its CommitAuthorityContext
    # must pass channel="autonomous" as a literal Constant.
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "CommitAuthorityContext"
        ):
            for kw in node.keywords:
                if kw.arg == "channel":
                    assert isinstance(kw.value, ast.Constant), (
                        "channel must be a literal, not computed "
                        "(no env / resolve_commit_channel)"
                    )
                    assert kw.value.value == "autonomous"
                    found = True
    assert found, "auto_committer must call verify_pre_commit via " \
        "CommitAuthorityContext(channel='autonomous', ...)"
    assert "verify_pre_commit" in src
    # The autonomous committer must NEVER *call* resolve_commit_channel
    # nor read JARVIS_COMMIT_CHANNEL (a doc-comment naming the
    # constraint is fine — we assert on AST usage, not substrings).
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "resolve_commit_channel", (
                "auto_committer must not call resolve_commit_channel"
            )
        if isinstance(node, ast.Name):
            assert node.id != "resolve_commit_channel"
        if isinstance(node, ast.Constant) and isinstance(
            node.value, str
        ):
            assert node.value != "JARVIS_COMMIT_CHANNEL", (
                "auto_committer must not read JARVIS_COMMIT_CHANNEL"
            )


def test_ast_autocommitter_maps_verdict_to_skipped_reason():
    src = Path(ac.__file__).read_text(encoding="utf-8")
    assert "ledger_sovereignty_refused" in src
    assert "governance_manifest_drift" in src
    assert "skipped_reason" in src


# --------------------------------------------------------------------------
# #5 — verified-marker primitives
# --------------------------------------------------------------------------


def test_marker_write_read_clear_oneshot(tmp_path):
    root = str(tmp_path)
    cli._write_verified_marker(
        root, channel="ide", matched_grant_id="g-1",
    )
    p = cli._verified_marker_path(root)
    assert p.exists()
    m = cli._read_and_clear_verified_marker(root)
    assert m is not None
    assert m["channel"] == "ide" and m["matched_grant_id"] == "g-1"
    assert "ts" in m
    # One-shot: gone after read.
    assert not p.exists()
    assert cli._read_and_clear_verified_marker(root) is None


def test_marker_corrupt_returns_none(tmp_path):
    root = str(tmp_path)
    p = cli._verified_marker_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert cli._read_and_clear_verified_marker(root) is None


# --------------------------------------------------------------------------
# #5 — cmd_hook_post_commit behavior (real tmp repo)
# --------------------------------------------------------------------------


def _git_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    for a in (
        ["init", "-q"], ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *a], cwd=p, check=True,
                        capture_output=True)
    (p / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=p, check=True,
                    capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=p,
                    check=True, capture_output=True)
    return p


def test_post_commit_oca_off_is_noop(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "r")
    monkeypatch.setattr(cli, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".master_enabled", lambda: False,
    )
    assert cli.cmd_hook_post_commit() == 0
    assert ca.recent(10) == []  # nothing observed when OCA off


def test_post_commit_bypass_suspected_when_no_marker(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path / "r")
    monkeypatch.setattr(cli, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".master_enabled", lambda: True,
    )
    rc = cli.cmd_hook_post_commit()
    assert rc == 0  # post-commit never fails the commit
    rec = ca.recent(10)
    assert len(rec) == 1
    assert rec[0]["kind"] == "bypass_suspected"
    assert "absent" in rec[0]["detail"]["reason"]


def test_post_commit_fresh_marker_no_bypass(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "r")
    monkeypatch.setattr(cli, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".master_enabled", lambda: True,
    )
    cli._write_verified_marker(
        str(repo), channel="ide", matched_grant_id="g-9",
    )
    assert cli.cmd_hook_post_commit() == 0
    # No bypass archived; marker consumed (one-shot).
    kinds = [r["kind"] for r in ca.recent(10)]
    assert "bypass_suspected" not in kinds
    assert cli._read_and_clear_verified_marker(str(repo)) is None


def test_post_commit_oneshot_consume(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "r")
    monkeypatch.setattr(cli, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".master_enabled", lambda: True,
    )
    consumed = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".consume_grant", lambda gid, **kw: consumed.append(gid),
    )
    monkeypatch.setenv("JARVIS_COMMIT_GRANT_ONESHOT", "true")
    cli._write_verified_marker(
        str(repo), channel="ide", matched_grant_id="g-42",
    )
    assert cli.cmd_hook_post_commit() == 0
    assert consumed == ["g-42"]
    assert "consume" in [r["kind"] for r in ca.recent(10)]


def test_post_commit_default_no_consume(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "r")
    monkeypatch.setattr(cli, "_repo_root", lambda: str(repo))
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".master_enabled", lambda: True,
    )
    consumed = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.operator_commit_authority"
        ".consume_grant", lambda gid, **kw: consumed.append(gid),
    )
    monkeypatch.delenv("JARVIS_COMMIT_GRANT_ONESHOT", raising=False)
    cli._write_verified_marker(
        str(repo), channel="ide", matched_grant_id="g-1",
    )
    assert cli.cmd_hook_post_commit() == 0
    # Default: session-lived grants — NOT consumed per commit.
    assert consumed == []


def test_ast_post_commit_wired():
    src = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert any(
        isinstance(n, ast.FunctionDef)
        and n.name == "cmd_hook_post_commit"
        for n in ast.walk(tree)
    )
    # parser accepts post-commit + main dispatches it
    p = cli.build_parser()
    ns = p.parse_args(["hook", "post-commit"])
    assert ns.cmd == "hook" and ns.phase == "post-commit"
    assert '"post-commit"' in src and "post-commit" in src


def test_post_commit_never_raises(monkeypatch):
    # Even with a totally broken repo root, returns 0, no raise.
    monkeypatch.setattr(cli, "_repo_root", lambda: "/nonexistent/zz")
    assert cli.cmd_hook_post_commit() == 0
