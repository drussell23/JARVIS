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
# Per-slice SHA lock — updated by EVERY slice that mutates
# providers.py. The constant name does not change across slices; the
# value does. The docstring on
# ``test_ast_pin_providers_file_sha_matches_lock`` tracks the
# provenance of each successive update.
#
# Slice 2A-ii    (PR #48860): a33f9fb67fdcf8e10f8e47fbd3a6b8075aea8a7d
#                             (providers.py byte-identical to main)
# Slice 2A-iii   (PR #48912): d3e409ce032ae3954dbd98d0102682fef968206b
#                             (_boundary_audit_sampler extracted; closure
#                             1036 → 1012 lines, 8 → 7 nested helpers)
# Slice 2B-i     (PR #49578): b2bfe35fe831786e71c56e371f2870fa6b62928d
#                             (_retrieve_stream_exc extracted as
#                             @staticmethod; closure 1012 → 1011 lines,
#                             7 → 6 nested helpers)
# Slice 2B-ii    (PR #49606): 1be2caaddce0809910deb4e9782499da9eea4b2e
#                             (_create_with_prefill_fallback +
#                             _create_with_resilience PAIRED extracted;
#                             closure 1011 → 977 lines (-34),
#                             6 → 4 nested helpers)
# Slice 2B-iii   (PR #49641): 12e0e0f314d8370918858c38d667601c91aa0130
#                             (_stream_with_prefill_fallback +
#                             _stream_with_resilience PAIRED extracted;
#                             closure 977 → 953 lines, 4 → 2 nested)
# Slice 2C-i     (PR #49833): 42ba72372c7024d61c7f545c7c9994eccea774f5
#                             (_do_stream extracted; closure 953 →
#                             712, 2 → 1 nested; SUBSTRATE WENT LIVE)
# Slice 2C-ii    (this PR):   1090feb3734a4e235cc40a947da5026e13dfeca5
#                             (_stream_fanout extracted as
#                             ClaudeProvider._claude_make_stream_fanout
#                             @staticmethod factory; closure 712 → 709
#                             (-3), 1 → 0 nested helpers. _generate_raw
#                             is now structurally clean of nested
#                             defs — the closure-extraction phase of
#                             the arc is COMPLETE. 8 _claude_* class
#                             methods extracted cumulatively.)
_PROVIDERS_SHA_LOCK = (
    "1090feb3734a4e235cc40a947da5026e13dfeca5"
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


def test_ast_pin_providers_imports_claude_dispatch_state_substrate():
    """SUBSTRATE-LIVE INVARIANT — positive-presence pin.

    Slice 2C-i FLIPPED the dormancy pin: ``providers.py`` now imports
    ``_ClaudeDispatchState`` AND ``_ClaudeStreamContext`` from
    ``claude_dispatch_state``. This is the load-bearing structural
    transition the substrate was designed for — the
    ``_claude_do_stream`` class method takes them as keyword-only
    parameters (``state`` + ``ctx``) and the caller in
    ``_generate_raw`` constructs both objects, threads them into
    ``functools.partial(self._claude_do_stream, state=..., ctx=...)``
    via ``_claude_stream_with_resilience``'s ``do_stream_fn``
    parameter, and reads ``state.*`` back into outer locals via
    boundary translation after the stream task completes.

    Provenance history:
      * Slices 2A-iii / 2B-i / 2B-ii / 2B-iii: substrate dormant
        (extracted helpers did not touch substrate-targeted cells).
      * Slice 2C-i (this slice): substrate LIVE — providers.py
        imports + ``_claude_do_stream(state, ctx)`` mutates 6 of
        the 8 substrate fields (raw_content / input_tokens /
        output_tokens / cached_input / first_token_ms / last_msg).

    Future slices that move ``_claude_do_stream`` or restructure
    the substrate import MUST update this pin in the same commit.
    """
    tree = _load_module_ast(_PROVIDERS_FILE)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "claude_dispatch_state" in module:
                for alias in node.names:
                    imported_names.add(alias.name)
    assert "_ClaudeDispatchState" in imported_names, (
        f"providers.py must import _ClaudeDispatchState from "
        f"claude_dispatch_state. Found imports: "
        f"{sorted(imported_names)}"
    )
    assert "_ClaudeStreamContext" in imported_names, (
        f"providers.py must import _ClaudeStreamContext from "
        f"claude_dispatch_state. Found imports: "
        f"{sorted(imported_names)}"
    )


def test_ast_pin_providers_file_sha_matches_lock():
    """Per-slice SHA lock: providers.py SHA1 equals the constant
    above. EVERY slice that mutates providers.py updates the
    constant (see provenance comments at top of file).

    Slice 2A-iii update: ``_boundary_audit_sampler`` extracted to
    ``ClaudeProvider._claude_boundary_audit_sampler`` — the nested
    closure (~38 lines) is gone from ``_generate_raw``; the class
    method (~75 lines including docstring) is the new home.
    Closure shrunk 1036 → 1012 lines."""
    actual_sha = hashlib.sha1(_PROVIDERS_FILE.read_bytes()).hexdigest()
    assert actual_sha == _PROVIDERS_SHA_LOCK, (
        f"providers.py SHA shifted: {actual_sha}. Update "
        f"_PROVIDERS_SHA_LOCK in this file to the new value AND "
        f"document the provenance in the comment above it."
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


def test_ast_pin_boundary_audit_sampler_is_class_method_on_claude_provider():
    """Slice 2A-iii extraction proof — positive presence pin.

    ``_claude_boundary_audit_sampler`` MUST exist as an
    ``async def`` method directly under ``class ClaudeProvider``.
    Future slices that move this method (e.g. into a stream-helper
    mixin class) MUST update this pin in the same commit."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_claude_boundary_audit_sampler"
                ):
                    return
            pytest.fail(
                "ClaudeProvider._claude_boundary_audit_sampler not "
                "found — Slice 2A-iii extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_boundary_audit_sampler_no_longer_nested_in_generate_raw():
    """Slice 2A-iii extraction proof — negative absence pin.

    The original nested ``async def _boundary_audit_sampler`` MUST
    NOT exist anywhere inside ``_generate_raw``. The closure should
    only call ``self._claude_boundary_audit_sampler(...)``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    # Find _generate_raw inside generate.
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            # Walk _generate_raw for any nested def
                            # named _boundary_audit_sampler.
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_boundary_audit_sampler"
                                ):
                                    pytest.fail(
                                        f"_boundary_audit_sampler is "
                                        f"still nested in "
                                        f"_generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2A-iii extraction "
                                        f"incomplete"
                                    )
                            return  # _generate_raw walked clean
    # Reaching here means the structure changed beyond expectation;
    # later slices will need to update this pin.


def test_ast_pin_generate_raw_size_after_2c_ii():
    """Per-slice closure-size envelope. Tightened on each slice that
    actually shrinks ``_generate_raw``.

    Slice 2A-iii (PR #48912) opened at [950, 1025] for size 1012.
    Slice 2B-i   (PR #49578) tightened to [1000, 1015] for size 1011.
    Slice 2B-ii  (PR #49606) tightened to [965, 990] for size 977.
    Slice 2B-iii (PR #49641) tightened to [940, 965] for size 953.
    Slice 2C-i   (PR #49833) tightened to [690, 740] for size 712.
    Slice 2C-ii (this PR) extracts the last nested helper
    ``_stream_fanout`` to the ``_claude_make_stream_fanout``
    @staticmethod factory. Net closure delta is small (712 → 709)
    because the factory invocation replaces the 9-line inline def.
    The structural value is that ``_generate_raw`` now has ZERO
    nested helpers. Envelope tightens to [695, 720]."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            size = (
                                sub.end_lineno - sub.lineno + 1
                            )
                            assert 695 <= size <= 720, (
                                f"_generate_raw size after Slice "
                                f"2C-ii is {size}; expected window "
                                f"[695, 720]. If this slice "
                                f"intentionally moved more, update "
                                f"the envelope."
                            )
                            return


def test_ast_pin_generate_raw_nested_helper_count_after_2c_ii():
    """Per-slice nested-helper-count envelope. Slice 2C-ii drops
    the count from 1 → 0 (``_stream_fanout`` extracted to
    ``_claude_make_stream_fanout``).

    ``_generate_raw`` is now STRUCTURALLY CLEAN of nested ``def`` /
    ``async def`` helpers. The closure-extraction phase of the arc
    is COMPLETE. Any future slice that re-introduces a nested
    helper inside ``_generate_raw`` MUST update this pin
    deliberately."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            nested = [
                                n for n in ast.walk(sub)
                                if isinstance(
                                    n,
                                    (
                                        ast.FunctionDef,
                                        ast.AsyncFunctionDef,
                                    ),
                                )
                                and n is not sub
                            ]
                            count = len(nested)
                            assert count == 0, (
                                f"_generate_raw has {count} nested "
                                f"helpers after Slice 2C-ii; "
                                f"expected exactly 0. The closure "
                                f"is structurally clean post-arc. "
                                f"Got: {sorted(n.name for n in nested)}"
                            )
                            return


def test_ast_pin_make_stream_fanout_is_static_method_on_claude_provider():
    """Slice 2C-ii extraction proof — positive presence pin.

    ``_claude_make_stream_fanout`` MUST exist as a ``@staticmethod``
    directly under ``class ClaudeProvider``. The @staticmethod
    decorator is load-bearing: the factory has zero ``self`` usage
    and returns a closure that captures only its two parameters."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.FunctionDef)
                    and child.name == "_claude_make_stream_fanout"
                ):
                    deco_names = []
                    for deco in child.decorator_list:
                        if isinstance(deco, ast.Name):
                            deco_names.append(deco.id)
                        elif isinstance(deco, ast.Attribute):
                            deco_names.append(deco.attr)
                    assert "staticmethod" in deco_names, (
                        f"_claude_make_stream_fanout missing "
                        f"@staticmethod decorator (got: {deco_names})"
                    )
                    return
            pytest.fail(
                "ClaudeProvider._claude_make_stream_fanout not "
                "found — Slice 2C-ii extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_stream_fanout_no_longer_nested_in_generate_raw():
    """Slice 2C-ii extraction proof — negative absence pin.

    The original nested ``def _stream_fanout`` MUST NOT exist
    anywhere inside ``_generate_raw``. With this pin enforced,
    ``_generate_raw`` is now structurally clean of nested defs
    (count == 0, verified by the sibling pin
    ``test_ast_pin_generate_raw_nested_helper_count_after_2c_ii``)."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name == "_stream_fanout"
                                ):
                                    pytest.fail(
                                        f"_stream_fanout is still "
                                        f"nested in _generate_raw "
                                        f"at line {deeper.lineno} "
                                        f"— Slice 2C-ii extraction "
                                        f"incomplete"
                                    )
                            return


def test_ast_pin_claude_do_stream_is_async_method_on_claude_provider():
    """Slice 2C-i extraction proof — positive presence pin.

    ``_claude_do_stream`` MUST exist as an ``async def`` method
    directly under ``class ClaudeProvider``. Signature MUST be
    ``(self, *, state, ctx)`` only — no additional positional or
    keyword parameters. The two-parameter signature is the
    Option A-prime structural invariant: state is the mutable
    output carrier, ctx is the frozen read-only call context."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_claude_do_stream"
                ):
                    args = child.args
                    # Expect exactly: self + kwonly(state, ctx)
                    pos_args = [a.arg for a in args.args]
                    kwonly_args = [a.arg for a in args.kwonlyargs]
                    assert pos_args == ["self"], (
                        f"_claude_do_stream positional args: "
                        f"{pos_args}; expected ['self']"
                    )
                    assert sorted(kwonly_args) == ["ctx", "state"], (
                        f"_claude_do_stream kw-only args: "
                        f"{sorted(kwonly_args)}; expected "
                        f"['ctx', 'state']"
                    )
                    return
            pytest.fail(
                "ClaudeProvider._claude_do_stream not found — "
                "Slice 2C-i extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_do_stream_no_longer_nested_in_generate_raw():
    """Slice 2C-i extraction proof — negative absence pin.

    The original nested ``async def _do_stream`` MUST NOT exist
    anywhere inside ``_generate_raw``. The closure should only
    construct ``_stream_state`` + ``_stream_ctx`` and invoke
    ``self._claude_do_stream`` via
    ``functools.partial`` threaded through
    ``_claude_stream_with_resilience``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name == "_do_stream"
                                ):
                                    pytest.fail(
                                        f"_do_stream is still "
                                        f"nested in _generate_raw "
                                        f"at line {deeper.lineno} "
                                        f"— Slice 2C-i extraction "
                                        f"incomplete"
                                    )
                            return


# ============================================================================
# Slice 2C-i — _ClaudeStreamContext frozen dataclass spine
# ============================================================================


class TestClaudeStreamContextShape:
    """Closed-field-count + frozen-by-design + to_debug_dict
    contract for the new ``_ClaudeStreamContext`` substrate
    dataclass that landed live in Slice 2C-i."""

    def test_schema_version_is_v1(self) -> None:
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            CLAUDE_STREAM_CONTEXT_SCHEMA_VERSION,
        )
        assert CLAUDE_STREAM_CONTEXT_SCHEMA_VERSION == (
            "claude_stream_context.v1"
        )

    def test_field_names_tuple_has_exactly_twelve_entries(self) -> None:
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _CLAUDE_STREAM_CONTEXT_FIELD_NAMES,
        )
        assert isinstance(_CLAUDE_STREAM_CONTEXT_FIELD_NAMES, tuple)
        assert len(_CLAUDE_STREAM_CONTEXT_FIELD_NAMES) == 12

    def test_field_names_exact_set(self) -> None:
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _CLAUDE_STREAM_CONTEXT_FIELD_NAMES,
        )
        assert set(_CLAUDE_STREAM_CONTEXT_FIELD_NAMES) == {
            "context", "deadline", "timeout_s",
            "effective_max_tokens", "temperature", "thinking_param",
            "system_with_cache", "messages", "is_tool_round",
            "prompt_chars", "call_start", "stream_callback",
        }

    def test_dataclass_field_count_matches_declaration(self) -> None:
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _ClaudeStreamContext,
        )
        runtime_fields = dataclasses.fields(_ClaudeStreamContext)
        assert len(runtime_fields) == 12

    def test_dataclass_is_frozen(self) -> None:
        """Operator-mandated: ``_ClaudeStreamContext`` MUST be
        ``@dataclass(frozen=True)`` so attribute re-binding raises
        ``FrozenInstanceError``. Contained-collection mutability
        (the messages list, system_with_cache blocks) is NOT
        frozen by this — by design, since the prefill-fallback
        wrapper mutates ``messages.pop()`` from outside."""
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _ClaudeStreamContext,
        )
        # Runtime check: attribute assignment after construction
        # raises FrozenInstanceError.
        from datetime import datetime, timezone
        instance = _ClaudeStreamContext(
            context=object(),
            deadline=datetime.now(tz=timezone.utc),
            timeout_s=60.0,
            effective_max_tokens=1024,
            temperature=0.0,
            thinking_param=None,
            system_with_cache=[],
            messages=[],
            is_tool_round=False,
            prompt_chars=0,
            call_start=0.0,
            stream_callback=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.timeout_s = 999.0  # type: ignore[misc]

    def test_dataclass_has_no_from_dict_method(self) -> None:
        """Operator-mandated: NO ``from_dict()`` for
        ``_ClaudeStreamContext``. The dataclass contains callables
        (``stream_callback``) and opaque objects (``context``,
        ``thinking_param``) that cannot be faithfully reconstructed
        from a dict. We do not pretend otherwise. Snapshot via
        ``to_debug_dict`` only."""
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _ClaudeStreamContext,
        )
        assert not hasattr(_ClaudeStreamContext, "from_dict"), (
            "_ClaudeStreamContext must NOT have from_dict — "
            "callables / opaque objects cannot be faithfully "
            "reconstructed from a dict (operator-mandated)."
        )

    def test_to_debug_dict_returns_documented_keys(self) -> None:
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _ClaudeStreamContext,
        )
        from datetime import datetime, timezone
        ctx = _ClaudeStreamContext(
            context=object(),
            deadline=datetime.now(tz=timezone.utc),
            timeout_s=60.0,
            effective_max_tokens=1024,
            temperature=0.2,
            thinking_param=None,
            system_with_cache=[],
            messages=[{"role": "user", "content": "hi"}],
            is_tool_round=False,
            prompt_chars=42,
            call_start=12.3,
            stream_callback=lambda t: None,
        )
        d = ctx.to_debug_dict()
        for key in (
            "schema_version", "context_repr", "deadline_iso",
            "timeout_s", "effective_max_tokens", "temperature",
            "thinking_param", "system_with_cache_repr",
            "messages_len", "is_tool_round", "prompt_chars",
            "call_start", "stream_callback_repr",
        ):
            assert key in d, f"missing key: {key}"
        # Callable repr-stringified, not faithful object.
        assert isinstance(d["stream_callback_repr"], str)
        # messages_len, not the messages body (privacy / log-bloat).
        assert d["messages_len"] == 1

    def test_required_fields_no_defaults(self) -> None:
        """Operator-mandated: NO defaults on any context field.
        Construction without all 12 fields MUST raise
        ``TypeError``."""
        from backend.core.ouroboros.governance.claude_dispatch_state import (
            _ClaudeStreamContext,
        )
        with pytest.raises(TypeError):
            _ClaudeStreamContext()  # type: ignore[call-arg]


def test_ast_pin_stream_with_prefill_fallback_is_async_method_on_claude_provider():
    """Slice 2B-iii extraction proof — positive presence pin (1/2).

    ``_claude_stream_with_prefill_fallback`` MUST exist as an
    ``async def`` method directly under ``class ClaudeProvider``.
    Accepts ``do_stream_fn`` as a caller-supplied 0-arg async
    callable; the closure's ``_do_stream`` is passed in (still
    nested in ``_generate_raw`` until Slice 2C-i)."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name
                    == "_claude_stream_with_prefill_fallback"
                ):
                    return
            pytest.fail(
                "ClaudeProvider._claude_stream_with_prefill_fallback "
                "not found as async method — Slice 2B-iii "
                "extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_stream_with_resilience_is_async_method_on_claude_provider():
    """Slice 2B-iii extraction proof — positive presence pin (2/2).

    ``_claude_stream_with_resilience`` MUST exist as an
    ``async def`` method directly under ``class ClaudeProvider``.
    Accepts a ``progress_probe: Callable[[], bool]`` parameter
    that the caller constructs as a lambda over the closure's
    ``raw_content`` nonlocal — the extracted method itself does
    not touch any substrate-targeted cell."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_claude_stream_with_resilience"
                ):
                    return
            pytest.fail(
                "ClaudeProvider._claude_stream_with_resilience "
                "not found as async method — Slice 2B-iii "
                "extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_stream_with_prefill_fallback_no_longer_nested_in_generate_raw():
    """Slice 2B-iii extraction proof — negative absence pin (1/2).

    The original nested ``async def _stream_with_prefill_fallback``
    MUST NOT exist anywhere inside ``_generate_raw``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_stream_with_prefill_fallback"
                                ):
                                    pytest.fail(
                                        f"_stream_with_prefill_"
                                        f"fallback is still nested "
                                        f"in _generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2B-iii extraction "
                                        f"incomplete"
                                    )
                            return


def test_ast_pin_stream_with_resilience_no_longer_nested_in_generate_raw():
    """Slice 2B-iii extraction proof — negative absence pin (2/2).

    The original nested ``async def _stream_with_resilience``
    MUST NOT exist anywhere inside ``_generate_raw``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_stream_with_resilience"
                                ):
                                    pytest.fail(
                                        f"_stream_with_resilience "
                                        f"is still nested in "
                                        f"_generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2B-iii extraction "
                                        f"incomplete"
                                    )
                            return


def test_ast_pin_create_with_prefill_fallback_is_async_method_on_claude_provider():
    """Slice 2B-ii extraction proof — positive presence pin (1/2).

    ``_claude_create_with_prefill_fallback`` MUST exist as an
    ``async def`` method directly under ``class ClaudeProvider``.
    Distinct from ``@staticmethod`` (which 2B-i used for the
    stateless retrieval callback) because this method composes
    ``self._ensure_client()`` and may compose other ``self``
    surfaces in future hardening (e.g. cache reasoning, route
    stamping). Future slices that move or restructure this method
    MUST update this pin in the same commit."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name
                    == "_claude_create_with_prefill_fallback"
                ):
                    return
            pytest.fail(
                "ClaudeProvider._claude_create_with_prefill_fallback "
                "not found as async method — Slice 2B-ii "
                "extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_create_with_resilience_is_async_method_on_claude_provider():
    """Slice 2B-ii extraction proof — positive presence pin (2/2).

    ``_claude_create_with_resilience`` MUST exist as an
    ``async def`` method directly under ``class ClaudeProvider``.
    Composes ``self._call_with_backoff`` (the canonical backoff
    surface) around the prefill-fallback method."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_claude_create_with_resilience"
                ):
                    return
            pytest.fail(
                "ClaudeProvider._claude_create_with_resilience "
                "not found as async method — Slice 2B-ii "
                "extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_create_with_prefill_fallback_no_longer_nested_in_generate_raw():
    """Slice 2B-ii extraction proof — negative absence pin (1/2).

    The original nested ``async def _create_with_prefill_fallback``
    MUST NOT exist anywhere inside ``_generate_raw``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_create_with_prefill_fallback"
                                ):
                                    pytest.fail(
                                        f"_create_with_prefill_"
                                        f"fallback is still nested "
                                        f"in _generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2B-ii extraction "
                                        f"incomplete"
                                    )
                            return


def test_ast_pin_create_with_resilience_no_longer_nested_in_generate_raw():
    """Slice 2B-ii extraction proof — negative absence pin (2/2).

    The original nested ``async def _create_with_resilience``
    MUST NOT exist anywhere inside ``_generate_raw``."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_create_with_resilience"
                                ):
                                    pytest.fail(
                                        f"_create_with_resilience "
                                        f"is still nested in "
                                        f"_generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2B-ii extraction "
                                        f"incomplete"
                                    )
                            return


def test_ast_pin_retrieve_stream_exc_is_static_method_on_claude_provider():
    """Slice 2B-i extraction proof — positive presence pin.

    ``_claude_retrieve_stream_exc`` MUST exist as a ``@staticmethod``
    directly under ``class ClaudeProvider``. The ``@staticmethod``
    decorator is load-bearing: the helper has zero ``self`` usage and
    is registered as a done-callback (which the asyncio runtime calls
    with one positional argument). Future slices that change this
    decorator MUST update this pin in the same commit."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.FunctionDef)
                    and child.name == "_claude_retrieve_stream_exc"
                ):
                    # Confirm @staticmethod decorator present.
                    deco_names = []
                    for deco in child.decorator_list:
                        if isinstance(deco, ast.Name):
                            deco_names.append(deco.id)
                        elif isinstance(deco, ast.Attribute):
                            deco_names.append(deco.attr)
                    assert "staticmethod" in deco_names, (
                        f"_claude_retrieve_stream_exc missing "
                        f"@staticmethod decorator (got: {deco_names})"
                    )
                    return
            pytest.fail(
                "ClaudeProvider._claude_retrieve_stream_exc not "
                "found — Slice 2B-i extraction missing"
            )
    pytest.fail("ClaudeProvider class not found in providers.py")


def test_ast_pin_retrieve_stream_exc_no_longer_nested_in_generate_raw():
    """Slice 2B-i extraction proof — negative absence pin.

    The original nested ``def _retrieve_stream_exc`` MUST NOT exist
    anywhere inside ``_generate_raw``. The closure should only call
    ``self._claude_retrieve_stream_exc`` (passed to
    ``add_done_callback``)."""
    tree = _load_module_ast(_PROVIDERS_FILE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "ClaudeProvider"
        ):
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "generate"
                ):
                    for sub in ast.walk(child):
                        if (
                            isinstance(sub, ast.AsyncFunctionDef)
                            and sub.name == "_generate_raw"
                        ):
                            for deeper in ast.walk(sub):
                                if deeper is sub:
                                    continue
                                if (
                                    isinstance(
                                        deeper,
                                        (
                                            ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                        ),
                                    )
                                    and deeper.name
                                    == "_retrieve_stream_exc"
                                ):
                                    pytest.fail(
                                        f"_retrieve_stream_exc is "
                                        f"still nested in "
                                        f"_generate_raw at line "
                                        f"{deeper.lineno} — Slice "
                                        f"2B-i extraction "
                                        f"incomplete"
                                    )
                            return


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
