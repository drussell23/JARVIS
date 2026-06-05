"""Slice 95d — async multi-call mutation batching + AST-hash dedup +
escaped-source capture.

TDD regression spine.  ALL LLM calls are MOCKED — zero live calls.

Test plan
---------
1. _ast_structural_hash: whitespace/comment-only differences collapse to
   the SAME hash; structurally-different sources differ; unparseable → None.
2. Batching reaches target across multiple calls (provider.call_count > 1).
3. Dedup filters duplicate sources → only 1 unique LLM candidate, loop
   terminates at max_calls_per_seed (not infinite).
4. max_calls_per_seed cap honoured (empty / all-dupes provider).
5. Backward-compat: batching default-OFF → exactly ONE mutate() call per
   seed (legacy path); llm_per_seed=None path unchanged.
6. AegisLeaseError propagates through the batching loop.
7. EscapeCaptureSink: writes the FULL mutated_source; default-OFF = no file.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Sequence
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WT_ROOT = Path(__file__).resolve().parents[2]
if str(_WT_ROOT) not in sys.path:
    sys.path.insert(0, str(_WT_ROOT))

from backend.core.ouroboros.governance import self_immunization as si  # noqa: E402


_AEGIS_ENABLED_PATH = "backend.core.ouroboros.aegis.client.is_enabled"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_minimal_seed():
    """Minimal seed-like object with name/category/source."""
    from backend.core.ouroboros.governance.graduation.adversarial_cage import (
        CorpusCategory,
    )

    class _Seed:
        name = "slice95d_seed"
        category = CorpusCategory.SANDBOX_ESCAPE
        source = "x = 1\n"

    return _Seed()


class _CountingProvider:
    """Fake async MutationProvider returning distinct valid sources.

    Each mutate(n=k) call returns up to k DISTINCT, structurally-unique,
    valid Python sources (a fresh assignment per variant).  Tracks the
    number of calls + the n requested each call.
    """

    def __init__(self, per_call_cap: int = 1000) -> None:
        self.call_count = 0
        self.requested_ns: List[int] = []
        self._counter = 0
        self._per_call_cap = per_call_cap

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        self.call_count += 1
        self.requested_ns.append(n)
        out: List[str] = []
        give = min(n, self._per_call_cap)
        for _ in range(max(0, give)):
            # Distinct variable name → structurally distinct AST.
            out.append(f"v{self._counter} = {self._counter}\n")
            self._counter += 1
        return out


class _SameSourceProvider:
    """Always returns the SAME single valid source (dedup target)."""

    def __init__(self) -> None:
        self.call_count = 0

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        self.call_count += 1
        return ["dup = 1\n"]


class _EmptyProvider:
    """Always returns []."""

    def __init__(self) -> None:
        self.call_count = 0

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        self.call_count += 1
        return []


class _LeaseRaisingProvider:
    """Raises AegisLeaseError on the first call."""

    def __init__(self) -> None:
        self.call_count = 0

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        self.call_count += 1
        raise si.AegisLeaseError("[CRITICAL] test lease denial")


async def _drive_campaign(**kwargs) -> None:
    async for _ in si.run_immunization_campaign(**kwargs):
        pass


def _count_llm_candidates_via_corpus(corpus_path: Path, seed_src: str) -> int:
    """Count accepted LLM (IDENTITY) candidates by reading the corpus cache
    JSONL — every evaluated candidate is recorded there.

    The deterministic ``_mut_identity`` operator ALSO emits exactly one
    IDENTITY candidate whose source == the seed; exclude it by skipping the
    single IDENTITY row whose ``mutated_source_bytes`` equals the seed's
    byte length (the deterministic identity)."""
    if not corpus_path.exists():
        return 0
    seed_bytes = len(seed_src.encode("utf-8", "replace"))
    count = 0
    skipped_det_identity = False
    for line in corpus_path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        cand = rec.get("candidate") or {}
        if cand.get("strategy") != "identity":
            continue
        if rec.get("verdict") not in ("escaped", "still_caged", "harness_error"):
            continue
        if (
            not skipped_det_identity
            and cand.get("mutated_source_bytes") == seed_bytes
        ):
            # The lone deterministic identity (source == seed). Exclude once.
            skipped_det_identity = True
            continue
        count += 1
    return count


# ===========================================================================
# 1. _ast_structural_hash
# ===========================================================================

class TestAstStructuralHash:
    def test_whitespace_and_comment_differences_collapse(self):
        a = "def f(x):\n    return x + 1\n"
        b = "def f(x):  # a comment\n\n    return x + 1\n\n"
        c = "def f(x):\n        return x + 1\n"  # different indent only
        ha = si._ast_structural_hash(a)
        hb = si._ast_structural_hash(b)
        hc = si._ast_structural_hash(c)
        assert ha is not None
        assert ha == hb, "comment/blank-line difference must not change hash"
        assert ha == hc, "indentation difference must not change hash"

    def test_structurally_different_sources_differ(self):
        a = si._ast_structural_hash("x = 1\n")
        b = si._ast_structural_hash("x = 2\n")
        c = si._ast_structural_hash("y = 1\n")
        assert a is not None and b is not None and c is not None
        assert a != b, "different literal value → different structure"
        assert a != c, "different target name → different structure"

    def test_unparseable_returns_none(self):
        assert si._ast_structural_hash("def (:\n") is None
        assert si._ast_structural_hash("this is not python ===") is None


# ===========================================================================
# 2. Batching reaches target across multiple calls
# ===========================================================================

class TestBatchingReachesTarget:
    def test_multiple_calls_accumulate_unique_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        corpus_path = tmp_path / "corpus.jsonl"
        provider = _CountingProvider(per_call_cap=5)  # ~5 per call
        seed = _make_minimal_seed()
        sink = si.CorpusCacheSink(path=corpus_path)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    corpus_sink=sink,
                    llm_per_seed=25,
                )
            )

        assert provider.call_count > 1, (
            f"expected pagination (>1 call), got {provider.call_count}"
        )
        n_llm = _count_llm_candidates_via_corpus(corpus_path, seed.source)
        assert n_llm == 25, (
            f"expected 25 accumulated unique LLM candidates, got {n_llm}"
        )


# ===========================================================================
# 3. Dedup filters duplicates + loop terminates at cap
# ===========================================================================

class TestDedupFiltersDuplicates:
    def test_same_source_yields_one_unique_and_stops_at_cap(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        monkeypatch.setenv(si._ENV_MAX_CALLS_PER_SEED, "4")
        corpus_path = tmp_path / "corpus.jsonl"
        provider = _SameSourceProvider()
        seed = _make_minimal_seed()
        sink = si.CorpusCacheSink(path=corpus_path)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    corpus_sink=sink,
                    llm_per_seed=25,
                )
            )

        n_llm = _count_llm_candidates_via_corpus(corpus_path, seed.source)
        assert n_llm == 1, f"dedup must keep exactly 1 unique, got {n_llm}"
        # Same-source never advances target → loop runs to the call-cap.
        assert provider.call_count == 4, (
            f"expected loop to hit cap=4, got {provider.call_count}"
        )


# ===========================================================================
# 4. max_calls_per_seed cap
# ===========================================================================

class TestMaxCallsCap:
    def test_empty_provider_stops_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        monkeypatch.setenv(si._ENV_MAX_CALLS_PER_SEED, "6")
        provider = _EmptyProvider()
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    llm_per_seed=25,
                )
            )
        # Empty return terminates the loop after a single call (model
        # exhausted) — must NOT burn the whole cap.
        assert provider.call_count == 1, (
            f"empty provider must stop after 1 call, got {provider.call_count}"
        )

    def test_all_dupes_respect_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        monkeypatch.setenv(si._ENV_MAX_CALLS_PER_SEED, "3")
        provider = _SameSourceProvider()
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    llm_per_seed=25,
                )
            )
        # Persistent dupes never reach the target and never return empty, so
        # the loop runs to EXACTLY the call cap (not merely <=).
        assert provider.call_count == 3, (
            f"provider.call_count must hit cap=3 exactly, got {provider.call_count}"
        )


# ===========================================================================
# 5. Backward-compat (default-OFF)
# ===========================================================================

class TestBackwardCompatDefaultOff:
    def test_batching_unset_single_call_per_seed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.delenv(si._ENV_BATCHING, raising=False)
        provider = _CountingProvider(per_call_cap=2)
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    llm_per_seed=25,
                )
            )
        assert provider.call_count == 1, (
            "default-OFF must use legacy single-call path "
            f"(exactly 1 call), got {provider.call_count}"
        )

    def test_llm_per_seed_none_unchanged_with_batching_on(
        self, tmp_path, monkeypatch
    ):
        # Even with batching ON, llm_per_seed=None must stay on the legacy
        # single-call path (batching only engages when a quota is given).
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        provider = _CountingProvider(per_call_cap=2)
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(
                _drive_campaign(
                    seeds=[seed],
                    mutation_provider=provider,
                    llm_per_seed=None,
                )
            )
        assert provider.call_count == 1, (
            "llm_per_seed=None must stay single-call even with batching ON, "
            f"got {provider.call_count}"
        )


# ===========================================================================
# 6. AegisLeaseError propagates through the batching loop
# ===========================================================================

class TestAegisLeasePropagates:
    def test_lease_error_propagates(self, monkeypatch):
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_BATCHING, "true")
        provider = _LeaseRaisingProvider()
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with pytest.raises(si.AegisLeaseError):
                _run(
                    _drive_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=25,
                    )
                )
        assert provider.call_count == 1, "lease error on first call"


# ===========================================================================
# 7. EscapeCaptureSink
# ===========================================================================

def _make_escaped_result() -> "si.MutationResult":
    cand = si.MutationCandidate(
        seed_entry_name="escape_seed",
        seed_category="sandbox_escape",
        strategy=si.MutationStrategy.IDENTITY,
        mutated_source="import os\nos.system('id')\n",
    )
    return si.MutationResult(
        candidate=cand,
        verdict=si.ImmunizationVerdict.ESCAPED,
        cage_verdict="passed_through",
        semguard_findings=("permission_loosened",),
        detail="escaped the cage",
    )


class TestEscapeCaptureSink:
    def test_record_escape_writes_full_source(self, tmp_path):
        path = tmp_path / "escapes.jsonl"
        sink = si.EscapeCaptureSink(path=path)
        res = _make_escaped_result()

        ok = _run(sink.record_escape(res))
        assert ok is True
        assert path.exists()

        lines = [l for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["kind"] == "escaped_variant"
        assert rec["seed_entry_name"] == "escape_seed"
        assert rec["seed_category"] == "sandbox_escape"
        assert rec["strategy"] == "identity"
        assert rec["cage_verdict"] == "passed_through"
        assert rec["semguard_findings"] == ["permission_loosened"]
        # The whole point: the FULL source, not a byte count.
        assert rec["mutated_source"] == "import os\nos.system('id')\n"
        assert "mutated_source_bytes" not in rec
        assert "schema_version" in rec
        assert "wrote_at_unix" in rec

    def test_record_escape_never_raises_on_bad_path(self, tmp_path):
        # A path whose parent cannot be created → swallow + return False.
        bad = tmp_path / "afile"
        bad.write_text("not a dir")
        path = bad / "nested" / "escapes.jsonl"
        sink = si.EscapeCaptureSink(path=path)
        res = _make_escaped_result()
        ok = _run(sink.record_escape(res))
        assert ok is False  # best-effort, never raised

    def test_default_off_no_file_written(self, tmp_path, monkeypatch):
        # With escape-capture disabled, the campaign must NOT construct the
        # sink nor write any escapes file.
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.delenv(si._ENV_ESCAPE_CAPTURE, raising=False)
        capture_path = tmp_path / "escapes.jsonl"
        monkeypatch.setenv(si._ENV_ESCAPE_CAPTURE_PATH, str(capture_path))
        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            _run(_drive_campaign(seeds=[seed]))
        assert not capture_path.exists(), (
            "no escape file may be written when capture is disabled"
        )

    def test_enabled_campaign_captures_escape(self, tmp_path, monkeypatch):
        # Wire a provider that returns a source which escapes the cage, then
        # assert the escapes file is written with the full source.  We force
        # an escape by stubbing _evaluate_candidate to return ESCAPED for the
        # LLM IDENTITY candidate.
        monkeypatch.setenv(si._ENV_MASTER, "true")
        monkeypatch.setenv(si._ENV_ESCAPE_CAPTURE, "true")
        capture_path = tmp_path / "escapes.jsonl"
        monkeypatch.setenv(si._ENV_ESCAPE_CAPTURE_PATH, str(capture_path))
        seed = _make_minimal_seed()

        escaping_source = "import os\nos.system('whoami')\n"

        class _EscapeProvider:
            async def mutate(self, seed_source, *, n):
                return [escaping_source]

        real_eval = si._evaluate_candidate

        def _fake_eval(cand):
            if cand.mutated_source == escaping_source:
                return si.MutationResult(
                    candidate=cand,
                    verdict=si.ImmunizationVerdict.ESCAPED,
                    cage_verdict="passed_through",
                    semguard_findings=(),
                    detail="forced escape",
                )
            return real_eval(cand)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch.object(si, "_evaluate_candidate", _fake_eval):
                _run(
                    _drive_campaign(
                        seeds=[seed],
                        mutation_provider=_EscapeProvider(),
                        llm_per_seed=1,
                    )
                )

        assert capture_path.exists(), "escape capture file must be written"
        lines = [l for l in capture_path.read_text().splitlines() if l.strip()]
        assert any(
            json.loads(l).get("mutated_source") == escaping_source
            for l in lines
        ), "captured record must contain the full escaping source"
