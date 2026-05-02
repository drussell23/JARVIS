"""TerminationHookRegistry Slice 3 — harness wire-up regression.

Pins the migration of the harness's signal-driven + wall-clock-
driven termination paths to dispatch through the
TerminationHookRegistry. The single most important invariant is
**pristine equivalency**: the migrated signal path MUST invoke
``_atexit_fallback_write`` with byte-identical kwargs to the
pre-migration direct call, so downstream summary.json parsers
(LastSessionSummary etc.) see no change.

Strict directives validated:

  * Pristine equivalency: signal-path migration produces an
    `_atexit_fallback_write(session_outcome="incomplete_kill")`
    call indistinguishable from the pre-migration direct call.
  * Wall-cap-path bug fix: dispatch fires BEFORE the
    BoundedShutdownWatchdog arms, lands a partial summary on
    disk that the pre-migration path NEVER wrote.
  * Sync-first preserved: every dispatch path uses the registry's
    threading-only dispatcher (the AST pin from Slice 2 already
    asserts this on the registry module; this slice's tests
    confirm the harness invokes through that surface).
  * Discovery contract: the default-adapter module exposes
    ``register_termination_hooks`` and the discovery loop
    installs the partial_summary_writer hook at boot.

Covers:

  §A   Adapter module — schema + cause→session_outcome map
  §B   set/get/clear active harness — singleton lifecycle
  §C   partial_summary_writer_hook — happy path invokes
       _atexit_fallback_write with the documented kwargs
  §D   Cause matrix → session_outcome value pin
  §E   No active harness → silent no-op (no crash, no write)
  §F   Stop-reason stamping — preserves earlier classification
  §G   register_termination_hooks discovery contract +
       idempotency on duplicate
  §H   PRISTINE EQUIVALENCY — signal handler migration produces
       byte-identical _atexit_fallback_write call args
  §I   THE BUG FIX — wall-cap dispatch lands a summary write
       (paired with the spy-based byte-equivalency check above)
  §J   AST authority pin — adapter module no asyncio import
"""
from __future__ import annotations

import ast
import inspect
from typing import Any, List, Optional
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.termination_hook import (
    TerminationCause,
    TerminationHookContext,
    TerminationPhase,
)
from backend.core.ouroboros.battle_test.termination_hook_default_adapters import (  # noqa: E501
    PARTIAL_SUMMARY_WRITER_HOOK_NAME,
    TERMINATION_HOOK_DEFAULT_ADAPTERS_SCHEMA_VERSION,
    _CAUSE_TO_SESSION_OUTCOME,
    clear_active_harness,
    get_active_harness,
    partial_summary_writer_hook,
    register_termination_hooks,
    set_active_harness,
)
from backend.core.ouroboros.battle_test.termination_hook_registry import (  # noqa: E501
    DuplicateHookNameError,
    TerminationHookRegistry,
    reset_default_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubHarness:
    """Minimal harness shape the adapter introspects.

    Captures every call to _atexit_fallback_write with its exact
    kwargs so the byte-equivalency tests can assert on them."""

    def __init__(
        self,
        *,
        stop_reason: str = "unknown",
    ) -> None:
        self._stop_reason: Any = stop_reason
        self._calls: List[dict] = []

    def _atexit_fallback_write(
        self, session_outcome: Optional[str] = None,
    ) -> None:
        # Capture the call signature exactly — kwargs and all.
        self._calls.append({
            "session_outcome": session_outcome,
            "stop_reason_at_call": self._stop_reason,
        })


@pytest.fixture(autouse=True)
def _isolate():
    clear_active_harness()
    reset_default_registry_for_tests()
    yield
    clear_active_harness()
    reset_default_registry_for_tests()


def _ctx(
    *,
    cause: TerminationCause = TerminationCause.WALL_CLOCK_CAP,
    phase: TerminationPhase = (
        TerminationPhase.PRE_SHUTDOWN_EVENT_SET
    ),
    session_dir: str = "/tmp/test",
    started_at: float = 1000.0,
    stop_reason: str = "",
) -> TerminationHookContext:
    return TerminationHookContext(
        cause=cause, phase=phase,
        session_dir=session_dir,
        started_at=started_at,
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# §A — Adapter module schema + map
# ---------------------------------------------------------------------------


class TestAdapterSchema:
    def test_schema_version_pin(self):
        assert (
            TERMINATION_HOOK_DEFAULT_ADAPTERS_SCHEMA_VERSION
            == "termination_hook_default_adapters.1"
        )

    def test_partial_summary_writer_name_pin(self):
        # Slice 4's AST validator asserts this name persists
        # across refactors.
        assert (
            PARTIAL_SUMMARY_WRITER_HOOK_NAME
            == "partial_summary_writer"
        )

    def test_cause_map_covers_all_8_causes(self):
        # Every TerminationCause must have an explicit mapping —
        # silent additions to TerminationCause MUST be classified
        # against this map by the next contributor.
        for cause in TerminationCause:
            assert cause in _CAUSE_TO_SESSION_OUTCOME, (
                f"cause {cause} missing from "
                f"_CAUSE_TO_SESSION_OUTCOME"
            )

    def test_cause_map_signal_paths_pin_to_incomplete_kill(self):
        # Pristine-equivalency invariant: the three signal causes
        # MUST map to "incomplete_kill" — same value the pre-
        # migration signal handler passed to the writer at line
        # 3290.
        for sig in (
            TerminationCause.SIGTERM,
            TerminationCause.SIGINT,
            TerminationCause.SIGHUP,
        ):
            assert (
                _CAUSE_TO_SESSION_OUTCOME[sig]
                == "incomplete_kill"
            )

    def test_normal_exit_maps_to_none(self):
        # NORMAL_EXIT preserves the writer's default-args call
        # path (no session_outcome stamp). Pre-migration: this
        # is what the legacy `signal_name=None` test-harness
        # branch did.
        assert (
            _CAUSE_TO_SESSION_OUTCOME[
                TerminationCause.NORMAL_EXIT
            ]
            is None
        )


# ---------------------------------------------------------------------------
# §B — Active-harness singleton
# ---------------------------------------------------------------------------


class TestActiveHarnessSingleton:
    def test_get_returns_none_when_unset(self):
        assert get_active_harness() is None

    def test_set_and_get_round_trip(self):
        h = _StubHarness()
        set_active_harness(h)
        assert get_active_harness() is h

    def test_set_same_idempotent(self):
        h = _StubHarness()
        set_active_harness(h)
        set_active_harness(h)  # no warning, no swap
        assert get_active_harness() is h

    def test_set_different_replaces(self):
        h1 = _StubHarness()
        h2 = _StubHarness()
        set_active_harness(h1)
        set_active_harness(h2)
        assert get_active_harness() is h2

    def test_clear_drops_singleton(self):
        h = _StubHarness()
        set_active_harness(h)
        clear_active_harness()
        assert get_active_harness() is None


# ---------------------------------------------------------------------------
# §C — Hook happy path
# ---------------------------------------------------------------------------


class TestHookHappyPath:
    def test_hook_invokes_writer_with_documented_kwargs(self):
        h = _StubHarness()
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.SIGTERM,
            stop_reason="sigterm",
        ))
        assert len(h._calls) == 1
        # Pristine-equivalency: same kwarg the pre-migration
        # signal handler passed at harness.py:3290.
        assert h._calls[0]["session_outcome"] == "incomplete_kill"


# ---------------------------------------------------------------------------
# §D — Cause→session_outcome matrix
# ---------------------------------------------------------------------------


class TestCauseMatrix:
    @pytest.mark.parametrize("cause,expected", [
        (TerminationCause.SIGTERM, "incomplete_kill"),
        (TerminationCause.SIGINT, "incomplete_kill"),
        (TerminationCause.SIGHUP, "incomplete_kill"),
        (TerminationCause.WALL_CLOCK_CAP, "incomplete_kill"),
        (TerminationCause.IDLE_TIMEOUT, "incomplete_kill"),
        (TerminationCause.BUDGET_EXCEEDED, "incomplete_kill"),
        (TerminationCause.UNKNOWN, "incomplete_kill"),
    ])
    def test_cause_maps_to_session_outcome(self, cause, expected):
        h = _StubHarness()
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(cause=cause))
        assert h._calls[0]["session_outcome"] == expected

    def test_normal_exit_calls_writer_with_no_kwargs(self):
        # NORMAL_EXIT branch — preserves the legacy
        # signal_name=None code path's writer() call (no
        # session_outcome stamp).
        h = _StubHarness()
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.NORMAL_EXIT,
        ))
        # session_outcome not passed → stub records None.
        assert h._calls[0]["session_outcome"] is None


# ---------------------------------------------------------------------------
# §E — No active harness → silent no-op
# ---------------------------------------------------------------------------


class TestNoHarness:
    def test_hook_silent_when_no_harness(self):
        clear_active_harness()
        # Must not raise, must not produce any side effect.
        partial_summary_writer_hook(_ctx())

    def test_hook_silent_when_writer_missing(self):
        # Defensive: harness present but lacks the writer method
        # (synthetic test class). Must not crash.
        class _BareHarness:
            _stop_reason = "unknown"
        set_active_harness(_BareHarness())
        partial_summary_writer_hook(_ctx())  # no raise


# ---------------------------------------------------------------------------
# §F — stop_reason stamping discipline
# ---------------------------------------------------------------------------


class TestStopReasonStamping:
    def test_stamps_when_unknown(self):
        h = _StubHarness(stop_reason="unknown")
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.WALL_CLOCK_CAP,
            stop_reason="wall_clock_cap",
        ))
        assert h._stop_reason == "wall_clock_cap"

    def test_preserves_earlier_classification(self):
        # If a path already classified (e.g. wall-cap stamping
        # "wall_clock_cap" before dispatch fires), the adapter
        # MUST NOT clobber it. This is the same predicate
        # signal handler uses at lines 3286-3287.
        h = _StubHarness(stop_reason="wall_clock_cap")
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.UNKNOWN,
            stop_reason="some_other_value",
        ))
        # Earlier classification preserved.
        assert h._stop_reason == "wall_clock_cap"

    def test_falls_back_to_cause_value_when_ctx_empty(self):
        h = _StubHarness(stop_reason="unknown")
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.SIGINT,
            stop_reason="",  # caller didn't classify
        ))
        # Adapter falls back to ctx.cause.value.
        assert h._stop_reason == "sigint"


# ---------------------------------------------------------------------------
# §G — Discovery + idempotency
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_register_installs_one_hook(self):
        reg = TerminationHookRegistry()
        n = register_termination_hooks(reg)
        assert n == 1
        # Hook lands at PRE_SHUTDOWN_EVENT_SET with the
        # documented name.
        bucket = reg.for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        )
        assert len(bucket) == 1
        assert bucket[0].name == PARTIAL_SUMMARY_WRITER_HOOK_NAME

    def test_register_priority_10_runs_first(self):
        # Pin: partial_summary_writer is priority 10 → runs
        # before any operator-defined hook (default 100).
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        bucket = reg.for_phase(
            TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
        )
        assert bucket[0].priority == 10

    def test_register_idempotent_on_duplicate(self):
        # Re-calling MUST NOT raise — discovery loop swallows
        # DuplicateHookNameError so re-imports don't break boot.
        reg = TerminationHookRegistry()
        n1 = register_termination_hooks(reg)
        n2 = register_termination_hooks(reg)
        assert n1 == 1
        assert n2 == 0  # already registered


# ---------------------------------------------------------------------------
# §H — PRISTINE EQUIVALENCY (the user's strict directive)
# ---------------------------------------------------------------------------


class TestPristineEquivalency:
    """For each pre-migration signal-handler call site, the
    post-migration registry-dispatch path MUST produce a
    byte-identical _atexit_fallback_write call. The stub harness
    records exact call kwargs; these tests assert the kwargs
    survive the registry round-trip."""

    def test_sigterm_path_byte_equivalent(self):
        # Pre-migration: harness.py:3290 called
        # self._atexit_fallback_write(session_outcome="incomplete_kill")
        # Post-migration: dispatcher → adapter → same call.
        h = _StubHarness()
        set_active_harness(h)
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.SIGTERM,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="sigterm",
        )
        assert len(h._calls) == 1
        assert h._calls[0]["session_outcome"] == "incomplete_kill"

    def test_sigint_path_byte_equivalent(self):
        h = _StubHarness()
        set_active_harness(h)
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.SIGINT,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="sigint",
        )
        assert len(h._calls) == 1
        assert h._calls[0]["session_outcome"] == "incomplete_kill"

    def test_sighup_path_byte_equivalent(self):
        h = _StubHarness()
        set_active_harness(h)
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.SIGHUP,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="sighup",
        )
        assert len(h._calls) == 1
        assert h._calls[0]["session_outcome"] == "incomplete_kill"

    def test_legacy_signal_name_none_path_byte_equivalent(self):
        # Pre-migration test-harness path
        # (signal_name=None at line 3292) called
        # self._atexit_fallback_write() with NO kwargs. Mapped
        # to NORMAL_EXIT in the adapter; writer() called with
        # no session_outcome.
        h = _StubHarness()
        set_active_harness(h)
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.NORMAL_EXIT,
            session_dir="/tmp/test",
            started_at=1000.0,
        )
        assert len(h._calls) == 1
        assert h._calls[0]["session_outcome"] is None

    def test_writer_idempotent_via_summary_written_gate(self):
        # The pre-migration writer's _summary_written gate keeps
        # double-writes from happening (clean async path won →
        # fallback no-op). The adapter MUST pass through that
        # idempotency — by calling the SAME writer, the gate
        # naturally applies.
        class _GatedHarness(_StubHarness):
            def __init__(self):
                super().__init__()
                self._summary_written = True
                # Override writer to honor the gate (matches
                # production behavior at harness.py:295-296).
            def _atexit_fallback_write(
                self, session_outcome=None,
            ):
                if self._summary_written:
                    return  # Gate fired — no-op
                super()._atexit_fallback_write(
                    session_outcome=session_outcome,
                )
        h = _GatedHarness()
        set_active_harness(h)
        partial_summary_writer_hook(_ctx(
            cause=TerminationCause.SIGTERM,
        ))
        # Writer was called but the gate suppressed the write.
        # Slice 3 doesn't bypass the gate — production behavior
        # preserved.
        assert h._calls == []


# ---------------------------------------------------------------------------
# §I — THE BUG FIX (wall-cap path — pre-migration wrote nothing)
# ---------------------------------------------------------------------------


class TestWallCapBugFix:
    """Pre-migration: the wall-clock watchdog at harness.py:3640+
    set the wall_clock_event + armed BoundedShutdownWatchdog
    WITHOUT any sync write. Result on bt-2026-05-02-203805:
    no summary.json on disk after os._exit(75).

    Post-migration: dispatch fires BEFORE the watchdog arms.
    The registered hook lands the partial summary."""

    def test_wall_cap_dispatch_invokes_writer(self):
        h = _StubHarness()
        set_active_harness(h)
        reg = TerminationHookRegistry()
        register_termination_hooks(reg)
        # Dispatch the wall-cap cause — same call site Slice 3
        # added to _wall_clock_watchdog.
        reg.dispatch(
            phase=TerminationPhase.PRE_SHUTDOWN_EVENT_SET,
            cause=TerminationCause.WALL_CLOCK_CAP,
            session_dir="/tmp/test",
            started_at=1000.0,
            stop_reason="wall_clock_cap",
        )
        # THE bug fix: writer was called. Pre-migration: this
        # would have been zero calls.
        assert len(h._calls) == 1
        assert h._calls[0]["session_outcome"] == "incomplete_kill"

    def test_wall_cap_classified_session_outcome(self):
        # The session_outcome value distinguishes
        # complete-vs-interrupted for LastSessionSummary (the
        # downstream parser). Pin: WALL_CLOCK_CAP yields
        # "incomplete_kill" so audit tooling treats it the same
        # as a signal-driven shutdown (both are interrupted).
        assert (
            _CAUSE_TO_SESSION_OUTCOME[
                TerminationCause.WALL_CLOCK_CAP
            ]
            == "incomplete_kill"
        )

    def test_idle_and_budget_paths_also_write(self):
        # Slice 3 plan called for migrating the idle watchdog +
        # budget waiter to the same dispatch pattern. Even if
        # the harness doesn't yet wire them (deferred to a
        # follow-up), the adapter MUST be ready to handle them
        # — pin the cause map.
        for cause in (
            TerminationCause.IDLE_TIMEOUT,
            TerminationCause.BUDGET_EXCEEDED,
        ):
            h = _StubHarness()
            set_active_harness(h)
            partial_summary_writer_hook(_ctx(cause=cause))
            assert len(h._calls) == 1
            assert (
                h._calls[0]["session_outcome"]
                == "incomplete_kill"
            )
            clear_active_harness()


# ---------------------------------------------------------------------------
# §J — AST authority pin
# ---------------------------------------------------------------------------


class TestAuthorityPin:
    def test_adapter_module_no_asyncio_import(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook_default_adapters,
        )
        src = inspect.getsource(termination_hook_default_adapters)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    "asyncio" not in node.module.split(".")
                ), f"forbidden asyncio import: {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        "asyncio" not in alias.name.split(".")
                    ), f"forbidden asyncio: {alias.name}"

    def test_adapter_module_no_authority_imports(self):
        from backend.core.ouroboros.battle_test import (
            termination_hook_default_adapters,
        )
        src = inspect.getsource(termination_hook_default_adapters)
        tree = ast.parse(src)
        forbidden = {
            "yaml_writer", "orchestrator", "iron_gate",
            "risk_tier", "change_engine",
            "candidate_generator", "gate", "policy",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split(".")
                for f in forbidden:
                    assert f not in parts, (
                        f"forbidden import: {node.module}"
                    )

    def test_adapter_does_not_import_harness(self):
        # Avoid import cycle — adapter is imported by harness
        # __init__; if adapter imports harness, circular.
        from backend.core.ouroboros.battle_test import (
            termination_hook_default_adapters,
        )
        src = inspect.getsource(termination_hook_default_adapters)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Allow battle_test.termination_hook_* imports
                # (substrate + registry); forbid the harness
                # module itself.
                if node.module.endswith(".harness"):
                    pytest.fail(
                        f"forbidden harness import: "
                        f"{node.module}"
                    )
