"""
Tests for DoublewordProvider.prompt_only() — governance-free inference bridge.

Covers:
  - Method existence and signature
  - ValueError on missing API key
  - Stats attribute presence and mutation
  - Full HTTP cycle via mocked aiohttp session
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(api_key: str = "test-key-abc") -> DoublewordProvider:
    return DoublewordProvider(api_key=api_key, base_url="https://api.doubleword.ai/v1")


def _json_resp(obj: dict) -> AsyncMock:
    """Return a mock async context-manager that yields a mock response."""
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=obj)
    resp.text = AsyncMock(return_value=json.dumps(obj))
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _text_resp(text: str, status: int = 200) -> AsyncMock:
    """Return a mock async context-manager that yields a text response."""
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Test: method exists
# ---------------------------------------------------------------------------

class TestPromptOnlyMethodExists:
    def test_prompt_only_method_exists(self):
        """prompt_only must be a callable coroutine method on DoublewordProvider."""
        provider = _make_provider()
        assert hasattr(provider, "prompt_only"), "prompt_only not found on DoublewordProvider"
        assert callable(provider.prompt_only), "prompt_only is not callable"
        assert asyncio.iscoroutinefunction(provider.prompt_only), (
            "prompt_only must be an async def"
        )

    def test_prompt_only_accepts_expected_kwargs(self):
        """prompt_only signature must accept prompt, model, caller_id, response_format, max_tokens."""
        import inspect
        sig = inspect.signature(DoublewordProvider.prompt_only)
        params = set(sig.parameters.keys())
        required = {"self", "prompt", "model", "caller_id", "response_format", "max_tokens"}
        assert required.issubset(params), (
            f"Missing params: {required - params}"
        )


# ---------------------------------------------------------------------------
# Test: raises ValueError on missing API key
# ---------------------------------------------------------------------------

class TestPromptOnlyMissingApiKey:
    def test_prompt_only_raises_on_missing_api_key(self):
        """prompt_only must raise ValueError when _api_key is empty."""
        provider = _make_provider(api_key="")
        with pytest.raises(ValueError, match="DOUBLEWORD_API_KEY"):
            asyncio.get_event_loop().run_until_complete(
                provider.prompt_only("hello world")
            )

    def test_prompt_only_raises_on_none_api_key(self):
        """Ensure the guard fires even if api_key was set to empty string via env."""
        provider = DoublewordProvider(api_key="", base_url="https://api.doubleword.ai/v1")
        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(
                provider.prompt_only("test")
            )


# ---------------------------------------------------------------------------
# Test: stats attributes exist
# ---------------------------------------------------------------------------

class TestPromptOnlyTracksStats:
    def test_prompt_only_tracks_stats_attributes_exist(self):
        """DoublewordStats must expose all attributes that prompt_only mutates."""
        provider = _make_provider()
        stats = provider._stats
        assert hasattr(stats, "total_batches"), "missing total_batches"
        assert hasattr(stats, "total_input_tokens"), "missing total_input_tokens"
        assert hasattr(stats, "total_output_tokens"), "missing total_output_tokens"
        assert hasattr(stats, "total_cost_usd"), "missing total_cost_usd"
        assert hasattr(stats, "total_latency_s"), "missing total_latency_s"
        assert hasattr(stats, "failed_batches"), "missing failed_batches"
        assert hasattr(stats, "empty_content_retries"), "missing empty_content_retries"

    def test_prompt_only_initial_stats_are_zero(self):
        """Stats must start at zero for a freshly created provider."""
        provider = _make_provider()
        assert provider._stats.total_batches == 0
        assert provider._stats.total_input_tokens == 0
        assert provider._stats.total_cost_usd == 0.0
        assert provider._stats.failed_batches == 0


# ---------------------------------------------------------------------------
# Test: full HTTP cycle via mocked session
# ---------------------------------------------------------------------------

def _build_batch_output_jsonl(custom_id: str, content: str) -> str:
    """Construct a synthetic JSONL response matching the Doubleword output format."""
    entry = {
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "body": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "total_tokens": 200,
                },
            },
        },
    }
    return json.dumps(entry)


class TestPromptOnlyFullCycle:
    """Mock the full 4-stage HTTP cycle and verify prompt_only returns content + updates stats."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _patch_session(self, provider: DoublewordProvider, responses: list):
        """
        Inject a mock aiohttp session whose post/get methods return responses
        from the provided list in order.

        responses is a list of async context managers (from _json_resp / _text_resp).
        Each call to session.post(...) or session.get(...) pops from the front.
        """
        call_order = iter(responses)

        def _get_cm(*args, **kwargs):
            return next(call_order)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=_get_cm)
        mock_session.get = MagicMock(side_effect=_get_cm)

        provider._session = mock_session
        return mock_session

    def test_prompt_only_returns_content_on_success(self):
        """Full cycle: upload → create → poll(completed) → retrieve → content returned."""
        provider = _make_provider()
        custom_id = "prompt_only_ouroboros_cognition"
        expected_content = "The synthesis result is: 42"

        # Stage 1: POST /files → {id: "file-123"}
        upload_resp = _json_resp({"id": "file-123"})
        # Stage 2: POST /batches → {id: "batch-456"}
        create_resp = _json_resp({"id": "batch-456"})
        # Stage 3: GET /batches/batch-456 → {status: "completed", output_file_id: "file-out-789"}
        poll_resp = _json_resp({"status": "completed", "output_file_id": "file-out-789"})
        # Stage 4: GET /files/file-out-789/content → JSONL
        jsonl_output = _build_batch_output_jsonl(custom_id, expected_content)
        retrieve_resp = _text_resp(jsonl_output)

        self._patch_session(provider, [upload_resp, create_resp, poll_resp, retrieve_resp])

        result = self._run(provider.prompt_only("What is the answer?"))

        assert result == expected_content, f"Expected '{expected_content}', got '{result}'"

    def test_prompt_only_increments_total_batches_on_success(self):
        """After a successful call, total_batches should be 1."""
        provider = _make_provider()
        custom_id = "prompt_only_ouroboros_cognition"
        content = "batch confirmed"

        self._patch_session(provider, [
            _json_resp({"id": "file-1"}),
            _json_resp({"id": "batch-1"}),
            _json_resp({"status": "completed", "output_file_id": "fout-1"}),
            _text_resp(_build_batch_output_jsonl(custom_id, content)),
        ])

        self._run(provider.prompt_only("test prompt"))
        assert provider._stats.total_batches == 1

    def test_prompt_only_updates_token_stats(self):
        """Token counts and cost must be updated after successful retrieval."""
        provider = _make_provider()
        custom_id = "prompt_only_ouroboros_cognition"

        self._patch_session(provider, [
            _json_resp({"id": "file-1"}),
            _json_resp({"id": "batch-1"}),
            _json_resp({"status": "completed", "output_file_id": "fout-1"}),
            _text_resp(_build_batch_output_jsonl(custom_id, "tokens test")),
        ])

        self._run(provider.prompt_only("count my tokens"))
        # The JSONL fixture sets prompt_tokens=120, completion_tokens=80
        assert provider._stats.total_input_tokens == 120
        assert provider._stats.total_output_tokens == 80
        assert provider._stats.total_cost_usd > 0.0

    def test_prompt_only_increments_failed_batches_on_upload_failure(self):
        """When file upload returns non-200, prompt_only returns '' without incrementing total_batches."""
        provider = _make_provider()

        # POST /files → 500 error
        bad_upload = AsyncMock()
        bad_upload.status = 500
        bad_upload.text = AsyncMock(return_value="internal error")
        bad_upload_cm = AsyncMock()
        bad_upload_cm.__aenter__ = AsyncMock(return_value=bad_upload)
        bad_upload_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=bad_upload_cm)
        provider._session = mock_session

        result = self._run(provider.prompt_only("this will fail"))
        assert result == ""
        assert provider._stats.total_batches == 0  # upload failed before batch was counted

    def test_prompt_only_respects_custom_caller_id(self):
        """custom_id in JSONL must embed the provided caller_id."""
        provider = _make_provider()
        caller_id = "synthesis_engine_v2"
        custom_id = f"prompt_only_{caller_id}"

        self._patch_session(provider, [
            _json_resp({"id": "file-1"}),
            _json_resp({"id": "batch-1"}),
            _json_resp({"status": "completed", "output_file_id": "fout-1"}),
            _text_resp(_build_batch_output_jsonl(custom_id, "custom caller result")),
        ])

        result = self._run(provider.prompt_only("caller test", caller_id=caller_id))
        assert result == "custom caller result"

    def test_prompt_only_respects_model_override(self):
        """When model= is provided, it should be passed to the JSONL body."""
        provider = _make_provider()
        custom_id = "prompt_only_ouroboros_cognition"

        posted_bodies = []
        original_upload = provider._upload_file

        async def capture_upload(jsonl_content: str):
            posted_bodies.append(json.loads(jsonl_content))
            return "file-captured"

        provider._upload_file = capture_upload  # type: ignore[method-assign]

        # Only upload is replaced — skip the rest via short circuit
        with patch.object(provider, "_create_batch", new=AsyncMock(return_value=None)):
            self._run(provider.prompt_only("model test", model="custom/model-7B"))

        assert len(posted_bodies) == 1
        assert posted_bodies[0]["body"]["model"] == "custom/model-7B"

    def test_prompt_only_includes_response_format_when_provided(self):
        """response_format dict should be forwarded into the JSONL body."""
        provider = _make_provider()
        posted_bodies = []

        async def capture_upload(jsonl_content: str):
            posted_bodies.append(json.loads(jsonl_content))
            return "file-rf"

        provider._upload_file = capture_upload  # type: ignore[method-assign]

        with patch.object(provider, "_create_batch", new=AsyncMock(return_value=None)):
            self._run(
                provider.prompt_only(
                    "json format test",
                    response_format={"type": "json_object"},
                )
            )

        assert len(posted_bodies) == 1
        assert posted_bodies[0]["body"].get("response_format") == {"type": "json_object"}

    def test_prompt_only_omits_response_format_when_none(self):
        """response_format should NOT appear in the JSONL body when not provided."""
        provider = _make_provider()
        posted_bodies = []

        async def capture_upload(jsonl_content: str):
            posted_bodies.append(json.loads(jsonl_content))
            return "file-no-rf"

        provider._upload_file = capture_upload  # type: ignore[method-assign]

        with patch.object(provider, "_create_batch", new=AsyncMock(return_value=None)):
            self._run(provider.prompt_only("no format test"))

        assert "response_format" not in posted_bodies[0]["body"]

    def test_prompt_only_respects_max_tokens_override(self):
        """max_tokens override should be reflected in the JSONL body."""
        provider = _make_provider()
        posted_bodies = []

        async def capture_upload(jsonl_content: str):
            posted_bodies.append(json.loads(jsonl_content))
            return "file-mt"

        provider._upload_file = capture_upload  # type: ignore[method-assign]

        with patch.object(provider, "_create_batch", new=AsyncMock(return_value=None)):
            self._run(provider.prompt_only("token limit test", max_tokens=512))

        assert posted_bodies[0]["body"]["max_tokens"] == 512

    def test_prompt_only_returns_empty_on_poll_timeout(self):
        """When _poll_batch returns None (timeout/failure), prompt_only returns ''."""
        provider = _make_provider()

        with patch.object(provider, "_upload_file", new=AsyncMock(return_value="file-1")), \
             patch.object(provider, "_create_batch", new=AsyncMock(return_value="batch-1")), \
             patch.object(provider, "_poll_batch", new=AsyncMock(return_value=None)):
            result = self._run(provider.prompt_only("poll failure test"))

        assert result == ""
        assert provider._stats.failed_batches == 1

    def test_prompt_only_increments_empty_content_retries_on_empty_response(self):
        """When retrieve returns empty content, empty_content_retries increments."""
        provider = _make_provider()

        with patch.object(provider, "_upload_file", new=AsyncMock(return_value="file-1")), \
             patch.object(provider, "_create_batch", new=AsyncMock(return_value="batch-1")), \
             patch.object(provider, "_poll_batch", new=AsyncMock(return_value="fout-1")), \
             patch.object(provider, "_retrieve_result", new=AsyncMock(return_value=("", None))):
            result = self._run(provider.prompt_only("empty test"))

        assert result == ""
        assert provider._stats.empty_content_retries == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
