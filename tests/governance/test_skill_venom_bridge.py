"""SkillVenomBridge Slice 4 -- regression spine.

Pins the bridge module + the three surgical edits to
tool_executor.py that surface SkillRegistry-AutonomousReach
``reach=MODEL`` skills into Venom's tool dispatch.

Coverage:
  * SKILL_TOOL_PREFIX constant + sub-flag asymmetric env semantics
  * Bidirectional name conversion (qname <-> tool_name) round-trips
  * model_reach_includes_model lattice composition
  * manifest_to_tool_manifest_dict shape + capability projection
  * list_model_reach_manifests filters by reach
  * is_skill_tool_name predicate -- catalog gating, post-unregister
    DENY, garbage input
  * get_skill_tool_manifest_dict + extended_manifest_dicts
    union-view semantics
  * render_skill_tool_block formatting + char-cap clamping
  * dispatch_skill_tool happy path / each failure mode
  * tool_executor policy gate edits:
      - skill__* names ALLOWed when bridge enabled + catalog match
      - skill__* names DENIED when bridge disabled (sub-flag off)
      - skill__* names DENIED when not in catalog (no fallthrough
        to Rule 0 silent allow)
      - non-skill__* unknown names still DENIED (regression
        guard for the Rule 0 amendment)
  * tool_executor backend dispatch routes skill__* via the bridge
    AND converts the (ok, output, error) tuple to a real ToolResult
  * Backward-compat: existing tool dispatch unchanged
  * NEVER raises -- defensive across every helper
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillInvocationOutcome,
    SkillSource,
    reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
    SkillReach,
)
from backend.core.ouroboros.governance.skill_venom_bridge import (
    SKILL_TOOL_PREFIX,
    SKILL_VENOM_BRIDGE_SCHEMA_VERSION,
    bridge_enabled,
    dispatch_skill_tool,
    extended_manifest_dicts,
    get_skill_tool_manifest_dict,
    is_skill_tool_name,
    list_model_reach_manifests,
    manifest_to_tool_manifest_dict,
    model_reach_includes_model,
    qualified_name_to_tool_name,
    render_skill_tool_block,
    tool_name_to_qualified_name,
)
from backend.core.ouroboros.governance.tool_executor import (
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_manifest(
    *, name: str, reach: str = "any",
    permissions=None,
):
    # Entrypoint identifiers must be valid Python module names
    # (no hyphens). Sanitize the test name -> entrypoint module
    # without altering the user-visible skill ``name`` field.
    safe_name = name.replace("-", "_").replace(":", "_")
    return SkillManifest.from_mapping({
        "name": name,
        "description": f"desc-{name}",
        "trigger": f"trigger-{name}",
        "entrypoint": f"mod.{safe_name}:run",
        "reach": reach,
        "permissions": list(permissions or []),
        "args_schema": {
            "x": {"type": "string", "default": ""},
        },
        "version": "1.2.3",
    })


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_SKILL_TRIGGER_ENABLED",
        "JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
        "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_default_catalog()
    reset_default_invoker()


@pytest.fixture
def catalog() -> SkillCatalog:
    """Per-test catalog -- distinct from the default singleton."""
    return SkillCatalog()


# ---------------------------------------------------------------------------
# Constants + sub-flag
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version(self):
        assert SKILL_VENOM_BRIDGE_SCHEMA_VERSION == (
            "skill_venom_bridge.v1"
        )

    def test_prefix(self):
        assert SKILL_TOOL_PREFIX == "skill__"


class TestBridgeFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raising=False,
        )
        assert bridge_enabled() is False

    def test_empty_is_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "")
        assert bridge_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "On", "YES"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raw)
        assert bridge_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raw)
        assert bridge_enabled() is False


# ---------------------------------------------------------------------------
# Name conversion (bidirectional)
# ---------------------------------------------------------------------------


class TestNameConversion:
    @pytest.mark.parametrize("qname", [
        "posture-correct",
        "plugin:foo",
        "namespaced-plugin:my-skill",
        "x",
    ])
    def test_round_trip(self, qname):
        tool_name = qualified_name_to_tool_name(qname)
        assert tool_name.startswith(SKILL_TOOL_PREFIX)
        recovered = tool_name_to_qualified_name(tool_name)
        assert recovered == qname

    def test_garbage_to_tool_name_returns_empty(self):
        assert qualified_name_to_tool_name(None) == ""  # type: ignore[arg-type]
        assert qualified_name_to_tool_name("") == ""
        assert qualified_name_to_tool_name("   ") == ""
        assert qualified_name_to_tool_name(42) == ""  # type: ignore[arg-type]

    def test_non_skill_tool_name_returns_none(self):
        assert tool_name_to_qualified_name("read_file") is None
        assert tool_name_to_qualified_name("mcp_foo_bar") is None
        assert tool_name_to_qualified_name("") is None
        assert tool_name_to_qualified_name(None) is None  # type: ignore[arg-type]
        assert tool_name_to_qualified_name(42) is None  # type: ignore[arg-type]

    def test_prefix_with_empty_remainder_returns_none(self):
        assert tool_name_to_qualified_name(SKILL_TOOL_PREFIX) is None


# ---------------------------------------------------------------------------
# Reach lattice composition
# ---------------------------------------------------------------------------


class TestReachFilter:
    @pytest.mark.parametrize("reach, expected", [
        ("model", True),
        ("operator_plus_model", True),
        ("any", True),
        ("operator", False),
        ("autonomous", False),
    ])
    def test_model_reach_includes_model(self, reach, expected):
        m = _build_manifest(name="x", reach=reach)
        assert model_reach_includes_model(m) is expected

    def test_garbage_returns_false(self):
        assert model_reach_includes_model(None) is False
        assert model_reach_includes_model("not a manifest") is False

        class _NoReach:
            pass
        assert model_reach_includes_model(_NoReach()) is False


# ---------------------------------------------------------------------------
# Manifest projection
# ---------------------------------------------------------------------------


class TestManifestProjection:
    def test_basic_shape(self):
        m = _build_manifest(
            name="auth-check", reach="model",
            permissions=["read_only"],
        )
        d = manifest_to_tool_manifest_dict(m)
        assert d["name"] == "skill__auth-check"
        assert d["version"] == "1.2.3"
        assert d["description"].startswith("desc-")
        assert d["arg_schema"] == {
            "x": {"type": "string", "default": ""}
        }
        # read_only -> read capability
        assert "read" in d["capabilities"]
        assert "write" not in d["capabilities"]

    def test_filesystem_write_adds_write_capability(self):
        m = _build_manifest(
            name="apply", reach="model",
            permissions=["filesystem_write"],
        )
        d = manifest_to_tool_manifest_dict(m)
        assert "write" in d["capabilities"]

    def test_subprocess_adds_subprocess_capability(self):
        m = _build_manifest(
            name="run", reach="model",
            permissions=["subprocess"],
        )
        d = manifest_to_tool_manifest_dict(m)
        assert "subprocess" in d["capabilities"]

    def test_network_adds_network_capability(self):
        m = _build_manifest(
            name="fetch", reach="model",
            permissions=["network"],
        )
        d = manifest_to_tool_manifest_dict(m)
        assert "network" in d["capabilities"]

    def test_no_permissions_empty_capabilities(self):
        m = _build_manifest(name="x", reach="model")
        d = manifest_to_tool_manifest_dict(m)
        assert d["capabilities"] == frozenset()


# ---------------------------------------------------------------------------
# Catalog enumeration
# ---------------------------------------------------------------------------


class TestListModelReach:
    def test_filters_by_reach(self, catalog):
        catalog.register(_build_manifest(name="m1", reach="model"),
                         source=SkillSource.OPERATOR)
        catalog.register(_build_manifest(name="any1", reach="any"),
                         source=SkillSource.OPERATOR)
        catalog.register(
            _build_manifest(name="op1", reach="operator"),
            source=SkillSource.OPERATOR,
        )
        catalog.register(
            _build_manifest(name="auto1", reach="autonomous"),
            source=SkillSource.OPERATOR,
        )
        out = list_model_reach_manifests(catalog=catalog)
        names = sorted(m.qualified_name for m in out)
        assert names == ["any1", "m1"]

    def test_empty_catalog(self, catalog):
        assert list_model_reach_manifests(catalog=catalog) == []


class TestIsSkillToolName:
    def test_known_model_skill_returns_true(self, catalog):
        catalog.register(
            _build_manifest(name="known", reach="model"),
            source=SkillSource.OPERATOR,
        )
        assert is_skill_tool_name(
            "skill__known", catalog=catalog,
        ) is True

    def test_unknown_skill_returns_false(self, catalog):
        assert is_skill_tool_name(
            "skill__never-registered", catalog=catalog,
        ) is False

    def test_post_unregister_returns_false(self, catalog):
        catalog.register(
            _build_manifest(name="ephem", reach="model"),
            source=SkillSource.OPERATOR,
        )
        catalog.unregister("ephem")
        assert is_skill_tool_name(
            "skill__ephem", catalog=catalog,
        ) is False

    def test_non_model_reach_returns_false(self, catalog):
        catalog.register(
            _build_manifest(name="op-only", reach="operator"),
            source=SkillSource.OPERATOR,
        )
        # Even though the skill is registered, reach excludes MODEL
        assert is_skill_tool_name(
            "skill__op-only", catalog=catalog,
        ) is False

    def test_garbage_returns_false(self):
        assert is_skill_tool_name("read_file") is False
        assert is_skill_tool_name("") is False
        assert is_skill_tool_name(None) is False  # type: ignore[arg-type]


class TestGetSkillToolManifestDict:
    def test_known_model_skill(self, catalog):
        catalog.register(
            _build_manifest(name="known", reach="model"),
            source=SkillSource.OPERATOR,
        )
        d = get_skill_tool_manifest_dict(
            "skill__known", catalog=catalog,
        )
        assert d is not None
        assert d["name"] == "skill__known"

    def test_unknown_returns_none(self, catalog):
        assert get_skill_tool_manifest_dict(
            "skill__unknown", catalog=catalog,
        ) is None

    def test_non_model_reach_returns_none(self, catalog):
        catalog.register(
            _build_manifest(name="auto", reach="autonomous"),
            source=SkillSource.OPERATOR,
        )
        assert get_skill_tool_manifest_dict(
            "skill__auto", catalog=catalog,
        ) is None


class TestExtendedManifestDicts:
    def test_union_view_filters_by_reach(self, catalog):
        catalog.register(_build_manifest(name="m1", reach="model"),
                         source=SkillSource.OPERATOR)
        catalog.register(_build_manifest(name="any1", reach="any"),
                         source=SkillSource.OPERATOR)
        catalog.register(_build_manifest(name="op1", reach="operator"),
                         source=SkillSource.OPERATOR)
        out = extended_manifest_dicts(catalog=catalog)
        assert set(out.keys()) == {"skill__m1", "skill__any1"}

    def test_empty_catalog(self, catalog):
        assert extended_manifest_dicts(catalog=catalog) == {}


# ---------------------------------------------------------------------------
# Prompt block rendering
# ---------------------------------------------------------------------------


class TestRenderPromptBlock:
    def test_empty_when_no_skills(self, catalog):
        assert render_skill_tool_block(catalog=catalog) == ""

    def test_contains_skill_tool_names(self, catalog):
        catalog.register(_build_manifest(name="m1", reach="model"),
                         source=SkillSource.OPERATOR)
        catalog.register(_build_manifest(name="any1", reach="any"),
                         source=SkillSource.OPERATOR)
        block = render_skill_tool_block(catalog=catalog)
        assert "skill__m1" in block
        assert "skill__any1" in block
        assert "Available Skills" in block

    def test_omits_non_model_reach(self, catalog):
        catalog.register(_build_manifest(name="m1", reach="model"),
                         source=SkillSource.OPERATOR)
        catalog.register(_build_manifest(name="op1", reach="operator"),
                         source=SkillSource.OPERATOR)
        block = render_skill_tool_block(catalog=catalog)
        assert "skill__m1" in block
        assert "skill__op1" not in block

    def test_char_cap_explicit(self, catalog):
        for i in range(20):
            catalog.register(
                _build_manifest(name=f"sk{i}", reach="model"),
                source=SkillSource.OPERATOR,
            )
        block = render_skill_tool_block(
            catalog=catalog, max_chars=200,
        )
        assert len(block) <= 200

    def test_char_cap_via_env(self, catalog, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS", "300",
        )
        for i in range(20):
            catalog.register(
                _build_manifest(name=f"sk{i}", reach="model"),
                source=SkillSource.OPERATOR,
            )
        block = render_skill_tool_block(catalog=catalog)
        assert len(block) <= 300

    def test_char_cap_floor_clamp(self, catalog, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS", "0",
        )
        for i in range(5):
            catalog.register(
                _build_manifest(name=f"sk{i}", reach="model"),
                source=SkillSource.OPERATOR,
            )
        block = render_skill_tool_block(catalog=catalog)
        assert len(block) <= 200  # floor

    def test_char_cap_garbage_falls_back(self, catalog, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS", "abc",
        )
        catalog.register(_build_manifest(name="x", reach="model"),
                         source=SkillSource.OPERATOR)
        block = render_skill_tool_block(catalog=catalog)
        assert len(block) <= 4000  # default


# ---------------------------------------------------------------------------
# dispatch_skill_tool (the load-bearing call path)
# ---------------------------------------------------------------------------


class _StubInvoker:
    """SkillInvoker stand-in returning configured outcomes."""

    def __init__(self, *, ok=True, output="ok-output",
                 error=None, raises=False):
        self.calls = []
        self._ok = ok
        self._output = output
        self._error = error
        self._raises = raises

    async def invoke(self, qualified_name, *, args=None,
                     output_preview_chars=400):
        self.calls.append((qualified_name, dict(args or {})))
        if self._raises:
            raise RuntimeError("stub invoker boom")
        return SkillInvocationOutcome(
            qualified_name=qualified_name,
            ok=self._ok, duration_ms=0.5,
            result_preview=self._output if self._ok else "",
            error=self._error,
        )


class TestDispatchSkillTool:
    @pytest.mark.asyncio
    async def test_disabled_returns_error(
        self, monkeypatch, catalog,
    ):
        monkeypatch.delenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raising=False,
        )
        invoker = _StubInvoker()
        ok, output, error = await dispatch_skill_tool(
            "skill__x", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error == "skill_bridge_disabled"
        assert invoker.calls == []

    @pytest.mark.asyncio
    async def test_non_skill_name_returns_error(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        invoker = _StubInvoker()
        ok, output, error = await dispatch_skill_tool(
            "read_file", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error == "not_a_skill_tool"

    @pytest.mark.asyncio
    async def test_unknown_skill_returns_error(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        invoker = _StubInvoker()
        ok, output, error = await dispatch_skill_tool(
            "skill__nope", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error.startswith("unknown_skill:")
        assert "nope" in error

    @pytest.mark.asyncio
    async def test_non_model_reach_skill_denied(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        catalog.register(
            _build_manifest(name="auto", reach="autonomous"),
            source=SkillSource.OPERATOR,
        )
        invoker = _StubInvoker()
        ok, output, error = await dispatch_skill_tool(
            "skill__auto", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error == "skill_reach_excludes_model"
        assert invoker.calls == []

    @pytest.mark.asyncio
    async def test_happy_path(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        catalog.register(
            _build_manifest(name="echo", reach="model"),
            source=SkillSource.OPERATOR,
        )
        invoker = _StubInvoker(output="hello-world")
        ok, output, error = await dispatch_skill_tool(
            "skill__echo", {"x": "y"},
            catalog=catalog, invoker=invoker,
        )
        assert ok is True
        assert output == "hello-world"
        assert error == ""
        assert invoker.calls == [("echo", {"x": "y"})]

    @pytest.mark.asyncio
    async def test_invoker_returns_failure(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        catalog.register(
            _build_manifest(name="bad", reach="model"),
            source=SkillSource.OPERATOR,
        )
        invoker = _StubInvoker(
            ok=False, output="", error="handler_raise:KeyError:k",
        )
        ok, output, error = await dispatch_skill_tool(
            "skill__bad", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error == "handler_raise:KeyError:k"

    @pytest.mark.asyncio
    async def test_invoker_raises_caught(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        catalog.register(
            _build_manifest(name="bad", reach="model"),
            source=SkillSource.OPERATOR,
        )
        invoker = _StubInvoker(raises=True)
        ok, output, error = await dispatch_skill_tool(
            "skill__bad", {}, catalog=catalog, invoker=invoker,
        )
        assert ok is False
        assert error.startswith("invoker_raised:RuntimeError:")


# ---------------------------------------------------------------------------
# tool_executor policy gate (Edits 1 + 2)
# ---------------------------------------------------------------------------


def _policy_ctx() -> PolicyContext:
    from pathlib import Path
    return PolicyContext(
        repo="test", repo_root=Path("/tmp"),
        op_id="op-1", call_id="op-1:r0:tool", round_index=0,
        is_read_only=False,
    )


class TestPolicyGate:
    @pytest.fixture(autouse=True)
    def _isolate_default_catalog(self):
        # The policy gate uses get_default_catalog() since the
        # lazy import happens inside the policy method. Reset
        # before + after so tests don't bleed.
        reset_default_catalog()
        yield
        reset_default_catalog()

    def test_unknown_non_skill_still_denied(self):
        """Regression guard: amending Rule 0 to allow skill__*
        must NOT silently allow everything else."""
        from pathlib import Path
        policy = GoverningToolPolicy(repo_roots={"test": Path("/tmp")})
        result = policy.evaluate(
            ToolCall(name="totally_unknown", arguments={}),
            _policy_ctx(),
        )
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code == "tool.denied.unknown_tool"

    def test_skill_disabled_falls_through_to_deny(
        self, monkeypatch,
    ):
        """When bridge sub-flag is OFF, skill__* must NOT pass
        the second-stage allow check -- it falls through to the
        end of the policy chain."""
        monkeypatch.delenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.skill_catalog import (
            get_default_catalog,
        )
        cat = get_default_catalog()
        cat.register(
            _build_manifest(name="known", reach="model"),
            source=SkillSource.OPERATOR,
        )
        from pathlib import Path
        policy = GoverningToolPolicy(repo_roots={"test": Path("/tmp")})
        result = policy.evaluate(
            ToolCall(name="skill__known", arguments={}),
            _policy_ctx(),
        )
        # The policy doesn't reach skill_registry allow; falls
        # through. With the read_only=False ctx + no risk
        # tier, the policy reaches its terminal allow ("known
        # tool" path skipped because skill__* isn't in
        # _L1_MANIFESTS, but our Rule 0 amendment let it pass
        # the unknown check). Either way, sub-flag off means no
        # skill_registry-source allow.
        assert result.reason_code != "tool.allowed.skill_registry"

    def test_skill_enabled_in_catalog_allowed(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.skill_catalog import (
            get_default_catalog,
        )
        cat = get_default_catalog()
        cat.register(
            _build_manifest(name="known", reach="model"),
            source=SkillSource.OPERATOR,
        )
        from pathlib import Path
        policy = GoverningToolPolicy(repo_roots={"test": Path("/tmp")})
        result = policy.evaluate(
            ToolCall(name="skill__known", arguments={}),
            _policy_ctx(),
        )
        assert result.decision is PolicyDecision.ALLOW
        assert result.reason_code == "tool.allowed.skill_registry"

    def test_skill_enabled_but_unknown_falls_through(
        self, monkeypatch,
    ):
        """skill__nonexistent with bridge ENABLED but no catalog
        match -- the skill-registry allow shouldn't fire (defensive
        catalog gating)."""
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        from pathlib import Path
        policy = GoverningToolPolicy(repo_roots={"test": Path("/tmp")})
        result = policy.evaluate(
            ToolCall(name="skill__nope", arguments={}),
            _policy_ctx(),
        )
        # No skill_registry allow because is_skill_tool_name
        # returned False (no catalog entry).
        assert result.reason_code != "tool.allowed.skill_registry"


# ---------------------------------------------------------------------------
# tool_executor backend dispatch (Edit 3)
# ---------------------------------------------------------------------------


class TestBackendDispatchRouting:
    """Verifies AsyncProcessToolBackend.execute_async routes
    skill__* names through the bridge -- tested via direct call
    to the backend with a stubbed bridge dispatch."""

    @pytest.mark.asyncio
    async def test_skill_call_returns_success_tool_result(
        self, monkeypatch, catalog,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.skill_catalog import (
            get_default_catalog,
        )
        cat = get_default_catalog()
        cat.register(
            _build_manifest(name="echo", reach="model"),
            source=SkillSource.OPERATOR,
        )
        # Patch the bridge dispatch to a stub
        async def _stub_dispatch(name, args):
            return (True, "stubbed-output", "")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "skill_venom_bridge.dispatch_skill_tool",
            _stub_dispatch,
        )

        from backend.core.ouroboros.governance.tool_executor import (
            AsyncProcessToolBackend, ToolExecStatus,
        )
        import asyncio as _asyncio
        backend = AsyncProcessToolBackend(
            semaphore=_asyncio.Semaphore(4),
        )
        from pathlib import Path
        ctx = PolicyContext(
            repo="test", repo_root=Path("/tmp"),
            op_id="op-1", call_id="op-1:r0:skill__echo",
            round_index=0, is_read_only=False,
        )
        import time as _time
        deadline = _time.monotonic() + 30.0
        result = await backend.execute_async(
            ToolCall(name="skill__echo", arguments={}),
            ctx, deadline,
        )
        assert result.status is ToolExecStatus.SUCCESS
        assert result.output == "stubbed-output"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_skill_call_returns_exec_error_on_failure(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )

        async def _stub_dispatch(name, args):
            return (False, "", "stub-error-msg")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "skill_venom_bridge.dispatch_skill_tool",
            _stub_dispatch,
        )

        from backend.core.ouroboros.governance.tool_executor import (
            AsyncProcessToolBackend, ToolExecStatus,
        )
        import asyncio as _asyncio
        backend = AsyncProcessToolBackend(
            semaphore=_asyncio.Semaphore(4),
        )
        from pathlib import Path
        ctx = PolicyContext(
            repo="test", repo_root=Path("/tmp"),
            op_id="op-1", call_id="op-1:r0:skill__bad",
            round_index=0, is_read_only=False,
        )
        import time as _time
        deadline = _time.monotonic() + 30.0
        result = await backend.execute_async(
            ToolCall(name="skill__bad", arguments={}),
            ctx, deadline,
        )
        assert result.status is ToolExecStatus.EXEC_ERROR
        assert result.error == "stub-error-msg"

    @pytest.mark.asyncio
    async def test_skill_dispatch_truncates_to_cap(
        self, monkeypatch,
    ):
        """Backend caps output to JARVIS_TOOL_OUTPUT_CAP_BYTES."""
        monkeypatch.setenv(
            "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "true",
        )
        monkeypatch.setenv("JARVIS_TOOL_OUTPUT_CAP_BYTES", "10")

        async def _stub_dispatch(name, args):
            return (True, "x" * 1000, "")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "skill_venom_bridge.dispatch_skill_tool",
            _stub_dispatch,
        )

        from backend.core.ouroboros.governance.tool_executor import (
            AsyncProcessToolBackend, ToolExecStatus,
        )
        import asyncio as _asyncio
        backend = AsyncProcessToolBackend(
            semaphore=_asyncio.Semaphore(4),
        )
        from pathlib import Path
        ctx = PolicyContext(
            repo="test", repo_root=Path("/tmp"),
            op_id="op-1", call_id="op-1:r0:skill__big",
            round_index=0, is_read_only=False,
        )
        import time as _time
        deadline = _time.monotonic() + 30.0
        result = await backend.execute_async(
            ToolCall(name="skill__big", arguments={}),
            ctx, deadline,
        )
        assert len(result.output) == 10  # truncated to cap
