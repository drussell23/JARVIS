"""Behavioral + structural spine for P2 Slice 3 wiring.

Proves the in-flight registry + convergence reaper compose into
``GovernedLoopService``'s lifecycle without raising into the hot
path and without breaking the byte-identical master-OFF
contract.

Surfaces tested:

  1. The three :mod:`in_flight_registry` safe-wire helpers
     (``register_op_safely`` / ``unregister_op_safely`` /
     ``update_phase_safely``) are master-gated NEVER-raise.

  2. The two :mod:`convergence_reaper` safe-wire helpers
     (``safe_start_default_reaper`` / ``safe_stop_default_reaper``)
     are master-gated NEVER-raise and idempotent.

  3. ``governed_loop_service.py`` source contains exactly the
     wiring contract:

       * every ``self._active_ops.add(...)`` is followed within
         a bounded window by a call to a safe-register helper
         (the "paired register" structural pin)
       * every ``self._active_ops.discard(...)`` is followed by
         a paired safe-unregister
       * ``start()`` boots the reaper; ``stop()`` stops it FIRST
         (before draining in-flight ops, so the reaper's
         background task doesn't race the drain)
       * The reaper boot/stop calls are wrapped in try/except
         (NEVER-raise into the live loop)

  4. The module-level helper functions
     (``_register_op_in_flight_safely`` /
     ``_unregister_op_in_flight_safely`` /
     ``_op_registry_metadata``) exist + are callable.

This is a *structural* spine. End-to-end "the reaper actually
converges a synthetic op when wired through the live loop"
already lives in the reaper substrate's own spine
(``test_convergence_reaper.py`` —
``TestLoadBearingConvergence``). This file proves the WIRING is
present + correct, not that the substrate works (which is
already proven).
"""
from __future__ import annotations

import ast as _ast
import inspect
import os
from pathlib import Path
from typing import Iterator

import pytest

import backend.core.ouroboros.governance.governed_loop_service as gls_mod
from backend.core.ouroboros.governance.convergence_reaper import (
    safe_start_default_reaper,
    safe_stop_default_reaper,
)
from backend.core.ouroboros.governance.in_flight_registry import (
    InFlightRegistry,
    get_default_registry,
    register_op_safely,
    reset_default_registry,
    unregister_op_safely,
    update_phase_safely,
)


_REGISTRY_FLAG = "JARVIS_IN_FLIGHT_REGISTRY_ENABLED"
_REAPER_FLAG = "JARVIS_CONVERGENCE_REAPER_ENABLED"
_GLS_SOURCE_PATH = Path(inspect.getfile(gls_mod))


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    saved = {
        k: os.environ.pop(k, None)
        for k in (_REGISTRY_FLAG, _REAPER_FLAG)
    }
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    reset_default_registry()


# ===========================================================================
# Surface 1 — substrate safe-wire helpers
# ===========================================================================


class TestRegistrySafeWireHelpers:
    def test_register_master_off_is_noop(self):
        assert register_op_safely("op-x") is False
        assert get_default_registry().size() == 0

    def test_register_master_on_actually_registers(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_REGISTRY_FLAG, "true")
        assert register_op_safely(
            "op-x",
            ctx_ref=object(),
            metadata={"provider": "claude"},
        ) is True
        rec = get_default_registry().lookup("op-x")
        assert rec is not None
        assert ("provider", "claude") in rec.metadata

    def test_unregister_master_off_is_noop(self):
        # Populate via the underlying registry directly.
        r = get_default_registry()
        r.register("op-x")
        # Master OFF — the safe wrapper short-circuits, BUT the
        # entry is still there because the bare register WAS
        # called.
        assert unregister_op_safely("op-x") is False
        assert r.size() == 1

    def test_unregister_master_on_removes(self, monkeypatch):
        monkeypatch.setenv(_REGISTRY_FLAG, "true")
        register_op_safely("op-x")
        assert unregister_op_safely("op-x") is True
        assert get_default_registry().size() == 0

    def test_update_phase_master_off_noop(self):
        assert update_phase_safely(
            "op-x", phase_name="apply",
        ) is False

    def test_update_phase_master_on_succeeds(self, monkeypatch):
        monkeypatch.setenv(_REGISTRY_FLAG, "true")
        register_op_safely("op-x", last_phase_name="route")
        assert update_phase_safely(
            "op-x", phase_name="generate",
        ) is True
        assert (
            get_default_registry().lookup("op-x").last_phase_name
            == "generate"
        )

    def test_helpers_never_raise_on_garbage_input(self):
        # Wrap each call in a try; none should propagate.
        for op_id in (None, "", 12345, b"bytes"):
            try:
                register_op_safely(op_id)  # type: ignore[arg-type]
                unregister_op_safely(
                    op_id,  # type: ignore[arg-type]
                )
                update_phase_safely(
                    op_id,  # type: ignore[arg-type]
                    phase_name="x",
                )
            except Exception as e:
                pytest.fail(
                    f"safe-wire helper raised on {op_id!r}: {e}"
                )


# ===========================================================================
# Surface 2 — reaper safe-wire helpers
# ===========================================================================


class TestReaperSafeWireHelpers:
    def test_start_master_off_is_noop(self):
        assert safe_start_default_reaper() is False

    @pytest.mark.asyncio
    async def test_stop_master_off_is_noop(self):
        assert await safe_stop_default_reaper() is False

    @pytest.mark.asyncio
    async def test_start_stop_idempotent_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_REAPER_FLAG, "true")
        monkeypatch.setenv(
            "JARVIS_CONVERGENCE_REAPER_TICK_S", "1",
        )
        try:
            assert safe_start_default_reaper() is True
            # Re-start is silent.
            assert safe_start_default_reaper() is True
            # Stop returns True (something was running).
            assert await safe_stop_default_reaper() is True
            # Re-stop returns False (nothing running).
            assert await safe_stop_default_reaper() is False
        finally:
            await safe_stop_default_reaper()


# ===========================================================================
# Surface 3 — wiring-presence pins (read governed_loop_service.py)
# ===========================================================================


def _read_gls_source() -> str:
    return _GLS_SOURCE_PATH.read_text(encoding="utf-8")


class TestWiringPresence:
    def test_module_level_helpers_defined(self):
        for name in (
            "_register_op_in_flight_safely",
            "_unregister_op_in_flight_safely",
            "_op_registry_metadata",
        ):
            assert hasattr(gls_mod, name), (
                f"module-level helper {name!r} missing"
            )
            assert callable(getattr(gls_mod, name))

    def test_module_helpers_are_master_off_byte_identical(self):
        """The helpers MUST return False under master-OFF
        without touching the registry — the live loop's hot
        path must not depend on the registry being available."""
        assert (
            gls_mod._register_op_in_flight_safely("op-x") is False
        )
        assert (
            gls_mod._unregister_op_in_flight_safely("op-x")
            is False
        )

    def test_op_registry_metadata_extracts_safe_fields(self):
        class _FakeCtx:
            provider = "claude"
            route = "standard"
            urgency_level = "normal"
            outcome_source = "test_failure"
        md = gls_mod._op_registry_metadata(_FakeCtx())
        assert md == {
            "provider": "claude",
            "route": "standard",
            "urgency": "normal",
            "source": "test_failure",
        }

    def test_op_registry_metadata_never_raises(self):
        class _Broken:
            @property
            def provider(self):
                raise RuntimeError("boom")
        # MUST return {} not raise.
        result = gls_mod._op_registry_metadata(_Broken())
        assert result == {}

    def test_paired_register_after_active_ops_add(self):
        """Structural invariant: every ``self._active_ops.add(...)``
        site in ``governed_loop_service.py`` MUST be followed
        within a bounded window (200 chars) by a call to one of
        the registry safe-wire helpers. This is the "paired
        register" pin operator asked for — its drift means a
        future contributor added a new in-flight site without
        wiring the registry parity."""
        source = _read_gls_source()
        lines = source.split("\n")
        # Find every add call site.
        add_lines = [
            (i, line) for i, line in enumerate(lines)
            if "self._active_ops.add(" in line
        ]
        assert len(add_lines) >= 2, (
            "expected at least 2 _active_ops.add sites (fg + bg)"
        )
        for idx, line in add_lines:
            # Look ahead 10 lines for a register helper call.
            window = "\n".join(lines[idx: idx + 12])
            assert (
                "_register_op_in_flight_safely" in window
                or "register_op_safely" in window
            ), (
                f"_active_ops.add at line {idx + 1} not paired "
                f"with a register helper within 12 lines:\n"
                f"{window!r}"
            )

    def test_paired_unregister_after_active_ops_discard(self):
        """Mirror invariant for discard sites."""
        source = _read_gls_source()
        lines = source.split("\n")
        discard_lines = [
            (i, line) for i, line in enumerate(lines)
            if "self._active_ops.discard(" in line
        ]
        assert len(discard_lines) >= 2, (
            "expected at least 2 _active_ops.discard sites"
        )
        for idx, line in discard_lines:
            window = "\n".join(lines[idx: idx + 12])
            assert (
                "_unregister_op_in_flight_safely" in window
                or "unregister_op_safely" in window
            ), (
                f"_active_ops.discard at line {idx + 1} not "
                f"paired with unregister helper within 12 "
                f"lines:\n{window!r}"
            )

    def test_start_boots_reaper(self):
        """``GovernedLoopService.start()`` MUST compose
        ``safe_start_default_reaper`` so the universal
        convergence guarantee activates with the loop."""
        source = _read_gls_source()
        # Anchor on the start() definition.
        anchor = "    async def start(self) -> None:"
        idx = source.find(anchor)
        assert idx > 0, "start() definition not found"
        # Compute the next 'async def' to bound the search.
        next_def = source.find("\n    async def ", idx + 1)
        body = (
            source[idx:next_def] if next_def > 0
            else source[idx:]
        )
        assert "safe_start_default_reaper" in body, (
            "start() must compose safe_start_default_reaper"
        )

    def test_stop_stops_reaper_before_drain(self):
        """``stop()`` MUST stop the reaper BEFORE draining
        in-flight ops — otherwise the reaper's background task
        races the drain and may converge an op that the
        natural shutdown was about to finalize.

        Structural check: ``safe_stop_default_reaper`` appears
        in stop()'s body AND its position precedes the first
        drain-related primitive (the health probe cancellation
        is the canonical earliest drain marker)."""
        source = _read_gls_source()
        anchor = "    async def stop(self) -> None:"
        idx = source.find(anchor)
        assert idx > 0, "stop() definition not found"
        next_def = source.find("\n    async def ", idx + 1)
        body = (
            source[idx:next_def] if next_def > 0
            else source[idx:]
        )
        reaper_idx = body.find("safe_stop_default_reaper")
        probe_idx = body.find("self._health_probe_task.cancel")
        assert reaper_idx > 0, (
            "stop() must compose safe_stop_default_reaper"
        )
        assert probe_idx > 0, (
            "stop() must cancel _health_probe_task "
            "(canonical drain marker)"
        )
        assert reaper_idx < probe_idx, (
            "reaper stop must precede drain: "
            f"reaper_idx={reaper_idx} probe_idx={probe_idx}"
        )

    def test_reaper_boot_wrapped_in_try_except(self):
        """The reaper boot MUST be inside a try/except so a
        substrate failure can NEVER take down the live loop's
        start path. This is the NEVER-raise-into-live-loop
        contract."""
        source = _read_gls_source()
        # Find the safe_start_default_reaper call site.
        boot_idx = source.find("safe_start_default_reaper")
        assert boot_idx > 0
        # Walk backwards up to 500 chars and forward up to
        # 500 chars — a try block must enclose the boot.
        window = source[max(0, boot_idx - 500): boot_idx + 500]
        assert "try:" in window, (
            "reaper boot site has no enclosing try block"
        )
        assert "except" in window, (
            "reaper boot site has no enclosing except clause"
        )


# ===========================================================================
# Surface 4 — substrate __all__ exports the helpers
# ===========================================================================


class TestSubstrateExports:
    def test_registry_module_exports_safe_wire_helpers(self):
        import backend.core.ouroboros.governance.in_flight_registry as r  # noqa: E501
        for name in (
            "register_op_safely",
            "unregister_op_safely",
            "update_phase_safely",
        ):
            assert name in r.__all__, (
                f"in_flight_registry.__all__ missing {name!r}"
            )

    def test_reaper_module_exports_safe_wire_helpers(self):
        import backend.core.ouroboros.governance.convergence_reaper as r  # noqa: E501
        for name in (
            "safe_start_default_reaper",
            "safe_stop_default_reaper",
        ):
            assert name in r.__all__, (
                f"convergence_reaper.__all__ missing {name!r}"
            )


# ===========================================================================
# Surface 5 — AST authority pin: no concurrent register/discard
#             without master-gated wrapper
# ===========================================================================


def test_governed_loop_does_not_call_registry_bare():
    """The live loop MUST NOT call the substrate's bare
    :meth:`InFlightRegistry.register` / ``.unregister`` directly
    — it must compose the safe-wire helpers so the master gate
    + NEVER-raise envelope are always present. Bare access from
    governed_loop_service would bypass the gate.

    Structural check: AST-walk all attribute accesses; any
    ``.register(`` / ``.unregister(`` called on an
    ``InFlightRegistry`` literal name is a drift signal.
    """
    src = _read_gls_source()
    tree = _ast.parse(src)
    bare_calls = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        # Look for `InFlightRegistry().register(...)` or
        # `get_default_registry().register(...)` patterns.
        if (
            isinstance(func, _ast.Attribute)
            and func.attr in ("register", "unregister")
            and isinstance(func.value, _ast.Call)
            and isinstance(func.value.func, _ast.Name)
            and func.value.func.id in (
                "InFlightRegistry",
                "get_default_registry",
            )
        ):
            bare_calls.append(
                f"line {node.lineno}: bare "
                f"{func.value.func.id}().{func.attr}"
            )
    assert bare_calls == [], (
        "GovernedLoopService must compose safe-wire helpers, "
        f"not bare registry methods. Drift: {bare_calls}"
    )
