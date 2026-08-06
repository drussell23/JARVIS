"""
Regression spine for the cleartext-credential eradication sweep.

Two kinds of assertion live here, and the distinction is load-bearing:

* **Behavioural** -- calling a former retrieval path raises rather than
  returning ``None``. A soft return would let a caller fall through to the next
  branch of a cascade and rediscover the same dead end.
* **Structural** -- the eradicated call shapes are absent from the live unlock
  modules' ASTs. This is what actually prevents resurrection. Deleting code does
  not stop it being rewritten; a test that fails when it comes back does.

The structural checks parse source rather than importing, so they stay fast and
do not depend on the heavyweight import graphs of ``main``/``macos_controller``.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.security.credential_eradication import (
    CleartextCredentialEradicated,
    SecurityError,
    eradicated_credentials,
    eradicated_env_keys,
    eradicated_path,
    sanitize_mapping,
    sanitize_process_environment,
    zero_buffer,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Modules that formerly sat on the live "unlock my screen" path. These are the
#: files the sweep gutted; the structural guard is scoped to them so it stays
#: decidable and does not police ad-hoc debug scripts.
LIVE_UNLOCK_MODULES = (
    "backend/macos_keychain_unlock.py",
    "backend/api/simple_unlock_handler.py",
    "backend/system_control/macos_controller.py",
    "backend/voice_unlock/secure_password_typer.py",
)


def _module_sources():
    for rel in LIVE_UNLOCK_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"guarded module vanished: {rel}"
        yield rel, path.read_text()


def _string_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


# =============================================================================
# BEHAVIOURAL — the old fetchers refuse instead of fetching
# =============================================================================


def test_eradicated_path_raises_security_error():
    with pytest.raises(CleartextCredentialEradicated) as excinfo:
        eradicated_path("probe.subject")

    assert isinstance(excinfo.value, SecurityError)
    assert "zero-trust compliance" in str(excinfo.value)
    assert "probe.subject" in str(excinfo.value)


@pytest.mark.parametrize(
    "method_name",
    [
        "get_password_from_keychain",
        "get_password_hash",
        "preload_cache",
        "_fetch_password_async",
        "_query_keychain_async",
    ],
)
def test_keychain_service_methods_raise(method_name, monkeypatch):
    """
    The former fetchers must raise -- and must not shell out on the way.

    Both halves matter. A version that raised *after* running
    ``security find-generic-password`` would still have put the credential in a
    subprocess pipe, so the subprocess spawner is booby-trapped here.
    """
    from backend import macos_keychain_unlock

    def _explode(*args, **kwargs):
        raise AssertionError(
            "eradicated path attempted to spawn a subprocess before raising"
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _explode)

    service = macos_keychain_unlock.MacOSKeychainUnlock()
    method = getattr(service, method_name)
    args = ("svc", "acct") if method_name == "_query_keychain_async" else ()

    with pytest.raises(CleartextCredentialEradicated):
        asyncio.run(method(*args))


@pytest.mark.parametrize(
    "func_name",
    ["get_password_async", "get_password_hash_async", "preload_keychain_cache"],
)
def test_module_level_helpers_raise(func_name):
    from backend import macos_keychain_unlock

    with pytest.raises(CleartextCredentialEradicated):
        asyncio.run(getattr(macos_keychain_unlock, func_name)())


def test_unlock_screen_reports_rather_than_raises():
    """
    ``unlock_screen`` sits on the spoken-response path, so it reports.

    The operator needs a sentence they can hear, not a traceback. The credential
    paths beneath it raise; this one seam degrades deliberately.
    """
    from backend import macos_keychain_unlock

    service = macos_keychain_unlock.MacOSKeychainUnlock()
    result = asyncio.run(service.unlock_screen(verified_speaker="Derek J. Russell"))

    assert result["success"] is False
    assert result["action"] == "eradicated"
    assert "SecureEventInput" in result["message"]


# =============================================================================
# STRUCTURAL — the shapes cannot come back
# =============================================================================


def test_no_live_module_references_the_eradicated_keychain_item():
    """No live unlock module may name the eradicated credential coordinates."""
    service, account = eradicated_credentials()[0].coordinates

    for rel, source in _module_sources():
        tree = ast.parse(source)
        constants = set(_string_constants(tree))
        assert service not in constants, f"{rel} still references keychain service {service!r}"
        assert account not in constants, f"{rel} still references keychain account {account!r}"


def test_no_live_module_invokes_find_generic_password():
    for rel, source in _module_sources():
        tree = ast.parse(source)
        assert "find-generic-password" not in set(_string_constants(tree)), (
            f"{rel} reintroduced a `security find-generic-password` invocation"
        )


def test_no_live_module_injects_credentials_into_a_child_environment():
    """
    ``JARVIS_UNLOCK_PASS`` must not appear as a runtime string.

    Docstrings are exempt on purpose -- the gutted functions explain what they
    used to do, and that explanation is the point. Only executable string
    constants are policed.
    """
    for rel, source in _module_sources():
        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        live = [
            value
            for value in _string_constants(tree)
            if value not in docstrings and "JARVIS_UNLOCK_PASS" in value
        ]
        assert not live, f"{rel} reintroduced JARVIS_UNLOCK_PASS as a runtime string"


# =============================================================================
# ENVIRONMENT SWEEP
# =============================================================================


def test_sanitize_removes_legacy_keys_and_reports_them():
    env = {
        "JARVIS_UNLOCK_PASS": "hunter2",
        "JARVIS_UNLOCK_PASSWORD": "hunter2",
        "ANTHROPIC_API_KEY": "sk-keep-me",
        "PATH": "/usr/bin",
    }

    removed = sanitize_process_environment(env)

    assert removed == frozenset({"JARVIS_UNLOCK_PASS", "JARVIS_UNLOCK_PASSWORD"})
    assert "JARVIS_UNLOCK_PASS" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-keep-me", "sweep must not strip provider keys"
    assert env["PATH"] == "/usr/bin"


def test_sanitize_is_idempotent():
    env = {"JARVIS_UNLOCK_PASS": "hunter2"}

    assert sanitize_process_environment(env) == frozenset({"JARVIS_UNLOCK_PASS"})
    assert sanitize_process_environment(env) == frozenset()


def test_pattern_catches_unenumerated_credential_shapes():
    env = {"JARVIS_VOICE_UNLOCK_SECRET": "x", "JARVIS_TTS_MAX_QUEUE_S": "12"}

    assert "JARVIS_VOICE_UNLOCK_SECRET" in eradicated_env_keys(env)
    assert "JARVIS_TTS_MAX_QUEUE_S" not in eradicated_env_keys(env)


def test_operator_declared_keys_extend_rather_than_replace():
    env = {"JARVIS_ERADICATED_ENV_KEYS": "CUSTOM_LEGACY_SECRET"}
    keys = eradicated_env_keys(env)

    assert "CUSTOM_LEGACY_SECRET" in keys
    assert "JARVIS_UNLOCK_PASS" in keys, "builtin registry must survive an extension"


def test_sanitize_mapping_clears_config_singletons():
    singleton = {"JARVIS_UNLOCK_PASS": bytearray(b"hunter2"), "keep": "yes"}

    removed = sanitize_mapping(singleton, ["JARVIS_UNLOCK_PASS"])

    assert removed == frozenset({"JARVIS_UNLOCK_PASS"})
    assert singleton == {"keep": "yes"}


# =============================================================================
# HONESTY — zeroing claims must match what CPython can actually do
# =============================================================================


def test_zero_buffer_zeroes_writable_buffers():
    buf = bytearray(b"hunter2")

    assert zero_buffer(buf) is True
    assert bytes(buf) == b"\x00" * 7


def test_zero_buffer_reports_false_for_immutable_strings():
    """
    A ``str`` cannot be overwritten in place, and this must not pretend it can.

    The guarantee the sweep offers is structural (no code path loads a
    credential), not hygienic. A ``zero_buffer`` that returned True for ``str``
    would be exactly the kind of security theatre the sweep exists to remove.
    """
    assert zero_buffer("hunter2") is False
    assert zero_buffer(b"hunter2") is False
