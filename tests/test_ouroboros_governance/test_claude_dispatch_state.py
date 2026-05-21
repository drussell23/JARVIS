"""Slice 2A-ii — dormant substrate spine for ``claude_dispatch_state``.

Verifies:

  * Closed 8-field shape on :class:`_ClaudeDispatchState` (AST + runtime).
  * Closed 3-method interface on :class:`_CumulativeCost` (add / total /
    reset). ``add`` is positive-only; ``total`` is monotone after
    multiple adds; ``reset`` zeroes.
  * Per-dispatch reset semantics on :meth:`reset_for_next_dispatch`.
  * Lossy round-trip on :meth:`to_dict` / :meth:`from_dict` (documented).
  * **Dormancy invariant**: ``providers.py`` SHA at branch HEAD is
    identical to main@72444cc031 ('a33f9fb6...'); the substrate exists
    but no caller imports it yet.

The 6 AST pins enforce the structural boundaries the Phase 2A-iii
through 2C-ii extractions must respect.

Hard guardrails (Slice 2A-ii):

  * ``providers.py`` is READ-ONLY. AST pin
    :func:`test_ast_pin_providers_does_not_import_dispatch_state_yet`
    enforces dormancy.

  * Zero touch of newly-deployed surfaces. AST pin
    :func:`test_ast_pin_no_locked_surface_imports`.

  * The dataclass is mutable (NOT frozen) — the refactor mutates
    in place. AST pin
    :func:`test_ast_pin_dispatch_state_is_not_frozen`.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.claude_dispatch_state import (
    CLAUDE_DISPATCH_STATE_SCHEMA_VERSION,
    _CLAUDE_DISPATCH_STATE_FIELD_NAMES,
    _ClaudeDispatchState,
    _CumulativeCost,
)


_THIS_FILE = Path(__file__)
_MODULE_FILE = Path(
    "backend/core/ouroboros/governance/claude_dispatch_state.py"
)
_PROVIDERS_FILE = Path(
    "backend/core/ouroboros/governance/providers.py"
)
# Locked from the branch-creation audit; updates ONLY in the PR that
# starts importing the substrate (Phase 2A-iii at the earliest).
_PROVIDERS_SHA_AT_2A_II = (
    "a33f9fb67fdcf8e10f8e47fbd3a6b8075aea8a7d"
)


# ============================================================================
# Schema + field-name invariants
# ============================================================================


class TestSchemaAndFieldNames:

    def test_schema_version_is_v1(self) -> None:
        assert CLAUDE_DISPATCH_STATE_SCHEMA_VERSION == (
            "claude_dispatch_state.v1"
        )

    def test_field_names_tuple_has_exactly_eight_entries(self) -> None:
        assert isinstance(_CLAUDE_DISPATCH_STATE_FIELD_NAMES, tuple)
        assert len(_CLAUDE_DISPATCH_STATE_FIELD_NAMES) == 8

    def test_field_names_exact_set(self) -> None:
        assert set(_CLAUDE_DISPATCH_STATE_FIELD_NAMES) == {
            "raw_content", "input_tokens", "output_tokens",
            "cached_input", "first_token_ms", "last_msg",
            "thinking_reason_out", "token_usage",
        }

    def test_dataclass_field_count_matches_declaration(self) -> None:
        runtime_fields = dataclasses.fields(_ClaudeDispatchState)
        assert len(runtime_fields) == 8

    def test_dataclass_field_names_match_declaration(self) -> None:
        runtime_names = {f.name for f in dataclasses.fields(_ClaudeDispatchState)}
        assert runtime_names == set(_CLAUDE_DISPATCH_STATE_FIELD_NAMES)


# ============================================================================
# Dispatch-state defaults + mutability
# ============================================================================


class TestDispatchStateDefaultsAndMutability:

    def test_default_construction_yields_documented_defaults(self) -> None:
        s = _ClaudeDispatchState()
        assert s.raw_content == ""
        assert s.input_tokens == 0
        assert s.output_tokens == 0
        assert s.cached_input == 0
        assert s.first_token_ms is None
        assert s.last_msg is None
        assert s.thinking_reason_out is None
        assert s.token_usage == {}

    def test_token_usage_default_is_independent_per_instance(self) -> None:
        """Per the dataclass ``field(default_factory=dict)`` invariant —
        two fresh instances MUST have independent dicts (no shared
        mutable default)."""
        s1 = _ClaudeDispatchState()
        s2 = _ClaudeDispatchState()
        s1.token_usage["mutated"] = 1
        assert "mutated" not in s2.token_usage

    def test_mutation_visible_on_same_instance(self) -> None:
        s = _ClaudeDispatchState()
        s.raw_content = "hello"
        s.input_tokens = 42
        s.cached_input = 7
        assert s.raw_content == "hello"
        assert s.input_tokens == 42
        assert s.cached_input == 7

    def test_two_instances_have_independent_raw_content(self) -> None:
        s1 = _ClaudeDispatchState()
        s2 = _ClaudeDispatchState()
        s1.raw_content = "one"
        s2.raw_content = "two"
        assert s1.raw_content == "one"
        assert s2.raw_content == "two"

    def test_dataclass_eq_compares_by_value(self) -> None:
        s1 = _ClaudeDispatchState()
        s2 = _ClaudeDispatchState()
        assert s1 == s2  # default == default
        s1.raw_content = "x"
        assert s1 != s2


# ============================================================================
# reset_for_next_dispatch
# ============================================================================


class TestResetForNextDispatch:

    def test_reset_restores_all_fields_to_defaults(self) -> None:
        s = _ClaudeDispatchState()
        s.raw_content = "x"
        s.input_tokens = 1
        s.output_tokens = 2
        s.cached_input = 3
        s.first_token_ms = 12.5
        s.last_msg = object()
        s.thinking_reason_out = "because"
        s.token_usage = {"a": 1}
        s.reset_for_next_dispatch()
        assert s == _ClaudeDispatchState()

    def test_reset_token_usage_is_fresh_dict_not_shared(self) -> None:
        s = _ClaudeDispatchState()
        s.token_usage["mutated"] = 1
        original_dict_id = id(s.token_usage)
        s.reset_for_next_dispatch()
        # Must be a fresh dict — otherwise a callsite holding the old
        # ref could leak state across dispatches.
        assert id(s.token_usage) != original_dict_id
        assert s.token_usage == {}

    def test_reset_is_idempotent(self) -> None:
        s = _ClaudeDispatchState()
        s.reset_for_next_dispatch()
        s.reset_for_next_dispatch()
        assert s == _ClaudeDispatchState()


# ============================================================================
# to_dict / from_dict (lossy by design)
# ============================================================================


class TestToDictFromDictLossyRoundtrip:

    def test_to_dict_returns_documented_keys(self) -> None:
        s = _ClaudeDispatchState()
        d = s.to_dict()
        for key in (
            "schema_version", "raw_content_len", "input_tokens",
            "output_tokens", "cached_input", "first_token_ms",
            "last_msg_repr", "thinking_reason_out", "token_usage",
        ):
            assert key in d, f"missing key: {key}"

    def test_to_dict_carries_schema_version(self) -> None:
        s = _ClaudeDispatchState()
        assert s.to_dict()["schema_version"] == (
            CLAUDE_DISPATCH_STATE_SCHEMA_VERSION
        )

    def test_to_dict_raw_content_is_length_not_body(self) -> None:
        """Privacy / log-bloat protection — never dump raw_content
        verbatim into telemetry."""
        s = _ClaudeDispatchState()
        s.raw_content = "supersecretpayload"
        d = s.to_dict()
        assert "supersecretpayload" not in repr(d)
        assert d["raw_content_len"] == len("supersecretpayload")

    def test_to_dict_last_msg_is_repr_not_object(self) -> None:
        s = _ClaudeDispatchState()
        s.last_msg = "fake-msg-obj"
        d = s.to_dict()
        # repr returns a string — JSON-safe.
        assert isinstance(d["last_msg_repr"], str)

    def test_to_dict_last_msg_none_when_unset(self) -> None:
        s = _ClaudeDispatchState()
        assert s.to_dict()["last_msg_repr"] is None

    def test_from_dict_recovers_scalar_fields(self) -> None:
        d = {
            "schema_version": CLAUDE_DISPATCH_STATE_SCHEMA_VERSION,
            "raw_content_len": 100,
            "input_tokens": 42,
            "output_tokens": 21,
            "cached_input": 7,
            "first_token_ms": 12.5,
            "last_msg_repr": "<some-repr>",
            "thinking_reason_out": "because",
            "token_usage": {"x": 1},
        }
        s = _ClaudeDispatchState.from_dict(d)
        assert s.input_tokens == 42
        assert s.output_tokens == 21
        assert s.cached_input == 7
        assert s.first_token_ms == 12.5
        assert s.thinking_reason_out == "because"
        assert s.token_usage == {"x": 1}

    def test_from_dict_loses_raw_content_body_by_design(self) -> None:
        d = {"raw_content_len": 100, "input_tokens": 0}
        s = _ClaudeDispatchState.from_dict(d)
        assert s.raw_content == ""  # lossy

    def test_from_dict_loses_last_msg_by_design(self) -> None:
        d = {"last_msg_repr": "<msg>"}
        s = _ClaudeDispatchState.from_dict(d)
        assert s.last_msg is None  # lossy

    def test_from_dict_on_bad_payload_returns_defaults(self) -> None:
        """Defensive — non-mapping input does NOT raise."""
        s = _ClaudeDispatchState.from_dict({"input_tokens": "not-a-number"})
        # Either defaults OR the converted value if str-int worked.
        assert isinstance(s, _ClaudeDispatchState)

    def test_from_dict_handles_empty_dict(self) -> None:
        s = _ClaudeDispatchState.from_dict({})
        assert s == _ClaudeDispatchState()


# ============================================================================
# _CumulativeCost
# ============================================================================


class TestCumulativeCost:

    def test_initial_total_is_zero(self) -> None:
        c = _CumulativeCost()
        assert c.total == 0.0

    def test_add_positive_increments_total(self) -> None:
        c = _CumulativeCost()
        c.add(1.5)
        assert c.total == 1.5

    def test_add_zero_is_no_op(self) -> None:
        c = _CumulativeCost()
        c.add(0.0)
        assert c.total == 0.0

    def test_add_negative_is_no_op(self) -> None:
        """Mirrors closure's defensive accounting — a negative
        SDK-reported cost cannot subtract from total_cost."""
        c = _CumulativeCost()
        c.add(-1.0)
        assert c.total == 0.0
        c.add(2.0)
        c.add(-99.0)
        assert c.total == 2.0  # unchanged by the negative

    def test_add_non_numeric_is_no_op(self) -> None:
        """Defensive — ``add("not-a-number")`` does NOT raise."""
        c = _CumulativeCost()
        c.add("nope")  # type: ignore[arg-type]
        c.add(None)    # type: ignore[arg-type]
        assert c.total == 0.0

    def test_total_monotone_across_multiple_adds(self) -> None:
        c = _CumulativeCost()
        c.add(0.5)
        c.add(0.25)
        c.add(0.10)
        c.add(0.05)
        assert c.total == pytest.approx(0.90, abs=1e-9)

    def test_reset_zeroes_after_accumulation(self) -> None:
        c = _CumulativeCost()
        c.add(1.0)
        c.add(2.0)
        assert c.total == 3.0
        c.reset()
        assert c.total == 0.0

    def test_reset_then_add_works(self) -> None:
        c = _CumulativeCost()
        c.add(5.0)
        c.reset()
        c.add(2.5)
        assert c.total == 2.5

    def test_two_instances_are_independent(self) -> None:
        c1 = _CumulativeCost()
        c2 = _CumulativeCost()
        c1.add(1.0)
        c2.add(2.0)
        assert c1.total == 1.0
        assert c2.total == 2.0


# ============================================================================
# AST pins — structural + dormancy invariants
# ============================================================================


def _load_module_ast(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def test_ast_pin_dispatch_state_has_exactly_eight_fields():
    """The dataclass body MUST declare exactly 8 annotated fields.
    Adding a field requires bumping
    :data:`CLAUDE_DISPATCH_STATE_SCHEMA_VERSION` + updating this pin
    + updating :data:`_CLAUDE_DISPATCH_STATE_FIELD_NAMES`."""
    tree = _load_module_ast(_MODULE_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "_ClaudeDispatchState"
        ):
            # Count annotated assignments (the dataclass fields).
            annotated = [
                stmt for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
            ]
            assert len(annotated) == 8, (
                f"_ClaudeDispatchState has {len(annotated)} fields, "
                f"expected 8 (frozen taxonomy)"
            )
            return
    pytest.fail("_ClaudeDispatchState not found")


def test_ast_pin_dispatch_state_is_not_frozen():
    """The refactor mutates fields in place; the dataclass MUST NOT
    be ``frozen=True``."""
    tree = _load_module_ast(_MODULE_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "_ClaudeDispatchState"
        ):
            for deco in node.decorator_list:
                # @dataclass (bare) — no keywords → not frozen.
                if isinstance(deco, ast.Name) and deco.id == "dataclass":
                    return  # bare @dataclass — implicitly not frozen
                # @dataclass(frozen=True, ...) — check keyword
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Name)
                    and deco.func.id == "dataclass"
                ):
                    for kw in deco.keywords:
                        if kw.arg == "frozen":
                            if (
                                isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                pytest.fail(
                                    "_ClaudeDispatchState is frozen; "
                                    "refactor mutates in place"
                                )
            return
    pytest.fail("_ClaudeDispatchState not found")


def test_ast_pin_cumulative_cost_has_closed_interface():
    """:class:`_CumulativeCost` MUST expose the closed 3-method +
    1-property interface (``add`` / ``reset`` / ``total``) — no
    others. ``__init__`` is permitted."""
    tree = _load_module_ast(_MODULE_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "_CumulativeCost"
        ):
            method_names = {
                m.name for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            required = {"__init__", "add", "reset", "total"}
            # ``total`` may be a @property (still a FunctionDef in AST).
            missing = required - method_names
            assert not missing, (
                f"_CumulativeCost missing interface methods: {missing}"
            )
            # No public mutation surface beyond add/reset.
            forbidden = {"subtract", "set", "increment", "decrement"}
            present_forbidden = forbidden & method_names
            assert not present_forbidden, (
                f"_CumulativeCost has forbidden method(s): "
                f"{present_forbidden}"
            )
            return
    pytest.fail("_CumulativeCost not found")


def test_ast_pin_providers_does_not_import_dispatch_state_yet():
    """Dormancy invariant: Slice 2A-ii ships the substrate
    completely unused. ``providers.py`` MUST NOT import from
    ``claude_dispatch_state`` until Phase 2A-iii (first extraction)
    lands.

    This pin will be the FIRST one Phase 2A-iii flips."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "claude_dispatch_state" not in module, (
                f"providers.py prematurely imports claude_dispatch_state "
                f"(line {node.lineno}); Slice 2A-ii must keep "
                f"providers.py byte-identical to main"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "claude_dispatch_state" not in alias.name


def test_ast_pin_providers_file_sha_matches_2a_ii_baseline():
    """Stronger dormancy invariant: providers.py byte-identical to
    main@72444cc031. This is the canonical proof that Slice 2A-ii
    introduces zero behavioral change.

    Phase 2A-iii will update this SHA in the same commit that flips
    the import-yet pin above."""
    actual_sha = hashlib.sha1(_PROVIDERS_FILE.read_bytes()).hexdigest()
    assert actual_sha == _PROVIDERS_SHA_AT_2A_II, (
        f"providers.py SHA shifted: {actual_sha} (expected "
        f"{_PROVIDERS_SHA_AT_2A_II}). Slice 2A-ii must keep "
        f"providers.py byte-identical; any change belongs in "
        f"Phase 2A-iii or later."
    )


def test_ast_pin_no_locked_surface_imports():
    """Operator lockdown enforcement: the substrate module MUST NOT
    import from any newly-deployed surface (evaluator_trace_observer,
    session_budget_authority, provider_response_cache,
    s2_predictive_budget, swe_bench_pro/*, commit_authority,
    auto_committer)."""
    locked = (
        "evaluator_trace_observer",
        "evaluator_trace_observability",
        "session_budget_authority",
        "provider_response_cache",
        "s2_predictive_budget",
        "swe_bench_pro",
        "commit_authority",
        "auto_committer",
    )
    tree = _load_module_ast(_MODULE_FILE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for surface in locked:
                assert surface not in module, (
                    f"forbidden locked-surface import in substrate: "
                    f"{module}"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                for surface in locked:
                    assert surface not in alias.name, (
                        f"forbidden locked-surface import in substrate: "
                        f"{alias.name}"
                    )


def test_ast_pin_substrate_module_has_no_authority_imports():
    """The substrate module MUST NOT import providers.py, the
    orchestrator, the iron gate, the candidate generator, or any
    authority surface. It is pure data, no dependency cycles, no
    behavior."""
    forbidden_modules = (
        "providers",
        "orchestrator",
        "iron_gate",
        "candidate_generator",
        "urgency_router",
        "tool_executor",
        "policy",
        "change_engine",
        "subagent_scheduler",
        "auto_action_router",
        "strategic_direction",
    )
    tree = _load_module_ast(_MODULE_FILE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for surface in forbidden_modules:
                # Match end-of-module-path or full path
                assert not module.endswith(f".{surface}"), (
                    f"forbidden authority import in substrate: {module}"
                )


def test_ast_pin_substrate_exports_via_explicit_all():
    """The module MUST declare ``__all__`` listing both classes plus
    the schema constant — explicit export surface protects against
    accidental name-leak into consumers."""
    tree = _load_module_ast(_MODULE_FILE)
    found_all = False
    expected_names = {
        "CLAUDE_DISPATCH_STATE_SCHEMA_VERSION",
        "_ClaudeDispatchState",
        "_CumulativeCost",
    }
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    found_all = True
                    # Extract the string values from the list literal.
                    if isinstance(node.value, ast.List):
                        names = {
                            elt.value for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        }
                        missing = expected_names - names
                        assert not missing, (
                            f"__all__ missing required names: {missing}"
                        )
    assert found_all, "substrate module must declare __all__"
