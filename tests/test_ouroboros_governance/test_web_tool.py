"""Tests for WebTool -- governance pipeline web access.

Covers:
- Tool is disabled by default
- URL validation rejects non-http schemes
- URL validation rejects non-allowlisted domains
- Rate limiting works (exceed limit -> 429 / error)
- _strip_html removes tags and decodes entities
- _parse_ddg_results extracts results from HTML
- fetch returns WebResult with correct fields
- search returns SearchResult with correct fields
- Tool definitions are valid MCP schema
- close() cleans up session

All network calls are mocked via FakeSession/FakeResponse. No real HTTP
requests are made.

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.tools.web_tool import (
    SearchResult,
    WebResult,
    WebTool,
    WebToolConfig,
    _DEFAULT_ALLOWED_DOMAINS,
)


# ---------------------------------------------------------------------------
# Fake aiohttp objects (no real network)
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for aiohttp.ClientResponse."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "text/html",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": content_type}

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeSession:
    """Minimal stand-in for aiohttp.ClientSession."""

    def __init__(self, response: Optional[FakeResponse] = None) -> None:
        self._response = response or FakeResponse()
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._response

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def enabled_config() -> WebToolConfig:
    """WebToolConfig with tool enabled and a tight rate limit for testing."""
    return WebToolConfig(
        enabled=True,
        timeout_s=5.0,
        max_response_bytes=1024,
        rate_limit_per_minute=3,
        allowed_domains=_DEFAULT_ALLOWED_DOMAINS,
        unrestricted_fetch=False,
    )


@pytest.fixture
def disabled_config() -> WebToolConfig:
    """WebToolConfig with tool disabled (the default)."""
    return WebToolConfig(enabled=False)


@pytest.fixture
def unrestricted_config() -> WebToolConfig:
    """WebToolConfig with unrestricted domain access."""
    return WebToolConfig(
        enabled=True,
        timeout_s=5.0,
        max_response_bytes=1024,
        rate_limit_per_minute=10,
        unrestricted_fetch=True,
    )


# ---------------------------------------------------------------------------
# Tests: disabled by default
# ---------------------------------------------------------------------------


class TestDisabledByDefault:
    """Tool should be disabled by default and reject all operations."""

    def test_default_config_disabled(self) -> None:
        config = WebToolConfig()
        assert config.enabled is False

    def test_from_env_disabled_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            config = WebToolConfig.from_env()
            assert config.enabled is False

    def test_from_env_enabled(self) -> None:
        with patch.dict("os.environ", {"JARVIS_WEB_TOOL_ENABLED": "true"}, clear=False):
            config = WebToolConfig.from_env()
            assert config.enabled is True

    def test_from_env_custom_timeout(self) -> None:
        with patch.dict(
            "os.environ",
            {"JARVIS_WEB_TOOL_ENABLED": "1", "JARVIS_WEB_TOOL_TIMEOUT": "30"},
            clear=False,
        ):
            config = WebToolConfig.from_env()
            assert config.enabled is True
            assert config.timeout_s == 30.0

    def test_from_env_custom_max_size(self) -> None:
        with patch.dict(
            "os.environ",
            {"JARVIS_WEB_TOOL_ENABLED": "yes", "JARVIS_WEB_TOOL_MAX_SIZE": "1024"},
            clear=False,
        ):
            config = WebToolConfig.from_env()
            assert config.max_response_bytes == 1024

    def test_is_enabled_property(self, disabled_config: WebToolConfig) -> None:
        tool = WebTool(config=disabled_config)
        assert tool.is_enabled is False

    @pytest.mark.asyncio
    async def test_fetch_when_disabled(self, disabled_config: WebToolConfig) -> None:
        tool = WebTool(config=disabled_config)
        result = await tool.fetch("https://docs.python.org/3/")
        assert isinstance(result, WebResult)
        assert result.error == "WebTool is disabled"
        assert result.status_code == 0

    @pytest.mark.asyncio
    async def test_search_when_disabled(self, disabled_config: WebToolConfig) -> None:
        tool = WebTool(config=disabled_config)
        result = await tool.search("python asyncio")
        assert isinstance(result, SearchResult)
        assert result.error == "WebTool is disabled"
        assert result.results == []


# ---------------------------------------------------------------------------
# Tests: URL validation
# ---------------------------------------------------------------------------


class TestURLValidation:
    """URL validation should reject bad schemes and non-allowlisted domains."""

    def test_rejects_ftp_scheme(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("ftp://example.com/file.txt")
        assert error is not None
        assert "Only http/https" in error

    def test_rejects_file_scheme(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("file:///etc/passwd")
        assert error is not None
        assert "Only http/https" in error

    def test_rejects_javascript_scheme(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("javascript:alert(1)")
        assert error is not None
        assert "Only http/https" in error

    def test_rejects_non_allowlisted_domain(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("https://evil-site.example.com/payload")
        assert error is not None
        assert "not in allowed domains" in error

    def test_accepts_allowlisted_domain(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("https://docs.python.org/3/library/asyncio.html")
        assert error is None

    def test_accepts_github(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        error = tool._validate_url("https://github.com/user/repo")
        assert error is None

    def test_unrestricted_allows_any_domain(self, unrestricted_config: WebToolConfig) -> None:
        tool = WebTool(config=unrestricted_config)
        error = tool._validate_url("https://any-random-domain.example.com/page")
        assert error is None

    def test_unrestricted_still_checks_scheme(self, unrestricted_config: WebToolConfig) -> None:
        tool = WebTool(config=unrestricted_config)
        error = tool._validate_url("ftp://any-random-domain.example.com/file")
        assert error is not None
        assert "Only http/https" in error

    @pytest.mark.asyncio
    async def test_fetch_blocked_domain_returns_error(
        self, enabled_config: WebToolConfig
    ) -> None:
        tool = WebTool(config=enabled_config)
        result = await tool.fetch("https://blocked-domain.example.com/page")
        assert result.error != ""
        assert "not in allowed domains" in result.error
        assert result.status_code == 0


# ---------------------------------------------------------------------------
# Tests: rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Rate limiter should cap requests per minute."""

    def test_within_limit(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        # Config allows 3 per minute
        assert tool._check_rate_limit() is True
        assert tool._check_rate_limit() is True
        assert tool._check_rate_limit() is True

    def test_exceeds_limit(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        # Exhaust 3 allowed
        for _ in range(3):
            assert tool._check_rate_limit() is True
        # 4th should fail
        assert tool._check_rate_limit() is False

    def test_old_entries_evicted(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        # Simulate 3 requests from 70 seconds ago (should be evicted)
        import time

        old_time = time.monotonic() - 70.0
        tool._request_times = [old_time, old_time + 1, old_time + 2]
        # Should succeed because old entries are evicted
        assert tool._check_rate_limit() is True

    @pytest.mark.asyncio
    async def test_fetch_rate_limited(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        # Exhaust rate limit
        for _ in range(enabled_config.rate_limit_per_minute):
            tool._check_rate_limit()

        result = await tool.fetch("https://docs.python.org/3/")
        assert result.status_code == 429
        assert "Rate limit exceeded" in result.error

    @pytest.mark.asyncio
    async def test_search_rate_limited(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        # Exhaust rate limit
        for _ in range(enabled_config.rate_limit_per_minute):
            tool._check_rate_limit()

        result = await tool.search("test query")
        assert "Rate limit exceeded" in result.error
        assert result.results == []


# ---------------------------------------------------------------------------
# Tests: HTML stripping
# ---------------------------------------------------------------------------


class TestStripHTML:
    """_strip_html should remove tags and decode entities."""

    def test_removes_basic_tags(self) -> None:
        result = WebTool._strip_html("<p>Hello <b>world</b></p>")
        assert "Hello" in result
        assert "world" in result
        assert "<p>" not in result
        assert "<b>" not in result

    def test_removes_script_blocks(self) -> None:
        html_text = '<p>Before</p><script>alert("xss")</script><p>After</p>'
        result = WebTool._strip_html(html_text)
        assert "alert" not in result
        assert "Before" in result
        assert "After" in result

    def test_removes_style_blocks(self) -> None:
        html_text = "<p>Content</p><style>body { color: red; }</style><p>More</p>"
        result = WebTool._strip_html(html_text)
        assert "color" not in result
        assert "Content" in result
        assert "More" in result

    def test_decodes_entities(self) -> None:
        result = WebTool._strip_html("&lt;hello&gt; &amp; &quot;world&quot;")
        assert "<hello>" in result
        assert '& "world"' in result

    def test_collapses_whitespace(self) -> None:
        result = WebTool._strip_html("<p>  lots   of   spaces  </p>")
        # Should be collapsed to single spaces
        assert "  " not in result
        assert "lots of spaces" in result

    def test_empty_string(self) -> None:
        result = WebTool._strip_html("")
        assert result == ""

    def test_no_tags(self) -> None:
        result = WebTool._strip_html("plain text no tags")
        assert result == "plain text no tags"


# ---------------------------------------------------------------------------
# Tests: DuckDuckGo result parsing
# ---------------------------------------------------------------------------


class TestParseDDGResults:
    """_parse_ddg_results should extract structured results from DDG HTML."""

    _SAMPLE_DDG_HTML = """
    <div class="result">
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">
        <b>asyncio</b> - Python docs
      </a>
      <a class="result__snippet">The asyncio module provides infrastructure for writing single-threaded concurrent code.</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fstackoverflow.com%2Fquestions%2Fasyncio">
        asyncio - Stack Overflow
      </a>
      <a class="result__snippet">Questions tagged [asyncio] on Stack Overflow.</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.com/no-redirect">
        Direct link example
      </a>
    </div>
    """

    def test_extracts_titles(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=5)
        assert len(results) == 3
        assert "asyncio" in results[0]["title"]
        assert "Stack Overflow" in results[1]["title"]

    def test_extracts_urls_with_redirect_unwrap(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=5)
        assert results[0]["url"] == "https://docs.python.org/3/library/asyncio.html"
        assert results[1]["url"] == "https://stackoverflow.com/questions/asyncio"

    def test_direct_url_kept(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=5)
        assert results[2]["url"] == "https://example.com/no-redirect"

    def test_extracts_snippets(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=5)
        assert "single-threaded" in results[0]["snippet"]
        assert "Stack Overflow" in results[1]["snippet"]

    def test_missing_snippet(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=5)
        # Third result has no matching snippet
        assert results[2]["snippet"] == ""

    def test_max_results_caps_output(self) -> None:
        results = WebTool._parse_ddg_results(self._SAMPLE_DDG_HTML, max_results=1)
        assert len(results) == 1

    def test_empty_html(self) -> None:
        results = WebTool._parse_ddg_results("", max_results=5)
        assert results == []

    def test_no_results_in_html(self) -> None:
        results = WebTool._parse_ddg_results("<html><body>No results found</body></html>", max_results=5)
        assert results == []


# ---------------------------------------------------------------------------
# Tests: fetch
# ---------------------------------------------------------------------------


class TestFetch:
    """fetch() should return WebResult with correct fields."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self, enabled_config: WebToolConfig) -> None:
        body = b"<html><body><p>Hello World</p></body></html>"
        fake_resp = FakeResponse(status=200, body=body, content_type="text/html")
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.fetch("https://docs.python.org/3/")

        assert isinstance(result, WebResult)
        assert result.status_code == 200
        assert result.url == "https://docs.python.org/3/"
        assert "Hello World" in result.content
        assert "<p>" not in result.content  # HTML stripped
        assert result.error == ""
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_fetch_plain_text(self, enabled_config: WebToolConfig) -> None:
        body = b"Just plain text, no HTML"
        fake_resp = FakeResponse(status=200, body=body, content_type="text/plain")
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.fetch("https://raw.githubusercontent.com/user/repo/main/README.md")

        assert result.status_code == 200
        assert result.content == "Just plain text, no HTML"

    @pytest.mark.asyncio
    async def test_fetch_truncates_large_response(self, enabled_config: WebToolConfig) -> None:
        # Config max is 1024 bytes
        body = b"x" * 2048
        fake_resp = FakeResponse(status=200, body=body, content_type="text/plain")
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.fetch("https://docs.python.org/3/big-page")

        assert result.truncated is True
        assert len(result.content) <= enabled_config.max_response_bytes

    @pytest.mark.asyncio
    async def test_fetch_timeout(self, enabled_config: WebToolConfig) -> None:
        class TimeoutSession:
            def get(self, url: str, **kwargs: Any) -> "TimeoutContext":
                return TimeoutContext()

            async def close(self) -> None:
                pass

        class TimeoutContext:
            async def __aenter__(self) -> None:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *args: Any) -> None:
                pass

        tool = WebTool(config=enabled_config)
        tool._session = TimeoutSession()

        result = await tool.fetch("https://docs.python.org/3/slow")

        assert result.status_code == 0
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_fetch_generic_error(self, enabled_config: WebToolConfig) -> None:
        class ErrorSession:
            def get(self, url: str, **kwargs: Any) -> "ErrorContext":
                return ErrorContext()

            async def close(self) -> None:
                pass

        class ErrorContext:
            async def __aenter__(self) -> None:
                raise ConnectionError("Connection refused")

            async def __aexit__(self, *args: Any) -> None:
                pass

        tool = WebTool(config=enabled_config)
        tool._session = ErrorSession()

        result = await tool.fetch("https://docs.python.org/3/broken")

        assert result.status_code == 0
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_fetch_non_200_status(self, enabled_config: WebToolConfig) -> None:
        fake_resp = FakeResponse(status=404, body=b"Not Found", content_type="text/html")
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.fetch("https://docs.python.org/3/missing")

        assert result.status_code == 404
        assert result.error == ""  # non-200 is not an error per se

    @pytest.mark.asyncio
    async def test_fetch_content_type_preserved(self, enabled_config: WebToolConfig) -> None:
        fake_resp = FakeResponse(
            status=200,
            body=b'{"key": "value"}',
            content_type="application/json",
        )
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.fetch("https://api.github.com/repos/user/repo")

        assert result.content_type == "application/json"
        assert result.content == '{"key": "value"}'


# ---------------------------------------------------------------------------
# Tests: search
# ---------------------------------------------------------------------------


class TestSearch:
    """search() should return SearchResult with correct fields."""

    _SEARCH_RESPONSE = """
    <div class="result">
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fresult1">
        Result One
      </a>
      <a class="result__snippet">Snippet for result one</a>
    </div>
    """

    @pytest.mark.asyncio
    async def test_successful_search(self, enabled_config: WebToolConfig) -> None:
        fake_resp = FakeResponse(
            status=200,
            body=self._SEARCH_RESPONSE.encode(),
            content_type="text/html",
        )
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.search("test query", max_results=5)

        assert isinstance(result, SearchResult)
        assert result.query == "test query"
        assert result.error == ""
        assert len(result.results) >= 1
        assert result.results[0]["title"] == "Result One"
        assert "example.com/result1" in result.results[0]["url"]

    @pytest.mark.asyncio
    async def test_search_non_200(self, enabled_config: WebToolConfig) -> None:
        fake_resp = FakeResponse(status=503, body=b"Service Unavailable")
        fake_session = FakeSession(response=fake_resp)

        tool = WebTool(config=enabled_config)
        tool._session = fake_session

        result = await tool.search("query")

        assert "status 503" in result.error
        assert result.results == []

    @pytest.mark.asyncio
    async def test_search_timeout(self, enabled_config: WebToolConfig) -> None:
        class TimeoutSession:
            def get(self, url: str, **kwargs: Any) -> "TimeoutCtx":
                return TimeoutCtx()

            async def close(self) -> None:
                pass

        class TimeoutCtx:
            async def __aenter__(self) -> None:
                raise asyncio.TimeoutError()

            async def __aexit__(self, *args: Any) -> None:
                pass

        tool = WebTool(config=enabled_config)
        tool._session = TimeoutSession()

        result = await tool.search("slow query")

        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_search_generic_error(self, enabled_config: WebToolConfig) -> None:
        class ErrorSession:
            def get(self, url: str, **kwargs: Any) -> "ErrorCtx":
                return ErrorCtx()

            async def close(self) -> None:
                pass

        class ErrorCtx:
            async def __aenter__(self) -> None:
                raise OSError("Network unreachable")

            async def __aexit__(self, *args: Any) -> None:
                pass

        tool = WebTool(config=enabled_config)
        tool._session = ErrorSession()

        result = await tool.search("broken query")

        assert "Network unreachable" in result.error


# ---------------------------------------------------------------------------
# Tests: MCP tool definitions
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    """to_tool_definitions should return valid MCP-compatible schemas."""

    def test_returns_two_tools(self) -> None:
        tool = WebTool(config=WebToolConfig())
        defs = tool.to_tool_definitions()
        assert len(defs) == 2

    def test_tool_names(self) -> None:
        tool = WebTool(config=WebToolConfig())
        defs = tool.to_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {"web_fetch", "web_search"}

    def test_fetch_schema_valid(self) -> None:
        tool = WebTool(config=WebToolConfig())
        defs = tool.to_tool_definitions()
        fetch_def = next(d for d in defs if d["name"] == "web_fetch")

        assert "description" in fetch_def
        assert isinstance(fetch_def["description"], str)

        schema = fetch_def["inputSchema"]
        assert schema["type"] == "object"
        assert "url" in schema["properties"]
        assert schema["properties"]["url"]["type"] == "string"
        assert "url" in schema["required"]

    def test_search_schema_valid(self) -> None:
        tool = WebTool(config=WebToolConfig())
        defs = tool.to_tool_definitions()
        search_def = next(d for d in defs if d["name"] == "web_search")

        assert "description" in search_def
        assert isinstance(search_def["description"], str)

        schema = search_def["inputSchema"]
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert schema["properties"]["query"]["type"] == "string"
        assert "query" in schema["required"]
        # max_results is optional
        assert "max_results" in schema["properties"]
        assert "max_results" not in schema["required"]


# ---------------------------------------------------------------------------
# Tests: session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """close() should clean up the aiohttp session."""

    @pytest.mark.asyncio
    async def test_close_with_session(self) -> None:
        fake_session = FakeSession()
        tool = WebTool(config=WebToolConfig(enabled=True))
        tool._session = fake_session

        await tool.close()

        assert fake_session.closed is True
        assert tool._session is None

    @pytest.mark.asyncio
    async def test_close_without_session(self) -> None:
        tool = WebTool(config=WebToolConfig(enabled=True))
        # Should not raise
        await tool.close()
        assert tool._session is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self) -> None:
        fake_session = FakeSession()
        tool = WebTool(config=WebToolConfig(enabled=True))
        tool._session = fake_session

        await tool.close()
        await tool.close()  # second call should be a no-op

        assert tool._session is None

    @pytest.mark.asyncio
    async def test_lazy_session_not_created_until_needed(self) -> None:
        tool = WebTool(config=WebToolConfig(enabled=True))
        assert tool._session is None
        # Just checking is_enabled does NOT create a session
        _ = tool.is_enabled
        assert tool._session is None


# ---------------------------------------------------------------------------
# Tests: config property
# ---------------------------------------------------------------------------


class TestConfigProperty:
    """The config property should expose the current configuration."""

    def test_config_property(self, enabled_config: WebToolConfig) -> None:
        tool = WebTool(config=enabled_config)
        assert tool.config is enabled_config
        assert tool.config.enabled is True
        assert tool.config.timeout_s == 5.0
