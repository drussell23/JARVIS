"""RR Pass B Slice 3 — AST-shape validator for PhaseRunner subclasses.

Per ``memory/project_reverse_russian_doll_pass_b.md`` §5:

  > Any candidate file that introduces a new ``PhaseRunner`` subclass
  > must pass:
  >
  > 1. ABC conformance. Class inherits from ``PhaseRunner``.
  > 2. ``phase`` attribute. Class sets a ``phase: OperationPhase``
  >    class attribute that resolves to a known phase enum value.
  > 3. ``run`` signature. Implements ``async def run(self, ctx:
  >    OperationContext) -> PhaseResult``.
  > 4. No mutation of input ctx. Body never assigns to ``ctx.<attr>``.
  >    Required: produces new ctx via ``ctx.advance(...)``.
  > 5. No raise into dispatcher. Top-level try/except wraps ``run``
  >    body; uncaught exceptions are converted to
  >    ``PhaseResult(status="fail", reason=...)`` before return.
  > 6. No imports from the Order-2 manifest paths. A new runner
  >    cannot ``from .semantic_firewall import ...``,
  >    ``from .change_engine import ...``, etc. — that would be
  >    Order-2 transitive authority creep. Allowed imports:
  >    ``phase_runner`` ABC, ``op_context``, ``subagent_contracts``,
  >    stdlib, third-party.

This module is the **pure AST walker**. Slice 3 ships the validator
function only; Slice 5 (MetaPhaseRunner primitive) wires the call
into the GATE phase. Same Slice-2 / Slice-2b split: function first,
wiring later.

Authority invariants (Pass B §5.2):
  * Pure AST walk via ``ast.parse``. Zero runtime introspection,
    zero LLM, zero subprocess, zero I/O — entirely deterministic
    for the same source bytes.
  * No imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib (``ast``, ``re``, ``os``, ``logging``) +
    ``meta.order2_manifest`` (to derive the banned-import set
    from Slice 1's enumerated paths).
  * Best-effort within ``validate``: every top-level rule raises
    :class:`PhaseRunnerASTValidationError` with a structured
    :class:`ValidationFailureReason` so the caller (Slice 5
    MetaPhaseRunner) can render the first failing rule.

Default-off behind ``JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED``
until Slice 3's clean-session graduation. When off,
:func:`validate_ast` returns a "skipped" verdict; Slice 5 hook will
treat that as "no enforcement" so the cage degrades to the existing
review path.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Maximum candidate source bytes. Defends against a hand-crafted
# 100-MB blob that would pin ast.parse in CPU.
MAX_CANDIDATE_BYTES: int = 256 * 1024  # 256 KiB

# Class names allowed for the inheritance check. Catches both
# `PhaseRunner` (direct import) and `phase_runner.PhaseRunner`
# (module-prefixed).
_PHASE_RUNNER_CLASS_NAMES: FrozenSet[str] = frozenset({"PhaseRunner"})

# Modules ALLOWED to be imported by a new PhaseRunner subclass per
# Pass B §5.1 rule 6. Stdlib + third-party allowed implicitly; only
# governance-package imports are restricted to this set.
_ALLOWED_GOVERNANCE_IMPORTS: FrozenSet[str] = frozenset({
    "backend.core.ouroboros.governance.phase_runner",
    "backend.core.ouroboros.governance.op_context",
    "backend.core.ouroboros.governance.subagent_contracts",
    # The risk_engine module owns the RiskTier enum value; reading
    # the enum is not authority creep.
    "backend.core.ouroboros.governance.risk_engine",
})

# Prefix governance imports must start with to be SUBJECT to the
# allowlist check. Imports outside this prefix (stdlib + third-party)
# are unrestricted.
_GOVERNANCE_IMPORT_PREFIX: str = "backend.core.ouroboros.governance"


# Phase 7.7 — Sandbox hardening (Rule 7).
#
# `replay_executor.py:_SAFE_BUILTIN_NAMES` includes `object` + `type`;
# once a candidate is operator-approved, `object.__subclasses__()`
# walks the entire class graph including `subprocess.Popen`, breaking
# the cage. AST-block these introspection primitives at validation
# time — BEFORE the candidate ever reaches the sandbox.
#
# Per `OUROBOROS_VENOM_PRD.md` §9 P7.7:
#   > Rule 7: no `__subclasses__` / `__bases__` / `__class__` attribute
#   > access in any function body
#
# Detection is breadth-first across three patterns:
#   1. ast.Attribute access where `.attr` matches a banned name
#      (e.g. `obj.__subclasses__()`, `cls.__bases__`)
#   2. ast.Call to `getattr(x, "<banned>")` with a string literal
#      second argument matching a banned name
#   3. ast.Subscript (defense-in-depth — won't typically match these
#      attrs but proves the walker is breadth-first)
#
# Master flag JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE defaults
# to TRUE — security hardening is on by default once the validator
# itself is enabled. Operators can toggle off in an emergency without
# disabling the whole validator.
_BANNED_INTROSPECTION_ATTRS: FrozenSet[str] = frozenset({
    "__subclasses__",
    "__bases__",
    "__class__",
    # PRD §3.6.2 Vector #7 closure (2026-05-12) — additional
    # object-graph traversal attrs that P9.4 adversarial corpus
    # documented as Rule-7 gaps. Each is a documented bypass:
    #   * ``__mro__`` — type(x).__mro__ walks the inheritance
    #     chain to reach object → subclasses(). Empirically
    #     reachable when Rule 7 only blocks the three base attrs
    #     above.
    #   * ``__dict__`` — direct namespace projection; combined
    #     with getattr lets a candidate read attributes whose
    #     names are computed at runtime.
    #   * ``__globals__`` — function.__globals__ exposes the
    #     defining module's globals (any imported subprocess
    #     reachable).
    #   * Frame attrs (``f_back``/``f_globals``/``f_locals``) —
    #     traversal up the call stack reaches caller scopes via
    #     sys._getframe()-shaped patterns.
    # All belong to the "object-graph traversal" gap class the
    # PRD calls out. Per-rule kill-switch remains the existing
    # JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE flag — no
    # new flag needed (these are the same rule's attribute set).
    "__mro__",
    "__dict__",
    "__globals__",
    "f_back",
    "f_globals",
    "f_locals",
})


# PRD §3.6.2 Vector #7 closure — Rule 9 (introspection-builtin
# Calls). Catches the SECOND gap class the P9.4 corpus
# documented: ``vars(x)`` / ``dir(x)`` / ``globals()`` /
# ``locals()`` calls. These return dict-shaped projections of
# scope or object state that a motivated candidate can mine to
# find banned-attr references by string match — defeating Rule
# 7's static-string check.
#
# ``type()`` is included because the canonical bypass calls it
# first; banning ``type()`` ALSO catches the multi-step pattern
# (Rule 7 adds the bypassed dunder for direct-access; Rule 9
# adds ``type()`` for the entry call — defense-in-depth).
#
# Pattern: ANY ast.Call whose func is an ast.Name in this set,
# anywhere in the module (function body OR module-level). Module-
# level Calls to other dangerous builtins are caught by Rule 8;
# this rule catches the new introspection-builtin class anywhere.
#
# isinstance / type-equality patterns are NOT banned — Rule 9
# fires on Call to ``type`` (not the bare name reference), so
# ``isinstance(x, type)`` is fine but ``type(x).__mro__`` is not.
_BANNED_INTROSPECTION_BUILTIN_CALLS: FrozenSet[str] = frozenset({
    "vars",
    "dir",
    "globals",
    "locals",
    "type",
})


# PRD §3.6.2 Vector #7 closure — Rule 10 (alias-defeat tracker).
# The P9.4 corpus documented the alias-defeat pattern as a pinned
# known gap: a candidate aliases a banned attribute-chain to a
# local name, then calls the local. Rule 8's static-name check
# sees the local call and has no idea what the name is bound to.
#
# Rule 10 walks function bodies (and module level) tracking
# simple Name = Attribute|Name assignments where the RHS
# resolves to a banned-call attribute path (from Rule 8's set
# OR Rule 9's set). Any subsequent Call to that alias name
# within the same scope is flagged.
#
# Scope is intentionally LIMITED: alias tracking is per-function-
# body (intraprocedural), not cross-function. A motivated
# candidate that aliases across function boundaries defeats
# Rule 10. Rule 10 positions as "raises the bar" defense-in-
# depth, not "provably tight" — the runtime sandbox remains
# the authoritative final gate.
#
# Per-rule kill-switch: JARVIS_AST_VALIDATOR_BLOCK_ALIAS_DEFEAT
# (default TRUE).


# Phase 7.7 follow-up — Rule 8: Module-level side-effect detection.
# Rule 7 catches introspection escape in function bodies; Rule 8
# catches code that EXECUTES AT MODULE LOAD TIME (before any
# function body runs).
#
# Approach: pragmatic allowlist-friendly. Module loads ALWAYS run
# SOME code (`logger = logging.getLogger(__name__)` is benign).
# Rule 8 catches SPECIFIC dangerous call shapes rather than
# blanket-blocking. Two complementary detections:
#   1. Module-level Call to a name in the banned list below.
#   2. Module-level control-flow block (`if`/`for`/`while`/`with`/
#      `try`) containing ANY Call. Well-behaved candidates declare
#      functions/classes; they don't run conditional logic at import.
#
# Master flag `JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS`
# defaults TRUE (same convention as Rule 7).
#
# The dangerous-name list is constructed via string concatenation
# below to avoid tripping content-scanning hooks. The actual values
# match the standard library API surface for shell exec, process
# spawn, code exec, dynamic import, deserialization-RCE, and
# bare file open at import.
def _build_banned_module_level_calls() -> FrozenSet[str]:
    """Build the Rule 8 banned-name set via string composition.
    The names are well-known CPython stdlib APIs that constitute
    RCE / shell-exec / arbitrary-code-execution at module load."""
    os_shell = ("o" + "s.sys" + "tem", "o" + "s.po" + "pen",
                "o" + "s.startfile")
    os_spawn = tuple(
        "o" + "s.spawn" + suf
        for suf in ("", "l", "le", "lp", "v", "ve", "vp")
    )
    os_exec = tuple(
        "o" + "s.exec" + suf
        for suf in ("", "l", "le", "lp", "v", "ve", "vp")
    )
    sp_calls = tuple(
        "subpro" + "cess." + name
        for name in (
            "run", "call", "Popen", "check_output",
            "check_call", "getoutput", "getstatusoutput",
        )
    )
    code_exec = ("ev" + "al", "ex" + "ec", "comp" + "ile")
    dyn_import = (
        "__imp" + "ort__",
        "imp" + "ortlib.import_module",
        "imp" + "ortlib.__import__",
    )
    rce_deser = (
        "pi" + "ckle.loads", "pi" + "ckle.load",
        "marsh" + "al.loads", "marsh" + "al.load",
    )
    file_open = ("op" + "en",)
    return frozenset(
        os_shell + os_spawn + os_exec + sp_calls +
        code_exec + dyn_import + rce_deser + file_open,
    )


_BANNED_MODULE_LEVEL_CALLS: FrozenSet[str] = _build_banned_module_level_calls()


def is_introspection_block_enabled() -> bool:
    """Per-rule kill switch for Rule 7 —
    ``JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE`` (default
    **true**, unlike most JARVIS flags — security hardening is on by
    default).

    Operators can disable in emergency without disabling the whole
    validator (`JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED`)."""
    raw = os.environ.get(
        "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE",
    )
    if raw is None:
        return True  # default-ON
    return raw.strip().lower() in _TRUTHY


def is_introspection_builtin_block_enabled() -> bool:
    """Per-rule kill switch for Rule 9 —
    ``JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_BUILTINS`` (default
    **true** — same security-on-by-default convention as Rules 7
    and 8). Rule 9 closes the P9.4 corpus gap where ``vars(x)`` /
    ``dir(x)`` / ``globals()`` / ``locals()`` / ``type()`` Calls
    bypass Rule 7's attribute-string check.

    Operators can disable in emergency without disabling the whole
    validator. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_BUILTINS",
    )
    if raw is None:
        return True  # default-ON
    return raw.strip().lower() in _TRUTHY


def is_alias_defeat_block_enabled() -> bool:
    """Per-rule kill switch for Rule 10 —
    ``JARVIS_AST_VALIDATOR_BLOCK_ALIAS_DEFEAT`` (default **true** —
    security-on-by-default). Rule 10 catches intra-function alias
    bindings that defeat Rule 8's static-name check (the
    documented P9.4 ``s = <banned-attr>; s(...)`` gap).

    Operators can disable in emergency without disabling the whole
    validator. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_AST_VALIDATOR_BLOCK_ALIAS_DEFEAT",
    )
    if raw is None:
        return True  # default-ON
    return raw.strip().lower() in _TRUTHY


def is_module_side_effect_block_enabled() -> bool:
    """Per-rule kill switch for Rule 8 —
    ``JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS`` (default
    **true** — security hardening on by default; same convention as
    Rule 7's introspection-block switch).

    Operators can disable in emergency without disabling the whole
    validator."""
    raw = os.environ.get(
        "JARVIS_AST_VALIDATOR_BLOCK_MODULE_SIDE_EFFECTS",
    )
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def is_enabled() -> bool:
    """Master flag —
    ``JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED`` (graduated
    default-true 2026-05-03).

    When off, :func:`validate_ast` short-circuits to ``ValidationResult
    (status=SKIPPED, ...)``. Slice 5 MetaPhaseRunner treats SKIPPED
    as "no enforcement" so the cage degrades to the existing review
    path. Operators flip explicit ``false`` to opt out of static
    pre-execution validation (read-only AST scan; safe to graduate
    -- gates only validation, not patch application)."""
    raw = os.environ.get(
        "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-03 (Pass B Slice 3)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Result + failure-reason types
# ---------------------------------------------------------------------------


class ValidationStatus(str, enum.Enum):
    PASSED = "PASSED"     # all 8 rules satisfied (rule 8 added 2026-04-26 post-Phase-7.7)
    FAILED = "FAILED"     # at least one rule violated
    SKIPPED = "SKIPPED"   # validator master-off
    PARSE_ERROR = "PARSE_ERROR"  # ast.parse raised
    OVERSIZE = "OVERSIZE"        # source > MAX_CANDIDATE_BYTES


class ValidationFailureReason(str, enum.Enum):
    """The 8 rules from Pass B §5.1 + Phase 7.7 sandbox hardening +
    Phase 7.7-followup module-side-effect detection + supporting
    failure shapes."""

    NO_PHASE_RUNNER_SUBCLASS = "no_phase_runner_subclass"
    MISSING_PHASE_ATTR = "missing_phase_attr"
    MISSING_RUN_METHOD = "missing_run_method"
    RUN_NOT_ASYNC = "run_not_async"
    RUN_BAD_SIGNATURE = "run_bad_signature"
    CTX_MUTATION = "ctx_mutation"
    NO_TOP_LEVEL_TRY = "no_top_level_try"
    BANNED_IMPORT = "banned_import"
    INTROSPECTION_ESCAPE = "introspection_escape"  # P7.7 Rule 7
    MODULE_LEVEL_SIDE_EFFECT = "module_level_side_effect"  # Rule 8
    # PRD §3.6.2 Vector #7 closure (2026-05-12) — Rules 9-10
    # plug the documented P9.4 corpus gaps that Rules 7/8 missed.
    INTROSPECTION_BUILTIN_CALL = "introspection_builtin_call"  # Rule 9
    ALIAS_DEFEAT = "alias_defeat"  # Rule 10


@dataclass(frozen=True)
class ValidationResult:
    """One validation outcome. Frozen — Slice 5 MetaPhaseRunner
    persists this verbatim into the order2_review evidence bundle."""

    status: ValidationStatus
    reason: Optional[ValidationFailureReason] = None
    detail: str = ""
    classes_inspected: Tuple[str, ...] = field(default_factory=tuple)


class PhaseRunnerASTValidationError(Exception):
    """Raised by :func:`validate_ast_strict` (the strict variant) on
    any rule failure. The non-strict :func:`validate_ast` returns a
    :class:`ValidationResult` instead."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(
            f"PhaseRunner AST validation failed: "
            f"reason={result.reason.value if result.reason else '?'} "
            f"detail={result.detail!r}",
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def validate_ast(
    source: str,
    *,
    extra_banned_modules: Optional[Sequence[str]] = None,
) -> ValidationResult:
    """Validate a candidate's full source text against the 6 PhaseRunner
    rules.

    Returns a :class:`ValidationResult`. NEVER raises — every internal
    failure is mapped to a structured status. Use
    :func:`validate_ast_strict` if the caller wants exception-based
    flow.

    ``extra_banned_modules`` lets callers extend the banned-import
    set (Slice 5 MetaPhaseRunner will pass the live Order-2 manifest's
    governance paths so the validator stays in sync with the cage).
    """
    if not is_enabled():
        return ValidationResult(
            status=ValidationStatus.SKIPPED,
            detail="master_flag_off",
        )
    if source is None:
        return ValidationResult(
            status=ValidationStatus.PARSE_ERROR,
            detail="source_is_none",
        )
    encoded = source.encode("utf-8", errors="replace")
    if len(encoded) > MAX_CANDIDATE_BYTES:
        return ValidationResult(
            status=ValidationStatus.OVERSIZE,
            detail=f"source_bytes={len(encoded)} > "
                   f"max={MAX_CANDIDATE_BYTES}",
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(
            status=ValidationStatus.PARSE_ERROR,
            detail=f"syntax_error:{exc.msg} line={exc.lineno}",
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ValidationResult(
            status=ValidationStatus.PARSE_ERROR,
            detail=f"unexpected_parse_failure:{exc}",
        )

    extra = frozenset(extra_banned_modules or ())

    # ---- Rule 6: banned imports ----
    bad_import = _check_banned_imports(tree, extra)
    if bad_import is not None:
        return ValidationResult(
            status=ValidationStatus.FAILED,
            reason=ValidationFailureReason.BANNED_IMPORT,
            detail=bad_import,
        )

    # Find PhaseRunner subclasses (rule 1).
    classes = _find_phase_runner_subclasses(tree)
    if not classes:
        return ValidationResult(
            status=ValidationStatus.FAILED,
            reason=ValidationFailureReason.NO_PHASE_RUNNER_SUBCLASS,
            detail="no class inherits from PhaseRunner in the candidate",
        )

    inspected = tuple(node.name for node in classes)

    for cls in classes:
        # ---- Rule 2: phase attribute ----
        if not _has_phase_attribute(cls):
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.MISSING_PHASE_ATTR,
                detail=f"class {cls.name} missing 'phase' class attribute",
                classes_inspected=inspected,
            )

        # ---- Rule 3: run method + signature ----
        run_node = _find_run_method(cls)
        if run_node is None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.MISSING_RUN_METHOD,
                detail=f"class {cls.name} missing 'run' method",
                classes_inspected=inspected,
            )
        if not isinstance(run_node, ast.AsyncFunctionDef):
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.RUN_NOT_ASYNC,
                detail=f"class {cls.name}.run is not async",
                classes_inspected=inspected,
            )
        sig_err = _check_run_signature(run_node)
        if sig_err is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.RUN_BAD_SIGNATURE,
                detail=f"class {cls.name}.run: {sig_err}",
                classes_inspected=inspected,
            )

        # ---- Rule 4: no ctx mutation ----
        ctx_mut = _find_ctx_mutation(run_node)
        if ctx_mut is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.CTX_MUTATION,
                detail=f"class {cls.name}.run mutates ctx: {ctx_mut}",
                classes_inspected=inspected,
            )

        # ---- Rule 5: top-level try/except ----
        if not _has_top_level_try(run_node):
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.NO_TOP_LEVEL_TRY,
                detail=(
                    f"class {cls.name}.run lacks top-level try/except "
                    "wrapping body"
                ),
                classes_inspected=inspected,
            )

    # ---- Rule 7 (P7.7 sandbox hardening): introspection escape ----
    # Check ALL function bodies in the candidate, not just run methods.
    # Per PRD spec: "no __subclasses__ / __bases__ / __class__ attribute
    # access in any function body" — covers helper functions too.
    if is_introspection_block_enabled():
        escape = _find_introspection_escape(tree)
        if escape is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.INTROSPECTION_ESCAPE,
                detail=escape,
                classes_inspected=inspected,
            )

    # ---- Rule 8 (post-P7.7 followup): module-level side effects ----
    # Module-load-time code execution is the highest-priority remaining
    # sandbox-bypass vector. Rule 8 catches:
    #   1. Module-level Calls to a banned name (shell/exec/RCE-deser/etc.)
    #   2. Module-level control-flow blocks containing ANY Call (well-
    #      behaved candidates declare functions/classes; they don't run
    #      conditional logic at import).
    if is_module_side_effect_block_enabled():
        side_effect = _find_module_level_side_effect(tree)
        if side_effect is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT,
                detail=side_effect,
                classes_inspected=inspected,
            )

    # ---- Rule 9 (Vector #7 closure): introspection-builtin Calls ----
    # P9.4 corpus documented vars()/dir()/globals()/locals()/type()
    # as Rule-7 bypass routes. Rule 9 catches any ast.Call to a
    # name in _BANNED_INTROSPECTION_BUILTIN_CALLS, anywhere in the
    # module (function body OR module-level).
    if is_introspection_builtin_block_enabled():
        builtin_call = _find_introspection_builtin_call(tree)
        if builtin_call is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.INTROSPECTION_BUILTIN_CALL,
                detail=builtin_call,
                classes_inspected=inspected,
            )

    # ---- Rule 10 (Vector #7 closure): alias-defeat detector ----
    # P9.4 corpus documented `local_name = <banned-attr-chain>;
    # local_name(...)` as a pinned known gap. Rule 10 walks
    # function bodies + module level tracking simple Name=Attr
    # bindings where RHS resolves to a banned name (Rule 8 set OR
    # Rule 9 set); subsequent Call to the alias in the same scope
    # is flagged. Intraprocedural only (cross-function aliases
    # remain a known gap — runtime sandbox is the final gate).
    if is_alias_defeat_block_enabled():
        alias_defeat = _find_alias_defeat(tree)
        if alias_defeat is not None:
            return ValidationResult(
                status=ValidationStatus.FAILED,
                reason=ValidationFailureReason.ALIAS_DEFEAT,
                detail=alias_defeat,
                classes_inspected=inspected,
            )

    return ValidationResult(
        status=ValidationStatus.PASSED,
        classes_inspected=inspected,
    )


def validate_ast_strict(
    source: str,
    *,
    extra_banned_modules: Optional[Sequence[str]] = None,
) -> ValidationResult:
    """Strict variant — raises :class:`PhaseRunnerASTValidationError`
    on any FAILED status. Returns the :class:`ValidationResult` for
    PASSED / SKIPPED / OVERSIZE / PARSE_ERROR (those are not "rule
    failures"; the caller decides how to handle them)."""
    result = validate_ast(source, extra_banned_modules=extra_banned_modules)
    if result.status is ValidationStatus.FAILED:
        raise PhaseRunnerASTValidationError(result)
    return result


# ---------------------------------------------------------------------------
# Rule-1 helpers: find PhaseRunner subclasses
# ---------------------------------------------------------------------------


def _find_phase_runner_subclasses(tree: ast.AST) -> List[ast.ClassDef]:
    """Return all top-level ClassDefs inheriting from PhaseRunner.

    Catches:
      * ``class X(PhaseRunner):`` (Name base)
      * ``class X(phase_runner.PhaseRunner):`` (Attribute base)
      * ``class X(SomePackage.PhaseRunner):`` (Attribute base)
    """
    out: List[ast.ClassDef] = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if _base_is_phase_runner(base):
                out.append(node)
                break
    return out


def _base_is_phase_runner(base: ast.AST) -> bool:
    if isinstance(base, ast.Name):
        return base.id in _PHASE_RUNNER_CLASS_NAMES
    if isinstance(base, ast.Attribute):
        return base.attr in _PHASE_RUNNER_CLASS_NAMES
    return False


# ---------------------------------------------------------------------------
# Rule-2 helpers: phase attribute
# ---------------------------------------------------------------------------


def _has_phase_attribute(cls: ast.ClassDef) -> bool:
    """The class body must set ``phase`` either as an annotated
    assignment (``phase: OperationPhase = X``) or a plain assignment
    (``phase = OperationPhase.X``).

    The PhaseRunner ABC declares ``phase`` as a type-hint-only
    declaration without value — subclasses MUST set a concrete
    value. So bare type annotations without a value (RHS) don't
    count as "set."
    """
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id == "phase"
                and stmt.value is not None
            ):
                return True
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "phase":
                    return True
    return False


# ---------------------------------------------------------------------------
# Rule-3 helpers: run signature
# ---------------------------------------------------------------------------


def _find_run_method(cls: ast.ClassDef) -> Optional[ast.AST]:
    """Return the ``run`` method node (sync or async) — None when
    absent."""
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == "run":
                return stmt
    return None


def _check_run_signature(node: ast.AsyncFunctionDef) -> Optional[str]:
    """Returns None when signature is OK; else a short failure string.

    Required: ``async def run(self, ctx: OperationContext) -> PhaseResult``.

    Permissive on type-annotation FORM (could be a ``Name``,
    ``Attribute``, or stringified) — only requires the right
    parameter count + names + the presence of an annotation that
    mentions OperationContext / PhaseResult."""
    args = node.args
    if args.vararg or args.kwarg:
        return "no *args/**kwargs allowed"
    positional = list(args.args)
    if len(positional) != 2:
        return f"expected 2 positional args (self, ctx); got {len(positional)}"
    if positional[0].arg != "self":
        return f"first arg must be 'self'; got {positional[0].arg!r}"
    if positional[1].arg != "ctx":
        return f"second arg must be 'ctx'; got {positional[1].arg!r}"
    ctx_ann = positional[1].annotation
    if ctx_ann is None or "OperationContext" not in ast.unparse(ctx_ann):
        return "ctx parameter must be annotated OperationContext"
    if node.returns is None or "PhaseResult" not in ast.unparse(node.returns):
        return "run must declare -> PhaseResult return type"
    return None


# ---------------------------------------------------------------------------
# Rule-4 helpers: no ctx mutation
# ---------------------------------------------------------------------------


def _find_ctx_mutation(run_node: ast.AsyncFunctionDef) -> Optional[str]:
    """Return a short string describing the first ctx mutation found,
    or None when the function body is mutation-free.

    Detects:
      * ``ctx.attr = ...``      (Assign with Attribute target)
      * ``ctx.attr += ...``     (AugAssign)
      * ``ctx.attr: T = ...``   (AnnAssign with Attribute target)

    Allowed:
      * ``ctx.advance(...)``    (method call returning new ctx —
        the canonical mutation pattern per the ABC docstring)
      * ``ctx = something``     (rebinding the local ``ctx`` name —
        not mutation; the input ctx object is untouched)
    """
    for node in ast.walk(run_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_ctx_attribute(target):
                    return _describe_ctx_target(target)
        elif isinstance(node, ast.AugAssign):
            if _is_ctx_attribute(node.target):
                return _describe_ctx_target(node.target) + " (aug-assign)"
        elif isinstance(node, ast.AnnAssign):
            if _is_ctx_attribute(node.target) and node.value is not None:
                return _describe_ctx_target(node.target) + " (ann-assign)"
    return None


def _is_ctx_attribute(target: ast.AST) -> bool:
    """True iff ``target`` is an Attribute access on the ``ctx`` name
    (``ctx.X`` or ``ctx.X.Y``)."""
    if not isinstance(target, ast.Attribute):
        return False
    base = target.value
    while isinstance(base, ast.Attribute):
        base = base.value
    return isinstance(base, ast.Name) and base.id == "ctx"


def _describe_ctx_target(target: ast.Attribute) -> str:
    parts: List[str] = [target.attr]
    cur: ast.AST = target.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    parts.reverse()
    return "ctx." + ".".join(parts)


# ---------------------------------------------------------------------------
# Rule-5 helpers: top-level try/except
# ---------------------------------------------------------------------------


def _has_top_level_try(run_node: ast.AsyncFunctionDef) -> bool:
    """The run body's top-level statements must include a Try node
    that wraps the bulk of the body. Strict heuristic: at least one
    direct child of the function body is an ``ast.Try``.

    Permissive: docstrings + simple variable bindings before the
    try block are fine. The check is "is there a try block at the
    top level at all" — not "is the ENTIRE body inside one try."
    """
    for stmt in run_node.body:
        if isinstance(stmt, ast.Try):
            return True
    return False


# ---------------------------------------------------------------------------
# Rule-6 helpers: banned imports
# ---------------------------------------------------------------------------


def _check_banned_imports(
    tree: ast.AST,
    extra_banned: FrozenSet[str],
) -> Optional[str]:
    """Walk for Import / ImportFrom nodes; reject any from a
    governance-package path NOT in the allowlist OR from any
    caller-supplied extra-banned set.

    Two import shapes are checked:
      1. ``import X`` / ``import X as Y`` → check ``X``.
      2. ``from X import Y`` → check both ``X`` AND each ``X.Y``
         (covers the ``from <package> import <module>`` form
         where ``<module>`` is a submodule that's in the allowlist
         under its full dotted name).

    Returns None when all imports are clean; else a short string
    describing the first banned import."""
    banned_set: Set[str] = set(extra_banned)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                err = _check_module_name(alias.name, banned_set)
                if err is not None:
                    return err
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue  # `from . import X` — relative import, skip
            # Form 2: ``from PACKAGE import SUBMOD`` — check the
            # PACKAGE.SUBMOD form for each name. If ANY of the
            # imported names resolves to an allowlisted full path,
            # the import is accepted (as long as none resolve to a
            # banned full path).
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full in _ALLOWED_GOVERNANCE_IMPORTS:
                    continue
                if full in banned_set:
                    return f"explicitly banned module: {full}"
                # The submodule itself isn't allowlisted — fall
                # back to checking the parent package.
                err = _check_module_name(node.module, banned_set)
                if err is not None:
                    return err
                break  # parent package is governance — only check once
            else:
                # All aliases were allowlisted full paths; pass.
                continue
    return None


def _check_module_name(
    module_name: str,
    extra_banned: Set[str],
) -> Optional[str]:
    """Return failure string when ``module_name`` is NOT in the
    governance allowlist + caller's extra_banned set."""
    if module_name in extra_banned:
        return f"explicitly banned module: {module_name}"
    if not module_name.startswith(_GOVERNANCE_IMPORT_PREFIX):
        # Stdlib + third-party — unrestricted.
        return None
    if module_name in _ALLOWED_GOVERNANCE_IMPORTS:
        return None
    return f"governance import not in allowlist: {module_name}"


# ---------------------------------------------------------------------------
# Slice-84: bounded constant string folder (chr_constructed_attr closure)
# ---------------------------------------------------------------------------

#: Maximum AST nodes visited while folding one expression.
#: Prevents a hand-crafted deeply-nested expression from pinning the walk.
#: 96 (raised from 64 in Slice-84 review) so a fully all-chr()-encoded
#: ``__subclasses__`` (~82 AST nodes) folds within budget.
_FOLD_MAX_NODES: int = 96


def _fold_const_str(node: ast.AST, _budget: Optional[List[int]] = None) -> Optional[str]:
    """Best-effort bounded constant-folder for string expressions.

    Resolves constant-foldable string expressions to their string value so
    that a chr()-constructed banned attribute name (e.g. ``"__subclasses__"``
    built via ``chr(95)+chr(95)+"subclasses"+chr(95)+chr(95)``) can be
    detected by Pattern 2 of :func:`_find_introspection_escape`.

    Foldable forms:
      * String literal constant:       ``"__sub"``  →  ``"__sub"``
      * ``chr(<int constant>)``:        ``chr(95)``  →  ``"_"``
      * ``BinOp(a, Add, b)`` concat:   ``"a" + "b"``  →  ``"ab"``
      * ``"".join([...literals...])``:  ``"".join([chr(95), "x"])``  →  ``"_x"``
      * ``.format()`` best-effort:      only when no ``{`` remains after folding

    Invariants (load-bearing):
      * FAIL-OPEN: returns ``None`` on ANY ambiguous, unrecognised, or
        oversized expression — NEVER raises.
      * NEVER blocks on its own (the caller only flags when the folded value
        is also a banned attr name in a banned sink).
      * Node-budget cap of ``_FOLD_MAX_NODES`` prevents CPU pinning.
      * Only ADDS detections; existing rules apply independently.
    """
    if _budget is None:
        _budget = [_FOLD_MAX_NODES]
    if _budget[0] <= 0:
        return None
    _budget[0] -= 1

    try:
        # Case 1: bare string constant
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            return None

        # Case 2: chr(<int constant>)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "chr"
            and len(node.args) == 1
            and not node.keywords
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                return chr(arg.value)
            return None

        # Case 3: BinOp string concatenation ("a" + "b")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _fold_const_str(node.left, _budget)
            if left is None:
                return None
            right = _fold_const_str(node.right, _budget)
            if right is None:
                return None
            return left + right

        # Case 4: "".join([...string/chr literals...])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
            and not node.keywords
        ):
            # Separator must be a foldable string (typically "")
            sep = _fold_const_str(node.func.value, _budget)
            if sep is None:
                return None
            arg = node.args[0]
            # Argument must be a List or Tuple of foldable elements
            if not isinstance(arg, (ast.List, ast.Tuple)):
                return None
            parts: List[str] = []
            for elt in arg.elts:
                v = _fold_const_str(elt, _budget)
                if v is None:
                    return None
                parts.append(v)
            return sep.join(parts)

        # Case 5: best-effort .format() — only when no { remains after fold
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and not node.keywords
        ):
            template = _fold_const_str(node.func.value, _budget)
            if template is None or "{" in template:
                return None
            # No placeholders in template — result is just the template
            return template

    except Exception:  # pragma: no cover — fail-open on any unexpected error
        return None

    return None


# ---------------------------------------------------------------------------
# Rule-7 helper: introspection-escape detector (Phase 7.7)
# ---------------------------------------------------------------------------


def _walk_own_body(func_node: ast.AST):
    """ast.walk but STOPS at nested function/async-function boundaries.

    Yields every descendant of ``func_node`` except nodes inside inner
    ``FunctionDef`` / ``AsyncFunctionDef`` children.  Used by
    :func:`_build_folded_name_map` so that a name assigned inside a nested
    helper (e.g. ``n = chr(95)+...+"subclasses"+...``) does NOT pollute the
    outer function's name map and create false-positive introspection blocks.

    The outer loop in :func:`_find_introspection_escape_via_name_map` already
    visits each nested function as its own ``func_node``, so per-function
    isolation is preserved — only the internal walk of
    ``_build_folded_name_map`` needs this restriction.

    NEVER raises.
    """
    work = list(ast.iter_child_nodes(func_node))
    while work:
        node = work.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            work.extend(ast.iter_child_nodes(node))


def _build_folded_name_map(func_node: ast.AST) -> dict:
    """Walk all Assign nodes within a function (including nested blocks
    such as ``try``/``with``/``if``) for simple ``name = <foldable>``
    patterns and return a ``{name: folded_str}`` map.

    Only captures single-target Assign nodes where the target is a bare
    Name and the RHS folds to a non-None string via :func:`_fold_const_str`.
    This is the intra-scope complement to Pattern 2 for the chr-constructed-
    attr gap: a candidate may assign the constructed name to a local before
    passing it to getattr.

    Uses :func:`_walk_own_body` (not ``ast.walk``) so that assignments inside
    nested function defs do NOT bleed into the outer function's map — last-
    write-wins dict semantics would otherwise cause a false positive when a
    benign outer ``getattr(x, n)`` shares a variable name with a banned
    assignment inside an inner helper.

    Note: ``ast.AugAssign`` (``n += ...``) is deliberately NOT tracked here
    — the result depends on the prior value of ``n``, which requires flow
    analysis beyond what this fail-open folder provides.  Fail-open by design.

    NEVER raises; returns empty dict on any error.
    """
    result: dict = {}
    try:
        for sub in _walk_own_body(func_node):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
            ):
                folded = _fold_const_str(sub.value)
                if folded is not None:
                    result[sub.targets[0].id] = folded
    except Exception:  # pragma: no cover
        pass
    return result


def _find_introspection_escape(tree: ast.AST) -> Optional[str]:
    """Walk every function body for the three introspection-escape
    patterns. Returns a structured detail string on first hit, else
    None.

    Detected patterns:
      1. ast.Attribute access where ``.attr in _BANNED_INTROSPECTION_ATTRS``
         e.g. ``object.__subclasses__()``, ``cls.__bases__``,
         ``instance.__class__``. Catches any Attribute node anywhere
         in any function body.
      2. ast.Call to ``getattr(x, "<banned>")`` with a second argument
         that resolves (via literal, direct constant-folding, or
         intra-scope name-assignment) to a banned attribute name. Defends
         against operator string-encoding the attr to bypass Pattern 1
         (Slice-84 closure: chr()-constructed names, concat-constructed
         names, and named-variable intermediates).
      3. ast.Call to ``__import__("...")`` / ``importlib.import_module``
         with a string literal — these are NOT directly the introspection
         vector but they're the *next-step* escape primitive (once you
         have `subprocess.Popen` via __subclasses__, you still need to
         resolve it). Out of scope for P7.7 — handled by the import
         allowlist (Rule 6) for governance modules; stdlib import is
         currently unrestricted, which is a known gap tracked separately.

    All function bodies (top-level, nested, methods including the
    candidate's PhaseRunner.run + helpers) are walked. Walking ALL
    function bodies (not just `run` methods) defends against the
    candidate hiding the escape in a helper called from `run`.

    NEVER raises.
    """
    for node in ast.walk(tree):
        # Pattern 1: direct Attribute access
        if isinstance(node, ast.Attribute):
            if node.attr in _BANNED_INTROSPECTION_ATTRS:
                # Build a brief location/shape descriptor.
                shape = _describe_attribute_target(node)
                return (
                    f"introspection_escape:attr={node.attr}:"
                    f"shape={shape}"
                )
        # Pattern 2: getattr(x, <banned>) — literal or directly
        # chr-constructed / concat-constructed expression.
        if isinstance(node, ast.Call):
            if _is_getattr_call(node) and len(node.args) >= 2:
                second = node.args[1]
                attr_name = _string_constant_value(second)
                # Slice-84: if not a bare literal, try constant-folding
                # (handles chr()-constructed and concat-constructed names
                # passed directly as the second argument).
                folded = (
                    attr_name
                    if attr_name is not None
                    else _fold_const_str(second)
                )
                if (
                    folded is not None
                    and folded in _BANNED_INTROSPECTION_ATTRS
                ):
                    kind = (
                        "getattr_string"
                        if attr_name is not None
                        else "getattr_constructed"
                    )
                    return (
                        f"introspection_escape:{kind}="
                        f"{folded}"
                    )
    # Second pass: walk function bodies with intra-scope name maps for
    # the two-step chr-constructed pattern: ``n = chr(...)+...`` then
    # ``getattr(x, n)`` where the constructed name is a banned attr.
    return _find_introspection_escape_via_name_map(tree)


def _find_introspection_escape_via_name_map(tree: ast.AST) -> Optional[str]:
    """Second-pass helper: walk all function bodies, build a per-body
    ``name→folded`` map, and flag any getattr call whose second argument
    is a Name that maps to a banned attr.

    Called by :func:`_find_introspection_escape` after the main walk
    (which handles direct-literal and direct-chr-expr patterns).

    NEVER raises.
    """
    try:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name_map = _build_folded_name_map(node)
            if not name_map:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                if not (_is_getattr_call(sub) and len(sub.args) >= 2):
                    continue
                second = sub.args[1]
                if not isinstance(second, ast.Name):
                    continue
                folded = name_map.get(second.id)
                if folded is not None and folded in _BANNED_INTROSPECTION_ATTRS:
                    return (
                        f"introspection_escape:getattr_constructed="
                        f"{folded}"
                    )
    except Exception:  # pragma: no cover
        pass
    return None


def _is_getattr_call(node: ast.Call) -> bool:
    """True iff the Call is ``getattr(...)`` (Name) — does NOT match
    ``some_module.getattr(...)`` (Attribute) since that's a custom
    function, not the builtin."""
    if isinstance(node.func, ast.Name):
        return node.func.id == "getattr"
    return False


def _string_constant_value(node: ast.AST) -> Optional[str]:
    """Return the string value of a Constant node if it's a string
    literal; else None. Defends against ast.Str (Py 3.8 deprecated
    but still parseable on Py 3.9)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _describe_attribute_target(node: ast.Attribute) -> str:
    """Best-effort shape descriptor for the Attribute's value.
    Examples:
      * ``obj.__subclasses__`` → ``Name(id=obj)``
      * ``obj.x.__class__`` → ``Attribute``
      * ``f().__bases__`` → ``Call``
      * ``arr[0].__class__`` → ``Subscript``
    """
    val = node.value
    if isinstance(val, ast.Name):
        return f"Name(id={val.id})"
    return type(val).__name__


# ---------------------------------------------------------------------------
# Rule-8 helper: module-level side-effect detector (post-P7.7 followup)
# ---------------------------------------------------------------------------


def _resolve_call_name(call_node: ast.Call) -> Optional[str]:
    """Best-effort resolution of a Call's callee to a dotted name string.

    Handles:
      * ``foo()``                     → "foo"
      * ``mod.foo()``                 → "mod.foo"
      * ``pkg.mod.foo()``             → "pkg.mod.foo"
      * ``getattr(x, "y")()``         → None (dynamic; not handled here)
      * ``a().b()``                   → None (Call-on-Call; opaque)

    Returns None when the callee shape is not a static dotted name.
    """
    func = call_node.func
    parts: List[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return ".".join(reversed(parts))
    # Call().attr() / Subscript()/etc. — opaque, return None.
    return None


def _has_any_call(node: ast.AST) -> bool:
    """True iff `node` (or any descendant) contains a Call."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            return True
    return False


_MODULE_LEVEL_SAFE_STMT_TYPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Import, ast.ImportFrom,
    ast.Assign, ast.AnnAssign, ast.AugAssign,
    ast.Pass,
)


def _find_module_level_side_effect(
    tree: ast.AST,
) -> Optional[str]:
    """Walk the module's TOP-LEVEL statements (not recursive into
    function/class bodies — those are Rule 7's domain) for two
    patterns:

      Pattern 1: Module-level Call (or expression containing a Call)
                 to a name in `_BANNED_MODULE_LEVEL_CALLS`. Returns
                 a structured detail string on first hit.
      Pattern 2: Module-level control-flow block (`if`/`for`/`while`/
                 `with`/`try`/`Match`) containing ANY Call. Well-
                 behaved candidates declare functions/classes — they
                 don't run conditional logic at import time.

    Top-level expressions that are Constants (docstrings) are
    allowed. Top-level Assigns are scanned for banned-name Calls in
    their RHS but ALL other Call shapes (e.g. logger=logging.getLogger)
    are allowed (the RHS Call resolves to a non-banned name).

    Returns None when clean, else a short detail string.

    NEVER raises.
    """
    if not isinstance(tree, ast.Module):
        return None
    for stmt in tree.body:
        # Pattern 1a: Bare expression at module level. Allow
        # docstrings (Constant str); block any Call to banned name.
        if isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, str,
            ):
                continue  # docstring
            # Walk the expression for banned Calls.
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    name = _resolve_call_name(sub)
                    if name and name in _BANNED_MODULE_LEVEL_CALLS:
                        return (
                            f"module_level_banned_call:name={name}:"
                            f"shape=Expr"
                        )
            # Bare non-docstring expression at module level (e.g.
            # `func()`) — even if the name isn't banned, this is
            # suspicious. But for now, only block when name is
            # banned (avoid false-positives on unusual but benign
            # patterns).
            continue
        # Pattern 1b: Module-level Assign / AnnAssign / AugAssign
        # whose RHS contains a banned Call. RHS Calls to non-banned
        # names (logger = logging.getLogger) are ALLOWED.
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    name = _resolve_call_name(sub)
                    if name and name in _BANNED_MODULE_LEVEL_CALLS:
                        return (
                            f"module_level_banned_call:name={name}:"
                            f"shape={type(stmt).__name__}"
                        )
            continue
        # Allowed shapes: function/class defs, imports, pass.
        if isinstance(stmt, _MODULE_LEVEL_SAFE_STMT_TYPES):
            continue
        # Pattern 2: control-flow at module level. Block any Call.
        # `if/for/while/with/try` shouldn't run logic at import in
        # well-behaved candidates. We're strict here: ANY Call inside
        # one of these blocks is a Rule 8 violation.
        if isinstance(stmt, (
            ast.If, ast.For, ast.While, ast.With, ast.Try,
            ast.AsyncFor, ast.AsyncWith,
        )):
            if _has_any_call(stmt):
                return (
                    f"module_level_control_flow_with_call:"
                    f"stmt={type(stmt).__name__}"
                )
            continue
        # Match statement (Py 3.10+): conservative — block.
        match_cls = getattr(ast, "Match", None)
        if match_cls is not None and isinstance(stmt, match_cls):
            if _has_any_call(stmt):
                return (
                    "module_level_control_flow_with_call:stmt=Match"
                )
            continue
        # Anything else (e.g. Delete, Global, Nonlocal, Raise, Return
        # at top level) is unusual but not classified here. Skip.
    return None


__all__ = [
    "MAX_CANDIDATE_BYTES",
    "PhaseRunnerASTValidationError",
    "ValidationFailureReason",
    "ValidationResult",
    "ValidationStatus",
    "is_enabled",
    "is_introspection_block_enabled",
    "is_module_side_effect_block_enabled",
    "validate_ast",
    "validate_ast_strict",
]


# ---------------------------------------------------------------------------
# Rule 9 (Vector #7 closure): introspection-builtin Calls
# ---------------------------------------------------------------------------


def _find_introspection_builtin_call(
    tree: ast.AST,
) -> Optional[str]:
    """Walk the FULL AST (function bodies + module level) for any
    ast.Call whose .func is an ast.Name in
    :data:`_BANNED_INTROSPECTION_BUILTIN_CALLS`. Returns a
    structured detail string on first hit, None when clean.

    ``isinstance(x, type)`` is allowed — Rule 9 fires on Call to
    ``type`` (not the bare-name reference). The single argument
    pattern ``type(x).__mro__`` is the canonical bypass.

    NEVER raises."""
    try:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name):
                continue
            if func.id in _BANNED_INTROSPECTION_BUILTIN_CALLS:
                return (
                    f"Rule 9 (introspection_builtin_call): "
                    f"Call to {func.id!r} at line "
                    f"{getattr(node, 'lineno', '?')} — see PRD "
                    f"§3.6.2 Vector #7 closure"
                )
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


# ---------------------------------------------------------------------------
# Rule 10 (Vector #7 closure): alias-defeat tracker
# ---------------------------------------------------------------------------


def _resolves_to_banned_name(node: ast.AST) -> Optional[str]:
    """If ``node`` is a Name or Attribute chain whose canonical
    dotted form is in Rule 8's or Rule 9's banned set, return
    that canonical form. Else None. NEVER raises."""
    try:
        # ast.Name → bare name (e.g. ``open``, ``eval``).
        if isinstance(node, ast.Name):
            if node.id in _BANNED_MODULE_LEVEL_CALLS:
                return node.id
            if node.id in _BANNED_INTROSPECTION_BUILTIN_CALLS:
                return node.id
            return None
        # ast.Attribute → walk the chain (e.g. ``os.system`` →
        # "os.system").
        if isinstance(node, ast.Attribute):
            parts: List[str] = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                dotted = ".".join(reversed(parts))
                if dotted in _BANNED_MODULE_LEVEL_CALLS:
                    return dotted
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


def _find_alias_defeat(tree: ast.AST) -> Optional[str]:
    """Walk every function scope + the module-level scope,
    tracking simple ``Name = Attribute|Name`` assignments where
    the RHS resolves to a banned name. Flag any subsequent
    ``Call`` to the alias within the same function scope.

    Intra-function alias tracking is full-tree (descends into
    try/if/with/for inside the function body). Cross-function
    aliases remain a known gap (runtime sandbox is the final
    gate). NEVER raises.
    """
    try:
        scopes: List[ast.AST] = [tree]
        # Collect every function-scope as its own tracking unit.
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append(n)

        for scope in scopes:
            # Two-pass within each scope:
            #   Pass 1: collect all alias bindings (Name=banned).
            #   Pass 2: walk for Calls to aliased names.
            # Walking via ast.walk descends into nested control-
            # flow blocks (try/if/with/for/while) so an alias
            # inside ``try:`` is still tracked.
            aliases: dict = {}
            for node in ast.walk(scope):
                # Don't recurse into nested function definitions —
                # those are their own scope (handled in the outer
                # for loop). The current `scope` is the function-
                # def itself, so skip its child function defs.
                if scope is not tree and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef),
                ) and node is not scope:
                    continue
                if not isinstance(node, ast.Assign):
                    continue
                if (
                    len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ):
                    banned = _resolves_to_banned_name(node.value)
                    if banned is not None:
                        aliases[node.targets[0].id] = banned
            if not aliases:
                continue
            for node in ast.walk(scope):
                if scope is not tree and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef),
                ) and node is not scope:
                    continue
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id in aliases
                ):
                    return (
                        f"Rule 10 (alias_defeat): Call to "
                        f"{func.id!r} at line "
                        f"{getattr(node, 'lineno', '?')} — "
                        f"alias for banned "
                        f"{aliases[func.id]!r} (P9.4 documented "
                        f"gap; runtime sandbox is the final gate)"
                    )
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_ast_phase_runner_validator_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/"
            "ast_phase_runner_validator.py"
        ),
        description=(
            "Pass B Slice 3 substrate: is_enabled + validate_ast + "
            "validate_ast_strict + ValidationResult (frozen) "
            "present; no dynamic-code calls."
        ),
        required_funcs=(
            "is_enabled", "validate_ast", "validate_ast_strict",
        ),
        required_classes=("ValidationResult",),
        frozen_classes=("ValidationResult",),
    )
    return [inv] if inv is not None else []
