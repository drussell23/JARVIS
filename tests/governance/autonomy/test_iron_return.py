"""Phase 1b — Iron Return: cryptographic artifact handoff, fail-CLOSED verify."""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.autonomy.iron_return import (
    IRON_RETURN_SCHEMA_VERSION,
    build_iron_artifact,
    verify_artifact,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    WorkUnitResult,
    WorkUnitState,
    repo_patch_to_dict,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)


def _patch(repo="jarvis", path="jarvis/a.py", content=b"# hi\n"):
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=path, op=FileOp.CREATE, preimage=None),),
        new_content=((path, content),),
    )


def _result(status=WorkUnitState.COMPLETED, patch=None, error="", failure_class=""):
    now = time.monotonic_ns()
    return WorkUnitResult(
        unit_id="u1",
        repo="jarvis",
        status=status,
        patch=patch,
        attempt_count=1,
        started_at_ns=now,
        finished_at_ns=now,
        failure_class=failure_class,
        error=error,
    )


# --- build: status / payload / diff_hash -----------------------------------


def test_verified_artifact_shape():
    art = build_iron_artifact(_result(patch=_patch()))
    assert art["schema_version"] == IRON_RETURN_SCHEMA_VERSION
    assert art["status"] == "VERIFIED"
    assert art["payload"] == repo_patch_to_dict(_patch())
    assert isinstance(art["diff_hash"], str) and len(art["diff_hash"]) == 64
    assert art["unit_id"] == "u1"
    assert art["repo"] == "jarvis"


def test_diff_hash_is_sha256_of_canonical_patch():
    import hashlib
    import json

    art = build_iron_artifact(_result(patch=_patch()))
    canon = json.dumps(
        repo_patch_to_dict(_patch()), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert art["diff_hash"] == expected


def test_identical_diff_same_hash():
    a = build_iron_artifact(_result(patch=_patch()))
    b = build_iron_artifact(_result(patch=_patch()))
    assert a["diff_hash"] == b["diff_hash"]


def test_different_diff_different_hash():
    a = build_iron_artifact(_result(patch=_patch(content=b"AAA")))
    b = build_iron_artifact(_result(patch=_patch(content=b"BBB")))
    assert a["diff_hash"] != b["diff_hash"]


def test_failed_result_is_summary_not_scratchpad():
    art = build_iron_artifact(
        _result(status=WorkUnitState.FAILED, error="validation failed", failure_class="test")
    )
    assert art["status"] == "FAILED"
    assert art["payload"]["kind"] == "summary"
    assert art["payload"]["failure_class"] == "test"
    assert "validation failed" in art["payload"]["error"]


def test_cancelled_status():
    art = build_iron_artifact(_result(status=WorkUnitState.CANCELLED, error="cancelled"))
    assert art["status"] == "CANCELLED"


def test_completed_empty_patch_is_noop():
    art = build_iron_artifact(_result(patch=RepoPatch(repo="jarvis", files=())))
    assert art["status"] == "NOOP"


def test_long_error_truncated():
    art = build_iron_artifact(
        _result(status=WorkUnitState.FAILED, error="x" * 5000)
    )
    assert len(art["payload"]["error"]) < 600
    assert art["payload"]["error"].endswith("...[truncated]")


# --- the Iron Return rule: scratchpad excluded ------------------------------


def test_scratchpad_never_in_artifact():
    """Only status/payload/diff_hash cross — never intermediate worker messages.

    We assert the artifact carries ONLY the verified patch payload + metadata;
    a worker's scratchpad (which would be a sandbox message list) is absent.
    """
    art = build_iron_artifact(_result(patch=_patch()))
    assert set(art.keys()) == {
        "schema_version",
        "status",
        "payload",
        "diff_hash",
        "unit_id",
        "repo",
    }
    # The payload is the patch dict — it has no "messages"/"scratchpad" key.
    assert "messages" not in art["payload"]
    assert "scratchpad" not in art["payload"]
    assert "conversation" not in art["payload"]


# --- verify_artifact: fail-CLOSED -------------------------------------------


def test_verify_accepts_valid():
    art = build_iron_artifact(_result(patch=_patch()))
    assert verify_artifact(art) is True


def test_verify_rejects_tampered_payload():
    art = build_iron_artifact(_result(patch=_patch()))
    # tamper the payload — diff_hash no longer matches
    art["payload"]["new_content"][0]["content"] = "TAMPERED"
    assert verify_artifact(art) is False


def test_verify_rejects_tampered_hash():
    art = build_iron_artifact(_result(patch=_patch()))
    art["diff_hash"] = "0" * 64
    assert verify_artifact(art) is False


def test_verify_rejects_non_verified_status():
    art = build_iron_artifact(_result(status=WorkUnitState.FAILED, error="boom"))
    assert verify_artifact(art) is False


def test_verify_rejects_malformed():
    assert verify_artifact(None) is False
    assert verify_artifact("not a dict") is False
    assert verify_artifact({}) is False
    assert verify_artifact({"status": "VERIFIED"}) is False
    assert verify_artifact(
        {"schema_version": "wrong", "status": "VERIFIED", "payload": None, "diff_hash": "x"}
    ) is False


def test_verify_rejects_missing_diff_hash():
    art = build_iron_artifact(_result(patch=_patch()))
    del art["diff_hash"]
    assert verify_artifact(art) is False
