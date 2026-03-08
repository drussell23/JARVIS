# tests/governance/self_dev/test_candidate_parser.py
"""Tests for Phase 2B strict candidate parser (schema_version: 2b.1)."""
import ast
import hashlib
import json
import pytest

from backend.core.ouroboros.governance.providers import _parse_generation_response
from backend.core.ouroboros.governance.op_context import OperationContext, GenerationResult


DUMMY_SOURCE_HASH = "abc123" * 10  # 60 chars
DUMMY_SOURCE_PATH = "backend/core/foo.py"


def _ctx():
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="improve foo",
    )


def _valid_response(n_candidates=1, extra_top=None, extra_candidate=None):
    candidates = []
    for i in range(n_candidates):
        c = {
            "candidate_id": f"c{i+1}",
            "file_path": "backend/core/foo.py",
            "full_content": f"x = {i+1}\n",
            "rationale": f"candidate {i+1}",
        }
        if extra_candidate:
            c.update(extra_candidate)
        candidates.append(c)
    data = {
        "schema_version": "2b.1",
        "candidates": candidates,
        "provider_metadata": {
            "model_id": "llama-3.3-70b",
            "reasoning_summary": "made it better",
        },
    }
    if extra_top:
        data.update(extra_top)
    return json.dumps(data)


# ── JSON parse failure ────────────────────────────────────────────────────

def test_json_parse_error_raises():
    with pytest.raises(RuntimeError, match="json_parse_error"):
        _parse_generation_response("not json", "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


# ── schema_version checks ─────────────────────────────────────────────────

def test_wrong_schema_version_raises():
    data = json.loads(_valid_response())
    data["schema_version"] = "1.0"
    with pytest.raises(RuntimeError, match="wrong_schema_version"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


def test_missing_schema_version_raises():
    data = json.loads(_valid_response())
    del data["schema_version"]
    with pytest.raises(RuntimeError, match="wrong_schema_version"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


# ── extra key rejection ───────────────────────────────────────────────────

def test_extra_top_level_key_raises():
    with pytest.raises(RuntimeError, match="unexpected_keys"):
        _parse_generation_response(
            _valid_response(extra_top={"unknown_field": "oops"}),
            "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH
        )


def test_extra_candidate_key_raises():
    with pytest.raises(RuntimeError, match="unexpected_keys"):
        _parse_generation_response(
            _valid_response(extra_candidate={"bonus_key": "oops"}),
            "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH
        )


# ── candidates validation ─────────────────────────────────────────────────

def test_empty_candidates_list_raises():
    data = json.loads(_valid_response())
    data["candidates"] = []
    with pytest.raises(RuntimeError, match="candidates_empty"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


def test_missing_candidates_raises():
    data = json.loads(_valid_response())
    del data["candidates"]
    with pytest.raises(RuntimeError, match="missing_candidates"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


def test_more_than_3_candidates_truncates_to_3():
    raw = _valid_response(n_candidates=5)
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert len(result.candidates) == 3


def test_missing_candidate_field_raises():
    data = json.loads(_valid_response())
    del data["candidates"][0]["rationale"]
    with pytest.raises(RuntimeError, match="missing_rationale"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


# ── SyntaxError handling ──────────────────────────────────────────────────

def test_syntax_error_candidate_is_skipped():
    data = json.loads(_valid_response(n_candidates=2))
    # make c1 invalid syntax
    data["candidates"][0]["full_content"] = "def broken(:\n    pass"
    result = _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert len(result.candidates) == 1
    assert result.candidates[0]["candidate_id"] == "c2"


def test_all_syntax_error_raises():
    data = json.loads(_valid_response(n_candidates=2))
    data["candidates"][0]["full_content"] = "def broken(:\n    pass"
    data["candidates"][1]["full_content"] = "def also_broken(:\n    pass"
    with pytest.raises(RuntimeError, match="all_candidates_syntax_error"):
        _parse_generation_response(json.dumps(data), "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)


# ── hash provenance ───────────────────────────────────────────────────────

def test_candidate_hash_computed():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    c = result.candidates[0]
    expected = hashlib.sha256(c["full_content"].encode()).hexdigest()
    assert c["candidate_hash"] == expected


def test_source_hash_added_to_candidate():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert result.candidates[0]["source_hash"] == DUMMY_SOURCE_HASH


def test_source_path_added_to_candidate():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert result.candidates[0]["source_path"] == DUMMY_SOURCE_PATH


# ── GenerationResult fields ───────────────────────────────────────────────

def test_model_id_extracted_from_provider_metadata():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert result.model_id == "llama-3.3-70b"


def test_provider_name_set():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert result.provider_name == "gcp-jprime"


def test_returns_generation_result():
    raw = _valid_response()
    result = _parse_generation_response(raw, "gcp-jprime", 0.1, _ctx(), DUMMY_SOURCE_HASH, DUMMY_SOURCE_PATH)
    assert isinstance(result, GenerationResult)
