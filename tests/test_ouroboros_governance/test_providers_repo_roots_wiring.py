"""Tests for repo_roots wiring in PrimeProvider and ClaudeProvider (Task 6)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# PrimeProvider repo_roots wiring
# ---------------------------------------------------------------------------


def test_prime_provider_accepts_repo_roots():
    """PrimeProvider.__init__ must accept and store repo_roots."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    roots = {"jarvis": Path("/repos/jarvis"), "prime": Path("/repos/prime")}
    provider = PrimeProvider(
        prime_client=MagicMock(),
        repo_roots=roots,
    )
    assert provider._repo_roots == roots


async def test_prime_provider_passes_repo_roots_to_prompt_and_parse(tmp_path):
    """PrimeProvider.generate() must forward repo_roots to _build_codegen_prompt
    and _parse_generation_response when context is cross-repo."""
    from unittest.mock import patch
    from backend.core.ouroboros.governance.providers import PrimeProvider
    from backend.core.ouroboros.governance.op_context import OperationContext

    # Two files across two repos
    jarvis_file = tmp_path / "jarvis" / "api.py"
    prime_file = tmp_path / "prime" / "handler.py"
    jarvis_file.parent.mkdir(parents=True)
    prime_file.parent.mkdir(parents=True)
    jarvis_file.write_text("def api(): pass\n")
    prime_file.write_text("def handle(): pass\n")

    ctx = OperationContext.create(
        target_files=(str(jarvis_file), str(prime_file)),
        description="Cross-repo fix",
        op_id="op-t6-001",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    )
    roots = {"jarvis": tmp_path / "jarvis", "prime": tmp_path / "prime"}

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = '{"schema_version":"2b.1","candidates":[{"candidate_id":"c1","file_path":"api.py","full_content":"def api(): return 1\\n","rationale":"test"}],"provider_metadata":{"model_id":"m","reasoning_summary":"s"}}'
    mock_client.generate = AsyncMock(return_value=mock_response)

    provider = PrimeProvider(prime_client=mock_client, repo_roots=roots)

    prompt_kwargs: dict = {}
    parse_kwargs: dict = {}

    original_build = __import__(
        "backend.core.ouroboros.governance.providers",
        fromlist=["_build_codegen_prompt"],
    )._build_codegen_prompt

    original_parse = __import__(
        "backend.core.ouroboros.governance.providers",
        fromlist=["_parse_generation_response"],
    )._parse_generation_response

    def capture_build(ctx_, **kw):
        prompt_kwargs.update(kw)
        return original_build(ctx_, **kw)

    def capture_parse(raw, pname, dur, ctx_, sh, sp, **kw):
        parse_kwargs.update(kw)
        return original_parse(raw, pname, dur, ctx_, sh, sp, **kw)

    import datetime as _dt
    with (
        patch(
            "backend.core.ouroboros.governance.providers._build_codegen_prompt",
            side_effect=capture_build,
        ),
        patch(
            "backend.core.ouroboros.governance.providers._parse_generation_response",
            side_effect=capture_parse,
        ),
    ):
        await provider.generate(ctx, deadline=_dt.datetime.now())

    assert prompt_kwargs.get("repo_roots") == roots, (
        "repo_roots must be forwarded to _build_codegen_prompt"
    )
    assert parse_kwargs.get("repo_roots") == roots, (
        "repo_roots must be forwarded to _parse_generation_response"
    )


# ---------------------------------------------------------------------------
# ClaudeProvider repo_roots wiring
# ---------------------------------------------------------------------------


def test_claude_provider_accepts_repo_roots():
    """ClaudeProvider.__init__ must accept and store repo_roots."""
    from backend.core.ouroboros.governance.providers import ClaudeProvider

    roots = {"jarvis": Path("/repos/jarvis")}
    provider = ClaudeProvider(api_key="test-key", repo_roots=roots)
    assert provider._repo_roots == roots


# ---------------------------------------------------------------------------
# _build_components injects repo_roots_map into providers
# ---------------------------------------------------------------------------


async def test_build_components_injects_repo_roots_into_prime_provider(tmp_path):
    """_build_components must set _repo_roots on PrimeProvider from RepoRegistry."""
    from unittest.mock import AsyncMock, patch
    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )
    from backend.core.ouroboros.governance.multi_repo.registry import (
        RepoConfig,
        RepoRegistry,
    )

    config = GovernedLoopConfig(project_root=tmp_path)

    jarvis_path = tmp_path / "jarvis"
    prime_path = tmp_path / "prime"
    jarvis_path.mkdir()
    prime_path.mkdir()

    fake_registry = RepoRegistry(
        configs=(
            RepoConfig(name="jarvis", local_path=jarvis_path, canary_slices=()),
            RepoConfig(name="prime", local_path=prime_path, canary_slices=()),
        )
    )

    mock_prime = AsyncMock()
    mock_health = MagicMock()
    mock_health.name = "AVAILABLE"
    mock_prime._check_health = AsyncMock(return_value=mock_health)

    gls = GovernedLoopService(config=config, stack=None, prime_client=mock_prime)

    with patch(
        "backend.core.ouroboros.governance.governed_loop_service.RepoRegistry.from_env",
        return_value=fake_registry,
    ):
        await gls._build_components()

    from backend.core.ouroboros.governance.providers import PrimeProvider

    assert gls._generator is not None
    provider = gls._generator._primary
    assert isinstance(provider, PrimeProvider)
    assert provider._repo_roots is not None
    assert "jarvis" in provider._repo_roots
    assert provider._repo_roots["jarvis"] == jarvis_path
