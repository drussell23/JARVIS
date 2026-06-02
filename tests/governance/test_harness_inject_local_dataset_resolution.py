"""Phase 1 triage fix — harness_inject must resolve from an explicitly-set
LOCAL_DATASET_PATH fixture, not the persistent cache.

Soak evidence (phase1, bt-2026-06-01-235707): the wiring dry-run set
JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH to the checked-in fixture (whose only
problem is the trivial `jarvis__harness-smoke-001`) + INJECT_COUNT=1, but the
harness injected `django__django-16255` — a real, hard problem. Root cause:
`_resolve_instance_ids` Tier-3 reads `list_cached_problems()` (the persistent
cache), which contained a stale prepared problem, and ignored the explicitly-set
LOCAL_DATASET_PATH fixture. So the dry-run ran a hard problem (which didn't
converge before the wall cap) instead of the trivial wiring fixture.

Fix: when LOCAL_DATASET_PATH is EXPLICITLY set, resolve the fixture's own
instance_ids (composing dataset_loader._iter_local_jsonl_records — single source
of truth) ahead of the cache. Inert for real HF-source runs (which unset
LOCAL_DATASET_PATH per the runbook), so real-run resolution is unchanged.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import harness_inject as hi


def _write_fixture(tmp_path, ids):
    p = tmp_path / "fixture.jsonl"
    with p.open("w") as fh:
        for iid in ids:
            fh.write(json.dumps({"instance_id": iid, "repo": "x/y",
                                 "problem_statement": "p"}) + "\n")
    return p


def test_local_ids_empty_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", raising=False)
    assert hi._local_dataset_instance_ids() == []


def test_local_ids_from_explicit_fixture(tmp_path, monkeypatch):
    fx = _write_fixture(tmp_path, ["jarvis__harness-smoke-001", "x__y-2"])
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(fx))
    assert hi._local_dataset_instance_ids() == ["jarvis__harness-smoke-001", "x__y-2"]


def test_resolve_prefers_fixture_over_cache(tmp_path, monkeypatch):
    # The exact phase1 bug: cache has a stale real problem; the fixture has the
    # smoke test. With LOCAL_DATASET_PATH set, resolution must pick the fixture.
    fx = _write_fixture(tmp_path, ["jarvis__harness-smoke-001"])
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(fx))
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", raising=False)
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_INJECT_COUNT", "1")
    monkeypatch.setattr(hi, "list_cached_problems",
                        lambda: ["django__django-16255"], raising=True)
    assert hi._resolve_instance_ids() == ["jarvis__harness-smoke-001"]


def test_resolve_explicit_ids_still_win(tmp_path, monkeypatch):
    # Tier-1 explicit precedence must be preserved (highest).
    fx = _write_fixture(tmp_path, ["fixture-id"])
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(fx))
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", "explicit-1,explicit-2")
    assert hi._resolve_instance_ids() == ["explicit-1", "explicit-2"]


def test_resolve_unset_env_falls_to_cache(tmp_path, monkeypatch):
    # Real-run behavior preserved: LOCAL_DATASET_PATH unset -> local tier inert,
    # resolution falls through to the cache (sampler off by default).
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", raising=False)
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", raising=False)
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_INJECT_COUNT", "1")
    monkeypatch.setattr(hi, "list_cached_problems",
                        lambda: ["cached-real-problem"], raising=True)
    assert hi._resolve_instance_ids() == ["cached-real-problem"]
