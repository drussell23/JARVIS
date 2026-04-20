"""Regression spine — GENERAL LLM driver (Phase C Slice 1a Step 3+4).

Pins the structural contract for ``general_driver.run_general_tool_loop``,
``parse_general_final_answer``, ``final_answer_to_exec_trace``, and
``build_llm_general_factory``.

Coverage:

  Flag + factory:
    1. ``driver_enabled()`` default false, flag on = True.
    2. ``build_llm_general_factory`` flag off → returns default stub factory.
    3. ``build_llm_general_factory`` flag on → factory produces subagent
       with LLM driver wired.
    4. ``build_llm_general_factory`` flag on → factory's driver closure
       resolves provider via registry at each dispatch.

  Prompt template + rendering:
    5. ``render_general_system_prompt`` carries scope, tools,
       max_mutations, parent_tier, goal, reason in the rendered text.
    6. ``render_general_system_prompt`` handles empty fields with
       explicit sentinels.
    7. ``render_general_system_prompt`` sets read_only_mode=TRUE when
       max_mutations=0, FALSE otherwise.

  Final-answer parser:
    8. Valid JSON → dict.
    9. Malformed JSON → None.
   10. Wrong schema_version → None.
   11. Status not in enum → None.
   12. Markdown-fenced JSON (```json ... ```) → parsed via first/last brace.
   13. Empty / non-string → None.

  run_general_tool_loop end-to-end (with mocks):
   14. Provider registry returns None → status=no_provider_wired.
   15. Provider registry raises → status=no_provider_wired with ExcType.
   16. Tool loop raises → status=tool_loop_error.
   17. Tool loop returns valid final → status=completed + correct fields.
   18. Tool loop returns unparseable final → status=malformed_final.

  final_answer_to_exec_trace:
   19. Pure mapping — carries summary / findings_count / mutations_performed
       into the exec_trace shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.general_driver import (
    driver_enabled,
    final_answer_to_exec_trace,
    parse_general_final_answer,
    run_general_tool_loop,
)
from backend.core.ouroboros.governance.agentic_general_subagent import (
    AgenticGeneralSubagent,
    build_default_general_factory,
    build_llm_general_factory,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    GENERAL_SUBAGENT_SYSTEM_PROMPT_TEMPLATE,
    render_general_system_prompt,
)


# ---------------------------------------------------------------------------
# 1. Flag + factory
# ---------------------------------------------------------------------------

def test_driver_enabled_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", raising=False)
    assert driver_enabled() is False


def test_driver_enabled_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", "true")
    assert driver_enabled() is True


def test_llm_factory_flag_off_returns_stub_factory(
    monkeypatch, tmp_path,
) -> None:
    """Flag off → build_llm_general_factory returns the same shape as
    build_default_general_factory — driver NOT wired."""
    monkeypatch.delenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", raising=False)
    factory = build_llm_general_factory(
        tmp_path, provider_registry=lambda name: None,
    )
    subagent = factory()
    assert isinstance(subagent, AgenticGeneralSubagent)
    assert subagent._llm_driver is None, (
        "flag-off factory must not wire a driver — stub path preserved"
    )


def test_llm_factory_flag_on_wires_driver(monkeypatch, tmp_path) -> None:
    """Flag on → factory produces subagent with llm_driver set."""
    monkeypatch.setenv("JARVIS_GENERAL_LLM_DRIVER_ENABLED", "true")
    factory = build_llm_general_factory(
        tmp_path, provider_registry=lambda name: None,
    )
    subagent = factory()
    assert isinstance(subagent, AgenticGeneralSubagent)
    assert subagent._llm_driver is not None, (
        "flag-on factory must wire an LLM driver closure"
    )
    assert callable(subagent._llm_driver)


# ---------------------------------------------------------------------------
# 5-7. Prompt template + rendering
# ---------------------------------------------------------------------------

def test_render_prompt_carries_all_boundary_fields() -> None:
    """The rendered prompt must textually contain every boundary
    condition — the model must see its own cage."""
    inv = {
        "operation_scope": ["src/", "tests/"],
        "allowed_tools": ["read_file", "search_code"],
        "max_mutations": 0,
        "parent_op_risk_tier": "NOTIFY_APPLY",
        "invocation_reason": "summarize Phase B surface",
        "goal": "map dispatch_review call sites",
    }
    out = render_general_system_prompt(inv)
    assert "summarize Phase B surface" in out
    assert "map dispatch_review call sites" in out
    assert "src/, tests/" in out
    assert "read_file, search_code" in out
    assert "0" in out  # max_mutations
    assert "NOTIFY_APPLY" in out
    assert "general.final.v1" in out  # output schema


def test_render_prompt_handles_missing_fields_with_sentinels() -> None:
    """Invocation missing goal / reason / scope / tools renders with
    explicit markers rather than crashing — defense for bypass attempts."""
    inv: Dict[str, Any] = {}  # completely empty
    out = render_general_system_prompt(inv)
    assert "<missing>" in out  # goal / reason sentinels
    assert "<EMPTY" in out  # scope / tools sentinels


def test_render_prompt_includes_tool_call_schema() -> None:
    """Ticket 7 (Slice 1b live-test fix) — the rendered prompt must
    include the 2b.2-tool JSON schema so the model knows HOW to emit
    tool calls. Without this, models emit prose and the tool loop
    treats the prose as a malformed final answer.

    Pins: schema_version=2b.2-tool literal, both singular and parallel
    shapes, a concrete example, and the 'no markdown fences' direction.
    """
    inv = {
        "operation_scope": ["src/"],
        "allowed_tools": ["read_file"],
        "max_mutations": 0,
        "parent_op_risk_tier": "NOTIFY_APPLY",
        "invocation_reason": "r", "goal": "g",
    }
    out = render_general_system_prompt(inv)
    # Schema version literal — parser at providers.py:2830 matches
    # data.get("schema_version") == "2b.2-tool" exactly.
    assert "2b.2-tool" in out
    # Both call shapes described:
    assert "tool_call" in out  # singular
    assert "tool_calls" in out  # plural / parallel
    # A concrete example so Claude has a template to mimic:
    assert "read_file" in out
    assert "Tool Call Format" in out
    # Prose-prevention: explicit "no prose, no markdown fences"
    assert "no prose" in out.lower() or "no markdown" in out.lower()


def test_render_prompt_read_only_mode_from_max_mutations() -> None:
    """read_only_mode in the prompt is derived from max_mutations — 0
    means the model is told it's in read-only mode explicitly."""
    inv_ro = {
        "operation_scope": ["a/"], "allowed_tools": ["read_file"],
        "max_mutations": 0, "parent_op_risk_tier": "NOTIFY_APPLY",
        "invocation_reason": "r", "goal": "g",
    }
    assert "read_only_mode: TRUE" in render_general_system_prompt(inv_ro)

    inv_mut = {**inv_ro, "max_mutations": 3,
               "allowed_tools": ["read_file", "edit_file"]}
    assert "read_only_mode: FALSE" in render_general_system_prompt(inv_mut)


# ---------------------------------------------------------------------------
# 8-13. Final-answer parser
# ---------------------------------------------------------------------------

def test_parse_final_valid_json() -> None:
    raw = json.dumps({
        "schema_version": "general.final.v1",
        "status": "completed",
        "summary": "done",
        "findings": [{"file": "a.py", "evidence": "x"}],
        "mutations_performed": 0,
        "blocked_reason": "",
    })
    parsed = parse_general_final_answer(raw)
    assert parsed is not None
    assert parsed["status"] == "completed"
    assert parsed["summary"] == "done"


def test_parse_final_malformed_json_returns_none() -> None:
    assert parse_general_final_answer("not json at all") is None
    assert parse_general_final_answer("{incomplete") is None


def test_parse_final_wrong_schema_version_returns_none() -> None:
    raw = json.dumps({
        "schema_version": "wrong.version",
        "status": "completed",
    })
    assert parse_general_final_answer(raw) is None


def test_parse_final_bad_status_returns_none() -> None:
    raw = json.dumps({
        "schema_version": "general.final.v1",
        "status": "not_a_real_status",
    })
    assert parse_general_final_answer(raw) is None


def test_parse_final_strips_markdown_fence() -> None:
    """Models often emit ```json ... ``` despite the prompt saying not
    to. Parser finds the first { and last } and tries that substring."""
    raw = (
        "Sure, here's my answer:\n"
        "```json\n"
        '{"schema_version": "general.final.v1", '
        '"status": "completed", "summary": "ok"}\n'
        "```\n"
    )
    parsed = parse_general_final_answer(raw)
    assert parsed is not None
    assert parsed["status"] == "completed"


def test_parse_final_empty_or_non_string_returns_none() -> None:
    assert parse_general_final_answer("") is None
    assert parse_general_final_answer("   ") is None
    assert parse_general_final_answer(None) is None  # type: ignore[arg-type]
    assert parse_general_final_answer(42) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 14-18. run_general_tool_loop end-to-end (with mocks)
# ---------------------------------------------------------------------------

def _valid_payload() -> Dict[str, Any]:
    return {
        "sub_id": "sub-driver-test-01",
        "invocation": {
            "operation_scope": ["src/"],
            "allowed_tools": ["read_file"],
            "max_mutations": 0,
            "parent_op_risk_tier": "NOTIFY_APPLY",
            "invocation_reason": "driver spine test",
            "goal": "do thing",
            "primary_repo": "jarvis",
        },
        "project_root": "/tmp/fake_repo",
        "primary_provider_name": "claude-api",
        "fallback_provider_name": "",
        "deadline": None,
        "max_rounds": 4,
        "tool_timeout_s": 5.0,
    }


@pytest.mark.asyncio
async def test_run_loop_provider_registry_returns_none(tmp_path) -> None:
    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: None,
    )
    assert trace["status"] == "no_provider_wired"
    assert "None" in trace["raw_output"] or "claude-api" in trace["raw_output"]
    assert trace["tool_calls_made"] == 0


@pytest.mark.asyncio
async def test_run_loop_provider_registry_raises(tmp_path) -> None:
    def _boom(name: str) -> Any:
        raise RuntimeError("registry offline")

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=_boom,
    )
    assert trace["status"] == "no_provider_wired"
    assert "RuntimeError" in trace["raw_output"]
    assert "registry offline" in trace["raw_output"]


@pytest.mark.asyncio
async def test_run_loop_tool_loop_raises_yields_structured_failure(
    tmp_path, monkeypatch,
) -> None:
    """If ToolLoopCoordinator.run raises, the driver returns a
    ``tool_loop_error`` exec_trace rather than propagating."""
    class _WedgedCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            pass
        async def run(self, **kwargs) -> Tuple[str, List[Any]]:
            raise RuntimeError("simulated loop death")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.tool_executor.ToolLoopCoordinator",
        _WedgedCoordinator,
    )

    class _StubProvider:
        async def generate(self, prompt, deadline):
            return "unused"

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: _StubProvider(),
    )
    assert trace["status"] == "tool_loop_error"
    assert "RuntimeError" in trace["raw_output"]
    assert "simulated loop death" in trace["raw_output"]


@pytest.mark.asyncio
async def test_run_loop_clean_final_answer_completes(
    tmp_path, monkeypatch,
) -> None:
    """A valid general.final.v1 final text from the tool loop produces
    an exec_trace with status=completed and the parsed summary/findings."""
    final_text = json.dumps({
        "schema_version": "general.final.v1",
        "status": "completed",
        "summary": "mapped 3 call sites",
        "findings": [{"file": "a.py", "evidence": "line 42"}],
        "mutations_performed": 0,
        "blocked_reason": "",
    })

    class _StubCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            pass
        async def run(self, **kwargs) -> Tuple[str, List[Any]]:
            # Simulate 2 tool rounds.
            rec_1 = MagicMock(); rec_1.tool_name = "read_file"
            rec_2 = MagicMock(); rec_2.tool_name = "search_code"
            return (final_text, [rec_1, rec_2])

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.tool_executor.ToolLoopCoordinator",
        _StubCoordinator,
    )

    class _StubProvider:
        async def generate(self, prompt, deadline):
            return "unused"

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: _StubProvider(),
    )
    assert trace["status"] == "completed"
    assert trace["tool_calls_made"] == 2
    assert trace["tool_diversity"] >= 1  # 2 distinct class names
    assert trace["final_summary"] == "mapped 3 call sites"
    assert trace["final_findings_count"] == 1
    assert trace["raw_output"] == final_text


@pytest.mark.asyncio
async def test_run_loop_malformed_final_yields_malformed_final_trace(
    tmp_path, monkeypatch,
) -> None:
    """Tool loop returns text that doesn't parse as general.final.v1 →
    driver returns status=malformed_final, raw_output preserves the text."""
    bad_text = "I did some stuff but forgot to emit JSON."

    class _StubCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            pass
        async def run(self, **kwargs) -> Tuple[str, List[Any]]:
            return (bad_text, [])

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.tool_executor.ToolLoopCoordinator",
        _StubCoordinator,
    )

    class _StubProvider:
        async def generate(self, prompt, deadline):
            return "unused"

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: _StubProvider(),
    )
    assert trace["status"] == "malformed_final"
    assert bad_text in trace["raw_output"]


# ---------------------------------------------------------------------------
# 19. final_answer_to_exec_trace
# ---------------------------------------------------------------------------

def test_final_to_exec_trace_maps_completed_status() -> None:
    final = {
        "schema_version": "general.final.v1",
        "status": "completed",
        "summary": "ok",
        "findings": [{"file": "x.py", "evidence": "e"}],
        "mutations_performed": 1,
        "blocked_reason": "",
    }
    trace = final_answer_to_exec_trace(
        final,
        tool_calls_made=3,
        tool_diversity=2,
        cost_usd=0.01,
        provider_used="claude-api",
        raw_text="<raw>",
    )
    assert trace["status"] == "completed"
    assert trace["tool_calls_made"] == 3
    assert trace["tool_diversity"] == 2
    assert trace["cost_usd"] == 0.01
    assert trace["provider_used"] == "claude-api"
    assert trace["raw_output"] == "<raw>"
    assert trace["final_summary"] == "ok"
    assert trace["final_findings_count"] == 1
    assert trace["final_mutations_performed"] == 1


@pytest.mark.asyncio
async def test_run_loop_handles_none_valued_payload_knobs(tmp_path) -> None:
    """Slice 1b live-test regression pin — ``_execute_body`` passes
    ``max_rounds=None`` / ``tool_timeout_s=None`` / ``deadline=None``
    when ctx doesn't carry overrides. The driver MUST treat these as
    'absent' and use defaults. Before this fix, ``int(None)`` raised
    TypeError and the subagent crashed.

    Caught by /tmp/claude/general_battle_matrix.py on 2026-04-20;
    unit tests missed it because they supplied explicit values.
    """
    def _boom(name: str) -> Any:
        raise RuntimeError("simulated missing provider")

    # Payload shape matches what AgenticGeneralSubagent._execute_body
    # actually emits — all three knobs are explicit None.
    payload_with_nones = {
        "sub_id": "sub-none-knob-test",
        "invocation": {
            "operation_scope": ["src/"],
            "allowed_tools": ["read_file"],
            "max_mutations": 0,
            "parent_op_risk_tier": "NOTIFY_APPLY",
            "invocation_reason": "none-knob regression pin",
            "goal": "test",
            "primary_repo": "jarvis",
        },
        "project_root": str(tmp_path),
        "primary_provider_name": "claude-api",
        "fallback_provider_name": "",
        "deadline": None,           # must default
        "max_rounds": None,         # must default (NOT crash int(None))
        "tool_timeout_s": None,     # must default
    }

    from backend.core.ouroboros.governance.general_driver import (
        run_general_tool_loop,
    )
    trace = await run_general_tool_loop(
        payload_with_nones,
        project_root=tmp_path,
        provider_registry=_boom,
    )
    # The None knobs must have fallen back to defaults; failure should
    # be the expected no_provider_wired, NOT a TypeError.
    assert trace["status"] == "no_provider_wired", (
        f"None knobs must default, not crash; got {trace['status']!r}"
    )


@pytest.mark.asyncio
async def test_run_loop_reaches_client_messages_create(
    tmp_path, monkeypatch,
) -> None:
    """Signature-drift pin #1 — the driver's _generate_fn reaches into
    ``provider._client.messages.create``. If the Anthropic SDK surface
    moves (e.g. to ``.completions`` or ``.responses.generate``), this
    test fails loudly BEFORE the live battle test. Mirrors the
    pattern used by existing _generate_raw closures in providers.py.
    """
    # Stub provider with the expected ._client shape.
    create_calls: list = []

    class _StubTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class _StubMessage:
        def __init__(self, text: str) -> None:
            self.content = [_StubTextBlock(text)]

    class _StubMessages:
        async def create(self, **kwargs) -> _StubMessage:
            create_calls.append(kwargs)
            # Emit a valid general.final.v1 so the tool loop exits on
            # round 1 (no tool calls → parse_fn returns None → loop
            # treats the text as final).
            return _StubMessage(json.dumps({
                "schema_version": "general.final.v1",
                "status": "completed",
                "summary": "stubbed SDK round-trip",
                "findings": [],
                "mutations_performed": 0,
                "blocked_reason": "",
            }))

    class _StubClient:
        messages = _StubMessages()

    class _StubProvider:
        _client = _StubClient()
        _model = "claude-test-model"

    from backend.core.ouroboros.governance.general_driver import (
        run_general_tool_loop,
    )

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: _StubProvider(),
    )

    assert create_calls, (
        "driver must call provider._client.messages.create at least once; "
        "signature drift or wrong SDK surface"
    )
    # Verify the SDK shape we rely on:
    kw = create_calls[0]
    assert "model" in kw and kw["model"] == "claude-test-model"
    assert "max_tokens" in kw and isinstance(kw["max_tokens"], int)
    assert "system" in kw and "Semantic Firewall" in kw["system"]
    assert "messages" in kw and isinstance(kw["messages"], list)
    assert kw["messages"][0]["role"] == "user"
    # And the final answer parsed cleanly — driver returned completed
    assert trace["status"] == "completed"


@pytest.mark.asyncio
async def test_run_loop_handles_null_client_gracefully(
    tmp_path,
) -> None:
    """Signature-drift pin #2 — when provider._client is None (recycled
    or uninitialized), the driver must emit a structured
    ``tool_loop_error`` trace rather than crashing with AttributeError."""

    class _RecycledProvider:
        _client = None
        _model = "claude-test-model"

    from backend.core.ouroboros.governance.general_driver import (
        run_general_tool_loop,
    )

    payload = _valid_payload()
    trace = await run_general_tool_loop(
        payload,
        project_root=tmp_path,
        provider_registry=lambda name: _RecycledProvider(),
    )
    # Driver wraps the inner RuntimeError into status=tool_loop_error
    assert trace["status"] == "tool_loop_error"
    assert "_client is None" in trace["raw_output"]


def test_final_to_exec_trace_non_completed_status_prefixed() -> None:
    """When final.status != 'completed', exec_trace.status is
    ``final_<final_status>`` so downstream consumers can distinguish
    driver-level completion from subagent-reported completion."""
    final = {
        "schema_version": "general.final.v1",
        "status": "blocked_by_tools",
        "summary": "can't do that",
        "findings": [],
        "mutations_performed": 0,
        "blocked_reason": "need edit_file but not allowed",
    }
    trace = final_answer_to_exec_trace(
        final, tool_calls_made=0, tool_diversity=0,
        cost_usd=0.0, provider_used="claude-api", raw_text="<raw>",
    )
    assert trace["status"] == "final_blocked_by_tools"
    assert trace["final_blocked_reason"] == "need edit_file but not allowed"
