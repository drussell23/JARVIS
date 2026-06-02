"""Slice 64 — context-aware APPLY write-root routing.

Guarded soak (bt-2026-06-02-081453): Claude solved the element-web bug and
proposed a patch for ``src/Markdown.ts``, but APPLY failed with
``No such file or directory: .worktrees/ouroboros__auto__bt-<session>/src/Markdown.ts``.

Root cause: the harness sets ``JARVIS_AUTO_COMMIT_WORKSPACE`` to a JARVIS-repo
worktree (harness.py:2637), and Slice 56's ``ChangeEngine._redirect_target``
blindly rebased EVERY write there. A swe_bench op's target belongs in its
prepared per-problem worktree (an element-web clone, carried in the envelope as
``EVIDENCE_REPO_ROOT_KEY``), not the JARVIS auto-commit workspace — so the
rebased path didn't exist, APPLY failed, the op never reached a clean terminal,
the operation_terminal SSE never fired, and the closed-loop evaluator hung
(0 scored rows).

Fix (additive + gated): ``ChangeRequest`` gains an optional ``write_root``;
``_effective_write_root`` prefers it over ``JARVIS_AUTO_COMMIT_WORKSPACE``; the
orchestrator populates it from the validated envelope repo_root ONLY for
swe_bench_pro ops. ``write_root=None`` is byte-identical for every other op
(contained blast radius).

(Runbook Phase 2 — indexer asyncio.sleep pacing — was NOT built: Oracle indexing
is ``execution_mode=process`` (process-isolated, off the main loop), so the
"indexer locks the GIL" premise is falsified.)
"""
from __future__ import annotations

import dataclasses
import pathlib

from backend.core.ouroboros.governance.change_engine import ChangeEngine, ChangeRequest

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _engine(root):
    # _effective_write_root / _redirect_target only touch self._project_root +
    # env + the arg, so a stub ledger is fine.
    return ChangeEngine(project_root=root, ledger=object())  # type: ignore[arg-type]


def test_change_request_has_write_root_field():
    names = {f.name for f in dataclasses.fields(ChangeRequest)}
    assert "write_root" in names


def test_request_write_root_wins_over_auto_commit_env(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(tmp_path / "autocommit"))
    wt = tmp_path / "swebp_wt" / "element-web"
    assert eng._effective_write_root(wt) == wt


def test_no_request_root_falls_back_to_env_then_project(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    assert eng._effective_write_root(None) == tmp_path           # project_root
    ac = tmp_path / "ac"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ac))
    assert eng._effective_write_root(None) == ac                 # env override


def test_redirect_rebases_onto_request_write_root(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    # auto-commit env present but MUST be ignored when a request write_root is given
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(tmp_path / "autocommit"))
    wt = tmp_path / "swebp_wt" / "element-web"
    target = tmp_path / "src" / "Markdown.ts"   # absolute under project_root
    assert eng._redirect_target(target, wt) == wt / "src" / "Markdown.ts"


def test_redirect_none_preserves_legacy(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    target = tmp_path / "src" / "x.py"
    assert eng._redirect_target(target, None) == target   # no override -> unchanged


def test_orchestrator_gates_write_root_on_swe_bench_source():
    src = (_REPO / "backend/core/ouroboros/governance/orchestrator.py").read_text()
    assert "write_root=" in src, "orchestrator must set ChangeRequest.write_root"
    assert "guard_envelope_repo_root" in src, (
        "write_root must come from the validated envelope repo_root, not raw evidence"
    )
