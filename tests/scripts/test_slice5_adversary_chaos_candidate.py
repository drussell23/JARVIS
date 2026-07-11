"""Slice 5 T7 — adversary batch lane serves the manifest's full-file repair."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "synthetic_adversary",
    Path(__file__).resolve().parents[2] / "scripts" / "synthetic_adversary.py",
)
adv = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("synthetic_adversary", adv)
_SPEC.loader.exec_module(adv)

_FULL_FILE = "def clamp01(x):\n    return max(0.0, min(1.0, x))\n" + ("# pad\n" * 400)
_MANIFEST = {
    "target_file": "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py",
    "original_source": _FULL_FILE,
    "function": "clamp01",
}


class TestBatchChaosCandidate:
    def test_chaos_targeted_batch_gets_full_file_candidate(self):
        prompt = (
            "Fix the failing test in "
            "backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py "
            "(clamp01 regression)."
        )
        body = adv.build_batch_candidates_content(prompt, manifest=_MANIFEST)
        payload = json.loads(body)
        assert payload["schema_version"] == "2b.1"
        cand = payload["candidates"][0]
        assert cand["file_path"] == _MANIFEST["target_file"]
        assert cand["full_content"] == _FULL_FILE, "must be the FULL original file, not a truncation"

    def test_non_chaos_batch_unchanged(self):
        prompt = "Summarize recent TODO debt in backend/voice/."
        body = adv.build_batch_candidates_content(prompt, manifest=_MANIFEST)
        payload = json.loads(body)
        assert payload["candidates"][0].get("full_content") != _FULL_FILE

    def test_no_manifest_keeps_legacy(self):
        prompt = "Fix leaf_predicates.py"
        body = adv.build_batch_candidates_content(prompt, manifest=None)
        json.loads(body)  # legacy canned body must remain parseable JSON
