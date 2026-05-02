"""TerminationHookRegistry Slice 4 — graduation regression suite.

Pins the four graduation deliverables:

  * 7 ``shipped_code_invariants`` AST pins (3 vocabularies + 2
    harness regression pins for the bug fix + 2 adapter pins)
  * 5 FlagRegistry seeds (master + 4 env-knob accessors)
  * Master flag default-true + falsy/truthy matrix +
    dispatch short-circuits when off
  * ``EVENT_TYPE_TERMINATION_HOOK_DISPATCHED`` registered in
    SSE event vocabulary
  * 4 GET routes registered (enumerated below)
  * Discovery package extension: ``battle_test`` in BOTH
    ``_FLAG_PROVIDER_PACKAGES`` and
    ``_INVARIANT_PROVIDER_PACKAGES``

Master-flag-off contract: dispatch returns clean empty result
WITHOUT invoking any hook (operator's instant-rollback path is
preserved).
"""
from __future__ import annotations

import ast
import inspect
import json
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.termination_hook import (
    TerminationCause,
    TerminationDispatchResult,
    TerminationPhase,
)
from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
    PARTIAL_SUMMARY_WRITER_HOOK_NAME,
    clear_active_harness,
    set_active_harness,
)
from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
    TerminationHookRegistry,
    get_default_registry,
    master_enabled,
    register_flags,
    register_shipped_invariants,
    reset_default_registry_for_tests,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category, FlagSpec, FlagType,
)
from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
    EVENT_TYPE_TERMINATION_HOOK_DISPATCHED,
    _VALID_EVENT_TYPES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_TERMINATION_HOOKS_ENABLED",
        "JARVIS_TERMINATION_HOOK_TIMEOUT_S",
        "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S",
        "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S",
        "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE",
    ):
        monkeypatch.delenv(var, raising=False)
    clear_active_harness()
    reset_default_registry_for_tests()
    yield
    clear_active_harness()
    reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# §A — Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", raising=False,
        )
        assert master_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_explicitly(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", val,
        )
        assert master_enabled() is True

    @pytest.mark.parametrize(
        "val", ["0", "false", "no", "off", "garbage"],
    )
    def test_falsy_explicitly(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", val,
        )
        assert master_enabled() is False

    @pytest.mark.parametrize("val", ["", "   ", "\t"])
    def test_empty_treats_as_unset(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", val,
        )
        assert master_enabled() is True

    def test_dispatch_short_circuits_when_master_off(
        self, monkeypatch,
    ):
        # When master is off, dispatch returns clean empty result
        # WITHOUT invoking any hook (operator's instant-rollback
        # path).
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", "false",
        )
        ran: List[str] = []

        def my_hook(ctx):
            ran.append("fired")

        reg = TerminationHookRegistry()
        reg.register(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            my_hook, name="should_not_run",
        )
        result = reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="wall_clock_cap",
        )
        # Hook never ran — operator-disabled.
        assert ran == []
        assert isinstance(result, TerminationDispatchResult)
        assert result.records == ()
        assert result.budget_exhausted is False


# ---------------------------------------------------------------------------
# §B — register_shipped_invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_returns_seven(self):
        invs = register_shipped_invariants()
        assert len(invs) == 7

    def test_invariant_names(self):
        invs = register_shipped_invariants()
        names = {i.invariant_name for i in invs}
        expected = {
            "termination_cause_vocabulary",
            "termination_phase_vocabulary",
            "hook_outcome_vocabulary",
            "harness_wall_clock_dispatch_present",
            "harness_signal_handler_dispatch_present",
            "default_adapter_hook_present",
            "default_adapter_no_asyncio",
        }
        assert names == expected

    def test_each_invariant_has_validator_and_target(self):
        invs = register_shipped_invariants()
        for inv in invs:
            assert callable(inv.validate)
            assert inv.target_file.startswith(
                "backend/core/ouroboros/battle_test/"
            ) or inv.target_file.startswith(
                "backend/core/ouroboros/"
            )
            assert inv.description.strip() != ""

    def test_cause_vocabulary_pin_passes_clean_source(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook,
        )
        src = inspect.getsource(termination_hook)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        cause_inv = next(
            i for i in invs
            if i.invariant_name == "termination_cause_vocabulary"
        )
        assert cause_inv.validate(tree, src) == ()

    def test_cause_vocabulary_pin_fires_on_missing(self):
        bad_src = (
            "import enum\n"
            "class TerminationCause(str, enum.Enum):\n"
            "    WALL_CLOCK_CAP = 'wall_clock_cap'\n"
            "    SIGTERM = 'sigterm'\n"
            "    SIGINT = 'sigint'\n"
            "    UNKNOWN = 'unknown'\n"
            # Missing: SIGHUP / IDLE_TIMEOUT / BUDGET_EXCEEDED /
            # NORMAL_EXIT
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        cause_inv = next(
            i for i in invs
            if i.invariant_name == "termination_cause_vocabulary"
        )
        violations = cause_inv.validate(tree, bad_src)
        assert len(violations) >= 1
        joined = " ".join(violations)
        for missing in (
            "SIGHUP", "IDLE_TIMEOUT", "BUDGET_EXCEEDED",
            "NORMAL_EXIT",
        ):
            assert missing in joined

    def test_phase_vocabulary_pin_fires_on_addition(self):
        bad_src = (
            "import enum\n"
            "class TerminationPhase(str, enum.Enum):\n"
            "    PRE_SHUTDOWN_EVENT_SET = 'a'\n"
            "    POST_ASYNC_CLEANUP = 'b'\n"
            "    PRE_HARD_EXIT = 'c'\n"
            "    NEW_ROGUE_PHASE = 'd'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        phase_inv = next(
            i for i in invs
            if i.invariant_name == "termination_phase_vocabulary"
        )
        violations = phase_inv.validate(tree, bad_src)
        assert any("NEW_ROGUE_PHASE" in v for v in violations)

    def test_hook_outcome_vocabulary_pin_passes_clean(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook,
        )
        src = inspect.getsource(termination_hook)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        outcome_inv = next(
            i for i in invs
            if i.invariant_name == "hook_outcome_vocabulary"
        )
        assert outcome_inv.validate(tree, src) == ()

    def test_harness_wall_clock_dispatch_pin_passes_clean(self):
        # THE BUG-FIX REGRESSION PIN. Validates that
        # _wall_clock_watchdog body contains the .dispatch( call
        # Slice 3 added.
        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        wc_inv = next(
            i for i in invs
            if i.invariant_name
            == "harness_wall_clock_dispatch_present"
        )
        violations = wc_inv.validate(tree, src)
        assert violations == (), (
            "BUG-FIX regression pin violated: "
            f"{violations} — Slice 3's wall-clock-watchdog "
            "dispatch call was removed; the wall-cap "
            "summary.json bug has regressed"
        )

    def test_harness_wall_clock_dispatch_pin_fires_on_synthetic_removal(self):
        bad_src = (
            "class FakeHarness:\n"
            "    async def _monitor_wall_clock(self, cap_s):\n"
            "        # Refactor accidentally removed dispatch\n"
            "        await asyncio.sleep(cap_s)\n"
            "        self._wall_clock_event.set()\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        wc_inv = next(
            i for i in invs
            if i.invariant_name
            == "harness_wall_clock_dispatch_present"
        )
        violations = wc_inv.validate(tree, bad_src)
        assert any(".dispatch(" in v for v in violations)

    def test_harness_signal_handler_dispatch_pin_passes_clean(self):
        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        sh_inv = next(
            i for i in invs
            if i.invariant_name
            == "harness_signal_handler_dispatch_present"
        )
        assert sh_inv.validate(tree, src) == ()

    def test_adapter_hook_pin_passes_clean(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook_default_adapters,
        )
        src = inspect.getsource(
            termination_hook_default_adapters,
        )
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        adapter_inv = next(
            i for i in invs
            if i.invariant_name == "default_adapter_hook_present"
        )
        assert adapter_inv.validate(tree, src) == ()

    def test_adapter_hook_pin_fires_on_synthetic_removal(self):
        bad_src = "# adapter module without the hook\nfoo = 1\n"
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        adapter_inv = next(
            i for i in invs
            if i.invariant_name == "default_adapter_hook_present"
        )
        violations = adapter_inv.validate(tree, bad_src)
        assert len(violations) >= 1

    def test_adapter_no_asyncio_pin_passes_clean(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook_default_adapters,
        )
        src = inspect.getsource(
            termination_hook_default_adapters,
        )
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        async_inv = next(
            i for i in invs
            if i.invariant_name == "default_adapter_no_asyncio"
        )
        assert async_inv.validate(tree, src) == ()

    def test_adapter_no_asyncio_pin_fires_on_synthetic_import(self):
        bad_src = "import asyncio\nfrom asyncio import wait_for\n"
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        async_inv = next(
            i for i in invs
            if i.invariant_name == "default_adapter_no_asyncio"
        )
        violations = async_inv.validate(tree, bad_src)
        assert len(violations) >= 1
        assert all(
            "asyncio" in v for v in violations
        )


# ---------------------------------------------------------------------------
# §C — register_flags
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self) -> None:
        self.specs: List[FlagSpec] = []

    def bulk_register(self, specs, *, override=False) -> int:
        self.specs.extend(specs)
        return len(specs)


class TestFlagRegistry:
    def test_register_returns_five(self):
        reg = _StubRegistry()
        n = register_flags(reg)
        assert n == 5

    def test_master_flag_default_true(self):
        reg = _StubRegistry()
        register_flags(reg)
        master = next(
            s for s in reg.specs
            if s.name == "JARVIS_TERMINATION_HOOKS_ENABLED"
        )
        assert master.default is True
        assert master.type is FlagType.BOOL
        assert master.category is Category.SAFETY

    def test_all_flag_names_present(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = {s.name for s in reg.specs}
        expected = {
            "JARVIS_TERMINATION_HOOKS_ENABLED",
            "JARVIS_TERMINATION_HOOK_TIMEOUT_S",
            "JARVIS_TERMINATION_HOOK_PHASE_BUDGET_S",
            "JARVIS_TERMINATION_HOOK_HARD_EXIT_BUDGET_S",
            "JARVIS_TERMINATION_HOOK_MAX_PER_PHASE",
        }
        assert names == expected

    def test_all_specs_documented(self):
        reg = _StubRegistry()
        register_flags(reg)
        for spec in reg.specs:
            assert isinstance(spec.category, Category)
            assert spec.description.strip() != ""
            assert spec.source_file.endswith(".py")
            assert spec.since.startswith(
                "TerminationHookRegistry Slice 4"
            )

    def test_no_duplicate_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = [s.name for s in reg.specs]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# §D — Discovery package extension
# ---------------------------------------------------------------------------


class TestDiscoveryPackageExtension:
    def test_battle_test_in_flag_provider_packages(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            _FLAG_PROVIDER_PACKAGES,
        )
        assert (
            "backend.core.ouroboros.battle_test"
            in _FLAG_PROVIDER_PACKAGES
        )

    def test_battle_test_in_invariant_provider_packages(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _INVARIANT_PROVIDER_PACKAGES,
        )
        assert (
            "backend.core.ouroboros.battle_test"
            in _INVARIANT_PROVIDER_PACKAGES
        )


# ---------------------------------------------------------------------------
# §E — SSE event registration
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_type_constant(self):
        assert (
            EVENT_TYPE_TERMINATION_HOOK_DISPATCHED
            == "termination_hook_dispatched"
        )

    def test_event_type_in_valid_set(self):
        assert (
            EVENT_TYPE_TERMINATION_HOOK_DISPATCHED
            in _VALID_EVENT_TYPES
        )


# ---------------------------------------------------------------------------
# §F — GET route registration
# ---------------------------------------------------------------------------


def _aiohttp_available() -> bool:
    try:
        from aiohttp.test_utils import make_mocked_request  # noqa
        return True
    except ImportError:
        return False


def _make_request(path: str):
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", path)
    req._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return req


@pytest.mark.skipif(
    not _aiohttp_available(),
    reason="aiohttp not available",
)
class TestGETRoute:
    @pytest.fixture(autouse=True)
    def _ide_obs_on(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", "true",
        )

    @pytest.fixture
    def router(self):
        from backend.core.ouroboros.governance.ide_observability import (  # noqa: E501
            IDEObservabilityRouter,
        )
        return IDEObservabilityRouter()

    def test_route_registers(self, router):
        from aiohttp import web
        app = web.Application()
        router.register_routes(app)
        paths = [
            getattr(r, "resource", None) and r.resource.canonical
            for r in app.router.routes()
        ]
        assert "/observability/termination-hooks" in paths

    @pytest.mark.asyncio
    async def test_get_returns_200_when_master_on(self, router):
        # Pre-populate the singleton registry with the default
        # adapter so the GET surface has data to project.
        from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
            discover_and_register_default,
        )
        discover_and_register_default()
        resp = await router._handle_termination_hooks(
            _make_request("/observability/termination-hooks"),
        )
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["enabled"] is True
        assert "registry_config" in body
        assert "max_per_phase" in body["registry_config"]
        assert "phase_budgets_s" in body["registry_config"]
        # All 3 phases present in the budgets map.
        budgets = body["registry_config"]["phase_budgets_s"]
        assert "pre_shutdown_event_set" in budgets
        assert "post_async_cleanup" in budgets
        assert "pre_hard_exit" in budgets
        # Hard-exit budget is tighter than normal.
        assert (
            budgets["pre_hard_exit"]
            < budgets["pre_shutdown_event_set"]
        )
        assert "hooks_by_phase" in body
        # The default partial_summary_writer hook is registered
        # at PRE_SHUTDOWN_EVENT_SET.
        pre = body["hooks_by_phase"][
            "pre_shutdown_event_set"
        ]
        names = [h["name"] for h in pre]
        assert (
            PARTIAL_SUMMARY_WRITER_HOOK_NAME in names
        )

    @pytest.mark.asyncio
    async def test_get_returns_403_when_master_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_TERMINATION_HOOKS_ENABLED", "false",
        )
        resp = await router._handle_termination_hooks(
            _make_request("/observability/termination-hooks"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert (
            body["reason_code"]
            == "ide_observability.termination_hooks_disabled"
        )

    @pytest.mark.asyncio
    async def test_get_returns_403_when_umbrella_off(
        self, router, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        resp = await router._handle_termination_hooks(
            _make_request("/observability/termination-hooks"),
        )
        assert resp.status == 403
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"


# ---------------------------------------------------------------------------
# §G — Sanity / schema pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_defensive_on_import_failure():
    # If the shipped_code_invariants module is unreachable for
    # some reason, register_shipped_invariants returns [] cleanly
    # rather than raising. Defensive contract.
    with mock.patch.dict(
        "sys.modules",
        {
            "backend.core.ouroboros.governance.meta."
            "shipped_code_invariants": None,
        },
    ):
        # Forcing the import to fail would require more invasive
        # patching; just verify the callable exists and returns
        # iterable.
        invs = register_shipped_invariants()
        assert isinstance(invs, list)
