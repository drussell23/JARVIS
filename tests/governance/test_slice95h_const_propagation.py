"""Slice 95h — banned-constant propagation hardening of the LIVE adversarial cage.

The converged ~8.8% adversarial benchmark's taxonomy captured a
"constant-in-variable" technique that defeats literal-string matching: a banned
reflection attr NAME is held in a PLAIN string-literal local, then used to
resolve the attr dynamically via ``getattr``/``setattr``::

    lookup = "__subclasses__"
    getattr(obj, lookup)()           # banned attr name via a plain-literal var

The existing Rule 11 (``_check_taint_flow`` + ``_collect_tainted_names``) taints
only string-ASSEMBLY exprs (``'o'+'s'``, ``"".join(...)``, ``.format``). A PLAIN
literal (``lookup = "__subclasses__"``) is NOT assembled → not tainted → the
``getattr(obj, lookup)`` escapes. Slice 95h closes this by ALSO tracking plain
string-literal bindings whose VALUE is a banned reflection attr name, and
flagging their flow into ``getattr``/``setattr``/``delattr`` attr-sinks —
whether or not the result is immediately called.

ZERO clean-control false positives is a HARD, non-negotiable invariant: a false
positive in this LIVE cage BLOCKS legitimate autonomous code-apply. The
clean-control matrix below is the gating deliverable.

All detections stay under the existing ``TAINT_EXPLOIT`` reason + the existing
kill-switch ``JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT`` (default TRUE) — same
rule (Rule 11), no new ValidationFailureReason, no new flag.
"""
from __future__ import annotations

import textwrap
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (  # noqa: E501
    ValidationFailureReason,
    ValidationStatus,
    _BANNED_INTROSPECTION_ATTRS,
    _collect_banned_const_names,
    validate_ast,
)
import ast as _ast


_VALIDATOR_FLAG = "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED"
_TAINT_FLAG = "JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Master flag + the taint-exploit kill-switch default-TRUE
    (security-on); clear any stray env so the defaults take effect."""
    for flag in (_VALIDATOR_FLAG, _TAINT_FLAG):
        monkeypatch.delenv(flag, raising=False)
    yield


# ---------------------------------------------------------------------------
# Source builder — mirrors tests/governance/test_slice95g_indirection_taint.py
# ---------------------------------------------------------------------------


def _runner_body(body: str) -> str:
    """Wrap a function-body fragment in a valid PhaseRunner stub so Rules 1-6
    (incl. the Rule-6 import allowlist) don't short-circuit."""
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


# ===========================================================================
# 1. ADVERSARIAL INTERCEPTION
#
# Two sub-groups, because the LIVE cage is defense-in-depth and ORDER matters:
#
#  (1a) Cases the NEW Slice-95h Rule-11 check is the *first* (and in some cases
#       only) rule to catch → MUST block with reason == TAINT_EXPLOIT. These
#       are the genuine gaps 95h closes: setattr/delattr with a banned-const
#       attr key (Rule 7's getattr-only matcher never sees them), and getattr
#       via an ALIAS-PROPAGATED banned-const (`a=X; b=a; getattr(self,b)` —
#       Rule 7's constant-fold name-map does not chase `b=a` aliases).
#
#  (1b) The plain getattr-with-banned-literal / single-binding forms. These are
#       ALSO caught by the pre-existing Rule 7 (INTROSPECTION_ESCAPE), which
#       runs BEFORE Rule 11 in validate_ast — so they block under
#       INTROSPECTION_ESCAPE, NOT TAINT_EXPLOIT. What the security invariant
#       requires is that they BLOCK (they do, doubly so). We assert BLOCKED and
#       that the new Rule-11 check ALSO independently catches them (proven via
#       _check_taint_flow directly) so 95h's coverage is complete even if Rule
#       7 were ever disabled.
# ===========================================================================


# (1a) — NEW coverage; the 95h check is the gating rule → reason TAINT_EXPLOIT.
@pytest.mark.parametrize(
    "body",
    [
        # alias propagation b = a → getattr (Rule 7's fold map misses `b=a`)
        'a = "__class__"\nb = a\ngetattr(self, b)',
        # setattr with banned attr literal (Rule 7 is getattr-only)
        'setattr(obj, "__class__", x)',
        # setattr via banned-const variable
        'k = "__getattribute__"\nsetattr(obj, k, x)',
        # delattr with banned attr literal
        'delattr(obj, "__class__")',
        # delattr via banned-const variable
        'g = "__getattr__"\ndelattr(obj, g)',
    ],
)
def test_const_propagation_new_coverage_taint_exploit(body):
    src = _runner_body(body)
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED, (
        f"escape not blocked: {body!r} → {result.status} {result.detail!r}"
    )
    assert result.reason is ValidationFailureReason.TAINT_EXPLOIT, (
        f"{body!r} blocked for {result.reason}, expected TAINT_EXPLOIT "
        f"(detail={result.detail!r})"
    )


# (1b) — defense-in-depth getattr forms: MUST block (Rule 7 wins the race).
@pytest.mark.parametrize(
    "body",
    [
        'lookup = "__subclasses__"\ngetattr(obj, lookup)()',
        'lookup = "__subclasses__"\ngetattr(obj, lookup)',
        'getattr(obj, "__bases__")',
        'getattr(type(obj), "__mro__")',
        'd = "__dict__"\ngetattr(obj, d)',
    ],
)
def test_const_propagation_getattr_blocked_defense_in_depth(body):
    src = _runner_body(body)
    result = validate_ast(src)
    assert result.status is ValidationStatus.FAILED, (
        f"escape not blocked: {body!r} → {result.status} {result.detail!r}"
    )
    # Blocked by whichever rule fires first — Rule 7 (INTROSPECTION_ESCAPE)
    # precedes Rule 11 in validate_ast. Security requires only that it BLOCK.
    assert result.reason in (
        ValidationFailureReason.INTROSPECTION_ESCAPE,
        ValidationFailureReason.TAINT_EXPLOIT,
    ), (
        f"{body!r} blocked for unexpected reason {result.reason} "
        f"(detail={result.detail!r})"
    )


# Prove the NEW Rule-11 check ALSO catches the (1b) getattr forms on its own —
# i.e. 95h's coverage is complete even if Rule 7 were disabled. We call
# _check_taint_flow directly (the unit under test) on the parsed tree.
@pytest.mark.parametrize(
    "body, expected_sink_prefix",
    [
        ('lookup = "__subclasses__"\ngetattr(obj, lookup)()', "sink=getattr:"),
        ('lookup = "__subclasses__"\ngetattr(obj, lookup)', "sink=getattr:"),
        ('getattr(obj, "__bases__")', "sink=getattr:"),
        ('getattr(type(obj), "__mro__")', "sink=getattr:"),
        ('d = "__dict__"\ngetattr(obj, d)', "sink=getattr:"),
    ],
)
def test_new_check_independently_catches_getattr(body, expected_sink_prefix):
    from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (  # noqa: E501
        _check_taint_flow,
    )
    tree = _ast.parse(textwrap.dedent(body))
    diag = _check_taint_flow(tree)
    assert diag is not None and diag.startswith(expected_sink_prefix), (
        f"new check did not catch {body!r}: {diag!r}"
    )
    assert diag.endswith("banned_attr_const_propagated"), diag


# Direct unit test of the new collector — proves it ONLY collects names bound
# to a plain Constant string whose value is a banned reflection attr.
@pytest.mark.parametrize(
    "src, expected",
    [
        ('lookup = "__subclasses__"', {"lookup"}),
        ('a = "__class__"\nb = a', {"a", "b"}),
        # benign string consts are NEVER collected
        ('x = "hello"', set()),
        ('s = "__main__"', set()),       # __main__ is NOT a banned reflection attr
        ('mode = "__doc__"', set()),     # __doc__ is benign — must not collect
        ('name = "items"', set()),
        # assembled (not plain) string is Rule-11-taint, not banned-const
        ("p = 'o' + 's'", set()),
        # multiple
        ('a = "__bases__"\nx = "ok"\nc = a', {"a", "c"}),
    ],
)
def test_collect_banned_const_names(src, expected):
    tree = _ast.parse(textwrap.dedent(src))
    assert _collect_banned_const_names(tree) == frozenset(expected)


def test_banned_set_excludes_benign_dunders():
    """Sanity: the rule is driven off _BANNED_INTROSPECTION_ATTRS, and the
    benign dunders that legitimately appear in getattr keys are NOT in it."""
    for benign in ("__doc__", "__name__", "__module__", "__main__", "__init__"):
        assert benign not in _BANNED_INTROSPECTION_ATTRS, (
            f"{benign} unexpectedly banned — would cause a false positive"
        )
    # high-signal reflection attrs ARE banned (drive the new check)
    for banned in ("__subclasses__", "__bases__", "__class__"):
        assert banned in _BANNED_INTROSPECTION_ATTRS


# ===========================================================================
# 2. CLEAN-CONTROL MATRIX — must PASS, zero false positives. THE GATE.
# ===========================================================================


@pytest.mark.parametrize(
    "body",
    [
        # --- getattr with benign string literals ---------------------------
        'getattr(obj, "value")',
        'getattr(self, "_cache", None)',
        'getattr(ctx, "data", None)',
        'getattr(obj, "items")',
        # --- getattr with an UNKNOWN var (function param / runtime value) --
        # `name` is the run() body's free var here; its value is unknown so it
        # must NOT be flagged — only proven banned-const names flow.
        "getattr(obj, name)",
        "getattr(obj, name, None)",
        # --- getattr with a benign string CONST in a var -------------------
        'attr = "items"\ngetattr(obj, attr)',
        'fld = "_value"\ngetattr(self, fld, None)',
        # --- benign dunder string consts (NOT banned reflection attrs) -----
        's = "__main__"\nif __name__ == s:\n    return None',
        'mode = "__doc__"\nreturn getattr(obj, mode)',
        'n = "__name__"\nreturn getattr(obj, n)',
        'fmt = "{}"\nreturn fmt.format(x)',
        # --- setattr / delattr benign -------------------------------------
        'setattr(self, "_x", 1)',
        "setattr(obj, key, v)",          # key is an unknown var/param
        'attr = "_priv"\nsetattr(self, attr, 1)',
        "delattr(obj, key)",             # unknown var
        'delattr(obj, "_scratch")',
        # --- benign string locals that happen to be near getattr ----------
        'lookup = "value"\nreturn getattr(obj, lookup, None)',
    ],
)
def test_clean_control_not_flagged(body):
    src = _runner_body(body)
    result = validate_ast(src)
    assert result.status is ValidationStatus.PASSED, (
        f"FALSE POSITIVE: clean control flagged: {body!r} → "
        f"{result.status} reason={result.reason} detail={result.detail!r}"
    )


# A realistic ~25-line benign PhaseRunner — the strongest FP probe.
_REALISTIC_BENIGN = textwrap.dedent('''
    from backend.core.ouroboros.governance.phase_runner import PhaseRunner
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.phase_runner import PhaseResult


    class RealisticPR(PhaseRunner):
        phase = "VALIDATE"

        async def run(self, ctx: OperationContext) -> PhaseResult:
            try:
                data = getattr(ctx, "data", None)
                if data is None:
                    return None
                mode = "summary"
                results = []
                for key in ("alpha", "beta", "gamma"):
                    value = getattr(data, key, None)
                    if value is not None:
                        results.append((key, value))
                doc_field = "__doc__"
                label = getattr(self, doc_field, "")
                cache = {k: v for k, v in results if v}
                names = [n for n in cache if not n.startswith("_")]
                setattr(self, "_last_mode", mode)
                return PhaseResult(ok=True, detail=f"{label}:{len(names)}")
            except Exception:
                return None
''').strip()


def test_realistic_benign_runner_validates_clean():
    result = validate_ast(_REALISTIC_BENIGN)
    assert result.status is ValidationStatus.PASSED, (
        f"FALSE POSITIVE on realistic benign runner: "
        f"{result.status} reason={result.reason} detail={result.detail!r}"
    )


# ===========================================================================
# 3. KILL-SWITCH — disabling JARVIS_AST_VALIDATOR_BLOCK_TAINT_EXPLOIT
#    disables the new check (and the rest of Rule 11).
# ===========================================================================


@pytest.mark.parametrize(
    "body",
    [
        # NEW-coverage cases: ONLY Rule 11 catches these (Rule 7 is getattr-only
        # and does not chase `b=a` aliases) — so with Rule 11 disabled they must
        # PASS. (getattr-with-banned-literal is intentionally NOT used here: it
        # is also caught by Rule 7, whose own kill-switch is separate.)
        'a = "__class__"\nb = a\ngetattr(self, b)',
        'setattr(obj, "__class__", x)',
        'k = "__getattribute__"\nsetattr(obj, k, x)',
        'delattr(obj, "__class__")',
    ],
)
def test_kill_switch_disables_new_check(body, monkeypatch):
    monkeypatch.setenv(_TAINT_FLAG, "false")
    src = _runner_body(body)
    result = validate_ast(src)
    assert result.reason is not ValidationFailureReason.TAINT_EXPLOIT, (
        f"kill-switch failed: {body!r} still TAINT_EXPLOIT "
        f"(status={result.status} detail={result.detail!r})"
    )
    assert result.status is ValidationStatus.PASSED, (
        f"kill-switch off but still failed for another reason: {body!r} → "
        f"{result.status} reason={result.reason} detail={result.detail!r}"
    )
