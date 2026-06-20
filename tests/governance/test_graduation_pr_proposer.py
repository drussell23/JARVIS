"""Sovereign GitOps graduation PR — source-of-truth rewriter tests (2026-06-20)."""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.governance.graduation.graduation_pr_proposer import (
    ProposalResult,
    flip_default_to_true,
    graduation_pr_enabled,
    propose_graduation_pr,
)


# ── the pure rewriter (safety-critical) ─────────────────────────────────────

def test_flip_false_literal():
    src = 'x = os.environ.get("JARVIS_CURIOSITY_ENGINE_ENABLED", "false")\n'
    r = flip_default_to_true(src, "JARVIS_CURIOSITY_ENGINE_ENABLED")
    assert r.changed is True
    assert '"true"' in r.new_text
    assert '"false"' not in r.new_text
    ast.parse(r.new_text)  # still valid


def test_flip_empty_default():
    src = 'raw = os.environ.get("JARVIS_X", "").strip().lower()\n'
    r = flip_default_to_true(src, "JARVIS_X")
    assert r.changed is True
    assert 'os.environ.get("JARVIS_X", "true")' in r.new_text


def test_flip_single_quotes_and_zero():
    src = "v = os.environ.get('JARVIS_Y', '0')\n"
    r = flip_default_to_true(src, "JARVIS_Y")
    assert r.changed is True
    assert "os.environ.get('JARVIS_Y', 'true')" in r.new_text


def test_no_match_abstains():
    src = 'os.environ.get("JARVIS_OTHER", "false")\n'
    r = flip_default_to_true(src, "JARVIS_MISSING")
    assert r.changed is False
    assert r.matches == 0
    assert r.detail == "no_default_literal_found"


def test_ambiguous_multiple_matches_abstains():
    src = (
        'a = os.environ.get("JARVIS_DUP", "false")\n'
        'b = os.environ.get("JARVIS_DUP", "false")\n'
    )
    r = flip_default_to_true(src, "JARVIS_DUP")
    assert r.changed is False
    assert r.matches == 2
    assert "ambiguous" in r.detail
    assert r.new_text == src  # untouched


def test_already_truthy_abstains():
    src = 'os.environ.get("JARVIS_ON", "true")\n'
    r = flip_default_to_true(src, "JARVIS_ON")
    assert r.changed is False
    assert "already_truthy" in r.detail


def test_only_targets_the_named_flag():
    src = (
        'a = os.environ.get("JARVIS_A", "false")\n'
        'b = os.environ.get("JARVIS_B", "false")\n'
    )
    r = flip_default_to_true(src, "JARVIS_A")
    assert r.changed is True
    assert 'os.environ.get("JARVIS_A", "true")' in r.new_text
    assert 'os.environ.get("JARVIS_B", "false")' in r.new_text  # untouched


def test_rewriter_never_raises_on_garbage():
    assert flip_default_to_true(None, "JARVIS_X").changed is False  # type: ignore
    assert flip_default_to_true("x=1", "").changed is False


# ── gate + orchestration ────────────────────────────────────────────────────

def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", raising=False)
    assert graduation_pr_enabled() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", "true")
    assert graduation_pr_enabled() is True


class _FakeSpec:
    source_file = "pkg/mod.py"


class _FakeRegistry:
    def get_spec(self, flag):
        return _FakeSpec()


class _FakeReviewer:
    def __init__(self):
        self.calls = []

    async def create_review_pr(self, **kwargs):
        self.calls.append(kwargs)

        class _PR:
            url = "https://github.com/x/y/pull/42"
        return _PR()


def _clean_ev():
    return {
        "ttft_n": 3, "ttft_mean_ms": 800.0, "ttft_max_ms": 1000.0,
        "ttft_degraded": False, "ast_corruption_signals": 0,
        "ast_corrupted": False, "recovered": True,
        "session_outcome": "complete",
    }


async def test_proposer_opens_pr_when_clean(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", "true")
    (tmp_path / "pkg").mkdir()
    src = 'flag = os.environ.get("JARVIS_CURIOSITY_ENGINE_ENABLED", "false")\n'
    (tmp_path / "pkg" / "mod.py").write_text(src)
    reviewer = _FakeReviewer()
    res = await propose_graduation_pr(
        "JARVIS_CURIOSITY_ENGINE_ENABLED",
        soak_evidence=[_clean_ev(), _clean_ev(), _clean_ev()],
        session_ids=["bt-1", "bt-2", "bt-3"],
        required_clean=3,
        ttft_ceiling_ms=30000.0,
        repo_root=str(tmp_path),
        registry=_FakeRegistry(),
        reviewer=reviewer,
    )
    assert res.proposed is True
    assert res.pr_url == "https://github.com/x/y/pull/42"
    assert len(reviewer.calls) == 1
    call = reviewer.calls[0]
    assert call["description"] == "[SOVEREIGN GRADUATION] Activated JARVIS_CURIOSITY_ENGINE_ENABLED"
    # the rewritten file content carries the flip
    assert any('"true"' in c for (_, c) in call["files"])


async def test_proposer_gate_off_no_pr(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", raising=False)
    reviewer = _FakeReviewer()
    res = await propose_graduation_pr(
        "JARVIS_X", soak_evidence=[_clean_ev()] * 3, session_ids=["a"],
        required_clean=3, ttft_ceiling_ms=30000.0, repo_root=str(tmp_path),
        registry=_FakeRegistry(), reviewer=reviewer,
    )
    assert res.proposed is False
    assert res.detail == "gate_disabled"
    assert reviewer.calls == []


async def test_proposer_vetoes_when_evidence_dirty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", "true")
    reviewer = _FakeReviewer()
    dirty = _clean_ev()
    dirty["ttft_degraded"] = True
    res = await propose_graduation_pr(
        "JARVIS_X", soak_evidence=[_clean_ev(), _clean_ev(), dirty],
        session_ids=["a"], required_clean=3, ttft_ceiling_ms=30000.0,
        repo_root=str(tmp_path), registry=_FakeRegistry(), reviewer=reviewer,
    )
    assert res.proposed is False
    assert res.detail == "evidence_did_not_clear_veto"
    assert reviewer.calls == []
