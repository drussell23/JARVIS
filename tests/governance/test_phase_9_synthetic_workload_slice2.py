"""Phase 9 Slice 2 — battle-test harness wiring regression spine.

Pins the wire-up that connects the Slice 1 factory to the canonical
``UnifiedIntakeRouter`` pipeline through the harness. Single-
pipeline guardrail enforced: all synthetic envelopes route via
``IntakeLayerService.ingest_envelope`` (which delegates to the
canonical router), NEVER via a parallel ingestion path.

Verifies (16 tests):

  * ``HarnessConfig`` carries ``seed_intents`` field, default 0
  * ``IntakeLayerService.ingest_envelope`` exists + delegates +
    NEVER raises
  * Battle-test CLI registers ``--seed-intents`` argument
  * Harness method ``_inject_phase_9_synthetic_workload`` exists
    and is async
  * Behavioral pins (mocked):
    - seed_intents=0 → injection skipped entirely (zero behavior
      change for non-cadence runs)
    - seed_intents>0 AND non-headless → injection skipped (real
      workload IS the workload)
    - seed_intents>0 AND headless AND intake_service=None →
      skipped, logged warning
    - seed_intents>0 AND headless AND intake_service ready →
      injection routes through ingest_envelope N times
    - one envelope ingest raises → others still attempted
  * AST pin: harness method calls ``ingest_envelope`` (single
    pipeline) AND ``build_synthetic_envelopes`` (canonical
    factory); no parallel router access (``self._intake_service.
    _router`` direct).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# HarnessConfig: seed_intents field
# ---------------------------------------------------------------------------


def test_harness_config_has_seed_intents_field():
    from backend.core.ouroboros.battle_test.harness import (
        HarnessConfig,
    )
    config = HarnessConfig()
    assert hasattr(config, "seed_intents")
    assert config.seed_intents == 0  # default = no injection


def test_harness_config_seed_intents_settable():
    from backend.core.ouroboros.battle_test.harness import (
        HarnessConfig,
    )
    config = HarnessConfig(seed_intents=3)
    assert config.seed_intents == 3


# ---------------------------------------------------------------------------
# IntakeLayerService: public ingest_envelope delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_service_ingest_envelope_returns_false_when_no_router():
    """No router booted → returns False; NEVER raises."""
    from backend.core.ouroboros.governance.intake.intake_layer_service import (  # noqa: E501
        IntakeLayerService,
    )
    svc = IntakeLayerService.__new__(IntakeLayerService)
    svc._router = None
    result = await svc.ingest_envelope(MagicMock())
    assert result is False


@pytest.mark.asyncio
async def test_intake_service_ingest_envelope_delegates_to_router():
    from backend.core.ouroboros.governance.intake.intake_layer_service import (  # noqa: E501
        IntakeLayerService,
    )
    svc = IntakeLayerService.__new__(IntakeLayerService)
    fake_router = MagicMock()
    fake_router.ingest = AsyncMock()
    svc._router = fake_router
    envelope = MagicMock()
    result = await svc.ingest_envelope(envelope)
    assert result is True
    fake_router.ingest.assert_awaited_once_with(envelope)


@pytest.mark.asyncio
async def test_intake_service_ingest_envelope_swallows_exceptions():
    """Defensive: router.ingest raises → returns False, NEVER
    raises into caller."""
    from backend.core.ouroboros.governance.intake.intake_layer_service import (  # noqa: E501
        IntakeLayerService,
    )
    svc = IntakeLayerService.__new__(IntakeLayerService)
    fake_router = MagicMock()
    fake_router.ingest = AsyncMock(
        side_effect=RuntimeError("router broke"),
    )
    svc._router = fake_router
    result = await svc.ingest_envelope(MagicMock())
    assert result is False  # swallowed, not raised


# ---------------------------------------------------------------------------
# Battle-test CLI: --seed-intents argument
# ---------------------------------------------------------------------------


def test_battle_test_cli_registers_seed_intents():
    """The CLI MUST register --seed-intents N argument with
    default 0. AST pin for argparse.add_argument call."""
    target = _repo_root() / "scripts/ouroboros_battle_test.py"
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "add_argument"
            ):
                if node.args:
                    arg0 = node.args[0]
                    if (
                        isinstance(arg0, ast.Constant)
                        and arg0.value == "--seed-intents"
                    ):
                        found = True
                        break
    assert found, (
        "--seed-intents argument missing from "
        "ouroboros_battle_test.py — Slice 2 regression"
    )


def test_battle_test_cli_passes_seed_intents_to_config():
    """The CLI MUST plumb args.seed_intents into HarnessConfig.
    AST pin: HarnessConfig(...) call carries seed_intents kwarg."""
    target = _repo_root() / "scripts/ouroboros_battle_test.py"
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "HarnessConfig"
            ):
                for kw in node.keywords:
                    if kw.arg == "seed_intents":
                        found = True
                        break
    assert found, (
        "HarnessConfig(...) missing seed_intents= keyword — "
        "Slice 2 wiring incomplete"
    )


# ---------------------------------------------------------------------------
# Harness method: _inject_phase_9_synthetic_workload
# ---------------------------------------------------------------------------


def test_inject_method_exists_and_is_async():
    import inspect
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness,
    )
    assert hasattr(
        BattleTestHarness, "_inject_phase_9_synthetic_workload",
    )
    method = getattr(
        BattleTestHarness, "_inject_phase_9_synthetic_workload",
    )
    assert inspect.iscoroutinefunction(method), (
        "_inject_phase_9_synthetic_workload must be async"
    )


@pytest.mark.asyncio
async def test_inject_skipped_when_seed_intents_zero():
    """Default config (seed_intents=0) → injection is a no-op.
    Zero behavior change for non-cadence runs."""
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._config = HarnessConfig(seed_intents=0)
    harness._intake_service = MagicMock()
    harness._intake_service.ingest_envelope = AsyncMock()
    await harness._inject_phase_9_synthetic_workload()
    harness._intake_service.ingest_envelope.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_skipped_when_not_headless():
    """seed_intents > 0 but interactive (headless=False) →
    injection skipped. Real workload IS the workload."""
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._config = HarnessConfig(
        seed_intents=3, headless=False,
    )
    harness._intake_service = MagicMock()
    harness._intake_service.ingest_envelope = AsyncMock()
    await harness._inject_phase_9_synthetic_workload()
    harness._intake_service.ingest_envelope.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_skipped_when_intake_service_missing():
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._config = HarnessConfig(
        seed_intents=3, headless=True,
    )
    harness._intake_service = None
    # Should not raise
    await harness._inject_phase_9_synthetic_workload()


@pytest.mark.asyncio
async def test_inject_routes_through_ingest_envelope():
    """seed_intents > 0 + headless + intake ready → factory builds
    N envelopes; harness routes each through
    IntakeLayerService.ingest_envelope (single-pipeline guardrail
    behaviorally enforced)."""
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._config = HarnessConfig(
        seed_intents=3, headless=True,
    )
    fake_intake = MagicMock()
    fake_intake.ingest_envelope = AsyncMock(return_value=True)
    harness._intake_service = fake_intake
    await harness._inject_phase_9_synthetic_workload()
    # Factory built 3, all routed through ingest_envelope.
    assert fake_intake.ingest_envelope.await_count == 3


@pytest.mark.asyncio
async def test_inject_continues_after_one_envelope_raises():
    """One envelope's ingest_envelope raising MUST NOT prevent
    other envelopes from being routed. Defense-in-depth."""
    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness, HarnessConfig,
    )
    harness = BattleTestHarness.__new__(BattleTestHarness)
    harness._config = HarnessConfig(
        seed_intents=3, headless=True,
    )
    fake_intake = MagicMock()
    fake_intake.ingest_envelope = AsyncMock(
        side_effect=[
            True,
            RuntimeError("simulated transient"),
            True,
        ],
    )
    harness._intake_service = fake_intake
    # Should not raise.
    await harness._inject_phase_9_synthetic_workload()
    # All 3 attempted.
    assert fake_intake.ingest_envelope.await_count == 3


# ---------------------------------------------------------------------------
# AST: single-pipeline guardrail enforced structurally
# ---------------------------------------------------------------------------


def test_inject_method_calls_ingest_envelope():
    """AST pin: ``_inject_phase_9_synthetic_workload`` MUST call
    ``ingest_envelope`` (the canonical IntakeLayerService
    delegation). Single-pipeline guardrail."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "_inject_phase_9_synthetic_workload":
                target_func = node
                break
    assert target_func is not None, (
        "_inject_phase_9_synthetic_workload not found"
    )
    found_ingest_call = False
    for node in ast.walk(target_func):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "ingest_envelope"
            ):
                found_ingest_call = True
                break
    assert found_ingest_call, (
        "_inject_phase_9_synthetic_workload MUST call "
        "ingest_envelope (single-pipeline guardrail)"
    )


def test_inject_method_uses_canonical_factory():
    """AST pin: ``_inject_phase_9_synthetic_workload`` MUST
    import from the canonical
    ``phase_9_synthetic_workload`` module (no parallel
    factory)."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "_inject_phase_9_synthetic_workload":
                target_func = node
                break
    assert target_func is not None
    has_canonical_import = False
    for node in ast.walk(target_func):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "phase_9_synthetic_workload" in module:
                has_canonical_import = True
                for alias in node.names:
                    if (
                        alias.name == "build_synthetic_envelopes"
                    ):
                        return  # passes
    assert has_canonical_import, (
        "_inject_phase_9_synthetic_workload MUST import "
        "build_synthetic_envelopes from "
        "phase_9_synthetic_workload (canonical factory)"
    )


def test_inject_method_does_not_reach_into_private_router():
    """AST pin: NO direct access to
    ``self._intake_service._router`` (or any other private
    attribute) inside the inject method. The single pipeline
    flows through ``ingest_envelope`` only."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "_inject_phase_9_synthetic_workload":
                target_func = node
                break
    assert target_func is not None
    # Walk attribute chains. Forbid `self._intake_service._router`
    # specifically — that's the private-router shortcut.
    for node in ast.walk(target_func):
        if isinstance(node, ast.Attribute):
            if node.attr == "_router":
                pytest.fail(
                    "_inject_phase_9_synthetic_workload MUST "
                    "NOT access ._router directly — go through "
                    "ingest_envelope (single-pipeline guardrail)"
                )


def test_inject_called_after_boot_intake_in_run():
    """AST pin: ``_inject_phase_9_synthetic_workload`` MUST be
    called AFTER ``boot_intake`` in the harness run() boot
    sequence. Order matters — intake must be ready before
    injection."""
    target = (
        _repo_root()
        / "backend/core/ouroboros/battle_test/harness.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "run":
                run_func = node
                break
    assert run_func is not None
    boot_intake_line = None
    inject_line = None
    for node in ast.walk(run_func):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if (
                    func.attr == "boot_intake"
                    and boot_intake_line is None
                ):
                    boot_intake_line = node.lineno
                elif (
                    func.attr
                    == "_inject_phase_9_synthetic_workload"
                ):
                    inject_line = node.lineno
    assert boot_intake_line is not None, (
        "boot_intake call missing in run()"
    )
    assert inject_line is not None, (
        "_inject_phase_9_synthetic_workload call missing in "
        "run() — Slice 2 wiring incomplete"
    )
    assert inject_line > boot_intake_line, (
        f"_inject must be called AFTER boot_intake "
        f"(intake at line {boot_intake_line}, inject at line "
        f"{inject_line})"
    )
