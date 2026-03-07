"""
Operation Identity System
=========================

Every autonomous Ouroboros operation receives a globally unique, time-sortable
identifier based on UUIDv7 (RFC 9562).  The format is::

    op-<uuidv7>-<repo_origin>

UUIDv7 embeds a Unix-epoch millisecond timestamp in the most significant 48
bits, guaranteeing monotonic sort order when IDs are generated sequentially.

Companion dataclass :class:`OperationMetadata` bundles the ID with policy
version and a deterministic SHA-256 hash of the decision inputs so that every
decision can be audited and replayed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import uuid6


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_operation_id(repo_origin: str = "jarvis") -> str:
    """Return a globally unique, time-sortable operation identifier.

    Format: ``op-<uuidv7>-<repo_origin>``

    Parameters
    ----------
    repo_origin:
        Short label identifying the source repository (e.g. ``"jarvis"``,
        ``"prime"``, ``"reactor"``).

    Returns
    -------
    str
        A unique operation ID that sorts chronologically by string comparison.
    """
    return f"op-{uuid6.uuid7()}-{repo_origin}"


def _hash_inputs(inputs: Dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hex digest of *inputs*.

    Keys are sorted and non-serialisable values are coerced to ``str`` via
    ``json.dumps(..., sort_keys=True, default=str)``.
    """
    canonical = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# OperationMetadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationMetadata:
    """Immutable record tying an operation ID to its policy context.

    Parameters
    ----------
    op_id:
        The ``op-<uuidv7>-<origin>`` identifier returned by
        :func:`generate_operation_id`.
    policy_version:
        Semantic version string of the governance policy that was active when
        this operation was evaluated.
    decision_inputs:
        Arbitrary dict capturing every input that fed into the risk /
        classification decision.  Hashed deterministically into
        ``decision_inputs_hash``.
    model_metadata_hash:
        Optional hash of model metadata (weights version, prompt hash, etc.)
        for full reproducibility.
    """

    op_id: str
    policy_version: str
    decision_inputs: Dict[str, Any]
    model_metadata_hash: Optional[str] = None

    # Computed after __init__ via __post_init__
    decision_inputs_hash: str = field(init=False, repr=True)

    def __post_init__(self) -> None:
        # frozen=True requires object.__setattr__ for post-init assignment
        object.__setattr__(
            self,
            "decision_inputs_hash",
            _hash_inputs(self.decision_inputs),
        )

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def is_new(self, seen_ids: Set[str]) -> bool:
        """Return ``True`` if this operation has not been recorded yet.

        Parameters
        ----------
        seen_ids:
            A set of previously observed operation IDs.

        Returns
        -------
        bool
            ``True`` when ``self.op_id`` is absent from *seen_ids*.
        """
        return self.op_id not in seen_ids
