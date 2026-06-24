"""Tests for G1 — Cross-Repo AST Blast-Radius Context provider.

Spec: docs/superpowers/specs/2026-06-23-sovereign-cross-repo-mutator.md §3, §8.

The module under test reads the Oracle's cross-repo blast radius and renders
the downstream (cross-repo) dependents into the generation context window so
the model is forced to recognize what it would break. It is a pure
read+render CONTEXT PROVIDER with ZERO write/policy authority.

These tests use a FAKE oracle + FAKE registry — no real Oracle build.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

import pytest

from backend.core.ouroboros.governance.multi_repo import cross_repo_blast_context as mod
from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (
    BlastRadiusContext,
    DependentRef,
    enabled,
    render_blast_block,
    trace_cross_repo_blast,
)


# ---------------------------------------------------------------------------
# Fakes — mirror the real Oracle/Registry surface the module consumes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeNodeID:
    """Mirror of oracle.NodeID (repo / file_path / name / node_type)."""

    repo: str
    file_path: str
    name: str
    node_type: str = "function"


@dataclass
class FakeBlastRadius:
    """Mirror of oracle.BlastRadius — directly (depth-1) + transitively (deeper)."""

    source_node: FakeNodeID
    directly_affected: Set[FakeNodeID]
    transitively_affected: Set[FakeNodeID]


class FakeOracle:
    def __init__(self, report: FakeBlastRadius, *, raises: bool = False) -> None:
        self._report = report
        self._raises = raises
        self.last_max_depth: Optional[int] = None

    def compute_blast_radius(self, node_id, max_depth=None):  # noqa: ANN001
        if self._raises:
            raise RuntimeError("oracle boom")
        self.last_max_depth = max_depth
        return self._report


class FakeRegistry:
    def __init__(self, files: Dict[tuple, str], *, raises: bool = False) -> None:
        # files keyed by (repo, path) -> source text
        self._files = files
        self._raises = raises

    async def read_file(self, repo: str, path: str) -> Optional[str]:
        if self._raises:
            raise RuntimeError("registry boom")
        return self._files.get((repo, path))


# ---------------------------------------------------------------------------
# Fixtures: a reactor target with jarvis + prime cross-repo dependents
# and a same-repo (reactor) dependent that MUST be excluded.
# ---------------------------------------------------------------------------


TARGET = FakeNodeID("reactor", "telemetry/metric.py", "MetricStruct", "class")

JARVIS_DEP = FakeNodeID("jarvis", "backend/consumer.py", "caller_fn", "function")
PRIME_DEP = FakeNodeID("prime", "mind/router.py", "route_metric", "function")
REACTOR_SAME = FakeNodeID("reactor", "telemetry/sink.py", "local_sink", "function")


def _registry_with_sources() -> FakeRegistry:
    return FakeRegistry(
        {
            ("jarvis", "backend/consumer.py"): (
                "def caller_fn():\n"
                "    m = MetricStruct()\n"
                "    return m.emit()\n"
            ),
            ("prime", "mind/router.py"): (
                "def route_metric(m):\n"
                "    return MetricStruct().wrap(m)\n"
            ),
            ("reactor", "telemetry/sink.py"): (
                "def local_sink():\n    return 1\n"
            ),
        }
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# trace_cross_repo_blast — cross-repo collection only
# ---------------------------------------------------------------------------


def test_collects_only_cross_repo_dependents():
    report = FakeBlastRadius(
        source_node=TARGET,
        directly_affected={JARVIS_DEP},
        transitively_affected={PRIME_DEP, REACTOR_SAME},
    )
    oracle = FakeOracle(report)
    registry = _registry_with_sources()

    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=oracle, registry=registry
        )
    )

    repos = {d.repo for d in ctx.dependents}
    assert repos == {"jarvis", "prime"}  # reactor (same repo) excluded
    assert ctx.total_dependents == 2
    assert ctx.target_repo == "reactor"
    assert ctx.target_symbol == "MetricStruct"
    # same-repo dependent never surfaces
    assert all(d.repo != "reactor" for d in ctx.dependents)


def test_dense_source_excerpt_read_via_registry():
    report = FakeBlastRadius(
        source_node=TARGET,
        directly_affected={JARVIS_DEP},
        transitively_affected=set(),
    )
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET,
            oracle=FakeOracle(report),
            registry=_registry_with_sources(),
        )
    )
    assert len(ctx.dependents) == 1
    ref = ctx.dependents[0]
    assert isinstance(ref, DependentRef)
    assert ref.repo == "jarvis"
    assert ref.file == "backend/consumer.py"
    assert ref.symbol == "caller_fn"
    # excerpt scoped to the enclosing symbol, not whole-file noise
    assert "caller_fn" in ref.relevance
    assert "MetricStruct" in ref.relevance


def test_default_depth_from_env(monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_BLAST_DEPTH", "7")
    report = FakeBlastRadius(TARGET, {JARVIS_DEP}, set())
    oracle = FakeOracle(report)
    _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=oracle, registry=_registry_with_sources()
        )
    )
    assert oracle.last_max_depth == 7


def test_explicit_max_depth_overrides_env(monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_BLAST_DEPTH", "7")
    report = FakeBlastRadius(TARGET, {JARVIS_DEP}, set())
    oracle = FakeOracle(report)
    _run(
        trace_cross_repo_blast(
            target_node_id=TARGET,
            oracle=oracle,
            registry=_registry_with_sources(),
            max_depth=2,
        )
    )
    assert oracle.last_max_depth == 2


# ---------------------------------------------------------------------------
# render_blast_block — header + listing
# ---------------------------------------------------------------------------


def test_render_block_header_and_dependents():
    report = FakeBlastRadius(
        TARGET, {JARVIS_DEP}, {PRIME_DEP}
    )
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET,
            oracle=FakeOracle(report),
            registry=_registry_with_sources(),
        )
    )
    block = render_blast_block(ctx)
    assert "## CROSS-REPO BLAST RADIUS" in block
    assert "MetricStruct" in block
    assert "reactor" in block
    assert "Nerves" in block  # reactor -> Nerves label
    # both downstream dependents listed with their repo tag + file
    assert "[jarvis] backend/consumer.py::caller_fn" in block
    assert "[prime] mind/router.py::route_metric" in block
    # dense excerpt present
    assert "m = MetricStruct()" in block


def test_render_block_empty_when_no_dependents():
    ctx = BlastRadiusContext(
        target_repo="reactor",
        target_symbol="MetricStruct",
        dependents=(),
        rendered_prompt_block="",
        truncated=False,
        total_dependents=0,
    )
    assert render_blast_block(ctx) == ""


def test_prime_target_labels_mind():
    prime_target = FakeNodeID("prime", "mind/core.py", "Brain", "class")
    report = FakeBlastRadius(
        prime_target,
        {FakeNodeID("jarvis", "body/x.py", "use_brain", "function")},
        set(),
    )
    registry = FakeRegistry(
        {("jarvis", "body/x.py"): "def use_brain():\n    return Brain()\n"}
    )
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=prime_target, oracle=FakeOracle(report), registry=registry
        )
    )
    block = render_blast_block(ctx)
    assert "Mind" in block


# ---------------------------------------------------------------------------
# Token-budget truncation — NEVER silent
# ---------------------------------------------------------------------------


def test_token_budget_truncation_sets_flag_and_marker(monkeypatch, caplog):
    # Build many dependents so the rendered block exceeds a tiny budget.
    deps = {
        FakeNodeID("jarvis", f"backend/c{i}.py", f"fn_{i}", "function")
        for i in range(20)
    }
    files = {
        ("jarvis", f"backend/c{i}.py"): (
            f"def fn_{i}():\n"
            + "    # padding line that makes the excerpt large\n" * 10
            + "    return MetricStruct()\n"
        )
        for i in range(20)
    }
    report = FakeBlastRadius(TARGET, deps, set())
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=FakeOracle(report), registry=FakeRegistry(files)
        )
    )
    with caplog.at_level("WARNING"):
        block = render_blast_block(ctx, token_budget=300)

    # not all 20 fit -> truncated marker present and NOT silent
    assert "further dependents elided" in block
    m = re.search(r"(\d+) further dependents elided", block)
    assert m is not None
    elided = int(m.group(1))
    assert elided > 0
    # the truncation count was logged (never silent)
    assert any("elided" in r.message or "truncat" in r.message.lower()
               for r in caplog.records)


def test_truncation_keeps_nearest_first():
    # depth-1 (directly_affected) dependents must survive truncation before
    # depth-3 (transitively_affected) ones.
    near = {FakeNodeID("jarvis", "near.py", "near_fn", "function")}
    far = {
        FakeNodeID("jarvis", f"far{i}.py", f"far_{i}", "function")
        for i in range(15)
    }
    files = {("jarvis", "near.py"): "def near_fn():\n    return MetricStruct()\n"}
    for i in range(15):
        files[("jarvis", f"far{i}.py")] = (
            f"def far_{i}():\n" + "    # pad\n" * 8 + "    return 1\n"
        )
    report = FakeBlastRadius(TARGET, near, far)
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=FakeOracle(report), registry=FakeRegistry(files)
        )
    )
    # nearest dependent is first in the ordered tuple
    assert ctx.dependents[0].symbol == "near_fn"
    block = render_blast_block(ctx, token_budget=200)
    assert "near_fn" in block  # nearest survived truncation


# ---------------------------------------------------------------------------
# Fail-soft -> empty context (caller escalates / fail-CLOSED)
# ---------------------------------------------------------------------------


def test_oracle_error_returns_empty_context():
    oracle = FakeOracle(FakeBlastRadius(TARGET, set(), set()), raises=True)
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=oracle, registry=_registry_with_sources()
        )
    )
    assert ctx.dependents == ()
    assert ctx.total_dependents == 0
    assert render_blast_block(ctx) == ""


def test_registry_error_returns_empty_context():
    report = FakeBlastRadius(TARGET, {JARVIS_DEP}, set())
    registry = FakeRegistry({}, raises=True)
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=FakeOracle(report), registry=registry
        )
    )
    # a registry that raises on every read yields no usable dependents
    assert ctx.dependents == ()
    assert render_blast_block(ctx) == ""


def test_missing_source_file_skipped_not_crashing():
    # registry returns None for the dependent's file -> skip it, don't crash.
    report = FakeBlastRadius(TARGET, {JARVIS_DEP}, set())
    registry = FakeRegistry({})  # no sources at all -> read_file returns None
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=FakeOracle(report), registry=registry
        )
    )
    assert ctx.dependents == ()


# ---------------------------------------------------------------------------
# OFF -> empty block (no-op)
# ---------------------------------------------------------------------------


def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_CROSS_REPO_BLAST_CONTEXT_ENABLED", raising=False)
    assert enabled() is True


def test_disabled_returns_empty_block(monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_BLAST_CONTEXT_ENABLED", "false")
    report = FakeBlastRadius(TARGET, {JARVIS_DEP}, set())
    ctx = _run(
        trace_cross_repo_blast(
            target_node_id=TARGET, oracle=FakeOracle(report), registry=_registry_with_sources()
        )
    )
    # off -> empty context, render is a no-op empty block
    assert ctx.dependents == ()
    assert render_blast_block(ctx) == ""


# ---------------------------------------------------------------------------
# Authority-free — no write/policy imports (source grep)
# ---------------------------------------------------------------------------


def test_module_has_no_write_or_policy_imports():
    src = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = (
        "orchestrator",
        "change_engine",
        "policy",
        "risk_tier",
        "auto_committer",
        "saga_apply",
    )
    # Scan only actual import statements -- the module may *mention* these
    # words in prose (it documents that it has zero such authority), but must
    # never IMPORT them. Authority is carried by imports, not docstrings.
    import_lines = [
        ln.strip()
        for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    for token in forbidden:
        for ln in import_lines:
            assert token not in ln, (
                f"authority-bearing import found: {token!r} in {ln!r}"
            )
