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


import pytest

from backend.core.ouroboros.governance.cognitive_persistence import (
    CognitiveExperienceStore,
)


class _Entry:
    def __init__(self, key, value):
        self.key, self.value = key, value


class _FakePIM:
    """Mirrors PersistentIntelligenceManager's real read/write contract."""

    def __init__(self):
        self.rows: dict = {}

    async def set(self, key, value, category=None, metadata=None, **kw):
        self.rows[key] = _Entry(key, value)
        return self.rows[key]

    async def get_entry(self, key):
        return self.rows.get(key)

    async def get_by_prefix(self, prefix, limit=100):
        return [e for k, e in sorted(self.rows.items()) if k.startswith(prefix)][:limit]


@pytest.fixture()
def store():
    return CognitiveExperienceStore(pim=_FakePIM())


async def test_record_then_load_round_trips(store):
    exp = CognitiveExperience(
        kind=ExperienceKind.HALLUCINATED_TOOL,
        footprint="qwen3:32b@16384", subject="fetch_url", error_class="unknown_tool",
    )
    assert await store.record(exp, op_id="op-1") is True
    loaded = await store.load(footprint="qwen3:32b@16384")
    assert len(loaded) == 1 and loaded[0].subject == "fetch_url" and loaded[0].count == 1


async def test_record_same_pattern_merges_count(store):
    for i in range(3):
        exp = CognitiveExperience(
            kind=ExperienceKind.HALLUCINATED_TOOL,
            footprint="f@1", subject="fetch_url", error_class="unknown_tool",
        )
        await store.record(exp, op_id=f"op-{i}")
    loaded = await store.load(footprint="f@1")
    assert len(loaded) == 1 and loaded[0].count == 3


async def test_load_filters_by_footprint(store):
    for fp in ("a@1", "b@2"):
        await store.record(
            CognitiveExperience(kind=ExperienceKind.GENERATION_FAILURE,
                                footprint=fp, subject="GENERATE", error_class="timeout"),
            op_id="op-x",
        )
    assert len(await store.load(footprint="a@1")) == 1
    assert len(await store.load()) == 2  # cross-footprint load


async def test_store_never_raises_on_broken_pim(store):
    class _Broken:
        async def set(self, *a, **k): raise RuntimeError("db locked")
        async def get_entry(self, *a, **k): raise RuntimeError("db locked")
        async def get_by_prefix(self, *a, **k): raise RuntimeError("db locked")

    broken = CognitiveExperienceStore(pim=_Broken())
    exp = CognitiveExperience(kind=ExperienceKind.FAILED_TOOL_PATTERN,
                              footprint="f@1", subject="s", error_class="e")
    assert await broken.record(exp, op_id="op-1") is False
    assert await broken.load() == []


async def test_record_concurrent_same_pattern_never_loses_updates(store):
    """Read-modify-write must be serialized: N concurrent records of the
    same pattern yield count == N (no lost updates)."""
    import asyncio

    # Force interleaving: make the fake PIM yield control inside its
    # async methods so an unserialized read-modify-write WOULD lose updates.
    orig_get, orig_set = store._pim.get_entry, store._pim.set

    async def yielding_get(key):
        await asyncio.sleep(0)
        return await orig_get(key)

    async def yielding_set(key, value, **kw):
        await asyncio.sleep(0)
        return await orig_set(key, value, **kw)

    store._pim.get_entry, store._pim.set = yielding_get, yielding_set

    def _exp():
        return CognitiveExperience(
            kind=ExperienceKind.FAILED_TOOL_PATTERN,
            footprint="f@1", subject="run_tests", error_class="TimeoutError",
        )

    results = await asyncio.gather(
        *(store.record(_exp(), op_id=f"op-{i}") for i in range(10))
    )
    assert all(results)
    loaded = await store.load(footprint="f@1")
    assert len(loaded) == 1 and loaded[0].count == 10
