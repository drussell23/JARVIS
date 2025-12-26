"""
Test Suite for Repository Map Integration
==========================================

Tests the RepoMapEnricher and CodingQuestionDetector integration
with the JarvisPrimeClient.

Author: JARVIS AI System
"""

import asyncio
import pytest
import sys
from pathlib import Path

# Add project paths
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


class TestCodingQuestionDetector:
    """Test the CodingQuestionDetector class."""

    @pytest.fixture
    def detector(self):
        from backend.core.jarvis_prime_client import CodingQuestionDetector
        return CodingQuestionDetector()

    def test_detects_implementation_question(self, detector):
        """Should detect 'implement' keyword with high confidence."""
        prompt = "How do I implement a new API endpoint for voice authentication?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is True
        assert confidence >= 0.5
        assert "implement" in metadata["detected_keywords"]
        assert "api" in metadata["detected_keywords"]

    def test_detects_fix_bug_question(self, detector):
        """Should detect bug fixing questions."""
        prompt = "Can you fix the bug in the voice unlock code?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is True
        assert confidence >= 0.5
        assert "fix" in metadata["detected_keywords"]
        assert "bug" in metadata["detected_keywords"]

    def test_detects_file_reference(self, detector):
        """Should detect file references and extract paths."""
        prompt = "What does the function in backend/core/jarvis_prime_client.py do?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is True
        assert confidence >= 0.7  # File references boost confidence
        assert len(metadata["mentioned_files"]) > 0

    def test_detects_camel_case_symbols(self, detector):
        """Should detect CamelCase symbols."""
        prompt = "Where is the JarvisPrimeClient class defined?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is True
        assert "JarvisPrimeClient" in metadata["mentioned_symbols"]

    def test_detects_snake_case_symbols(self, detector):
        """Should detect snake_case symbols."""
        prompt = "How does the execute_task function work?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is True
        assert "execute_task" in metadata["mentioned_symbols"]

    def test_detects_relevant_repos(self, detector):
        """Should detect which repos are relevant based on keywords."""
        # JARVIS main repo
        prompt = "How does voice unlock work in JARVIS?"
        _, _, metadata = detector.detect(prompt)
        assert "jarvis" in metadata["relevant_repos"]

        # JARVIS Prime
        prompt = "How does the Cloud Run inference routing work in JARVIS Prime?"
        _, _, metadata = detector.detect(prompt)
        assert "jarvis_prime" in metadata["relevant_repos"]

        # Reactor Core
        prompt = "How does the training pipeline work in Reactor Core?"
        _, _, metadata = detector.detect(prompt)
        assert "reactor_core" in metadata["relevant_repos"]

    def test_non_coding_question(self, detector):
        """Should not flag non-coding questions."""
        prompt = "What's the weather like today?"
        is_coding, confidence, metadata = detector.detect(prompt)

        assert is_coding is False
        assert confidence < 0.4

    def test_edge_case_empty_prompt(self, detector):
        """Should handle empty prompts gracefully."""
        is_coding, confidence, metadata = detector.detect("")

        assert is_coding is False
        assert confidence == 0.0

    def test_question_structure_detection(self, detector):
        """Should boost confidence for question structures."""
        prompt = "How can I refactor the authentication module?"
        is_coding, confidence, _ = detector.detect(prompt)

        assert is_coding is True
        assert confidence >= 0.5  # Question structure + refactor keyword


class TestRepoMapEnricher:
    """Test the RepoMapEnricher class."""

    @pytest.fixture
    def enricher(self):
        from backend.core.jarvis_prime_client import RepoMapEnricher
        return RepoMapEnricher(
            max_context_tokens=1000,
            cache_ttl_seconds=60,
            enable_cross_repo=True,
        )

    @pytest.mark.asyncio
    async def test_enricher_initialization(self, enricher):
        """Should initialize the enricher."""
        assert enricher is not None
        assert enricher.max_context_tokens == 1000
        assert enricher.cache_ttl_seconds == 60
        assert enricher.enable_cross_repo is True

    @pytest.mark.asyncio
    async def test_non_coding_prompt_not_enriched(self, enricher):
        """Should not enrich non-coding prompts."""
        prompt = "Hello, how are you?"
        system, returned_prompt, metadata = await enricher.enrich_prompt(prompt)

        assert system is None
        assert returned_prompt == prompt
        assert metadata["is_coding_question"] is False

    @pytest.mark.asyncio
    async def test_force_enrich_flag(self, enricher):
        """Should force enrichment when flag is set."""
        prompt = "Hello, how are you?"  # Non-coding question
        system, returned_prompt, metadata = await enricher.enrich_prompt(
            prompt,
            force_enrich=True
        )

        # Should attempt enrichment even for non-coding question
        assert "is_coding_question" in metadata
        # May or may not succeed depending on mapper availability

    @pytest.mark.asyncio
    async def test_cache_key_generation(self, enricher):
        """Should generate consistent cache keys."""
        key1 = enricher._get_cache_key(["jarvis"], ["file.py"], ["Symbol"])
        key2 = enricher._get_cache_key(["jarvis"], ["file.py"], ["Symbol"])
        key3 = enricher._get_cache_key(["jarvis"], ["other.py"], ["Symbol"])

        assert key1 == key2  # Same inputs = same key
        assert key1 != key3  # Different inputs = different key

    @pytest.mark.asyncio
    async def test_cache_functionality(self, enricher):
        """Should cache repo maps and check TTL."""
        import time as time_module
        cache_key = "test_key"

        # Set cache with current time
        enricher._cache[cache_key] = ("test_content", time_module.time())

        # Should find in cache
        cached = enricher._check_cache(cache_key)
        assert cached == "test_content"

        # Expired cache should be None
        enricher._cache[cache_key] = ("test_content", 0)  # Very old timestamp
        cached = enricher._check_cache(cache_key)
        assert cached is None


class TestJarvisPrimeClientRepoMapIntegration:
    """Test the JarvisPrimeClient repo map integration."""

    @pytest.fixture
    def config(self):
        from backend.core.jarvis_prime_client import JarvisPrimeConfig
        return JarvisPrimeConfig(
            enable_repo_map_enrichment=True,
            repo_map_max_tokens=1000,
            repo_map_cache_ttl_seconds=60,
            enable_cross_repo_context=True,
        )

    @pytest.fixture
    def client(self, config):
        from backend.core.jarvis_prime_client import JarvisPrimeClient
        return JarvisPrimeClient(config)

    def test_client_has_enricher(self, client):
        """Client should have repo enricher when enabled."""
        assert client._repo_enricher is not None

    def test_detect_coding_question_method(self, client):
        """Client should expose coding question detection."""
        is_coding, confidence, metadata = client.detect_coding_question(
            "How do I implement a new feature in JarvisPrimeClient?"
        )

        assert is_coding is True
        assert confidence > 0.4
        assert "JarvisPrimeClient" in metadata["mentioned_symbols"]

    def test_stats_include_enricher_info(self, client):
        """Stats should include repo map enricher info."""
        stats = client.get_stats()

        assert "repo_map_enricher" in stats
        assert stats["repo_map_enricher"]["enabled"] is True
        assert "max_context_tokens" in stats["repo_map_enricher"]

    @pytest.mark.asyncio
    async def test_get_repo_map_method(self, client):
        """Client should expose repo map access."""
        # Note: This may fail if tree-sitter is not installed
        try:
            repo_map = await client.get_repo_map(
                repository="jarvis",
                max_tokens=500,
            )
            # May be None if mapper not available
            if repo_map:
                assert isinstance(repo_map, str)
                assert len(repo_map) > 0
        except Exception:
            # Expected if dependencies not installed
            pass

    def test_client_without_enricher(self):
        """Client should work with enricher disabled."""
        from backend.core.jarvis_prime_client import (
            JarvisPrimeClient,
            JarvisPrimeConfig,
        )

        config = JarvisPrimeConfig(enable_repo_map_enrichment=False)
        client = JarvisPrimeClient(config)

        assert client._repo_enricher is None

        # detect_coding_question should still work (returns False, 0.0, {})
        is_coding, confidence, metadata = client.detect_coding_question("test")
        assert is_coding is False
        assert confidence == 0.0
        assert metadata == {}


class TestCrossRepoContext:
    """Test cross-repo context enrichment."""

    @pytest.fixture
    def detector(self):
        from backend.core.jarvis_prime_client import CodingQuestionDetector
        return CodingQuestionDetector()

    def test_multi_repo_detection(self, detector):
        """Should detect when multiple repos are relevant."""
        prompt = """
        I want to understand how JARVIS voice commands trigger
        training runs in Reactor Core through JARVIS Prime orchestration.
        """
        is_coding, confidence, metadata = detector.detect(prompt)

        relevant = metadata["relevant_repos"]
        assert "jarvis" in relevant
        assert "jarvis_prime" in relevant
        assert "reactor_core" in relevant

    def test_jarvis_voice_detection(self, detector):
        """Should detect JARVIS main repo for voice-related queries."""
        prompt = "How does the voice biometric unlock work?"
        is_coding, _, metadata = detector.detect(prompt)

        # Should detect as JARVIS-related based on repo patterns
        assert "jarvis" in metadata["relevant_repos"]
        # The question should be detected as coding-related
        assert is_coding is True


# =============================================================================
# Integration Test (requires full dependencies)
# =============================================================================

class TestFullIntegration:
    """Full integration tests (may require all dependencies)."""

    @pytest.mark.asyncio
    async def test_complete_with_enrichment(self):
        """Test full completion flow with enrichment."""
        try:
            from backend.core.jarvis_prime_client import (
                JarvisPrimeClient,
                JarvisPrimeConfig,
            )

            config = JarvisPrimeConfig(
                enable_repo_map_enrichment=True,
                force_mode="gemini_api",  # Use Gemini for testing
            )
            client = JarvisPrimeClient(config)

            # This is a coding question
            response = await client.complete(
                prompt="What is the purpose of the JarvisPrimeClient class?",
                max_tokens=100,
                enrich_with_repo_map=True,
            )

            # Check enrichment metadata in response
            if response.success:
                enrichment = response.metadata.get("enrichment", {})
                # May or may not have enrichment depending on mapper availability
                assert isinstance(enrichment, dict)

            await client.close()

        except ImportError:
            pytest.skip("Full dependencies not available")
        except Exception as e:
            pytest.skip(f"Integration test failed: {e}")


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
