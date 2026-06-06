"""Slice 101 Phase 7 — adversarial autobiography: O+V audits its OWN commits.

Proves the organism's self-integrity detector: scoped to O+V-signed commits, it
probes each diff against the canonical adversarial corpus and flags a shipped
cage-bypass (CORPUS_ESCAPE). Driven hermetically via the injectable git runners.
Also covers the Sleep-Daemon self-audit step and the non-blocking post-commit
trigger — both master-gated, both never-raise.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.governance.adversarial_autobiography import (
    AutobiographyFinding,
    audit_autobiography,
)
from backend.core.ouroboros.governance.auto_committer import (
    ov_signature_substring,
    _schedule_post_commit_self_audit,
)
from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (
    CORPUS,
    materialize_pattern,
)

_OV_SIG = ov_signature_substring()


def _runner_returning(stdout: str):
    def _runner(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=stdout)
    return _runner


def _ov_commit_log(commit_hash: str = "abc123def") -> str:
    body = "feat(governance): something\n\n" + _OV_SIG
    return f"{commit_hash}\n1700000000\n{body}__END_HEADER__\n__OV_AUTOBIO__\n"


def _non_ov_commit_log(commit_hash: str = "deadbeef") -> str:
    body = "feat: a human commit\n\nCo-Authored-By: Someone"
    return f"{commit_hash}\n1700000000\n{body}__END_HEADER__\n__OV_AUTOBIO__\n"


def _first_materializable_pattern() -> str:
    for entry in CORPUS:
        p = materialize_pattern(entry)
        if isinstance(p, str) and p and not (p.startswith("<") and p.endswith(">")):
            return p
    raise AssertionError("no materializable corpus entry found")


def _diff_embedding(pattern: str) -> str:
    added = "\n".join("+" + line for line in pattern.splitlines())
    return "+++ b/backend/x.py\n" + added + "\n"


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_AUTOBIOGRAPHY_LEDGER_PATH", str(tmp_path / "autobio.jsonl"),
    )


# === THE SAFETY CORE: O+V detects a cage-bypass in its OWN commit ===========

def test_self_audit_detects_corpus_escape_in_own_commit(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    pattern = _first_materializable_pattern()
    report = audit_autobiography(
        force_refresh=True,
        git_log_runner=_runner_returning(_ov_commit_log()),
        git_show_runner=_runner_returning(_diff_embedding(pattern)),
    )
    assert report.finding is AutobiographyFinding.CORPUS_ESCAPE
    assert report.escape_count >= 1
    assert report.commits_audited == 1


def test_self_audit_clean_when_own_commit_has_no_pattern(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    report = audit_autobiography(
        force_refresh=True,
        git_log_runner=_runner_returning(_ov_commit_log()),
        git_show_runner=_runner_returning("+++ b/x.py\n+print('hello world')\n"),
    )
    assert report.finding is AutobiographyFinding.CORPUS_CLEAN
    assert report.escape_count == 0


def test_self_audit_ignores_non_ov_commits(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    pattern = _first_materializable_pattern()
    # Even a human commit carrying the pattern is NOT audited (only O+V's own).
    report = audit_autobiography(
        force_refresh=True,
        git_log_runner=_runner_returning(_non_ov_commit_log()),
        git_show_runner=_runner_returning(_diff_embedding(pattern)),
    )
    assert report.finding is AutobiographyFinding.CORPUS_NO_COMMITS
    assert report.commits_audited == 0


def test_self_audit_disabled_when_master_off(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", raising=False)
    report = audit_autobiography(
        force_refresh=True,
        git_log_runner=_runner_returning(_ov_commit_log()),
        git_show_runner=_runner_returning("+anything\n"),
    )
    assert report.finding is AutobiographyFinding.CORPUS_DISABLED


# === Sleep-Daemon self-audit step ==========================================

def test_sleep_cycle_includes_autobiography_finding(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import sleep_daemon as SD
    monkeypatch.setenv("JARVIS_SLEEP_DAEMON_ENABLED", "1")
    # autobiography master OFF → the step self-gates to corpus_disabled (no git).
    monkeypatch.delenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", raising=False)
    report = SD.run_sleep_cycle_once(idle_seconds=99999.0)
    assert report.master_enabled is True
    assert report.autobiography_finding == "corpus_disabled"
    assert report.autobiography_escape_count == 0


def test_sleep_cycle_runs_real_autobiography_when_enabled(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance import sleep_daemon as SD
    monkeypatch.setenv("JARVIS_SLEEP_DAEMON_ENABLED", "1")
    _enable(monkeypatch, tmp_path)
    report = SD.run_sleep_cycle_once(idle_seconds=99999.0)
    # Real git over this worktree (no O+V-signed commits) → a valid finding,
    # never a raise. Robust to environment.
    assert report.autobiography_finding in (
        "corpus_clean", "corpus_escape", "corpus_no_commits", "corpus_disabled",
    )


# === Non-blocking post-commit trigger ======================================

def test_post_commit_schedule_inert_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", raising=False)
    # No running loop + master off → returns immediately, never raises.
    _schedule_post_commit_self_audit("op-1", "abc123")


def test_post_commit_schedule_never_raises_without_loop(monkeypatch):
    monkeypatch.setenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", "1")
    # Master on but no running loop → get_running_loop raises → swallowed.
    _schedule_post_commit_self_audit("op-2", "def456")
