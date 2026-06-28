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
