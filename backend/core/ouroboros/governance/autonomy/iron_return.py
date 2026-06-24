"""iron_return — the Cryptographic Artifact Handoff (Phase 1b).

THE IRON RETURN RULE: the Fleet Commander ingests ONLY the artifact produced
here. A worker's scratchpad reasoning, failed tool attempts, and intermediate
messages (the contents of its ``EphemeralMemorySandbox``) are PERMANENTLY
EXCLUDED from the parent's context — they live only in the sandbox, which is
vaporized the instant the worker terminates.

The artifact is a strict JSON-serializable dict::

    {
      "schema_version": "iron_return.1b",
      "status":   "VERIFIED" | "FAILED" | "CANCELLED" | "NOOP",
      "payload":  <the verified patch as repo_patch_to_dict, OR a bounded
                   summary for a non-VERIFIED result>,
      "diff_hash": "<sha256 of the canonical patch — the cryptographic
                    fingerprint of the returned diff>",
      "unit_id":  "<unit>",
      "repo":     "<repo>",
    }

``diff_hash`` is computed over the **canonical** serialization of the patch
(``repo_patch_to_dict`` -> deterministic ``json.dumps(sort_keys=True)``), so an
identical diff always yields an identical fingerprint and any tampering with the
payload makes :func:`verify_artifact` fail (fail-CLOSED).

Reuse: the patch is serialized via the EXISTING
``subagent_types.repo_patch_to_dict`` — no reimplementation. The artifact wraps
``WorkUnitResult``; only the verified patch (or a bounded summary) crosses.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    WorkUnitResult,
    WorkUnitState,
    repo_patch_to_dict,
)

logger = logging.getLogger(__name__)

IRON_RETURN_SCHEMA_VERSION = "iron_return.1b"

# Canonical fingerprint of "no diff" — an empty-payload result. Stable across
# processes so a NOOP / FAILED artifact still has a well-defined diff_hash.
_EMPTY_PAYLOAD_CANON = "null"


def _canonical_json(value: Any) -> str:
    """Deterministic JSON serialization (sorted keys, tight separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _diff_hash_for_payload(payload: Optional[Dict[str, Any]]) -> str:
    """sha256 over the canonical patch payload.

    ``None`` payload (a non-VERIFIED summary or a no-patch result) hashes the
    canonical ``"null"`` so the field is always present and verifiable.
    """
    if payload is None:
        canon = _EMPTY_PAYLOAD_CANON
    else:
        canon = _canonical_json(payload)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _status_token(state: WorkUnitState, *, has_patch: bool) -> str:
    """Map a WorkUnitState to the artifact status token.

    A COMPLETED result WITH a non-empty patch -> VERIFIED. A COMPLETED result
    with an empty/absent patch -> NOOP (nothing crosses but the work is done).
    """
    if state is WorkUnitState.COMPLETED:
        return "VERIFIED" if has_patch else "NOOP"
    if state is WorkUnitState.CANCELLED:
        return "CANCELLED"
    return "FAILED"


def _bounded_summary(result: WorkUnitResult, *, max_len: int = 512) -> Dict[str, Any]:
    """A bounded, scratchpad-free summary for a non-VERIFIED result.

    Carries ONLY terminal-status metadata — never the worker's intermediate
    messages or tool transcript. Truncated to ``max_len`` so a verbose error
    cannot smuggle scratchpad-sized context across the boundary.
    """
    error = str(result.error or "")
    if len(error) > max_len:
        error = error[:max_len] + "...[truncated]"
    return {
        "kind": "summary",
        "failure_class": str(result.failure_class or ""),
        "error": error,
    }


def build_iron_artifact(result: WorkUnitResult) -> Dict[str, Any]:
    """Build the strict Iron Return artifact from a ``WorkUnitResult``.

    The ONLY thing that crosses to the Fleet Commander. For a VERIFIED result
    the payload is the canonical patch (``repo_patch_to_dict`` — reused, no
    reimplementation); for any non-VERIFIED result the payload is a bounded,
    scratchpad-free summary. ``diff_hash`` is the sha256 fingerprint of the
    canonical payload.

    Never raises for a well-formed ``WorkUnitResult``.
    """
    patch = result.patch
    has_patch = patch is not None and bool(getattr(patch, "files", ()) or getattr(patch, "new_content", ()))
    status = _status_token(result.status, has_patch=has_patch)

    if status == "VERIFIED":
        # Only the verified patch crosses — serialized via the EXISTING helper.
        payload: Optional[Dict[str, Any]] = repo_patch_to_dict(patch)  # type: ignore[arg-type]
    else:
        payload = _bounded_summary(result)

    artifact = {
        "schema_version": IRON_RETURN_SCHEMA_VERSION,
        "status": status,
        "payload": payload,
        "diff_hash": _diff_hash_for_payload(payload if status == "VERIFIED" else None),
        "unit_id": str(result.unit_id),
        "repo": str(result.repo),
    }
    logger.info(
        "[IronReturn] built artifact unit=%s repo=%s status=%s diff_hash=%s",
        artifact["unit_id"], artifact["repo"], status, artifact["diff_hash"][:12],
    )
    return artifact


def verify_artifact(artifact: Any) -> bool:
    """Fail-CLOSED verification of an Iron Return artifact.

    Returns True ONLY when:
      * ``artifact`` is a dict with the expected schema_version,
      * ``status == "VERIFIED"``, and
      * ``diff_hash`` matches the recomputed sha256 of the canonical payload.

    A tampered payload, a mismatched hash, a missing field, a non-VERIFIED
    status, or any malformed input -> False. The Commander rejects anything
    that does not verify.
    """
    if not isinstance(artifact, dict):
        return False
    if artifact.get("schema_version") != IRON_RETURN_SCHEMA_VERSION:
        return False
    if artifact.get("status") != "VERIFIED":
        return False
    if "diff_hash" not in artifact or "payload" not in artifact:
        return False

    payload = artifact.get("payload")
    claimed = artifact.get("diff_hash")
    if not isinstance(claimed, str) or not claimed:
        return False

    try:
        recomputed = _diff_hash_for_payload(payload)
    except Exception:  # noqa: BLE001 — any serialization failure fails CLOSED
        logger.warning("[IronReturn] verify: payload not serializable -> REJECT")
        return False

    if recomputed != claimed:
        logger.warning(
            "[IronReturn] verify: diff_hash mismatch unit=%s (claimed=%s real=%s) -> REJECT",
            artifact.get("unit_id"), str(claimed)[:12], recomputed[:12],
        )
        return False
    return True
