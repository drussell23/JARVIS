"""Slice 6 Task 3 — versioned attribution evidence schema (mirrors the
VisionSignalEvidence validate discipline; TestFailure evidence was
previously schema-free)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intent.signals import (
    TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION,
    build_attribution_evidence,
    validate_attribution_evidence,
)


def test_build_resolved_block() -> None:
    block = build_attribution_evidence(
        status="resolved",
        test_locus="tests/g/test_leaf.py",
        source_loci=["backend/core/leaf.py"],
        method="direct_import",
    )
    assert block["schema_version"] == TEST_FAILURE_ATTRIBUTION_SCHEMA_VERSION
    ok, err = validate_attribution_evidence(block)
    assert ok, err


def test_build_unresolved_block_requires_reason() -> None:
    block = build_attribution_evidence(
        status="unresolved",
        test_locus="tests/g/test_leaf.py",
        reason="no_first_party_source_imports",
    )
    ok, err = validate_attribution_evidence(block)
    assert ok, err


@pytest.mark.parametrize("mutation,expected_err", [
    ({"status": "banana"}, "status"),
    ({"source_loci": "not-a-list"}, "source_loci"),
    ({"test_locus": ""}, "test_locus"),
    ({"schema_version": 99}, "schema_version"),
])
def test_validator_rejects(mutation, expected_err) -> None:
    block = build_attribution_evidence(
        status="resolved",
        test_locus="tests/g/test_leaf.py",
        source_loci=["backend/core/leaf.py"],
        method="direct_import",
    )
    block.update(mutation)
    ok, err = validate_attribution_evidence(block)
    assert not ok
    assert expected_err in err


def test_resolved_requires_nonempty_source_loci() -> None:
    block = build_attribution_evidence(
        status="resolved", test_locus="tests/g/test_leaf.py",
        source_loci=[], method="direct_import",
    )
    ok, err = validate_attribution_evidence(block)
    assert not ok and "source_loci" in err
