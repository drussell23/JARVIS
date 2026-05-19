"""Regression spine — Operator Commit Authority CLI / hook dispatcher
(Slice 2).

Proves the behavior contract: master-OFF preserves the legacy
operator-token gate byte-equivalently; master-ON authorizes via a
signed grant with NO env token (the Cursor IDE fix); authority pass
chains to the file-integrity guardian.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import commit_authority_cli as cli
from backend.core.ouroboros.governance import (
    operator_commit_authority as oca,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED",
        "JARVIS_AUTHORIZE_COMMIT_TOKEN",
        "JARVIS_COMMIT_TOKEN_HASH_FILE",
        "JARVIS_COMMIT_CHANNEL",
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        "JARVIS_LEDGER_SOVEREIGNTY_ENABLED",
        "JARVIS_GOVERNANCE_MANIFEST_ENABLED",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH",
        str(tmp_path / "grants.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_SECRET_PATH",
        str(tmp_path / "secret"),
    )
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE",
        str(tmp_path / "enabled"),
    )
    # Pin repo introspection to a deterministic tmp root and stub the
    # chained integrity hook so we test authority in isolation.
    monkeypatch.setattr(cli, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_branch", lambda root: "main")
    monkeypatch.setattr(cli, "_staged_files", lambda root: ())
    monkeypatch.setattr(
        cli, "_chain_project_hook", lambda root: 0
    )
    yield


def _hash_file(tmp_path, token: str) -> Path:
    hf = tmp_path / "commit_token.sha256"
    hf.write_text(
        hashlib.sha256(token.encode()).hexdigest() + "\n"
    )
    return hf


# ---------------------------------------------------------------------------
# Legacy token compat (single source -- retires the bash hook)
# ---------------------------------------------------------------------------


class TestLegacyToken:
    def test_unset_fails(self):
        ok, reason = cli.legacy_token_ok()
        assert ok is False and "unset" in reason

    def test_correct_token_ok(self, monkeypatch, tmp_path):
        hf = _hash_file(tmp_path, "s3cr3t")
        monkeypatch.setenv("JARVIS_COMMIT_TOKEN_HASH_FILE", str(hf))
        monkeypatch.setenv("JARVIS_AUTHORIZE_COMMIT_TOKEN", "s3cr3t")
        ok, _ = cli.legacy_token_ok()
        assert ok is True

    def test_wrong_token_mismatch(self, monkeypatch, tmp_path):
        hf = _hash_file(tmp_path, "right")
        monkeypatch.setenv("JARVIS_COMMIT_TOKEN_HASH_FILE", str(hf))
        monkeypatch.setenv("JARVIS_AUTHORIZE_COMMIT_TOKEN", "wrong")
        ok, reason = cli.legacy_token_ok()
        assert ok is False and "mismatch" in reason

    def test_missing_hash_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_COMMIT_TOKEN_HASH_FILE",
            str(tmp_path / "nope.sha256"),
        )
        monkeypatch.setenv("JARVIS_AUTHORIZE_COMMIT_TOKEN", "x")
        ok, reason = cli.legacy_token_ok()
        assert ok is False and "missing" in reason


# ---------------------------------------------------------------------------
# Hook dispatcher -- master OFF (legacy gate preserved byte-equivalently)
# ---------------------------------------------------------------------------


class TestHookMasterOff:
    def test_legacy_ok_chains_and_passes(
        self, monkeypatch, tmp_path
    ):
        hf = _hash_file(tmp_path, "tok")
        monkeypatch.setenv("JARVIS_COMMIT_TOKEN_HASH_FILE", str(hf))
        monkeypatch.setenv("JARVIS_AUTHORIZE_COMMIT_TOKEN", "tok")
        assert cli.cmd_hook_pre_commit() == 0

    def test_legacy_missing_blocks(self, monkeypatch):
        # No JARVIS_AUTHORIZE_COMMIT_TOKEN -> Iron Gate refusal.
        assert cli.cmd_hook_pre_commit() == 1

    def test_chain_invoked_only_after_auth(
        self, monkeypatch, tmp_path
    ):
        called = {"n": 0}
        monkeypatch.setattr(
            cli,
            "_chain_project_hook",
            lambda root: called.__setitem__("n", called["n"] + 1)
            or 0,
        )
        # auth fails -> chain must NOT run
        assert cli.cmd_hook_pre_commit() == 1
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# Hook dispatcher -- master ON (the Cursor IDE fix: NO env token)
# ---------------------------------------------------------------------------


class TestHookMasterOn:
    def test_no_grant_blocks(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        assert cli.cmd_hook_pre_commit() == 1

    def test_signed_grant_authorizes_without_env_token(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        # Critical: NO JARVIS_AUTHORIZE_COMMIT_TOKEN in env.
        assert "JARVIS_AUTHORIZE_COMMIT_TOKEN" not in __import__(
            "os"
        ).environ
        out = oca.issue_grant(
            channel="ide",
            operator_label="derek",
            repo_root=tmp_path,
        )
        assert out.ok
        assert cli.cmd_hook_pre_commit() == 0

    def test_channel_env_override_respected(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        monkeypatch.setenv("JARVIS_COMMIT_CHANNEL", "cli")
        oca.issue_grant(
            channel="ide",
            operator_label="d",
            repo_root=tmp_path,
        )
        # grant was for ide, commit channel is cli -> blocked
        assert cli.cmd_hook_pre_commit() == 1
        oca.issue_grant(
            channel="cli",
            operator_label="d",
            repo_root=tmp_path,
        )
        assert cli.cmd_hook_pre_commit() == 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


class TestSubcommands:
    def test_parser_has_all_verbs(self):
        p = cli.build_parser()
        # argparse raises SystemExit on unknown; valid ones parse.
        for argv in (
            ["hook", "pre-commit"],
            ["grant", "--minutes", "30"],
            ["revoke", "--all"],
            ["status"],
        ):
            assert p.parse_args(argv) is not None

    def test_grant_then_status_then_revoke(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        assert cli.main(
            ["grant", "--minutes", "60", "--channel", "ide",
             "--label", "t"]
        ) == 0
        assert cli.main(["status"]) == 0
        assert cli.main(["revoke", "--all"]) == 0
        # after revoke-all, hook is blocked again
        assert cli.cmd_hook_pre_commit() == 1

    def test_grant_failure_returns_1(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED", "true"
        )
        # unknown channel -> issue_grant fails -> exit 1
        assert cli.main(
            ["grant", "--channel", "zzz", "--label", "t"]
        ) == 1


class TestEnableDisableCli:
    def test_enable_then_hook_authorizes_with_no_env(
        self, monkeypatch, tmp_path
    ):
        # No JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED — Cursor SCM
        # scenario. enable -> persistent master ON -> grant -> hook 0.
        assert "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED" not in (
            __import__("os").environ
        )
        assert cli.main(["enable", "--label", "cursor"]) == 0
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
        assert oca.master_enabled() is True
        oca.issue_grant(
            channel="ide",
            operator_label="cursor",
            repo_root=tmp_path,
        )
        assert cli.cmd_hook_pre_commit() == 0

    def test_disable_reverts(self, monkeypatch, tmp_path):
        cli.main(["enable", "--label", "x"])
        assert cli.main(["disable"]) == 0
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
        assert oca.master_enabled() is False
        # master off + no token -> hook blocked (legacy path)
        assert cli.cmd_hook_pre_commit() == 1

    def test_status_reports_persistent_enable(
        self, monkeypatch, tmp_path, capsys
    ):
        cli.main(["enable", "--label", "x"])
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "persistent enable" in out
        assert "master_enabled        : True" in out

    def test_parser_has_enable_disable(self):
        p = cli.build_parser()
        assert p.parse_args(["enable", "--label", "z"]) is not None
        assert p.parse_args(["disable"]) is not None
