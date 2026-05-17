"""Phase B spine — SWE path↔advisor preflight (B1) + envelope fail-closed (B2).

Mathematically proves:
  * B2 helper taxonomy: NO_PROMISE / RESOLVED / REJECTED + feature-off
    byte-identity (the byte-identity-preserving distinction that stops
    B2 shattering when the worktree-aware advisor flag is OFF)
  * guard_envelope_repo_root: path on RESOLVED, None on NO_PROMISE,
    raises EnvelopeRepoRootRejected on REJECTED (no shared-tree fallback)
  * B1 assess_swe_path_readiness: default-FALSE byte-identity,
    anchor-pass→PROCEED(under_project_root True), anchor-fail→REFUSE,
    feature-off→PROCEED_FEATURE_OFF, never-raises
  * AST pins: helper composes resolve_envelope_repo_root (no parallel
    prefix math); both advisor sites swapped to guard + carry an
    except EnvelopeRepoRootRejected POSTMORTEM handler; B1 never-raises;
    single B1 seam in harness.py

pytest.ini asyncio_mode=auto — async tests need no decorator.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import operation_advisor as oa
from backend.core.ouroboros.governance.operation_advisor import (
    EnvelopeRepoRootRejected,
    RepoRootPromiseStatus,
    envelope_repo_root_status,
    guard_envelope_repo_root,
)
from backend.core.ouroboros.battle_test import swe_path_preflight as sp
from backend.core.ouroboros.battle_test.swe_path_preflight import (
    SwePathVerdict,
    assess_swe_path_readiness,
)

_WT_AWARE = "JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED"
_B1_MASTER = "JARVIS_BATTLE_PREFLIGHT_SWE_PATH_ENABLED"
_GOV = "backend/core/ouroboros/governance"


def _ev(p) -> str:
    return json.dumps({"repo_root": str(p)})


@pytest.fixture
def anchor(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def outside(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("outside_anchor")
    return Path(d)


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setenv(_WT_AWARE, "true")


# ── B2 helper taxonomy ─────────────────────────────────────────────────
def test_no_promise_empty(anchor):
    assert envelope_repo_root_status("", project_root=anchor) == (
        RepoRootPromiseStatus.NO_PROMISE, None, ""
    )


def test_no_promise_no_key(anchor):
    s, p, raw = envelope_repo_root_status(
        json.dumps({"other": "x"}), project_root=anchor
    )
    assert s is RepoRootPromiseStatus.NO_PROMISE and p is None and raw == ""


def test_no_promise_malformed_json(anchor):
    s, _, _ = envelope_repo_root_status("{not json", project_root=anchor)
    assert s is RepoRootPromiseStatus.NO_PROMISE


def test_resolved_under_anchor(anchor, feature_on):
    sub = anchor / "wt"
    sub.mkdir()
    s, p, raw = envelope_repo_root_status(_ev(sub), project_root=anchor)
    assert s is RepoRootPromiseStatus.RESOLVED
    assert p == sub.resolve() and raw == str(sub)


def test_rejected_outside_anchor(anchor, outside, feature_on):
    s, p, raw = envelope_repo_root_status(_ev(outside), project_root=anchor)
    assert s is RepoRootPromiseStatus.REJECTED
    assert p is None and raw == str(outside)


def test_feature_off_byte_identity(anchor, outside, monkeypatch):
    # Feature OFF: a promised outside path must NOT be REJECTED — it
    # collapses to NO_PROMISE so byte-identical legacy fallback holds.
    monkeypatch.setenv(_WT_AWARE, "false")
    s, _, _ = envelope_repo_root_status(_ev(outside), project_root=anchor)
    assert s is RepoRootPromiseStatus.NO_PROMISE


# ── B2 guard ───────────────────────────────────────────────────────────
def test_guard_returns_path_resolved(anchor, feature_on):
    sub = anchor / "wt"
    sub.mkdir()
    assert guard_envelope_repo_root(_ev(sub), project_root=anchor) == (
        sub.resolve()
    )


def test_guard_none_on_no_promise(anchor):
    assert guard_envelope_repo_root("", project_root=anchor) is None


def test_guard_raises_on_rejected(anchor, outside, feature_on):
    with pytest.raises(EnvelopeRepoRootRejected) as ei:
        guard_envelope_repo_root(_ev(outside), project_root=anchor)
    assert str(outside) in str(ei.value)
    assert ei.value.raw_repo_root == str(outside)


# ── B1 assess_swe_path_readiness ───────────────────────────────────────
def _rpt(d) -> Path:
    return Path(d) / "swe_path_readiness.json"


async def test_b1_default_false_byte_identity(tmp_path, anchor, monkeypatch):
    monkeypatch.delenv(_B1_MASTER, raising=False)
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.PROCEED_DISABLED
    assert not _rpt(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []


async def test_b1_explicit_false(tmp_path, anchor, monkeypatch):
    monkeypatch.setenv(_B1_MASTER, "false")
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.PROCEED_DISABLED


async def test_b1_anchor_pass(tmp_path, anchor, monkeypatch, feature_on):
    monkeypatch.setenv(_B1_MASTER, "true")
    monkeypatch.setattr(sp, "worktree_base_path", lambda: anchor / "wt")
    monkeypatch.setattr(sp, "repo_cache_path", lambda: anchor / "rc")
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.PROCEED
    rpt = json.loads(_rpt(tmp_path).read_text())
    assert rpt["under_project_root"] is True
    assert rpt["verdict"] == "proceed"


async def test_b1_anchor_fail_refuses(
    tmp_path, anchor, outside, monkeypatch, feature_on
):
    monkeypatch.setenv(_B1_MASTER, "true")
    monkeypatch.setattr(sp, "worktree_base_path", lambda: outside / "wt")
    monkeypatch.setattr(sp, "repo_cache_path", lambda: outside / "rc")
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.REFUSE_OUTSIDE_ANCHOR
    rpt = json.loads(_rpt(tmp_path).read_text())
    assert rpt["under_project_root"] is False


async def test_b1_feature_off_proceeds_warn(
    tmp_path, anchor, outside, monkeypatch
):
    monkeypatch.setenv(_B1_MASTER, "true")
    monkeypatch.setenv(_WT_AWARE, "false")
    monkeypatch.setattr(sp, "worktree_base_path", lambda: outside / "wt")
    monkeypatch.setattr(sp, "repo_cache_path", lambda: outside / "rc")
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.PROCEED_FEATURE_OFF


async def test_b1_never_raises(tmp_path, anchor, monkeypatch, feature_on):
    monkeypatch.setenv(_B1_MASTER, "true")

    def _boom():
        raise RuntimeError("worktree path explode")

    monkeypatch.setattr(sp, "worktree_base_path", _boom)
    v = await assess_swe_path_readiness(str(tmp_path), project_root=anchor)
    assert v is SwePathVerdict.PROCEED_FEATURE_OFF  # degraded, not raised


# ── AST pins ───────────────────────────────────────────────────────────
def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            n.name == name
        ):
            return n
    raise AssertionError(f"function {name} not found")


def test_ast_pin_helper_composes_resolver_no_parallel_math():
    tree = ast.parse(Path(oa.__file__).read_text())
    fn = _fn(tree, "envelope_repo_root_status")
    calls = {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "resolve_envelope_repo_root" in calls, (
        "helper MUST compose the canonical resolver (single prefix-math "
        "owner) — no parallel allowlist logic"
    )
    # no reimplemented prefix math (_is_under / relative_to) in the helpers
    for fname in ("envelope_repo_root_status", "guard_envelope_repo_root"):
        body = ast.unparse(_fn(tree, fname))
        assert "_is_under" not in body and "relative_to" not in body


@pytest.mark.parametrize(
    "rel",
    [
        f"{_GOV}/orchestrator.py",
        f"{_GOV}/phase_runners/classify_runner.py",
    ],
)
def test_ast_pin_advisor_sites_swapped_and_failclosed(rel):
    root = Path(__file__).resolve().parents[2]
    src = (root / rel).read_text()
    tree = ast.parse(src)
    call_names = [
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "guard_envelope_repo_root" in call_names, (
        f"{rel}: must call the fail-closed guard"
    )
    assert "resolve_envelope_repo_root" not in call_names, (
        f"{rel}: bare resolver call must be GONE (silent-fallback path)"
    )
    handlers = [
        h
        for n in ast.walk(tree)
        if isinstance(n, ast.Try)
        for h in n.handlers
        if h.type is not None
        and (
            (isinstance(h.type, ast.Name)
             and h.type.id == "EnvelopeRepoRootRejected")
        )
    ]
    assert handlers, f"{rel}: must carry an except EnvelopeRepoRootRejected"
    # the handler body must drive a POSTMORTEM terminal (no fallback)
    assert any(
        "POSTMORTEM" in ast.unparse(h) for h in handlers
    ), f"{rel}: fail-closed handler must advance POSTMORTEM"


def test_ast_pin_b1_never_raises():
    tree = ast.parse(Path(sp.__file__).read_text())
    fn = _fn(tree, "assess_swe_path_readiness")
    assert [n for n in ast.walk(fn) if isinstance(n, ast.Raise)] == [], (
        "assess_swe_path_readiness must never raise"
    )


def test_ast_pin_b1_single_seam():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse(
        (root / "backend/core/ouroboros/battle_test/harness.py").read_text()
    )
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "assess_swe_path_readiness"
    ]
    assert len(calls) == 1, f"expected ONE B1 seam, found {len(calls)}"
