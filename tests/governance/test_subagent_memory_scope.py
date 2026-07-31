"""Regression spine for subagent memory scoping.

The defect this closes is not "subagents lack memory". It is a BOUNDARY whose
crossing rule existed only as the absence of code — answered by omission, at
four call sites, unreadable afterwards.

So the tests split in two, and the second half is the important half:

* **Policy** — each type resolves to the scope its epistemic role argues for,
  overrides work, and the one override that would route around the Semantic
  Firewall is refused.
* **Enforcement** — an AST invariant proving no subagent executor reads the
  parent's memory off ``parent_ctx``. Today that is true by accident; this
  is what converts it into a property, and it is the test that will still be
  earning its keep in a year.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.memory_scope import (
    MemoryScope,
    render_scope_lines,
    resolve_scope,
    scope_for,
)

GOVERNANCE = Path(__file__).resolve().parents[2] / (
    "backend/core/ouroboros/governance")

#: Every executor that receives a SubagentContext / parent_ctx.
EXECUTORS = (
    "exploration_subagent.py",
    "agentic_plan_subagent.py",
    "agentic_review_subagent.py",
    "agentic_general_subagent.py",
)

#: Parent-context attributes that carry architecture memory. Reading any of
#: these inside an executor is an UNDECLARED crossing — memory entering a
#: subagent without passing the policy, which is exactly the state this arc
#: ended.
MEMORY_ATTRS = frozenset({
    "strategic_memory_prompt",
    "strategic_memory_digest",
    "strategic_memory_fact_ids",
    "human_instructions",
})


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subagent_type,expected", [
    ("explore", MemoryScope.NONE),
    ("review", MemoryScope.COMPLEMENT),
    ("plan", MemoryScope.INHERIT),
    ("general", MemoryScope.NONE),
])
def test_each_type_resolves_to_its_argued_default(subagent_type, expected):
    assert scope_for(subagent_type) is expected


def test_unknown_subagent_type_gets_nothing():
    """An undecided boundary resolves to "nothing crosses", not to a guess."""
    assert scope_for("refactor") is MemoryScope.NONE
    assert scope_for("") is MemoryScope.NONE
    assert scope_for(None) is MemoryScope.NONE


def test_master_gate_off_denies_every_type(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_ENABLED", "0")
    for name in ("explore", "review", "plan", "general"):
        assert scope_for(name) is MemoryScope.NONE


def test_operator_may_widen_a_type_that_policy_permits(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_EXPLORE", "independent")
    assert scope_for("explore") is MemoryScope.INDEPENDENT


def test_operator_may_narrow_any_type(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_REVIEW", "none")
    assert scope_for("review") is MemoryScope.NONE


@pytest.mark.parametrize("widening", ["inherit", "independent", "complement"])
def test_general_cannot_be_widened_by_an_env_var(monkeypatch, widening):
    """The load-bearing refusal.

    GENERAL sits behind the Semantic Firewall because it is the most
    injection-vulnerable surface in the system. A policy a deployment can
    widen by setting a string routes around that reasoning without ever
    touching the firewall.
    """
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_GENERAL", widening)
    assert scope_for("general") is MemoryScope.NONE


def test_general_may_still_be_narrowed(monkeypatch):
    """Refusal is directional — tightening is always allowed."""
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_GENERAL", "none")
    assert scope_for("general") is MemoryScope.NONE


def test_garbage_override_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBAGENT_MEMORY_SCOPE_PLAN", "yes-please")
    assert scope_for("plan") is MemoryScope.INHERIT


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class _ParentCtx:
    op_id = "op-parent"
    strategic_memory_prompt = "## Relevant Architecture Memory\n\ninherited"


def _resolve(**kw):
    base = dict(
        subagent_type="plan", parent_op_id="op-parent", parent_ctx=_ParentCtx(),
        subagent_id="op-parent::sub-1", goal="do a thing",
        target_files=("a.py",), project_root=Path("."),
    )
    base.update(kw)
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        resolve_scope(**base))


def test_none_scope_carries_no_section():
    resolution = _resolve(subagent_type="explore")
    assert resolution.scope is MemoryScope.NONE
    assert resolution.section == ""
    assert resolution.carries_memory is False


def test_inherit_scope_reuses_the_parents_rendered_section():
    resolution = _resolve(subagent_type="plan")
    assert resolution.scope is MemoryScope.INHERIT
    assert "inherited" in resolution.section


def test_inherit_from_a_parent_with_no_memory_is_not_an_error():
    class Bare:
        op_id = "op-parent"

    resolution = _resolve(subagent_type="plan", parent_ctx=Bare())
    assert resolution.section == ""
    # The scope still RESOLVED — an empty inheritance is a successful
    # inheritance of nothing, not a denial. Collapsing the two would report
    # "policy withheld memory" for a parent that simply had none.
    assert resolution.scope is MemoryScope.INHERIT
    assert "inherit" in resolution.detail


def test_resolution_never_raises_on_a_hostile_parent():
    class Hostile:
        @property
        def strategic_memory_prompt(self):
            raise RuntimeError("boom")

    assert _resolve(subagent_type="plan", parent_ctx=Hostile()).section == ""


def test_every_dispatch_files_a_record_including_the_denied_ones():
    """A boundary you cannot observe is one you are trusting, not enforcing."""
    from backend.core.ouroboros.governance.memory_admission import (
        latest_record, reset_default_registry,
    )
    reset_default_registry()
    _resolve(subagent_type="explore", subagent_id="op-parent::sub-9")
    record = latest_record()
    assert record is not None
    assert record.op_id == "op-parent::sub-9"
    assert record.consumer.value == "explore"
    assert record.admitted_count == 0
    assert record.extra.get("scope") == "none"
    # "withheld by policy" must not render identically to "the ledger was off"
    assert "policy" in record.query


# ---------------------------------------------------------------------------
# COMPLEMENT — the scope Claude Code cannot express
# ---------------------------------------------------------------------------


def test_complement_withholds_what_the_parent_was_shown(tmp_path, monkeypatch):
    """A reviewer handed the author's topics inherits the author's blind spot."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")
    from backend.core.ouroboros.governance.module_routing import (
        ModuleContextRouter,
    )

    topics = tmp_path / "docs" / "memory_topics" / "d"
    topics.mkdir(parents=True)
    for i in range(4):
        (topics / f"t{i}.md").write_text(
            f"---\nmodules: [a.py]\n---\n\n# T{i}\n\nbody {i}\n")

    loop = asyncio.get_event_loop_policy().new_event_loop()
    router = ModuleContextRouter(tmp_path, topics_dir=topics)

    first = loop.run_until_complete(router.route(
        ["a.py"], "q", max_topics=2, op_id="parent", consumer="main"))
    seen = [t.content_hash for t in first.topics]
    assert len(seen) == 2

    second = loop.run_until_complete(router.route(
        ["a.py"], "q", max_topics=2, op_id="reviewer", consumer="review",
        exclude_hashes=seen))
    got = [t.content_hash for t in second.topics]

    assert got, "complement returned nothing — the reviewer would be blind"
    assert not (set(got) & set(seen)), "reviewer saw the author's topics"


def test_excluded_topics_are_recorded_with_their_own_reason(
    tmp_path, monkeypatch,
):
    """"Deliberately not shown" and "ranked low" are different facts."""
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionReason,
    )
    from backend.core.ouroboros.governance.module_routing import (
        ModuleContextRouter,
    )

    topics = tmp_path / "docs" / "memory_topics" / "d"
    topics.mkdir(parents=True)
    for i in range(3):
        (topics / f"t{i}.md").write_text(
            f"---\nmodules: [a.py]\n---\n\n# T{i}\n\nbody {i}\n")

    loop = asyncio.get_event_loop_policy().new_event_loop()
    router = ModuleContextRouter(tmp_path, topics_dir=topics)
    first = loop.run_until_complete(router.route(
        ["a.py"], "q", max_topics=1, op_id="p", consumer="main"))
    excluded = [t.content_hash for t in first.topics]

    result = loop.run_until_complete(router.route(
        ["a.py"], "q", max_topics=1, op_id="r", consumer="review",
        exclude_hashes=excluded))
    rows = {r.content_hash: r for r in result.record.rows}
    assert rows[excluded[0]].reason is AdmissionReason.SCOPE_EXCLUDED


def test_complement_with_no_parent_history_is_plain_independent():
    """Cold start must not blank the reviewer."""
    from backend.core.ouroboros.governance.memory_admission import (
        reset_default_registry,
    )
    from backend.core.ouroboros.governance.memory_scope import (
        _parent_admitted_hashes,
    )
    reset_default_registry()
    assert _parent_admitted_hashes("op-never-routed") == ()


# ---------------------------------------------------------------------------
# Enforcement — the half that keeps earning its keep
# ---------------------------------------------------------------------------


def _undeclared_memory_reads(source: str) -> List[str]:
    """Attribute reads of parent memory inside *source*, via AST.

    AST rather than grep because prose is the dominant content of these
    files: a docstring naming ``strategic_memory_prompt`` is discussion, and
    a substring match would fail on it forever while teaching everyone to
    ignore the test.
    """
    found: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in MEMORY_ATTRS:
            found.append(node.attr)
        # getattr(parent_ctx, "strategic_memory_prompt") — the same crossing
        # spelled dynamically, which an attribute walk alone would miss.
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in MEMORY_ATTRS):
            found.append(str(node.args[1].value))
    return found


@pytest.mark.parametrize("executor", EXECUTORS)
def test_no_executor_reads_parent_memory_directly(executor):
    """The structural invariant.

    Subagents inherit no architecture memory today because nobody wired it,
    not because anything forbade it. That is an accident, and an accident
    holds only until the next person needs context in a hurry. Memory must
    enter a subagent through ``memory_scope`` — where the crossing is
    declared, refusable, and recorded — or not at all.
    """
    path = GOVERNANCE / executor
    if not path.is_file():
        pytest.skip(f"{executor} not present")
    leaks = _undeclared_memory_reads(path.read_text(encoding="utf-8"))
    assert not leaks, (
        f"{executor} reads parent memory {sorted(set(leaks))} directly, "
        f"bypassing the scope policy")


def test_the_invariant_detector_actually_detects():
    """A guard nobody has seen fail is a guard nobody knows works."""
    assert _undeclared_memory_reads(
        "x = parent_ctx.strategic_memory_prompt") == [
        "strategic_memory_prompt"]
    assert _undeclared_memory_reads(
        'x = getattr(parent_ctx, "human_instructions", "")') == [
        "human_instructions"]
    # Prose must NOT trip it — the reason this is an AST walk.
    assert _undeclared_memory_reads(
        '"""We deliberately never read strategic_memory_prompt here."""') == []


def test_scope_is_applied_at_the_only_construction_site():
    """One builder, and both dispatch paths must scope what it returns.

    A boundary rule enforced at some construction sites is a boundary rule
    that does not exist.
    """
    source = (GOVERNANCE / "subagent_orchestrator.py").read_text(
        encoding="utf-8")
    assert source.count("SubagentContext(") == 1, (
        "a second SubagentContext construction site would bypass scoping")
    assert source.count("_apply_memory_scope(") == 3, (
        "expected one definition plus the single and parallel dispatch paths")


def test_context_defaults_to_no_memory():
    """The dataclass default is the safe direction, not the convenient one."""
    from backend.core.ouroboros.governance.subagent_contracts import (
        SubagentContext, SubagentRequest, SubagentType,
    )
    ctx = SubagentContext(
        parent_op_id="p", parent_ctx=None, subagent_id="s",
        subagent_type=SubagentType.EXPLORE,
        request=SubagentRequest(subagent_type=SubagentType.EXPLORE, goal="g"),
    )
    assert ctx.memory_scope == "none"
    assert ctx.memory_section == ""


def test_scope_surface_names_its_own_reach():
    """The surface must not imply a section reaches subagents that have no
    prompt — the wired-but-inert claim this codebase keeps rediscovering."""
    out = "\n".join(render_scope_lines())
    assert "deterministic" in out
    assert "review" in out and "complement" in out
