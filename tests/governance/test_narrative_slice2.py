"""Tests for Gap #6 Slice 2 — preamble synthesizer + intent prompter."""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.narrative_channel import (
    NarrativeChannel,
    NarrativeKind,
    reset_default_channel_for_tests,
)
from backend.core.ouroboros.governance.intent_prompter import (
    INTENT_PROMPTER_SCHEMA_VERSION,
    IntentRequest,
    IntentResult,
    MASTER_FLAG_ENV_VAR,
    MAX_TOKENS_ENV_VAR,
    TIMEOUT_ENV_VAR,
    build_user_prompt,
    is_master_flag_enabled,
    read_max_tokens,
    read_timeout_s,
    request_intent,
    request_intent_and_emit,
)
from backend.core.ouroboros.governance.tool_preamble_synthesizer import (
    PreambleTemplate,
    TOOL_PREAMBLE_SCHEMA_VERSION,
    get_template,
    is_known_tool,
    known_tool_kinds,
    synthesize_preamble,
)


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv(MAX_TOKENS_ENV_VAR, raising=False)
    reset_default_channel_for_tests()
    yield
    reset_default_channel_for_tests()


# ===========================================================================
# ToolPreambleSynthesizer — schema + closed catalog
# ===========================================================================


def test_preamble_schema_version():
    assert TOOL_PREAMBLE_SCHEMA_VERSION == "tool_preamble_synthesizer.v1"


_EXPECTED_TOOLS = frozenset({
    "read_file", "list_symbols", "search_code", "run_tests",
    "get_callers", "glob_files", "list_dir", "git_log", "git_diff",
    "git_blame", "bash", "edit_file", "write_file", "delete_file",
    "type_check", "web_fetch", "web_search", "ask_human",
})


def test_all_venom_tools_have_templates():
    registered = set(known_tool_kinds())
    missing = _EXPECTED_TOOLS - registered
    assert not missing, f"missing preamble templates: {sorted(missing)}"


@pytest.mark.parametrize("kind", sorted(_EXPECTED_TOOLS))
def test_each_template_returns_string(kind: str):
    out = synthesize_preamble(kind, "args-string")
    assert isinstance(out, str) and out


def test_unknown_tool_uses_default_template():
    desc = get_template("mcp_unknown")
    assert desc.tool_kind == "_default"


def test_non_string_kind_returns_default():
    assert get_template(None).tool_kind == "_default"
    assert get_template(42).tool_kind == "_default"


# ===========================================================================
# synthesize_preamble — fallback semantics
# ===========================================================================


def test_existing_preamble_passes_through_when_fallback_only():
    """Model-emitted preamble wins when present."""
    out = synthesize_preamble(
        "read_file", "foo.py",
        existing_preamble="I'll read the auth module to verify JWT logic",
    )
    assert "JWT logic" in out


def test_synthesized_when_existing_empty():
    out = synthesize_preamble(
        "read_file", "backend/auth.py",
        existing_preamble="",
    )
    assert "backend/auth.py" in out
    assert "I" in out  # first-person


def test_synthesized_when_existing_whitespace():
    out = synthesize_preamble("read_file", "x.py", existing_preamble="   ")
    assert "x.py" in out


def test_force_synthesis_when_fallback_only_false():
    out = synthesize_preamble(
        "read_file", "foo.py",
        fallback_only=False,
        existing_preamble="model-emitted preamble",
    )
    # Always synthesizes
    assert "foo.py" in out


def test_synthesize_handles_pathological_args():
    """Even garbage args produce a non-empty string — no crash."""
    out = synthesize_preamble("bash", "\x00\xff" * 50)
    assert isinstance(out, str) and len(out) > 0


def test_synthesize_handles_none_inputs():
    out = synthesize_preamble(None, None)
    assert isinstance(out, str) and len(out) > 0


# ===========================================================================
# Per-tool template content sanity
# ===========================================================================


@pytest.mark.parametrize("kind,arg,must_contain", [
    ("read_file", "backend/auth.py", "backend/auth.py"),
    ("search_code", "jwt.encode", "jwt.encode"),
    ("bash", "pytest -x", "pytest -x"),
    ("edit_file", "backend/foo.py", "backend/foo.py"),
    ("write_file", "new_file.py", "new_file.py"),
    ("ask_human", "Should I delete this?", "Should I delete this?"),
])
def test_template_includes_args(kind, arg, must_contain):
    out = synthesize_preamble(kind, arg)
    assert must_contain in out


def test_path_truncation_to_60_chars():
    long_path = "x" * 200
    out = synthesize_preamble("read_file", long_path)
    # Truncation marker must appear; result should not contain the
    # full 200-char string.
    assert "x" * 200 not in out
    assert "…" in out


# ===========================================================================
# IntentPrompter — schema + flag parsing
# ===========================================================================


def test_intent_schema_version():
    assert INTENT_PROMPTER_SCHEMA_VERSION == "intent_prompter.v1"


def test_intent_master_flag_default_on_post_graduation():
    """Slice 5 graduation flipped this default-true (2026-05-04)."""
    assert is_master_flag_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    # Empty / unset → default ON post-graduation
    ("", True),
    ("true", True), ("1", True), ("on", True), ("yes", True),
    ("garbage", True),  # not in off-token set → ON
    # Off-tokens
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_intent_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_master_flag_enabled() is expected


def test_default_timeout_5s():
    assert read_timeout_s() == 5.0


def test_timeout_clamped(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "0.1")
    assert read_timeout_s() == 0.5  # MIN
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "999")
    assert read_timeout_s() == 30.0  # MAX


def test_default_max_tokens_50():
    assert read_max_tokens() == 50


def test_max_tokens_clamped(monkeypatch):
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "0")
    assert read_max_tokens() == 10  # MIN
    monkeypatch.setenv(MAX_TOKENS_ENV_VAR, "9999")
    assert read_max_tokens() == 200  # MAX


# ===========================================================================
# IntentRequest + prompt construction
# ===========================================================================


def test_build_user_prompt_includes_op_id_goal_tier():
    req = IntentRequest(
        op_id="op-019d8",
        goal="Fix authentication JWT validation gap",
        risk_tier="notify_apply",
        target_files=("backend/auth.py", "tests/test_auth.py"),
    )
    prompt = build_user_prompt(req)
    assert "op-019d8" in prompt
    assert "JWT validation gap" in prompt
    assert "NOTIFY_APPLY" in prompt
    assert "backend/auth.py" in prompt


def test_build_user_prompt_handles_empty_goal():
    req = IntentRequest(
        op_id="op-x", goal="", risk_tier="safe_auto", target_files=(),
    )
    prompt = build_user_prompt(req)
    # Empty goal is replaced with a placeholder, not crashed
    assert "op-x" in prompt
    assert isinstance(prompt, str)


def test_build_user_prompt_truncates_to_5_files():
    files = tuple(f"f{i}.py" for i in range(20))
    req = IntentRequest(
        op_id="op-x", goal="test", risk_tier="safe_auto",
        target_files=files,
    )
    prompt = build_user_prompt(req)
    # Only first 5 should appear
    assert "f0.py" in prompt
    assert "f4.py" in prompt
    assert "f10.py" not in prompt


# ===========================================================================
# request_intent — short-circuits + failure modes
# ===========================================================================


def _req(**overrides) -> IntentRequest:
    base = dict(
        op_id="op-x",
        goal="Fix something",
        risk_tier="notify_apply",
        target_files=("foo.py",),
    )
    base.update(overrides)
    return IntentRequest(**base)


def test_request_intent_short_circuits_when_master_flag_off():
    """Master flag off → returns empty result without LLM call."""
    result = asyncio.get_event_loop().run_until_complete(
        request_intent(_req()),
    )
    assert result.prose == ""
    assert result.error == "master flag off"
    assert not result.succeeded


def test_request_intent_handles_provider_unavailable(monkeypatch):
    """If the provider import fails, returns empty result not crash."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    with mock.patch.dict("sys.modules", {
        "backend.core.ouroboros.governance.doubleword_provider": None,
    }):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent(_req()),
        )
    assert not result.succeeded
    # Either provider unavailable OR call-time error — both are
    # acceptable signals.
    assert result.error  # non-empty


def test_request_intent_timeout_returns_empty(monkeypatch):
    """Provider hanging → asyncio.wait_for timeout → empty result."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "0.5")

    class _SlowProvider:
        async def generate(self, **kwargs):
            await asyncio.sleep(2.0)  # exceeds the 0.5s timeout
            return "should not return"

    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _SlowProvider,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent(_req()),
        )
    assert result.error == "timeout"


def test_request_intent_success(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _OkProvider:
        async def generate(self, **kwargs):
            return "I'm going to fix the JWT validation by adding issuer check"

    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _OkProvider,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent(_req()),
        )
    assert result.succeeded
    assert "JWT validation" in result.prose


def test_request_intent_handles_provider_exception(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _RaisingProvider:
        async def generate(self, **kwargs):
            raise RuntimeError("provider exploded")

    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _RaisingProvider,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent(_req()),
        )
    assert not result.succeeded
    assert "exploded" in result.error or "raised" in result.error


def test_request_intent_handles_empty_response(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _EmptyProvider:
        async def generate(self, **kwargs):
            return "   "  # whitespace only

    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _EmptyProvider,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent(_req()),
        )
    assert not result.succeeded
    assert result.error == "empty response"


# ===========================================================================
# request_intent_and_emit — integrates with NarrativeChannel
# ===========================================================================


def test_intent_emits_into_channel_on_success(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _OkProvider:
        async def generate(self, **kwargs):
            return "I'll patch auth.py to verify JWT issuer"

    channel = NarrativeChannel(capacity=10)
    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _OkProvider,
    ):
        result = asyncio.get_event_loop().run_until_complete(
            request_intent_and_emit(_req(), channel=channel),
        )
    assert result.succeeded
    intents = channel.find_by_kind(NarrativeKind.INTENT)
    assert len(intents) == 1
    assert "JWT issuer" in intents[0].prose


def test_intent_does_not_emit_on_failure(monkeypatch):
    """Failed intent prompts must not stamp empty frames."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")

    class _RaisingProvider:
        async def generate(self, **kwargs):
            raise RuntimeError("nope")

    channel = NarrativeChannel(capacity=10)
    with mock.patch(
        "backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider",
        _RaisingProvider,
    ):
        asyncio.get_event_loop().run_until_complete(
            request_intent_and_emit(_req(), channel=channel),
        )
    assert channel.find_by_kind(NarrativeKind.INTENT) == ()


def test_intent_emit_with_master_flag_off_no_channel_change():
    """Master flag off → no LLM call, no channel emission."""
    channel = NarrativeChannel(capacity=10)
    asyncio.get_event_loop().run_until_complete(
        request_intent_and_emit(_req(), channel=channel),
    )
    assert len(channel) == 0
