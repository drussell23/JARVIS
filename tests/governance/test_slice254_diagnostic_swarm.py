"""Slice 254 — Ephemeral Swarm Intelligence & Dynamic Diagnostic Delegation.

Proves the diagnostic swarm: a trapped COMPLEX anomaly spawns an ephemeral,
read-only DiagnosticSubAgent (bounded by a hard TTL) that investigates the system
state asynchronously — NEVER blocking the kernel — and pipes a concise root-cause
analysis back to the endorsement gateway, so the Host's [Y/N] decision is
intelligence-backed instead of blind. Decoupled from the 102K kernel (fakes); the
kernel-chain proof lives in test_reanimation_kernel_wiring.py (sandbox-off).
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.cybernetic_reanimation as cr
import backend.core.diagnostic_swarm as ds


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_DIAGNOSTIC_SWARM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "true")
    monkeypatch.delenv("JARVIS_DIAGNOSTIC_AGENT_TTL_S", raising=False)
    cr.reset_trap_observers()
    cr.reset_pending_shadow_actions()
    yield
    cr.reset_trap_observers()
    cr.reset_pending_shadow_actions()


class FakeProbe:
    """Duck-typed read-only diagnostic probe for tests."""
    def __init__(self, facts=None, delay=0.0, boom=False):
        self.facts = facts or {"top_process": "python(pid=999) cpu=95%", "mem_pct": 92}
        self.delay = delay
        self.boom = boom
        self.calls = 0

    async def investigate(self, context):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("probe blew up")
        return dict(self.facts)


def _trap_payload(signal="anomaly_detected:worker-7:rising", aid="shadow-000001"):
    return {
        "organ_name": "SelfHealingOrchestrator",
        "intended_action": "execute remediation 'restart' on 'worker-7'",
        "triggering_signal": signal,
        "action_id": aid,
        "op_id": "",
    }


# ---------------------------------------------------------------------------
# Phase 1/2 — env flags + complexity filter
# ---------------------------------------------------------------------------

class TestEnvAndFilter:
    def test_swarm_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DIAGNOSTIC_SWARM_ENABLED", raising=False)
        assert ds.swarm_enabled() is True

    def test_swarm_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIAGNOSTIC_SWARM_ENABLED", "0")
        assert ds.swarm_enabled() is False

    def test_agent_ttl_defaults_to_60s(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DIAGNOSTIC_AGENT_TTL_S", raising=False)
        assert ds.agent_ttl_s() == 60.0

    @pytest.mark.parametrize("sig,expected", [
        ("anomaly_detected:worker-7:rising", True),
        ("resource_pressure:cpu:rising", True),
        ("component_degraded:db:rising", False),
        ("", False),
    ])
    def test_complexity_filter(self, sig, expected):
        assert ds.is_complex_trap(_trap_payload(signal=sig)) is expected


# ---------------------------------------------------------------------------
# Phase 2 — the ephemeral, read-only DiagnosticSubAgent
# ---------------------------------------------------------------------------

class TestDiagnosticSubAgent:
    def test_investigate_returns_finding_with_facts(self):
        agent = ds.DiagnosticSubAgent(probe=FakeProbe())
        f = asyncio.run(agent.investigate(
            action_id="shadow-000001", organ="SelfHealingOrchestrator",
            intended_action="execute remediation", triggering_signal="anomaly_detected:w:rising",
        ))
        assert f.status == "ok"
        assert f.action_id == "shadow-000001"
        assert f.organ == "SelfHealingOrchestrator"
        assert f.facts.get("mem_pct") == 92
        assert f.summary                                  # concise root-cause text
        assert "python(pid=999)" in f.summary or "python(pid=999)" in str(f.facts)

    def test_ttl_timeout_yields_timeout_status_not_raise(self):
        agent = ds.DiagnosticSubAgent(probe=FakeProbe(delay=0.5), ttl_s=0.01)
        f = asyncio.run(agent.investigate(action_id="shadow-1"))
        assert f.status == "timeout"                      # bounded — a hung probe cannot leak

    def test_probe_error_is_swallowed(self):
        agent = ds.DiagnosticSubAgent(probe=FakeProbe(boom=True))
        f = asyncio.run(agent.investigate(action_id="shadow-1"))
        assert f.status == "error"                        # fail-soft, never raises

    def test_subagent_is_structurally_read_only(self):
        agent = ds.DiagnosticSubAgent(probe=FakeProbe())
        # No authority surface — a diagnostic agent can observe but never act.
        for forbidden in ("execute", "endorse", "kill", "shed", "apply", "mutate"):
            assert not hasattr(agent, forbidden)


# ---------------------------------------------------------------------------
# Phase 2 — the findings store (bounded, awaitable)
# ---------------------------------------------------------------------------

class TestFindingsStore:
    def _finding(self, aid):
        return ds.DiagnosticFinding(
            action_id=aid, op_id="", organ="SHO", summary="hot cpu", facts={}, status="ok", elapsed_s=0.01,
        )

    def test_put_then_get(self):
        store = ds.DiagnosticFindingsStore()
        store.put(self._finding("a1"))
        assert store.get("a1").summary == "hot cpu"
        assert store.get("missing") is None

    def test_wait_for_present(self):
        store = ds.DiagnosticFindingsStore()
        store.put(self._finding("a1"))
        got = asyncio.run(store.wait_for("a1", timeout=1.0))
        assert got is not None and got.action_id == "a1"

    def test_wait_for_arrives_concurrently(self):
        store = ds.DiagnosticFindingsStore()

        async def _go():
            async def _late_put():
                await asyncio.sleep(0.02)
                store.put(self._finding("a1"))
            asyncio.ensure_future(_late_put())
            return await store.wait_for("a1", timeout=1.0)

        got = asyncio.run(_go())
        assert got is not None and got.action_id == "a1"

    def test_wait_for_times_out_to_none(self):
        store = ds.DiagnosticFindingsStore()
        got = asyncio.run(store.wait_for("never", timeout=0.05))
        assert got is None

    def test_store_is_bounded(self):
        store = ds.DiagnosticFindingsStore(max_size=2)
        for i in range(3):
            store.put(self._finding(f"a{i}"))
        assert store.get("a0") is None                    # oldest evicted
        assert store.get("a2") is not None


# ---------------------------------------------------------------------------
# Phase 2 — the orchestrator spawns NON-BLOCKING ephemeral agents
# ---------------------------------------------------------------------------

class TestOrchestratorSpawn:
    def test_handle_trap_is_non_blocking(self):
        async def _go():
            orch = ds.SubAgentOrchestrator(agent_factory=lambda: ds.DiagnosticSubAgent(probe=FakeProbe(delay=0.05)))
            payload = _trap_payload()
            task = orch.handle_trap(payload)
            # returns immediately with a scheduled task — finding NOT ready yet
            assert task is not None
            assert orch.store.get("shadow-000001") is None
            await task                                     # let the ephemeral agent run
            return orch.store.get("shadow-000001")

        finding = asyncio.run(_go())
        assert finding is not None and finding.status == "ok"

    def test_handle_trap_skips_non_complex(self):
        async def _go():
            orch = ds.SubAgentOrchestrator(agent_factory=lambda: ds.DiagnosticSubAgent(probe=FakeProbe()))
            return orch.handle_trap(_trap_payload(signal="component_degraded:db:rising"))
        assert asyncio.run(_go()) is None

    def test_handle_trap_skips_when_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIAGNOSTIC_SWARM_ENABLED", "0")
        async def _go():
            orch = ds.SubAgentOrchestrator(agent_factory=lambda: ds.DiagnosticSubAgent(probe=FakeProbe()))
            return orch.handle_trap(_trap_payload())
        assert asyncio.run(_go()) is None

    def test_handle_trap_no_running_loop_is_fail_soft(self):
        # Called from a sync context (no loop) — must not raise, returns None.
        orch = ds.SubAgentOrchestrator(agent_factory=lambda: ds.DiagnosticSubAgent(probe=FakeProbe()))
        assert orch.handle_trap(_trap_payload()) is None


# ---------------------------------------------------------------------------
# Phase 3 — REPL enrichment matrix
# ---------------------------------------------------------------------------

class TestEnrichment:
    def test_enriched_prompt_includes_diagnosis(self):
        finding = ds.DiagnosticFinding(
            action_id="shadow-000001", op_id="", organ="SHO",
            summary="root cause: runaway python(pid=999) at 95% cpu", facts={}, status="ok", elapsed_s=0.02,
        )
        out = ds.enriched_endorsement_prompt(_trap_payload(), finding)
        assert "runaway python(pid=999)" in out           # diagnosis shown first
        assert "[Y/N]" in out                             # then the decision
        # diagnosis must precede the endorsement line
        assert out.index("runaway") < out.index("[Y/N]")

    def test_enriched_prompt_pending_when_no_finding(self):
        out = ds.enriched_endorsement_prompt(_trap_payload(), None)
        assert "pending" in out.lower()
        assert "[Y/N]" in out


# ---------------------------------------------------------------------------
# Phase 4 — full chain: trap -> spawn -> investigate -> enriched prompt
# ---------------------------------------------------------------------------

class TestPhase4Chain:
    def test_trap_spawns_agent_and_repl_renders_enriched_prompt(self):
        async def _go():
            probe = FakeProbe(facts={"top_process": "python(pid=999) cpu=95%", "mem_pct": 92})
            orch = ds.SubAgentOrchestrator(agent_factory=lambda: ds.DiagnosticSubAgent(probe=probe))
            orch.attach_to_trap_stream()                  # subscribe to SHADOW_ACTION_TRAPPED

            # A real organ traps a dangerous action under a dispatched ANOMALY signal.
            dispatcher = cr.EventActivationDispatcher()

            async def _kill():
                return "killed"

            async def organ(signal):
                await cr.shadow_guard_async(
                    "execute remediation 'restart' on 'worker-7'",
                    _kill, organ="SelfHealingOrchestrator",
                )

            dispatcher.register_organ("SelfHealingOrchestrator", organ, [cr.PressureSignalType.ANOMALY_DETECTED])
            await dispatcher.dispatch(
                cr.PressureSignal(cr.PressureSignalType.ANOMALY_DETECTED, "worker-7", cr.SignalEdge.RISING)
            )

            aid = cr.pending_shadow_action_ids()[-1]      # the trapped action awaiting endorsement
            payload = {
                "organ_name": "SelfHealingOrchestrator",
                "intended_action": "execute remediation 'restart' on 'worker-7'",
                "triggering_signal": "anomaly_detected:worker-7:rising",
                "action_id": aid,
            }
            # the REPL gateway awaits the ephemeral agent's findings, then renders.
            prompt = await ds.endorsement_prompt_with_diagnosis(orch, payload, wait_s=2.0)
            return aid, prompt, probe.calls

        aid, prompt, calls = asyncio.run(_go())
        assert calls == 1, "exactly one ephemeral agent investigated the trap"
        assert "python(pid=999)" in prompt, "root-cause analysis surfaced to the Host"
        assert aid in prompt and "[Y/N]" in prompt, "the contextualized endorsement decision"
