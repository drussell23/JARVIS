"""Tests for the Ouroboros Operation Identity System (UUIDv7)."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.core.ouroboros.governance.operation_id import (
    OperationMetadata,
    generate_operation_id,
)


# ---------------------------------------------------------------------------
# Pattern: op-<uuidv7>-<repo_origin>
# UUIDv7 is 32 hex chars with 4 hyphens = 36 chars
# ---------------------------------------------------------------------------
_OP_ID_RE = re.compile(
    r"^op-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}-.+$"
)


class TestGenerateOperationId:
    """Tests for generate_operation_id()."""

    def test_generate_returns_valid_format(self):
        """ID starts with 'op-', ends with '-jarvis', UUIDv7 in the middle."""
        op_id = generate_operation_id()
        assert op_id.startswith("op-")
        assert op_id.endswith("-jarvis")
        # Extract the UUID portion between first 'op-' and last '-jarvis'
        uuid_part = op_id[3 : -len("-jarvis")]
        # UUIDv7 has version nibble '7' in the 13th hex position
        assert _OP_ID_RE.match(op_id), f"ID does not match expected pattern: {op_id}"

    def test_generate_monotonic_sorting(self):
        """100 sequential IDs must sort chronologically by plain string comparison."""
        ids = [generate_operation_id() for _ in range(100)]
        assert ids == sorted(ids), "Sequential IDs are not monotonically sortable"

    def test_generate_no_collisions_concurrent(self):
        """10,000 concurrent generations via ThreadPoolExecutor produce zero collisions."""
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _: generate_operation_id(), range(10_000)))
        assert len(results) == 10_000
        assert len(set(results)) == 10_000, "Collision detected among 10,000 IDs"

    def test_different_repo_origins(self):
        """Repo origin suffix is correctly applied for various origins."""
        for origin in ("jarvis", "prime", "reactor"):
            op_id = generate_operation_id(repo_origin=origin)
            assert op_id.endswith(f"-{origin}"), (
                f"Expected suffix '-{origin}', got: {op_id}"
            )
            assert op_id.startswith("op-")


class TestOperationMetadata:
    """Tests for the OperationMetadata dataclass."""

    def test_create_with_policy_version(self):
        """OperationMetadata stores version and computes a 64-char hex hash."""
        meta = OperationMetadata(
            op_id=generate_operation_id(),
            policy_version="1.0.0",
            decision_inputs={"action": "modify", "file": "foo.py"},
        )
        assert meta.policy_version == "1.0.0"
        assert isinstance(meta.decision_inputs_hash, str)
        assert len(meta.decision_inputs_hash) == 64
        # Must be valid lowercase hex
        assert re.fullmatch(r"[0-9a-f]{64}", meta.decision_inputs_hash)

    def test_same_inputs_produce_same_hash(self):
        """Identical decision_inputs must yield an identical hash (deterministic)."""
        inputs = {"action": "delete", "path": "/tmp/test", "count": 42}
        meta_a = OperationMetadata(
            op_id=generate_operation_id(),
            policy_version="1.0.0",
            decision_inputs=inputs,
        )
        meta_b = OperationMetadata(
            op_id=generate_operation_id(),
            policy_version="1.0.0",
            decision_inputs=inputs,
        )
        assert meta_a.decision_inputs_hash == meta_b.decision_inputs_hash

    def test_idempotency_check(self):
        """is_new() returns True the first time, False after adding to the seen set."""
        meta = OperationMetadata(
            op_id=generate_operation_id(),
            policy_version="1.0.0",
            decision_inputs={"action": "test"},
        )
        seen: set[str] = set()
        assert meta.is_new(seen) is True
        # Simulate recording this operation
        seen.add(meta.op_id)
        assert meta.is_new(seen) is False
