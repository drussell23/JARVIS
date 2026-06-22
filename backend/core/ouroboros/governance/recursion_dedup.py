"""Task B4 — Semantic DAG de-dup: bounded attempt ledger + active-plan cross-check.

Prevents recursive chunking from cycling or flooding the queue by hashing each
candidate sub-goal and discarding duplicates / already-attempted ones.

Public API
----------
subgoal_hash(scoped_targets, description) -> str
    Stable, scope-sensitive SHA-256 hex digest.

class AttemptLedger
    Bounded FIFO of seen hashes.  Bound set via JARVIS_RECURSION_LEDGER_SIZE
    (default 512).

is_duplicate(h, ledger, active_plan_hashes) -> bool
    True if h already appears in the ledger OR the active-plan set.
"""
from __future__ import annotations

import collections
import hashlib
import os


# ---------------------------------------------------------------------------
# Stable, scope-sensitive hash
# ---------------------------------------------------------------------------

def subgoal_hash(scoped_targets: tuple[str, ...], description: str) -> str:  # noqa: ANN001
    """Return a stable SHA-256 hex digest for a sub-goal.

    Normalization:
    - ``scoped_targets`` is sorted so order doesn't matter.
    - ``description`` is lower-cased and stripped.
    - The two parts are joined by a NUL byte so neither can spill into the
      other.

    Never raises: bad / None inputs fall back to hashing ``repr()``.
    """
    try:
        sorted_targets = sorted(scoped_targets)
        normalized_desc = description.lower().strip()
        payload = "\n".join(sorted_targets) + "\x00" + normalized_desc
        return hashlib.sha256(payload.encode()).hexdigest()
    except Exception:  # pragma: no cover  # fail-soft
        return hashlib.sha256(repr((scoped_targets, description)).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Bounded FIFO ledger
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER_SIZE = 512
_ENV_KEY = "JARVIS_RECURSION_LEDGER_SIZE"


class AttemptLedger:
    """Bounded FIFO of hashes seen so far in this chunking session.

    Uses a ``collections.deque(maxlen=N)`` for O(1) append+eviction and a
    shadow ``set`` for O(1) membership tests.  When the deque evicts the
    oldest entry the set is lazily rebuilt only if it goes out of sync (the
    normal path is that we never need to rebuild because duplicates in the
    deque are harmless — only the set check matters for correctness).

    Env ``JARVIS_RECURSION_LEDGER_SIZE`` controls the bound (default 512).
    """

    def __init__(self) -> None:
        size = int(os.environ.get(_ENV_KEY, _DEFAULT_LEDGER_SIZE))
        self.maxlen: int = size
        self._deque: collections.deque[str] = collections.deque(maxlen=size)
        self._seen_set: set[str] = set()

    # ------------------------------------------------------------------
    def seen(self, h: str) -> bool:
        """Return True if *h* has been marked previously."""
        return h in self._seen_set

    def mark(self, h: str) -> None:
        """Record *h* as attempted.

        When the deque is full the oldest entry is silently evicted.  After
        eviction the shadow set is rebuilt from the deque so it stays in sync.
        """
        if len(self._deque) == self.maxlen:
            # About to evict — rebuild set from deque after append.
            self._deque.append(h)
            self._seen_set = set(self._deque)
        else:
            self._deque.append(h)
            self._seen_set.add(h)


# ---------------------------------------------------------------------------
# Duplicate predicate
# ---------------------------------------------------------------------------

def is_duplicate(
    h: str,
    ledger: AttemptLedger,
    active_plan_hashes: frozenset[str],
) -> bool:
    """Return True if *h* is already known — either in the ledger or the
    active plan's current hash-set.

    Both checks are O(1).
    """
    return ledger.seen(h) or h in active_plan_hashes
