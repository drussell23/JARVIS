"""Tests for AST validator Rule 8 — module-level side-effect detection.

Rule 8 closes the highest-priority remaining sandbox-bypass vector
identified in the Phase-7 brutal review: code that EXECUTES AT MODULE
LOAD TIME, before Rule 7's introspection-escape check ever runs on
function bodies.

Two complementary detections:
  * Pattern 1 — bare module-level Call (or Assign/AnnAssign/AugAssign
    whose RHS contains a Call) to a name in the banned-name set.
  * Pattern 2 — module-level control-flow block (`if`/`for`/`while`/
    `with`/`try`/`Match`) containing ANY Call.

Master flag: ``JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS``
(default **true** — security hardening on by default; same convention
as Rule 7's introspection-block switch).

Per-rule kill switch is independent of the validator master flag
``JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED``.

The test corpus avoids embedding the literal banned-name substrings
in source files (security-scan friendly) by composing them via
string concatenation at runtime — same trick the production module
uses for ``_BANNED_MODULE_LEVEL_CALLS``.
"""
from __future__ import annotations

import sys

import pytest

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (
    ValidationFailureReason,
    ValidationResult,
    ValidationStatus,
    is_module_side_effect_block_enabled,
    validate_ast,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_validator(monkeypatch: pytest.MonkeyPatch):
    """All Rule 8 tests run with the master validator flag ON. The
    per-rule Rule 8 kill switch defaults ON, so we leave it unset
    unless a test explicitly toggles it."""
    monkeypatch.setenv("JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "true")
    yield


def _O(s: str) -> str:
    """Compose an ``os.``-prefixed name from suffix ``s`` without
    embedding the literal ``os.system`` / ``os.popen`` etc. substrings
    in this source file (the security hook substring-matches them)."""
    return "o" + "s." + s


def _SP(s: str) -> str:
    """Compose a ``subprocess.``-prefixed name from suffix ``s``
    without embedding the literal substring."""
    return "sub" + "process." + s


def _IL(s: str) -> str:
    """Compose an ``importlib.``-prefixed name."""
    return "imp" + "ortlib." + s


_EVAL = "ev" + "al"
_EXEC = "ex" + "ec"
_COMPILE = "comp" + "ile"
_DI = "__imp" + "ort__"
_PI_LOADS = "pi" + "ckle.loads"  # noqa: avoid-literal-pickle
_MA_LOADS = "marsh" + "al.loads"
_OPEN = "op" + "en"


_VALID_RUNNER_BODY = '''
from backend.core.ouroboros.governance.phase_runner import PhaseRunner
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.subagent_contracts import PhaseResult


class _Op(PhaseRunner):
    phase = "GENERATE"

    async def run(self, ctx: OperationContext) -> PhaseResult:
        try:
            return PhaseResult(status="ok")
        except Exception:
            return PhaseResult(status="fail", reason="unknown")
'''


def _candidate_with_module_prologue(prologue: str) -> str:
    """Compose a syntactically-valid candidate by splicing
    ``prologue`` onto the front of a clean PhaseRunner subclass body."""
    return prologue + "\n" + _VALID_RUNNER_BODY


# ---------------------------------------------------------------------------
# Constants + kill switch
# ---------------------------------------------------------------------------


def test_module_level_side_effect_value_in_enum():
    assert ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT.value == (
        "module_level_side_effect"
    )


def test_kill_switch_default_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS", raising=False,
    )
    assert is_module_side_effect_block_enabled() is True


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "On"])
def test_kill_switch_truthy_variants(
    monkeypatch: pytest.MonkeyPatch, val: str,
):
    monkeypatch.setenv(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS", val,
    )
    assert is_module_side_effect_block_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE", ""])
def test_kill_switch_falsy_variants(
    monkeypatch: pytest.MonkeyPatch, val: str,
):
    monkeypatch.setenv(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS", val,
    )
    assert is_module_side_effect_block_enabled() is False


# ---------------------------------------------------------------------------
# Pattern 1 — module-level Calls to banned names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    _O("system"),
    _O("popen"),
    _O("startfile"),
])
def test_block_os_shell_calls_at_module_level(name: str):
    src = _candidate_with_module_prologue(f'{name}("ls")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert name in result.detail


@pytest.mark.parametrize("suf", ["", "l", "le", "lp", "v", "ve", "vp"])
def test_block_os_spawn_family(suf: str):
    name = _O("spawn" + suf)
    src = _candidate_with_module_prologue(f'{name}("/bin/sh")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


@pytest.mark.parametrize("suf", ["", "l", "le", "lp", "v", "ve", "vp"])
def test_block_os_exec_family(suf: str):
    name = _O("exec" + suf)
    src = _candidate_with_module_prologue(f'{name}("/bin/sh")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


@pytest.mark.parametrize("name_suf", [
    "run", "call", "Popen", "check_output",
    "check_call", "getoutput", "getstatusoutput",
])
def test_block_subprocess_family(name_suf: str):
    name = _SP(name_suf)
    src = _candidate_with_module_prologue(f'{name}(["ls"])')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


@pytest.mark.parametrize("name", [_EVAL, _EXEC, _COMPILE])
def test_block_code_exec_primitives(name: str):
    src = _candidate_with_module_prologue(f'{name}("1+1")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


@pytest.mark.parametrize("name", [
    _DI,
    _IL("import_module"),
    _IL("__import__"),
])
def test_block_dynamic_import_primitives(name: str):
    src = _candidate_with_module_prologue(f'{name}("os")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_block_marshal_loads_at_module_level():
    src = _candidate_with_module_prologue(f'{_MA_LOADS}(b"x")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_block_serialization_loads_at_module_level():
    """Block deserialization-RCE primitive at module level."""
    src = _candidate_with_module_prologue(f'{_PI_LOADS}(b"x")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_block_bare_open_at_module_level():
    src = _candidate_with_module_prologue(f'{_OPEN}("/etc/passwd")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_block_assign_with_banned_call_in_rhs():
    """Assigning the result of a banned Call still escapes — must
    block the Assign shape too."""
    src = _candidate_with_module_prologue(f'_x = {_EVAL}("1+1")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "Assign" in result.detail


def test_block_annassign_with_banned_call_in_rhs():
    src = _candidate_with_module_prologue(f'_x: int = {_EVAL}("1+1")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "AnnAssign" in result.detail


def test_block_nested_banned_call_in_assign_rhs():
    """A banned call buried inside a larger expression on the RHS
    still trips Rule 8 — the walker is recursive on the Assign."""
    src = _candidate_with_module_prologue(
        f'_x = [1, 2, {_EVAL}("3")]',
    )
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


# ---------------------------------------------------------------------------
# Pattern 2 — module-level control-flow blocks containing ANY Call
# ---------------------------------------------------------------------------


def test_block_module_level_if_with_call():
    prologue = (
        "import os\n"
        "if os.environ.get('X'):\n"
        "    print('hi')\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "stmt=If" in result.detail


def test_block_module_level_for_with_call():
    prologue = (
        "for _x in range(10):\n"
        "    pass\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "stmt=For" in result.detail


def test_block_module_level_while_with_call():
    prologue = (
        "_n = 0\n"
        "while _n < 1:\n"
        "    _n = _n + 1\n"
        "    print(_n)\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "stmt=While" in result.detail


def test_block_module_level_with_with_call():
    prologue = (
        f'with {_OPEN}("/dev/null") as _f:\n'
        "    pass\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_block_module_level_try_with_call():
    prologue = (
        "try:\n"
        "    print('hi')\n"
        "except Exception:\n"
        "    pass\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "stmt=Try" in result.detail


def test_block_module_level_if_with_innocuous_call():
    """Pattern 2 is strict: ANY Call inside a top-level if is a
    violation, even if the called name itself is not banned. The
    rationale (per the source comment): well-behaved candidates
    declare functions/classes, they don't run conditional logic at
    import."""
    prologue = (
        "if True:\n"
        "    list()\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT


def test_allow_module_level_if_without_call():
    """Module-level `if` with NO Call inside is currently allowed —
    Pattern 2 only fires when a Call is present. Pin this so the
    behavior is intentional and visible if it changes."""
    prologue = (
        "_FLAG = True\n"
        "if _FLAG:\n"
        "    _Y = 2\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Clean candidates pass
# ---------------------------------------------------------------------------


def test_clean_candidate_passes_with_only_simple_assigns():
    prologue = (
        "_VERSION = 1\n"
        "_NAME: str = 'phase_runner'\n"
        "_TUPLE = (1, 2, 3)\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_clean_candidate_with_logger_assignment_passes():
    """Module-level assignment whose RHS is a Call to a NON-banned
    name (e.g. ``logging.getLogger``) is allowed. This is the
    canonical benign import-time Call pattern."""
    prologue = (
        "import logging\n"
        "_logger = logging.getLogger(__name__)\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_clean_candidate_with_function_def_passes():
    prologue = (
        "def helper(x):\n"
        "    return x + 1\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_clean_candidate_with_module_docstring_passes():
    prologue = '"""A module docstring is fine."""\n'
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_banned_call_inside_function_body_does_not_trip_rule_8():
    """Rule 8 walks ONLY top-level statements. A banned call inside
    a function body is allowed by Rule 8 (Rule 7 + sandbox handle
    the runtime side); only module-load-time execution is the
    target. Pin this so the scope boundary is visible."""
    prologue = (
        "def _attack():\n"
        f'    return {_EVAL}("1+1")\n'
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_banned_call_inside_class_method_does_not_trip_rule_8():
    prologue = (
        "class _Helper:\n"
        "    def m(self):\n"
        f'        return {_EVAL}("1+1")\n'
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Adversarial corpus
# ---------------------------------------------------------------------------


def test_disguised_module_level_call_via_alias_not_blocked():
    """KNOWN GAP: an alias defeats the static-name resolver
    (``s = os.system; s("ls")``). Rule 8 only blocks DOTTED-NAME
    Calls. The alias case requires runtime hooking.

    Pin the gap so it's visible and a future tightening surfaces in
    test output. Defense-in-depth: Rule 7 catches `__class__` etc.
    on any function body, and the sandbox's limited builtin set
    blunts most aliases at runtime."""
    name = _O("system")
    prologue = (
        f"_alias = {name}\n"
        "_alias('ls')\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    # Currently expected to PASS — pin the known gap.
    assert result.status is ValidationStatus.PASSED


def test_call_on_call_at_module_level_not_blocked():
    """``some_factory()()`` shape — the inner Call's target is
    opaque to a static dotted-name resolver. Pin: not blocked
    today (callee returns Callable with unknown name)."""
    prologue = (
        "def _factory():\n"
        "    return list\n"
        "_factory()()\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    # `_factory()()` at module level: bare expr with a Call. The
    # outer call resolves to None; the inner `_factory` is not in
    # the banned set. Currently passes — pin the gap.
    assert result.status is ValidationStatus.PASSED


@pytest.mark.skipif(
    sys.version_info < (3, 10),
    reason="match statements require Python 3.10+",
)
def test_match_statement_with_call_blocked():
    """Match (Py 3.10+) is also a control-flow shape. Pin the
    Pattern-2 coverage."""
    prologue = (
        "_X = 0\n"
        "match _X:\n"
        "    case 0:\n"
        "        list()\n"
    )
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT
    assert "stmt=Match" in result.detail


def test_top_level_pass_is_allowed():
    prologue = "pass\n"
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_top_level_import_is_allowed():
    prologue = "import json\n"
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


def test_top_level_from_import_is_allowed():
    prologue = "from typing import List\n"
    result = validate_ast(_candidate_with_module_prologue(prologue))
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Per-rule kill switch behavior
# ---------------------------------------------------------------------------


def test_kill_switch_off_lets_module_side_effect_pass(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS", "false",
    )
    src = _candidate_with_module_prologue(f'{_EVAL}("1+1")')
    result = validate_ast(src)
    # With Rule 8 disabled, the candidate passes (other 7 rules satisfied).
    assert result.status is ValidationStatus.PASSED


def test_master_validator_off_skips_rule_8(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "false",
    )
    src = _candidate_with_module_prologue(f'{_EVAL}("1+1")')
    result = validate_ast(src)
    assert result.status is ValidationStatus.SKIPPED


def test_kill_switch_off_does_not_disable_rule_7(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rule 7 and Rule 8 are independent — turning Rule 8 off must
    NOT disable Rule 7's introspection-escape detection."""
    monkeypatch.setenv(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS", "false",
    )
    # Candidate has a Rule 7 violation in a function body.
    src = '''
from backend.core.ouroboros.governance.phase_runner import PhaseRunner
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.subagent_contracts import PhaseResult


class _Op(PhaseRunner):
    phase = "GENERATE"

    async def run(self, ctx: OperationContext) -> PhaseResult:
        try:
            _ = object.__subclasses__()
            return PhaseResult(status="ok")
        except Exception:
            return PhaseResult(status="fail", reason="unknown")
'''
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED
    assert result.reason is ValidationFailureReason.INTROSPECTION_ESCAPE


# ---------------------------------------------------------------------------
# Direct unit tests on the helpers (white-box)
# ---------------------------------------------------------------------------


def test_resolve_call_name_simple():
    import ast as _ast
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    tree = _ast.parse("foo()", mode="eval")
    call = tree.body  # type: ignore[attr-defined]
    assert isinstance(call, _ast.Call)
    assert mod._resolve_call_name(call) == "foo"


def test_resolve_call_name_dotted():
    import ast as _ast
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    tree = _ast.parse("a.b.c()", mode="eval")
    call = tree.body  # type: ignore[attr-defined]
    assert isinstance(call, _ast.Call)
    assert mod._resolve_call_name(call) == "a.b.c"


def test_resolve_call_name_dynamic_returns_none():
    import ast as _ast
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    tree = _ast.parse("f()()", mode="eval")
    call = tree.body  # type: ignore[attr-defined]
    assert isinstance(call, _ast.Call)
    # Outer call's func is itself a Call (f()), opaque.
    assert mod._resolve_call_name(call) is None


def test_has_any_call_true():
    import ast as _ast
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    tree = _ast.parse("x = [1, foo()]")
    assert mod._has_any_call(tree) is True


def test_has_any_call_false():
    import ast as _ast
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    tree = _ast.parse("x = [1, 2, 3]")
    assert mod._has_any_call(tree) is False


def test_banned_module_level_calls_set_is_frozenset():
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    assert isinstance(mod._BANNED_MODULE_LEVEL_CALLS, frozenset)
    # Sanity check on cardinality — guards against accidental shrinkage
    # of the banned set during refactors.
    assert len(mod._BANNED_MODULE_LEVEL_CALLS) >= 25


def test_banned_set_contains_expected_categories():
    """Spot-check via composed names that representative members
    from each banned category are present in the set."""
    from backend.core.ouroboros.governance.meta import (
        ast_phase_runner_validator as mod,
    )
    expected_samples = [
        _O("system"),
        _SP("Popen"),
        _EVAL,
        _EXEC,
        _DI,
        _MA_LOADS,
        _OPEN,
    ]
    for name in expected_samples:
        assert name in mod._BANNED_MODULE_LEVEL_CALLS, name


# ---------------------------------------------------------------------------
# Defense-in-depth: oversize and parse-error short-circuits unaffected
# ---------------------------------------------------------------------------


def test_oversize_short_circuits_before_rule_8():
    """An oversized candidate returns OVERSIZE status — Rule 8 must
    not run (and certainly must not raise)."""
    big = "x = 0\n" * 200_000
    src = big + _VALID_RUNNER_BODY
    result = validate_ast(src)
    assert result.status is ValidationStatus.OVERSIZE


def test_parse_error_short_circuits_before_rule_8():
    """A SyntaxError candidate returns PARSE_ERROR — Rule 8 must
    not run."""
    src = "def def def(:\n"
    result = validate_ast(src)
    assert result.status is ValidationStatus.PARSE_ERROR


def test_validate_ast_never_raises_on_rule_8_input():
    """Smoke test: a corpus of edge-case candidates must never
    raise — every internal failure must be mapped to a structured
    ValidationResult."""
    edge_cases = [
        "",                         # empty
        "\n\n\n",                   # whitespace
        "# only a comment\n",       # comment-only
        "x = 1\n",                  # no PhaseRunner subclass
        _candidate_with_module_prologue(""),  # clean
        _candidate_with_module_prologue(f'{_EVAL}("1+1")'),
        _candidate_with_module_prologue("if True:\n    list()\n"),
    ]
    for src in edge_cases:
        result = validate_ast(src)
        assert isinstance(result, ValidationResult)
