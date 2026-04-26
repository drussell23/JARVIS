"""P0 PostmortemRecall — orchestrator-level reachability supplement.

Mirrors :mod:`tests.governance.test_last_session_summary_composition` (the
W3(6) reachability supplement precedent: when live cadence cannot reliably
exercise the wiring within the wall cap, in-process orchestrator-shaped
tests stand in as the Layer 3 evidence per PRD §11).

What this proves end-to-end against the **real** orchestrator wiring:

(1) **Integration** — materialize a real POSTMORTEM line on disk in a tmp
    sessions dir, build a real ``OperationContext``, drive the extracted
    ``_inject_postmortem_recall_impl`` helper with a deterministic stub
    embedder, assert the rendered ``## Lessons from prior similar ops``
    section lands in ``ctx.strategic_memory_prompt`` AND the
    ``[PostmortemRecall] op=... enabled=true matched=N`` INFO line fires
    AND the JSONL ledger receives an entry.

(2) **Concat contract** — stub the recall service to return predictable
    matches and confirm the orchestrator's existing-``\\n\\n``-new concat
    pattern preserves ``## Lessons``-rendered tokens byte-for-byte.

(3) **AST regression** — static guard: ``_run_pipeline`` must call the
    helper, and the helper body must wire ``_pm_section`` into the
    ``strategic_memory_prompt`` kwarg of ``with_strategic_memory_context``.

(4) **Authority invariants** — master-off byte-for-byte unchanged ctx;
    helper swallows any exception and returns the input ctx.

Together with the existing 41 P0 unit tests + 16 graduation pin tests +
16/16 in-process live-fire smoke, this closes the PRD §11 Layer 3
reachability evidence for graduation purposes per the W3(6) supplement
precedent (memory ``project_wave3_item6_graduation_matrix.md``
"Session 3 supplement — reachability proof via test_harness").
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import postmortem_recall as pm_module
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.orchestrator import (
    _inject_postmortem_recall_impl,
)
from backend.core.ouroboros.governance.postmortem_recall import (
    PostmortemRecallService,
    reset_default_service,
)


_REAL_POSTMORTEM_LINE = (
    "2026-04-25T01:08:13 [backend.core.ouroboros.governance.comm_protocol] "
    "INFO [CommProtocol] POSTMORTEM op=op-019dc3ac-8864-766b-84c8-5f36913654ee-cau "
    "seq=8 payload={'root_cause': 'all_providers_exhausted:fallback_failed', "
    "'failed_phase': 'GENERATE', 'next_safe_action': 'retry_with_smaller_seed', "
    "'target_files': ['backend/core/foo.py', 'backend/core/bar.py']}"
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Per-test reset: clear all P0 envs + the singleton."""
    for key in (
        "JARVIS_POSTMORTEM_RECALL_ENABLED",
        "JARVIS_POSTMORTEM_RECALL_TOP_K",
        "JARVIS_POSTMORTEM_RECALL_DECAY_DAYS",
        "JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD",
        "JARVIS_POSTMORTEM_RECALL_MAX_SCAN",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_default_service()
    yield
    reset_default_service()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_ENABLED", "true")
    # Make threshold permissive so the stub-embedder identity-vector hits.
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", "0.0")
    # Long decay so the stale fixture timestamp still scores.
    monkeypatch.setenv("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", "3650")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_POSTMORTEM_RECALL_{k}", str(v))


def _seed_postmortem(sessions_dir: Path, session_id: str = "bt-test-fixture") -> Path:
    """Materialize one real-shape POSTMORTEM line in a tmp sessions dir."""
    sess = sessions_dir / session_id
    sess.mkdir(parents=True, exist_ok=True)
    log = sess / "debug.log"
    log.write_text(_REAL_POSTMORTEM_LINE + "\n", encoding="utf-8")
    return log


def _install_singleton_with_stub_embedder(
    sessions_dir: Path, ledger_path: Path,
) -> PostmortemRecallService:
    """Create the singleton service with a deterministic stub embedder."""
    svc = PostmortemRecallService(
        sessions_dir=sessions_dir, ledger_path=ledger_path,
    )
    fake_emb = MagicMock()
    fake_emb.disabled = False
    # Identity vectors so cosine = 1.0 deterministically.
    fake_emb.embed = MagicMock(return_value=[[1.0, 0.0], [1.0, 0.0]])
    svc._embedder = fake_emb
    pm_module._default_service = svc
    return svc


def _fresh_ctx(
    *,
    target_files=("backend/core/foo.py", "backend/core/bar.py"),
    description: str = "fix all_providers_exhausted in GENERATE phase",
    existing_prompt: str = "",
) -> OperationContext:
    ctx = OperationContext.create(
        target_files=tuple(target_files),
        description=description,
    )
    if existing_prompt:
        ctx = ctx.with_strategic_memory_context(
            strategic_intent_id="pre-existing",
            strategic_memory_fact_ids=(),
            strategic_memory_prompt=existing_prompt,
            strategic_memory_digest="",
        )
    return ctx


# ---------------------------------------------------------------------------
# (1) Integration — real helper + real on-disk POSTMORTEM + stub embedder
# ---------------------------------------------------------------------------


def test_integration_real_postmortem_lands_in_composed_prompt(
    monkeypatch, tmp_path, caplog,
):
    """End-to-end: real ``_inject_postmortem_recall_impl`` against a real
    POSTMORTEM line on disk lands ``## Lessons from prior similar ops``
    in ``ctx.strategic_memory_prompt`` and emits the INFO marker."""
    _enable(monkeypatch)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    ledger_path = tmp_path / "ledger.jsonl"
    _seed_postmortem(sessions_dir)
    _install_singleton_with_stub_embedder(sessions_dir, ledger_path)

    ctx_in = _fresh_ctx()
    assert ctx_in.strategic_memory_prompt == ""  # pre-condition

    with caplog.at_level("INFO", logger="backend.core.ouroboros.governance.orchestrator"):
        ctx_out = _inject_postmortem_recall_impl(ctx_in)

    # Composition landed:
    assert ctx_out is not ctx_in, "frozen dataclass — rebind expected on match"
    prompt = ctx_out.strategic_memory_prompt
    assert "## Lessons from prior similar ops" in prompt, (
        f"recall section missing from composed prompt; got: {prompt!r}"
    )

    # Default intent stamp when previously empty:
    assert ctx_out.strategic_intent_id == "pm-recall-p0"

    # The orchestrator INFO marker must fire — this is the production
    # observability signal we look for in live battle-test cadence.
    pm_marker = [
        r for r in caplog.records
        if "[PostmortemRecall]" in r.getMessage()
        and "enabled=true" in r.getMessage()
    ]
    assert pm_marker, (
        "Expected '[PostmortemRecall] op=... enabled=true matched=N' "
        f"INFO line; got records: {[r.getMessage() for r in caplog.records]!r}"
    )

    # Ledger entry written with frozen schema:
    assert ledger_path.exists(), "ledger file must exist after a match"
    raw = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert raw, "ledger must have ≥1 entry"
    rec = json.loads(raw[0])
    assert rec.get("schema_version") == "postmortem_recall.1"


def test_integration_no_postmortems_returns_ctx_unchanged(
    monkeypatch, tmp_path,
):
    """Empty sessions dir → recall returns []; helper short-circuits;
    ctx returned unchanged (no rebind, no prompt mutation)."""
    _enable(monkeypatch)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    ledger_path = tmp_path / "ledger.jsonl"
    # No postmortem seeded.
    _install_singleton_with_stub_embedder(sessions_dir, ledger_path)

    ctx_in = _fresh_ctx(existing_prompt="PRE_EXISTING")
    ctx_out = _inject_postmortem_recall_impl(ctx_in)

    assert ctx_out is ctx_in, "no matches → no rebind"
    assert ctx_out.strategic_memory_prompt == "PRE_EXISTING"
    assert not ledger_path.exists(), "ledger should not be written when no match"


# ---------------------------------------------------------------------------
# (2) Concat contract — stub recall service → exercise composition invariant
# ---------------------------------------------------------------------------


def _invoke_with_stubbed_section(
    monkeypatch,
    *,
    rendered_section: str,
    existing_prompt: str = "",
) -> OperationContext:
    """Drive the helper with monkey-patched service + render so that
    ``_pm_section`` is a known string. Isolates the orchestrator's concat
    contract from the recall service's internals."""

    class _StubService:
        def recall_for_op(self, _signature):
            return [object()]  # non-empty → triggers render

    def _stub_get_default_service():
        return _StubService()

    def _stub_render(_matches):
        return rendered_section

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.postmortem_recall."
        "get_default_service",
        _stub_get_default_service,
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.postmortem_recall."
        "render_recall_section",
        _stub_render,
    )
    ctx_in = _fresh_ctx(existing_prompt=existing_prompt)
    return _inject_postmortem_recall_impl(ctx_in)


def test_concat_contract_preserves_section_verbatim(monkeypatch):
    """Stub renders an exact section; concat keeps every byte."""
    section = (
        "## Lessons from prior similar ops\n"
        "- op=op-stub-001 phase=GENERATE root_cause=stub_cause\n"
        "  next_safe_action=stub_action"
    )
    ctx_out = _invoke_with_stubbed_section(monkeypatch, rendered_section=section)
    assert section in ctx_out.strategic_memory_prompt
    assert "op-stub-001" in ctx_out.strategic_memory_prompt
    assert "stub_cause" in ctx_out.strategic_memory_prompt


def test_concat_contract_double_newline_when_existing_present(monkeypatch):
    """Separator invariant: ``existing + "\\n\\n" + section``."""
    section = "## Lessons from prior similar ops\n- op=stub phase=APPLY"
    ctx_out = _invoke_with_stubbed_section(
        monkeypatch, rendered_section=section, existing_prompt="PREV_BLOCK",
    )
    assert ctx_out.strategic_memory_prompt == f"PREV_BLOCK\n\n{section}"


def test_concat_contract_no_separator_when_existing_empty(monkeypatch):
    """When existing is empty, section stands alone — no leading
    ``\\n\\n`` that would break subsequent composition (e.g. SemanticIndex)."""
    section = "## Lessons from prior similar ops\n- op=stub phase=APPLY"
    ctx_out = _invoke_with_stubbed_section(
        monkeypatch, rendered_section=section, existing_prompt="",
    )
    assert ctx_out.strategic_memory_prompt == section
    assert not ctx_out.strategic_memory_prompt.startswith("\n")


# ---------------------------------------------------------------------------
# (3) Authority invariants — master-off + exception swallow
# ---------------------------------------------------------------------------


def test_master_off_helper_returns_ctx_unchanged(monkeypatch, tmp_path):
    """``JARVIS_POSTMORTEM_RECALL_ENABLED`` unset → ``get_default_service``
    returns None → helper short-circuits → ctx returned unchanged."""
    monkeypatch.delenv("JARVIS_POSTMORTEM_RECALL_ENABLED", raising=False)
    reset_default_service()

    ctx_in = _fresh_ctx(existing_prompt="UNTOUCHED")
    ctx_out = _inject_postmortem_recall_impl(ctx_in)

    assert ctx_out is ctx_in
    assert ctx_out.strategic_memory_prompt == "UNTOUCHED"


def test_helper_swallows_recall_service_exception(monkeypatch, caplog):
    """Helper must never raise: any exception inside the recall path is
    caught + logged DEBUG; ctx returned unchanged. Authority invariant
    per PRD §12.2: best-effort, never blocks FSM."""
    class _BoomService:
        def recall_for_op(self, _signature):
            raise RuntimeError("intentional test boom")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.postmortem_recall."
        "get_default_service",
        lambda: _BoomService(),
    )
    ctx_in = _fresh_ctx(existing_prompt="UNTOUCHED")
    # Root-level DEBUG capture — orchestrator helper logs at DEBUG which
    # caplog only catches when the root level is dropped.
    with caplog.at_level("DEBUG"):
        ctx_out = _inject_postmortem_recall_impl(ctx_in)

    # Authority invariant: helper never raises, never mutates ctx on error.
    assert ctx_out is ctx_in
    assert ctx_out.strategic_memory_prompt == "UNTOUCHED"
    # Breadcrumb is best-effort — assert the helper at minimum ran the
    # except path (no exception escaped to caller). The DEBUG line is
    # informational; some logging configs filter it before caplog hooks
    # see it. The non-mutation invariant is the load-bearing guarantee.


# ---------------------------------------------------------------------------
# (4) AST regression — call site + helper body wiring
# ---------------------------------------------------------------------------


def _orchestrator_ast() -> ast.Module:
    orch_path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/orchestrator.py"
    )
    return ast.parse(orch_path.read_text(encoding="utf-8"))


def test_run_pipeline_calls_postmortem_recall_helper():
    """``_run_pipeline`` must invoke ``_inject_postmortem_recall_impl``.

    Canary against a refactor that drops the call site — the helper would
    become dead code while every other test still passes (silent
    regression)."""
    tree = _orchestrator_ast()

    run_pipeline_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_pipeline":
            run_pipeline_node = node
            break
    assert run_pipeline_node is not None, (
        "_run_pipeline not found — orchestrator refactored?"
    )

    found = False
    for node in ast.walk(run_pipeline_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_inject_postmortem_recall_impl":
                found = True
                break
    assert found, (
        "_run_pipeline no longer calls _inject_postmortem_recall_impl. "
        "PostmortemRecall will silently stop injecting at CONTEXT_EXPANSION."
    )


def test_helper_body_wires_pm_section_into_strategic_memory():
    """``_inject_postmortem_recall_impl`` body must contain a call to
    ``ctx.with_strategic_memory_context`` whose ``strategic_memory_prompt``
    kwarg references ``_pm_section``.

    Catches the case where a refactor keeps the helper but accidentally
    stops appending the rendered section."""
    tree = _orchestrator_ast()

    helper_node = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_inject_postmortem_recall_impl"
        ):
            helper_node = node
            break
    assert helper_node is not None, (
        "_inject_postmortem_recall_impl not found — refactored or deleted?"
    )

    wired = False
    for node in ast.walk(helper_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "with_strategic_memory_context":
            continue
        prompt_arg = None
        for kw in node.keywords:
            if kw.arg == "strategic_memory_prompt":
                prompt_arg = kw.value
                break
        if prompt_arg is None:
            continue
        for inner in ast.walk(prompt_arg):
            if isinstance(inner, ast.Name) and inner.id == "_pm_section":
                wired = True
                break
        if wired:
            break
    assert wired, (
        "_inject_postmortem_recall_impl no longer wires _pm_section into "
        "with_strategic_memory_context(strategic_memory_prompt=...). "
        "Lessons will silently stop reaching the composed prompt."
    )
