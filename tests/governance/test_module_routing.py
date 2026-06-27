"""Tests for backend.core.ouroboros.governance.module_routing (MEM-1).

TDD: tests written first; implementation must make them green.
All tests are isolated — no filesystem side-effects, no real Oracle/embedder
calls (those are mocked where relevant).
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_topic(
    topics_dir: Path,
    filename: str,
    content: str,
) -> Path:
    """Write a fixture .md topic file and return its path."""
    p = topics_dir / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixture topic files
# ---------------------------------------------------------------------------

TOPIC_ORCHESTRATOR = textwrap.dedent("""\
    ---
    title: Orchestrator 11-Phase FSM
    modules:
      - orchestrator.py
      - governed_loop_service.py
    status: active
    ---

    # Orchestrator 11-Phase FSM

    The orchestrator runs an 11-phase governance pipeline: CLASSIFY -> ROUTE ->
    CONTEXT_EXPANSION -> PLAN -> GENERATE -> VALIDATE -> GATE -> APPROVE ->
    APPLY -> VERIFY -> COMPLETE.  Each phase is independently gated and
    fail-soft.  Key env: JARVIS_MULTI_FILE_GEN_ENABLED.
""")

TOPIC_PROVIDERS = textwrap.dedent("""\
    ---
    title: Provider Failback Chain
    modules: [providers.py, doubleword_provider.py]
    status: active
    ---

    # Provider Failback Chain

    Three-tier failback: DoubleWord 397B (Tier 0, RT SSE + webhook + adaptive
    poll) -> Claude (Tier 1, extended thinking + prompt caching) -> J-Prime
    (Tier 2, GCP self-hosted).  Urgency-aware routing by UrgencyRouter.
""")

TOPIC_SWARM = textwrap.dedent("""\
    ---
    title: Sovereign Multi-Agent Swarm
    modules:
      - swarm_orchestrator.py
      - subagent_scheduler.py
      - worktree_manager.py
    status: active
    ---

    # Sovereign Multi-Agent Swarm

    Delegates parallel sub-goals to dynamically-defined ephemeral sandboxed
    workers via the Epistemic Deadlock Breaker and elastic adaptive fan-out
    (MemoryPressureGate-gated).  Worker shape synthesised via AST/semantic
    sub-goal inspection.
""")

TOPIC_UNRELATED = textwrap.dedent("""\
    ---
    title: Voice Pipeline Notes
    modules:
      - voice_pipeline.py
      - wake_word.py
    status: notes
    ---

    # Voice Pipeline Notes

    The voice I/O layer handles wake word detection, TTS, and STT.  Completely
    orthogonal to the governance pipeline.
""")


# ---------------------------------------------------------------------------
# Test 1 — routing_enabled() default False
# ---------------------------------------------------------------------------

class TestRoutingEnabled:
    def test_default_false(self, monkeypatch):
        """routing_enabled() must be False when env var is absent."""
        monkeypatch.delenv("JARVIS_MEMORY_ROUTING_ENABLED", raising=False)
        from backend.core.ouroboros.governance.module_routing import routing_enabled
        assert routing_enabled() is False

    def test_truthy_values(self, monkeypatch):
        from backend.core.ouroboros.governance.module_routing import routing_enabled
        for val in ("1", "true", "True", "yes", "on"):
            monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", val)
            assert routing_enabled() is True, f"expected True for {val!r}"

    def test_falsy_values(self, monkeypatch):
        from backend.core.ouroboros.governance.module_routing import routing_enabled
        for val in ("0", "false", "False", "no", "off", ""):
            monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", val)
            assert routing_enabled() is False, f"expected False for {val!r}"


# ---------------------------------------------------------------------------
# Test 2 — flag off → empty RoutedContext immediately
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_route_returns_empty_when_flag_off(self, tmp_path, monkeypatch):
        """When JARVIS_MEMORY_ROUTING_ENABLED is false, route() returns empty."""
        monkeypatch.delenv("JARVIS_MEMORY_ROUTING_ENABLED", raising=False)

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
        router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
        ctx = router.route(
            target_files=["backend/core/ouroboros/governance/orchestrator.py"],
            query="refactor plan phase",
        )
        assert ctx.topics == ()
        assert ctx.section == ""


# ---------------------------------------------------------------------------
# Test 3 — topics ranked by relevance, flag on, no oracle
# ---------------------------------------------------------------------------

class TestSemanticRanking:
    def test_topics_returned_when_flag_on(self, tmp_path, monkeypatch):
        """With flag on and topics dir populated, route() returns topics."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)
        _write_topic(topics_dir, "providers.md", TOPIC_PROVIDERS)
        _write_topic(topics_dir, "swarm.md", TOPIC_SWARM)
        _write_topic(topics_dir, "voice.md", TOPIC_UNRELATED)

        # Mock Oracle to return empty (so only semantic ranking applies)
        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["backend/core/ouroboros/governance/orchestrator.py"],
                query="11-phase orchestrator governance pipeline PLAN phase",
                max_topics=3,
            )

        assert len(ctx.topics) >= 1
        assert ctx.section  # non-empty rendered block
        assert "## Relevant Architecture Memory" in ctx.section

    def test_section_contains_topic_title(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="orchestrator plan phase",
            )

        assert "Orchestrator" in ctx.section


# ---------------------------------------------------------------------------
# Test 4 — structural boost: topic's modules: overlap → boosted
# ---------------------------------------------------------------------------

class TestStructuralBoost:
    def test_matching_module_topic_ranked_first(self, tmp_path, monkeypatch):
        """A topic whose modules: matches the Oracle-returned related files
        must outrank a purely semantically similar but unrelated topic."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "swarm.md", TOPIC_SWARM)
        _write_topic(topics_dir, "voice.md", TOPIC_UNRELATED)

        # Oracle returns swarm_orchestrator.py as a related module for a
        # completely different target — simulating the AST dependency graph
        fake_related = ["swarm_orchestrator.py", "subagent_scheduler.py"]

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=fake_related,
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["unrelated_target.py"],
                query="something about voice pipeline",  # semantically points to voice
                max_topics=2,
            )

        # swarm topic must appear first (structural boost wins over semantic)
        assert len(ctx.topics) >= 1
        assert ctx.topics[0].source_id == "memory_topic:swarm"

    def test_direct_target_file_match_scores_structural_boost(self, tmp_path, monkeypatch):
        """A topic whose modules: contains the exact target file gets score=1.0 structural."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)
        _write_topic(topics_dir, "voice.md", TOPIC_UNRELATED)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="something",
                max_topics=2,
            )

        titles = [t.title for t in ctx.topics]
        assert "Orchestrator 11-Phase FSM" in titles
        # orchestrator topic must rank above voice (no structural overlap)
        assert ctx.topics[0].title == "Orchestrator 11-Phase FSM"


# ---------------------------------------------------------------------------
# Test 5 — token_budget and max_topics caps
# ---------------------------------------------------------------------------

class TestBudgetCaps:
    def test_max_topics_capped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        for i, content in enumerate(
            [TOPIC_ORCHESTRATOR, TOPIC_PROVIDERS, TOPIC_SWARM, TOPIC_UNRELATED]
        ):
            _write_topic(topics_dir, f"topic_{i}.md", content)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="governance pipeline",
                max_topics=2,
            )

        assert len(ctx.topics) <= 2

    def test_token_budget_limits_topics(self, tmp_path, monkeypatch):
        """A very small token_budget must limit the number of returned topics."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        for i, content in enumerate(
            [TOPIC_ORCHESTRATOR, TOPIC_PROVIDERS, TOPIC_SWARM]
        ):
            _write_topic(topics_dir, f"topic_{i}.md", content)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="governance pipeline",
                max_topics=3,
                token_budget=50,  # tiny budget — only one topic can fit
            )

        # At most 1 topic should fit within 50 chars of summary budget
        assert len(ctx.topics) == 1


# ---------------------------------------------------------------------------
# Test 6 — fail-soft: Oracle import/call failure → semantic-only (no crash)
# ---------------------------------------------------------------------------

class TestFailSoftOracle:
    def test_oracle_import_error_returns_semantic_ranking(self, tmp_path, monkeypatch):
        """When Oracle import fails, route() must still return topics (no crash)."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)
        _write_topic(topics_dir, "providers.md", TOPIC_PROVIDERS)

        # Simulate Oracle import failure
        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            side_effect=ImportError("oracle unavailable"),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            # Must not raise
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="governance pipeline",
            )

        # Should still return something (semantic ranking took over), or empty
        # — but must NEVER raise
        assert isinstance(ctx.topics, tuple)
        assert isinstance(ctx.section, str)

    def test_oracle_call_error_does_not_crash(self, tmp_path, monkeypatch):
        """When the _get_oracle_related_modules helper raises, route() degrades gracefully."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            side_effect=RuntimeError("oracle blew up"),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="something",
            )

        assert isinstance(ctx.topics, tuple)


# ---------------------------------------------------------------------------
# Test 7 — fail-soft: embedder unavailable → structural or empty (no crash)
# ---------------------------------------------------------------------------

class TestFailSoftEmbedder:
    def test_embedder_unavailable_falls_back_to_structural(self, tmp_path, monkeypatch):
        """When the embedder raises, route() falls back to structural-only ranking."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "swarm.md", TOPIC_SWARM)
        _write_topic(topics_dir, "voice.md", TOPIC_UNRELATED)

        # Swarm topic should win via structural match even when embedder fails
        fake_related = ["swarm_orchestrator.py"]

        with (
            patch(
                "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
                return_value=fake_related,
            ),
            patch(
                "backend.core.ouroboros.governance.module_routing._embed_texts",
                side_effect=RuntimeError("fastembed not installed"),
            ),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["unrelated_target.py"],
                query="something",
                max_topics=2,
            )

        # Structural signal should have placed swarm first
        assert isinstance(ctx.topics, tuple)
        if ctx.topics:
            assert ctx.topics[0].source_id == "memory_topic:swarm"

    def test_embedder_returns_none_handled(self, tmp_path, monkeypatch):
        """When embedder returns None, route() does not crash."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        with (
            patch(
                "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
                return_value=[],
            ),
            patch(
                "backend.core.ouroboros.governance.module_routing._embed_texts",
                return_value=None,
            ),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="something",
            )

        assert isinstance(ctx.topics, tuple)
        assert isinstance(ctx.section, str)


# ---------------------------------------------------------------------------
# Test 8 — empty topics_dir → empty RoutedContext
# ---------------------------------------------------------------------------

class TestEmptyTopicsDir:
    def test_empty_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()  # empty

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=[],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="anything",
            )

        assert ctx == ctx.empty()

    def test_nonexistent_topics_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
        router = ModuleContextRouter(tmp_path, topics_dir=tmp_path / "does_not_exist")
        ctx = router.route(target_files=["foo.py"], query="anything")
        assert ctx == ctx.empty()


# ---------------------------------------------------------------------------
# Test 9 — frontmatter parsing
# ---------------------------------------------------------------------------

class TestFrontmatterParsing:
    def test_inline_list_parsed(self):
        from backend.core.ouroboros.governance.module_routing import _parse_modules_frontmatter
        content = "---\nmodules: [a.py, b.py, c.py]\n---\n# Title\nBody"
        result = _parse_modules_frontmatter(content)
        assert result == ["a.py", "b.py", "c.py"]

    def test_multiline_list_parsed(self):
        from backend.core.ouroboros.governance.module_routing import _parse_modules_frontmatter
        content = "---\nmodules:\n  - x.py\n  - y.py\n---\n# Title\nBody"
        result = _parse_modules_frontmatter(content)
        assert result == ["x.py", "y.py"]

    def test_no_frontmatter_returns_empty(self):
        from backend.core.ouroboros.governance.module_routing import _parse_modules_frontmatter
        content = "# Title\nNo frontmatter here."
        result = _parse_modules_frontmatter(content)
        assert result == []

    def test_no_modules_key_returns_empty(self):
        from backend.core.ouroboros.governance.module_routing import _parse_modules_frontmatter
        content = "---\ntitle: Something\nstatus: active\n---\n# Title"
        result = _parse_modules_frontmatter(content)
        assert result == []

    def test_inline_modules_no_fence(self):
        """modules: key without --- fence, compact inline form."""
        from backend.core.ouroboros.governance.module_routing import _parse_modules_frontmatter
        content = "modules: [orchestrator.py, providers.py]\n\n# Title\nBody text"
        result = _parse_modules_frontmatter(content)
        assert "orchestrator.py" in result
        assert "providers.py" in result


# ---------------------------------------------------------------------------
# Test 10 — RoutedContext.empty() helper
# ---------------------------------------------------------------------------

class TestRoutedContextEmpty:
    def test_empty_has_no_topics_and_empty_section(self):
        from backend.core.ouroboros.governance.module_routing import RoutedContext
        ctx = RoutedContext.empty()
        assert ctx.topics == ()
        assert ctx.section == ""

    def test_section_render_with_topics(self, tmp_path, monkeypatch):
        """Rendered section contains heading and topic title."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        with patch(
            "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
            return_value=["orchestrator.py"],
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            ctx = router.route(
                target_files=["orchestrator.py"],
                query="plan phase",
            )

        assert "## Relevant Architecture Memory" in ctx.section
        assert "Orchestrator" in ctx.section


# ---------------------------------------------------------------------------
# Test 12 — I-1: candidate-first narrowing + persisted embedding cache
# ---------------------------------------------------------------------------

class TestCandidateFirstNarrowing:
    """I-1: candidate-first narrowing — embedder must NOT be called with all topics."""

    def _make_topics(self, topics_dir: Path, n_total: int, n_matching: int) -> None:
        """Write n_total topic files; n_matching have modules: [orchestrator.py]."""
        for i in range(n_total):
            mod_line = "orchestrator.py" if i < n_matching else f"unrelated_{i}.py"
            content = textwrap.dedent(f"""\
                ---
                title: Topic {i}
                modules:
                  - {mod_line}
                status: active
                ---

                # Topic {i}

                Summary text for topic number {i}.
            """)
            (topics_dir / f"topic_{i:03d}.md").write_text(content, encoding="utf-8")

    def test_bounded_embedding_with_module_match(self, tmp_path, monkeypatch):
        """When 2 topics match via modules:, embedder gets ≤ 3 texts (query + 2), not 41."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        self._make_topics(topics_dir, n_total=40, n_matching=2)

        # Clear module-level cache so disk/previous state doesn't interfere
        import backend.core.ouroboros.governance.module_routing as _mr
        if hasattr(_mr, "_emb_cache"):
            _mr._emb_cache.clear()
        if hasattr(_mr, "_emb_cache_loaded_roots"):
            _mr._emb_cache_loaded_roots.clear()

        embed_call_sizes: List[int] = []

        def _mock_embed(texts):
            embed_call_sizes.append(len(texts))
            return [[0.1 * j for j in range(4)] for _ in texts]

        with (
            patch(
                "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
                return_value=[],
            ),
            patch(
                "backend.core.ouroboros.governance.module_routing._embed_texts",
                side_effect=_mock_embed,
            ),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            router.route(
                target_files=["orchestrator.py"],
                query="orchestrator plan phase",
                max_topics=2,
            )

        total_embedded = sum(embed_call_sizes)
        # Must NOT embed all 40 topics; only the 2 matching + 1 query = ≤ 3
        assert total_embedded <= 3, (
            f"Expected ≤ 3 embedded texts (query + 2 matching topics), "
            f"got {total_embedded}. Candidate-first narrowing is not implemented."
        )

    def test_lexical_fallback_bounded(self, tmp_path, monkeypatch):
        """When no topics match via modules:, embedder gets ≤ prefilter_k+1 texts."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        # No topic matches orchestrator.py (all have unrelated modules)
        self._make_topics(topics_dir, n_total=40, n_matching=0)

        import backend.core.ouroboros.governance.module_routing as _mr
        if hasattr(_mr, "_emb_cache"):
            _mr._emb_cache.clear()
        if hasattr(_mr, "_emb_cache_loaded_roots"):
            _mr._emb_cache_loaded_roots.clear()

        embed_call_sizes: List[int] = []

        def _mock_embed(texts):
            embed_call_sizes.append(len(texts))
            return [[0.1 * j for j in range(4)] for _ in texts]

        with (
            patch(
                "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
                return_value=[],
            ),
            patch(
                "backend.core.ouroboros.governance.module_routing._embed_texts",
                side_effect=_mock_embed,
            ),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)
            router.route(
                target_files=["orchestrator.py"],
                query="orchestrator plan phase",
                max_topics=2,
            )

        total_embedded = sum(embed_call_sizes)
        # prefilter_k=24 topics + 1 query = 25 max; MUST NOT embed all 40
        assert total_embedded <= 25, (
            f"Expected ≤ 25 embedded texts (query + prefilter_k), "
            f"got {total_embedded}. Lexical prefilter is not implemented."
        )

    def test_cache_reuse_no_reembedding_on_second_call(self, tmp_path, monkeypatch):
        """Second route() call with same topics must not re-embed (all cached)."""
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)
        _write_topic(topics_dir, "providers.md", TOPIC_PROVIDERS)

        import backend.core.ouroboros.governance.module_routing as _mr
        if hasattr(_mr, "_emb_cache"):
            _mr._emb_cache.clear()
        if hasattr(_mr, "_emb_cache_loaded_roots"):
            _mr._emb_cache_loaded_roots.clear()

        embed_call_sizes_by_call: List[List[int]] = [[], []]
        call_index = [0]

        def _mock_embed(texts):
            embed_call_sizes_by_call[call_index[0]].append(len(texts))
            return [[float(j) * 0.1 for j in range(4)] for _ in texts]

        with (
            patch(
                "backend.core.ouroboros.governance.module_routing._get_oracle_related_modules",
                return_value=[],
            ),
            patch(
                "backend.core.ouroboros.governance.module_routing._embed_texts",
                side_effect=_mock_embed,
            ),
        ):
            from backend.core.ouroboros.governance.module_routing import ModuleContextRouter
            router = ModuleContextRouter(tmp_path, topics_dir=topics_dir)

            # First call — embeds fresh
            router.route(target_files=["orchestrator.py"], query="plan phase")
            call_index[0] = 1
            # Second call — same topics, same query: cache should be hit
            router.route(target_files=["orchestrator.py"], query="plan phase")

        second_call_total = sum(embed_call_sizes_by_call[1])
        assert second_call_total == 0, (
            f"Expected 0 embeddings on 2nd call (all cache hits), "
            f"got {second_call_total}. Embedding cache is not implemented."
        )


# ---------------------------------------------------------------------------
# Test 11 — integration-path: real get_oracle() import resolves, no crash
# ---------------------------------------------------------------------------

class TestOracleRealImport:
    def test_oracle_related_modules_real_import_does_not_raise(self, tmp_path, monkeypatch):
        """Exercise _get_oracle_related_modules WITHOUT mocking it.

        This test proves:
        1. The import ``from backend.core.ouroboros.oracle import get_oracle``
           resolves correctly (no ImportError / AttributeError on the old
           ``Oracle`` / ``Oracle.get_instance()`` path).
        2. The returned value is a list (possibly empty when the Oracle graph
           is cold/unbuilt — that is fine; empty is not a failure).
        3. No unhandled exception propagates.

        A cold oracle returning [] is explicitly acceptable; an ImportError or
        AttributeError would fail the test — which is the whole point.
        """
        monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "true")

        topics_dir = tmp_path / "memory_topics"
        topics_dir.mkdir()
        _write_topic(topics_dir, "orchestrator.md", TOPIC_ORCHESTRATOR)

        from backend.core.ouroboros.governance.module_routing import (
            ModuleContextRouter,
            _get_oracle_related_modules,
        )

        # Call the real helper — no mocking of _get_oracle_related_modules
        result = _get_oracle_related_modules(
            ["backend/core/ouroboros/governance/module_routing.py"]
        )

        # Must return a list (empty is fine — Oracle graph may be cold)
        assert isinstance(result, list), (
            f"Expected list, got {type(result).__name__!r}"
        )
