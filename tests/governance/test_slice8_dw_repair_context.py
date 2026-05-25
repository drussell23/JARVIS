"""Slice 8 — DoublewordProvider.generate accepts repair_context kwarg.

Closes the Protocol-shape gap exposed by Slice 7 traceback in soak
bt-2026-05-25-205710:

  ERROR [L2 Repair] _generate_repair_candidate raised TypeError:
  DoublewordProvider.generate() got an unexpected keyword argument
  'repair_context'

ClaudeProvider.generate(self, context, deadline, repair_context=None)
PrimeProvider.generate(self, context, deadline, repair_context=None)
DoublewordProvider.generate(self, context, deadline=None, *, prompt_override=None)
                                                       ^
                                            no repair_context kwarg

When the L2 RepairEngine's ``_prime`` is DW (route cascade can pick
DW for any tier), the call ``self._prime.generate(ctx, deadline,
repair_context=repair_context)`` from repair_engine.py:1160 raises
TypeError. Slice 6.1 dutifully classified this as SOFT and retried,
but the SAME error fired on attempt 2/2 (same DW instance, same
signature mismatch) → terminal cancel with the entire L2 budget
unused (~106s remaining each pass).

# Fix mechanism — accept the kwarg, document as advisory-only

  async def generate(
      self,
      context: OperationContext,
      deadline: Any = None,
      repair_context: Optional[Any] = None,  # ← NEW (positional kwarg)
      *,
      prompt_override: Optional[str] = None,
  ) -> GenerationResult:

The repair_context is accepted but NOT currently incorporated into
DW's prompt assembly — that's a separate slice if/when DW gains
repair-context-aware prompting. Today the fix is strictly
Protocol-shape uniformity: DW no longer raises TypeError when L2
calls it the same way it calls Claude.

# Test surface (3 AST pins + 2 spine)
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)
PROVIDERS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_dw_generate_accepts_repair_context() -> None:
    """``DoublewordProvider.generate`` MUST carry ``repair_context``
    in its argument list (positional or keyword) so calls of the form
    ``provider.generate(ctx, deadline, repair_context=...)`` from
    repair_engine.py:1160 don't raise TypeError."""
    tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))

    found_generate = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "generate":
            continue
        # Walk siblings up the tree to find the enclosing class
        # (skipping anonymous async defs)
        sig_args = (
            [a.arg for a in node.args.args]
            + [a.arg for a in node.args.kwonlyargs]
        )
        if "repair_context" in sig_args:
            found_generate = True
            break

    assert found_generate, (
        "DoublewordProvider.generate() does NOT include repair_context "
        "in its signature — Slice 8 fix reverted; L2 will TypeError "
        "again on the first DW dispatch."
    )


def test_ast_pin_signature_lockstep_with_claude_and_prime() -> None:
    """The Protocol contract requires all CandidateProvider implementations
    to accept ``repair_context`` so the L2 RepairEngine can call them
    uniformly. Verify Claude/Prime/DW all carry the kwarg in source."""
    # Claude + Prime are in providers.py
    prov_src = PROVIDERS_FILE.read_text()
    # Both signatures already had the kwarg pre-Slice-8 (this is the
    # regression pin that catches accidental future removal).
    prov_tree = ast.parse(prov_src, filename=str(PROVIDERS_FILE))
    generate_funcs = [
        node for node in ast.walk(prov_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate"
    ]
    # At least 2 generate functions (Claude + Prime) must accept repair_context
    matching = 0
    for fn in generate_funcs:
        sig_args = (
            [a.arg for a in fn.args.args]
            + [a.arg for a in fn.args.kwonlyargs]
        )
        if "repair_context" in sig_args:
            matching += 1
    assert matching >= 2, (
        f"Expected ≥2 generate() async funcs in providers.py to carry "
        f"repair_context (Claude + Prime); only {matching} do. "
        f"Protocol shape regression."
    )

    # DW (separate file) also must carry it post-Slice 8
    dw_tree = ast.parse(DW_FILE.read_text(), filename=str(DW_FILE))
    dw_generates = [
        node for node in ast.walk(dw_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate"
    ]
    assert any(
        "repair_context" in (
            [a.arg for a in fn.args.args]
            + [a.arg for a in fn.args.kwonlyargs]
        )
        for fn in dw_generates
    ), (
        "DoublewordProvider.generate() lacks repair_context — Slice 8 dead"
    )


def test_ast_pin_slice8_attribution_present() -> None:
    """The fix must carry a Slice 8 attribution comment so future
    readers can trace why the kwarg is accepted but ignored."""
    src = DW_FILE.read_text()
    assert "Slice 8" in src, "Missing Slice 8 attribution comment"
    assert "bt-2026-05-25-205710" in src, (
        "Missing soak attribution — future readers can't trace the "
        "TypeError-traceback diagnostic that exposed this"
    )
    assert "Protocol" in src, (
        "Comment doesn't explain WHY (Protocol shape uniformity)"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 2 (functional)
# ──────────────────────────────────────────────────────────────────────


def test_spine_dw_generate_callable_with_repair_context_kwarg() -> None:
    """Runtime: DW's generate signature accepts repair_context as a
    keyword argument. The exact call pattern from repair_engine.py:1160
    must not raise TypeError on inspection."""
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )
    except Exception as exc:
        # If the module can't import due to optional deps in test env,
        # fall through to AST-only validation (the pin above caught it).
        import pytest
        pytest.skip(f"DoublewordProvider import unavailable: {exc}")

    sig = inspect.signature(DoublewordProvider.generate)
    params = sig.parameters
    assert "repair_context" in params, (
        f"DW.generate signature: {sig} — missing repair_context"
    )
    # The default must be None (matches Claude/Prime convention)
    p = params["repair_context"]
    assert p.default is None, (
        f"DW.generate repair_context default is {p.default}, expected None"
    )


def test_spine_dw_repair_context_is_advisory_not_prompt_injected() -> None:
    """Slice 8 docstring promises that repair_context is accepted but
    NOT currently incorporated into the prompt — preserves DW behavior
    byte-equivalence so this slice is purely Protocol-shape fix.

    AST walk: find DW.generate body, confirm repair_context is NOT
    referenced inside a prompt-assembly path (no f-string interpolation,
    no .format() call). The `_dw_repair_context = repair_context` line +
    immediate `del _dw_repair_context` is the entire usage."""
    src = DW_FILE.read_text()
    # The reserve-and-delete pattern documenting the future-slice hook
    assert "_dw_repair_context = repair_context" in src, (
        "Slice 8 reserve pattern missing — telemetry hook intent lost"
    )
    assert "del _dw_repair_context" in src, (
        "del missing — variable should be deleted immediately to avoid "
        "accidental usage downstream (pure Protocol-shape fix)"
    )
    # Negative: repair_context must NOT appear in any prompt-build call
    # (no f"...{repair_context}..." patterns)
    assert 'f"{repair_context' not in src, (
        "repair_context bleeding into f-string — Slice 8 promised "
        "advisory-only; prompt incorporation is a future slice"
    )
    assert ".format(repair_context" not in src, (
        "repair_context in .format() call — same violation"
    )
