"""
PolicyEngine — Declarative Permission Rules
============================================

Loads ``deny`` / ``ask`` / ``allow`` rules from two optional YAML policy
files and classifies (tool, target) pairs into one of four decisions:

* **BLOCKED**            — matched by a deny rule; always wins.
* **APPROVAL_REQUIRED**  — matched by an ask rule (and no deny rule).
* **SAFE_AUTO**          — matched by an allow rule (no deny, no ask).
* **NO_MATCH**           — no rule matched; caller decides.

Rule resolution order
---------------------
1. All deny rules are checked first (global + repo combined).
   If *any* deny rule matches -> BLOCKED immediately.
2. All ask rules are checked next.  First match -> APPROVAL_REQUIRED.
3. All allow rules are checked next.  First match -> SAFE_AUTO.
4. Fallthrough -> NO_MATCH.

Repo-level policy is loaded *in addition to* global policy.  Because deny
rules always win, a repo-level deny beats a global-level allow regardless of
the order the files are loaded.

YAML schema
-----------
Each policy file may contain zero or more of the top-level keys
``deny``, ``ask``, and ``allow``.  Each key maps to a list of rule
objects with the following fields:

.. code-block:: yaml

    deny:
      - tool: "*"          # fnmatch pattern matched against the tool name
        pattern: "**/.env*"  # fnmatch pattern matched against the target

    ask:
      - tool: "edit"
        pattern: "backend/core/**"

    allow:
      - tool: "pytest"
        pattern: "*"

Missing files and malformed YAML are silently ignored so that the engine
degrades to NO_MATCH rather than crashing the pipeline.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger("Ouroboros.PolicyEngine")


# ---------------------------------------------------------------------------
# Public API — decision enum
# ---------------------------------------------------------------------------


class PolicyDecision(Enum):
    """Classification produced by :class:`PolicyEngine`."""

    BLOCKED = auto()
    APPROVAL_REQUIRED = auto()
    SAFE_AUTO = auto()
    NO_MATCH = auto()


# ---------------------------------------------------------------------------
# Internal rule representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    """A single resolved policy rule.

    Parameters
    ----------
    tool:
        fnmatch pattern matched against the *tool* argument of
        :meth:`PolicyEngine.classify`.
    pattern:
        fnmatch pattern matched against the *target* argument.
    tier:
        One of ``"deny"``, ``"ask"``, or ``"allow"``.
    """

    tool: str
    pattern: str
    tier: str  # "deny" | "ask" | "allow"


# ---------------------------------------------------------------------------
# PolicyEngine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Load and evaluate declarative permission rules from YAML policy files.

    Parameters
    ----------
    global_root:
        Directory that MAY contain ``.jarvis/policy.yaml`` (the user-global
        policy, typically ``~/.jarvis``).
    repo_root:
        Directory that MAY contain ``.jarvis/policy.yaml`` (the per-repo
        policy override).  When ``None`` only the global policy is loaded.
    """

    def __init__(
        self,
        global_root: Optional[Path] = None,
        repo_root: Optional[Path] = None,
    ) -> None:
        self._deny: List[_Rule] = []
        self._ask: List[_Rule] = []
        self._allow: List[_Rule] = []

        if global_root is not None:
            self._load_policy(Path(global_root))
        if repo_root is not None:
            self._load_policy(Path(repo_root))

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def classify(self, tool: str, target: str) -> PolicyDecision:
        """Classify a (tool, target) pair against the loaded rules.

        Deny rules always win.  Repo-level overrides are already merged
        at construction time (both global and repo deny lists are checked).

        Parameters
        ----------
        tool:
            The tool being invoked (e.g. ``"edit"``, ``"bash"``).
        target:
            The target the tool is acting on (file path, command string, …).

        Returns
        -------
        PolicyDecision
        """
        # 1. Deny wins unconditionally.
        for rule in self._deny:
            if self._matches_rule(rule, tool, target):
                logger.debug(
                    "PolicyEngine BLOCKED tool=%r target=%r by rule tool=%r pattern=%r",
                    tool, target, rule.tool, rule.pattern,
                )
                return PolicyDecision.BLOCKED

        # 2. Ask comes before allow.
        for rule in self._ask:
            if self._matches_rule(rule, tool, target):
                logger.debug(
                    "PolicyEngine APPROVAL_REQUIRED tool=%r target=%r by rule tool=%r pattern=%r",
                    tool, target, rule.tool, rule.pattern,
                )
                return PolicyDecision.APPROVAL_REQUIRED

        # 3. Allow.
        for rule in self._allow:
            if self._matches_rule(rule, tool, target):
                logger.debug(
                    "PolicyEngine SAFE_AUTO tool=%r target=%r by rule tool=%r pattern=%r",
                    tool, target, rule.tool, rule.pattern,
                )
                return PolicyDecision.SAFE_AUTO

        return PolicyDecision.NO_MATCH

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _matches_rule(self, rule: _Rule, tool: str, target: str) -> bool:
        """Return True if *rule* matches the given (tool, target) pair."""
        tool_match = self._matches(tool, rule.tool)
        if not tool_match:
            return False
        return self._matches(target, rule.pattern)

    @staticmethod
    def _matches(value: str, pattern: str) -> bool:
        """fnmatch-based matching with a basename fallback.

        Tries a full-path match first.  If that fails, also tries matching
        only the basename of *value* against *pattern* (so ``**/.env*``
        matches ``.env.local`` without a directory prefix).

        Parameters
        ----------
        value:
            The string to test (file path, command, …).
        pattern:
            The fnmatch/glob pattern.
        """
        if fnmatch.fnmatch(value, pattern):
            return True
        # Basename fallback: useful for patterns like "**/.env*" matching
        # plain ".env.local" as well as "path/to/.env.local".
        basename = Path(value).name
        if basename != value and fnmatch.fnmatch(basename, pattern):
            return True
        # Also match the trailing component of the pattern itself stripped
        # of directory wildcards, e.g. "**/.env*" -> ".env*"
        pattern_name = Path(pattern).name
        if pattern_name and pattern_name != pattern:
            if fnmatch.fnmatch(basename, pattern_name) or fnmatch.fnmatch(value, pattern_name):
                return True
        return False

    def _load_policy(self, root: Path) -> None:
        """Load ``.jarvis/policy.yaml`` under *root* into the rule lists.

        Missing files and YAML parse errors are silently skipped.
        """
        policy_path = root / ".jarvis" / "policy.yaml"
        if not policy_path.exists():
            return

        try:
            import yaml  # type: ignore[import-untyped]
            raw = yaml.safe_load(policy_path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PolicyEngine: failed to load %s (%s); skipping",
                policy_path, exc,
            )
            return

        if not isinstance(raw, dict):
            logger.warning(
                "PolicyEngine: %s does not contain a YAML mapping; skipping",
                policy_path,
            )
            return

        self._deny.extend(self._parse_rules(raw, "deny", policy_path))
        self._ask.extend(self._parse_rules(raw, "ask", policy_path))
        self._allow.extend(self._parse_rules(raw, "allow", policy_path))

    @staticmethod
    def _parse_rules(
        raw: dict,
        tier: str,
        source: Path,
    ) -> List[_Rule]:
        """Extract and validate rules for the given *tier* from *raw*."""
        entries = raw.get(tier)
        if not entries:
            return []
        if not isinstance(entries, list):
            logger.warning(
                "PolicyEngine: '%s' in %s must be a list; skipping",
                tier, source,
            )
            return []

        rules: List[_Rule] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                logger.warning(
                    "PolicyEngine: %s[%d] in %s is not a mapping; skipping",
                    tier, idx, source,
                )
                continue
            tool = entry.get("tool")
            pattern = entry.get("pattern")
            if not tool or not pattern:
                logger.warning(
                    "PolicyEngine: %s[%d] in %s missing 'tool' or 'pattern'; skipping",
                    tier, idx, source,
                )
                continue
            rules.append(_Rule(tool=str(tool), pattern=str(pattern), tier=tier))
        return rules
