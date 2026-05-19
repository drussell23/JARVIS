"""CursorRuleGuard — Layer-1 durability pin for the Agent git-write ban.

The `.cursor/rules/no-agent-git-write.mdc` rule (Layer 1: prevention)
is advisory — a Cursor Agent can ignore it, and a contributor (or an
Agent) could silently delete or hollow it. **A rule that can vanish
is no defense.** This module makes the rule's existence,
non-emptiness, `alwaysApply: true`, and its load-bearing prohibition
tokens **structurally permanent**: an auto-discovered shipped-code
invariant (composed via :mod:`meta.shipped_code_invariants`,
discovered by ``module_discovery`` through the
``register_shipped_invariants`` name — zero registry edits) goes RED
in meta-validation / CI if the rule is missing, empty, deactivated,
or gutted of any load-bearing prohibition.

Zero-trust framing (deliberate): this pin does NOT make the advisory
into a gate. Layer 2 (the OCA sovereign gate, ``denied_sovereignty``)
is the structural load-bearer and is **not touched here**. This is
only the durability guarantee for the Layer-1 intent signal.

Pure stdlib + filesystem read. NEVER raises; ImportError-tolerant.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("Ouroboros.CursorRuleGuard")


CURSOR_RULE_GUARD_SCHEMA_VERSION: str = "cursor_rule_guard.v1"

# Repo-relative location of the Layer-1 rule.
RULE_RELPATH: Tuple[str, ...] = (
    ".cursor", "rules", "no-agent-git-write.mdc",
)

# Load-bearing semantic tokens (lower-cased substring match). Their
# presence proves the rule still carries its prohibition + redirect
# + the don't-delete rationale. NOT a single rigid string — a set of
# independent semantic anchors; gutting any one fails the pin.
_REQUIRED_TOKENS: Tuple[str, ...] = (
    "git commit",          # the core prohibited verb family
    "git push",
    "git reset",
    "git add",
    "background",          # scoped to background / autonomous agents
    "agent",
    "worktree",            # the sanctioned redirect for autonomous git
    "commit-authority",    # the operator-driven sanctioned path
    "operator",            # operator holds commit authority
)

# The rule must be ACTIVE, not a dormant file.
_ALWAYS_APPLY_TOKEN = "alwaysapply: true"


def _repo_root() -> Path:
    """Resolve the repo root from this module's fixed location:
    ``backend/core/ouroboros/governance/cursor_rule_guard.py`` →
    ``parents[4]`` is the repo root. No subprocess, no hardcoded
    absolute path. NEVER raises."""
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:  # noqa: BLE001
        return Path(".").resolve()


def cursor_rule_path() -> Path:
    """Absolute path of the Layer-1 rule. NEVER raises."""
    try:
        return _repo_root().joinpath(*RULE_RELPATH)
    except Exception:  # noqa: BLE001
        return Path(*RULE_RELPATH)


def evaluate_rule() -> Tuple[str, ...]:
    """Return a tuple of violation strings (empty == healthy).
    Pure, NEVER raises. Reusable by the AST pin AND by
    ``scripts/verify_oca.py`` for a read-only presence check."""
    violations = []
    try:
        path = cursor_rule_path()
        if not path.exists():
            return (
                f"Layer-1 rule MISSING at {('/'.join(RULE_RELPATH))} "
                "— the Agent git-write ban has been deleted",
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (f"Layer-1 rule unreadable: {exc}",)
        if not text.strip():
            return ("Layer-1 rule is EMPTY (gutted)",)
        low = text.lower()
        if _ALWAYS_APPLY_TOKEN not in low.replace(" ", " "):
            # tolerate arbitrary whitespace around the colon/value
            import re as _re
            if not _re.search(
                r"alwaysapply\s*:\s*true", low,
            ):
                violations.append(
                    "Layer-1 rule is not active "
                    "(`alwaysApply: true` absent in frontmatter) "
                    "— a dormant rule is no prevention"
                )
        missing = [t for t in _REQUIRED_TOKENS if t not in low]
        if missing:
            violations.append(
                "Layer-1 rule gutted — missing load-bearing "
                f"prohibition/redirect tokens: {sorted(missing)}"
            )
        return tuple(violations)
    except Exception as exc:  # noqa: BLE001 — pin must never raise
        logger.debug("[CursorRuleGuard] evaluate degraded: %s", exc)
        # Fail-closed for a security pin: an unexplained failure to
        # evaluate the ban is itself a violation.
        return (f"Layer-1 rule evaluation failed defensively: "
                f"{type(exc).__name__}",)


def register_shipped_invariants() -> list:
    """Auto-discovered. One invariant whose ``validate`` ignores the
    (Python) AST of this module and instead asserts the on-disk
    ``.cursor/rules`` Layer-1 rule is present, active, and
    un-gutted. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree, source) -> tuple:  # noqa: ANN001
        # The rule is a policy file, not Python — the durability
        # check is a filesystem assertion, not an AST walk. The
        # invariant is targeted at this module so it is always
        # evaluated, but the load-bearing check is evaluate_rule().
        del tree, source
        return evaluate_rule()

    return [
        ShippedCodeInvariant(
            invariant_name="cursor_agent_git_write_ban_present",
            target_file=(
                "backend/core/ouroboros/governance/"
                "cursor_rule_guard.py"
            ),
            description=(
                "The Layer-1 .cursor/rules/no-agent-git-write.mdc "
                "exists, is non-empty, is active "
                "(alwaysApply: true), and retains every "
                "load-bearing prohibition/redirect token. A "
                "deleted or gutted Agent git-write ban fails CI — "
                "the advisory cannot silently vanish."
            ),
            validate=_validate,
        ),
    ]


__all__ = [
    "CURSOR_RULE_GUARD_SCHEMA_VERSION",
    "RULE_RELPATH",
    "cursor_rule_path",
    "evaluate_rule",
    "register_shipped_invariants",
]
