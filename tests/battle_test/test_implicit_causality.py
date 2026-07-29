"""Causality flows through the async context, not through signatures.

`op_fanout_tree` (777L) renders a parent/child op graph from
`OpBlockBuffer`. The store had the fields, the renderer had the walk, both
had master flags — and `register_parent`, the one method that records an
edge, had **zero call sites**. Every hit in the tree was a docstring.

The reason nobody called it is structural: every candidate call site would
have needed a `parent_op_id` threaded down to it. So a ContextVar carries
it instead — which is exactly what an async parent/child relationship IS:
a value scoped to the task that set it, inherited by what that task
spawns, invisible to its siblings.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.op_block_buffer import (
    OpBlockBuffer,
    active_parent_op,
    executing,
)


@pytest.fixture(autouse=True)
def _graph_on(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "1")
    yield


class TestTheEdgeRecordsItself:
    def test_an_op_minted_inside_another_is_its_child(self):
        b = OpBlockBuffer()
        with executing("root"):
            b.start_op("root")
            b.start_op("child")
        assert b.get_parent_op_id("child") == "root"
        assert "child" in b.get_child_op_ids("root")

    def test_no_context_means_no_parent(self):
        """A root op is a root. Inventing a parent would be worse than
        the flat tree this replaces."""
        b = OpBlockBuffer()
        b.start_op("lonely")
        assert b.get_parent_op_id("lonely") in ("", None)

    def test_nothing_is_threaded_through_a_signature(self):
        """The DRY mandate: `start_op` must not have grown a
        `parent_op_id` parameter."""
        import inspect
        params = inspect.signature(OpBlockBuffer.start_op).parameters
        assert set(params) == {"self", "op_id"}

    def test_re_starting_an_op_does_not_re_link(self):
        """`start_op` is idempotent for an active op; a retry must not
        append a duplicate child edge."""
        b = OpBlockBuffer()
        with executing("root"):
            b.start_op("root")
            b.start_op("child")
            b.start_op("child")
        assert b.get_child_op_ids("root").count("child") == 1


class TestAsyncIsolation:
    @pytest.mark.asyncio
    async def test_parallel_parents_do_not_leak_into_each_other(self):
        """THE mandated proof. Two parents running concurrently must not
        adopt each other's children."""
        b = OpBlockBuffer()

        async def branch(parent: str, child: str, delay: float):
            with executing(parent):
                b.start_op(parent)
                await asyncio.sleep(delay)      # interleave deliberately
                b.start_op(child)

        await asyncio.gather(
            branch("A", "a1", 0.02),
            branch("B", "b1", 0.01),
        )
        assert b.get_parent_op_id("a1") == "A"
        assert b.get_parent_op_id("b1") == "B"
        assert b.get_child_op_ids("A") == ("a1",)
        assert b.get_child_op_ids("B") == ("b1",)

    @pytest.mark.asyncio
    async def test_a_spawned_task_INHERITS_the_context(self):
        """Inheritance is the whole reason this is a ContextVar and not a
        thread-local or a global."""
        b = OpBlockBuffer()

        async def grandchild():
            b.start_op("gc")

        with executing("root"):
            b.start_op("root")
            await asyncio.create_task(grandchild())
        assert b.get_parent_op_id("gc") == "root"

    @pytest.mark.asyncio
    async def test_a_sibling_started_after_does_NOT_inherit(self):
        b = OpBlockBuffer()
        with executing("root"):
            b.start_op("root")
        b.start_op("after")          # outside the frame
        assert b.get_parent_op_id("after") in ("", None)

    def test_nesting_restores_the_predecessor(self):
        assert active_parent_op() == ""
        with executing("outer"):
            with executing("inner"):
                assert active_parent_op() == "inner"
            assert active_parent_op() == "outer", "reset() lost the frame"
        assert active_parent_op() == ""

    def test_the_token_is_reset_not_reassigned(self):
        """Under concurrency these differ: `reset(token)` restores THIS
        frame's predecessor, while assigning a saved value can clobber a
        sibling task that ran in between."""
        import inspect
        src = inspect.getsource(executing)
        assert ".reset(" in src and ".set(" in src


class TestTheDAGStaysAcyclic:
    def test_a_direct_cycle_is_refused(self):
        b = OpBlockBuffer()
        with executing("root"):
            b.start_op("root")
            b.start_op("child")
        assert b.register_parent(
            child_op_id="root", parent_op_id="child") is False

    def test_self_parent_is_refused(self):
        b = OpBlockBuffer()
        b.start_op("solo")
        assert b.register_parent(
            child_op_id="solo", parent_op_id="solo") is False

    def test_a_deep_cycle_is_refused(self):
        b = OpBlockBuffer()
        # `b` must be minted under `a`'s frame, not its own: starting an op
        # inside `executing(<itself>)` is a self-parent, which is correctly
        # REFUSED — so the first draft of this test built a two-node chain
        # and then asserted a three-node cycle was caught.
        with executing("a"):
            b.start_op("a")
            b.start_op("b")
        with executing("b"):
            b.start_op("c")
        assert b.get_parent_op_id("b") == "a"
        assert b.get_parent_op_id("c") == "b"
        # a -> b -> c already; making `a` a child of `c` closes the loop.
        assert b.register_parent(child_op_id="a", parent_op_id="c") is False

    def test_a_legitimate_deep_chain_is_allowed(self):
        b = OpBlockBuffer()
        b.start_op("x"); b.start_op("y")
        assert b.register_parent(child_op_id="y", parent_op_id="x") is True

    def test_the_walk_is_depth_capped(self):
        """A corrupt store could present a chain with no cycle and no end;
        the render happens on the operator's frame."""
        import inspect
        src = inspect.getsource(OpBlockBuffer.register_parent)
        assert "_hops" in src and "64" in src


class TestNeverRaises:
    @pytest.mark.parametrize("bad", [None, "", 42, object()])
    def test_junk_op_ids_degrade(self, bad):
        b = OpBlockBuffer()
        with executing(bad):          # type: ignore[arg-type]
            b.start_op(bad)           # type: ignore[arg-type]

    def test_a_failing_link_never_breaks_start_op(self, monkeypatch):
        b = OpBlockBuffer()
        monkeypatch.setattr(
            b, "register_parent",
            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with executing("root"):
            assert b.start_op("child") is not None

    def test_the_body_exception_is_not_swallowed(self):
        with pytest.raises(ValueError):
            with executing("root"):
                raise ValueError("must propagate")
