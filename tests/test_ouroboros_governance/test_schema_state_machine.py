"""Below the exploration floor, a premature answer must be UNREPRESENTABLE.

Levers 1 and 2 of Stateful Tool Masking are advisory and reactive: one edits
prose the model may ignore, the other rejects a patch the model has already
spent its whole token budget writing. ``tool_masking``'s own header states
the limit they share -- "enforcing exploration via prompt wording is
non-deterministic".

Measured, soak bt-2026-08-31-155308 (qwen3-coder:30b): **106 in-loop
rejections, every one at 0/2 exploration calls**, and 25 of 55 generations
never called a tool at all. Told, rejected, told again, rejected again, then
rejected post-hoc by the Iron Gate. Zero candidates from the entire soak. A
model that does not follow instructions is not fixed by more instructions.

Lever 3 removes the CAPABILITY instead of the permission: while the op is
below the floor, the sampler's JSON-Schema union is narrowed to the tool-call
shape, so a premature patch cannot be emitted at all.

Measured against the live engine, same prompts, same seeds, only the grammar
differing:

    full union (today)      ['<unparseable>', '2b.1', '<unparseable>']  0/3 explored
    {tool, noop}            ['2b.1-noop', '2b.1-noop', '2b.1-noop']     0/3 explored
    {tool} only  (shipped)  ['2b.2-tool', '2b.2-tool', '2b.2-tool']     3/3 explored

That middle row is why noop is withheld too, and it is the whole reason
these tests exist: the obvious design -- "narrow to tool calls and noop, so
it can still say the work is done" -- is taken 100% of the time and would
have converted 106 premature patches into 106 premature noops while looking
like a fix.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance import tool_masking as tm
from backend.core.ouroboros.governance.providers import (
    _NOOP_SCHEMA_VERSION,
    _SCHEMA_VERSION,
    _SCHEMA_VERSION_DIFF,
    _TOOL_SCHEMA_VERSION,
    build_response_json_schema,
)

_ENV_SCHEMA_MASK = "JARVIS_TOOL_SCHEMA_MASKING_ENABLED"


def _versions(schema: Dict[str, Any]) -> List[str]:
    return [b["properties"]["schema_version"]["const"] for b in schema["anyOf"]]


# --------------------------------------------------------------------------
# The grammar projection
# --------------------------------------------------------------------------


def test_unnarrowed_union_is_unchanged() -> None:
    """The default must stay byte-identical: all four legal answer shapes.

    The union is load-bearing -- constraining to candidates alone would make
    tool calls unrepresentable and the Venom loop could never run.
    """
    assert _versions(build_response_json_schema()) == [
        _SCHEMA_VERSION, _SCHEMA_VERSION_DIFF,
        _NOOP_SCHEMA_VERSION, _TOOL_SCHEMA_VERSION,
    ]


def test_narrowing_to_tool_removes_every_final_answer_shape() -> None:
    """Patch, diff AND noop all go. Each is an answer about an unread file."""
    got = _versions(build_response_json_schema(frozenset({_TOOL_SCHEMA_VERSION})))
    assert got == [_TOOL_SCHEMA_VERSION]
    assert _SCHEMA_VERSION not in got
    assert _SCHEMA_VERSION_DIFF not in got, (
        "the diff shape is a patch too -- narrowing that leaves it behind "
        "just moves the premature patch to the other branch"
    )
    assert _NOOP_SCHEMA_VERSION not in got, (
        "measured: with {tool, noop} the MoE took the noop escape 3/3 -- "
        "allowing it converts premature patches into premature noops"
    )


def test_shapes_are_defined_once_not_duplicated_per_grammar() -> None:
    """A narrowed branch must be the SAME object shape as the full one.

    If narrowing rebuilt the shapes, the constrained grammar and the
    validator could describe the same answer differently -- exactly the
    drift the builder was written as a projection to prevent.
    """
    full = build_response_json_schema()
    narrow = build_response_json_schema(frozenset({_TOOL_SCHEMA_VERSION}))
    full_tool = next(
        b for b in full["anyOf"]
        if b["properties"]["schema_version"]["const"] == _TOOL_SCHEMA_VERSION
    )
    assert narrow["anyOf"][0] == full_tool


def test_unmatched_allow_falls_back_to_the_full_union() -> None:
    """A grammar that permits nothing is a guaranteed dead-end.

    The caller's mistake must not become an unparseable response, so a
    typo'd allow-set degrades to the pre-existing behaviour.
    """
    assert len(_versions(build_response_json_schema(frozenset({"nonsense"})))) == 4
    # Empty/None are both "no narrowing", not "narrow to nothing".
    assert len(_versions(build_response_json_schema(frozenset()))) == 4
    assert len(_versions(build_response_json_schema(None))) == 4


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_ENV_SCHEMA_MASK, raising=False)
    tok = tm.set_exploration_state(explore_count=0, floor=0, may_finalize=True)
    yield
    tm.reset_exploration_state(tok)


def test_no_published_state_means_no_narrowing() -> None:
    """Every non-local provider, and the local lane outside a tool loop.

    The default answer must be "full union" or this lever would silently
    constrain callers that never opted into it.
    """
    tok = tm.set_exploration_state(explore_count=0, floor=2, may_finalize=False)
    tm.reset_exploration_state(tok)  # back to no-state
    # A fresh context has never had state set.
    assert asyncio.run(_in_fresh_task(tm.answer_shapes_allowed)) is None


async def _in_fresh_task(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


@pytest.mark.parametrize(
    ("explore", "floor", "may_finalize", "narrowed"),
    [
        (0, 2, False, True),   # nothing gathered -> constrain
        (1, 2, False, True),   # partial -> still constrain
        (2, 2, False, False),  # floor met -> unlock
        (5, 2, False, False),  # past the floor -> unlock
        (0, 2, True, False),   # deadline reserve reached -> release valve
        (0, 0, False, False),  # no floor configured -> nothing to enforce
    ],
)
def test_transition_is_driven_by_call_history(
    explore: int, floor: int, may_finalize: bool, narrowed: bool,
) -> None:
    """Dynamic on the executed-call count, never a round index.

    A round whose every tool call was denied by policy advances the round
    and gathers nothing, so counting rounds would unlock the patch shape
    for a model that has still read nothing.
    """
    tok = tm.set_exploration_state(
        explore_count=explore, floor=floor, may_finalize=may_finalize,
    )
    try:
        got = tm.answer_shapes_allowed()
    finally:
        tm.reset_exploration_state(tok)
    if narrowed:
        assert got == frozenset({_TOOL_SCHEMA_VERSION})
    else:
        assert got is None


def test_release_valve_beats_an_unmet_floor() -> None:
    """A model that cannot explore must still get to answer.

    Without this, an op whose rounds are exhausted would face a grammar it
    cannot satisfy and dead-end -- trading the premature-patch failure for
    a no-answer failure, which is worse.
    """
    tok = tm.set_exploration_state(explore_count=0, floor=2, may_finalize=True)
    try:
        assert tm.answer_shapes_allowed() is None
    finally:
        tm.reset_exploration_state(tok)


def test_master_flag_retreats_to_prior_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separate from JARVIS_TOOL_MASKING_ENABLED on purpose.

    This lever changes what the SAMPLER can emit -- a materially stronger
    intervention than editing prose -- so an operator must be able to
    retreat from it without also losing lever 1.
    """
    monkeypatch.setenv(_ENV_SCHEMA_MASK, "false")
    tok = tm.set_exploration_state(explore_count=0, floor=2, may_finalize=False)
    try:
        assert tm.answer_shapes_allowed() is None
        # Lever 1 is untouched by lever 3's switch.
        assert tm.masked_prompt(
            "x\n**Write tools (Iron-Gate-governed)**\nblah\n\ny",
            explore_count=0, floor=2,
        ) != "x\n**Write tools (Iron-Gate-governed)**\nblah\n\ny"
    finally:
        tm.reset_exploration_state(tok)


def test_state_does_not_leak_between_concurrent_ops() -> None:
    """Two ops generating at once must not see each other's floor.

    A ContextVar is per-task; a module global would have made op B's
    grammar depend on op A's exploration progress.
    """

    async def _op(explore: int, floor: int) -> Optional[frozenset]:
        tok = tm.set_exploration_state(
            explore_count=explore, floor=floor, may_finalize=False,
        )
        try:
            await asyncio.sleep(0)  # force interleaving
            return tm.answer_shapes_allowed()
        finally:
            tm.reset_exploration_state(tok)

    async def _both():
        return await asyncio.gather(_op(0, 2), _op(9, 2))

    below, above = asyncio.run(_both())
    assert below == frozenset({_TOOL_SCHEMA_VERSION})
    assert above is None


def test_bad_state_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any fault yields the full union, never a broken grammar."""
    monkeypatch.setattr(
        tm, "_EXPLORATION_STATE",
        type("_Boom", (), {"get": staticmethod(lambda: 1 / 0)})(),
    )
    assert tm.answer_shapes_allowed() is None


# --------------------------------------------------------------------------
# The call site
# --------------------------------------------------------------------------


def test_tool_loop_publishes_and_resets_around_generate() -> None:
    """The state is scoped to ONE generation call.

    Leaking a narrowed grammar past it would constrain whatever the task
    does next, and the reset must survive an exception from generate_fn --
    hence `finally`, which a substring check on the source can prove
    without booting the whole tool loop.
    """
    from pathlib import Path

    src = Path(
        "backend/core/ouroboros/governance/tool_executor.py"
    ).read_text(encoding="utf-8", errors="replace")

    set_at = src.index("set_exploration_state as _tm_set_state")
    gen_at = src.index("raw = await generate_fn(_prompt_for_model)")
    reset_at = src.index("reset_exploration_state as _tm_reset_state")
    assert set_at < gen_at < reset_at, (
        "state must be published BEFORE generate_fn and reset AFTER it"
    )
    between = src[gen_at:reset_at]
    assert "finally:" in between, (
        "the reset must run in a finally -- a generate_fn that raises would "
        "otherwise leak a narrowed grammar into the rest of the task"
    )
    # The count must come from the executed-call history, not a round index.
    window = src[set_at - 200:gen_at]
    assert "_cumulative_explore_calls" in window
    assert "round_index" not in window, (
        "the transition must evaluate the tool-call history, not step counts"
    )
