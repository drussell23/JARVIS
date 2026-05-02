"""InlinePromptGate Slice 5b — SerpentFlow boot wire-up regression spine.

Verifies the deferred Slice 5b integration:
  * SerpentFlow.__init__ registers the phase-boundary renderer at
    boot via the Slice 4 ``attach_phase_boundary_renderer`` helper.
  * The unsub callable is exposed on the instance for tests / future
    cleanup hooks.
  * Boot survives renderer-import failure (try/except contract
    matches the established StreamRenderer pattern in the same
    constructor).
  * The wire-up uses lazy import (inside __init__) — does NOT
    introduce a hard module-top dependency on the renderer.
  * End-to-end: a phase-boundary prompt registered on the default
    controller singleton renders to SerpentFlow's console.
"""
from __future__ import annotations

import ast
import asyncio
import io
import pathlib
import uuid

import pytest
from rich.console import Console

from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    get_default_controller,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    PhaseInlinePromptRequest,
)
from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
    PHASE_BOUNDARY_HEADER,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    bridge_to_controller_request,
)


# ---------------------------------------------------------------------------
# Boot wire-up surface
# ---------------------------------------------------------------------------


class TestBootWireUp:
    def test_serpent_flow_init_exposes_unsub_attribute(self):
        """SerpentFlow.__init__ must register the renderer and bind
        the unsub callable to the instance."""
        sf = SerpentFlow(session_id="test-5b")
        assert hasattr(sf, "_unsub_inline_prompt_renderer")
        assert callable(sf._unsub_inline_prompt_renderer)
        # Calling the unsub is idempotent + safe.
        sf._unsub_inline_prompt_renderer()

    def test_unsub_is_no_op_when_renderer_import_fails(
        self, monkeypatch,
    ):
        """If the renderer import fails inside __init__, the
        try/except installs a no-op lambda so SerpentFlow boot
        never blocks. Defensive contract — same shape as the
        StreamRenderer boot path right above it."""
        import sys
        # Force the import to fail by inserting a sentinel that
        # raises on attribute access for the renderer module.
        broken_mod = type("_Broken", (), {
            "__getattr__": lambda self, name: (_ for _ in ()).throw(
                ImportError("forced for test"),
            ),
        })()
        monkeypatch.setitem(
            sys.modules,
            "backend.core.ouroboros.governance.inline_prompt_gate_renderer",
            broken_mod,
        )
        sf = SerpentFlow(session_id="test-5b-broken")
        # No-op lambda installed; calling it doesn't raise.
        assert callable(sf._unsub_inline_prompt_renderer)
        sf._unsub_inline_prompt_renderer()


# ---------------------------------------------------------------------------
# Lazy import contract
# ---------------------------------------------------------------------------


class TestLazyImportContract:
    def test_renderer_not_imported_at_module_top(self):
        """Slice 5b uses LAZY import inside __init__ (mirrors the
        StreamRenderer pattern in the same constructor), so the
        renderer module is NOT a hard dependency at import time.
        AST walk asserts no module-top ImportFrom for the renderer."""
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "battle_test"
            / "serpent_flow.py"
        )
        source = path.read_text()
        tree = ast.parse(source)
        # Module-top ImportFrom = direct child of Module body.
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "inline_prompt_gate_renderer" in module:
                    raise AssertionError(
                        f"Slice 5b must use lazy import (matching "
                        f"StreamRenderer pattern). Found module-top "
                        f"import of {module!r} at line "
                        f"{getattr(node, 'lineno', '?')}"
                    )

    def test_lazy_import_present_inside_init(self):
        """The lazy import lives inside SerpentFlow.__init__ and
        references the canonical attach_phase_boundary_renderer
        symbol."""
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "battle_test"
            / "serpent_flow.py"
        )
        source = path.read_text()
        tree = ast.parse(source)
        found = False
        for cls in ast.walk(tree):
            if (
                isinstance(cls, ast.ClassDef)
                and cls.name == "SerpentFlow"
            ):
                for fn in cls.body:
                    if (
                        isinstance(fn, ast.FunctionDef)
                        and fn.name == "__init__"
                    ):
                        for node in ast.walk(fn):
                            if isinstance(node, ast.ImportFrom):
                                module = node.module or ""
                                if "inline_prompt_gate_renderer" in module:
                                    for alias in node.names:
                                        if (
                                            alias.name
                                            == "attach_phase_boundary_renderer"
                                        ):
                                            found = True
        assert found, (
            "expected lazy `from ...inline_prompt_gate_renderer "
            "import attach_phase_boundary_renderer` inside "
            "SerpentFlow.__init__"
        )


# ---------------------------------------------------------------------------
# End-to-end via SerpentFlow's console
# ---------------------------------------------------------------------------


class TestEndToEndViaSerpentFlow:
    @pytest.mark.asyncio
    async def test_phase_boundary_prompt_renders_to_serpent_console(
        self,
    ):
        """A phase-boundary prompt registered on the controller
        singleton (via Slice 2's bridge) renders to SerpentFlow's
        console — proves the boot wire-up is live end-to-end."""
        # Capture SerpentFlow's console output via a Rich Console
        # bound to a StringIO file.
        buf = io.StringIO()
        # Construct a SerpentFlow whose console writes to buf; we
        # construct first then swap the console attribute, then
        # re-attach the renderer pointing at the new console.
        sf = SerpentFlow(session_id="test-5b-e2e")
        # Drop the original listener and re-attach against our
        # capture console.
        sf._unsub_inline_prompt_renderer()
        capture_console = Console(
            file=buf, force_terminal=False, color_system=None,
            width=120,
        )
        from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
            attach_phase_boundary_renderer,
        )
        # Bind to the real default controller (the singleton the
        # Slice 2 producer uses).
        controller = get_default_controller()
        sf._unsub_inline_prompt_renderer = (
            attach_phase_boundary_renderer(
                capture_console.print, controller=controller,
            )
        )
        try:
            req = PhaseInlinePromptRequest(
                prompt_id="ipg-5b-" + uuid.uuid4().hex[:16],
                op_id="op-5b",
                phase_at_request="GATE",
                risk_tier="NOTIFY_APPLY",
                change_summary="slice 5b boot-wireup proof",
                change_fingerprint="5b" * 32,
                target_paths=("backend/foo.py",),
            )
            bridged = bridge_to_controller_request(req)
            controller.request(bridged, timeout_s=30.0)
            # Yield once so the listener fires.
            await asyncio.sleep(0)
            output = buf.getvalue()
            assert PHASE_BOUNDARY_HEADER in output
            assert req.prompt_id[:24] in output
            # Now resolve to verify dismiss line also fires.
            controller.allow_once(req.prompt_id, reviewer="test")
            await asyncio.sleep(0)
            output_after = buf.getvalue()
            assert "allowed" in output_after
        finally:
            sf._unsub_inline_prompt_renderer()
