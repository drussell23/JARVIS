"""OCA structural channel resolution — regression spine.

Closes the root cause of the autonomous-commit incident
(2026-05-19): ``commit_authority_cli`` inferred the commit
channel from ``JARVIS_COMMIT_CHANNEL`` *hardcoded-defaulting to
"ide"*. A Cursor *Agent*'s headless ``git commit`` runs in the
same process tree / identical env as a human Cursor SCM commit,
so both resolved to ``ide`` and the Agent borrowed the
operator's interactive grant.

The structural fix: an operator channel must be *earned* by a
signed, short-TTL **operator-presence marker** (composes the
SAME ``_sign``/``_verify``/secret as grants — zero new crypto),
minted only at operator-only entry points. Absent a valid
marker, the commit resolves to ``AUTONOMOUS`` and the existing
:mod:`ledger_sovereignty` gate refuses it on a non-owned tree.

Coverage:
  * presence_ttl_s clamp (default / garbage / floor / ceiling)
  * mint → valid roundtrip; tamper / expired / wrong-repo /
    wrong-branch / empty-marker-branch / missing-secret
  * resolve_commit_channel: every case in the closed rule
    (autonomous-env / no-presence / forged-ide / earned-ide /
    explicit-repl / garbage-env)
  * issue_grant + enable_authority MINT presence (operator-only
    entry points)
  * AST pin: cmd_hook_pre_commit has NO ``or "ide"`` and DOES
    call resolve_commit_channel
  * end-to-end verify_pre_commit: forged ide w/o presence +
    sovereignty-on + unowned tree → DENIED_SOVEREIGNTY (the
    Agent-commit-denied proof); earned ide + grant → AUTHORIZED
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    operator_commit_authority as oca,
)


@pytest.fixture(autouse=True)
def _isolated_oca(monkeypatch, tmp_path):
    """Redirect every OCA artifact into a throwaway dir so tests
    never read/write the operator's real ~/.jarvis."""
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_PRESENCE_FILE",
        str(tmp_path / "presence.json"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        str(tmp_path / "enabled"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true",
    )
    for k in ("JARVIS_COMMIT_CHANNEL", "JARVIS_COMMIT_PRESENCE_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    yield


REPO = Path("/Users/op/checkout-A")
OTHER = Path("/Users/op/checkout-B")
C = oca.CommitChannel


# --------------------------------------------------------------------------
# presence_ttl_s clamp
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 900), ("", 900), ("garbage", 900),
        ("10", 60), ("0", 60), ("-5", 60),
        ("1200", 1200), ("999999", 86_400),
    ],
)
def test_presence_ttl_clamp(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("JARVIS_COMMIT_PRESENCE_TTL_S", raising=False)
    else:
        monkeypatch.setenv("JARVIS_COMMIT_PRESENCE_TTL_S", raw)
    assert oca.presence_ttl_s() == expected


# --------------------------------------------------------------------------
# presence mint / verify
# --------------------------------------------------------------------------


def test_mint_then_valid_roundtrip():
    assert oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.valid_operator_presence(REPO, "br", now_unix=1100.0)


def test_presence_tamper_invalidates():
    assert oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    pf = oca.presence_file_path()
    import json
    blob = json.loads(pf.read_text())
    # Multi-entry store: forge a field inside an entry's record —
    # the recomputed-from-trusted-fields HMAC must reject it.
    entries = blob["entries"]
    k = next(iter(entries))
    entries[k]["record"]["operator_label"] = "attacker"
    pf.write_text(json.dumps(blob))
    assert oca.valid_operator_presence(REPO, "br", now_unix=1100.0) is False


def test_presence_expired():
    assert oca.mint_operator_presence(
        REPO, "br", "op", ttl_s=900, now_unix=1000.0,
    )
    # 901s later — past the 900s TTL.
    assert oca.valid_operator_presence(
        REPO, "br", now_unix=1000.0 + 901,
    ) is False


def test_presence_wrong_repo():
    assert oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.valid_operator_presence(
        OTHER, "br", now_unix=1100.0,
    ) is False


def test_presence_wrong_branch():
    assert oca.mint_operator_presence(REPO, "feat", "op", now_unix=1000.0)
    assert oca.valid_operator_presence(
        REPO, "main", now_unix=1100.0,
    ) is False


def test_presence_empty_marker_branch_matches_any():
    assert oca.mint_operator_presence(REPO, "", "op", now_unix=1000.0)
    assert oca.valid_operator_presence(REPO, "anything", now_unix=1100.0)
    assert oca.valid_operator_presence(REPO, "", now_unix=1100.0)


def test_presence_missing_secret_fails_closed(tmp_path, monkeypatch):
    assert oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    # Remove the per-machine secret — verification must fail closed.
    Path(tmp_path / "secret").unlink()
    assert oca.valid_operator_presence(REPO, "br", now_unix=1100.0) is False


# --------------------------------------------------------------------------
# resolve_commit_channel — the closed rule
# --------------------------------------------------------------------------


def test_resolve_env_autonomous_always_autonomous():
    # No presence needed — autonomous is the safe sink.
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="autonomous",
    ) is C.AUTONOMOUS


def test_resolve_no_env_no_presence_is_autonomous():
    assert oca.resolve_commit_channel(REPO, "br") is C.AUTONOMOUS


def test_resolve_no_env_valid_presence_is_ide():
    oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.resolve_commit_channel(
        REPO, "br", now_unix=1100.0,
    ) is C.IDE


def test_resolve_forged_ide_without_presence_is_autonomous():
    # The exact attack: Agent sets/inherits JARVIS_COMMIT_CHANNEL=ide
    # but never minted presence → must NOT get an operator channel.
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="ide",
    ) is C.AUTONOMOUS


def test_resolve_earned_ide_with_presence():
    oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="ide", now_unix=1100.0,
    ) is C.IDE


def test_resolve_explicit_repl_with_presence():
    oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="repl", now_unix=1100.0,
    ) is C.REPL


def test_resolve_explicit_repl_without_presence_is_autonomous():
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="repl",
    ) is C.AUTONOMOUS


def test_resolve_garbage_env_with_presence_is_ide():
    oca.mint_operator_presence(REPO, "br", "op", now_unix=1000.0)
    assert oca.resolve_commit_channel(
        REPO, "br", env_channel="not-a-channel", now_unix=1100.0,
    ) is C.IDE


# --------------------------------------------------------------------------
# operator-only entry points mint presence
# --------------------------------------------------------------------------


def test_issue_grant_mints_presence():
    out = oca.issue_grant(
        channel="ide", operator_label="op",
        ttl_s=60, branch="feat", repo_root=REPO,
        now_unix=1000.0,
    )
    assert out.ok
    assert oca.valid_operator_presence(
        REPO, "feat", now_unix=1010.0,
    ) is True


def test_enable_authority_mints_presence(monkeypatch):
    # enable resolves repo/branch via git; force a deterministic
    # answer so the test is hermetic.
    monkeypatch.setattr(
        oca, "_resolve_repo_root", lambda: REPO,
    )
    monkeypatch.setattr(
        oca, "resolve_repo_root_and_branch",
        lambda _r: (REPO, ""),
    )
    assert oca.enable_authority("op", now_unix=1000.0)
    # Empty marker branch (whole-repo arming) → any branch valid.
    assert oca.valid_operator_presence(
        REPO, "whatever", now_unix=1010.0,
    ) is True


# --------------------------------------------------------------------------
# AST pin — the root-cause line must be gone
# --------------------------------------------------------------------------


def test_ast_pin_no_hardcoded_ide_default():
    cli = Path(
        oca.__file__,
    ).parent / "commit_authority_cli.py"
    tree = ast.parse(cli.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "cmd_hook_pre_commit"
    )
    # (a) no ``<x> or "ide"`` BoolOp anywhere in the hook.
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(
            node.op, ast.Or
        ):
            for v in node.values:
                assert not (
                    isinstance(v, ast.Constant) and v.value == "ide"
                ), "hardcoded `or \"ide\"` channel default is back"
    # (b) it MUST delegate to resolve_commit_channel.
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "resolve_commit_channel"
    ]
    assert calls, "cmd_hook_pre_commit must call resolve_commit_channel"


# --------------------------------------------------------------------------
# End-to-end through verify_pre_commit (real verdict path)
# --------------------------------------------------------------------------


def _tmp_git_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)

    def run(*a):
        subprocess.run(
            ["git", *a], cwd=str(tmp_path),
            check=True, capture_output=True, text=True,
        )
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x\n")
    run("add", "f.txt")
    run("commit", "-q", "-m", "seed")
    return tmp_path


def test_e2e_agent_forged_ide_denied_by_sovereignty(
    tmp_path, monkeypatch,
):
    """Forged JARVIS_COMMIT_CHANNEL=ide + NO presence + sovereignty
    master ON + unowned tree → resolve→AUTONOMOUS → DENIED_SOVEREIGNTY.
    This is the exact Agent-commit-on-main scenario, now refused."""
    monkeypatch.setenv("JARVIS_LEDGER_SOVEREIGNTY_ENABLED", "true")
    repo = _tmp_git_repo(tmp_path / "repo")
    ch = oca.resolve_commit_channel(
        repo, "main", env_channel="ide",
    )
    assert ch is C.AUTONOMOUS
    verdict = oca.verify_pre_commit(
        oca.CommitAuthorityContext(
            channel=ch.value, repo_root=str(repo), branch="main",
        )
    )
    assert (
        verdict.verdict
        is oca.CommitAuthorityVerdict.DENIED_SOVEREIGNTY
    )
    assert not verdict.authorized()


def test_e2e_operator_earned_ide_with_grant_authorized(
    tmp_path, monkeypatch,
):
    """Operator path: issue_grant (mints presence) → resolve earns
    IDE → matching grant → AUTHORIZED."""
    repo = _tmp_git_repo(tmp_path / "repo")
    out = oca.issue_grant(
        channel="ide", operator_label="op", ttl_s=600,
        branch="main", repo_root=repo, now_unix=2000.0,
    )
    assert out.ok
    ch = oca.resolve_commit_channel(
        repo, "main", env_channel="ide", now_unix=2010.0,
    )
    assert ch is C.IDE
    verdict = oca.verify_pre_commit(
        oca.CommitAuthorityContext(
            channel=ch.value, repo_root=str(repo), branch="main",
            now_unix=2010.0,
        )
    )
    assert verdict.authorized(), verdict.detail
