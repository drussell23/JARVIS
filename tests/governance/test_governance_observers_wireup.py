"""Tier 0.5 batch 1 — boot-wire regression suite for the 3 dormant
Slice 5b observers (InvariantDrift / Coherence / CIGW).

Pins:
  * `_start_governance_observers` and `_stop_governance_observers`
    helpers exist on `GovernedLoopService`
  * Each observer is booted when its substrate master + sub-flag are
    BOTH on
  * Each observer is skipped when EITHER the substrate master OR the
    observer sub-flag is off (per-observer independence)
  * Observer ImportError swallowed; loop continues with remaining
    observers
  * Single observer .start() failure does NOT prevent others from
    booting
  * `_stop_governance_observers` calls `.stop()` on each booted
    observer; one stop failure does NOT prevent the others
  * BUG-FIX REGRESSION PIN: `_start_governance_observers` body MUST
    contain the import + .start() invocation for each of the 3
    observers — silent removal regresses the audit's dead-code
    finding
  * `start()` invokes `_start_governance_observers`; `stop()` invokes
    `_stop_governance_observers` — additive append discipline
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSyncObserver:
    """Mimics InvariantDriftObserver / CoherenceObserver — sync start,
    async stop. Records every call for assertion."""

    def __init__(self, name: str = "fake-sync") -> None:
        self.name = name
        self.start_calls = 0
        self.stop_calls = 0
        self.start_should_raise: bool = False
        self.stop_should_raise: bool = False

    def start(self) -> None:
        self.start_calls += 1
        if self.start_should_raise:
            raise RuntimeError(f"{self.name}.start exploded")

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_should_raise:
            raise RuntimeError(f"{self.name}.stop exploded")


class _FakeAsyncObserver:
    """Mimics CIGWObserver — async start + async stop."""

    def __init__(self, name: str = "fake-async") -> None:
        self.name = name
        self.start_calls = 0
        self.stop_calls = 0
        self.start_should_raise: bool = False
        self.stop_should_raise: bool = False

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_should_raise:
            raise RuntimeError(f"{self.name}.start exploded")

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        self.stop_calls += 1
        if self.stop_should_raise:
            raise RuntimeError(f"{self.name}.stop exploded")


def _make_service():
    """Construct a bare GovernedLoopService instance for method-level
    testing. We bypass __init__ since the full constructor wires
    significant infrastructure we don't need for observer boot tests.
    """
    svc = GovernedLoopService.__new__(GovernedLoopService)
    return svc


# ---------------------------------------------------------------------------
# §A — Helper presence + signature
# ---------------------------------------------------------------------------


class TestHelperMethodsPresent:
    def test_start_helper_exists(self):
        assert hasattr(
            GovernedLoopService,
            "_start_governance_observers",
        )

    def test_stop_helper_exists(self):
        assert hasattr(
            GovernedLoopService,
            "_stop_governance_observers",
        )

    def test_start_helper_is_async(self):
        import asyncio
        method = GovernedLoopService._start_governance_observers
        assert asyncio.iscoroutinefunction(method)

    def test_stop_helper_is_async(self):
        import asyncio
        method = GovernedLoopService._stop_governance_observers
        assert asyncio.iscoroutinefunction(method)


# ---------------------------------------------------------------------------
# §B — Boot semantics — all flags on
# ---------------------------------------------------------------------------


class TestAllObserversBoot:
    @pytest.mark.asyncio
    async def test_all_three_booted_when_flags_on(
        self, monkeypatch,
    ):
        # Master + sub-flags all on.
        for v in (
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_CIGW_ENABLED",
            "JARVIS_CIGW_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")

        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")

        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            await svc._start_governance_observers()

        assert drift.start_calls == 1
        assert coh.start_calls == 1
        assert cigw.start_calls == 1
        assert svc._invariant_drift_observer is drift
        assert svc._coherence_observer is coh
        assert svc._cigw_observer is cigw


# ---------------------------------------------------------------------------
# §C — Per-observer master-flag-off skips ONLY that observer
# ---------------------------------------------------------------------------


class TestPerObserverIndependence:
    @pytest.mark.asyncio
    async def test_drift_master_off_skips_drift_only(
        self, monkeypatch,
    ):
        # Drift master OFF; coherence + cigw ON.
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        for v in (
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_CIGW_ENABLED",
            "JARVIS_CIGW_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")

        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            await svc._start_governance_observers()

        assert drift.start_calls == 0
        assert coh.start_calls == 1
        assert cigw.start_calls == 1
        assert svc._invariant_drift_observer is None
        assert svc._coherence_observer is coh

    @pytest.mark.asyncio
    async def test_cigw_master_off_skips_cigw_only(
        self, monkeypatch,
    ):
        for v in (
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")
        monkeypatch.setenv("JARVIS_CIGW_ENABLED", "false")

        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            await svc._start_governance_observers()

        assert drift.start_calls == 1
        assert coh.start_calls == 1
        assert cigw.start_calls == 0
        assert svc._cigw_observer is None

    @pytest.mark.asyncio
    async def test_coherence_observer_subflag_off_skips_coherence(
        self, monkeypatch,
    ):
        for v in (
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_CIGW_ENABLED",
            "JARVIS_CIGW_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")
        monkeypatch.setenv(
            "JARVIS_COHERENCE_OBSERVER_ENABLED", "false",
        )

        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            await svc._start_governance_observers()

        assert coh.start_calls == 0
        assert svc._coherence_observer is None


# ---------------------------------------------------------------------------
# §D — Fail-open per observer
# ---------------------------------------------------------------------------


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_drift_start_exception_doesnt_block_others(
        self, monkeypatch,
    ):
        for v in (
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_CIGW_ENABLED",
            "JARVIS_CIGW_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")

        drift = _FakeSyncObserver("drift")
        drift.start_should_raise = True
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            # Must NOT raise.
            await svc._start_governance_observers()

        # Drift failed but did NOT block coh/cigw.
        assert svc._invariant_drift_observer is None
        assert coh.start_calls == 1
        assert cigw.start_calls == 1

    @pytest.mark.asyncio
    async def test_cigw_async_start_exception_doesnt_block_drift(
        self, monkeypatch,
    ):
        for v in (
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_CIGW_ENABLED",
            "JARVIS_CIGW_OBSERVER_ENABLED",
        ):
            monkeypatch.setenv(v, "true")

        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        cigw.start_should_raise = True
        svc = _make_service()
        with mock.patch(
            "backend.core.ouroboros.governance.invariant_drift_observer.get_default_observer",  # noqa: E501
            return_value=drift,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.coherence_observer.get_default_observer",  # noqa: E501
            return_value=coh,
        ), mock.patch(
            "backend.core.ouroboros.governance.verification.gradient_observer.CIGWObserver",  # noqa: E501
            return_value=cigw,
        ):
            await svc._start_governance_observers()

        assert drift.start_calls == 1
        assert coh.start_calls == 1
        assert svc._cigw_observer is None


# ---------------------------------------------------------------------------
# §E — Stop semantics
# ---------------------------------------------------------------------------


class TestStopSemantics:
    @pytest.mark.asyncio
    async def test_stop_calls_each_booted_observer(self):
        svc = _make_service()
        drift = _FakeSyncObserver("drift")
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc._invariant_drift_observer = drift
        svc._coherence_observer = coh
        svc._cigw_observer = cigw

        await svc._stop_governance_observers()

        assert drift.stop_calls == 1
        assert coh.stop_calls == 1
        assert cigw.stop_calls == 1

    @pytest.mark.asyncio
    async def test_stop_handles_none_observers(self):
        svc = _make_service()
        svc._invariant_drift_observer = None
        svc._coherence_observer = None
        svc._cigw_observer = None
        # Must NOT raise on all-None.
        await svc._stop_governance_observers()

    @pytest.mark.asyncio
    async def test_stop_one_failure_doesnt_block_others(self):
        svc = _make_service()
        drift = _FakeSyncObserver("drift")
        drift.stop_should_raise = True
        coh = _FakeSyncObserver("coh")
        cigw = _FakeAsyncObserver("cigw")
        svc._invariant_drift_observer = drift
        svc._coherence_observer = coh
        svc._cigw_observer = cigw

        # Must NOT raise.
        await svc._stop_governance_observers()

        assert coh.stop_calls == 1
        assert cigw.stop_calls == 1

    @pytest.mark.asyncio
    async def test_stop_handles_missing_attributes(self):
        # Service constructed without _start_governance_observers
        # ever called (e.g., aborted boot path) — stop must still
        # be safe.
        svc = _make_service()
        # No attributes set at all.
        await svc._stop_governance_observers()


# ---------------------------------------------------------------------------
# §F — BUG-FIX REGRESSION PIN (AST-level)
# ---------------------------------------------------------------------------


class TestBugFixRegressionPin:
    """The audit identified ~5,000 LOC + 2,000 tests of dead substrate
    because observers shipped graduated default-True but were never
    .start()ed in production. THIS PIN ensures the wire-up cannot be
    silently removed by a future refactor."""

    @staticmethod
    def _parse_method(method) -> ast.AST:
        src = textwrap.dedent(inspect.getsource(method))
        return ast.parse(src)

    def test_start_helper_imports_invariant_drift_observer(self):
        tree = self._parse_method(
            GovernedLoopService._start_governance_observers,
        )
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "invariant_drift_observer" in node.module
                ):
                    found = True
                    break
        assert found, (
            "BUG-FIX REGRESSION PIN: _start_governance_observers "
            "MUST import invariant_drift_observer — Tier 0.5 batch 1 "
            "wired this; silent removal regresses the audit's "
            "dead-code finding"
        )

    def test_start_helper_imports_coherence_observer(self):
        tree = self._parse_method(
            GovernedLoopService._start_governance_observers,
        )
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "coherence_observer" in node.module
                ):
                    found = True
                    break
        assert found, (
            "BUG-FIX REGRESSION PIN: _start_governance_observers "
            "MUST import coherence_observer — Tier 0.5 batch 1 "
            "wired this; silent removal regresses the audit's "
            "dead-code finding"
        )

    def test_start_helper_imports_gradient_observer(self):
        tree = self._parse_method(
            GovernedLoopService._start_governance_observers,
        )
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "gradient_observer" in node.module
                ):
                    found = True
                    break
        assert found, (
            "BUG-FIX REGRESSION PIN: _start_governance_observers "
            "MUST import gradient_observer (CIGWObserver) — "
            "Tier 0.5 batch 1 wired this; silent removal regresses "
            "the audit's dead-code finding"
        )

    def test_start_helper_calls_three_starts(self):
        # Body MUST contain three .start() invocations (one per
        # observer). Less than 3 = silent removal.
        src = textwrap.dedent(
            inspect.getsource(
                GovernedLoopService._start_governance_observers,
            )
        )
        tree = ast.parse(src)
        start_call_count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "start":
                    start_call_count += 1
        assert start_call_count >= 3, (
            f"BUG-FIX REGRESSION PIN: _start_governance_observers "
            f"contains only {start_call_count} .start() calls; "
            f"expected 3 (one per observer). At least one observer "
            "wire-up was removed."
        )


# ---------------------------------------------------------------------------
# §G — Service.start / .stop call the helpers
# ---------------------------------------------------------------------------


class TestServiceLifecycleInvokesHelpers:
    """Pin that GovernedLoopService.start invokes
    _start_governance_observers and .stop invokes
    _stop_governance_observers — the additive-append discipline that
    makes the wire-up actually fire."""

    def test_start_method_invokes_start_helper(self):
        src = textwrap.dedent(
            inspect.getsource(GovernedLoopService.start)
        )
        # AST find: any call whose attr is '_start_governance_observers'
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "_start_governance_observers"
                ):
                    found = True
                    break
        assert found, (
            "GovernedLoopService.start MUST invoke "
            "_start_governance_observers — Tier 0.5 batch 1 "
            "wire-up was removed from start()"
        )

    def test_stop_method_invokes_stop_helper(self):
        src = textwrap.dedent(
            inspect.getsource(GovernedLoopService.stop)
        )
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "_stop_governance_observers"
                ):
                    found = True
                    break
        assert found, (
            "GovernedLoopService.stop MUST invoke "
            "_stop_governance_observers — Tier 0.5 batch 1 "
            "shutdown wire-up was removed from stop()"
        )
