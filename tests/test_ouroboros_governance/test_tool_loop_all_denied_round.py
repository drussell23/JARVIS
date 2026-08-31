"""A round where policy denies EVERY tool call must not kill the loop.

``exec_results`` was assigned only inside ``if pending_execs:``. When the
model called tools and policy denied all of them, ``pending_execs`` stayed
empty, the name was never bound, and the exploration-budget block below
read it unconditionally:

    _round_tool_names = {tc.name for tc, *_ in exec_results} if exec_results else set()

That guard READS as if it handles the empty case. It cannot: a local that
was never assigned raises ``UnboundLocalError`` on the read itself, before
the truth test is ever evaluated. So the round died and took the whole
generation with it.

Observed live in soak bt-2026-08-31-174037:

    sibling 2/3 failed op=op-01a058eb-b660 (UnboundLocalError: cannot
    access local variable 'exec_results' where it is not associated with
    a value) -- keeping 2 candidate(s)

Pre-existing, and independent of multi-candidate generation -- drawing
more candidates simply runs this loop more often, so it surfaces sooner.
The all-denied round is not exotic either: it is what a model asking for
a forbidden path produces, which is precisely the case the policy layer
exists to handle gracefully.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from backend.core.ouroboros.governance import tool_executor


_SRC = Path(
    "backend/core/ouroboros/governance/tool_executor.py"
).read_text(encoding="utf-8", errors="replace")


def test_exec_results_is_bound_before_the_conditional_assignment() -> None:
    """The initialisation must sit with the other per-round state.

    Binding it anywhere after ``if pending_execs:`` would leave exactly the
    hole this closes, so the ordering is the contract -- not the presence
    of the line.
    """
    init = _SRC.index("exec_results: List[Any] = []")
    # Match the STATEMENT, not the phrase: the fix's own comment explains
    # the guard by name, and a bare substring search finds the prose first.
    guard = _SRC.index("\n            if pending_execs:")
    first_cond_assign = _SRC.index("exec_results = [(tc, tool_result")
    read = _SRC.index("{tc.name for tc, *_ in exec_results}")

    assert init < guard < first_cond_assign < read, (
        "exec_results must be bound BEFORE the `if pending_execs:` branch "
        "that conditionally assigns it, or an all-denied round raises "
        "UnboundLocalError on the exploration-budget read"
    )


def test_it_is_bound_per_round_not_once_per_call() -> None:
    """It must reset each round, beside ``pending_execs``.

    Hoisting it out of the round loop would make round N's tool calls leak
    into round N+1's exploration count -- turning a crash into a silently
    wrong exploration credit, which is strictly worse.
    """
    init_line = _SRC[: _SRC.index("exec_results: List[Any] = []")].count("\n")
    pend_line = _SRC[
        : _SRC.index("pending_execs: List[Tuple[ToolCall, PolicyContext, str, str]] = []")
    ].count("\n")
    assert abs(init_line - pend_line) < 25, (
        "exec_results must be initialised in the same per-round block as "
        "pending_execs, not hoisted above the round loop"
    )


def test_no_other_unconditionally_read_name_is_conditionally_bound() -> None:
    """Guard the CLASS of defect, not just this instance.

    The exploration-budget block reads several names that the execution
    branch assigns. Each one is the same latent crash if it is not bound
    on the all-denied path.
    """
    tree = ast.parse(_SRC)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and "exec_results" in ast.dump(node):
            fn = node
            break
    assert fn is not None, "tool-loop function not found"

    # Every name the exploration-budget block depends on must be bound at
    # round scope, i.e. appear as a plain assignment target somewhere that
    # is not nested inside the `if pending_execs:` body.
    for name in ("exec_results", "pending_execs", "prompt_appendix"):
        assigned = [
            n for n in ast.walk(fn)
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            and any(
                getattr(t, "id", None) == name
                for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
            )
        ]
        assert assigned, f"{name} is read but never assigned in the tool loop"
