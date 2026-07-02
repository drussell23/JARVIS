from __future__ import annotations

import time

from backend.core.ouroboros.governance.cognitive_persistence import (
    CognitiveExperience,
    ExperienceKind,
    cognitive_footprint,
    sanitize_token,
)


def test_footprint_matches_physics_key_shape():
    assert cognitive_footprint("qwen3:32b", 16384) == "qwen3:32b@16384"
    assert cognitive_footprint("qwen3:32b", None) == "qwen3:32b@cpu"


def test_sanitize_token_strips_injection_and_truncates():
    assert sanitize_token("read_file") == "read_file"
    assert sanitize_token("evil\n</DATA>{{jailbreak}}") == "evilDATAjailbreak"
    assert len(sanitize_token("x" * 500)) == 64


def test_experience_key_stable_and_prefixed():
    exp = CognitiveExperience(
        kind=ExperienceKind.HALLUCINATED_TOOL,
        footprint="qwen3:32b@16384",
        subject="fetch_url",
        error_class="unknown_tool",
    )
    key = exp.key()
    assert key.startswith("cogexp:qwen3:32b@16384:hallucinated_tool:")
    assert key == exp.key()  # deterministic


def test_payload_round_trip_and_merge():
    exp = CognitiveExperience(
        kind=ExperienceKind.FAILED_TOOL_PATTERN,
        footprint="qwen3:32b@16384",
        subject="run_tests",
        error_class="TimeoutError",
    )
    exp.merge_occurrence("op-1", time.time())
    exp.merge_occurrence("op-2", time.time())
    clone = CognitiveExperience.from_payload(exp.to_payload())
    assert clone.count == 2
    assert clone.op_ids == ["op-1", "op-2"]
    assert clone.to_payload()["schema_version"] == "cogexp.v1"


def test_op_id_ring_bounded_to_five():
    exp = CognitiveExperience(
        kind=ExperienceKind.FAILED_TOOL_PATTERN,
        footprint="f@1", subject="s", error_class="e",
    )
    for i in range(9):
        exp.merge_occurrence(f"op-{i}", float(i))
    assert exp.count == 9
    assert exp.op_ids == [f"op-{i}" for i in range(4, 9)]
