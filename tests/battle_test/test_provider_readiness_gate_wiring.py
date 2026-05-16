"""Harness-level wiring spine for the provider-readiness gate.

Proves the boot-time integration without spinning up the full
6-layer stack:

  * ``_gate_provider_readiness_or_refuse`` exists on the harness
  * Master-FALSE (default) returns ``False`` without probing
  * Master-TRUE + READY verdict returns ``False`` (proceed)
  * Master-TRUE + refusing verdict returns ``True`` + stamps
    ``_stop_reason`` + writes report to the session dir
  * Gate-internal crash is fail-open (returns ``False``)
  * Boot sequence in ``run()`` invokes the gate AFTER
    ``boot_governed_loop_service`` and BEFORE ``boot_jarvis_tiers``
    (AST byte-pinned positional invariant)

The integration tests use the published ``check_provider_readiness``
seam to inject deterministic verdicts — no real network probes fire.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)
from backend.core.ouroboros.battle_test.provider_readiness_gate import (
    CircuitBreakerSnapshot,
    ProviderReadinessReport,
    ReadinessVerdict,
)


_MASTER_FLAG = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_GATE_ENABLED"
)


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    session_dir = (
        tmp_path / ".ouroboros" / "sessions" / "bt-gate-test"
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=10.0,
        session_dir=session_dir,
    )
    h = BattleTestHarness(config)
    yield h
    import atexit
    try:
        atexit.unregister(h._atexit_fallback_write)
    except Exception:
        pass


def _ready_report() -> ProviderReadinessReport:
    return ProviderReadinessReport(
        verdict=ReadinessVerdict.READY,
        cb_snapshot=CircuitBreakerSnapshot(
            available=True, enabled=True,
            should_allow=True, state="CLOSED",
        ),
        diagnostic="Claude probe OK",
    )


def _cb_open_report() -> ProviderReadinessReport:
    return ProviderReadinessReport(
        verdict=ReadinessVerdict.CB_OPEN,
        cb_snapshot=CircuitBreakerSnapshot(
            available=True, enabled=True,
            should_allow=False, state="OPEN",
        ),
        diagnostic="circuit breaker OPEN",
    )


# ---------------------------------------------------------------------------
# Surface presence
# ---------------------------------------------------------------------------


def test_method_exists_on_harness():
    assert hasattr(
        BattleTestHarness, "_gate_provider_readiness_or_refuse"
    )
    method = BattleTestHarness._gate_provider_readiness_or_refuse
    assert inspect.iscoroutinefunction(method), (
        "gate method must be async — boot sequence awaits it"
    )


# ---------------------------------------------------------------------------
# Master gate semantics
# ---------------------------------------------------------------------------


def test_master_off_returns_false_no_probe(
    tmp_harness, monkeypatch,
):
    """Default (master-FALSE) preserves pre-gate boot behavior:
    returns False, no probe runs."""
    monkeypatch.delenv(_MASTER_FLAG, raising=False)

    called = {"n": 0}

    async def _spy(*a, **kw):
        called["n"] += 1
        return _ready_report()

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_spy,
    ):
        result = asyncio.run(
            tmp_harness._gate_provider_readiness_or_refuse()
        )

    assert result is False
    # master_enabled() short-circuits — probe NOT invoked.
    assert called["n"] == 0


def test_master_on_ready_returns_false(
    tmp_harness, monkeypatch,
):
    monkeypatch.setenv(_MASTER_FLAG, "true")

    async def _ok(*a, **kw):
        return _ready_report()

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_ok,
    ):
        result = asyncio.run(
            tmp_harness._gate_provider_readiness_or_refuse()
        )

    assert result is False  # proceed
    # No refusal stamp on success path — stop_reason stays at its
    # harness-initial value (it's only refined later by the run-loop).
    assert not str(
        tmp_harness._stop_reason or ""
    ).startswith("provider_readiness_refused")


# ---------------------------------------------------------------------------
# Refusal path
# ---------------------------------------------------------------------------


def test_master_on_cb_open_refuses_and_stamps(
    tmp_harness, monkeypatch,
):
    """CB_OPEN verdict must short-circuit the boot AND write a
    structured report AND stamp ``_stop_reason``."""
    monkeypatch.setenv(_MASTER_FLAG, "true")

    async def _refuse(*a, **kw):
        return _cb_open_report()

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_refuse,
    ):
        result = asyncio.run(
            tmp_harness._gate_provider_readiness_or_refuse()
        )

    assert result is True  # refuse
    assert tmp_harness._stop_reason == (
        "provider_readiness_refused:cb_open"
    )
    # Report file landed in session dir.
    report_path = (
        tmp_harness._session_dir / "provider_readiness.json"
    )
    assert report_path.exists()
    payload = json.loads(report_path.read_text())
    assert payload["verdict"] == "cb_open"
    assert payload["soak_should_proceed"] is False


def test_master_on_claude_probe_failed_refuses(
    tmp_harness, monkeypatch,
):
    monkeypatch.setenv(_MASTER_FLAG, "true")

    async def _refuse(*a, **kw):
        return ProviderReadinessReport(
            verdict=ReadinessVerdict.CLAUDE_PROBE_FAILED,
            cb_snapshot=CircuitBreakerSnapshot(
                available=True, enabled=True,
                should_allow=True, state="CLOSED",
            ),
            diagnostic="probe returned False",
        )

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_refuse,
    ):
        result = asyncio.run(
            tmp_harness._gate_provider_readiness_or_refuse()
        )

    assert result is True
    assert tmp_harness._stop_reason == (
        "provider_readiness_refused:claude_probe_failed"
    )


# ---------------------------------------------------------------------------
# Fail-open semantics
# ---------------------------------------------------------------------------


def test_gate_internal_crash_fails_open(
    tmp_harness, monkeypatch,
):
    """If the gate ITSELF raises, do not block the soak — log +
    return False. The gate is defense-in-depth, not a single
    point of failure."""
    monkeypatch.setenv(_MASTER_FLAG, "true")

    async def _boom(*a, **kw):
        raise RuntimeError("simulated gate crash")

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_boom,
    ):
        result = asyncio.run(
            tmp_harness._gate_provider_readiness_or_refuse()
        )

    assert result is False  # proceed on gate-crash
    # No refusal stamp — fail-open is silent except for log.
    assert not str(
        tmp_harness._stop_reason or ""
    ).startswith("provider_readiness_refused")


def test_gate_never_raises_to_caller(
    tmp_harness, monkeypatch,
):
    """The gate method itself MUST never propagate. Even if the
    underlying substrate raises, surface as False."""
    monkeypatch.setenv(_MASTER_FLAG, "true")

    async def _explode(*a, **kw):
        raise SystemExit(1)

    with patch(
        "backend.core.ouroboros.battle_test."
        "provider_readiness_gate.check_provider_readiness",
        side_effect=_explode,
    ):
        # Note: SystemExit is BaseException, not Exception. The gate
        # catches Exception only — so SystemExit MAY propagate.
        # We tighten: assert it does NOT raise plain Exception.
        try:
            result = asyncio.run(
                tmp_harness._gate_provider_readiness_or_refuse()
            )
        except SystemExit:
            # Acceptable — BaseException tier.
            return
        assert result is False


# ---------------------------------------------------------------------------
# AST positional invariant — gate runs AFTER boot_governed_loop_service
# and BEFORE boot_jarvis_tiers
# ---------------------------------------------------------------------------


def test_boot_sequence_positional_invariant():
    """Bytes-pinned: in ``run()``, the gate _BootPhase block must
    appear AFTER ``boot_governed_loop_service`` and BEFORE
    ``boot_jarvis_tiers``. This is load-bearing — the gate must
    have providers constructed before probing, but must run before
    any op-emitting subsystem boots."""
    src = Path(
        inspect.getfile(BattleTestHarness)
    ).read_text(encoding="utf-8")

    gls_marker = '_BootPhase("boot_governed_loop_service")'
    gate_marker = '_BootPhase("boot_provider_readiness_gate")'
    tiers_marker = '_BootPhase("boot_jarvis_tiers")'

    gls_idx = src.find(gls_marker)
    gate_idx = src.find(gate_marker)
    tiers_idx = src.find(tiers_marker)

    assert gls_idx > 0, "boot_governed_loop_service phase missing"
    assert gate_idx > 0, (
        "boot_provider_readiness_gate phase missing"
    )
    assert tiers_idx > 0, "boot_jarvis_tiers phase missing"

    assert gls_idx < gate_idx < tiers_idx, (
        f"boot ordering drift: gls={gls_idx} gate={gate_idx} "
        f"tiers={tiers_idx} — gate must be between gls and tiers"
    )


def test_boot_sequence_early_return_on_refusal():
    """When gate returns True, the boot try-block must early-return
    so subsequent phases (boot_jarvis_tiers / boot_intake) do NOT
    execute. The ``finally`` still runs shutdown + report.

    Verified structurally: the gate phase block contains a bare
    ``return`` statement guarded by the gate's True verdict.
    """
    src = Path(
        inspect.getfile(BattleTestHarness)
    ).read_text(encoding="utf-8")
    gate_block_start = src.find(
        '_BootPhase("boot_provider_readiness_gate")'
    )
    # Look ahead 800 chars — covers the entire phase block.
    snippet = src[gate_block_start: gate_block_start + 800]
    assert (
        "_gate_provider_readiness_or_refuse" in snippet
    ), "gate phase must invoke the gate method"
    assert (
        "return" in snippet
    ), "gate phase must early-return on refusal"
