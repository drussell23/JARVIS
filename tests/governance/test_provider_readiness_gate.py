"""Regression spine for battle-test Provider-Readiness Gate.

The gate is the structural fix for the SWE v18 ``bt-2026-05-16-
175621`` failure mode (Claude 500-storm → 27 min thrash → $0.26
burn → zero candidates). It runs BEFORE op emission begins and
refuses soak start when the provider stack is unhealthy.

Coverage axes:
  * Master-flag gating (§33.1; default-FALSE preserves byte-
    identical pre-existing behavior)
  * READY verdict: CB closed + Claude probe OK
  * CB_OPEN verdict: CB not allowing requests (highest-priority signal)
  * CLAUDE_PROBE_FAILED verdict: probe returns False / raises
  * TimeoutError on slow probe
  * DW probe gating: require_dw=True / False / None semantics
  * BOTH_UNHEALTHY verdict
  * DW_PROBE_FAILED (only when require_dw)
  * ERROR verdict: gate-internal failure path
  * NEVER-raises across every entry point
  * write_readiness_report: env override + session-dir fallback
  * 4 AST pins pass on current source
  * Authority asymmetry (no forbidden imports)
"""
from __future__ import annotations

import ast as _ast
import asyncio
import json
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend.core.ouroboros.battle_test.provider_readiness_gate import (
    PROVIDER_READINESS_GATE_SCHEMA_VERSION,
    CircuitBreakerSnapshot,
    ProbeResult,
    ProviderReadinessReport,
    ReadinessVerdict,
    check_provider_readiness,
    claude_probe_timeout_s,
    dw_probe_timeout_s,
    master_enabled,
    register_shipped_invariants,
    require_dw,
    write_readiness_report,
)


_MASTER_FLAG = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_GATE_ENABLED"
)
_REQUIRE_DW_FLAG = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_REQUIRE_DW"
)
_REPORT_PATH_FLAG = (
    "JARVIS_BATTLE_TEST_PROVIDER_READINESS_REPORT_PATH"
)


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.delenv(_MASTER_FLAG, raising=False)
    monkeypatch.delenv(_REQUIRE_DW_FLAG, raising=False)
    monkeypatch.delenv(_REPORT_PATH_FLAG, raising=False)
    yield


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(_MASTER_FLAG, "true")


class _FakeProvider:
    """Duck-typed shim for ClaudeProvider / DoublewordProvider."""

    def __init__(
        self, *,
        healthy: bool = True,
        delay_s: float = 0.0,
        raises: Optional[BaseException] = None,
    ) -> None:
        self._healthy = healthy
        self._delay = delay_s
        self._raises = raises
        self.probe_count = 0

    async def health_probe(self) -> bool:
        self.probe_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._healthy


def _healthy_cb() -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        available=True,
        enabled=True,
        should_allow=True,
        state="CLOSED",
    )


def _open_cb() -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        available=True,
        enabled=True,
        should_allow=False,
        state="OPEN",
        consecutive_failures=4,
        total_trips=3,
    )


# ---------------------------------------------------------------------------
# Master gate (§33.1)
# ---------------------------------------------------------------------------


class TestMasterGate:
    def test_master_default_false(self, monkeypatch):
        monkeypatch.delenv(_MASTER_FLAG, raising=False)
        assert master_enabled() is False

    def test_disabled_short_circuits_with_disabled_verdict(self):
        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(),
            )

        result = asyncio.run(_run())
        assert isinstance(result, ProviderReadinessReport)
        assert result.verdict is ReadinessVerdict.DISABLED
        assert result.soak_should_proceed is True

    def test_disabled_does_not_run_claude_probe(self):
        provider = _FakeProvider()

        async def _run():
            return await check_provider_readiness(
                claude_provider=provider,
            )

        asyncio.run(_run())
        # Master off → no probe invoked.
        assert provider.probe_count == 0


# ---------------------------------------------------------------------------
# READY verdict
# ---------------------------------------------------------------------------


class TestReadyVerdict:
    def test_ready_when_cb_closed_and_claude_probe_ok(
        self, monkeypatch,
    ):
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert result.verdict is ReadinessVerdict.READY
        assert result.soak_should_proceed is True
        # One probe ran, was healthy.
        assert len(result.probes) == 1
        assert result.probes[0].healthy is True
        assert result.probes[0].provider == "claude"

    def test_ready_diagnostic_carries_probe_timing(
        self, monkeypatch,
    ):
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert "Claude probe OK" in result.diagnostic


# ---------------------------------------------------------------------------
# CB_OPEN verdict (highest-priority signal)
# ---------------------------------------------------------------------------


class TestCircuitBreakerOpen:
    def test_cb_open_refuses_soak(self, monkeypatch):
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                cb_snapshot_override=_open_cb(),
            )

        result = asyncio.run(_run())
        assert result.verdict is ReadinessVerdict.CB_OPEN
        assert result.soak_should_proceed is False
        assert "circuit breaker" in result.diagnostic.lower()

    def test_cb_open_takes_priority_over_probe_failure(
        self, monkeypatch,
    ):
        """When CB is open AND probe would fail, CB_OPEN wins —
        it's the strongest single signal."""
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=False),
                cb_snapshot_override=_open_cb(),
            )

        result = asyncio.run(_run())
        assert result.verdict is ReadinessVerdict.CB_OPEN

    def test_cb_unavailable_does_not_force_cb_open(
        self, monkeypatch,
    ):
        """If the CB module import / snapshot fails, we degrade
        to probe-only — the gate doesn't refuse the soak just
        because the CB itself was unreadable."""
        _enable(monkeypatch)
        unavailable_cb = CircuitBreakerSnapshot(available=False)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                cb_snapshot_override=unavailable_cb,
            )

        result = asyncio.run(_run())
        # CB unavailable but probe healthy → READY (probe-only mode).
        assert result.verdict is ReadinessVerdict.READY


# ---------------------------------------------------------------------------
# CLAUDE_PROBE_FAILED verdict
# ---------------------------------------------------------------------------


class TestClaudeProbeFailed:
    def test_probe_returns_false(self, monkeypatch):
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=False),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert (
            result.verdict is ReadinessVerdict.CLAUDE_PROBE_FAILED
        )
        assert result.soak_should_proceed is False

    def test_probe_raises(self, monkeypatch):
        _enable(monkeypatch)
        provider = _FakeProvider(
            raises=RuntimeError("simulated 500"),
        )

        async def _run():
            return await check_provider_readiness(
                claude_provider=provider,
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert (
            result.verdict is ReadinessVerdict.CLAUDE_PROBE_FAILED
        )
        assert result.probes[0].err_class == "RuntimeError"

    def test_probe_times_out(self, monkeypatch):
        """Slow probe → timeout → CLAUDE_PROBE_FAILED with
        TimeoutError err_class."""
        _enable(monkeypatch)
        slow = _FakeProvider(delay_s=5.0, healthy=True)

        async def _run():
            return await check_provider_readiness(
                claude_provider=slow,
                claude_timeout_override=0.1,  # 100ms cap
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert (
            result.verdict is ReadinessVerdict.CLAUDE_PROBE_FAILED
        )
        assert result.probes[0].err_class == "TimeoutError"
        assert result.probes[0].healthy is False


# ---------------------------------------------------------------------------
# DW probe gating
# ---------------------------------------------------------------------------


class TestDWProbeGating:
    def test_dw_skipped_by_default(self, monkeypatch):
        """require_dw=False (default) + probe_dw=None →
        no DW probe."""
        _enable(monkeypatch)
        dw = _FakeProvider(healthy=True)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(),
                doubleword_provider=dw,
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert dw.probe_count == 0
        assert len(result.probes) == 1  # Claude only

    def test_dw_probed_when_explicitly_requested(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        dw = _FakeProvider(healthy=True)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(),
                doubleword_provider=dw,
                probe_dw=True,
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert dw.probe_count == 1
        assert len(result.probes) == 2

    def test_dw_probed_when_require_dw_set(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv(_REQUIRE_DW_FLAG, "true")
        dw = _FakeProvider(healthy=True)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(),
                doubleword_provider=dw,
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        # require_dw=True → DW probe fires + counts toward
        # verdict.
        assert dw.probe_count == 1
        assert result.verdict is ReadinessVerdict.READY

    def test_dw_failure_with_require_dw_blocks_soak(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(_REQUIRE_DW_FLAG, "true")

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                doubleword_provider=_FakeProvider(healthy=False),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert (
            result.verdict is ReadinessVerdict.DW_PROBE_FAILED
        )
        assert result.soak_should_proceed is False

    def test_dw_failure_without_require_is_informational(
        self, monkeypatch,
    ):
        """require_dw=False + DW probe explicitly enabled +
        DW fails → still READY (Claude is healthy)."""
        _enable(monkeypatch)

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=True),
                doubleword_provider=_FakeProvider(healthy=False),
                probe_dw=True,
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert result.verdict is ReadinessVerdict.READY
        assert "informational" in result.diagnostic.lower()

    def test_both_unhealthy_with_require_dw(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv(_REQUIRE_DW_FLAG, "true")

        async def _run():
            return await check_provider_readiness(
                claude_provider=_FakeProvider(healthy=False),
                doubleword_provider=_FakeProvider(healthy=False),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert (
            result.verdict is ReadinessVerdict.BOTH_UNHEALTHY
        )
        assert result.soak_should_proceed is False


# ---------------------------------------------------------------------------
# NEVER-raises contract
# ---------------------------------------------------------------------------


class TestNeverRaises:
    def test_garbage_provider_does_not_raise(self, monkeypatch):
        _enable(monkeypatch)

        class _Garbage:
            async def health_probe(self):
                raise ValueError("garbage")

        async def _run():
            return await check_provider_readiness(
                claude_provider=_Garbage(),
                cb_snapshot_override=_healthy_cb(),
            )

        result = asyncio.run(_run())
        assert isinstance(result, ProviderReadinessReport)
        # Surface as CLAUDE_PROBE_FAILED (the probe wrapper
        # catches everything), not as a propagated exception.
        assert (
            result.verdict is ReadinessVerdict.CLAUDE_PROBE_FAILED
        )

    def test_canceled_probe_does_not_raise(self, monkeypatch):
        """If the probe coroutine raises CancelledError, the
        gate must NOT let it propagate."""
        _enable(monkeypatch)

        class _Cancels:
            async def health_probe(self):
                raise asyncio.CancelledError()

        async def _run():
            return await check_provider_readiness(
                claude_provider=_Cancels(),
                cb_snapshot_override=_healthy_cb(),
            )

        try:
            result = asyncio.run(_run())
        except asyncio.CancelledError:
            pytest.fail(
                "gate let CancelledError propagate"
            )
        assert (
            result.verdict is ReadinessVerdict.CLAUDE_PROBE_FAILED
        )
        assert result.probes[0].err_class == "CancelledError"


# ---------------------------------------------------------------------------
# Report container shapes
# ---------------------------------------------------------------------------


class TestReportShapes:
    def test_report_to_dict_carries_schema_version(self):
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.READY,
            cb_snapshot=_healthy_cb(),
            probes=(
                ProbeResult(provider="claude", healthy=True),
            ),
        )
        d = r.to_dict()
        assert d["schema_version"] == (
            PROVIDER_READINESS_GATE_SCHEMA_VERSION
        )
        assert d["verdict"] == "ready"
        assert d["soak_should_proceed"] is True

    def test_soak_should_proceed_property_matches_verdict(self):
        # READY + DISABLED proceed; others do not.
        proceed_set = {
            ReadinessVerdict.READY,
            ReadinessVerdict.DISABLED,
        }
        for v in ReadinessVerdict:
            r = ProviderReadinessReport(
                verdict=v, cb_snapshot=_healthy_cb(),
            )
            assert r.soak_should_proceed == (v in proceed_set), (
                f"{v} mismatch"
            )

    def test_report_is_frozen(self):
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.READY,
            cb_snapshot=_healthy_cb(),
        )
        with pytest.raises(Exception):
            r.verdict = (  # type: ignore[misc]
                ReadinessVerdict.CB_OPEN
            )

    def test_probe_result_to_dict_truncates_long_msg(self):
        long = "x" * 1000
        p = ProbeResult(
            provider="claude", healthy=False,
            err_class="X", err_msg=long,
        )
        d = p.to_dict()
        assert len(d["err_msg"]) <= 256


# ---------------------------------------------------------------------------
# write_readiness_report
# ---------------------------------------------------------------------------


class TestReportPersistence:
    def test_writes_to_explicit_path(self, tmp_path):
        target = tmp_path / "explicit_readiness.json"
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.READY,
            cb_snapshot=_healthy_cb(),
        )
        written = write_readiness_report(
            r, path_override=target,
        )
        assert written == target
        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded["verdict"] == "ready"

    def test_writes_to_env_path(self, tmp_path, monkeypatch):
        env_path = tmp_path / "env_readiness.json"
        monkeypatch.setenv(_REPORT_PATH_FLAG, str(env_path))
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.CB_OPEN,
            cb_snapshot=_open_cb(),
        )
        written = write_readiness_report(r)
        assert written == env_path
        assert env_path.exists()

    def test_writes_to_session_dir(self, tmp_path):
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.READY,
            cb_snapshot=_healthy_cb(),
        )
        written = write_readiness_report(
            r, session_dir=tmp_path,
        )
        assert written == tmp_path / "provider_readiness.json"
        assert written.exists()

    def test_write_never_raises_on_bad_path(self):
        r = ProviderReadinessReport(
            verdict=ReadinessVerdict.READY,
            cb_snapshot=_healthy_cb(),
        )
        # Path with embedded NUL — write will fail; must return
        # None without raising.
        result = write_readiness_report(
            r, path_override=Path("/this/should/not/exist/\x00"),
        )
        assert result is None


# ---------------------------------------------------------------------------
# Env knob accessors
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_claude_probe_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_BATTLE_TEST_PROVIDER_READINESS_"
            "CLAUDE_PROBE_TIMEOUT_S", raising=False,
        )
        assert claude_probe_timeout_s() == 10.0

    def test_claude_probe_timeout_clamped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_BATTLE_TEST_PROVIDER_READINESS_"
            "CLAUDE_PROBE_TIMEOUT_S", "9999",
        )
        assert claude_probe_timeout_s() == 60.0

    def test_dw_probe_timeout_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_BATTLE_TEST_PROVIDER_READINESS_"
            "DW_PROBE_TIMEOUT_S", raising=False,
        )
        assert dw_probe_timeout_s() == 5.0

    def test_dw_probe_timeout_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_BATTLE_TEST_PROVIDER_READINESS_"
            "DW_PROBE_TIMEOUT_S", "not-a-number",
        )
        assert dw_probe_timeout_s() == 5.0

    def test_require_dw_default_false(self, monkeypatch):
        monkeypatch.delenv(_REQUIRE_DW_FLAG, raising=False)
        assert require_dw() is False


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_returns_four_pins(self):
        pins = register_shipped_invariants()
        names = {p.invariant_name for p in pins}
        assert names == {
            "provider_readiness_gate_verdict_taxonomy",
            "provider_readiness_gate_composes_canonical",
            "provider_readiness_gate_authority_asymmetry",
            "provider_readiness_gate_master_default_false",
        }

    def test_all_pins_pass_on_current_source(self):
        pins = register_shipped_invariants()
        src_path = Path(
            "backend/core/ouroboros/battle_test/"
            "provider_readiness_gate.py"
        )
        source = src_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        for pin in pins:
            violations = pin.validate(tree, source)
            assert violations == (), (
                f"{pin.invariant_name} drift: {violations}"
            )

    def test_authority_asymmetry_no_forbidden_imports(self):
        src_path = Path(
            "backend/core/ouroboros/battle_test/"
            "provider_readiness_gate.py"
        )
        source = src_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        forbidden = {
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden, (
                    f"forbidden import: {mod}"
                )
