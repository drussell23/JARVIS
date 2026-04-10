"""Tests for SecurityReviewer._client_is_compatible — prevents the
battle test bt-2026-04-10-184157 regression where SecurityReviewer was
wired with a CandidateGenerator (whose ``generate(context, deadline)``
signature crashed the review loop with
``TypeError: generate() got an unexpected keyword argument 'prompt'``).

The reviewer must refuse to enable itself with a client that doesn't
accept a ``prompt`` kwarg — silent PASS on every op is the opposite of
what this gate is for (Manifesto §7 — absolute observability).
"""

from __future__ import annotations

from typing import Any

from backend.core.ouroboros.governance.security_reviewer import SecurityReviewer


class _FakePrimeClient:
    """Mimics PrimeClient.generate(prompt=..., system_prompt=..., ...)."""

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.1,
        model_name: Any = None,
        task_profile: Any = None,
    ) -> Any:  # pragma: no cover - not invoked in compat check
        return None


class _FakeCandidateGenerator:
    """Mimics CandidateGenerator.generate(context, deadline) — INCOMPATIBLE."""

    async def generate(self, context: Any, deadline: Any) -> Any:  # pragma: no cover
        return None


class _NoGenerateAtAll:
    pass


class _FakeClientWithKwargs:
    """generate(**kwargs) — accepted via VAR_KEYWORD fallback."""

    async def generate(self, **kwargs: Any) -> Any:  # pragma: no cover
        return None


def test_none_client_disables_reviewer() -> None:
    reviewer = SecurityReviewer(prime_client=None)
    assert reviewer.is_enabled is False


def test_prime_client_style_enables_reviewer() -> None:
    reviewer = SecurityReviewer(prime_client=_FakePrimeClient())
    assert reviewer.is_enabled is True


def test_candidate_generator_style_disables_reviewer() -> None:
    """Regression: CandidateGenerator.generate(context, deadline) must NOT
    be accepted — that mismatch silently PASSed every op in bt-2026-04-10-184157.
    """
    reviewer = SecurityReviewer(prime_client=_FakeCandidateGenerator())
    assert reviewer.is_enabled is False


def test_client_without_generate_method_disables_reviewer() -> None:
    reviewer = SecurityReviewer(prime_client=_NoGenerateAtAll())
    assert reviewer.is_enabled is False


def test_client_with_kwargs_generate_accepted() -> None:
    """Libraries that use generate(**kwargs) should still work — we only
    reject when ``prompt`` is demonstrably absent AND there's no VAR_KEYWORD."""
    reviewer = SecurityReviewer(prime_client=_FakeClientWithKwargs())
    assert reviewer.is_enabled is True


def test_enabled_false_flag_overrides_compatible_client() -> None:
    reviewer = SecurityReviewer(prime_client=_FakePrimeClient(), enabled=False)
    assert reviewer.is_enabled is False
