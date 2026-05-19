"""Layer-3 spine — adaptive rogue-Agent-commit fingerprint +
archive enrichment.

Pins the *adaptive* (not rigid-string) detector and its
forensics-only wiring into the existing commit_authority_archive
BYPASS_SUSPECTED record. The OCA gate is never involved.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import agent_fingerprint as f
from backend.core.ouroboros.governance import (
    commit_authority_archive as ca,
)
from backend.core.ouroboros.governance import auto_committer as ac


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_COMMIT_AUTHORITY_ARCHIVE_PATH",
        str(tmp_path / "a.jsonl"),
    )
    ca.reset_default_archive_for_tests()
    monkeypatch.delenv(
        "JARVIS_AGENT_FINGERPRINT_THRESHOLD", raising=False,
    )
    yield
    ca.reset_default_archive_for_tests()


# --------------------------------------------------------------------------
# Adaptive detector
# --------------------------------------------------------------------------


def test_sanctioned_ov_commit_excluded():
    msg = (
        "feat(x): y\n\nLots of explanatory prose. This commit "
        "introduces things. Additionally it ensures that. "
        "[integrity-verified: deadbeef]\n\n"
        + ac.ov_signature_substring()
    )
    r = f.detect_agent_authored(msg)
    assert r.matched is False
    assert r.reason == "sanctioned_ov_commit_excluded"


def test_integrity_trailer_flexible_regex():
    for trailer in (
        "[integrity-verified: abc123]",
        "[ integrity-verified :  DEADBEEFCAFE ]",
        "[integrity_verified: 9f8e]",
        "[Integrity-Verified: AbC123dd]",
    ):
        r = f.detect_agent_authored("fix: x\n\n" + trailer)
        assert "integrity_trailer" in r.signals, trailer
        assert r.matched is True, trailer


def test_verbose_llm_prose_flagged_paraphrase_robust():
    # No integrity trailer; flagged purely on adaptive multi-signal
    # prose — and a *paraphrase* (no single canned phrase) still
    # trips it.
    msg = (
        "Reworked the subsystem so it behaves correctly. This "
        "change introduces a new coordinator and ensures that "
        "downstream consumers are notified. Additionally, the "
        "retry path was hardened in order to avoid duplicate "
        "work, as well as improving the observability surface "
        "for operators who need to debug it later."
    )
    r = f.detect_agent_authored(msg)
    assert r.matched is True
    assert "integrity_trailer" not in r.signals  # prose alone
    assert any(s.startswith("prose_") for s in r.signals)


def test_terse_conventional_not_flagged():
    for m in (
        "fix(core): correct off-by-one in loop bound",
        "feat(api): add /healthz endpoint",
        "chore: bump deps",
        "docs: typo",
    ):
        r = f.detect_agent_authored(m)
        assert r.matched is False, m


def test_empty_and_non_string_safe():
    for m in ("", "   ", None, 123, [], {}):
        r = f.detect_agent_authored(m)
        assert r.matched is False
    # never raises
    assert f.detect_agent_authored(None).reason == "empty_or_non_string"


def test_threshold_env_tunable():
    base = (
        "This change introduces a thing. Additionally it ensures "
        "behavior in order to improve coverage."
    )
    import os
    os.environ["JARVIS_AGENT_FINGERPRINT_THRESHOLD"] = "0.95"
    try:
        strict = f.detect_agent_authored(base)
        os.environ["JARVIS_AGENT_FINGERPRINT_THRESHOLD"] = "0.2"
        lax = f.detect_agent_authored(base)
    finally:
        os.environ.pop("JARVIS_AGENT_FINGERPRINT_THRESHOLD", None)
    # Same score, different gate → adaptive, not hardcoded.
    assert strict.score == lax.score
    assert lax.matched is True
    assert strict.matched is False


def test_threshold_clamped():
    import os
    os.environ["JARVIS_AGENT_FINGERPRINT_THRESHOLD"] = "9999"
    try:
        assert f.fingerprint_threshold() == 1.0
        os.environ["JARVIS_AGENT_FINGERPRINT_THRESHOLD"] = "-1"
        assert f.fingerprint_threshold() == 0.1
        os.environ["JARVIS_AGENT_FINGERPRINT_THRESHOLD"] = "junk"
        assert f.fingerprint_threshold() == 0.6
    finally:
        os.environ.pop("JARVIS_AGENT_FINGERPRINT_THRESHOLD", None)


def test_to_dict_shape():
    d = f.detect_agent_authored("fix: x\n\n[integrity-verified: ab12]").to_dict()
    assert d["agent_git_write_attempt"] is True
    assert "fingerprint_score" in d and "fingerprint_signals" in d


# --------------------------------------------------------------------------
# Archive enrichment (forensics-only; gate untouched)
# --------------------------------------------------------------------------


def test_bypass_suspected_enriched_for_agent_commit(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    rec = ca.record(kind="bypass_suspected", detail={
        "head": "abc", "reason": "verified-marker absent",
        "commit_message":
            "feat(x): y\n\nThis commit introduces a thing and "
            "ensures behavior. [integrity-verified: 652f38d1a651]",
    })
    assert rec is not None
    assert rec.detail.get("agent_git_write_attempt") is True
    assert "fingerprint_signals" in rec.detail


def test_bypass_suspected_not_flagged_for_terse(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    rec = ca.record(kind="bypass_suspected", detail={
        "head": "abc", "reason": "verified-marker stale",
        "commit_message": "fix(core): off-by-one",
    })
    assert rec.detail.get("agent_git_write_attempt") is False


def test_non_bypass_kind_not_enriched(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    rec = ca.record(kind="grant_issue", detail={
        "commit_message": "[integrity-verified: deadbeef] prose "
                          "this commit introduces ensures",
    })
    assert "agent_git_write_attempt" not in rec.detail


def test_enrichment_absent_when_no_message(monkeypatch):
    monkeypatch.setenv(ca.MASTER_FLAG_ENV_VAR, "true")
    rec = ca.record(kind="bypass_suspected",
                     detail={"head": "abc", "reason": "x"})
    assert "agent_git_write_attempt" not in rec.detail


# --------------------------------------------------------------------------
# AST pin
# --------------------------------------------------------------------------


def test_shipped_invariant_self_validates_green():
    invs = f.register_shipped_invariants()
    assert len(invs) == 1
    src = Path(f.__file__).read_text(encoding="utf-8")
    assert invs[0].validate(ast.parse(src), src) == ()


def test_register_flags_one_seed():
    seen = []

    class _R:
        def register(self, s):
            seen.append(s.name)

    assert f.register_flags(_R()) == 1
    assert "JARVIS_AGENT_FINGERPRINT_THRESHOLD" in seen
