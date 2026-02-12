"""
E2E smoke tests for the unified supervisor module.

These are lightweight tests that verify the supervisor module imports correctly
and its core classes (events, event bus, CLI renderers, config) behave as
expected at a basic level. They do NOT start the full system.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so `import unified_supervisor` resolves.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helper: safely import unified_supervisor with create_safe_task patched
# to plain asyncio.create_task (avoids side-effects from async_safety).
# ---------------------------------------------------------------------------
_us = None  # module-level cache


def _import_supervisor():
    """Import unified_supervisor once, caching the result."""
    global _us
    if _us is not None:
        return _us

    # Patch create_safe_task at the target location *before* it is used.
    # The module defines create_safe_task in a try/except; we patch it
    # after import to redirect the event bus's start() to plain create_task.
    import unified_supervisor

    unified_supervisor.create_safe_task = asyncio.create_task
    _us = unified_supervisor
    return _us


def _reset_event_bus_singleton():
    """Reset the SupervisorEventBus singleton so each test gets a fresh one."""
    us = _import_supervisor()
    us.SupervisorEventBus._instance = None
    us._supervisor_event_bus = None


# ===========================================================================
# TestSupervisorImport
# ===========================================================================

@pytest.mark.e2e
class TestSupervisorImport:
    """Verify the supervisor module can be imported without crashing."""

    def test_import_succeeds(self):
        """import unified_supervisor should not raise."""
        us = _import_supervisor()
        assert us is not None
        assert hasattr(us, "__name__")

    def test_key_classes_accessible(self):
        """Core public classes should be importable attributes."""
        us = _import_supervisor()
        for name in (
            "SupervisorEventType",
            "SupervisorEventSeverity",
            "SupervisorEvent",
            "SupervisorEventBus",
        ):
            cls = getattr(us, name, None)
            assert cls is not None, f"{name} not found on unified_supervisor"


# ===========================================================================
# TestSupervisorEventSmoke
# ===========================================================================

@pytest.mark.e2e
class TestSupervisorEventSmoke:
    """Basic SupervisorEvent construction and serialization."""

    def test_event_instantiation_defaults(self):
        """SupervisorEvent can be created with only required fields."""
        us = _import_supervisor()
        evt = us.SupervisorEvent(
            event_type=us.SupervisorEventType.LOG,
            timestamp=time.time(),
            message="smoke test",
        )
        assert evt.message == "smoke test"
        assert evt.severity == us.SupervisorEventSeverity.INFO
        assert evt.phase == ""
        assert evt.component == ""

    def test_event_to_json_dict(self):
        """to_json_dict() returns a dict with the mandatory keys."""
        us = _import_supervisor()
        now = time.time()
        evt = us.SupervisorEvent(
            event_type=us.SupervisorEventType.PHASE_START,
            timestamp=now,
            message="phase begin",
            severity=us.SupervisorEventSeverity.INFO,
            phase="preflight",
        )
        d = evt.to_json_dict()
        assert isinstance(d, dict)
        # Mandatory keys always present
        for key in ("event_type", "timestamp", "message", "severity"):
            assert key in d, f"Missing key: {key}"
        assert d["event_type"] == "phase_start"
        assert d["severity"] == "info"
        assert d["message"] == "phase begin"
        assert d["phase"] == "preflight"

    def test_all_event_types_accessible(self):
        """Every member of SupervisorEventType should be iterable."""
        us = _import_supervisor()
        members = list(us.SupervisorEventType)
        assert len(members) >= 10, f"Expected >=10 event types, got {len(members)}"
        # Spot-check a few known members
        names = {m.name for m in members}
        for expected in ("PHASE_START", "PHASE_END", "ERROR", "LOG"):
            assert expected in names, f"{expected} not found in SupervisorEventType"


# ===========================================================================
# TestSupervisorEventBusSmoke
# ===========================================================================

@pytest.mark.e2e
class TestSupervisorEventBusSmoke:
    """SupervisorEventBus singleton, subscribe, emit."""

    def setup_method(self):
        _reset_event_bus_singleton()

    def teardown_method(self):
        _reset_event_bus_singleton()

    def test_bus_instantiation(self):
        """SupervisorEventBus() should return a fresh instance after reset."""
        us = _import_supervisor()
        bus = us.SupervisorEventBus()
        assert bus is not None
        assert bus.handler_count == 0

    def test_get_event_bus(self):
        """get_event_bus() should return a SupervisorEventBus."""
        us = _import_supervisor()
        bus = us.get_event_bus()
        assert isinstance(bus, us.SupervisorEventBus)

    def test_emit_subscribe_roundtrip(self):
        """Subscribe a handler, emit an event, verify the handler receives it.

        Uses synchronous delivery path (no start() needed) since the bus
        delivers synchronously when the async consumer has not been started.
        """
        us = _import_supervisor()
        bus = us.SupervisorEventBus()

        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(handler)

        evt = us.SupervisorEvent(
            event_type=us.SupervisorEventType.LOG,
            timestamp=time.time(),
            message="roundtrip test",
        )
        bus.emit(evt)

        assert len(received) == 1
        assert received[0].message == "roundtrip test"


# ===========================================================================
# TestCliRendererSmoke
# ===========================================================================

@pytest.mark.e2e
class TestCliRendererSmoke:
    """CLI renderer factory and class verification."""

    def test_create_plain_renderer(self):
        """_create_cli_renderer('plain', ...) should return PlainCliRenderer."""
        us = _import_supervisor()
        renderer = us._create_cli_renderer("plain", "ops", False, False)
        assert isinstance(renderer, us.PlainCliRenderer)

    def test_create_json_renderer(self):
        """_create_cli_renderer('json', ...) should return JsonCliRenderer."""
        us = _import_supervisor()
        renderer = us._create_cli_renderer("json", "ops", False, False)
        assert isinstance(renderer, us.JsonCliRenderer)


# ===========================================================================
# TestSystemKernelConfigSmoke
# ===========================================================================

@pytest.mark.e2e
class TestSystemKernelConfigSmoke:
    """SystemKernelConfig dataclass field verification."""

    def test_config_has_ui_fields(self):
        """SystemKernelConfig must expose ui_mode, ui_verbosity, etc."""
        us = _import_supervisor()
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(us.SystemKernelConfig)}
        for expected in ("ui_mode", "ui_verbosity", "ui_no_ansi", "ui_no_animation"):
            assert expected in field_names, (
                f"SystemKernelConfig missing field: {expected}"
            )

    def test_cli_parser_has_ui_flags(self):
        """The argparse parser should accept --ui, --verbosity, --no-ansi, --no-animation."""
        us = _import_supervisor()
        parser = us.create_argument_parser()

        # Collect all option strings from the parser (including groups)
        all_options = set()
        for action in parser._actions:
            all_options.update(action.option_strings)
        for group in parser._action_groups:
            for action in group._group_actions:
                all_options.update(action.option_strings)

        for flag in ("--ui", "--verbosity", "--no-ansi", "--no-animation"):
            assert flag in all_options, f"CLI parser missing flag: {flag}"
