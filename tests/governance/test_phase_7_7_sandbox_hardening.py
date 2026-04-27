"""Phase 7.7 — Sandbox hardening (Rule 7) introspection-escape pins.

Per OUROBOROS_VENOM_PRD.md §9 P7.7: AST-block `__subclasses__` /
`__bases__` / `__class__` access in any function body. Hard-block at
validation time, BEFORE the candidate ever reaches the sandbox.

Pinned cage:
  * 3 banned attribute names: __subclasses__, __bases__, __class__
  * Pattern 1: direct Attribute access (any depth)
  * Pattern 2: getattr(x, "<banned>") with string literal
  * Per-rule kill switch JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE
    defaults to TRUE (security hardening on by default — unlike most
    JARVIS flags)
  * INTROSPECTION_ESCAPE failure reason added to enum (full set pinned)
  * Rule 7 walks ALL function bodies in the candidate, not just `run`
  * Adversarial test corpus (positive / negative / edge cases)
"""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.meta import (
    ast_phase_runner_validator as v,
)
from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (
    ValidationFailureReason,
    ValidationStatus,
    is_introspection_block_enabled,
    validate_ast,
)


# Test runner shell — passes rules 1-6 so the only remaining check is
# rule 7. Each test injects body content into `BODY` placeholder.
def _runner(body: str) -> str:
    return textwrap.dedent(f"""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
        )
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult,
        )

        class TestPR(PhaseRunner):
            phase = "TEST"
            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
{textwrap.indent(textwrap.dedent(body), '                    ')}
                except Exception:
                    return None
    """).strip()


def _enable_validator(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "1",
    )


# ---------------------------------------------------------------------------
# Section A — module constants + per-rule kill switch
# ---------------------------------------------------------------------------


class TestRule7Constants:
    def test_banned_attrs_set_shape(self):
        assert v._BANNED_INTROSPECTION_ATTRS == frozenset({
            "__subclasses__", "__bases__", "__class__",
        })

    def test_banned_attrs_is_frozen(self):
        with pytest.raises(AttributeError):
            v._BANNED_INTROSPECTION_ATTRS.add("__mro__")  # type: ignore[attr-defined]

    def test_introspection_escape_in_failure_reason_enum(self):
        assert "INTROSPECTION_ESCAPE" in ValidationFailureReason.__members__
        assert (
            ValidationFailureReason.INTROSPECTION_ESCAPE.value
            == "introspection_escape"
        )

    def test_full_failure_reason_set_pinned(self):
        # Pin the full set so adding/removing a reason is intentional.
        # Phase 7.7 added INTROSPECTION_ESCAPE as the 9th reason.
        assert {r.value for r in ValidationFailureReason} == {
            "no_phase_runner_subclass",
            "missing_phase_attr",
            "missing_run_method",
            "run_not_async",
            "run_bad_signature",
            "ctx_mutation",
            "no_top_level_try",
            "banned_import",
            "introspection_escape",
        }


class TestPerRuleKillSwitch:
    def test_default_true(self, monkeypatch):
        # Unlike most JARVIS flags, this defaults to TRUE — security
        # hardening is on by default.
        monkeypatch.delenv(
            "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE",
            raising=False,
        )
        assert is_introspection_block_enabled() is True

    def test_explicit_true_variants(self, monkeypatch):
        for val in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE", val,
            )
            assert is_introspection_block_enabled() is True, val

    def test_explicit_false_variants(self, monkeypatch):
        for val in ("0", "false", "FALSE", "No", "OFF", "", "  "):
            monkeypatch.setenv(
                "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE", val,
            )
            assert is_introspection_block_enabled() is False, val


# ---------------------------------------------------------------------------
# Section B — Pattern 1: direct Attribute access
# ---------------------------------------------------------------------------


class TestPattern1AttributeAccess:
    def test_subclasses_call_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("victims = object.__subclasses__()")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert (
            result.reason == ValidationFailureReason.INTROSPECTION_ESCAPE
        )
        assert "__subclasses__" in result.detail

    def test_bases_access_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("parents = type(self).__bases__")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__bases__" in result.detail

    def test_class_access_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("klass = self.__class__")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__class__" in result.detail

    def test_chained_attribute_access_blocked(self, monkeypatch):
        # obj.x.__class__ — chained Attribute; walker should still find
        # the inner banned attr.
        _enable_validator(monkeypatch)
        source = _runner("v = self.foo.__class__")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__class__" in result.detail

    def test_call_chain_blocked(self, monkeypatch):
        # f().__bases__ — Call → Attribute access on the result.
        _enable_validator(monkeypatch)
        source = _runner("v = type(self)().__bases__")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__bases__" in result.detail

    def test_subscript_then_attribute_blocked(self, monkeypatch):
        # arr[0].__class__ — Subscript → Attribute.
        _enable_validator(monkeypatch)
        source = _runner("arr = [1, 2]\nv = arr[0].__class__")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__class__" in result.detail

    def test_double_attribute_chain_subclasses(self, monkeypatch):
        # The classic CPython-sandbox-escape one-liner:
        # `().__class__.__bases__[0].__subclasses__()`
        _enable_validator(monkeypatch)
        source = _runner(
            "victims = ().__class__.__bases__[0].__subclasses__()"
        )
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        # First-hit wins; could be __class__ or __bases__ or
        # __subclasses__ depending on AST walk order.
        assert any(
            attr in result.detail
            for attr in ("__class__", "__bases__", "__subclasses__")
        )


# ---------------------------------------------------------------------------
# Section C — Pattern 2: getattr(x, "<banned>") string literal
# ---------------------------------------------------------------------------


class TestPattern2GetattrString:
    def test_getattr_subclasses_string_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("v = getattr(object, '__subclasses__')()")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert (
            result.reason
            == ValidationFailureReason.INTROSPECTION_ESCAPE
        )
        assert "getattr_string=__subclasses__" in result.detail

    def test_getattr_bases_string_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("v = getattr(self, '__bases__')")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__bases__" in result.detail

    def test_getattr_class_string_blocked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("v = getattr(self, '__class__')")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__class__" in result.detail

    def test_getattr_dynamic_string_NOT_blocked(self, monkeypatch):
        # getattr(self, dynamic_var) where the second arg is NOT a
        # string literal cannot be statically detected. Rule 7 is a
        # static-shape check — dynamic-string getattr is a known
        # limitation tracked separately. Pin the limitation.
        _enable_validator(monkeypatch)
        source = _runner(
            "name = '__sub' + 'classes__'\n"
            "v = getattr(object, name)"
        )
        result = validate_ast(source)
        # Note: this PASSES Rule 7 because Rule 7 only catches string
        # literals — dynamic attr names slip through. The escape vector
        # in this case requires the candidate to literally encode the
        # full string at parse time. Tracked as a known limitation.
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )

    def test_module_getattr_NOT_blocked(self, monkeypatch):
        # `some_module.getattr(...)` — NOT the builtin getattr; should
        # not trigger Pattern 2. (But the actual attribute access on
        # a result might still trigger Pattern 1.)
        _enable_validator(monkeypatch)
        source = _runner(
            "import operator\n"
            "v = operator.attrgetter('safe_attr')(self)"
        )
        result = validate_ast(source)
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )


# ---------------------------------------------------------------------------
# Section D — Walker scope: ALL function bodies (not just run)
# ---------------------------------------------------------------------------


class TestWalkerScope:
    def test_helper_function_body_also_walked(self, monkeypatch):
        # The escape is in a helper function `_steal()` defined at
        # module level — Rule 7 must still find it.
        _enable_validator(monkeypatch)
        source = textwrap.dedent("""
            from backend.core.ouroboros.governance.phase_runner import (
                PhaseRunner, PhaseResult,
            )
            from backend.core.ouroboros.governance.op_context import (
                OperationContext,
            )

            def _steal():
                return object.__subclasses__()

            class TestPR(PhaseRunner):
                phase = "TEST"
                async def run(self, ctx: OperationContext) -> PhaseResult:
                    try:
                        v = _steal()
                    except Exception:
                        return None
        """).strip()
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert (
            result.reason == ValidationFailureReason.INTROSPECTION_ESCAPE
        )

    def test_nested_function_body_walked(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("""
            def inner():
                return object.__subclasses__()
            v = inner()
        """)
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__subclasses__" in result.detail

    def test_method_body_walked(self, monkeypatch):
        # A second method on the PhaseRunner class.
        _enable_validator(monkeypatch)
        source = textwrap.dedent("""
            from backend.core.ouroboros.governance.phase_runner import (
                PhaseRunner, PhaseResult,
            )
            from backend.core.ouroboros.governance.op_context import (
                OperationContext,
            )

            class TestPR(PhaseRunner):
                phase = "TEST"
                def helper(self):
                    return self.__class__
                async def run(self, ctx: OperationContext) -> PhaseResult:
                    try:
                        v = self.helper()
                    except Exception:
                        return None
        """).strip()
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert "__class__" in result.detail


# ---------------------------------------------------------------------------
# Section E — Negative cases (clean candidates pass Rule 7)
# ---------------------------------------------------------------------------


class TestCleanCandidatesPass:
    def test_attribute_access_to_safe_names_passes(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("""
            v = ctx.metadata
            w = self.foo.bar.baz
        """)
        result = validate_ast(source)
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )

    def test_string_literal_with_banned_substring_passes(
        self, monkeypatch,
    ):
        # A string CONTAINING __class__ (e.g. a doc comment / log msg)
        # is NOT an Attribute access — should pass. Pin the false-
        # positive defense.
        _enable_validator(monkeypatch)
        source = _runner('v = "this mentions __class__ in a string"')
        result = validate_ast(source)
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )

    def test_other_dunder_attrs_pass(self, monkeypatch):
        # __init__ / __repr__ / __len__ etc. — common dunder method
        # access is NOT in the banned set.
        _enable_validator(monkeypatch)
        source = _runner("""
            v = self.__init__
            w = ctx.__repr__()
        """)
        result = validate_ast(source)
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )

    def test_safe_getattr_string_passes(self, monkeypatch):
        _enable_validator(monkeypatch)
        source = _runner("v = getattr(ctx, 'metadata', None)")
        result = validate_ast(source)
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )


# ---------------------------------------------------------------------------
# Section F — Per-rule kill switch behavior
# ---------------------------------------------------------------------------


class TestKillSwitchBehavior:
    def test_per_rule_off_lets_escape_through(self, monkeypatch):
        # Operator-emergency: per-rule kill switch off → Rule 7 skipped
        # but other rules still enforced.
        _enable_validator(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE", "false",
        )
        source = _runner("v = object.__subclasses__()")
        result = validate_ast(source)
        # Rule 7 disabled → the candidate (which violates only rule 7)
        # now PASSES.
        assert result.status == ValidationStatus.PASSED, (
            f"got {result.status}/{result.reason}/{result.detail}"
        )

    def test_per_rule_default_on_blocks(self, monkeypatch):
        # Confirm the inverse: default state (per-rule unset) blocks.
        _enable_validator(monkeypatch)
        monkeypatch.delenv(
            "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE",
            raising=False,
        )
        source = _runner("v = object.__subclasses__()")
        result = validate_ast(source)
        assert result.status == ValidationStatus.FAILED
        assert (
            result.reason == ValidationFailureReason.INTROSPECTION_ESCAPE
        )

    def test_validator_master_off_skips_everything(self, monkeypatch):
        # If the entire validator is off, Rule 7 doesn't fire either —
        # ValidationStatus.SKIPPED.
        monkeypatch.delenv(
            "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", raising=False,
        )
        source = _runner("v = object.__subclasses__()")
        result = validate_ast(source)
        assert result.status == ValidationStatus.SKIPPED


# ---------------------------------------------------------------------------
# Section G — Helper function direct tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_find_introspection_escape_returns_none_for_clean(self):
        import ast
        tree = ast.parse("x = 1\ny = ctx.metadata")
        assert v._find_introspection_escape(tree) is None

    def test_find_introspection_escape_finds_attr(self):
        import ast
        tree = ast.parse("v = obj.__subclasses__()")
        result = v._find_introspection_escape(tree)
        assert result is not None
        assert "__subclasses__" in result

    def test_find_introspection_escape_finds_getattr(self):
        import ast
        tree = ast.parse("v = getattr(obj, '__bases__')")
        result = v._find_introspection_escape(tree)
        assert result is not None
        assert "getattr_string=__bases__" in result

    def test_describe_attribute_target_name(self):
        import ast
        tree = ast.parse("v = obj.__class__")
        attr_node = tree.body[0].value  # type: ignore[attr-defined]
        assert isinstance(attr_node, ast.Attribute)
        shape = v._describe_attribute_target(attr_node)
        assert "Name" in shape and "obj" in shape

    def test_describe_attribute_target_other(self):
        import ast
        tree = ast.parse("v = arr[0].__class__")
        attr_node = tree.body[0].value  # type: ignore[attr-defined]
        assert isinstance(attr_node, ast.Attribute)
        shape = v._describe_attribute_target(attr_node)
        assert shape == "Subscript"

    def test_is_getattr_call_positive(self):
        import ast
        tree = ast.parse("v = getattr(x, 'y')")
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert isinstance(call, ast.Call)
        assert v._is_getattr_call(call)

    def test_is_getattr_call_negative_module(self):
        # `mod.getattr(x, "y")` is NOT the builtin
        import ast
        tree = ast.parse("v = mod.getattr(x, 'y')")
        call = tree.body[0].value  # type: ignore[attr-defined]
        assert isinstance(call, ast.Call)
        assert not v._is_getattr_call(call)

    def test_string_constant_value_string(self):
        import ast
        node = ast.parse("'hello'").body[0].value  # type: ignore[attr-defined]
        assert v._string_constant_value(node) == "hello"

    def test_string_constant_value_int_returns_none(self):
        import ast
        node = ast.parse("42").body[0].value  # type: ignore[attr-defined]
        assert v._string_constant_value(node) is None

    def test_string_constant_value_name_returns_none(self):
        import ast
        node = ast.parse("name").body[0].value  # type: ignore[attr-defined]
        assert v._string_constant_value(node) is None
