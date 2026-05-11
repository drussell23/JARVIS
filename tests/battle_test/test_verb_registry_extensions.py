"""Regression spine for §41.3 Slice 2/3 verb-registry extensions.

Builds on the existing `repl_completion.VerbDescriptor` substrate
(prior art) rather than replacing it. Tests cover the new fields
(aliases / examples / arg_spec / category) + the 3 new substrate
helpers (suggest_for_typo / fuzzy_match / format_verb_help) + the
docstring @-tag parser.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc
from backend.core.ouroboros.battle_test.repl_completion import (
    VerbCategory,
    VerbDescriptor,
    VerbRegistry,
    _infer_arg_spec_from_signature,
    _levenshtein,
    _parse_doc_tags,
    discover_verbs,
    format_verb_help,
    fuzzy_match,
    suggest_for_typo,
)


# --- VerbCategory closed taxonomy -------------------------------------------


def test_verb_category_closed():
    assert {v.value for v in VerbCategory} == {
        "lifecycle", "introspection", "navigation", "operational",
    }


# --- VerbDescriptor extensions ----------------------------------------------


def test_verb_descriptor_extensions_default_empty():
    v = VerbDescriptor(
        slash_form="/test",
        handler_method="_handle_test",
        description="x",
    )
    assert v.aliases == ()
    assert v.examples == ()
    assert v.arg_spec == ""
    assert v.category is VerbCategory.OPERATIONAL


def test_verb_descriptor_with_extensions():
    v = VerbDescriptor(
        slash_form="/cancel",
        handler_method="_handle_cancel",
        description="cancel an op",
        aliases=("/stop",),
        examples=("/cancel op-abc", "/cancel op-abc --immediate"),
        arg_spec="<op_id> [--immediate]",
        category=VerbCategory.LIFECYCLE,
    )
    assert v.aliases == ("/stop",)
    assert len(v.examples) == 2
    assert v.arg_spec == "<op_id> [--immediate]"
    assert v.category is VerbCategory.LIFECYCLE


def test_verb_descriptor_matches_alias():
    v = VerbDescriptor(
        slash_form="/quit",
        handler_method="",
        description="quit",
        aliases=("/exit", "/bye"),
    )
    assert v.matches("/quit") is True
    assert v.matches("/exit") is True
    assert v.matches("/bye") is True
    assert v.matches("/unknown") is False
    assert v.matches(None) is False  # type: ignore[arg-type]
    assert v.matches(42) is False  # type: ignore[arg-type]


def test_verb_descriptor_to_dict_serializes_extensions():
    v = VerbDescriptor(
        slash_form="/cancel",
        handler_method="_handle_cancel",
        description="cancel",
        aliases=("/stop",),
        examples=("/cancel x",),
        arg_spec="<op_id>",
        category=VerbCategory.LIFECYCLE,
    )
    d = v.to_dict()
    assert d["aliases"] == ["/stop"]
    assert d["examples"] == ["/cancel x"]
    assert d["arg_spec"] == "<op_id>"
    assert d["category"] == "lifecycle"


# --- VerbRegistry extensions -----------------------------------------------


def _registry(*verbs: VerbDescriptor) -> VerbRegistry:
    return VerbRegistry(verbs=verbs)


def test_find_by_alias():
    v = VerbDescriptor(
        slash_form="/quit",
        handler_method="",
        description="quit",
        aliases=("/exit",),
    )
    reg = _registry(v)
    assert reg.find("/quit") is v
    assert reg.find("/exit") is v
    assert reg.find("/unknown") is None
    assert reg.find(None) is None  # type: ignore[arg-type]


def test_by_category_filters():
    a = VerbDescriptor(
        slash_form="/a", handler_method="_handle_a",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    b = VerbDescriptor(
        slash_form="/b", handler_method="_handle_b",
        description="x", category=VerbCategory.INTROSPECTION,
    )
    c = VerbDescriptor(
        slash_form="/c", handler_method="_handle_c",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    reg = _registry(a, b, c)
    lifecycle = reg.by_category(VerbCategory.LIFECYCLE)
    assert {v.slash_form for v in lifecycle} == {"/a", "/c"}


def test_by_category_accepts_string():
    a = VerbDescriptor(
        slash_form="/a", handler_method="",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    reg = _registry(a)
    assert reg.by_category("lifecycle")[0].slash_form == "/a"


def test_by_category_garbage_safe():
    a = VerbDescriptor(
        slash_form="/a", handler_method="",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    reg = _registry(a)
    # Empty result, no exception
    assert reg.by_category(42) == () or reg.by_category(42) == ()  # type: ignore[arg-type]


def test_categories_returns_sorted_unique():
    a = VerbDescriptor(
        slash_form="/a", handler_method="",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    b = VerbDescriptor(
        slash_form="/b", handler_method="",
        description="x", category=VerbCategory.OPERATIONAL,
    )
    c = VerbDescriptor(
        slash_form="/c", handler_method="",
        description="x", category=VerbCategory.LIFECYCLE,
    )
    reg = _registry(a, b, c)
    assert reg.categories() == ("lifecycle", "operational")


# --- _parse_doc_tags -------------------------------------------------------


def test_parse_doc_tags_extracts_examples():
    def fake_handler():
        """Cancel an op.

        @example: /cancel op-abc
        @example: /cancel op-abc --immediate
        """
    tags = _parse_doc_tags(fake_handler)
    assert tags["examples"] == [
        "/cancel op-abc",
        "/cancel op-abc --immediate",
    ]


def test_parse_doc_tags_extracts_arg_spec():
    def fake():
        """Do thing.

        @arg_spec: <op_id> [--immediate]
        """
    tags = _parse_doc_tags(fake)
    assert tags["arg_spec"] == "<op_id> [--immediate]"


def test_parse_doc_tags_extracts_category():
    def fake():
        """Quit.

        @category: lifecycle
        """
    tags = _parse_doc_tags(fake)
    assert tags["category"] is VerbCategory.LIFECYCLE


def test_parse_doc_tags_unknown_category_defaults_operational():
    def fake():
        """X.

        @category: bogus_unknown
        """
    tags = _parse_doc_tags(fake)
    assert tags["category"] is VerbCategory.OPERATIONAL


def test_parse_doc_tags_aliases():
    def fake():
        """Quit.

        @alias: /exit
        @alias: bye
        """
    tags = _parse_doc_tags(fake)
    # `bye` should be prefixed with /
    assert tags["aliases"] == ["/exit", "/bye"]


def test_parse_doc_tags_no_docstring():
    def fake():
        pass
    tags = _parse_doc_tags(fake)
    assert tags["aliases"] == []
    assert tags["examples"] == []
    assert tags["arg_spec"] == ""
    assert tags["category"] is VerbCategory.OPERATIONAL


def test_parse_doc_tags_none_safe():
    tags = _parse_doc_tags(None)
    assert tags["category"] is VerbCategory.OPERATIONAL


# --- _infer_arg_spec_from_signature ----------------------------------------


def test_infer_arg_spec_required_only():
    def fake(self, op_id):
        pass
    spec = _infer_arg_spec_from_signature(fake)
    assert spec == "<op_id>"


def test_infer_arg_spec_with_optional():
    def fake(self, op_id, immediate=False):
        pass
    spec = _infer_arg_spec_from_signature(fake)
    assert "<op_id>" in spec
    assert "[immediate]" in spec


def test_infer_arg_spec_skips_args_kwargs():
    def fake(self, x, *args, **kwargs):
        pass
    spec = _infer_arg_spec_from_signature(fake)
    assert "<x>" in spec
    assert "args" not in spec
    assert "kwargs" not in spec


def test_infer_arg_spec_no_params():
    def fake(self):
        pass
    spec = _infer_arg_spec_from_signature(fake)
    assert spec == ""


def test_infer_arg_spec_invalid_callable_safe():
    spec = _infer_arg_spec_from_signature("not a function")
    assert spec == ""


# --- _levenshtein ----------------------------------------------------------


def test_levenshtein_equal():
    assert _levenshtein("abc", "abc") == 0


def test_levenshtein_empty_a():
    assert _levenshtein("", "abc") == 3


def test_levenshtein_empty_b():
    assert _levenshtein("abc", "") == 3


def test_levenshtein_one_swap():
    # /cancl -> /cancel = 1 insert
    assert _levenshtein("/cancl", "/cancel") == 1


def test_levenshtein_transposition():
    # /caencl -> /cancel = 2 (one substitution + one insertion)
    # Levenshtein counts transposition as 2 ops
    assert _levenshtein("/caencl", "/cancel") <= 2


def test_levenshtein_cap_early_exit():
    # When distance exceeds cap, returns cap+1
    assert _levenshtein("xxxxx", "yyyyy", cap=2) == 3


# --- suggest_for_typo ------------------------------------------------------


@pytest.fixture
def reg_sample():
    return _registry(
        VerbDescriptor(
            slash_form="/cancel", handler_method="_handle_cancel",
            description="cancel",
        ),
        VerbDescriptor(
            slash_form="/budget", handler_method="_handle_budget",
            description="budget",
        ),
        VerbDescriptor(
            slash_form="/quit", handler_method="",
            description="quit", aliases=("/exit",),
        ),
        VerbDescriptor(
            slash_form="/expand", handler_method="_handle_expand",
            description="expand",
        ),
    )


def test_suggest_typo_cancel(reg_sample):
    out = suggest_for_typo("/cancl", reg_sample)
    assert "/cancel" in out


def test_suggest_typo_budget(reg_sample):
    out = suggest_for_typo("/budgt", reg_sample)
    assert "/budget" in out


def test_suggest_typo_returns_empty_for_unrelated(reg_sample):
    out = suggest_for_typo("/zzzzz", reg_sample)
    assert out == ()


def test_suggest_typo_no_slash_prefix(reg_sample):
    # Non-slash input doesn't trigger suggestions
    out = suggest_for_typo("cancl", reg_sample)
    assert out == ()


def test_suggest_typo_empty_input(reg_sample):
    assert suggest_for_typo("", reg_sample) == ()
    assert suggest_for_typo(None, reg_sample) == ()  # type: ignore[arg-type]


def test_suggest_typo_max_results_cap(reg_sample):
    out = suggest_for_typo("/c", reg_sample, max_results=1)
    assert len(out) <= 1


def test_suggest_typo_matches_aliases(reg_sample):
    # /exit -> /exit (exact alias match)
    out = suggest_for_typo("/exi", reg_sample)
    assert "/exit" in out


def test_suggest_typo_garbage_input_safe(reg_sample):
    # Non-string input
    assert suggest_for_typo(42, reg_sample) == ()  # type: ignore[arg-type]


# --- fuzzy_match -----------------------------------------------------------


def test_fuzzy_match_prefix_first(reg_sample):
    out = fuzzy_match("/can", reg_sample)
    assert out[0].slash_form == "/cancel"


def test_fuzzy_match_prefix_excludes_others(reg_sample):
    # When prefix matches, ONLY prefix hits — don't surface
    # distant fuzzy candidates.
    out = fuzzy_match("/can", reg_sample)
    forms = {v.slash_form for v in out}
    assert "/budget" not in forms


def test_fuzzy_match_typo_fallback(reg_sample):
    out = fuzzy_match("/cancl", reg_sample)
    forms = {v.slash_form for v in out}
    assert "/cancel" in forms


def test_fuzzy_match_too_short_no_fuzzy(reg_sample):
    # "/x" is too short to trigger fuzzy fallback
    out = fuzzy_match("/x", reg_sample)
    assert out == ()


def test_fuzzy_match_empty_input(reg_sample):
    assert fuzzy_match("", reg_sample) == ()


def test_fuzzy_match_no_slash(reg_sample):
    assert fuzzy_match("cancel", reg_sample) == ()


def test_fuzzy_match_garbage_safe(reg_sample):
    assert fuzzy_match(None, reg_sample) == ()  # type: ignore[arg-type]
    assert fuzzy_match(42, reg_sample) == ()  # type: ignore[arg-type]


def test_fuzzy_match_max_results_cap(reg_sample):
    """Prefix-match path applies max_results cap. With sample of
    4 verbs all starting with `/`, cap=2 must truncate to 2."""
    out = fuzzy_match("/", reg_sample, max_results=2)
    assert len(out) == 2


def test_fuzzy_match_matches_aliases(reg_sample):
    # /exi -> /exit via alias prefix
    out = fuzzy_match("/exi", reg_sample)
    forms = {v.slash_form for v in out}
    assert "/quit" in forms  # primary surfaced via alias prefix


# --- format_verb_help ------------------------------------------------------


def test_format_verb_help_full():
    v = VerbDescriptor(
        slash_form="/cancel",
        handler_method="_handle_cancel",
        description="Cancel a pending op.",
        aliases=("/stop",),
        examples=("/cancel op-abc",),
        arg_spec="<op_id> [--immediate]",
        category=VerbCategory.LIFECYCLE,
    )
    out = format_verb_help(v)
    assert "/cancel <op_id> [--immediate]" in out
    assert "Cancel a pending op." in out
    assert "/stop" in out
    assert "/cancel op-abc" in out


def test_format_verb_help_minimal():
    v = VerbDescriptor(
        slash_form="/x",
        handler_method="",
        description="",
    )
    out = format_verb_help(v)
    # Should at minimum show the verb name without crashing
    assert "/x" in out


def test_format_verb_help_no_arg_spec():
    v = VerbDescriptor(
        slash_form="/status",
        handler_method="",
        description="show status",
    )
    out = format_verb_help(v)
    assert "/status" in out
    assert "show status" in out


# --- discover_verbs integration --------------------------------------------


class _FakeREPL:
    def _handle_cancel(self, op_id, immediate=False):
        """Cancel a pending op via cooperative cancellation.

        @arg_spec: <op_id> [--immediate]
        @example: /cancel op-abc123
        @example: /cancel op-abc123 --immediate
        @alias: /stop
        @category: lifecycle
        """

    def _handle_simple(self):
        """A simple action."""

    def _handle_introspect(self):
        """Show status.

        @category: introspection
        """


def test_discover_verbs_populates_extensions():
    reg = discover_verbs(_FakeREPL())
    cancel = reg.find("/cancel")
    assert cancel is not None
    assert cancel.arg_spec == "<op_id> [--immediate]"
    assert "/cancel op-abc123" in cancel.examples
    assert "/stop" in cancel.aliases
    assert cancel.category is VerbCategory.LIFECYCLE


def test_discover_verbs_alias_routes_via_find():
    reg = discover_verbs(_FakeREPL())
    # /stop should find the /cancel descriptor through alias
    assert reg.find("/stop") is not None
    assert reg.find("/stop").slash_form == "/cancel"


def test_discover_verbs_default_category_for_untagged():
    reg = discover_verbs(_FakeREPL())
    simple = reg.find("/simple")
    assert simple is not None
    assert simple.category is VerbCategory.OPERATIONAL


def test_discover_verbs_introspection_category():
    reg = discover_verbs(_FakeREPL())
    intro = reg.find("/introspect")
    assert intro is not None
    assert intro.category is VerbCategory.INTROSPECTION


def test_discover_verbs_arg_spec_inferred_from_signature():
    """When @arg_spec tag is absent, signature inspection fills in."""
    class _R:
        def _handle_with_args(self, name, count=3):
            """Do thing."""
    reg = discover_verbs(_R())
    v = reg.find("/with-args")
    assert v is not None
    assert "<name>" in v.arg_spec
    assert "[count]" in v.arg_spec


def test_discover_verbs_invalid_input_returns_empty_or_builtins():
    # None / non-object input still yields at least the built-ins
    reg = discover_verbs(None)
    assert len(reg) >= 1  # built-ins always present
    # Should always have /help among built-ins
    assert reg.find("/help") is not None


# --- AST pin: VerbCategory taxonomy frozen ---------------------------------


def test_ast_pin_verb_category_taxonomy_frozen():
    """Bytes-pin: VerbCategory enum has exactly 4 values matching
    the expected set."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    tree = ast.parse(src)
    expected = {"lifecycle", "introspection", "navigation", "operational"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "VerbCategory"
        ):
            found = set()
            for sub in node.body:
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, str)
                ):
                    found.add(sub.value.value)
            assert found == expected, (
                f"VerbCategory drift: {found} != {expected}"
            )
            return
    pytest.fail("VerbCategory class not found")


def test_ast_pin_substrate_helpers_exported():
    """Bytes-pin: new substrate helpers reachable from __all__."""
    src = Path(
        "backend/core/ouroboros/battle_test/repl_completion.py"
    ).read_text()
    for name in (
        '"VerbCategory"',
        '"format_verb_help"',
        '"fuzzy_match"',
        '"suggest_for_typo"',
    ):
        assert name in src, f"{name} missing from __all__"
