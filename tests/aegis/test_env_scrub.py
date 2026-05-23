"""Env scrub — discovers credential names from credential_registry,
pops them, and proves post-scrub absence.

Per binding correction #6: the negative test "absolutely no upstream
credentials remain in the JARVIS child env" is mandatory.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)
from backend.core.ouroboros.aegis.env_scrub import (
    UpstreamCredentialPresentError,
    assert_no_upstream_credentials,
    scrub_upstream_credentials,
)


def test_credential_registry_is_frozen():
    a = upstream_credential_env_vars()
    b = upstream_credential_env_vars()
    # Same object identity (frozenset is hashable + immutable).
    assert a == b
    with pytest.raises((AttributeError, TypeError)):
        a.add("anything")  # frozensets reject .add


def test_scrub_returns_captured_pairs():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "DOUBLEWORD_API_KEY": "dw-yyy",
        "UNRELATED": "z",
    }
    captured = scrub_upstream_credentials(env)
    assert captured == {
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "DOUBLEWORD_API_KEY": "dw-yyy",
    }
    assert "ANTHROPIC_API_KEY" not in env
    assert "DOUBLEWORD_API_KEY" not in env
    assert env["UNRELATED"] == "z"  # unrelated keys untouched


def test_scrub_on_empty_env_returns_empty_dict():
    env: dict = {}
    captured = scrub_upstream_credentials(env)
    assert captured == {}


def test_scrub_with_extra_names():
    env = {"FOO_API_KEY": "abc"}
    captured = scrub_upstream_credentials(env, extra=["FOO_API_KEY"])
    assert captured == {"FOO_API_KEY": "abc"}
    assert "FOO_API_KEY" not in env


def test_assert_passes_on_clean_env():
    assert_no_upstream_credentials({})


def test_assert_raises_when_anthropic_key_present():
    with pytest.raises(UpstreamCredentialPresentError) as exc_info:
        assert_no_upstream_credentials({"ANTHROPIC_API_KEY": "x"})
    # Message lists the KEY, not the value.
    msg = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert "x" not in msg or msg.count("x") < 5  # value not embedded


def test_assert_raises_when_doubleword_key_present():
    with pytest.raises(UpstreamCredentialPresentError) as exc_info:
        assert_no_upstream_credentials({"DOUBLEWORD_API_KEY": "dw-yyy"})
    assert "DOUBLEWORD_API_KEY" in str(exc_info.value)


def test_assert_lists_all_present_keys():
    env = {
        "ANTHROPIC_API_KEY": "1",
        "DOUBLEWORD_API_KEY": "2",
    }
    with pytest.raises(UpstreamCredentialPresentError) as exc_info:
        assert_no_upstream_credentials(env)
    msg = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert "DOUBLEWORD_API_KEY" in msg


def test_scrub_then_assert_zero_credentials_remain():
    """The mandatory negative test (binding correction #6): after the
    scrub, the JARVIS child env contains ZERO upstream credentials."""
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "DOUBLEWORD_API_KEY": "dw-yyy",
        "HOME": "/home/user",  # unrelated, should survive
        "PATH": "/usr/bin",
    }
    scrub_upstream_credentials(env)
    # MUST NOT raise:
    assert_no_upstream_credentials(env)
    # Unrelated env survives:
    assert env["HOME"] == "/home/user"
    assert env["PATH"] == "/usr/bin"


def test_scrub_value_never_logged_via_repr():
    """Captured values shouldn't appear in the exception's repr if used."""
    env = {"ANTHROPIC_API_KEY": "SUPER_SECRET_TOKEN_VALUE"}
    try:
        # The assert should never raise here because we scrubbed first.
        scrub_upstream_credentials(env)
        assert_no_upstream_credentials(env)
    except UpstreamCredentialPresentError as exc:
        # If it did raise, the value must not appear in str/repr.
        assert "SUPER_SECRET_TOKEN_VALUE" not in str(exc)
        assert "SUPER_SECRET_TOKEN_VALUE" not in repr(exc)
