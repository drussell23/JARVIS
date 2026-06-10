"""Slice 200 — Milestone Sovereignty & Genesis Proposal.

Rather than wait for the M10 miner to non-deterministically surface a pattern,
this slice DETERMINISTICALLY exercises the full code-shipping highway once:
build an honest architecture document → taste-check it → open ONE real review
PR via the orange reviewer → mark a durable sentinel so it never fires again.

Safety invariants (the genesis trigger must be impossible to weaponize into a
PR-spam loop under restart:always):
  * Gated default-FALSE (opens a live PR — operator opt-in).
  * SINGLE-USE: a durable sentinel (.jarvis/genesis_proposal.done) makes it a
    permanent no-op once shipped — survives restart via the bind mount.
  * Sentinel is written ONLY on a confirmed PR (a None/failed creator leaves
    it unset so a later boot can retry — but never double-ships).
  * Fail-soft: any error never blocks the soak.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.genesis_proposal import (
    build_genesis_doc,
    genesis_already_shipped,
    genesis_enabled,
    run_genesis_proposal,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_GENESIS_SENTINEL_PATH", str(tmp_path / "genesis.done"),
    )
    monkeypatch.delenv("JARVIS_GENESIS_PROPOSAL_ENABLED", raising=False)
    yield


class _FakePR:
    def __init__(self, url):
        self.pr_url = url


def _ok_creator(url="https://github.com/drussell23/JARVIS/pull/200"):
    async def _create(op_id, description, files, **kw):
        _create.calls.append((op_id, description, files))
        return _FakePR(url)
    _create.calls = []
    return _create


# ===========================================================================
# A — gating + single-use
# ===========================================================================

def test_genesis_disabled_by_default():
    assert genesis_enabled() is False


def test_genesis_enabled_via_env(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")
    assert genesis_enabled() is True


def test_sentinel_absent_initially():
    assert genesis_already_shipped() is False


def test_disabled_is_noop(monkeypatch):
    creator = _ok_creator()
    result = asyncio.run(run_genesis_proposal(pr_creator=creator))
    assert result is None
    assert creator.calls == []


# ===========================================================================
# B — the honest document
# ===========================================================================

def test_genesis_doc_is_real_and_grounded():
    path, content = build_genesis_doc()
    assert path.endswith(".md")
    assert "docs/architecture/" in path
    # Grounded in the ACTUAL resilience arc — not inflation.
    for token in (
        "transport-hedge", "observability_registry", "race",
        "adaptive", "graduation", "DO-NOT-AUTO-MERGE",
    ):
        assert token.lower() in content.lower(), token
    # No unfounded buzzword claims.
    for banned in ("quantum", "holographic", "tensorflow", "langchain"):
        assert banned.lower() not in content.lower(), banned


# ===========================================================================
# C — the happy path (deterministic ship)
# ===========================================================================

def test_genesis_ships_once_and_writes_sentinel(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")
    creator = _ok_creator()
    result = asyncio.run(run_genesis_proposal(pr_creator=creator))
    assert result is not None
    assert result["pr_url"].endswith("/200")
    assert len(creator.calls) == 1
    # branch derives from op_id → ouroboros/review/genesis-slice-200
    assert creator.calls[0][0] == "genesis-slice-200"
    assert genesis_already_shipped() is True


def test_genesis_does_not_double_ship(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")
    creator = _ok_creator()
    asyncio.run(run_genesis_proposal(pr_creator=creator))
    second = asyncio.run(run_genesis_proposal(pr_creator=creator))
    assert second is None
    assert len(creator.calls) == 1  # sentinel blocked the second


def test_taste_evaluator_runs_before_ship(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")
    seen = {}

    def _taste(files):
        seen["files"] = files
        return {"overall_verdict": "ACCEPTABLE"}

    creator = _ok_creator()
    asyncio.run(run_genesis_proposal(pr_creator=creator, taste_evaluator=_taste))
    assert "files" in seen


# ===========================================================================
# D — fail-soft (never blocks the soak, never double-ships)
# ===========================================================================

def test_creator_returns_none_leaves_sentinel_unset(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")

    async def _fail_create(op_id, description, files, **kw):
        return None

    result = asyncio.run(run_genesis_proposal(pr_creator=_fail_create))
    assert result is None
    assert genesis_already_shipped() is False  # may retry next boot


def test_creator_raising_is_fail_soft(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")

    async def _boom(op_id, description, files, **kw):
        raise RuntimeError("gh exploded")

    result = asyncio.run(run_genesis_proposal(pr_creator=_boom))
    assert result is None
    assert genesis_already_shipped() is False


def test_taste_raising_does_not_block_ship(monkeypatch):
    monkeypatch.setenv("JARVIS_GENESIS_PROPOSAL_ENABLED", "1")

    def _bad_taste(files):
        raise ValueError("taste exploded")

    creator = _ok_creator()
    result = asyncio.run(
        run_genesis_proposal(pr_creator=creator, taste_evaluator=_bad_taste),
    )
    # taste is advisory — a taste failure must not abort the milestone ship
    assert result is not None
    assert len(creator.calls) == 1


# ===========================================================================
# E — wiring + doctrine pins
# ===========================================================================

def test_gls_wires_genesis_boot_trigger():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "run_genesis_proposal" in src or "genesis_proposal" in src


def test_boundary_gate_not_weakened():
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src
    assert "genesis_proposal" not in src
