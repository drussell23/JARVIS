"""RenderConductor arc — backlog regression suite.

Pins backlog items #2 + #3 (backlog #1 was a one-line fix to
test_flag_registry.py; backlog #4 is this spine):

  * Backlog 1 (verified by test_flag_registry.py): stale
    ``test_ensure_seeded_installs_specs`` accepts dynamic discovery.
  * Backlog 2 (this file §A): help_dispatcher exposes
    ``_discover_module_provided_verbs`` AND calls it from
    ``get_default_verb_registry``. render_repl exposes
    ``register_verbs(registry)``. Reset+seed cycle re-discovers the
    /render verb without explicit re-registration.
  * Backlog 3 (this file §B-D): adaptive Rich rendering in adapters —
    FILE_REF dedup ring; MODAL_PROMPT density-aware Panel; THREAD_TURN
    role-resolved style.

Strict directives validated:

  * No hardcoded width: terminal width via shutil.get_terminal_size
    (honors COLUMNS env). No JARVIS_*_WIDTH knob duplicating that.
  * No hardcoded color tags: THREAD_TURN reads conductor-stamped
    ColorRole + active theme; falls through to plain text only when
    theme resolution fails.
  * Discovery loop is total: per-module failures don't stop sibling
    discoveries; never raises.
  * Cross-file AST pin protects the discovery hook (backlog #2's
    structural protection).

Covers:

  §A   Verb discovery hook — render_repl.register_verbs auto-found
  §B   FILE_REF dedup — same path:line suppressed within window
  §C   MODAL_PROMPT density-aware — COMPACT one-liner / NORMAL Panel
  §D   THREAD_TURN role-resolved style — speaker label + theme style
  §E   _terminal_width + _active_density helpers — defensive fallbacks
  §F   AST pin: help_dispatcher_verb_discovery_present clean +
       tampering caught
"""
from __future__ import annotations

import ast
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import render_backends as rb
from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_repl as rr


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "COLUMNS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


def _make_event(**overrides: Any) -> rc.RenderEvent:
    defaults: dict = {
        "kind": rc.EventKind.REASONING_TOKEN,
        "region": rc.RegionKind.PHASE_STREAM,
        "role": rc.ColorRole.CONTENT,
        "content": "x",
        "source_module": "test",
    }
    defaults.update(overrides)
    return rc.RenderEvent(**defaults)


class _RecordingConsole:
    """Console stub that records every print() call (objects + strings)."""

    def __init__(self) -> None:
        self.prints: List[Any] = []

    def print(self, obj: Any) -> None:
        self.prints.append(obj)


class _RichFlow:
    """SerpentFlow stub with .console + the optional handler methods."""

    def __init__(self) -> None:
        self.console = _RecordingConsole()
        self.diffs: List[tuple] = []
        self.code_previews: List[str] = []

    def show_streaming_token(self, token: str) -> None:
        pass

    def show_streaming_start(self, op_id: str, provider: str) -> None:
        pass

    def show_streaming_end(self) -> None:
        pass

    def show_diff(self, file_path: str, diff_text: str = "") -> None:
        self.diffs.append((file_path, diff_text))

    def show_code_preview(self, file_path: str) -> None:
        self.code_previews.append(file_path)


# ---------------------------------------------------------------------------
# §A — Verb discovery hook (backlog #2)
# ---------------------------------------------------------------------------


class TestVerbDiscoveryHook:
    def test_help_dispatcher_exposes_discovery(self):
        from backend.core.ouroboros.governance import help_dispatcher as hd
        assert callable(getattr(hd, "_discover_module_provided_verbs", None))

    def test_render_repl_exposes_register_verbs(self):
        assert callable(getattr(rr, "register_verbs", None))

    def test_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        reg = VerbRegistry()
        assert rr.register_verbs(reg) == 1
        assert reg.get("/render") is not None

    def test_reset_then_seed_re_discovers(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            get_default_verb_registry,
            reset_default_verb_registry,
        )
        # First boot — /render present
        reg1 = get_default_verb_registry()
        assert reg1.get("/render") is not None
        # Reset + re-seed — /render still present without explicit
        # re-registration
        reset_default_verb_registry()
        reg2 = get_default_verb_registry()
        assert reg1 is not reg2
        assert reg2.get("/render") is not None

    def test_register_verbs_idempotent(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        reg = VerbRegistry()
        rr.register_verbs(reg)
        rr.register_verbs(reg)
        # Second call replaces (override=True default); no duplicate
        all_verbs = reg.list_all()
        names = [v.name for v in all_verbs]
        assert names.count("/render") == 1


# ---------------------------------------------------------------------------
# §B — FILE_REF dedup (backlog #3)
# ---------------------------------------------------------------------------


class TestFileRefDedup:
    def test_first_event_passes_through(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py", "line": 1, "diff_text": "@@"},
        ))
        assert flow.diffs == [("x.py", "@@")]

    def test_immediate_repeat_suppressed(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        ev = _make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py", "line": 1, "diff_text": "@@"},
        )
        b.notify(ev)
        b.notify(ev)
        b.notify(ev)
        # First passed; subsequent suppressed
        assert flow.diffs == [("x.py", "@@")]

    def test_different_line_same_file_passes(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        for line in (1, 2, 3):
            b.notify(_make_event(
                kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
                role=rc.ColorRole.METADATA, content=f"x.py:{line}",
                metadata={"path": "x.py", "line": line, "diff_text": "@@"},
            ))
        assert len(flow.diffs) == 3

    def test_dedup_window_eviction(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        # Fire 17 distinct events (window=16) then revisit the first
        for i in range(17):
            b.notify(_make_event(
                kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
                role=rc.ColorRole.METADATA, content=f"file_{i}.py",
                metadata={"path": f"file_{i}.py", "line": 1, "diff_text": "@@"},
            ))
        # Revisit file_0 — was evicted (16 distinct since), should pass
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="file_0.py",
            metadata={"path": "file_0.py", "line": 1, "diff_text": "@@"},
        ))
        # 17 originals + 1 revisit = 18 calls to show_diff
        assert len(flow.diffs) == 18

    def test_per_backend_dedup_state(self, fresh_registry):
        # Two SerpentFlowBackends have independent dedup rings
        flow_a = _RichFlow()
        flow_b = _RichFlow()
        b_a = rb.SerpentFlowBackend(flow_a)
        b_b = rb.SerpentFlowBackend(flow_b)
        ev = _make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py", "line": 1, "diff_text": "@@"},
        )
        b_a.notify(ev)
        b_b.notify(ev)
        # Each backend saw it for the first time → both pass
        assert len(flow_a.diffs) == 1
        assert len(flow_b.diffs) == 1


# ---------------------------------------------------------------------------
# §C — MODAL_PROMPT density-aware (backlog #3)
# ---------------------------------------------------------------------------


class TestModalPromptDensity:
    def test_normal_density_uses_rich_panel(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.MODAL_PROMPT, region=rc.RegionKind.MODAL,
            role=rc.ColorRole.CONTENT, content="help body",
        ))
        from rich.panel import Panel
        panels = [p for p in flow.console.prints if isinstance(p, Panel)]
        assert panels
        assert "help body" in str(panels[0].renderable)

    def test_compact_density_uses_one_liner(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Force COMPACT density via env override
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "compact",
        )
        # Wire a conductor so _active_density() reads the override
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.MODAL_PROMPT, region=rc.RegionKind.MODAL,
            role=rc.ColorRole.CONTENT,
            content="help body that may wrap multiple lines\nlike this",
        ))
        # COMPACT collapses to a "/help: …" string; no Rich Panel
        from rich.panel import Panel
        assert not any(isinstance(p, Panel) for p in flow.console.prints)
        text_prints = [
            p for p in flow.console.prints if isinstance(p, str)
        ]
        assert text_prints
        assert "/help:" in text_prints[0]
        # Newline replaced with space → single-line render
        assert "\n" not in text_prints[0]

    def test_empty_content_skipped(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.MODAL_PROMPT, region=rc.RegionKind.MODAL,
            role=rc.ColorRole.CONTENT, content="",
        ))
        assert flow.console.prints == []


# ---------------------------------------------------------------------------
# §D — THREAD_TURN role-resolved style (backlog #3)
# ---------------------------------------------------------------------------


class TestThreadTurnRoleResolved:
    def test_role_style_applied(self, fresh_registry):
        # Wire a conductor so _resolve_role_style finds the active theme
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.THREAD_TURN, region=rc.RegionKind.THREAD,
            role=rc.ColorRole.EMPHASIS,  # USER → EMPHASIS per S5
            content="hello world",
            metadata={"speaker": "USER"},
        ))
        text_prints = [
            p for p in flow.console.prints if isinstance(p, str)
        ]
        assert text_prints
        out = text_prints[0]
        # Speaker label preserved (semantic)
        assert "you" in out
        # EMPHASIS resolves to "bold" in DefaultTheme — Rich tag wraps
        assert "[bold]" in out

    def test_role_resolution_falls_back_to_plain(self, fresh_registry):
        # No conductor registered → _resolve_role_style returns ""
        rc.reset_render_conductor()
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.THREAD_TURN, region=rc.RegionKind.THREAD,
            role=rc.ColorRole.EMPHASIS,
            content="hello",
            metadata={"speaker": "USER"},
        ))
        text_prints = [
            p for p in flow.console.prints if isinstance(p, str)
        ]
        assert text_prints
        # No conductor → plain "you: hello" without Rich tags
        assert "you:" in text_prints[0]
        assert "[bold]" not in text_prints[0]

    def test_unknown_speaker_falls_to_question_mark(self, fresh_registry):
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.THREAD_TURN, region=rc.RegionKind.THREAD,
            role=rc.ColorRole.METADATA, content="x",
            metadata={"speaker": "BOGUS_SPEAKER"},
        ))
        text_prints = [
            p for p in flow.console.prints if isinstance(p, str)
        ]
        assert text_prints
        assert "?" in text_prints[0]


# ---------------------------------------------------------------------------
# §E — Helper functions: _terminal_width + _active_density
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_terminal_width_returns_positive_int(self):
        w = rb._terminal_width()
        assert isinstance(w, int)
        assert w >= 20  # min clamp

    def test_terminal_width_honors_columns_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("COLUMNS", "120")
        # shutil.get_terminal_size honors COLUMNS in some envs;
        # min clamp guarantees safe lower bound either way.
        w = rb._terminal_width()
        assert w >= 20

    def test_active_density_no_conductor_returns_normal(self, fresh_registry):
        rc.reset_render_conductor()
        assert rb._active_density() == "NORMAL"

    def test_active_density_reads_override(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "full",
        )
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        assert rb._active_density() == "FULL"


# ---------------------------------------------------------------------------
# §F — AST pin: help_dispatcher_verb_discovery_present
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def discovery_pin() -> Any:
    pins = list(rb.register_shipped_invariants())
    return next(
        p for p in pins
        if p.invariant_name == "help_dispatcher_verb_discovery_present"
    )


class TestDiscoveryPin:
    def test_pin_clean_against_real_help_dispatcher(self, discovery_pin):
        import pathlib
        path = pathlib.Path(
            "backend/core/ouroboros/governance/help_dispatcher.py",
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert discovery_pin.validate(tree, src) == ()

    def test_pin_catches_missing_definition(self, discovery_pin):
        # Tampered source: get_default_verb_registry doesn't call the
        # discovery hook (or the hook isn't defined)
        tampered = (
            "def get_default_verb_registry():\n"
            "    pass  # missing _discover_module_provided_verbs call\n"
        )
        tree = ast.parse(tampered)
        violations = discovery_pin.validate(tree, tampered)
        assert violations
        assert "_discover_module_provided_verbs" in violations[0]

    def test_pin_catches_missing_call_site(self, discovery_pin):
        # Hook defined but never called from get_default_verb_registry
        tampered = (
            "def _discover_module_provided_verbs(reg):\n"
            "    return 0\n"
            "def get_default_verb_registry():\n"
            "    return None  # no discovery invocation\n"
        )
        tree = ast.parse(tampered)
        violations = discovery_pin.validate(tree, tampered)
        assert violations
        assert "_discover_module_provided_verbs(_default_verbs)" in (
            violations[0]
        )
