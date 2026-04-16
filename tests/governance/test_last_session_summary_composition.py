"""LastSessionSummary v1.1a composition tests — closes caveat 1.

Battle-test Runs 1-2 proved the mechanism fires: ``LastSessionSummary``
injects a rendered line at CONTEXT_EXPANSION, the observability contract
emits ``chars_out>0``, and the helper is clean. What those runs did
**not** prove is that v2 ``ops_digest`` tokens (``apply=MODE/N``,
``verify=P/T``, ``commit=HASH[:10]``) actually survive orchestrator
composition and land in the final ``ctx.strategic_memory_prompt`` the
model sees.

Existing test coverage stops at ``LastSessionSummary.format_for_prompt()``
in isolation — a refactor that rewired ``_run_pipeline``'s composition
step could silently drop LSS output while every v1.1a unit test still
passes. These three tests close that gap:

(1) **Integration** — materialize a full v2 ``ops_digest`` fixture,
    drive the extracted ``_inject_last_session_summary_impl`` helper,
    assert the dense tokens land in the composed prompt alongside any
    pre-existing Strategic/Bridge/Semantic content.

(2) **Concat contract** — stub LSS output and confirm the orchestrator's
    existing-``\\n\\n``-new concat pattern does not corrupt or truncate
    LSS tokens at composition.

(3) **AST regression** — static guard: ``_run_pipeline`` must call the
    helper, and the helper body must append ``_lss_prompt`` into the
    ``strategic_memory_prompt`` kwarg of ``with_strategic_memory_context``.
    Mirrors the observer-call-site AST check at
    :mod:`tests.governance.test_last_session_summary_v1_1a`.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.governance import (
    last_session_summary as lss,
    ops_digest_observer as odo,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.orchestrator import (
    _inject_last_session_summary_impl,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_LAST_SESSION_SUMMARY_"):
            monkeypatch.delenv(key, raising=False)
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    odo.reset_ops_digest_observer()
    yield
    lss.reset_default_summary()
    lss.set_active_session_id(None)
    odo.reset_ops_digest_observer()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_LAST_SESSION_SUMMARY_{k}", str(v))


def _write_v2_summary(
    root: Path,
    session_id: str,
    *,
    ops_digest: Dict[str, Any],
    stats_attempted: int = 2,
) -> Path:
    """Write a minimal v2 summary.json with a populated ops_digest."""
    payload = {
        "schema_version": 2,
        "session_id": session_id,
        "stop_reason": "idle_timeout",
        "duration_s": 300.0,
        "stats": {
            "attempted": stats_attempted,
            "completed": stats_attempted,
            "failed": 0,
            "cancelled": 0,
            "queued": 0,
        },
        "cost_total": 0.1,
        "cost_breakdown": {"claude": 0.1},
        "branch_stats": {
            "commits": 1, "files_changed": 4,
            "insertions": 200, "deletions": 50,
        },
        "strategic_drift": {"ratio": 0.0, "status": "ok"},
        "convergence_state": "IMPROVING",
        "ops_digest": ops_digest,
    }
    session_dir = root / ".ouroboros" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "summary.json"
    path.write_text(json.dumps(payload))
    return path


def _fresh_ctx(
    *,
    existing_prompt: str = "",
    existing_intent: str = "",
    target_files=("src/foo.py",),
) -> OperationContext:
    ctx = OperationContext.create(
        target_files=target_files,
        description="composition-test op",
    )
    if existing_prompt or existing_intent:
        ctx = ctx.with_strategic_memory_context(
            strategic_intent_id=existing_intent or "pre-existing-intent",
            strategic_memory_fact_ids=(),
            strategic_memory_prompt=existing_prompt,
            strategic_memory_digest="",
        )
    return ctx


# ---------------------------------------------------------------------------
# (1) Integration: full v2 ops_digest fixture → tokens in composed prompt
# ---------------------------------------------------------------------------


def test_integration_v2_ops_digest_tokens_land_in_composed_prompt(
    monkeypatch, tmp_path
):
    """Real ``_inject_last_session_summary_impl`` with a v2 fixture must
    land ``apply=multi/4 verify=20/20 commit=0890a7b6f0`` in the
    final ``ctx.strategic_memory_prompt``.
    """
    _enable(monkeypatch)
    _write_v2_summary(
        tmp_path,
        "bt-2026-04-15-999999",
        ops_digest={
            "last_apply_mode": "multi",
            "last_apply_files": 4,
            "last_apply_op_id": "op-integration-001",
            "last_verify_tests_passed": 20,
            "last_verify_tests_total": 20,
            # 40-char full hash; render truncates to 10.
            "last_commit_hash": "0890a7b6f09123456789abcdef0123456789abcd",
        },
    )
    ctx_in = _fresh_ctx()
    assert ctx_in.strategic_memory_prompt == ""  # pre-condition

    ctx_out = _inject_last_session_summary_impl(tmp_path, ctx_in)

    assert ctx_out is not ctx_in  # frozen dataclass — rebind happened
    prompt = ctx_out.strategic_memory_prompt
    assert prompt, "composed prompt must be non-empty after injection"

    # The three v1.1a tokens must all appear in the composed prompt.
    assert "apply=multi/4" in prompt, f"apply token missing; prompt={prompt!r}"
    assert "verify=20/20" in prompt, f"verify token missing; prompt={prompt!r}"
    assert "commit=0890a7b6f0" in prompt, f"commit token missing; prompt={prompt!r}"

    # Truncation invariant: full 40-char hash must NOT leak.
    assert "0890a7b6f09123" not in prompt

    # Strategic intent id gets a default when previously empty.
    assert ctx_out.strategic_intent_id == "last-session-v1"


def test_integration_preserves_pre_existing_strategic_memory(
    monkeypatch, tmp_path
):
    """When ``ctx.strategic_memory_prompt`` already has Strategic /
    Bridge / Semantic content, the LSS block must be **appended** via
    ``\\n\\n``, not overwrite the prior content.
    """
    _enable(monkeypatch)
    _write_v2_summary(
        tmp_path,
        "bt-2026-04-15-888888",
        ops_digest={
            "last_apply_mode": "single",
            "last_apply_files": 1,
            "last_commit_hash": "abcdef0123456789abcdef0123",
        },
    )
    existing = (
        "## Manifesto Principles\n- ALL-IN-ON-LOCAL\n\n"
        "<conversation untrusted=\"true\">\nprior turn\n</conversation>"
    )
    ctx_in = _fresh_ctx(
        existing_prompt=existing,
        existing_intent="strategic-v1",
    )

    ctx_out = _inject_last_session_summary_impl(tmp_path, ctx_in)

    prompt = ctx_out.strategic_memory_prompt
    # Strategic header must still be present AND first.
    assert prompt.startswith("## Manifesto Principles"), (
        "pre-existing strategic content must lead; LSS appends"
    )
    # Conversation bridge content preserved intact.
    assert "<conversation untrusted=\"true\">" in prompt
    # LSS tokens landed after pre-existing content.
    assert "apply=single/1" in prompt
    assert "commit=abcdef0123" in prompt
    assert prompt.index("Manifesto") < prompt.index("apply=single/1")
    # Append separator is double newline, per orchestrator contract.
    assert "\n\n" in prompt

    # Intent id preserved — not overwritten with the "last-session-v1" default.
    assert ctx_out.strategic_intent_id == "strategic-v1"


def test_integration_empty_ops_digest_still_renders_v1_line(
    monkeypatch, tmp_path
):
    """v1.1a backward-compat: ``ops_digest={}`` falls through to the v1
    one-liner shape. Prompt must still include the session id, but no
    apply/verify/commit tokens.
    """
    _enable(monkeypatch)
    _write_v2_summary(
        tmp_path,
        "bt-2026-04-15-777777",
        ops_digest={},
    )
    ctx_in = _fresh_ctx()
    ctx_out = _inject_last_session_summary_impl(tmp_path, ctx_in)
    prompt = ctx_out.strategic_memory_prompt
    assert "bt-2026-04-15-777777" in prompt
    assert "apply=" not in prompt
    assert "verify=" not in prompt
    assert "commit=" not in prompt


def test_integration_disabled_returns_ctx_unchanged(monkeypatch, tmp_path):
    """Master switch off — helper is a no-op. Hash identity must match."""
    # Intentionally NOT calling _enable().
    _write_v2_summary(
        tmp_path,
        "bt-2026-04-15-666666",
        ops_digest={"last_apply_mode": "multi", "last_apply_files": 2},
    )
    ctx_in = _fresh_ctx(existing_prompt="prior content")
    ctx_out = _inject_last_session_summary_impl(tmp_path, ctx_in)
    # Same object reference (no rebind) — helper exited before
    # with_strategic_memory_context could mint a new instance.
    assert ctx_out is ctx_in
    assert ctx_out.strategic_memory_prompt == "prior content"
    assert "apply=" not in ctx_out.strategic_memory_prompt


def test_integration_missing_summary_directory_does_not_raise(
    monkeypatch, tmp_path
):
    """Helper must swallow any exception from LSS (missing dir, bad
    fixture, OS errors) and return ``ctx`` unchanged — never propagate.
    """
    _enable(monkeypatch)
    # No .ouroboros/sessions/* created — LSS will discover zero records.
    ctx_in = _fresh_ctx(existing_prompt="survive unchanged")
    ctx_out = _inject_last_session_summary_impl(tmp_path, ctx_in)
    # LSS emits enabled=true / chars_out=0 and skips the concat branch.
    # ctx identity is preserved because no rebind fired.
    assert ctx_out is ctx_in
    assert ctx_out.strategic_memory_prompt == "survive unchanged"


# ---------------------------------------------------------------------------
# (2) Concat contract — orchestrator's existing + "\n\n" + new pattern
# ---------------------------------------------------------------------------
#
# This stubs LSS output and exercises only the composition line shape.
# A regression that e.g. strips whitespace or truncates at a char limit
# in the orchestrator path would fail here even if LSS itself is clean.


def _invoke_with_stub_lss(
    monkeypatch,
    tmp_path,
    *,
    stub_output: str,
    existing_prompt: str = "",
    enabled: bool = True,
) -> OperationContext:
    """Drive the helper with a monkey-patched LSS that returns a known
    rendered string. Isolates the concat contract from LSS internals."""
    class _StubLSS:
        def inject_metrics(self):
            # (enabled, n_sessions, session_id, chars_out, hash8)
            return (enabled, 1, "bt-stub-0001", len(stub_output), "deadbeef")

        def format_for_prompt(self):
            return stub_output

    def _stub_factory(_root):
        return _StubLSS()

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.last_session_summary."
        "get_default_summary",
        _stub_factory,
    )
    ctx_in = _fresh_ctx(existing_prompt=existing_prompt)
    return _inject_last_session_summary_impl(tmp_path, ctx_in)


def test_concat_contract_preserves_lss_tokens_verbatim(monkeypatch, tmp_path):
    """Stub LSS emits the v1.1a tokens; concat must keep every byte."""
    stub = (
        "## Previous Session Closure\n"
        "- stub: cost=$0.10 apply=multi/7 verify=15/16 "
        "commit=0123456789 branch=+42/-8"
    )
    ctx_out = _invoke_with_stub_lss(
        monkeypatch, tmp_path, stub_output=stub
    )
    prompt = ctx_out.strategic_memory_prompt
    # Byte-exact substring — concat must not mangle the rendered line.
    assert stub in prompt
    assert "apply=multi/7" in prompt
    assert "verify=15/16" in prompt
    assert "commit=0123456789" in prompt


def test_concat_contract_adds_double_newline_between_existing_and_lss(
    monkeypatch, tmp_path
):
    """Separator invariant: ``existing + "\\n\\n" + lss`` when existing
    is non-empty."""
    stub = "STUB_LSS_LINE apply=single/1"
    ctx_out = _invoke_with_stub_lss(
        monkeypatch, tmp_path,
        stub_output=stub,
        existing_prompt="PREV_CONTENT",
    )
    assert ctx_out.strategic_memory_prompt == f"PREV_CONTENT\n\n{stub}"


def test_concat_contract_no_separator_when_existing_empty(
    monkeypatch, tmp_path
):
    """When existing is empty, LSS line stands alone — no leading
    ``\\n\\n`` that would break subsequent composition."""
    stub = "STUB_LSS_LINE apply=single/1"
    ctx_out = _invoke_with_stub_lss(
        monkeypatch, tmp_path,
        stub_output=stub,
        existing_prompt="",
    )
    assert ctx_out.strategic_memory_prompt == stub
    assert not ctx_out.strategic_memory_prompt.startswith("\n")


def test_concat_contract_lss_empty_output_skips_rebind(monkeypatch, tmp_path):
    """When LSS emits empty string (no prior session found), the INFO
    line still fires but NO rebind of strategic_memory_prompt happens.
    ctx identity must be preserved."""
    ctx_out = _invoke_with_stub_lss(
        monkeypatch, tmp_path,
        stub_output="",
        existing_prompt="keep-me",
        enabled=True,
    )
    # enabled=True + empty LSS output → INFO fires, no rebind, identity kept.
    assert ctx_out.strategic_memory_prompt == "keep-me"


# ---------------------------------------------------------------------------
# (3) AST regression — _run_pipeline must call the helper,
#     helper body must wire _lss_prompt into strategic_memory_prompt
# ---------------------------------------------------------------------------


def _orchestrator_ast() -> ast.Module:
    orch_path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    return ast.parse(orch_path.read_text(encoding="utf-8"))


def test_run_pipeline_calls_lss_injection_helper():
    """``_run_pipeline`` must invoke ``_inject_last_session_summary_impl``.

    Rationale: the integration + concat tests above prove the helper
    works. If a future refactor drops the call site from ``_run_pipeline``,
    the helper becomes dead code and every v1.1a test still passes —
    silent regression. This AST check is the canary for that specific
    failure mode.
    """
    tree = _orchestrator_ast()

    # Find the _run_pipeline method body.
    run_pipeline_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_pipeline":
            run_pipeline_node = node
            break
    assert run_pipeline_node is not None, (
        "_run_pipeline not found — orchestrator refactored?"
    )

    # Walk _run_pipeline's body looking for a call to
    # _inject_last_session_summary_impl.
    found = False
    for node in ast.walk(run_pipeline_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_inject_last_session_summary_impl":
                found = True
                break
    assert found, (
        "_run_pipeline no longer calls _inject_last_session_summary_impl. "
        "LastSessionSummary will silently stop injecting at CONTEXT_EXPANSION."
    )


def test_helper_body_wires_lss_prompt_into_strategic_memory():
    """``_inject_last_session_summary_impl`` body must contain a call
    to ``ctx.with_strategic_memory_context`` whose
    ``strategic_memory_prompt`` kwarg references ``_lss_prompt``.

    Catches the case where a refactor keeps the helper but accidentally
    stops appending LSS output (e.g. passes ``_existing`` alone, or
    nulls the kwarg).
    """
    tree = _orchestrator_ast()

    helper_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_inject_last_session_summary_impl"
        ):
            helper_node = node
            break
    assert helper_node is not None, (
        "_inject_last_session_summary_impl not found — refactored or deleted?"
    )

    # Find ctx.with_strategic_memory_context(...) call within the helper.
    wired = False
    for node in ast.walk(helper_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "with_strategic_memory_context":
            continue
        # Find the strategic_memory_prompt kwarg's value.
        prompt_arg = None
        for kw in node.keywords:
            if kw.arg == "strategic_memory_prompt":
                prompt_arg = kw.value
                break
        if prompt_arg is None:
            continue
        # Walk the expression for any Name("_lss_prompt") — covers
        # both ``_lss_prompt`` and ``_existing + "\n\n" + _lss_prompt``
        # conditional/binop shapes.
        for inner in ast.walk(prompt_arg):
            if isinstance(inner, ast.Name) and inner.id == "_lss_prompt":
                wired = True
                break
        if wired:
            break
    assert wired, (
        "_inject_last_session_summary_impl no longer wires _lss_prompt into "
        "with_strategic_memory_context(strategic_memory_prompt=...). "
        "Tokens will silently stop reaching the composed prompt."
    )
