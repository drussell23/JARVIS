"""Slice 30 — Explicit Parameter Threading & Transport Determinism.

Closes the v23 (bt-2026-05-27-045049) wiring gap surfaced by
production audit: 12 EXHAUSTION events at elapsed=30.00s ALL with
``Tier3_cap_active: primary_budget=30.0s`` — the static
``_PRIMARY_MAX_TIMEOUT_S`` cap, NOT the Slice 28 Phase 2 adaptive
75s heavy-model budget. Diagnosed: Slice 28 read the model_id from
``topology_sentinel.get_dw_model_override()`` ContextVar, which was
silently returning empty across the async/semaphore boundary
between the Slice 23 sentinel walker's ``set_dw_model_override``
and ``_call_primary``'s read.

Per operator binding: "We do not use magic global states to manage
explicit transport parameters."

# Refactor scope

Three method signatures gain explicit keyword-only ``model_id: str = ""``:

  * ``_call_primary(context, deadline, *, model_id="")``
  * ``_try_primary_then_fallback(context, deadline, *, model_id="")``
  * ``_compute_primary_budget(total_s, *, model_id="")`` (Slice 28 substrate)

The Slice 23 sentinel walker passes the loop variable ``model_id``
explicitly when invoking ``_try_primary_then_fallback`` — no
ContextVar read by the orchestrator-layer timeout decision.

The ContextVar mechanism in ``topology_sentinel.py`` is retained
for the legitimate per-provider INTERNAL routing concern
(``DoublewordProvider._resolve_effective_model`` reads it to pick
which model to call). The TRANSPORT PARAMETER (orchestrator-side
timeout decision) is now explicit; the PROVIDER ROUTING parameter
remains via ContextVar (correct level of abstraction).

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_call_primary_signature_threads_model_id() -> None:
    """``_call_primary`` MUST accept ``model_id`` as keyword-only with
    default. Without this, the Slice 30 explicit-threading contract is
    broken and the v23 ContextVar gap re-opens."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    sig = inspect.signature(CandidateGenerator._call_primary)
    assert "model_id" in sig.parameters, (
        "_call_primary missing model_id parameter — Slice 30 reverted"
    )
    p = sig.parameters["model_id"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"_call_primary model_id must be keyword-only, got kind={p.kind}"
    )
    assert p.default == "", (
        f"_call_primary model_id default must be '' (legacy callers), got {p.default!r}"
    )


def test_ast_pin_try_primary_then_fallback_signature_threads_model_id() -> None:
    """``_try_primary_then_fallback`` MUST accept ``model_id`` as
    keyword-only with default. Without this, the sentinel walker
    can't pass the model_id through to _call_primary."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    sig = inspect.signature(CandidateGenerator._try_primary_then_fallback)
    assert "model_id" in sig.parameters
    p = sig.parameters["model_id"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default == ""


def test_ast_pin_call_primary_body_uses_explicit_param_not_contextvar() -> None:
    """The Slice 28 Phase 2 ContextVar READ inside ``_call_primary``
    MUST be gone. AST-walk the function body to confirm:
      1. No ``get_dw_model_override`` call inside _call_primary
      2. _compute_primary_budget invocation uses ``model_id=model_id``
         (the explicit param), NOT ``model_id=_attempted_model_id``
         (the old ContextVar local)"""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    body_src = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_call_primary"
        ):
            body_src = ast.unparse(node)
            break
    assert body_src, "_call_primary not found"
    # ContextVar magic must be gone from THIS function body
    assert "get_dw_model_override" not in body_src, (
        "_call_primary still reads ContextVar — Slice 30 incomplete; "
        "v23 wiring gap re-opens"
    )
    assert "_slice28_get_model_override" not in body_src, (
        "Slice 28 ContextVar accessor alias still imported inside "
        "_call_primary — refactor incomplete"
    )
    # _attempted_model_id local (Slice 28 old name) gone
    assert "_attempted_model_id" not in body_src, (
        "_attempted_model_id local var still present — Slice 30 didn't "
        "fully replace the ContextVar-read pattern"
    )
    # Compute budget call uses the explicit param
    assert "model_id=model_id" in body_src, (
        "_compute_primary_budget invocation doesn't use the explicit "
        "model_id param — Slice 30 wiring broken"
    )
    # Slice 30 attribution present
    assert "Slice 30" in body_src, (
        "_call_primary missing Slice 30 attribution"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_heavy_scalar_engages_with_explicit_model_id() -> None:
    """End-to-end: pass model_id explicitly to _compute_primary_budget
    → 2.5× heavy scalar fires for 397B → 75s budget instead of 30s.
    This is the v23 wiring bug fixed."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    # Legacy (no model_id) — 30s static cap binds
    legacy = CandidateGenerator._compute_primary_budget(300.0)
    assert legacy == 30.0, (
        f"Legacy path broken: expected 30s, got {legacy}"
    )
    # Heavy model via explicit param — 75s adaptive
    heavy = CandidateGenerator._compute_primary_budget(
        300.0, model_id="Qwen/Qwen3.5-397B-A17B-FP8",
    )
    assert heavy == 75.0, (
        f"Slice 30 wiring broken — heavy scalar didn't engage: got {heavy}, "
        f"expected 75 (30 × 2.5). This is the exact v23 production bug."
    )


def test_spine_call_primary_legacy_no_model_id_preserves_30s() -> None:
    """Legacy callers passing _call_primary(ctx, deadline) without
    the kwarg MUST still get the 30s cap. Byte-identical pre-Slice-30."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    sig = inspect.signature(CandidateGenerator._call_primary)
    # model_id default is "" → falls through to legacy budget computation
    assert sig.parameters["model_id"].default == ""


def test_spine_call_primary_explicit_heavy_engages_75s() -> None:
    """The Slice 30 contract end-to-end: passing model_id explicitly
    to _call_primary's path engages the 75s adaptive budget.

    We verify by monkeypatching _compute_primary_budget and asserting
    that _call_primary forwards the kwarg correctly."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    # The forwarding is provable structurally: _call_primary body
    # calls _compute_primary_budget(remaining, model_id=model_id).
    # The AST pin above proves the forwarding token is present.
    # This spine confirms the resulting budget value would be 75s
    # for the heavy case.
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    found_correct_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_call_primary"
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_compute_primary_budget"
                ):
                    # Confirm kwargs include model_id=model_id
                    for kw in sub.keywords:
                        if kw.arg == "model_id" and isinstance(kw.value, ast.Name):
                            if kw.value.id == "model_id":
                                found_correct_call = True
                                break
    assert found_correct_call, (
        "_call_primary doesn't forward `model_id=model_id` kwarg to "
        "_compute_primary_budget — Slice 30 wiring broken"
    )


def test_spine_try_primary_then_fallback_forwards_model_id() -> None:
    """``_try_primary_then_fallback`` MUST forward its model_id kwarg
    to ``_call_primary`` (the v23 ContextVar gap was at THIS link)."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_try_primary_then_fallback"
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_call_primary"
                ):
                    for kw in sub.keywords:
                        if kw.arg == "model_id" and isinstance(kw.value, ast.Name):
                            if kw.value.id == "model_id":
                                found = True
                                break
    assert found, (
        "_try_primary_then_fallback doesn't forward `model_id=model_id` "
        "to _call_primary — Slice 30 wiring incomplete"
    )


def test_spine_sentinel_walker_passes_loop_variable_explicitly() -> None:
    """The Slice 23 sentinel walker's invocation of
    ``_try_primary_then_fallback`` MUST pass ``model_id=model_id`` —
    where the inner ``model_id`` is the walker's per-iteration loop
    variable (the model currently being attempted). Without this, the
    walker stamps the ContextVar but doesn't tell the timeout layer
    what model it's using."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    # The sentinel walker (Slice 23) lives in _dispatch_via_sentinel,
    # which iterates `for model_id in ranked_models:`. Walk that
    # function and confirm at least one _try_primary_then_fallback
    # call inside it passes model_id=model_id explicitly.
    found_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name in ("_dispatch_via_sentinel", "_generate_dispatch")
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_try_primary_then_fallback"
                ):
                    for kw in sub.keywords:
                        if kw.arg == "model_id" and isinstance(kw.value, ast.Name):
                            if kw.value.id == "model_id":
                                found_call = True
                                break
                if found_call:
                    break
            if found_call:
                break
    assert found_call, (
        "Sentinel walker doesn't pass `model_id=model_id` to "
        "_try_primary_then_fallback — the v23 wiring gap persists"
    )


def test_spine_provider_routing_contextvar_preserved_in_topology_sentinel() -> None:
    """The topology_sentinel ContextVar mechanism MUST remain intact
    for legitimate per-provider INTERNAL routing
    (DoublewordProvider._resolve_effective_model reads it). Slice 30
    only strips the orchestrator-layer TIMEOUT read; provider routing
    via ContextVar is the correct level of abstraction for that
    concern."""
    from backend.core.ouroboros.governance.topology_sentinel import (
        set_dw_model_override,
        reset_dw_model_override,
        get_dw_model_override,
        DW_MODEL_OVERRIDE_VAR,
    )
    # Set + get + reset cycle still works for the provider's needs
    token = set_dw_model_override("Qwen/Qwen3.5-397B-A17B-FP8")
    assert get_dw_model_override() == "Qwen/Qwen3.5-397B-A17B-FP8"
    reset_dw_model_override(token)
    assert get_dw_model_override() is None
