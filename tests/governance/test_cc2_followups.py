"""CC2 follow-ups regression suite — ACTIVE_OP / Update() block / prompt.

Pins the deferred CC1 follow-ups landed in CC2:

  * StatusField.ACTIVE_OP + StatusField.TASK_LIST (composer fields)
  * ClaudeStyleTransport feeds composer on INTENT/DECISION
  * ClaudeStyleTransport implements RenderBackend Protocol
  * ClaudeStyleTransport renders FILE_REF as Update(<path>) blocks
  * SerpentFlow._build_repl_prompt_html — multi-line cwd/mode/posture
    prompt with operator-overrideable JARVIS_PROMPT_TEMPLATE
  * Cross-file AST pin: prompt helper + env-var token presence

Strict directives validated:

  * Closed-taxonomy StatusField extended (now 11 members) AST-pinned
  * Defensive everywhere — bad placeholder / missing module / etc.
    falls back to legacy single-line prompt
  * RenderBackend Protocol conformance (notify/flush/shutdown/name)
  * Bidirectional decoupling preserved — composer doesn't import
    transport; transport doesn't import composer at top level

Covers:

  §A   StatusField.ACTIVE_OP + TASK_LIST present in closed taxonomy
  §B   Composer formatters for ACTIVE_OP / TASK_LIST
  §C   Default field order has ACTIVE_OP first, TASK_LIST second
  §D   ClaudeStyleTransport Protocol conformance
  §E   ClaudeStyleTransport feeds composer on INTENT
  §F   ClaudeStyleTransport clears ACTIVE_OP on DECISION
  §G   ClaudeStyleTransport.notify renders FILE_REF as Update block
  §H   FILE_REF preview lines color-coded per diff prefix
  §I   _build_repl_prompt_html default template render
  §J   _build_repl_prompt_html operator override via env
  §K   _build_repl_prompt_html defensive — bad template falls back
  §L   AST graduation pin (serpent_flow_repl_prompt_helper_present)
"""
from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance import (
    claude_style_transport as cst,
    render_backends as rb,
    render_conductor as rc,
    status_line_composer as slc,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_STATUS_LINE_COMPOSER_ENABLED",
        "JARVIS_STATUS_LINE_DEBOUNCE_MS",
        "JARVIS_PROMPT_TEMPLATE",
        "JARVIS_RENDER_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    slc.reset_status_line_composer()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


def _msg(msg_type: str, op_id: str, payload: Dict[str, Any]) -> Any:
    return SimpleNamespace(
        msg_type=SimpleNamespace(value=msg_type),
        op_id=op_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# §A — StatusField.ACTIVE_OP + TASK_LIST present
# ---------------------------------------------------------------------------


class TestStatusFieldExtensions:
    def test_active_op_member_present(self):
        assert slc.StatusField.ACTIVE_OP.value == "ACTIVE_OP"

    def test_task_list_member_present(self):
        assert slc.StatusField.TASK_LIST.value == "TASK_LIST"

    def test_member_count_now_eleven(self):
        assert len(list(slc.StatusField)) == 11


# ---------------------------------------------------------------------------
# §B — Per-field formatters
# ---------------------------------------------------------------------------


class TestNewFieldFormatters:
    def test_active_op_formats_string(self):
        assert slc._format_active_op("TestFailure(op7c17)") == (
            "TestFailure(op7c17)"
        )

    def test_active_op_empty(self):
        assert slc._format_active_op("") == ""

    def test_active_op_truncates(self):
        long = "X" * 100
        assert len(slc._format_active_op(long)) == 48

    def test_task_list_dict_full(self):
        result = slc._format_task_list({
            "active": 3, "queued": 1, "done": 12,
        })
        assert "3 active" in result
        assert "1 queued" in result
        assert "12 done" in result
        assert " · " in result

    def test_task_list_dict_partial(self):
        result = slc._format_task_list({"active": 1})
        assert "1 active" in result
        assert "queued" not in result

    def test_task_list_empty_dict(self):
        assert slc._format_task_list({}) == ""

    def test_task_list_zeros_omitted(self):
        # Zero counts shouldn't appear
        result = slc._format_task_list({
            "active": 2, "queued": 0, "done": 0,
        })
        assert result == "2 active"


# ---------------------------------------------------------------------------
# §C — Default field order
# ---------------------------------------------------------------------------


class TestDefaultFieldOrder:
    def test_active_op_first(self, fresh_registry):
        order = slc.field_order()
        assert order[0] is slc.StatusField.ACTIVE_OP

    def test_task_list_second(self, fresh_registry):
        order = slc.field_order()
        assert order[1] is slc.StatusField.TASK_LIST


# ---------------------------------------------------------------------------
# §D — ClaudeStyleTransport Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_has_render_backend_methods(self):
        assert hasattr(cst.ClaudeStyleTransport, "notify")
        assert hasattr(cst.ClaudeStyleTransport, "flush")
        assert hasattr(cst.ClaudeStyleTransport, "shutdown")
        assert hasattr(cst.ClaudeStyleTransport, "name")

    def test_handled_kinds_includes_file_ref(self):
        assert "FILE_REF" in cst.ClaudeStyleTransport._HANDLED_KINDS

    def test_handled_no_op_partition_total(self):
        # Same totality contract as SerpentFlowBackend
        union = (
            cst.ClaudeStyleTransport._HANDLED_KINDS
            | cst.ClaudeStyleTransport._NO_OP_KINDS
        )
        all_kinds = {m.value for m in rc.EventKind}
        # ClaudeStyleTransport doesn't handle BACKEND_RESET or
        # FILE_REF + the others — verify total over EventKind
        # (BACKEND_RESET is in _NO_OP_KINDS per its declaration)
        assert all_kinds <= union, (
            f"Missing event kinds: {all_kinds - union}"
        )

    def test_implements_protocol(self):
        # Instantiate with stub console
        class _C:
            def print(self, t, **kw): pass
        t = cst.ClaudeStyleTransport(console=_C())
        # Protocol check via duck-type (RenderBackend is runtime-checkable)
        assert isinstance(t, rc.RenderBackend)


# ---------------------------------------------------------------------------
# §E + §F — Composer feed on INTENT / clear on DECISION
# ---------------------------------------------------------------------------


class _RecordingConsole:
    def __init__(self) -> None:
        self.prints: List[str] = []
    def print(self, text: str, **kw: Any) -> None:
        self.prints.append(text)


@pytest.fixture
def composer_pipeline(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STATUS_LINE_COMPOSER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STATUS_LINE_DEBOUNCE_MS", "0")
    composer = slc.StatusLineComposer()
    slc.register_status_line_composer(composer)
    yield composer
    slc.reset_status_line_composer()


@pytest.mark.asyncio
class TestComposerFeed:
    async def test_intent_sets_active_op(self, composer_pipeline):
        composer = composer_pipeline
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        await t.send(_msg("INTENT", "op-7c17aa-x", {
            "goal": "fix x", "outcome_source": "TestFailure",
        }))
        snapshot = composer.snapshot()
        assert snapshot.get(slc.StatusField.ACTIVE_OP) == (
            "TestFailure(op7c17)"
        )
        task_list = snapshot.get(slc.StatusField.TASK_LIST)
        assert isinstance(task_list, dict)
        assert task_list["active"] == 1
        assert task_list["done"] == 0

    async def test_decision_clears_active_op(self, composer_pipeline):
        composer = composer_pipeline
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        await t.send(_msg("INTENT", "op-1", {
            "goal": "x", "outcome_source": "TestFailure",
        }))
        await t.send(_msg("DECISION", "op-1", {"outcome": "completed"}))
        snapshot = composer.snapshot()
        assert snapshot.get(slc.StatusField.ACTIVE_OP) == ""
        task_list = snapshot.get(slc.StatusField.TASK_LIST)
        assert task_list["active"] == 0
        assert task_list["done"] == 1

    async def test_failed_decision_increments_done_count(
        self, composer_pipeline,
    ):
        composer = composer_pipeline
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        await t.send(_msg("INTENT", "op-1", {
            "goal": "x", "outcome_source": "TestFailure",
        }))
        await t.send(_msg("DECISION", "op-1", {
            "outcome": "failed", "reason_code": "x",
        }))
        snapshot = composer.snapshot()
        task_list = snapshot.get(slc.StatusField.TASK_LIST)
        assert task_list["done"] == 1


# ---------------------------------------------------------------------------
# §G + §H — FILE_REF Update block rendering
# ---------------------------------------------------------------------------


class TestFileRefUpdateBlock:
    def test_basic_file_ref_renders_update_block(self, fresh_registry):
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        event = SimpleNamespace(
            kind=SimpleNamespace(value="FILE_REF"),
            metadata={"path": "backend/x.py", "line": 42},
        )
        t.notify(event)
        line = console.prints[0]
        assert "Update" in line
        assert "backend/x.py:42" in line

    def test_diff_text_extracts_added_removed_count(
        self, fresh_registry,
    ):
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        event = SimpleNamespace(
            kind=SimpleNamespace(value="FILE_REF"),
            metadata={
                "path": "x.py",
                "diff_text": "@@ -1,3 +1,3 @@\n-old line\n+new line\n context",
            },
        )
        t.notify(event)
        # Stats line should appear
        assert any(
            "Added 1 lines" in p and "removed 1 lines" in p
            for p in console.prints
        )

    def test_diff_preview_color_coded(self, fresh_registry):
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        event = SimpleNamespace(
            kind=SimpleNamespace(value="FILE_REF"),
            metadata={
                "path": "x.py",
                "diff_text": "+added\n-removed\n context",
            },
        )
        t.notify(event)
        # Added/removed lines wrapped in green/red Rich markup
        joined = "\n".join(console.prints)
        assert "[green]+added[/green]" in joined
        assert "[red]-removed[/red]" in joined

    def test_no_path_skipped(self, fresh_registry):
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        event = SimpleNamespace(
            kind=SimpleNamespace(value="FILE_REF"),
            metadata={},
        )
        t.notify(event)
        assert console.prints == []

    def test_other_event_kinds_no_op(self, fresh_registry):
        console = _RecordingConsole()
        t = cst.ClaudeStyleTransport(console=console)
        for kind in ("PHASE_BEGIN", "REASONING_TOKEN", "STATUS_TICK"):
            event = SimpleNamespace(
                kind=SimpleNamespace(value=kind),
                metadata={"path": "x.py"},
            )
            t.notify(event)
        assert console.prints == []


# ---------------------------------------------------------------------------
# §I — Default prompt template
# ---------------------------------------------------------------------------


class TestPromptDefaultTemplate:
    def test_default_template_renders_multi_line(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html("> ")
        rendered = html.value if hasattr(html, "value") else str(html)
        # Should have newline (multi-line) + cwd portion
        assert "\n" in rendered
        # cwd resolved (current dir or ?)
        assert "ansigreen" in rendered  # cwd color tag

    def test_default_includes_render_mode(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html("> ")
        rendered = html.value if hasattr(html, "value") else str(html)
        # Mode appears (lowercase claude/serpent)
        assert "claude" in rendered.lower() or "serpent" in rendered.lower()

    def test_fallback_substitution(self):
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html("CUSTOM_PROMPT")
        rendered = html.value if hasattr(html, "value") else str(html)
        assert "CUSTOM_PROMPT" in rendered


# ---------------------------------------------------------------------------
# §J — Operator override via env
# ---------------------------------------------------------------------------


class TestPromptOperatorOverride:
    def test_simple_template(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("JARVIS_PROMPT_TEMPLATE", "{cwd} > ")
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html(">>")
        rendered = html.value if hasattr(html, "value") else str(html)
        # Template applied — fallback NOT shown (template doesn't ref {fallback})
        assert ">>" not in rendered

    def test_all_placeholders_substituted(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(
            "JARVIS_PROMPT_TEMPLATE",
            "[{mode}|{posture}|{sensors}] {cwd} {fallback}",
        )
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html("$")
        rendered = html.value if hasattr(html, "value") else str(html)
        # Each placeholder resolved (no literal { } left)
        assert "{cwd}" not in rendered
        assert "{mode}" not in rendered
        assert "$" in rendered

    def test_minimal_just_fallback(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Operator wants just "> "
        monkeypatch.setenv("JARVIS_PROMPT_TEMPLATE", "{fallback}")
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        html = _build_repl_prompt_html("> ")
        rendered = html.value if hasattr(html, "value") else str(html)
        assert rendered.strip() == ">"


# ---------------------------------------------------------------------------
# §K — Defensive fallback
# ---------------------------------------------------------------------------


class TestPromptDefensive:
    def test_invalid_placeholder_falls_back(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        # Bad placeholder name
        monkeypatch.setenv(
            "JARVIS_PROMPT_TEMPLATE", "{nonexistent_field} > ",
        )
        from backend.core.ouroboros.battle_test.serpent_flow import (
            _build_repl_prompt_html,
        )
        # Should not raise; falls back to single-line legacy
        html = _build_repl_prompt_html("legacy")
        rendered = html.value if hasattr(html, "value") else str(html)
        assert "legacy" in rendered


# ---------------------------------------------------------------------------
# §L — AST graduation pin
# ---------------------------------------------------------------------------


class TestPromptHelperPin:
    def test_pin_registered(self):
        all_pins = list(rb.register_shipped_invariants())
        names = {p.invariant_name for p in all_pins}
        assert "serpent_flow_repl_prompt_helper_present" in names

    def test_pin_clean_against_real_source(self):
        all_pins = list(rb.register_shipped_invariants())
        pin = next(
            p for p in all_pins
            if p.invariant_name ==
            "serpent_flow_repl_prompt_helper_present"
        )
        import pathlib
        path = pathlib.Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py",
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        assert pin.validate(tree, src) == ()

    def test_pin_catches_missing_helper(self):
        all_pins = list(rb.register_shipped_invariants())
        pin = next(
            p for p in all_pins
            if p.invariant_name ==
            "serpent_flow_repl_prompt_helper_present"
        )
        tampered_src = (
            "# serpent_flow without the prompt helper\n"
        )
        tampered = ast.parse(tampered_src)
        violations = pin.validate(tampered, tampered_src)
        assert violations
        assert (
            "_build_repl_prompt_html" in violations[0]
            or "JARVIS_PROMPT_TEMPLATE" in violations[0]
        )


# ---------------------------------------------------------------------------
# §M — Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscovery:
    def test_status_field_pin_includes_new_members(self, fresh_registry):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in slc.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        slc_failures = [
            r for r in results
            if r.invariant_name.startswith("status_line_composer_")
        ]
        assert slc_failures == [], (
            f"StatusField pin failing after CC2 additions: "
            f"{[r.to_dict() for r in slc_failures]}"
        )
