"""Slice 61 — closed-loop autoscore wake path.

Phase 1 closed-loop soak (bt-2026-06-02-011053): an autoscore-enabled run
(``JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED=true``) wrote NO ``results.jsonl``
row — ``evaluate_problem`` returned ``terminal_timeout`` / ``score=skipped``.

Root cause (verify-first, two compounding holes):

1. The closed-loop evaluator subscribes to the ``operation_terminal`` SSE, but
   that publish (``ide_observability_stream.publish_operation_terminal``, called
   at the orchestrator ``_record_ledger`` chokepoint for EVERY terminal state)
   is gated by ``JARVIS_OP_LIFECYCLE_SSE_ENABLED`` (§33.1 default-FALSE). Neither
   the soak script nor the autoscore path set it, so the SSE is never published
   for ANY op (NOT noop-specific — the noop's ``"failed"`` terminal IS in
   ``TERMINAL_OPERATION_STATES`` and would publish if the flag were on).
2. The ledger-authoritative fallback was disabled: ``_drive_parallel_evaluate``
   called ``parallel_evaluate`` WITHOUT ``operation_ledger``, so
   ``evaluate_problem`` had no ledger to fall back on at timeout
   ("when ``operation_ledger`` is None, TERMINAL_TIMEOUT is the only timeout
   outcome").

Fix:
* Backstop (flag-independent): thread ``operation_ledger`` from the boot hook
  through ``_inject_autoscore`` -> ``_drive_parallel_evaluate`` ->
  ``parallel_evaluate(operation_ledger=...)``. The noop terminal state
  ``"failed"`` is in the evaluator's terminal set, so the one-shot ledger
  fallback resolves it even with SSE off.
* Fast-wake observability: ``_inject_autoscore`` WARNS when autoscore is on but
  op-lifecycle SSE is off (the soak script sets the flag for the fast path).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from backend.core.ouroboros.governance.swe_bench_pro import harness_inject as hi
from backend.core.ouroboros.governance.swe_bench_pro import parallel_eval
from backend.core.ouroboros.governance.swe_bench_pro import dataset_loader

_REPO = Path(__file__).resolve().parents[2]


class _StubLedger:
    async def get_latest_state(self, op_id):  # noqa: D401
        return None


def _capturing_parallel_evaluate(captured):
    """Return a sync factory mimicking the ``parallel_evaluate`` async-gen
    function: it records the kwargs it was called with and yields nothing."""
    def _factory(specs, **kwargs):
        captured["kwargs"] = kwargs

        async def _agen():
            return
            yield  # pragma: no cover — empty async generator marker

        return _agen()

    return _factory


# ── Backstop: operation_ledger threading ───────────────────────────────────

def test_drive_forwards_operation_ledger(monkeypatch):
    captured = {}
    monkeypatch.setattr(parallel_eval, "parallel_evaluate",
                        _capturing_parallel_evaluate(captured))
    ledger = _StubLedger()
    asyncio.run(hi._drive_parallel_evaluate(
        [], intake_service=object(), operation_ledger=ledger))
    assert captured["kwargs"].get("operation_ledger") is ledger


def test_drive_defaults_ledger_none(monkeypatch):
    # Backward-compatible: no operation_ledger arg -> forwards None
    # (byte-identical to the pre-Slice-61 call when no ledger is available).
    captured = {}
    monkeypatch.setattr(parallel_eval, "parallel_evaluate",
                        _capturing_parallel_evaluate(captured))
    asyncio.run(hi._drive_parallel_evaluate([], intake_service=object()))
    assert captured["kwargs"].get("operation_ledger") is None


def test_inject_autoscore_threads_ledger(monkeypatch):
    monkeypatch.setattr(hi, "load_problem", lambda iid: (object(), None))
    monkeypatch.setenv("JARVIS_OP_LIFECYCLE_SSE_ENABLED", "true")
    captured = {}

    async def _stub_drive(specs, intake_service, **kwargs):
        captured["operation_ledger"] = kwargs.get("operation_ledger", "MISSING")

    monkeypatch.setattr(hi, "_drive_parallel_evaluate", _stub_drive)
    ledger = _StubLedger()

    async def _run():
        v = await hi._inject_autoscore(
            ["x"], intake_service=object(), operation_ledger=ledger)
        await asyncio.sleep(0)  # let the fire-and-forget driver task run
        await asyncio.sleep(0)
        return v

    verdict = asyncio.run(_run())
    assert verdict == hi.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE
    assert captured.get("operation_ledger") is ledger


def test_boot_hook_passes_ledger_to_autoscore(monkeypatch):
    monkeypatch.setattr(hi, "harness_inject_enabled", lambda: True)
    monkeypatch.setattr(dataset_loader, "swe_bench_pro_enabled", lambda: True)
    monkeypatch.setattr(hi, "_resolve_instance_ids", lambda: ["x"])
    monkeypatch.setattr(hi, "autoscore_enabled", lambda: True)
    captured = {}

    async def _stub_inject(instance_ids, intake_service, **kwargs):
        captured["operation_ledger"] = kwargs.get("operation_ledger", "MISSING")
        return hi.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE

    monkeypatch.setattr(hi, "_inject_autoscore", _stub_inject)
    ledger = _StubLedger()
    v = asyncio.run(hi.maybe_inject_swe_bench_at_boot(
        object(), operation_ledger=ledger))
    assert v == hi.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE
    assert captured.get("operation_ledger") is ledger


def test_boot_hook_backward_compatible_without_ledger(monkeypatch):
    # Legacy callers that don't pass operation_ledger keep working (None).
    monkeypatch.setattr(hi, "harness_inject_enabled", lambda: True)
    monkeypatch.setattr(dataset_loader, "swe_bench_pro_enabled", lambda: True)
    monkeypatch.setattr(hi, "_resolve_instance_ids", lambda: ["x"])
    monkeypatch.setattr(hi, "autoscore_enabled", lambda: True)
    captured = {}

    async def _stub_inject(instance_ids, intake_service, **kwargs):
        captured["operation_ledger"] = kwargs.get("operation_ledger", "MISSING")
        return hi.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE

    monkeypatch.setattr(hi, "_inject_autoscore", _stub_inject)
    v = asyncio.run(hi.maybe_inject_swe_bench_at_boot(object()))
    assert v == hi.SWEBenchProInjectionVerdict.INJECTED_AUTOSCORE
    assert captured.get("operation_ledger") is None


# ── Fast-wake observability: SSE coupling warning ──────────────────────────

def test_inject_autoscore_warns_when_sse_off(monkeypatch, caplog):
    monkeypatch.setattr(hi, "load_problem", lambda iid: (object(), None))
    monkeypatch.delenv("JARVIS_OP_LIFECYCLE_SSE_ENABLED", raising=False)

    async def _noop_drive(specs, intake_service, **kwargs):
        return

    monkeypatch.setattr(hi, "_drive_parallel_evaluate", _noop_drive)
    with caplog.at_level(logging.WARNING):
        asyncio.run(hi._inject_autoscore(["x"], intake_service=object()))
    assert any(
        "JARVIS_OP_LIFECYCLE_SSE_ENABLED" in r.getMessage()
        for r in caplog.records
    ), "expected a fast-wake coupling warning when SSE is off"


def test_inject_autoscore_no_warn_when_sse_on(monkeypatch, caplog):
    monkeypatch.setattr(hi, "load_problem", lambda iid: (object(), None))
    monkeypatch.setenv("JARVIS_OP_LIFECYCLE_SSE_ENABLED", "true")

    async def _noop_drive(specs, intake_service, **kwargs):
        return

    monkeypatch.setattr(hi, "_drive_parallel_evaluate", _noop_drive)
    with caplog.at_level(logging.WARNING):
        asyncio.run(hi._inject_autoscore(["x"], intake_service=object()))
    assert not any(
        "JARVIS_OP_LIFECYCLE_SSE_ENABLED" in r.getMessage()
        for r in caplog.records
    ), "no coupling warning expected when SSE is enabled"


# ── Wiring pins: guard against silent un-wiring (the original failure) ──────

def test_harness_passes_operation_ledger_at_call_site():
    src = (_REPO / "backend/core/ouroboros/battle_test/harness.py").read_text()
    # The boot-hook call must forward operation_ledger=... (the GLS ledger).
    m = re.search(
        r"maybe_inject_swe_bench_at_boot\((.*?)\)", src, re.DOTALL,
    )
    assert m, "maybe_inject_swe_bench_at_boot call not found in harness.py"
    assert "operation_ledger=" in m.group(1), (
        "harness must pass operation_ledger= to the boot hook (Slice 61 "
        "ledger-fallback backstop) — un-wiring this reintroduces the "
        "terminal_timeout/no-row failure"
    )


def test_soak_script_enables_op_lifecycle_sse():
    src = (_REPO / "scripts/swe_bench_pro_soak.sh").read_text()
    assert re.search(
        r"export\s+JARVIS_OP_LIFECYCLE_SSE_ENABLED=true", src,
    ), ("soak script must enable JARVIS_OP_LIFECYCLE_SSE_ENABLED for the "
        "closed-loop evaluator's fast (SSE) terminal wake")


def test_soak_script_enables_result_persistence():
    src = (_REPO / "scripts/swe_bench_pro_soak.sh").read_text()
    assert re.search(
        r"export\s+JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED=true", src,
    ), ("soak script must enable result persistence — EvaluationResultStore "
        "only appends the durable results.jsonl row when it is ON; without "
        "it every scored result (fixture AND real phase3) is lost on exit")
