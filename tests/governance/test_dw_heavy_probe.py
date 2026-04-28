"""Phase 12.2 Slice D — Heavy probe regression spine.

Pins:
  §1  ``heavy_probe_enabled()`` flag — default false; case-tolerance
  §2  HeavyProbeBudget — daily-USD ledger + UTC midnight rollover
  §3  HeavyProbeBudget — atomic disk persistence
  §4  HeavyProbeBudget — corrupt/missing → empty start (NEVER raises)
  §5  HeavyProber — flag-off short-circuit returns no-op result
  §6  HeavyProber — empty model_id rejected
  §7  HeavyProber — budget pre-flight refuses + doesn't charge
  §8  HeavyProber — success path records TTFT into observer
  §9  HeavyProber — failure path records ceiling TTFT (cold-storage signal)
  §10 HeavyProber — broken observer doesn't take down probe
  §11 HeavyProbeScheduler — picks first eligible candidate
  §12 HeavyProbeScheduler — skips already-promoted models
  §13 HeavyProbeScheduler — skips cold-storage flagged models
  §14 HeavyProbeScheduler — skips models within cooldown window
  §15 HeavyProbeScheduler — skips when budget exhausted
  §16 HeavyProbeScheduler — flag-off short-circuit
  §17 dw_discovery_runner — get_heavy_probe_budget singleton + reset
  §18 Authority invariants — heavy prober never mutates ledger
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_heavy_probe import (
    BUDGET_SCHEMA_VERSION,
    HeavyProbeBudget,
    HeavyProber,
    HeavyProbeResult,
    HeavyProbeScheduler,
    heavy_probe_enabled,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_budget(tmp_path, monkeypatch) -> HeavyProbeBudget:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "budget.json"),
    )
    bud = HeavyProbeBudget()
    bud.load()
    return bud


@pytest.fixture
def heavy_probe_on(monkeypatch):
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "true")


# ---------------------------------------------------------------------------
# §1 — heavy_probe_enabled flag
# ---------------------------------------------------------------------------


def test_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", raising=False,
    )
    assert heavy_probe_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "On"])
def test_flag_truthy_values(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", val)
    assert heavy_probe_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", " "])
def test_flag_falsy_values(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", val)
    assert heavy_probe_enabled() is False


# ---------------------------------------------------------------------------
# §2-§4 — HeavyProbeBudget
# ---------------------------------------------------------------------------


def test_budget_starts_with_full_remaining(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.10",
    )
    assert isolated_budget.spent_today_usd() == 0.0
    assert isolated_budget.remaining_usd() == 0.10


def test_budget_check_and_charge_commits(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.10",
    )
    assert isolated_budget.check_and_charge(0.04) is True
    assert isolated_budget.spent_today_usd() == pytest.approx(0.04)
    assert isolated_budget.remaining_usd() == pytest.approx(0.06)


def test_budget_check_and_charge_refuses_overflow(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.05",
    )
    assert isolated_budget.check_and_charge(0.04) is True
    # Second 0.04 charge would bust; refused + ledger unchanged
    assert isolated_budget.check_and_charge(0.04) is False
    assert isolated_budget.spent_today_usd() == pytest.approx(0.04)


def test_budget_negative_charge_refused(
    isolated_budget: HeavyProbeBudget,
) -> None:
    assert isolated_budget.check_and_charge(-0.01) is False
    assert isolated_budget.spent_today_usd() == 0.0


def test_budget_persists_across_instances(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "b.json"),
    )
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.10",
    )
    bud1 = HeavyProbeBudget()
    bud1.load()
    bud1.check_and_charge(0.03)

    bud2 = HeavyProbeBudget()
    bud2.load()
    assert bud2.spent_today_usd() == pytest.approx(0.03)


def test_budget_rollover_resets_at_utc_midnight(
    tmp_path, monkeypatch,
) -> None:
    """A persisted ledger from yesterday auto-resets on next access."""
    p = tmp_path / "b.json"
    yesterday = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%d")
    p.write_text(json.dumps({
        "schema_version": BUDGET_SCHEMA_VERSION,
        "current_day": yesterday,
        "spent_today_usd": 0.99,
    }))
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH", str(p),
    )
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.10",
    )
    bud = HeavyProbeBudget()
    bud.load()
    # Stale day discarded → fresh budget today
    assert bud.spent_today_usd() == 0.0
    assert bud.remaining_usd() == pytest.approx(0.10)


def test_budget_corrupt_json_starts_fresh(
    tmp_path, monkeypatch,
) -> None:
    p = tmp_path / "b.json"
    p.write_text("{ bad json")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH", str(p),
    )
    bud = HeavyProbeBudget()
    bud.load()  # NEVER raises
    assert bud.spent_today_usd() == 0.0


def test_budget_schema_mismatch_starts_fresh(
    tmp_path, monkeypatch,
) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({
        "schema_version": "wrong.0",
        "current_day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "spent_today_usd": 0.99,
    }))
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH", str(p),
    )
    bud = HeavyProbeBudget()
    bud.load()
    assert bud.spent_today_usd() == 0.0


# ---------------------------------------------------------------------------
# §5-§10 — HeavyProber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prober_flag_off_returns_noop(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "false",
    )
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=MagicMock(),
        model_id="vendor/m-7B",
        base_url="https://test.example",
        api_key="key",
    )
    assert result.success is False
    assert result.error == "master_flag_off"
    # Budget untouched
    assert isolated_budget.spent_today_usd() == 0.0


@pytest.mark.asyncio
async def test_prober_empty_model_id_rejected(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
) -> None:
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=MagicMock(),
        model_id="",
        base_url="https://test.example",
        api_key="key",
    )
    assert result.success is False
    assert result.error == "empty_model_id"


@pytest.mark.asyncio
async def test_prober_budget_exhausted_refuses(
    tmp_path, monkeypatch, heavy_probe_on,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "b.json"),
    )
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.0",
    )
    bud = HeavyProbeBudget()
    bud.load()
    prober = HeavyProber(budget=bud)
    result = await prober.probe(
        session=MagicMock(),
        model_id="vendor/m-7B",
        base_url="https://test.example",
        api_key="key",
    )
    assert result.success is False
    assert result.error == "budget_exhausted"


@pytest.mark.asyncio
async def test_prober_success_records_into_observer(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
) -> None:
    """When the SSE stream returns content, the prober records TTFT
    into the supplied observer."""
    fake_session = _SessionWith(
        chunks=(b"data: {\"choices\":[{\"delta\":{\"content\":\"H\"}}]}\n",),
    )
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session,
        model_id="vendor/m-7B",
        base_url="https://test.example",
        api_key="key",
        observer=fake_obs,
    )
    assert result.success is True
    assert result.ttft_ms >= 0
    fake_obs.record_ttft.assert_called_once()
    args, _ = fake_obs.record_ttft.call_args
    assert args[0] == "vendor/m-7B"


@pytest.mark.asyncio
async def test_prober_failure_records_ceiling_ttft(
    isolated_budget: HeavyProbeBudget, heavy_probe_on, monkeypatch,
) -> None:
    """A 500 response → prober records the timeout ceiling as TTFT
    (asymmetric cold-storage signal)."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S", "5")
    fake_session = _SessionWith(status=500, chunks=())
    fake_obs = MagicMock()
    prober = HeavyProber(budget=isolated_budget)
    result = await prober.probe(
        session=fake_session,
        model_id="vendor/m-7B",
        base_url="https://test.example",
        api_key="key",
        observer=fake_obs,
    )
    assert result.success is False
    assert "status_500" in result.error
    fake_obs.record_ttft.assert_called_once()
    args, _ = fake_obs.record_ttft.call_args
    # Ceiling = timeout_ms (5000)
    assert args[1] == 5000


@pytest.mark.asyncio
async def test_prober_broken_observer_doesnt_break_probe(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
) -> None:
    fake_session = _SessionWith(
        chunks=(b"data: {\"choices\":[{\"delta\":{\"content\":\"H\"}}]}\n",),
    )

    class _Broken:
        def record_ttft(self, *a, **kw):
            raise RuntimeError("observer faulted")

    prober = HeavyProber(budget=isolated_budget)
    # Should NOT raise
    result = await prober.probe(
        session=fake_session,
        model_id="vendor/m-7B",
        base_url="https://test.example",
        api_key="key",
        observer=_Broken(),
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# §11-§16 — HeavyProbeScheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_picks_first_eligible(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
) -> None:
    """Scheduler walks candidates in order, fires probe on first
    eligible one."""
    fake_session = _SessionWith(
        chunks=(b"data: {\"choices\":[{\"delta\":{\"content\":\"H\"}}]}\n",),
    )
    prober = _StubProber(budget=isolated_budget)
    sched = HeavyProbeScheduler(prober=prober, budget=isolated_budget)
    result = await sched.run_cycle(
        session=fake_session,
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B", "vendor/m-13B"),
    )
    assert result is not None
    assert prober.probed_ids == ["vendor/m-7B"]


@pytest.mark.asyncio
async def test_scheduler_skips_promoted(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
    tmp_path, monkeypatch,
) -> None:
    """Already-promoted model is skipped by eligibility check."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH", str(tmp_path / "led.json"),
    )
    ddr.reset_boot_state_for_tests()
    led = ddr._get_or_create_ledger()
    led.register_quarantine("vendor/m-7B")
    # Force-promote bypass eligibility — we want is_promoted=True
    rec = led._records["vendor/m-7B"]
    rec.promoted = True
    rec.promoted_at_unix = time.time()

    prober = _StubProber(budget=isolated_budget)
    sched = HeavyProbeScheduler(prober=prober, budget=isolated_budget)
    await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert prober.probed_ids == []
    ddr.reset_boot_state_for_tests()


@pytest.mark.asyncio
async def test_scheduler_skips_cold_storage(
    isolated_budget: HeavyProbeBudget, heavy_probe_on,
    tmp_path, monkeypatch,
) -> None:
    """Model already flagged as cold-storage by the observer is
    skipped (signal is already present)."""
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_TTFT_STATE_PATH",
        str(tmp_path / "ttft.json"),
    )
    ddr.reset_boot_state_for_tests()
    obs = ddr.get_ttft_observer()
    # Build a stable mean + cold-storage spike
    for ms in (100, 102, 99, 101, 100):
        obs.record_ttft("vendor/m-7B", ms)
    obs.record_ttft("vendor/m-7B", 1000)
    assert obs.is_cold_storage("vendor/m-7B") is True

    prober = _StubProber(budget=isolated_budget)
    sched = HeavyProbeScheduler(prober=prober, budget=isolated_budget)
    await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert prober.probed_ids == []
    ddr.reset_boot_state_for_tests()


@pytest.mark.asyncio
async def test_scheduler_skips_within_cooldown(
    isolated_budget: HeavyProbeBudget, heavy_probe_on, monkeypatch,
) -> None:
    """A model probed within ``_probe_interval_s`` is skipped."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_INTERVAL_S", "300")
    prober = _StubProber(budget=isolated_budget)
    sched = HeavyProbeScheduler(prober=prober, budget=isolated_budget)
    # First probe fires
    await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert prober.probed_ids == ["vendor/m-7B"]
    # Second probe within cooldown — skipped
    await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert prober.probed_ids == ["vendor/m-7B"]


@pytest.mark.asyncio
async def test_scheduler_skips_when_budget_exhausted(
    tmp_path, monkeypatch, heavy_probe_on,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "b.json"),
    )
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.0",
    )
    bud = HeavyProbeBudget()
    bud.load()
    prober = _StubProber(budget=bud)
    sched = HeavyProbeScheduler(prober=prober, budget=bud)
    result = await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert result is None
    assert prober.probed_ids == []


@pytest.mark.asyncio
async def test_scheduler_flag_off_short_circuit(
    isolated_budget: HeavyProbeBudget, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "false",
    )
    prober = _StubProber(budget=isolated_budget)
    sched = HeavyProbeScheduler(prober=prober, budget=isolated_budget)
    result = await sched.run_cycle(
        session=MagicMock(),
        base_url="https://test.example",
        api_key="key",
        candidate_ids=("vendor/m-7B",),
    )
    assert result is None
    assert prober.probed_ids == []


# ---------------------------------------------------------------------------
# §17 — dw_discovery_runner singleton + reset
# ---------------------------------------------------------------------------


def test_get_heavy_probe_budget_returns_none_when_off(
    monkeypatch, tmp_path,
) -> None:
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "false")
    ddr.reset_boot_state_for_tests()
    assert ddr.get_heavy_probe_budget() is None


def test_get_heavy_probe_budget_singleton_when_on(
    monkeypatch, tmp_path,
) -> None:
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "b.json"),
    )
    ddr.reset_boot_state_for_tests()
    bud1 = ddr.get_heavy_probe_budget()
    bud2 = ddr.get_heavy_probe_budget()
    assert bud1 is not None
    assert bud1 is bud2


def test_reset_drops_heavy_probe_budget_singleton(
    monkeypatch, tmp_path,
) -> None:
    from backend.core.ouroboros.governance import dw_discovery_runner as ddr
    monkeypatch.setenv("JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        str(tmp_path / "b.json"),
    )
    ddr.reset_boot_state_for_tests()
    bud1 = ddr.get_heavy_probe_budget()
    ddr.reset_boot_state_for_tests()
    bud2 = ddr.get_heavy_probe_budget()
    assert bud1 is not bud2


# ---------------------------------------------------------------------------
# §18 — Authority invariants
# ---------------------------------------------------------------------------


def test_heavy_prober_never_mutates_ledger() -> None:
    """Heavy prober reads observer + ledger but never mutates the
    ledger. Same authority invariant as Slice C classifier."""
    import ast
    import inspect
    from backend.core.ouroboros.governance import dw_heavy_probe
    src = inspect.getsource(dw_heavy_probe)
    tree = ast.parse(src)
    forbidden = {
        "register_quarantine",
        "record_success",
        "record_failure",
        "promote",
        "demote",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func
            if (
                isinstance(target.value, ast.Name)
                and target.value.id in {"led", "ledger", "promotion_ledger"}
                and target.attr in forbidden
            ):
                raise AssertionError(
                    f"heavy prober calls forbidden ledger.{target.attr} "
                    f"at line {node.lineno}"
                )


def test_heavy_probe_never_imports_orchestrator() -> None:
    """Heavy probe is a discovery-layer primitive — it must not
    reach into orchestrator / phase_runner authority."""
    import inspect
    from backend.core.ouroboros.governance import dw_heavy_probe
    src = inspect.getsource(dw_heavy_probe)
    for forbidden in (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.semantic_guardian",
    ):
        assert forbidden not in src, f"heavy probe imports {forbidden}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SessionWith:
    """Minimal aiohttp.ClientSession-shaped stub that returns the
    given chunks from resp.content.readline()."""
    def __init__(
        self,
        *,
        status: int = 200,
        chunks: Tuple[bytes, ...] = (),
    ) -> None:
        self._status = status
        self._chunks: List[bytes] = list(chunks) + [b""]  # EOF sentinel

    def post(self, *a, **kw):
        return _RespCM(status=self._status, chunks=self._chunks)


class _RespCM:
    def __init__(self, *, status: int, chunks: List[bytes]) -> None:
        self._status = status
        self._chunks = chunks

    async def __aenter__(self):
        return _Resp(status=self._status, chunks=self._chunks)

    async def __aexit__(self, *a):
        return None


class _Resp:
    def __init__(self, *, status: int, chunks: List[bytes]) -> None:
        self.status = status
        self.content = _Content(chunks=chunks)

    async def text(self) -> str:
        return f"HTTP {self.status}"


class _Content:
    def __init__(self, *, chunks: List[bytes]) -> None:
        self._chunks = chunks

    async def readline(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _StubProber:
    """A HeavyProber-shaped stub that records which model_ids it
    was asked to probe. Used to verify scheduler behavior without
    needing a real HTTP path."""
    def __init__(self, *, budget: HeavyProbeBudget) -> None:
        self._budget = budget
        self.probed_ids: List[str] = []

    async def probe(self, *, session, model_id, base_url, api_key,
                    observer=None):
        self.probed_ids.append(model_id)
        return HeavyProbeResult(
            model_id=model_id,
            success=True,
            ttft_ms=100,
            total_latency_ms=200,
            cost_usd=0.001,
            error="",
        )
