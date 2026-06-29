from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow import from scripts/ (repo root -> scripts/).
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Constraint 3 — Dynamic AST Mutation (no hardcoded strings).
# Load the target source's AST, inject a harmless deterministic no-op binding,
# compile-validate, return the mutated source. Deterministic in seed.
# ---------------------------------------------------------------------------

_SRC = "def add(a, b):\n    return a + b\n"


def test_build_deterministic_mutation_compiles_and_preserves_original():
    from a1_deterministic_fixture import build_deterministic_mutation

    out = build_deterministic_mutation(_SRC, seed=7)

    # It actually mutated the source.
    assert out != _SRC
    # The mutation is AST/compile valid (the pipeline proof).
    compile(out, "<fixture>", "exec")
    # Original behaviour is preserved verbatim (harmless mutation).
    assert "def add(a, b):" in out
    assert "return a + b" in out


def test_build_deterministic_mutation_is_deterministic_in_seed():
    from a1_deterministic_fixture import build_deterministic_mutation

    assert build_deterministic_mutation(_SRC, seed=7) == build_deterministic_mutation(
        _SRC, seed=7
    )


def test_build_deterministic_mutation_varies_with_seed():
    from a1_deterministic_fixture import build_deterministic_mutation

    assert build_deterministic_mutation(_SRC, seed=7) != build_deterministic_mutation(
        _SRC, seed=8
    )


def test_build_deterministic_mutation_rejects_unparseable_source():
    from a1_deterministic_fixture import build_deterministic_mutation

    with pytest.raises(SyntaxError):
        build_deterministic_mutation("def broken(:\n", seed=7)


# ---------------------------------------------------------------------------
# Constraint 1 — Cryptographic Network Airgap.
# Sever LLM-provider hosts (raise FatalAirgapException), but leave non-LLM
# hosts (e.g. GCS, which the telemetry sidecar needs) reachable.
# ---------------------------------------------------------------------------


def test_is_llm_provider_host_matches_providers_not_gcs():
    from a1_deterministic_fixture import DEFAULT_LLM_HOSTS, is_llm_provider_host

    assert is_llm_provider_host(
        "https://api.anthropic.com/v1/messages", DEFAULT_LLM_HOSTS
    )
    assert is_llm_provider_host(
        "https://api.doubleword.ai/v1/chat/completions", DEFAULT_LLM_HOSTS
    )
    # GCS must remain reachable — the sidecar streams to it during the fixture.
    assert not is_llm_provider_host(
        "https://storage.googleapis.com/bucket/obj", DEFAULT_LLM_HOSTS
    )


def test_airgap_severs_llm_send_without_invoking_real_send():
    from a1_deterministic_fixture import FatalAirgapException, LLMAirgap

    calls = []

    def fake_send(url):
        calls.append(url)
        return "real-network-result"

    wrapped = LLMAirgap(send=fake_send).wrap()

    with pytest.raises(FatalAirgapException):
        wrapped("https://api.anthropic.com/v1/messages")
    # The real send was never reached — proves severance, not just refusal.
    assert calls == []


def test_airgap_delegates_non_llm_send():
    from a1_deterministic_fixture import LLMAirgap

    calls = []

    def fake_send(url):
        calls.append(url)
        return "ok"

    wrapped = LLMAirgap(send=fake_send).wrap()

    assert wrapped("https://storage.googleapis.com/bucket/obj") == "ok"
    assert calls == ["https://storage.googleapis.com/bucket/obj"]


def test_fatal_airgap_exception_is_distinct_error_type():
    from a1_deterministic_fixture import FatalAirgapException

    assert issubclass(FatalAirgapException, Exception)


def test_default_hosts_block_vertex_and_openai_allow_gcs():
    from a1_deterministic_fixture import DEFAULT_LLM_HOSTS, is_llm_provider_host

    assert is_llm_provider_host(
        "https://aiplatform.googleapis.com/v1/projects/x", DEFAULT_LLM_HOSTS
    )
    assert is_llm_provider_host("https://api.openai.com/v1/chat", DEFAULT_LLM_HOSTS)
    # GCS sibling under googleapis.com must stay reachable (telemetry).
    assert not is_llm_provider_host(
        "https://storage.googleapis.com/bucket/obj", DEFAULT_LLM_HOSTS
    )


def test_airgapped_send_blocks_llm_delegates_others():
    from a1_deterministic_fixture import (
        DEFAULT_LLM_HOSTS,
        FatalAirgapException,
        _make_airgapped_send,
    )

    calls = []

    def orig(self, request, *a, **k):
        calls.append(request)
        return "sent"

    class _Req:
        def __init__(self, url):
            self.url = url

    send = _make_airgapped_send(orig, DEFAULT_LLM_HOSTS)

    with pytest.raises(FatalAirgapException):
        send(object(), _Req("https://api.anthropic.com/v1/messages"))
    assert calls == []  # real transport never reached

    assert send(object(), _Req("https://storage.googleapis.com/x")) == "sent"
    assert len(calls) == 1


def test_install_httpx_airgap_patches_and_restores():
    import httpx

    from a1_deterministic_fixture import install_httpx_airgap

    before_sync = httpx.Client.send
    before_async = httpx.AsyncClient.send

    uninstall = install_httpx_airgap()
    try:
        assert httpx.Client.send is not before_sync
        assert httpx.AsyncClient.send is not before_async
    finally:
        uninstall()

    assert httpx.Client.send is before_sync
    assert httpx.AsyncClient.send is before_async


def test_scoped_httpx_airgap_installs_only_within_scope():
    import asyncio

    import httpx

    from a1_deterministic_fixture import ScopedHttpxAirgap

    before = httpx.Client.send

    async def go():
        async with ScopedHttpxAirgap():
            # Severed ONLY inside the scope (boot probes outside are untouched).
            assert httpx.Client.send is not before

    asyncio.run(go())
    assert httpx.Client.send is before  # restored on exit


def test_scoped_httpx_airgap_severs_llm_inside_scope():
    import asyncio

    import httpx

    from a1_deterministic_fixture import FatalAirgapException, ScopedHttpxAirgap

    async def go():
        async with ScopedHttpxAirgap():
            c = httpx.Client()
            req = c.build_request("POST", "https://api.anthropic.com/v1/messages")
            with pytest.raises(FatalAirgapException):
                c.send(req)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Fixture candidate payload — the no-DW APPLY-path injection core.
# When fixture mode is active, produce a deterministic candidate (target file +
# AST-mutated content) WITHOUT any provider call. The orchestrator's real
# APPLY -> change_engine -> AutoCommitter -> VERIFY path consumes it unchanged.
# ---------------------------------------------------------------------------

_TARGET_SRC = "def add(a, b):\n    return a + b\n"
_FIXTURE_ENV = {
    "JARVIS_A1_FIXTURE_MODE": "1",
    "JARVIS_A1_FIXTURE_TARGET": "backend/util/math_util.py",
    "JARVIS_A1_FIXTURE_SEED": "7",
}


def test_fixture_payload_none_when_mode_off():
    from a1_deterministic_fixture import fixture_candidate_payload

    # Production-safe default: no env flag -> no fixture -> real generation runs.
    assert fixture_candidate_payload(env={}, read_file=lambda p: _TARGET_SRC) is None


def test_fixture_payload_none_when_target_missing():
    from a1_deterministic_fixture import fixture_candidate_payload

    # Fail-closed: mode on but no target -> None, never a half-formed candidate.
    assert (
        fixture_candidate_payload(
            env={"JARVIS_A1_FIXTURE_MODE": "1"}, read_file=lambda p: _TARGET_SRC
        )
        is None
    )


def test_fixture_payload_builds_mutated_candidate_for_target():
    from a1_deterministic_fixture import fixture_candidate_payload

    read = []

    def read_file(path):
        read.append(path)
        return _TARGET_SRC

    pc = fixture_candidate_payload(env=_FIXTURE_ENV, read_file=read_file)

    assert pc is not None
    assert pc.file_path == "backend/util/math_util.py"
    assert read == ["backend/util/math_util.py"]
    # Real mutated content, compile-valid, original preserved.
    assert pc.full_content != _TARGET_SRC
    compile(pc.full_content, "<fixture>", "exec")
    assert "def add(a, b):" in pc.full_content


def test_fixture_payload_is_deterministic():
    from a1_deterministic_fixture import fixture_candidate_payload

    a = fixture_candidate_payload(env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)
    b = fixture_candidate_payload(env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)
    assert a.full_content == b.full_content


def test_fixture_payload_full_content_is_compile_valid():
    # Pre-Flight AST Compilation Check (constraint #3): enforced in
    # build_deterministic_mutation (single source), so every fixture candidate
    # is guaranteed valid Python -> VERIFY never fails on a fixture syntax error.
    from a1_deterministic_fixture import fixture_candidate_payload

    pc = fixture_candidate_payload(env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)
    compile(pc.full_content, "<preflight>", "exec")


# ---------------------------------------------------------------------------
# FixtureGenerator — Decorator/DI drop-in. Wraps the real generator, delegates
# everything via __getattr__, overrides ONLY generate(). Production
# CandidateGenerator is never modified; downstream VALIDATE/APPLY runs blind.
# ---------------------------------------------------------------------------


class _FakeInner:
    provider_name = "real-dw"

    def __init__(self):
        self.generate_called = False

    async def generate(self, context, deadline):
        self.generate_called = True
        raise AssertionError("real generation must NOT run under the fixture")

    def some_other_method(self):
        return "delegated"


def test_fixture_generator_returns_candidate_without_invoking_inner():
    import asyncio

    from a1_deterministic_fixture import FixtureGenerator

    inner = _FakeInner()
    fg = FixtureGenerator(inner, env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)

    res = asyncio.run(fg.generate(context=object(), deadline=None))

    # The real generator was never reached.
    assert inner.generate_called is False
    # Faithful GenerationResult shape: candidates are dicts with file_path/full_content.
    cand = res.candidates[0]
    assert cand["file_path"] == "backend/util/math_util.py"
    assert cand["full_content"] != _TARGET_SRC
    assert res.provider_name  # a non-empty fixture provider label


def test_fixture_generator_delegates_unknown_attrs_to_inner():
    from a1_deterministic_fixture import FixtureGenerator

    fg = FixtureGenerator(_FakeInner(), env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)
    # Any method the orchestrator calls other than generate() falls through.
    assert fg.some_other_method() == "delegated"
    assert fg.provider_name == "real-dw"


def test_fixture_generator_failsafes_to_inner_when_mode_off():
    import asyncio

    from a1_deterministic_fixture import FixtureGenerator

    class _InnerOK:
        provider_name = "real-dw"

        def __init__(self):
            self.called = False

        async def generate(self, context, deadline):
            self.called = True
            return "real-generation-result"

    inner = _InnerOK()
    # Fixture mode OFF -> must delegate to the real generator, never fabricate.
    fg = FixtureGenerator(inner, env={}, read_file=lambda p: _TARGET_SRC)
    out = asyncio.run(fg.generate(context=object(), deadline=None))
    assert inner.called is True
    assert out == "real-generation-result"


# ---------------------------------------------------------------------------
# Factory-level DI overlay — swap holder._generator for FixtureGenerator IN
# PLACE when fixture mode is active. Applied at the GovernedLoopService
# generator-construction seam; default-off => byte-identical.
# ---------------------------------------------------------------------------


class _Holder:
    def __init__(self, gen):
        self._generator = gen


def test_overlay_wraps_generator_when_fixture_active():
    from a1_deterministic_fixture import FixtureGenerator, apply_fixture_generator_overlay

    inner = _FakeInner()
    h = _Holder(inner)

    applied = apply_fixture_generator_overlay(
        h, env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC
    )

    assert applied is True
    assert isinstance(h._generator, FixtureGenerator)
    # The wrapper still delegates unknown attrs to the real generator.
    assert h._generator.provider_name == "real-dw"


def test_overlay_is_noop_when_fixture_off():
    from a1_deterministic_fixture import apply_fixture_generator_overlay

    inner = _FakeInner()
    h = _Holder(inner)

    applied = apply_fixture_generator_overlay(h, env={}, read_file=lambda p: _TARGET_SRC)

    assert applied is False
    assert h._generator is inner  # untouched -> byte-identical production path


def test_overlay_is_noop_when_generator_not_built():
    from a1_deterministic_fixture import apply_fixture_generator_overlay

    h = _Holder(None)
    applied = apply_fixture_generator_overlay(
        h, env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC
    )
    assert applied is False
    assert h._generator is None


# ---------------------------------------------------------------------------
# Fail-Closed overlay — under fixture mode, activation failure MUST hard-crash,
# never silently fall back to the live LLM generator (the A1 run #15 bug).
# ---------------------------------------------------------------------------


def test_overlay_or_raise_is_noop_when_fixture_off():
    from a1_deterministic_fixture import apply_fixture_overlay_or_raise

    h = _Holder(_FakeInner())
    apply_fixture_overlay_or_raise(h, env={}, read_file=lambda p: _TARGET_SRC)
    assert h._generator.__class__ is _FakeInner  # untouched, no raise


def test_overlay_or_raise_applies_when_ok():
    from a1_deterministic_fixture import FixtureGenerator, apply_fixture_overlay_or_raise

    h = _Holder(_FakeInner())
    apply_fixture_overlay_or_raise(h, env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC)
    assert isinstance(h._generator, FixtureGenerator)


def test_overlay_or_raise_FAILS_CLOSED_when_unapplied():
    from a1_deterministic_fixture import (
        FatalFixtureConfigurationError,
        apply_fixture_overlay_or_raise,
    )

    # Fixture mode ON but generator not built -> cannot apply -> MUST raise,
    # never silently continue to the live generator.
    with pytest.raises(FatalFixtureConfigurationError):
        apply_fixture_overlay_or_raise(
            _Holder(None), env=_FIXTURE_ENV, read_file=lambda p: _TARGET_SRC
        )


def test_fatal_fixture_configuration_error_is_exception():
    from a1_deterministic_fixture import FatalFixtureConfigurationError

    assert issubclass(FatalFixtureConfigurationError, Exception)


# ---------------------------------------------------------------------------
# OS-agnostic interceptor — logical parity, not blind string copy. Maps the
# cloud node's /opt/trinity/jarvis repo prefix to the local root under
# JARVIS_LOCAL_MODE so a composed cloud env runs faithfully on macOS.
# ---------------------------------------------------------------------------


def test_remap_cloud_paths_is_noop_when_disabled():
    from a1_deterministic_fixture import remap_cloud_paths_for_local

    env = {"X": "/opt/trinity/jarvis/foo"}
    out = remap_cloud_paths_for_local(env, local_root="/Users/me/repo", enabled=False)
    assert out["X"] == "/opt/trinity/jarvis/foo"  # untouched


def test_remap_cloud_paths_maps_cloud_prefix_to_local_root():
    from a1_deterministic_fixture import remap_cloud_paths_for_local

    env = {"A": "/opt/trinity/jarvis/foo", "B": "/other/path", "C": "notapath", "N": 7}
    out = remap_cloud_paths_for_local(env, local_root="/Users/me/repo", enabled=True)
    assert out["A"] == "/Users/me/repo/foo"  # cloud repo prefix -> local root
    assert out["B"] == "/other/path"  # unrelated abs path untouched
    assert out["C"] == "notapath"
    assert out["N"] == 7  # non-str values pass through


# ---------------------------------------------------------------------------
# Strict Subprocess Contract — fail-fast on a mangled fixture config BEFORE the
# node boots, so we never burn a node window on a misconfigured run.
# ---------------------------------------------------------------------------


def test_validate_fixture_config_passes_when_mode_off():
    from a1_deterministic_fixture import validate_fixture_config

    validate_fixture_config({})  # nothing required when fixture is inactive


def test_validate_fixture_config_requires_target_and_seed():
    from a1_deterministic_fixture import validate_fixture_config

    with pytest.raises(ValueError):
        validate_fixture_config({"JARVIS_A1_FIXTURE_MODE": "1"})


def test_validate_fixture_config_rejects_non_py_target():
    from a1_deterministic_fixture import validate_fixture_config

    with pytest.raises(ValueError):
        validate_fixture_config(
            {
                "JARVIS_A1_FIXTURE_MODE": "1",
                "JARVIS_A1_FIXTURE_TARGET": "not_a_python_file",
                "JARVIS_A1_FIXTURE_SEED": "7",
            }
        )


def test_validate_fixture_config_rejects_non_integer_seed():
    from a1_deterministic_fixture import validate_fixture_config

    with pytest.raises(ValueError):
        validate_fixture_config(
            {
                "JARVIS_A1_FIXTURE_MODE": "1",
                "JARVIS_A1_FIXTURE_TARGET": "a/b.py",
                "JARVIS_A1_FIXTURE_SEED": "not-an-int",
            }
        )


def test_validate_fixture_config_rejects_bad_gcs_uri():
    from a1_deterministic_fixture import validate_fixture_config

    with pytest.raises(ValueError):
        validate_fixture_config(
            {
                "JARVIS_A1_FIXTURE_MODE": "1",
                "JARVIS_A1_FIXTURE_TARGET": "a/b.py",
                "JARVIS_A1_FIXTURE_SEED": "7",
                "JARVIS_A1_GCS_TELEMETRY_TARGET": "/not/a/gs/uri",
            }
        )


def test_validate_fixture_config_accepts_full_valid_config():
    from a1_deterministic_fixture import validate_fixture_config

    validate_fixture_config(
        {
            "JARVIS_A1_FIXTURE_MODE": "1",
            "JARVIS_A1_FIXTURE_TARGET": "backend/util/math_util.py",
            "JARVIS_A1_FIXTURE_SEED": "7",
            "JARVIS_A1_GCS_TELEMETRY_TARGET": "gs://bucket/a1/logs",
        }
    )
