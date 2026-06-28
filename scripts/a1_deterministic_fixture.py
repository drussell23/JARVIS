"""a1_deterministic_fixture -- Deterministic Fast-Forward fixture for the A1 harness.

The ``--inject-deterministic-fixture`` ("Fast-Forward") mode proves the
file-isolation / ``written=True`` durable-commit git plumbing on the real cloud
node in seconds, fully decoupled from DoubleWord network latency. It boots O+V
normally but feeds a deterministic, AST-validated candidate through the REAL
APPLY -> change_engine -> AutoCommitter -> VERIFY path -- never a bespoke
AutoCommitter shortcut.

This module holds the pure, independently-testable units that mode is built from:

  * ``build_deterministic_mutation`` (Constraint 3) -- programmatically load the
    target source's AST, inject a HARMLESS deterministic no-op binding, compile
    to validate, and return the mutated source. No hardcoded patch strings; the
    AST pipeline is genuinely exercised.

Further units (network airgap, fixture envelope composition) live alongside as
they are driven out test-first.
"""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, Optional
from urllib.parse import urlparse

# Name of the harmless module-level binding injected by the fixture mutation.
_SENTINEL_NAME = "_A1_FIXTURE_SENTINEL"


# --------------------------------------------------------------------------- #
# Constraint 1 — Cryptographic Network Airgap.
# --------------------------------------------------------------------------- #


class FatalAirgapException(RuntimeError):
    """Raised when an LLM-provider network call is attempted under the fixture
    airgap. A passing fixture run therefore PROVES no LLM call physically
    occurred -- the decoupling is structural, not a promise."""


# Default LLM-provider hosts severed under the airgap. GCS / metadata / other
# infra hosts are deliberately absent so the telemetry sidecar keeps streaming.
# Extend (never replace) via JARVIS_AIRGAP_LLM_HOSTS (comma-separated).
DEFAULT_LLM_HOSTS: FrozenSet[str] = frozenset(
    {
        "api.anthropic.com",
        "api.doubleword.ai",
        "api.openai.com",
        # GCP Vertex / Gemini LLM surfaces — DISTINCT from storage.googleapis.com
        # (the GCS telemetry host), which is deliberately NOT severed.
        "aiplatform.googleapis.com",
        "generativelanguage.googleapis.com",
    }
)


def llm_hosts_from_env(default: FrozenSet[str] = DEFAULT_LLM_HOSTS) -> FrozenSet[str]:
    """Default hosts unioned with any from ``JARVIS_AIRGAP_LLM_HOSTS`` (additive)."""
    raw = os.environ.get("JARVIS_AIRGAP_LLM_HOSTS", "") or ""
    extra = {h.strip() for h in raw.split(",") if h.strip()}
    return frozenset(set(default) | extra)


def is_llm_provider_host(url: str, hosts: FrozenSet[str]) -> bool:
    """True if ``url``'s host is (a subdomain of) a severed LLM-provider host."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


class LLMAirgap:
    """Wraps a ``send`` callable so calls to LLM-provider hosts raise
    ``FatalAirgapException`` while all other hosts pass through untouched."""

    def __init__(
        self,
        send: Callable[..., object],
        hosts: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._send = send
        self._hosts = hosts if hosts is not None else llm_hosts_from_env()

    def wrap(self) -> Callable[..., object]:
        send = self._send
        hosts = self._hosts

        def guarded(url, *args, **kwargs):
            if is_llm_provider_host(str(url), hosts):
                raise FatalAirgapException(
                    "fixture airgap: LLM provider call severed -> %s" % (url,)
                )
            return send(url, *args, **kwargs)

        return guarded


def _make_airgapped_send(orig_send: Callable, hosts: FrozenSet[str]) -> Callable:
    """Wrap an httpx-style ``send(self, request, ...)`` so a request whose URL
    host is an LLM provider raises ``FatalAirgapException`` before the transport
    is reached; all other hosts (including GCS) delegate to ``orig_send``."""

    def send(self, request, *args, **kwargs):
        url = str(getattr(request, "url", ""))
        if is_llm_provider_host(url, hosts):
            raise FatalAirgapException(
                "fixture airgap: LLM provider call severed -> %s" % (url,)
            )
        return orig_send(self, request, *args, **kwargs)

    return send


def install_httpx_airgap(*, hosts: Optional[FrozenSet[str]] = None) -> Callable[[], None]:
    """Universal Transport Interceptor: patch ``httpx.Client.send`` and
    ``httpx.AsyncClient.send`` so any LLM-provider request raises
    ``FatalAirgapException`` while GCS / other hosts pass through. Returns an
    ``uninstall()`` callable that restores the originals."""
    import httpx

    resolved = hosts if hosts is not None else llm_hosts_from_env()
    orig_sync = httpx.Client.send
    orig_async = httpx.AsyncClient.send

    sync_send = _make_airgapped_send(orig_sync, resolved)

    async def async_send(self, request, *args, **kwargs):
        url = str(getattr(request, "url", ""))
        if is_llm_provider_host(url, resolved):
            raise FatalAirgapException(
                "fixture airgap: LLM provider call severed -> %s" % (url,)
            )
        return await orig_async(self, request, *args, **kwargs)

    httpx.Client.send = sync_send  # type: ignore[assignment]
    httpx.AsyncClient.send = async_send  # type: ignore[assignment]

    def uninstall() -> None:
        httpx.Client.send = orig_sync  # type: ignore[assignment]
        httpx.AsyncClient.send = orig_async  # type: ignore[assignment]

    return uninstall


def _seed_value(seed: int) -> int:
    """Deterministic, seed-varying integer (stable across processes/runs)."""
    digest = hashlib.sha256(str(int(seed)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def build_deterministic_mutation(source_code: str, *, seed: int) -> str:
    """Return ``source_code`` with a harmless, deterministic AST mutation applied.

    Parses the source (raising ``SyntaxError`` on unparseable input), appends a
    module-level no-op sentinel binding whose value is derived from ``seed``,
    re-validates by compiling, and returns the unparsed result. The original
    definitions are preserved verbatim; only a new harmless binding is added.
    """
    tree = ast.parse(source_code)  # raises SyntaxError on malformed source
    sentinel = ast.Assign(
        targets=[ast.Name(id=_SENTINEL_NAME, ctx=ast.Store())],
        value=ast.Constant(value=_seed_value(seed)),
    )
    tree.body.append(sentinel)
    ast.fix_missing_locations(tree)
    mutated = ast.unparse(tree)
    compile(mutated, "<a1-fixture>", "exec")  # validate the mutated AST/source
    return mutated


# --------------------------------------------------------------------------- #
# Fixture candidate payload — the no-DW APPLY-path injection core.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FixtureCandidate:
    """A deterministic candidate the orchestrator's REAL APPLY path consumes.
    Carries the same ``file_path`` / ``full_content`` shape the generator emits,
    so APPLY -> change_engine -> AutoCommitter -> VERIFY run unchanged."""

    file_path: str
    full_content: str
    rationale: str = "a1-deterministic-fixture: harmless AST mutation (zero LLM)"


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def fixture_candidate_payload(
    *,
    env: "os._Environ[str] | dict",
    read_file: Callable[[str], str],
) -> Optional[FixtureCandidate]:
    """Return a deterministic fixture candidate when A1 fixture mode is active,
    else ``None`` (the production-safe default -> real generation runs).

    Reads ``JARVIS_A1_FIXTURE_MODE`` / ``_TARGET`` / ``_SEED`` from ``env`` and
    the target's current source via the injected ``read_file``. No provider
    call is ever made. Fail-closed: a missing target yields ``None``."""
    if not _truthy(env.get("JARVIS_A1_FIXTURE_MODE", "")):
        return None
    target = (env.get("JARVIS_A1_FIXTURE_TARGET", "") or "").strip()
    if not target:
        return None
    seed = int(env.get("JARVIS_A1_FIXTURE_SEED", "0") or "0")
    mutated = build_deterministic_mutation(read_file(target), seed=seed)
    return FixtureCandidate(file_path=target, full_content=mutated)


def _default_read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# Label stamped on fixture-generated results so telemetry/AutoCommitter show
# the candidate's provenance (never a real provider name).
FIXTURE_PROVIDER_NAME = "a1-fixture-generator"


class FixtureGenerator:
    """Decorator over the real candidate generator (DI / Strategy).

    Delegates every attribute to the wrapped generator via ``__getattr__`` and
    overrides ONLY ``generate``: under fixture mode it returns a deterministic
    ``GenerationResult`` with zero provider calls; otherwise it fail-safes to the
    wrapped generator. The production ``CandidateGenerator`` is never modified,
    and the downstream VALIDATE -> APPLY -> AutoCommitter pipeline runs unaware
    the generator was swapped."""

    def __init__(self, inner: Any, *, env=None, read_file=None) -> None:
        self._inner = inner
        self._env = env if env is not None else os.environ
        self._read_file = read_file if read_file is not None else _default_read_file

    def __getattr__(self, name: str):
        # Reached only for attrs not defined on FixtureGenerator -> delegate.
        # Fetch _inner via __getattribute__ to avoid __getattr__ recursion.
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)

    async def generate(self, context, deadline):
        payload = fixture_candidate_payload(env=self._env, read_file=self._read_file)
        if payload is None:
            # Fixture inactive -> never fabricate; defer to the real generator.
            return await self._inner.generate(context, deadline)
        from backend.core.ouroboros.governance.op_context import GenerationResult

        return GenerationResult(
            candidates=(
                {
                    "file_path": payload.file_path,
                    "full_content": payload.full_content,
                    "rationale": payload.rationale,
                },
            ),
            provider_name=FIXTURE_PROVIDER_NAME,
            generation_duration_s=0.0,
        )


def apply_fixture_generator_overlay(
    holder: Any,
    *,
    env=None,
    read_file: Optional[Callable[[str], str]] = None,
) -> bool:
    """Factory-level DI swap: when fixture mode is active, wrap
    ``holder._generator`` with :class:`FixtureGenerator` in place and return
    ``True``. Default-off and a no-op when the generator is not yet built ->
    the production path stays byte-identical. Called at the GovernedLoopService
    generator-construction seam; production ``CandidateGenerator`` is untouched."""
    env = env if env is not None else os.environ
    if not _truthy(env.get("JARVIS_A1_FIXTURE_MODE", "")):
        return False
    inner = getattr(holder, "_generator", None)
    if inner is None:
        return False
    holder._generator = FixtureGenerator(inner, env=env, read_file=read_file)
    return True


def validate_fixture_config(env) -> None:
    """Strict Subprocess Contract: fail-fast (``ValueError``) on a mangled fixture
    config BEFORE a node is provisioned. No-op when fixture mode is inactive."""
    if not _truthy(env.get("JARVIS_A1_FIXTURE_MODE", "")):
        return
    target = (env.get("JARVIS_A1_FIXTURE_TARGET", "") or "").strip()
    if not target:
        raise ValueError(
            "JARVIS_A1_FIXTURE_TARGET is required when JARVIS_A1_FIXTURE_MODE is set"
        )
    if not target.endswith(".py"):
        raise ValueError("JARVIS_A1_FIXTURE_TARGET must be a .py path: %r" % (target,))
    seed = env.get("JARVIS_A1_FIXTURE_SEED", "")
    try:
        int(seed)
    except (TypeError, ValueError):
        raise ValueError(
            "JARVIS_A1_FIXTURE_SEED must be an integer: %r" % (seed,)
        ) from None
    gcs = (env.get("JARVIS_A1_GCS_TELEMETRY_TARGET", "") or "").strip()
    if gcs and not gcs.startswith("gs://"):
        raise ValueError(
            "JARVIS_A1_GCS_TELEMETRY_TARGET must be a gs:// URI: %r" % (gcs,)
        )
