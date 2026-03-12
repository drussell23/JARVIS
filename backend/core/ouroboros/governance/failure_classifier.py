"""L2 Iterative Self-Repair — Failure Classifier.

Classifies sandbox validation failures into structured categories so that
RepairEngine can choose the appropriate retry strategy and prompt scaffold.

Public API
----------
FailureClass         : str enum — SYNTAX | TEST | ENV | FLAKE
NON_RETRYABLE_ENV_SUBTYPES : frozenset[str]
failure_signature_hash(failing_test_ids, failure_class) -> str
patch_signature_hash(unified_diff) -> str
ClassificationResult : frozen dataclass
FailureClassifier    : stateless classifier; call .classify(svr)
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# Enumerations and constants
# ---------------------------------------------------------------------------


class FailureClass(str, enum.Enum):
    """Mutually exclusive failure categories.

    Note: ``FLAKE`` is never returned by :class:`FailureClassifier` directly.
    Flake detection requires cross-iteration state (same test IDs repeating
    with no patch change). It is assigned by ``RepairEngine`` after
    ``flake_confirm_reruns`` confirmatory reruns, not by this classifier.
    """

    SYNTAX = "syntax"
    TEST = "test"
    ENV = "env"
    FLAKE = "flake"


NON_RETRYABLE_ENV_SUBTYPES: frozenset[str] = frozenset(
    {
        "missing_dependency",
        "interpreter_mismatch",
        "permission_denied",
        "port_conflict",
    }
)

# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

_FAILED_RE = re.compile(
    r"^FAILED\s+([\w/.\-:\[\]]+(?:::[^\s]+)?)",
    re.MULTILINE,
)


def failure_signature_hash(
    failing_test_ids: Iterable[str],
    failure_class: str,
) -> str:
    """Return a stable 64-char SHA-256 hex digest for a failure fingerprint.

    The digest is order-independent: IDs are sorted before hashing.

    Parameters
    ----------
    failing_test_ids:
        Collection of test node IDs (may be empty).
    failure_class:
        One of "syntax", "test", "env", "flake".

    Returns
    -------
    str
        64-character lowercase hex digest.
    """
    ids_part = "|".join(sorted(failing_test_ids))
    payload = f"{ids_part}:{failure_class}".encode()
    return hashlib.sha256(payload).hexdigest()


def patch_signature_hash(unified_diff: str) -> str:
    """Return a stable 64-char SHA-256 hex digest for a unified diff string.

    Parameters
    ----------
    unified_diff:
        Full text of a unified diff.

    Returns
    -------
    str
        64-character lowercase hex digest.
    """
    return hashlib.sha256(unified_diff.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ClassificationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Immutable output of :class:`FailureClassifier`.

    Attributes
    ----------
    failure_class:
        High-level failure category.
    env_subtype:
        Set only when *failure_class* is ``ENV``. Examples:
        ``"missing_dependency"``, ``"permission_denied"``,
        ``"port_conflict"``, ``"interpreter_mismatch"``.
    is_non_retryable:
        ``True`` when the failure cannot be fixed by regenerating code
        (e.g. missing dependency, permission error).
    failing_test_ids:
        Up to 5 pytest node IDs extracted from stdout. Empty for
        non-TEST classes.
    failure_signature_hash:
        64-char SHA-256 hex digest — stable fingerprint for deduplication
        and loop-termination checks.
    """

    failure_class: FailureClass
    env_subtype: Optional[str]
    is_non_retryable: bool
    failing_test_ids: Tuple[str, ...]
    failure_signature_hash: str


# ---------------------------------------------------------------------------
# FailureClassifier
# ---------------------------------------------------------------------------


class FailureClassifier:
    """Stateless classifier for sandbox validation failures.

    Classify in priority order:
    1. ENV   — environment problems that code changes cannot fix.
    2. SYNTAX — Python parse/indent errors.
    3. TEST   — pytest FAILED lines extracted from stdout.
    4. TEST (fallback) — any other non-zero exit.

    Usage
    -----
    ::

        clf = FailureClassifier()
        result = clf.classify(sandbox_validation_result)
    """

    # ENV signal patterns: (pattern, subtype)
    _ENV_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(r"ModuleNotFoundError|No module named", re.IGNORECASE),
            "missing_dependency",
        ),
        (
            re.compile(r"PermissionError|Permission denied", re.IGNORECASE),
            "permission_denied",
        ),
        (
            re.compile(r"address already in use|port.*in use", re.IGNORECASE),
            "port_conflict",
        ),
        (
            re.compile(r"interpreter mismatch", re.IGNORECASE),
            "interpreter_mismatch",
        ),
    )

    _SYNTAX_RE = re.compile(r"SyntaxError|IndentationError", re.IGNORECASE)

    def classify(self, svr: object) -> ClassificationResult:
        """Classify a sandbox validation result.

        Parameters
        ----------
        svr:
            Any object exposing ``stdout: str``, ``stderr: str``,
            ``returncode: int``, and ``passed: bool`` attributes.
            Compatible with both ``SandboxValidationResult`` and test stubs.

        Returns
        -------
        ClassificationResult
            Immutable classification with populated hash and metadata.
        """
        stdout: str = getattr(svr, "stdout", "") or ""
        stderr: str = getattr(svr, "stderr", "") or ""
        combined = stdout + "\n" + stderr

        # ------------------------------------------------------------------
        # Priority 1: ENV
        # ------------------------------------------------------------------
        for pattern, subtype in self._ENV_PATTERNS:
            if pattern.search(combined):
                sig = failure_signature_hash((), "env")
                return ClassificationResult(
                    failure_class=FailureClass.ENV,
                    env_subtype=subtype,
                    is_non_retryable=subtype in NON_RETRYABLE_ENV_SUBTYPES,
                    failing_test_ids=(),
                    failure_signature_hash=sig,
                )

        # ------------------------------------------------------------------
        # Priority 2: SYNTAX
        # ------------------------------------------------------------------
        if self._SYNTAX_RE.search(combined):
            sig = failure_signature_hash((), "syntax")
            return ClassificationResult(
                failure_class=FailureClass.SYNTAX,
                env_subtype=None,
                is_non_retryable=False,
                failing_test_ids=(),
                failure_signature_hash=sig,
            )

        # ------------------------------------------------------------------
        # Priority 3: TEST — extract FAILED lines from stdout
        # ------------------------------------------------------------------
        raw_ids = _FAILED_RE.findall(stdout)
        if raw_ids:
            top5: Tuple[str, ...] = tuple(raw_ids[:5])
            sig = failure_signature_hash(top5, "test")
            return ClassificationResult(
                failure_class=FailureClass.TEST,
                env_subtype=None,
                is_non_retryable=False,
                failing_test_ids=top5,
                failure_signature_hash=sig,
            )

        # ------------------------------------------------------------------
        # Priority 4: TEST fallback
        # ------------------------------------------------------------------
        sig = failure_signature_hash((), "test")
        return ClassificationResult(
            failure_class=FailureClass.TEST,
            env_subtype=None,
            is_non_retryable=False,
            failing_test_ids=(),
            failure_signature_hash=sig,
        )
