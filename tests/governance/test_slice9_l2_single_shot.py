"""Slice 9 — L2 single-shot fast path bypasses Venom tool loop.

Closes the deterministic tool-loop bail surfaced by soak
bt-2026-05-25-211028 after Slice 8 unblocked DW's signature:

  ERROR [L2 Repair] _generate_repair_candidate raised RuntimeError:
  tool_loop_starved_below_min_ttft_floor:round=1,remaining=97.25s,
  projected_per_round=9.69s,min_ttft_floor=45.00s

The BudgetPlan.is_next_round_viable gate fires when
``per_round_timeout < min(min_ttft_floor_s, max_per_round_s)``.
For L2 contexts, fair_share clamps to max_per_round_s (~10s)
which sits BELOW min_ttft_floor (45s) → effective_floor = 10s
→ per_round_timeout=9.69 < 10 → bail.

Both Slice 6.1 re-dispatches hit the SAME wall:
``history=['generate_error:RuntimeError', 'generate_error:RuntimeError']``

# Architectural insight

L2 has all context it needs to synthesize a fix:
  - target file path
  - original code
  - prior patch attempt
  - pytest failure output / critique

It does NOT need to EXPLORE via tools. Spinning up Venom for a
micro-fix is a category error — the model would never call a tool
in this context. Yet the tool-loop bail check still fires.

# Fix mechanism — repair_context as the single-shot signal

L2's ``_generate_repair_candidate`` is the ONLY call site that
passes ``repair_context=`` (verified by repo-wide grep:
repair_engine.py:727 and :1161). So presence of repair_context is
a reliable signal that we're on the L2 path.

In both PrimeProvider.generate and ClaudeProvider.generate, when
``repair_context is not None``, force ``_skip_tools = True``. The
existing gate ``if self._tool_loop is not None and not _skip_tools:``
already protects the tool-loop entry — set _skip_tools=True and
the model takes the non-tool single-shot path naturally.

# Discipline

* Hook key: ``repair_context is not None`` — no new parameter,
  no new env knob, no new state. The Protocol shape already
  carries the signal Slice 8 made uniform.
* Mirrored in BOTH providers (Prime + Claude). AST-pinned to
  catch drift.
* DoublewordProvider doesn't run a Venom tool loop in its
  generate path — its batch API is structurally single-shot
  already, so no edit needed there. (Slice 8 just gave it the
  kwarg.)
* Pre-existing route-based skips (background / speculative /
  wiring_validation) are preserved verbatim — Slice 9 only adds
  one more reason to skip, never disables an existing skip.

# Test surface (2 AST pins + 3 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "providers.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_both_providers_skip_tools_on_repair_context() -> None:
    """BOTH PrimeProvider.generate AND ClaudeProvider.generate must
    carry the Slice 9 guard ``if repair_context is not None and not
    _skip_tools: _skip_tools = True``. Without both, a provider chain
    routed to the un-patched provider deterministically bails inside
    the tool loop on the L2 path."""
    src = PROVIDERS_FILE.read_text()
    # The exact guard expression — both occurrences must be present
    guard_count = src.count("repair_context is not None and not _skip_tools")
    assert guard_count >= 2, (
        f"Expected ≥2 Slice 9 guards (Prime + Claude); found {guard_count}. "
        "If only one provider skips, the L2 path could still deterministically "
        "bail when route cascade picks the un-patched provider."
    )
    # The skip assignment must follow each guard
    skip_assignments = src.count("_skip_tools = True")
    assert skip_assignments >= 2, (
        f"Expected ≥2 _skip_tools = True assignments in Slice 9 blocks; "
        f"found {skip_assignments}"
    )
    # Slice 9 attribution + bt soak link present
    assert "Slice 9" in src
    assert "bt-2026-05-25-211028" in src, (
        "Missing soak attribution — future readers can't trace which "
        "diagnostic exposed this gap"
    )


def test_ast_pin_no_new_kwarg_added_to_provider_signatures() -> None:
    """Slice 9 explicitly uses repair_context as the signal (no new
    ``single_shot=True`` kwarg). This pin guards against future drift
    where someone adds a redundant parameter — keeping the Protocol
    shape Slice 8 fixed AS the single-shot signal."""
    tree = ast.parse(PROVIDERS_FILE.read_text(), filename=str(PROVIDERS_FILE))
    generates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate"
    ]
    # Each generate() arg list must NOT have a single_shot parameter
    for fn in generates:
        all_args = (
            [a.arg for a in fn.args.args]
            + [a.arg for a in fn.args.kwonlyargs]
        )
        assert "single_shot" not in all_args, (
            f"Slice 9 anti-pattern: ``generate`` at line {fn.lineno} "
            "added a single_shot kwarg. The design uses repair_context "
            "presence as the signal (avoid redundant Protocol surface)."
        )


# ──────────────────────────────────────────────────────────────────────
# Spine — 3
# ──────────────────────────────────────────────────────────────────────


def test_spine_guard_immediately_precedes_existing_tool_loop_gate() -> None:
    """The Slice 9 guard must come BEFORE the
    ``if self._tool_loop is not None and not _skip_tools:`` line
    so that flipping _skip_tools=True actually short-circuits the
    tool loop entry. AST-walk by source-line ordering."""
    src = PROVIDERS_FILE.read_text()
    lines = src.split("\n")

    # Find line numbers (1-indexed) of each Slice 9 guard
    guard_lines = [
        idx + 1 for idx, line in enumerate(lines)
        if "repair_context is not None and not _skip_tools" in line
    ]
    # Find line numbers of the tool_loop entry gates
    tool_loop_lines = [
        idx + 1 for idx, line in enumerate(lines)
        if "self._tool_loop is not None and not _skip_tools" in line
    ]

    assert len(guard_lines) >= 2, "Need ≥2 Slice 9 guards"
    assert len(tool_loop_lines) >= 2, "Need ≥2 tool_loop entry gates"

    # Each guard must precede a tool_loop gate (within a reasonable
    # window — same function body, typically <100 lines apart)
    for guard in guard_lines:
        nearest_gate = min(
            (gate for gate in tool_loop_lines if gate > guard),
            default=None,
        )
        assert nearest_gate is not None, (
            f"Slice 9 guard at line {guard} has no subsequent tool_loop "
            "gate — guard is dead code"
        )
        assert nearest_gate - guard < 100, (
            f"Slice 9 guard at line {guard} too far from tool_loop gate "
            f"at line {nearest_gate} (gap={nearest_gate - guard}) — "
            "likely in different function bodies"
        )


def test_spine_route_based_skip_preserved_verbatim() -> None:
    """Slice 9 must NOT alter the pre-existing route-based skip
    (background / speculative / wiring_validation). The new guard
    ADDS a skip reason; never removes an existing one. Regression
    guard: should_skip_venom_for_route still imported + called."""
    src = PROVIDERS_FILE.read_text()
    assert "should_skip_venom_for_route" in src, (
        "should_skip_venom_for_route reference lost — route-based "
        "skip broken by Slice 9 restructure"
    )
    # The pre-existing _skip_tools computation must still exist
    # (BEFORE the Slice 9 guard adds to it)
    assert "should_skip_venom_for_route(_route) and not _is_read_only" in src, (
        "Pre-existing route-based _skip_tools logic removed — "
        "regression"
    )


def test_spine_dw_dispatch_chain_threads_repair_context() -> None:
    """Slice 9.1 regression — when Slice 9's guard lives in DW's
    _generate_realtime (line 1670), the kwarg must be THREADED through
    the entire dispatch chain or it raises NameError at runtime
    (caught by Slice 7 traceback in bt-2026-05-25-213811).

    All three DW functions in the dispatch chain must carry the
    repair_context parameter:
      generate() → _dispatch_internal() → _generate_realtime()
    """
    dw_file = (
        REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
        / "doubleword_provider.py"
    )
    tree = ast.parse(dw_file.read_text(), filename=str(dw_file))

    chain_funcs = ("generate", "_dispatch_internal", "_generate_realtime")
    found = {name: False for name in chain_funcs}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name in chain_funcs
        ):
            args = (
                [a.arg for a in node.args.args]
                + [a.arg for a in node.args.kwonlyargs]
            )
            if "repair_context" in args:
                found[node.name] = True

    missing = [name for name, ok in found.items() if not ok]
    assert not missing, (
        f"Slice 9.1 dispatch-chain threading broken: {missing} "
        "missing repair_context parameter. NameError will fire "
        "at runtime when L2 routes to DW."
    )


def test_spine_dw_carries_slice9_guard_too() -> None:
    """DoublewordProvider DOES run a Venom tool loop in its
    _generate_realtime path. Slice 9 must apply there too so L2's
    DW-routed dispatches don't deterministically bail in DW's tool
    loop the way they did pre-Slice-9 in Prime/Claude.

    AST/text check: DW source must include the repair_context guard
    that flips its tool-skip flag to True."""
    dw_file = (
        REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
        / "doubleword_provider.py"
    )
    dw_src = dw_file.read_text()
    # DW uses _will_skip_tools (slightly different name vs Prime/Claude
    # which use _skip_tools). The Slice 9 guard must flip it.
    assert "repair_context is not None and not _will_skip_tools" in dw_src, (
        "DW missing Slice 9 guard — L2 dispatch via DW will still "
        "bail in DW's _generate_realtime tool loop"
    )
    # Slice 9 attribution comment present in DW too
    assert "Slice 9" in dw_src, (
        "DW missing Slice 9 attribution — future readers can't trace"
    )
