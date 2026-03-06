"""Ownership enforcement layer for the reactive state store -- stdlib only.

Provides ``OwnershipRule`` (frozen dataclass) for declaring which writer
domain owns a key prefix, and ``OwnershipRegistry`` for registering rules,
resolving ownership via longest-prefix match, and detecting ambiguities.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only.
* ``OwnershipRule`` is ``@dataclass(frozen=True)`` (immutable value object).
* Longest-prefix match: iterate all rules, track best match by ``len(prefix)``.
* ``validate_no_ambiguous_overlaps`` uses ``collections.defaultdict`` for
  detecting duplicate prefixes with different owners.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import List, Optional, Set


# -- Ownership Rule ----------------------------------------------------------


@dataclass(frozen=True)
class OwnershipRule:
    """Declares that keys starting with ``key_prefix`` are owned by ``writer_domain``.

    Attributes
    ----------
    key_prefix:
        Dotted prefix (e.g. ``"gcp."``).  A key matches if it starts with
        this prefix.
    writer_domain:
        Logical identity of the component allowed to write keys under this
        prefix (e.g. ``"gcp_controller"``).
    description:
        Human-readable explanation of why this ownership mapping exists.
    """

    key_prefix: str
    writer_domain: str
    description: str


# -- Ownership Registry ------------------------------------------------------


class OwnershipRegistry:
    """Registry of ownership rules with longest-prefix resolution.

    Rules are registered during startup and the registry is then frozen
    to prevent accidental mutations at runtime.

    Resolution strategy
    -------------------
    For a given key, the registry iterates **all** rules and selects the
    one whose ``key_prefix`` is the longest match (i.e. longest prefix
    that the key starts with).  This allows fine-grained overrides --
    e.g. ``"gcp.node."`` can have a different owner than ``"gcp."``.
    """

    def __init__(self) -> None:
        self._rules: List[OwnershipRule] = []
        self._frozen: bool = False

    # -- Mutation -----------------------------------------------------------

    def register(self, rule: OwnershipRule) -> None:
        """Add a new ownership rule.

        Raises
        ------
        RuntimeError
            If the registry has been frozen via :meth:`freeze`.
        """
        if self._frozen:
            raise RuntimeError(
                "OwnershipRegistry is frozen -- cannot register new rules"
            )
        self._rules.append(rule)

    def freeze(self) -> None:
        """Prevent further registration of rules."""
        self._frozen = True

    # -- Query --------------------------------------------------------------

    def resolve_owner(self, key: str) -> Optional[str]:
        """Return the ``writer_domain`` for the longest matching prefix.

        Returns ``None`` if no registered prefix matches *key*.
        """
        best_prefix_len: int = -1
        best_writer: Optional[str] = None

        for rule in self._rules:
            if key.startswith(rule.key_prefix):
                prefix_len = len(rule.key_prefix)
                if prefix_len > best_prefix_len:
                    best_prefix_len = prefix_len
                    best_writer = rule.writer_domain

        return best_writer

    def check_ownership(self, key: str, writer: str) -> bool:
        """Return ``True`` if *writer* is the declared owner of *key*.

        Returns ``False`` for undeclared keys (no matching prefix) as well
        as for mismatched writers.
        """
        owner = self.resolve_owner(key)
        if owner is None:
            return False
        return owner == writer

    # -- Diagnostics --------------------------------------------------------

    def validate_no_ambiguous_overlaps(self) -> List[str]:
        """Detect duplicate prefixes registered with different owners.

        Returns a list of human-readable error strings.  An empty list
        means no ambiguous overlaps were found.
        """
        prefix_to_writers: dict[str, set[str]] = collections.defaultdict(set)
        for rule in self._rules:
            prefix_to_writers[rule.key_prefix].add(rule.writer_domain)

        errors: List[str] = []
        for prefix, writers in sorted(prefix_to_writers.items()):
            if len(writers) > 1:
                writers_str = ", ".join(sorted(writers))
                errors.append(
                    f"Ambiguous ownership for prefix '{prefix}': "
                    f"claimed by [{writers_str}]"
                )
        return errors

    def all_prefixes(self) -> Set[str]:
        """Return the set of all registered key prefixes."""
        return {rule.key_prefix for rule in self._rules}
