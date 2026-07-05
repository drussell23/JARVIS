"""Isolated pure-leaf numeric predicates -- the A1 ignition target surface.

Every function here is a STRUCTURALLY PURE LEAF (operators only, no calls, no
I/O, no globals) so the AST chaos injector can select and mutate one, and each
is exhaustively exercised by ``tests/.../test_leaf_predicates.py`` (coverage
1.0). Nothing in the production graph imports this module (blast radius ~1).

Do not add imports of this module elsewhere -- its isolation is load-bearing:
it is what lets a synthetic failure here clear the OperationAdvisor gate
honestly (blast_radius=1 + coverage=1.0), never by relaxing the gate.
"""
from __future__ import annotations


def is_even(n: int) -> bool:
    """Return True iff ``n`` is even. Pure leaf: modulo + comparison, no calls."""
    return n % 2 == 0


def sign(n: int) -> int:
    """Return ``-1``, ``0`` or ``1`` for negative, zero, positive ``n``."""
    if n > 0:
        return 1
    if n < 0:
        return -1
    return 0


def in_closed_range(x: int, low: int, high: int) -> bool:
    """Return True iff ``low <= x <= high`` (inclusive on both ends)."""
    return low <= x <= high


def clamp01(x: int) -> int:
    """Clamp ``x`` into the closed interval ``[0, 1]`` (operators only)."""
    if x < 0:
        return 0
    if x > 1:
        return 1
    return x
