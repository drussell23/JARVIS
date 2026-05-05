"""Move 7 — Cross-op Semantic Budget Slice 2 producer-bridge +
flock'd JSONL recorder (PRD §29.4, 2026-05-05).

Composes:

  * **Slice 1** primitive — :class:`OpSemanticCentroid` artifact
    (adopts §33.5 Versioned-Artifact-Contract); this Slice
    only WRITES rows + reads them back; no math.
  * **§33.2 Producer-Bridge Pattern** — orchestrator's COMPLETE
    phase boundary lazy-imports :func:`record_op_centroid` inside
    a try/except. ImportError → silent no-op. Master-flag-off
    → silent no-op. Bridge owns ALL Move 7 policy
    (master-flag check, SemanticIndex resolution, defensive
    exception isolation).
  * **§33.4 Per-Cluster flock'd JSONL Persistence Pattern** —
    appends via :func:`cross_process_jsonl.flock_append_line`.
    Cross-process tear-safe; reader uses
    :func:`flock_critical_section`.
  * **`SemanticIndex.snapshot_global_centroid()`** (added 2026-05-05
    Slice 2 prereq) — pure read-side accessor for the existing
    recency-weighted global centroid. No parallel embedding /
    centroid computation; reuses the canonical substrate.

## Architectural locks (operator mandate, AST-pinned)

  1. **Authority asymmetry** — imports stdlib + Slice 1 primitive
     + ``cross_process_jsonl`` + ``SemanticIndex`` (read-only).
     NEVER imports orchestrator / iron_gate / policy /
     providers / candidate_generator / urgency_router /
     change_engine / semantic_guardian.
  2. **Master-flag-gated at every entry point** — every public
     function calls :func:`cross_op_semantic_budget_enabled` and
     returns immediately when off (Slice 1's flag governs the
     whole arc; no parallel flag).
  3. **NEVER raises** — every function exception-isolated; bridge
     callers ignore return values.
  4. **§33.5 schema-versioned rows** — every JSONL line carries
     ``schema_version`` field via :class:`OpSemanticCentroid`'s
     ``to_dict``; readers verify via
     :func:`meta.versioned_artifact.verify_artifact_schema`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


CROSS_OP_SEMANTIC_RECORDER_SCHEMA_VERSION: str = (
    "cross_op_semantic_recorder.1"
)


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def centroids_jsonl_path() -> Path:
    """``JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH`` — JSONL ledger
    path. Default ``.jarvis/cross_op_semantic_centroids.jsonl``.
    Resolved at call time so tests can override per-fixture."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return (
        Path(".jarvis") / "cross_op_semantic_centroids.jsonl"
    )


def centroids_max_file_bytes() -> int:
    """``JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_MAX_BYTES`` — hard
    cap on JSONL file size. Default 50 MiB. Reader bails on
    files larger than this (defensive — prevents accidental
    pathological growth from blowing memory). Clamped
    [1 MiB, 1 GiB]."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_MAX_BYTES", "",
    ).strip()
    try:
        n = int(raw) if raw else 50 * 1024 * 1024
    except (TypeError, ValueError):
        return 50 * 1024 * 1024
    one_mb = 1024 * 1024
    if n < one_mb:
        return one_mb
    one_gb = 1024 * 1024 * 1024
    if n > one_gb:
        return one_gb
    return n


# ---------------------------------------------------------------------------
# Centroid hashing — bytes-pinned for parity with semantic_index._centroid_hash8
# ---------------------------------------------------------------------------


def compute_centroid_hash(
    centroid: Tuple[float, ...],
) -> str:
    """Sha256[:8] of the centroid for compact identity. Empty
    centroid → empty string (semantically "no centroid", NOT a
    hash collision). Pure stdlib. NEVER raises."""
    if not centroid:
        return ""
    try:
        # Encode as repr-style bytes for deterministic hashing.
        # Format-invariant — round-trip via from_dict produces
        # the same float tuple, so hash matches across processes.
        s = ",".join(repr(float(x)) for x in centroid)
        return hashlib.sha256(
            s.encode("utf-8"),
        ).hexdigest()[:8]
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# Public producer-bridge entry point
# ---------------------------------------------------------------------------


def record_op_centroid(
    op_id: str,
    *,
    ts_unix: Optional[float] = None,
    centroid: Optional[Tuple[float, ...]] = None,
    path: Optional[Path] = None,
) -> bool:
    """Record one op's semantic centroid to the JSONL ledger.

    Producer-bridge entry point — called at the orchestrator's
    COMPLETE phase boundary via lazy import + try/except.

    Parameters:
      * ``op_id`` — stable op identifier for the row
      * ``ts_unix`` — wall-clock timestamp for display (when the
        op completed); defaults to :func:`time.time` (display
        semantic — wall-clock is correct here, NOT monotonic;
        cf. PRD §3.6.2 vector #11 distinction)
      * ``centroid`` — caller-supplied centroid (testing); when
        ``None``, reads the canonical
        :func:`SemanticIndex.snapshot_global_centroid`
      * ``path`` — caller-supplied JSONL path (testing); when
        ``None``, resolves :func:`centroids_jsonl_path`

    Returns ``True`` on successful append, ``False`` on master-
    flag-off / SemanticIndex unavailable / empty-centroid /
    flock failure / any exception. NEVER raises.

    Master-flag-gated: short-circuits on
    :func:`cross_op_semantic_budget_enabled` returning False."""
    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            OpSemanticCentroid,
            cross_op_semantic_budget_enabled,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] Slice 1 primitive "
            "unavailable: %s", exc,
        )
        return False

    if not cross_op_semantic_budget_enabled():
        return False

    op_id_str = str(op_id or "").strip()
    if not op_id_str:
        return False

    # Resolve centroid — caller-injected (testing) or via the
    # canonical SemanticIndex public surface.
    resolved_centroid: Tuple[float, ...]
    if centroid is not None:
        try:
            resolved_centroid = tuple(
                float(x) for x in centroid
            )
        except (TypeError, ValueError):
            return False
    else:
        try:
            from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
                get_default_index,
            )
            idx = get_default_index()
            resolved_centroid = idx.snapshot_global_centroid()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[CrossOpSemanticRecorder] SemanticIndex "
                "unavailable: %s", exc,
            )
            return False

    if not resolved_centroid:
        # Empty centroid → cold-start (index not yet built).
        # Skip silently; not an error.
        return False

    centroid_hash = compute_centroid_hash(resolved_centroid)
    ts = (
        float(ts_unix)
        if ts_unix is not None
        else time.time()
    )

    try:
        artifact = OpSemanticCentroid(
            op_id=op_id_str,
            ts_unix=ts,
            centroid=resolved_centroid,
            centroid_hash=centroid_hash,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] artifact build raised: "
            "%s", exc,
        )
        return False

    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] cross_process_jsonl "
            "unavailable: %s", exc,
        )
        return False

    target = path if path is not None else centroids_jsonl_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    try:
        line = json.dumps(
            artifact.to_dict(),
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] JSON encode raised: "
            "%s", exc,
        )
        return False

    try:
        return bool(flock_append_line(target, line))
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] flock_append_line "
            "raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Reader — read_recent_centroids
# ---------------------------------------------------------------------------


def read_recent_centroids(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> Tuple[Any, ...]:
    """Return up to ``limit`` most-recent
    :class:`OpSemanticCentroid` rows from the JSONL ledger in
    chronological order (oldest first within the limit window).

    NEVER raises. Returns empty tuple on missing file /
    primitive-unavailable / flock-failure / any parse exception.
    Schema verification is delegated to the caller via
    :func:`meta.versioned_artifact.verify_artifact_schema` —
    this reader passes through whatever rows parse, schema
    mismatches surface as ``None`` from
    :meth:`OpSemanticCentroid.from_dict` and are skipped."""
    try:
        bound = max(1, min(int(limit), 10_000))
    except (TypeError, ValueError):
        bound = 50

    target = (
        path if path is not None else centroids_jsonl_path()
    )
    if not target.exists():
        return tuple()

    try:
        from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
            OpSemanticCentroid,
        )
    except Exception:  # noqa: BLE001 — defensive
        return tuple()

    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
    except Exception:  # noqa: BLE001 — defensive
        return tuple()

    cap = centroids_max_file_bytes()

    try:
        with flock_critical_section(target) as acquired:
            if not acquired:
                return tuple()
            try:
                stat = target.stat()
                if stat.st_size > cap:
                    logger.debug(
                        "[CrossOpSemanticRecorder] ledger size "
                        "%d exceeds cap %d — bailing",
                        stat.st_size, cap,
                    )
                    return tuple()
                text = target.read_text(encoding="utf-8")
            except OSError:
                return tuple()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CrossOpSemanticRecorder] reader raised: %s",
            exc,
        )
        return tuple()

    out: list = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
            if not isinstance(row, dict):
                continue
            parsed = OpSemanticCentroid.from_dict(row)
            if parsed is not None:
                out.append(parsed)
        except json.JSONDecodeError:
            continue
        except Exception:  # noqa: BLE001 — defensive
            continue

    if len(out) > bound:
        out = out[-bound:]
    return tuple(out)


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pin: substrate authority asymmetry — recorder MUST NOT
    import orchestrator / iron_gate / policy / providers /
    candidate_generator / urgency_router / change_engine /
    semantic_guardian. Composes Slice 1 primitive +
    cross_process_jsonl + SemanticIndex (read-only) ONLY."""
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"cross_op_semantic_recorder.py "
                            f"MUST NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_slice1(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Recorder MUST compose Slice 1's `OpSemanticCentroid`
        + `cross_op_semantic_budget_enabled` (no parallel
        artifact / flag implementation). And MUST compose
        `cross_process_jsonl.flock_append_line` (no raw
        ``open(... 'a')`` for cross-process JSONL). And MUST
        compose `SemanticIndex.snapshot_global_centroid` (no
        parallel centroid computation)."""
        violations: list = []
        if "OpSemanticCentroid" not in source:
            violations.append(
                "recorder MUST import / use "
                "OpSemanticCentroid from Slice 1 (no parallel "
                "artifact)"
            )
        if "cross_op_semantic_budget_enabled" not in source:
            violations.append(
                "recorder MUST gate every entry point on "
                "cross_op_semantic_budget_enabled "
                "(no parallel flag)"
            )
        if "flock_append_line" not in source:
            violations.append(
                "recorder MUST use flock_append_line per "
                "§33.4 Per-Cluster flock'd JSONL Persistence "
                "Pattern (no raw open-append)"
            )
        if "snapshot_global_centroid" not in source:
            violations.append(
                "recorder MUST read SemanticIndex via "
                "snapshot_global_centroid (no parallel "
                "centroid computation)"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "cross_op_semantic_recorder.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_recorder_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "cross_op_semantic_recorder.py MUST stay pure "
                "substrate composing Slice 1 + cross_process_-"
                "jsonl + SemanticIndex (read-only) ONLY. No "
                "orchestrator / iron_gate / policy / "
                "providers imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_recorder_composes_slice1"
            ),
            target_file=target,
            description=(
                "Recorder composes Slice 1 OpSemanticCentroid "
                "+ cross_op_semantic_budget_enabled gate + "
                "cross_process_jsonl.flock_append_line + "
                "SemanticIndex.snapshot_global_centroid. No "
                "parallel artifacts / flags / locking / "
                "centroid computation."
            ),
            validate=_validate_composes_slice1,
        ),
    ]


__all__ = [
    "CROSS_OP_SEMANTIC_RECORDER_SCHEMA_VERSION",
    "centroids_jsonl_path",
    "centroids_max_file_bytes",
    "compute_centroid_hash",
    "read_recent_centroids",
    "record_op_centroid",
    "register_shipped_invariants",
]
