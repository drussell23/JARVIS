"""Shared invariant helper for the Pass B substrate.

Reusable AST-walk + ShippedCodeInvariant constructor. Eliminates the
~80 LOC of duplicated AST validation code that would otherwise live
in each of the 8 Pass B modules' ``register_shipped_invariants()``.

Single source of truth for the Pass B substrate-pin pattern:

  * Required functions present.
  * Required classes present.
  * Designated dataclasses stay ``@dataclass(frozen=True)``.
  * No dynamic-code calls (``exec`` / ``eval`` / ``compile``)
    anywhere in the module body.
  * Optional: ``locked_truthy_env_returns`` enforces that a named
    helper function returns a truthy default at module load time
    (used by ``order2_review_queue.amendment_requires_operator``
    cage invariant).

Authority invariant: this module imports only ``ast`` and the
``ShippedCodeInvariant`` registration contract. Zero authority over
risk tier, route, gate, policy, FORBIDDEN_PATH, or approval.
"""
from __future__ import annotations

import ast
import logging
from typing import Any, Optional, Sequence


logger = logging.getLogger(__name__)


_BANNED_DYNAMIC_BUILTINS = ("exec", "eval", "compile")


def make_pass_b_substrate_invariant(
    *,
    invariant_name: str,
    target_file: str,
    description: str,
    required_funcs: Sequence[str] = (),
    required_classes: Sequence[str] = (),
    frozen_classes: Sequence[str] = (),
    forbid_dynamic_builtins: bool = True,
) -> Optional[Any]:
    """Construct a ShippedCodeInvariant for a Pass B module.

    Returns ``None`` only if the registration contract is unavailable
    (matches the standard graceful-degrade pattern across all
    register_shipped_invariants helpers).
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return None

    required_funcs_t = tuple(required_funcs)
    required_classes_t = tuple(required_classes)
    frozen_classes_t = tuple(frozen_classes)

    def _validate(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        seen_funcs: set = set()
        seen_classes: set = set()
        frozen_status: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, ast.AsyncFunctionDef):
                seen_funcs.add(node.name)
            elif isinstance(node, ast.ClassDef):
                seen_classes.add(node.name)
                if node.name in frozen_classes_t:
                    is_frozen = False
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call):
                            for kw in dec.keywords:
                                if (
                                    kw.arg == "frozen"
                                    and isinstance(kw.value, ast.Constant)
                                    and kw.value.value is True
                                ):
                                    is_frozen = True
                                    break
                    frozen_status[node.name] = is_frozen
            elif forbid_dynamic_builtins and isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in _BANNED_DYNAMIC_BUILTINS:
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"{invariant_name} MUST NOT call "
                            f"{node.func.id}"
                        )
        for fn in required_funcs_t:
            if fn not in seen_funcs:
                violations.append(f"missing function {fn!r}")
        for cls in required_classes_t:
            if cls not in seen_classes:
                violations.append(f"missing class {cls!r}")
        for cls in frozen_classes_t:
            if not frozen_status.get(cls, False):
                violations.append(
                    f"{cls} dataclass MUST stay "
                    "@dataclass(frozen=True)"
                )
        return tuple(violations)

    return ShippedCodeInvariant(
        invariant_name=invariant_name,
        target_file=target_file,
        description=description,
        validate=_validate,
    )


def make_locked_truthy_env_invariant(
    *,
    invariant_name: str,
    target_file: str,
    description: str,
    helper_function_name: str,
    env_var_name: str,
) -> Optional[Any]:
    """Construct a cross-file invariant locking the named helper to
    return truthy when its env var is unset.

    This is the Pass B cost-contract cage pattern: e.g.,
    ``order2_review_queue.amendment_requires_operator()`` MUST return
    True when the env var is unset. The pin AST-validates the
    function body to ensure the default-truthy contract isn't
    quietly inverted across edits.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return None

    def _validate(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        helper_node: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == helper_function_name
            ):
                helper_node = node
                break
        if helper_node is None:
            violations.append(
                f"missing helper function {helper_function_name!r}"
            )
            return tuple(violations)
        # Env-var presence: scan the WHOLE module tree, not just the
        # function body. Pass B modules typically lift env-var names
        # into module-level constants (e.g.,
        # ``_AMENDMENT_INVARIANT_ENV = "JARVIS_..."``) so the
        # function body references a Name, not the literal string.
        env_seen = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == env_var_name
            ):
                env_seen = True
                break
        # Truthy-default: only inside the function body (the cage is
        # this specific helper, not the whole module). Accept either
        # a truthy string literal default OR a ``return True``
        # short-circuit anywhere in the body.
        truthy_default_seen = False
        for sub in ast.walk(helper_node):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and sub.value.strip().lower() in (
                    "1", "true", "yes", "on",
                )
            ):
                truthy_default_seen = True
                break
            if (
                isinstance(sub, ast.Return)
                and isinstance(sub.value, ast.Constant)
                and sub.value.value is True
            ):
                truthy_default_seen = True
                break
        if not env_seen:
            violations.append(
                f"{helper_function_name} module MUST reference env "
                f"var {env_var_name!r}"
            )
        if not truthy_default_seen:
            violations.append(
                f"{helper_function_name} MUST default to truthy "
                "when the env var is unset (cost-contract cage)"
            )
        return tuple(violations)

    return ShippedCodeInvariant(
        invariant_name=invariant_name,
        target_file=target_file,
        description=description,
        validate=_validate,
    )


__all__ = [
    "make_pass_b_substrate_invariant",
    "make_locked_truthy_env_invariant",
]
