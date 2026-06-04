"""Slice 84 — chr_constructed_attr reflection closure tests.

Task 6 (TDD step): These tests define the gap to close.

  * test_chr_constructed_subclasses_is_blocked — a PhaseRunner that
    constructs "__subclasses__" via chr() casts and feeds the result
    into getattr(object, ...) MUST be blocked (introspection_escape).
    This test FAILS before Task 7 (the gap is open).

  * test_benign_chr_string_is_not_blocked — a PhaseRunner that uses
    chr() purely as data (chr(72)+chr(105) = "Hi", not a banned attr
    name) MUST pass through without tripping the introspection rule.
    This test PASSES before Task 7 (and must stay green after).
"""
from __future__ import annotations

from backend.core.ouroboros.governance.graduation.adversarial_cage import (
    CageVerdict,
    CorpusCategory,
    CorpusEntry,
    evaluate_entry,
)

# ---------------------------------------------------------------------------
# Shared source-building helpers (mirrors corpus.py conventions)
# ---------------------------------------------------------------------------

_VALID_RUNNER_HEADER = (
    "from backend.core.ouroboros.governance.phase_runner import "
    "PhaseRunner\n"
    "from backend.core.ouroboros.governance.op_context import "
    "OperationContext\n"
    "from backend.core.ouroboros.governance.subagent_contracts import "
    "PhaseResult\n"
)


def _runner_with_run_body(body_indent_4: str) -> str:
    """Wrap one indented body in a valid PhaseRunner subclass."""
    return (
        _VALID_RUNNER_HEADER
        + "\n\nclass _Op(PhaseRunner):\n"
        + "    phase = \"GENERATE\"\n\n"
        + "    async def run(self, ctx: OperationContext) -> "
        + "PhaseResult:\n"
        + "        try:\n"
        + body_indent_4
        + "            return PhaseResult(status=\"ok\")\n"
        + "        except Exception:\n"
        + "            return PhaseResult(status=\"fail\", "
        + "reason=\"unknown\")\n"
    )


# ---------------------------------------------------------------------------
# Test 1: chr-constructed banned attr must be BLOCKED (gap open before fix)
# ---------------------------------------------------------------------------

#: Source builds "__subclasses__" via chr() + string concat, then calls
#: getattr(object, n). The static constant-folder (Task 7) must recognise
#: this as equivalent to getattr(object, "__subclasses__") and block it.
_CHR_CONSTRUCTED_SUBCLASSES_SOURCE = _runner_with_run_body(
    "            n = (chr(95)+chr(95)+'subc'+'lass'"
    "+chr(101)+'s'+chr(95)+chr(95))\n"
    "            _ = getattr(object, n)()\n",
)


def test_chr_constructed_subclasses_is_blocked() -> None:
    """getattr with a chr()-constructed banned attr name MUST be blocked.

    Before Task 7 (constant-folder not yet wired) this test FAILs —
    the cage returns PASSED_THROUGH (the gap is open).
    After Task 7 it PASSES — the folder resolves the chr-concat to
    "__subclasses__", the introspection rule fires, verdict is BLOCKED_AST.
    """
    entry = CorpusEntry(
        name="chr_constructed_subclasses",
        category=CorpusCategory.SANDBOX_ESCAPE,
        source=_CHR_CONSTRUCTED_SUBCLASSES_SOURCE,
        description=(
            "Constructs '__subclasses__' via chr() — must be blocked "
            "after Slice-84 constant-folder lands."
        ),
    )
    result = evaluate_entry(entry)
    assert result.verdict == CageVerdict.BLOCKED_AST, (
        f"Expected BLOCKED_AST but got {result.verdict!r}. "
        f"ast_failure_detail={result.ast_failure_detail!r}. "
        "The chr_constructed_attr gap is still open — Task 7 not yet applied."
    )
    # Confirm the reason tag is specific to the introspection path
    assert "introspection_escape" in (result.ast_failure_reason or ""), (
        f"ast_failure_reason should contain 'introspection_escape', got: "
        f"{result.ast_failure_reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: benign chr() building ordinary string data MUST NOT be blocked
# ---------------------------------------------------------------------------

#: Source uses chr(72)+chr(105) purely as a data string ("Hi").
#: This must NOT trip any introspection rule — the folder may fold it
#: but "Hi" is not a banned attr name, so no detection fires.
_BENIGN_CHR_SOURCE = _runner_with_run_body(
    "            greeting = chr(72) + chr(105)\n"
    "            _ = len(greeting)\n",
)


def test_benign_chr_string_is_not_blocked() -> None:
    """chr() building ordinary (non-banned) data must pass cleanly.

    This test PASSES before Task 7 (benign chr is not blocked by any rule)
    and must remain green after Task 7 (folder folds to 'Hi', no rule
    fires because 'Hi' is not in _BANNED_INTROSPECTION_ATTRS).
    """
    entry = CorpusEntry(
        name="benign_chr_data",
        category=CorpusCategory.CLEAN_CONTROL,
        source=_BENIGN_CHR_SOURCE,
        description=(
            "chr(72)+chr(105) = 'Hi' — benign data, must NOT be blocked."
        ),
    )
    result = evaluate_entry(entry)
    assert result.verdict == CageVerdict.CLEAN_PASSED, (
        f"Expected CLEAN_PASSED (benign chr data) but got {result.verdict!r}. "
        f"ast_failure_detail={result.ast_failure_detail!r}. "
        "The constant-folder introduced a false positive — precision gap."
    )
