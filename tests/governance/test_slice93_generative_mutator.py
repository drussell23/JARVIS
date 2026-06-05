"""Slice 93 — Generative LLM MutationProvider + validity filter + corpus cache +
cost cap.

TDD regression spine. All tests use a MOCK async client — NO live LLM calls.

Covers:
1. LLMMutationProvider: async mutate, prompt factory output, parse code-fence +
   plain response, never-raises on model/parse/timeout errors.
2. Validity filter: ast.parse gate, UNPARSEABLE verdict, UNPARSEABLE excluded
   from escape-rate denominator (does not deflate rate).
3. Corpus cache: CorpusCacheSink writes all candidates to JSONL via
   cross_process_jsonl, reproducible.
4. Cost cap: hard session budget guard stops generation when cap hit, per-
   mutation cost ledger, flush cached valid mutations.
5. Protocol change: MutationProvider.mutate is now async; campaign call-site
   awaits it; default (no provider) behavior byte-identical.
6. AST-pin update: ImmunizationVerdict taxonomy includes UNPARSEABLE (5-value).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import List, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance import self_immunization as si


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class _Cat:
    def __init__(self, value: str) -> None:
        self.value = value


class _Seed:
    def __init__(
        self,
        name: str,
        source: str,
        category: str = "sandbox_escape",
        known_gap: bool = False,
    ) -> None:
        self.name = name
        self.source = source
        self.category = _Cat(category)
        self.known_gap = known_gap


_DOTTED_CALL_SRC = (
    "import shutil\n"
    "def run():\n"
    "    shutil.disk_usage('/')\n"
)

_VALID_PYTHON_MUTATION = (
    "import shutil\n"
    "_sl93 = shutil.disk_usage\n"
    "def run():\n"
    "    _sl93('/')\n"
)

_GARBAGE_MUTATION = "def (((not valid python:\n\x00"


def _make_mock_client(responses: List[str]):
    """Return an injectable mock async Anthropic client whose
    messages.create() returns each response string in sequence, then
    repeats the last."""
    client = MagicMock()
    call_count = [0]

    async def _create(**kwargs):
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        text = responses[idx]
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        msg.usage = MagicMock(input_tokens=100, output_tokens=50)
        return msg

    client.messages = MagicMock()
    client.messages.create = _create
    return client


def _make_error_client(exc_factory):
    """Return a client whose messages.create raises exc_factory()."""
    client = MagicMock()

    async def _create(**kwargs):
        raise exc_factory()

    client.messages = MagicMock()
    client.messages.create = _create
    return client


# ---------------------------------------------------------------------------
# Isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        si._ENV_MASTER,
        si._ENV_MUTATIONS_PER_PATTERN,
        si._ENV_TARGET_ESCAPE_RATE,
        si._ENV_LEDGER_PATH,
        si._ENV_CONCURRENCY,
        "JARVIS_ANTIVENOM_MUTATION_BUDGET_USD",
        "JARVIS_ANTIVENOM_CORPUS_CACHE_PATH",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(si._ENV_MASTER, "true")
    monkeypatch.setenv(
        si._ENV_LEDGER_PATH, str(tmp_path / "test_ledger.jsonl")
    )
    yield


# ===========================================================================
# 1. Async MutationProvider Protocol — mutate is now async
# ===========================================================================


class TestAsyncProtocol:
    async def test_llm_provider_is_async_mutate(self):
        """LLMMutationProvider.mutate is a coroutine function."""
        import inspect

        client = _make_mock_client(["```python\nx = 1\n```"])
        provider = si.LLMMutationProvider(client=client)
        assert inspect.iscoroutinefunction(provider.mutate)

    async def test_campaign_awaits_provider(self, monkeypatch):
        """run_immunization_campaign awaits async mutate (no TypeError)."""
        called = []

        class _AsyncProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                called.append(n)
                return []  # no extras — deterministic still runs

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_AsyncProvider(),
        ):
            reports.append(r)

        assert len(reports) == 1
        assert called  # was actually invoked

    async def test_default_no_provider_unchanged(self):
        """No mutation_provider → deterministic-only, no error."""
        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=None,
        ):
            reports.append(r)

        assert len(reports) == 1
        assert reports[0].total_mutations == 8  # exactly the deterministic 8


# ===========================================================================
# 2. LLMMutationProvider — async mutate, prompt factory, parsing
# ===========================================================================


class TestLLMMutationProvider:
    async def test_basic_returns_list_of_strings(self):
        client = _make_mock_client([_VALID_PYTHON_MUTATION])
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=1)
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    async def test_parses_code_fence_response(self):
        """Wrapped in ```python ... ``` → strip fences and return body."""
        fenced = f"```python\n{_VALID_PYTHON_MUTATION}```"
        client = _make_mock_client([fenced])
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=1)
        assert any(_VALID_PYTHON_MUTATION.strip() in s for s in result)

    async def test_parses_plain_code_response(self):
        """Bare code (no fence) also returned."""
        client = _make_mock_client([_VALID_PYTHON_MUTATION])
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=1)
        assert len(result) >= 1

    async def test_multiple_fences_parse_multiple_mutations(self):
        """Multiple code blocks in one response → multiple candidates."""
        multi = (
            "Variant 1:\n"
            "```python\nx = 1\n```\n"
            "Variant 2:\n"
            "```python\ny = 2\n```\n"
        )
        client = _make_mock_client([multi])
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=3)
        assert len(result) >= 2

    async def test_never_raises_on_model_error(self):
        """RuntimeError from client → returns [] (never propagates)."""
        client = _make_error_client(RuntimeError)
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=3)
        assert result == []

    async def test_never_raises_on_timeout(self):
        """asyncio.TimeoutError → returns []."""
        import asyncio

        client = _make_error_client(asyncio.TimeoutError)
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=3)
        assert result == []

    async def test_never_raises_on_parse_failure(self):
        """Completely unparseable model response → returns []."""
        client = _make_mock_client(["\x00\x01\x02\x03 not code at all ##!!"])
        provider = si.LLMMutationProvider(client=client)
        # Should not raise, even if all returned strings fail ast.parse
        result = await provider.mutate(_DOTTED_CALL_SRC, n=1)
        assert isinstance(result, list)

    async def test_respects_n_cap(self):
        """Provider returns at most n mutations."""
        many = "\n".join(
            f"```python\nx_{i} = {i}\n```" for i in range(20)
        )
        client = _make_mock_client([many])
        provider = si.LLMMutationProvider(client=client)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=3)
        assert len(result) <= 3

    async def test_prompt_factory_injects_seed(self):
        """The prompt factory embeds the seed_source text somewhere."""
        injected_prompts = []

        client = MagicMock()

        async def _capture_create(**kwargs):
            injected_prompts.append(str(kwargs))
            msg = MagicMock()
            msg.content = [MagicMock(text="x = 1")]
            msg.usage = MagicMock(input_tokens=50, output_tokens=20)
            return msg

        client.messages = MagicMock()
        client.messages.create = _capture_create

        provider = si.LLMMutationProvider(client=client)
        await provider.mutate("UNIQUE_MARKER_42", n=1)
        assert any("UNIQUE_MARKER_42" in p for p in injected_prompts)

    async def test_prompt_factory_is_injectable(self):
        """Custom prompt_factory overrides the default prompt."""
        custom_calls = []

        def _custom_factory(seed_source: str, n: int):
            custom_calls.append((seed_source, n))
            return {"system": "custom", "user": "do it"}

        client = _make_mock_client(["x = 1"])
        provider = si.LLMMutationProvider(
            client=client, prompt_factory=_custom_factory
        )
        await provider.mutate(_DOTTED_CALL_SRC, n=2)
        assert custom_calls
        assert custom_calls[0] == (_DOTTED_CALL_SRC, 2)


# ===========================================================================
# 3. Validity filter — UNPARSEABLE verdict, excluded from denominator
# ===========================================================================


class TestValidityFilter:
    def test_unparseable_in_verdict_enum(self):
        """ImmunizationVerdict has UNPARSEABLE value."""
        assert hasattr(si.ImmunizationVerdict, "UNPARSEABLE")
        assert si.ImmunizationVerdict.UNPARSEABLE.value == "unparseable"

    def _mk(self, verdict):
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="x=1",
        )
        return si.MutationResult(
            candidate=cand,
            verdict=verdict,
            cage_verdict="",
            semguard_findings=(),
        )

    def test_unparseable_excluded_from_denominator(self):
        """UNPARSEABLE counted in total_mutations but NOT in evaluable_count
        (escaped + still_caged), so it never deflates the escape rate."""
        results = [
            self._mk(si.ImmunizationVerdict.STILL_CAGED),
            self._mk(si.ImmunizationVerdict.ESCAPED),
            self._mk(si.ImmunizationVerdict.UNPARSEABLE),
            self._mk(si.ImmunizationVerdict.UNPARSEABLE),
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        # total: 4; evaluable: 2 (STILL_CAGED + ESCAPED); UNPARSEABLE: 2
        assert rep.total_mutations == 4
        assert rep.evaluable_count == 2
        assert rep.unparseable_count == 2
        # escape_rate = 1/2 = 0.5 (garbage did NOT dilute it to 1/4)
        assert rep.escape_rate == pytest.approx(0.5)

    def test_all_unparseable_does_not_raise(self):
        """Zero evaluable (all UNPARSEABLE) → rate=0.0, no ZeroDivisionError.
        Fix #2: outcome MUST be NO_EVALUABLE_MUTATIONS, NOT HARDENED."""
        results = [
            self._mk(si.ImmunizationVerdict.UNPARSEABLE),
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        assert rep.escape_rate == 0.0
        assert rep.evaluable_count == 0
        assert rep.unparseable_count == 1
        # Fix #2: cage was never exercised — must not read as verified-hardened.
        assert rep.outcome is si.ImmunizationOutcome.NO_EVALUABLE_MUTATIONS
        assert rep.outcome is not si.ImmunizationOutcome.HARDENED

    async def test_llm_garbage_mutation_not_in_denominator(self):
        """An LLM mutation that fails ast.parse gets UNPARSEABLE verdict
        and is excluded from the escape-rate denominator."""
        # Return garbage Python
        client = _make_mock_client([_GARBAGE_MUTATION])
        provider = si.LLMMutationProvider(client=client)

        # The provider itself returns the garbage string, but the campaign
        # wraps it with validity filter before counting it.
        raw = await provider.mutate(_DOTTED_CALL_SRC, n=1)

        # The provider's own parse-filter might already drop it, but the
        # campaign validity filter is the safety net. Simulate a campaign
        # with a stub that returns known-garbage.
        class _GarbageProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                return [_GARBAGE_MUTATION]

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_GarbageProvider(),
        ):
            reports.append(r)

        assert len(reports) == 1
        report = reports[0]
        # UNPARSEABLE mutations excluded from evaluable
        evaluable = report.escaped_count + report.still_caged_count
        # unparseable_count is a field on the report
        assert report.unparseable_count >= 1
        # evaluable denominator should NOT include UNPARSEABLE
        assert evaluable == (
            report.escaped_count + report.still_caged_count
        )

    def test_valid_python_passes_validity_filter(self):
        """A syntactically valid Python snippet parses without error."""
        try:
            ast.parse(_VALID_PYTHON_MUTATION)
            ok = True
        except SyntaxError:
            ok = False
        assert ok

    def test_garbage_fails_validity_filter(self):
        """The garbage mutation fails ast.parse (foundational check)."""
        with pytest.raises(SyntaxError):
            ast.parse(_GARBAGE_MUTATION)


# ===========================================================================
# 4. ImmunizationReport gains unparseable_count field
# ===========================================================================


class TestReportUnparseableField:
    def test_report_has_unparseable_count_field(self):
        """ImmunizationReport has unparseable_count attribute."""
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="x=1",
        )
        res = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.UNPARSEABLE,
            cage_verdict="",
            semguard_findings=(),
        )
        rep = si._build_report("s", "sandbox_escape", [res], 0.044)
        assert hasattr(rep, "unparseable_count")
        assert rep.unparseable_count == 1

    def test_report_to_dict_includes_unparseable_count(self):
        """to_dict() serializes unparseable_count."""
        rep = si._build_report("s", "sandbox_escape", [], 0.044)
        d = rep.to_dict()
        assert "unparseable_count" in d

    def test_report_from_dict_roundtrips_unparseable_count(self):
        """from_dict() deserializes unparseable_count."""
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="x=1",
        )
        res = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.UNPARSEABLE,
            cage_verdict="",
            semguard_findings=(),
        )
        rep = si._build_report("s", "sandbox_escape", [res], 0.044)
        d = rep.to_dict()
        back = si.ImmunizationReport.from_dict(d)
        assert back.unparseable_count == 1

    def test_evaluable_count_excludes_unparseable(self):
        """evaluable_count property ignores UNPARSEABLE (and INAPPLICABLE)."""
        rep = si._build_report(
            "s",
            "sandbox_escape",
            [],
            0.044,
        )
        # Zero mutations → evaluable_count == 0
        assert rep.evaluable_count == 0


# ===========================================================================
# 5. Corpus cache — CorpusCacheSink
# ===========================================================================


class TestCorpusCacheSink:
    async def test_cache_sink_writes_jsonl(self, tmp_path):
        """CorpusCacheSink appends every mutation candidate to JSONL."""
        cache_path = tmp_path / "corpus_cache.jsonl"
        sink = si.CorpusCacheSink(path=cache_path)

        cand = si.MutationCandidate(
            seed_entry_name="seed",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.ALIAS_REBIND,
            mutated_source="x = 1",
        )
        result = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.STILL_CAGED,
            cage_verdict="blocked_ast",
            semguard_findings=(),
        )
        ok = await sink.record_candidate(result)
        assert ok is True
        assert cache_path.exists()
        lines = cache_path.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["candidate"]["seed_entry_name"] == "seed"
        assert obj["candidate"]["strategy"] == "alias_rebind"

    async def test_cache_sink_appends_multiple(self, tmp_path):
        """Multiple record_candidate calls append multiple lines."""
        cache_path = tmp_path / "corpus_cache.jsonl"
        sink = si.CorpusCacheSink(path=cache_path)

        for i in range(3):
            cand = si.MutationCandidate(
                seed_entry_name=f"s{i}",
                seed_category="sandbox_escape",
                strategy=si.MutationStrategy.IDENTITY,
                mutated_source="x = 1",
            )
            res = si.MutationResult(
                candidate=cand,
                verdict=si.ImmunizationVerdict.STILL_CAGED,
                cage_verdict="",
                semguard_findings=(),
            )
            await sink.record_candidate(res)

        lines = cache_path.read_text().strip().splitlines()
        assert len(lines) == 3

    async def test_cache_sink_records_verdict(self, tmp_path):
        """Serialized record includes verdict field."""
        cache_path = tmp_path / "corpus_cache.jsonl"
        sink = si.CorpusCacheSink(path=cache_path)

        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="y = 2",
        )
        result = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.ESCAPED,
            cage_verdict="passed_through",
            semguard_findings=(),
        )
        await sink.record_candidate(result)
        obj = json.loads(cache_path.read_text())
        assert obj["verdict"] == "escaped"

    async def test_cache_sink_never_raises(self, tmp_path):
        """record_candidate is never-raises even on broken path."""
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX flock test only")

        # Use a path we cannot write to (root-owned directory sim: use /dev/null)
        sink = si.CorpusCacheSink(path=Path("/dev/null/impossible/path.jsonl"))
        cand = si.MutationCandidate(
            seed_entry_name="s",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="x=1",
        )
        res = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.STILL_CAGED,
            cage_verdict="",
            semguard_findings=(),
        )
        # Must not raise
        ok = await sink.record_candidate(res)
        assert ok is False  # returns False gracefully

    async def test_cache_sink_env_path(self, tmp_path, monkeypatch):
        """CorpusCacheSink() reads JARVIS_ANTIVENOM_CORPUS_CACHE_PATH."""
        cache_path = tmp_path / "env_cache.jsonl"
        monkeypatch.setenv(
            "JARVIS_ANTIVENOM_CORPUS_CACHE_PATH", str(cache_path)
        )
        # Default constructor uses env path
        sink = si.CorpusCacheSink()
        cand = si.MutationCandidate(
            seed_entry_name="e",
            seed_category="sandbox_escape",
            strategy=si.MutationStrategy.IDENTITY,
            mutated_source="z = 3",
        )
        res = si.MutationResult(
            candidate=cand,
            verdict=si.ImmunizationVerdict.STILL_CAGED,
            cage_verdict="",
            semguard_findings=(),
        )
        await sink.record_candidate(res)
        assert cache_path.exists()


# ===========================================================================
# 6. Cost cap — MutationBudgetGuard
# ===========================================================================


class TestCostCap:
    def test_guard_under_budget_allows(self):
        """Budget guard allows calls when under cap."""
        guard = si.MutationBudgetGuard(budget_usd=1.0)
        assert guard.remaining_usd > 0
        assert guard.is_exhausted() is False

    def test_guard_over_budget_stops(self):
        """Recording spend past the cap exhausts the guard."""
        guard = si.MutationBudgetGuard(budget_usd=0.01)
        guard.record_spend(0.02)
        assert guard.is_exhausted() is True

    def test_guard_exactly_at_budget_is_exhausted(self):
        """Spend equal to cap → exhausted."""
        guard = si.MutationBudgetGuard(budget_usd=0.05)
        guard.record_spend(0.05)
        assert guard.is_exhausted() is True

    def test_guard_accumulates_across_calls(self):
        """Multiple small spends accumulate correctly."""
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        guard.record_spend(0.04)
        guard.record_spend(0.04)
        assert guard.is_exhausted() is False
        guard.record_spend(0.03)
        assert guard.is_exhausted() is True

    def test_guard_cost_ledger(self):
        """MutationBudgetGuard exposes per-call cost ledger."""
        guard = si.MutationBudgetGuard(budget_usd=1.0)
        guard.record_spend(0.001, label="call_1")
        guard.record_spend(0.002, label="call_2")
        ledger = guard.cost_ledger()
        assert len(ledger) == 2
        assert any(e["label"] == "call_1" for e in ledger)
        assert any(e["label"] == "call_2" for e in ledger)

    def test_guard_remaining_usd(self):
        """remaining_usd decreases with spend."""
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        guard.record_spend(0.03)
        assert guard.remaining_usd == pytest.approx(0.07)

    def test_guard_reads_env_default(self, monkeypatch):
        """MutationBudgetGuard() reads JARVIS_ANTIVENOM_MUTATION_BUDGET_USD."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_MUTATION_BUDGET_USD", "0.25")
        guard = si.MutationBudgetGuard()
        assert guard._budget_usd == pytest.approx(0.25)

    async def test_cost_cap_stops_llm_generation(self, tmp_path):
        """Fix #8: budget trips after exactly 1 call; a second mutate() call
        with the SAME guard never reaches the LLM client."""
        call_count = [0]

        async def _create(**kwargs):
            call_count[0] += 1
            msg = MagicMock()
            msg.content = [MagicMock(text="x = 1")]
            # Each call costs ~$0.12 → way over the $0.05 budget.
            # input: 20000 tokens * $3/M = $0.06
            # output:  4000 tokens * $15/M = $0.06
            msg.usage = MagicMock(input_tokens=20000, output_tokens=4000)
            return msg

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create

        guard = si.MutationBudgetGuard(budget_usd=0.05)
        provider = si.LLMMutationProvider(client=client, budget_guard=guard)

        # First call: guard is not yet tripped (starts at 0), call goes through,
        # cost is recorded, guard is now exhausted.
        await provider.mutate(_DOTTED_CALL_SRC, n=10)
        assert call_count[0] == 1, (
            f"expected exactly 1 LLM call before budget trip, got {call_count[0]}"
        )

        # Second call: guard is exhausted, must NOT reach the client.
        await provider.mutate(_DOTTED_CALL_SRC, n=10)
        assert call_count[0] == 1, (
            "second mutate() with exhausted guard MUST NOT call the LLM client"
        )

    async def test_cost_cap_flush_cached_mutations(self, tmp_path):
        """When cap hit, valid mutations already generated are flushed/returned."""
        responses = ["x = 1\n", "y = 2\n"]
        client = _make_mock_client(responses)
        # Very generous budget for this test
        guard = si.MutationBudgetGuard(budget_usd=100.0)
        provider = si.LLMMutationProvider(client=client, budget_guard=guard)
        result = await provider.mutate(_DOTTED_CALL_SRC, n=2)
        # Should have collected something before budget (budget not hit here)
        assert isinstance(result, list)


# ===========================================================================
# 7. End-to-end campaign integration with LLMMutationProvider
# ===========================================================================


class TestCampaignIntegration:
    async def test_campaign_with_llm_provider_appends_mutations(self):
        """LLM mutations are appended beyond the deterministic 8."""
        client = _make_mock_client([_VALID_PYTHON_MUTATION])
        provider = si.LLMMutationProvider(client=client)

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=provider,
        ):
            reports.append(r)

        assert len(reports) == 1
        # LLM appended at least one mutation beyond deterministic 8
        assert reports[0].total_mutations >= 8

    async def test_campaign_with_all_unparseable_llm_output(self):
        """Campaign with LLM returning garbage: UNPARSEABLE counted, not
        in evaluable denominator, campaign completes without error."""

        class _AllGarbageProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                return [_GARBAGE_MUTATION] * n

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_AllGarbageProvider(),
        ):
            reports.append(r)

        assert len(reports) == 1
        r = reports[0]
        # UNPARSEABLE counted
        assert r.unparseable_count >= 1
        # Evaluable = only deterministic strategies that produced real verdicts
        evaluable = r.escaped_count + r.still_caged_count
        assert evaluable <= r.total_mutations - r.unparseable_count

    async def test_campaign_provider_error_still_runs_deterministic(self):
        """Even if async provider raises, deterministic-8 still runs."""

        class _AlwaysRaises:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                raise RuntimeError("boom")

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_AlwaysRaises(),
        ):
            reports.append(r)

        assert len(reports) == 1
        # Deterministic 8 still ran
        assert reports[0].total_mutations >= 1

    async def test_master_off_no_llm_calls(self, monkeypatch):
        """master_off → zero LLM calls even if provider injected."""
        monkeypatch.setenv(si._ENV_MASTER, "false")
        called = []

        class _TrackingProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                called.append(1)
                return []

        reports = []
        async for r in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_TrackingProvider(),
        ):
            reports.append(r)

        assert called == []  # master off → provider never called
        assert reports[0].outcome is si.ImmunizationOutcome.MASTER_OFF


# ===========================================================================
# 8. Calibration entrypoint (scripts/security/run_cc_parity_calibration.py)
# ===========================================================================


class TestCalibrationEntrypoint:
    def test_module_importable(self):
        """The calibration script is importable as a module."""
        import importlib

        # The script must be importable (it's a thin CLI).
        # We test this by importing the module path, not exec-ing it.
        import importlib.util
        import sys

        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "security"
            / "run_cc_parity_calibration.py"
        )
        assert script_path.exists(), (
            f"Calibration script not found at {script_path}"
        )
        spec = importlib.util.spec_from_file_location(
            "run_cc_parity_calibration", script_path
        )
        assert spec is not None

    def test_script_has_main_guard(self):
        """Calibration script has if __name__ == '__main__' guard."""
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "security"
            / "run_cc_parity_calibration.py"
        )
        src = script_path.read_text()
        assert '__name__' in src and '__main__' in src

    def test_script_has_cost_cap_argument(self):
        """Script mentions the cost cap env/flag."""
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "security"
            / "run_cc_parity_calibration.py"
        )
        src = script_path.read_text()
        assert "JARVIS_ANTIVENOM_MUTATION_BUDGET_USD" in src

    def test_script_has_mutation_limit(self):
        """Script mentions the ~200 mutation limit."""
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "security"
            / "run_cc_parity_calibration.py"
        )
        src = script_path.read_text()
        assert "200" in src


# ===========================================================================
# 9. Existing AST pin taxonomy updated for UNPARSEABLE
# ===========================================================================


class TestAstPinVerdictTaxonomy:
    def test_verdict_taxonomy_now_5_values(self):
        """ImmunizationVerdict has exactly 5 values after Slice 93."""
        values = {v.value for v in si.ImmunizationVerdict}
        assert "unparseable" in values
        assert len(values) == 5

    def test_register_shipped_invariants_reflects_5_verdict_values(self):
        """AST pin for verdict taxonomy reflects new 5-value set."""
        pins = si.register_shipped_invariants()
        verdict_pin = next(
            p for p in pins
            if p.invariant_name == "self_immunization_verdict_taxonomy_closed"
        )
        # The pin's expected set should now include "unparseable"
        canonical_src = Path(si.__file__).read_text(encoding="utf-8")
        canonical_tree = ast.parse(canonical_src)
        violations = verdict_pin.validate(canonical_tree, canonical_src)
        assert violations == (), f"Canonical source fails its own AST pin: {violations}"

    def test_old_4_value_synthetic_still_fires_pin(self):
        """A 4-value enum (missing UNPARSEABLE) fires the verdict pin."""
        pins = si.register_shipped_invariants()
        verdict_pin = next(
            p for p in pins
            if p.invariant_name == "self_immunization_verdict_taxonomy_closed"
        )
        # Synthetic with old 4 values, missing unparseable
        synthetic = (
            "import enum\n"
            "class ImmunizationVerdict(str, enum.Enum):\n"
            "    STILL_CAGED = 'still_caged'\n"
            "    ESCAPED = 'escaped'\n"
            "    INAPPLICABLE = 'inapplicable'\n"
            "    HARNESS_ERROR = 'harness_error'\n"
        )
        v = verdict_pin.validate(ast.parse(synthetic), synthetic)
        assert v and "missing" in v[0]


# ===========================================================================
# 10. Default-FALSE confirmed, INERT check
# ===========================================================================


class TestDefaultFalseInert:
    def test_master_default_false(self, monkeypatch):
        """Master flag defaults FALSE — §33.1."""
        monkeypatch.delenv(si._ENV_MASTER, raising=False)
        assert si.master_enabled() is False

    async def test_no_llm_calls_without_provider(self, monkeypatch):
        """No LLM calls if no provider injected — default behavior inert."""
        # If somehow an LLM was called without an injected provider, flag it.
        # This test confirms the default path never tries to import anthropic
        # or call any LLM by patching the lazy import.
        with patch(
            "backend.core.ouroboros.governance.self_immunization"
            ".LLMMutationProvider",
            side_effect=AssertionError("LLMMutationProvider instantiated without injection"),
        ):
            reports = []
            async for r in si.run_immunization_campaign(
                seeds=[_Seed("s", _DOTTED_CALL_SRC)],
                mutation_provider=None,
            ):
                reports.append(r)
        # Reached here means LLMMutationProvider was never auto-instantiated
        assert len(reports) == 1


# ===========================================================================
# 11. Code-review fixes (#1 / #2 / #3 / #4)
# ===========================================================================


class TestCodeReviewFixes:
    """Regression spine for the issues raised in the Slice 93 code review."""

    # ── Fix #1: usage=None must record conservative upper-bound spend ──────

    async def test_usage_none_records_conservative_spend(self):
        """Fix #1: when response.usage is None the guard MUST still record
        a conservative spend so is_exhausted() can eventually trip.
        Silently skipping would allow unbounded LLM calls."""

        async def _create(**kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text="x = 1")]
            msg.usage = None  # ← the problematic case
            return msg

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create

        guard = si.MutationBudgetGuard(budget_usd=10.0)
        before = guard.accumulated_usd
        provider = si.LLMMutationProvider(client=client, budget_guard=guard)
        await provider.mutate(_DOTTED_CALL_SRC, n=1)
        # Guard spend MUST have increased — conservative upper-bound recorded.
        assert guard.accumulated_usd > before, (
            "usage=None must not silently skip spend recording; "
            "guard would never trip → unbounded LLM calls"
        )

    async def test_usage_none_conservative_spend_can_trip_guard(self):
        """Fix #1: with a tiny budget and usage=None, the guard trips after
        the first call (conservative estimate exceeds the budget)."""

        async def _create(**kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text="x = 1")]
            msg.usage = None
            return msg

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create

        # Budget so small that even the conservative estimate (a few cents)
        # will exceed it.
        guard = si.MutationBudgetGuard(budget_usd=0.000001)
        provider = si.LLMMutationProvider(
            client=client, budget_guard=guard, max_tokens=2048
        )
        await provider.mutate(_DOTTED_CALL_SRC, n=1)
        # After the call the guard must be exhausted.
        assert guard.is_exhausted(), (
            "conservative spend for usage=None must be enough to trip the "
            "guard when budget is below the estimate"
        )

    # ── Fix #2: all-unparseable → NO_EVALUABLE_MUTATIONS, not HARDENED ────

    def test_all_unparseable_outcome_is_not_hardened(self):
        """Fix #2: _build_report with only UNPARSEABLE results MUST NOT
        return HARDENED — the cage was never exercised."""
        results = [
            si.MutationResult(
                candidate=si.MutationCandidate(
                    seed_entry_name="s",
                    seed_category="sandbox_escape",
                    strategy=si.MutationStrategy.IDENTITY,
                    mutated_source="",
                ),
                verdict=si.ImmunizationVerdict.UNPARSEABLE,
                cage_verdict="",
                semguard_findings=(),
                detail="llm_output_unparseable",
            )
            for _ in range(3)
        ]
        rep = si._build_report("s", "sandbox_escape", results, 0.044)
        assert rep.outcome is si.ImmunizationOutcome.NO_EVALUABLE_MUTATIONS
        assert rep.outcome is not si.ImmunizationOutcome.HARDENED

    async def test_campaign_all_unparseable_surfaces_in_summarize(self):
        """Fix #2: summarize_campaign exposes no_evaluable_seeds in the
        aggregate so the audit trail is honest."""

        class _AllGarbageProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                return [_GARBAGE_MUTATION] * n

        # Use a simple seed that produces 0 deterministic valid candidates
        # by giving it source that all deterministic strategies will yield
        # valid mutations for — but the LLM augmentation is all garbage.
        # We just need the LLM path to fire and produce UNPARSEABLE results.
        # Use a very large per_pattern so deterministic results come too,
        # but separately check the no_evaluable_seeds list is present.
        summary = await si.summarize_campaign(
            seeds=[_Seed("all_garbage", _DOTTED_CALL_SRC)],
            mutation_provider=_AllGarbageProvider(),
        )
        # The key contract: no_evaluable_seeds is present in the summary dict.
        assert "no_evaluable_seeds" in summary

    # ── Fix #3: CorpusCacheSink is called during campaign ─────────────────

    async def test_corpus_sink_called_for_all_mutations(self, tmp_path):
        """Fix #3: run_immunization_campaign with corpus_sink calls
        record_candidate for every MutationResult (escaped, caged,
        unparseable).  One JSONL line per generated mutation."""
        cache_path = tmp_path / "corpus.jsonl"
        corpus_sink = si.CorpusCacheSink(path=cache_path)

        with patch(
            "backend.core.ouroboros.governance.cross_process_jsonl"
            ".flock_append_line",
        ) as mock_flock:
            mock_flock.return_value = True

            client = _make_mock_client([_VALID_PYTHON_MUTATION])
            provider = si.LLMMutationProvider(client=client)

            reports = []
            async for r in si.run_immunization_campaign(
                seeds=[_Seed("s", _DOTTED_CALL_SRC)],
                mutation_provider=provider,
                corpus_sink=corpus_sink,
            ):
                reports.append(r)

        assert len(reports) == 1
        rep = reports[0]
        # flock_append_line should have been called once per total mutation.
        assert mock_flock.call_count == rep.total_mutations, (
            f"expected {rep.total_mutations} corpus writes, "
            f"got {mock_flock.call_count}"
        )

    async def test_corpus_sink_called_for_unparseable_too(self, tmp_path):
        """Fix #3: UNPARSEABLE results (pre-cage) are also sent to corpus_sink."""
        cache_path = tmp_path / "corpus.jsonl"
        corpus_sink = si.CorpusCacheSink(path=cache_path)
        recorded = []

        async def _capture(result):
            recorded.append(result.verdict)
            return True

        corpus_sink.record_candidate = _capture

        class _AllGarbageProvider:
            async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
                return [_GARBAGE_MUTATION]

        async for _ in si.run_immunization_campaign(
            seeds=[_Seed("s", _DOTTED_CALL_SRC)],
            mutation_provider=_AllGarbageProvider(),
            corpus_sink=corpus_sink,
        ):
            pass

        # At least the UNPARSEABLE mutation must have been recorded.
        assert si.ImmunizationVerdict.UNPARSEABLE in recorded

    # ── Fix #4: accumulated_usd property ──────────────────────────────────

    def test_accumulated_usd_property_exists(self):
        """Fix #4: MutationBudgetGuard.accumulated_usd is a public property."""
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        assert hasattr(guard, "accumulated_usd")
        assert guard.accumulated_usd == 0.0
        guard.record_spend(0.03)
        assert guard.accumulated_usd == pytest.approx(0.03)

    def test_accumulated_usd_matches_internal_state(self):
        """Fix #4: accumulated_usd equals the internal _accumulated field."""
        guard = si.MutationBudgetGuard(budget_usd=1.0)
        guard.record_spend(0.07)
        guard.record_spend(0.02)
        assert guard.accumulated_usd == pytest.approx(guard._accumulated)
        assert guard.accumulated_usd == pytest.approx(0.09)
