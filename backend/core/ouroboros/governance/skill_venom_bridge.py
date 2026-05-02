"""SkillVenomBridge -- Slice 4 of SkillRegistry-AutonomousReach arc.
=====================================================================

Surfaces ``reach=MODEL`` (or ANY / OPERATOR_PLUS_MODEL) skills into
Venom's tool dispatch so the model can reach for them during
GENERATE the same way it reaches for built-in tools and MCP-forwarded
externals (Gap #7).

Three integration surfaces -- each minimal, additive, AST-pinnable:

* :func:`extended_manifests` -- read-only union view of
  ``_L1_MANIFESTS`` + skill-derived ``ToolManifest`` instances.
  Recomputed on demand; never mutates the built-in dict.
* :func:`is_skill_tool_name` -- predicate the policy gate consults
  to ALLOW ``skill__*`` calls (mirror of the existing
  ``name.startswith("mcp_")`` allowance).
* :func:`dispatch_skill_tool` -- async dispatcher returning
  ``(ok, output, error)`` tuples (no :class:`ToolResult` import,
  zero circular-dep risk). The backend wrapper in
  ``tool_executor.AsyncProcessToolBackend.execute_async`` converts
  the tuple to a real :class:`ToolResult`.

Naming convention
-----------------

Skill tool names use the prefix ``skill__`` (double underscore
separator) to disambiguate from MCP's ``mcp_*`` convention. The
remainder is the literal :attr:`SkillManifest.qualified_name`
(lowercase + digits + dots + colons + hyphens). The conversion is
bidirectional and lossless via :func:`tool_name_to_qualified_name`
/ :func:`qualified_name_to_tool_name`.

Reuse contract (no duplication)
-------------------------------

* Skill discovery: SkillCatalog (Slice 2 trigger index untouched
  -- the bridge reads ``catalog.list_all`` for prompt-time
  enumeration).
* Skill dispatch: SkillInvoker.invoke (existing arc).
* Args validation: handled by SkillInvoker via the manifest's
  ``args_schema`` (existing arc -- bridge does not re-validate).
* Reach gating: skill_trigger.SkillReach + reach_includes (Slice 1).
* Authority: SkillCatalog refuses MODEL-source registrations -- the
  bridge inherits this. The model can INVOKE catalog skills but
  cannot register manifests.

Reverse-Russian-Doll posture
----------------------------

* O+V's outer doll gains the model-reach surface. The model can
  now call autonomous-trigger skills explicitly during GENERATE.
* Antivenom scales proportionally:
    - Bridge default-FALSE until Slice 5 -- exposing skill names
      to the model before the SkillCatalog has anything to
      dispatch is operator-confusing.
    - Strict reach filter: only skills whose ``reach`` includes
      MODEL appear in :func:`extended_manifests`.
    - Dispatch path inherits SkillInvoker's authority + arg
      validation -- the bridge is a router, not a fire authority.
    - Defensive ``try/except`` at every external boundary: a
      buggy skill cannot stall the Venom tool loop.
    - asyncio.CancelledError propagates per asyncio convention.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillInvocationOutcome,
    SkillInvoker,
    get_default_catalog,
    get_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillReach,
    reach_includes,
)

logger = logging.getLogger("Ouroboros.SkillVenomBridge")


SKILL_VENOM_BRIDGE_SCHEMA_VERSION: str = "skill_venom_bridge.v1"

# Tool name prefix that disambiguates skill-routed tool calls from
# built-in tools and MCP forwards. Mirrors the existing ``mcp_``
# convention; double underscore prevents accidental collision with
# any underscore-using built-in (none today, but defensive).
SKILL_TOOL_PREFIX: str = "skill__"


# ---------------------------------------------------------------------------
# Sub-flag
# ---------------------------------------------------------------------------


def bridge_enabled() -> bool:
    """``JARVIS_SKILL_VENOM_BRIDGE_ENABLED`` (default ``true`` post
    Slice 5 graduation, 2026-05-02).

    Independent of the Slice 1 ``JARVIS_SKILL_TRIGGER_ENABLED``
    master flag -- an operator may want autonomous skills firing
    via the observer without exposing them to the model, or
    vice-versa. Each surface keeps its own escape hatch.
    """
    raw = os.environ.get(
        "JARVIS_SKILL_VENOM_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Name conversion (bidirectional, lossless)
# ---------------------------------------------------------------------------


def qualified_name_to_tool_name(qualified_name: str) -> str:
    """``"posture-correct"`` -> ``"skill__posture-correct"``.
    ``"plugin:foo"`` -> ``"skill__plugin:foo"``. NEVER raises.
    Returns empty string on garbage input."""
    try:
        if not isinstance(qualified_name, str):
            return ""
        q = qualified_name.strip()
        if not q:
            return ""
        return f"{SKILL_TOOL_PREFIX}{q}"
    except Exception:  # noqa: BLE001 -- defensive
        return ""


def tool_name_to_qualified_name(tool_name: str) -> Optional[str]:
    """``"skill__posture-correct"`` -> ``"posture-correct"``. Returns
    ``None`` for any non-skill tool name. NEVER raises."""
    try:
        if not isinstance(tool_name, str):
            return None
        if not tool_name.startswith(SKILL_TOOL_PREFIX):
            return None
        remainder = tool_name[len(SKILL_TOOL_PREFIX):]
        if not remainder:
            return None
        return remainder
    except Exception:  # noqa: BLE001 -- defensive
        return None


# ---------------------------------------------------------------------------
# Reach filter
# ---------------------------------------------------------------------------


def model_reach_includes_model(manifest: Any) -> bool:
    """Return True when the manifest's ``reach`` includes
    :attr:`SkillReach.MODEL`. Composes Slice 1's
    :func:`reach_includes` lattice. NEVER raises."""
    try:
        reach = getattr(manifest, "reach", None)
        if not isinstance(reach, SkillReach):
            return False
        return reach_includes(reach, SkillReach.MODEL)
    except Exception:  # noqa: BLE001 -- defensive
        return False


# ---------------------------------------------------------------------------
# Skill -> ToolManifest projection
# ---------------------------------------------------------------------------


def manifest_to_tool_manifest_dict(
    manifest: SkillManifest,
) -> Dict[str, Any]:
    """Convert a :class:`SkillManifest` into a plain dict matching
    the :class:`ToolManifest` shape (name, version, description,
    arg_schema, capabilities). Returns a plain dict (not a
    ToolManifest instance) to avoid importing tool_executor here
    -- callers that need a ToolManifest construct it themselves.

    Capabilities derive from the manifest's ``permissions`` list:
    ``"filesystem_write"`` adds ``"write"``;
    ``"filesystem_read"`` / ``"read_only"`` add ``"read"``.
    """
    perms = tuple(manifest.permissions or ())
    caps = set()
    if (
        "filesystem_read" in perms
        or "read_only" in perms
        or "env_read" in perms
    ):
        caps.add("read")
    if "filesystem_write" in perms:
        caps.add("write")
    if "subprocess" in perms:
        caps.add("subprocess")
    if "network" in perms:
        caps.add("network")
    tool_name = qualified_name_to_tool_name(manifest.qualified_name)
    return {
        "name": tool_name,
        "version": manifest.version or "0.0.0",
        "description": manifest.description or "",
        "arg_schema": dict(manifest.args_schema or {}),
        "capabilities": frozenset(caps),
        "schema_version": "tool.manifest.v1",
    }


# ---------------------------------------------------------------------------
# Catalog enumeration helpers
# ---------------------------------------------------------------------------


def list_model_reach_manifests(
    *, catalog: Optional[SkillCatalog] = None,
) -> List[SkillManifest]:
    """Return manifests whose reach includes MODEL. NEVER raises."""
    try:
        cat = catalog or get_default_catalog()
        manifests = cat.list_all()
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.debug(
            "[SkillVenomBridge] list_model_reach_manifests "
            "degraded: %s", exc,
        )
        return []
    return [m for m in manifests if model_reach_includes_model(m)]


def is_skill_tool_name(
    tool_name: str, *, catalog: Optional[SkillCatalog] = None,
) -> bool:
    """Predicate: True when ``tool_name`` has the skill prefix AND
    resolves to a registered model-reach manifest. NEVER raises.

    The catalog gating is load-bearing: a stale ``skill__foo`` call
    after the operator unregistered ``foo`` must be denied (not
    silently allowed)."""
    try:
        qname = tool_name_to_qualified_name(tool_name)
        if qname is None:
            return False
        cat = catalog or get_default_catalog()
        manifest = cat.get(qname)
        if manifest is None:
            return False
        return model_reach_includes_model(manifest)
    except Exception:  # noqa: BLE001 -- defensive
        return False


def get_skill_tool_manifest_dict(
    tool_name: str, *, catalog: Optional[SkillCatalog] = None,
) -> Optional[Dict[str, Any]]:
    """Return the projected tool-manifest dict for a skill tool
    name, or ``None`` if not a known model-reach skill. NEVER raises."""
    try:
        qname = tool_name_to_qualified_name(tool_name)
        if qname is None:
            return None
        cat = catalog or get_default_catalog()
        manifest = cat.get(qname)
        if manifest is None or not model_reach_includes_model(manifest):
            return None
        return manifest_to_tool_manifest_dict(manifest)
    except Exception:  # noqa: BLE001 -- defensive
        return None


def extended_manifest_dicts(
    *, catalog: Optional[SkillCatalog] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{tool_name: manifest_dict}`` for every model-reach
    skill currently in the catalog. Read-only -- does NOT touch
    ``_L1_MANIFESTS`` or any other mutable state. NEVER raises."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for manifest in list_model_reach_manifests(catalog=catalog):
            d = manifest_to_tool_manifest_dict(manifest)
            tool_name = d.get("name", "")
            if isinstance(tool_name, str) and tool_name:
                out[tool_name] = d
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.debug(
            "[SkillVenomBridge] extended_manifest_dicts "
            "degraded: %s", exc,
        )
    return out


# ---------------------------------------------------------------------------
# Prompt block helper (consumed by Slice 4b orchestrator integration)
# ---------------------------------------------------------------------------


def _bounded_int_env(
    name: str, *, default: int, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return max(floor, min(ceiling, n))


def render_skill_tool_block(
    *,
    catalog: Optional[SkillCatalog] = None,
    max_chars: Optional[int] = None,
) -> str:
    """Render a markdown prompt block listing model-reach skills.
    Empty string when no skills qualify. NEVER raises.

    Char cap: ``JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS`` (default
    4000, floor 200, ceiling 16000) so the block can't dominate
    the GENERATE prompt."""
    try:
        cap = (
            max_chars if max_chars is not None
            else _bounded_int_env(
                "JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS",
                default=4000, floor=200, ceiling=16000,
            )
        )
        manifests = list_model_reach_manifests(catalog=catalog)
        if not manifests:
            return ""
        lines = [
            "## Available Skills (operator-blessed callables)",
            "",
            (
                "Reach for these via tool name "
                f"``{SKILL_TOOL_PREFIX}<qualified_name>`` -- the "
                "Venom dispatcher routes to SkillInvoker."
            ),
            "",
        ]
        for manifest in manifests:
            tool_name = qualified_name_to_tool_name(
                manifest.qualified_name,
            )
            description = (manifest.description or "").strip()
            trigger = (manifest.trigger or "").strip()
            lines.append(f"- ``{tool_name}`` -- {description}")
            if trigger:
                lines.append(f"  When to use: {trigger}")
        block = "\n".join(lines)
        if len(block) > cap:
            block = block[: max(1, cap - 3)] + "..."
        return block
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.debug(
            "[SkillVenomBridge] render_skill_tool_block "
            "degraded: %s", exc,
        )
        return ""


# ---------------------------------------------------------------------------
# Async dispatch -- the load-bearing call path
# ---------------------------------------------------------------------------


async def dispatch_skill_tool(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    catalog: Optional[SkillCatalog] = None,
    invoker: Optional[SkillInvoker] = None,
) -> Tuple[bool, str, str]:
    """Resolve ``tool_name`` to a skill manifest, run it via
    SkillInvoker, return ``(ok, output, error)``.

    NEVER raises out -- caller-initiated
    :class:`asyncio.CancelledError` propagates per asyncio
    convention.

    Failure modes:
      * Bridge sub-flag off -> (False, "", "skill_bridge_disabled")
      * Tool name doesn't have skill prefix ->
        (False, "", "not_a_skill_tool")
      * Skill not in catalog -> (False, "", "unknown_skill:<qname>")
      * Skill reach excludes MODEL ->
        (False, "", "skill_reach_excludes_model")
      * SkillInvoker returns ``ok=False`` ->
        (False, "", invocation_outcome.error or "invocation_failed")

    Defensive: catches every other exception and converts to
    ``(False, "", "skill_dispatch_internal_error:<exc>")``.
    """
    try:
        if not bridge_enabled():
            return (False, "", "skill_bridge_disabled")
        qname = tool_name_to_qualified_name(tool_name)
        if qname is None:
            return (False, "", "not_a_skill_tool")
        cat = catalog or get_default_catalog()
        manifest = cat.get(qname)
        if manifest is None:
            return (False, "", f"unknown_skill:{qname}")
        if not model_reach_includes_model(manifest):
            return (False, "", "skill_reach_excludes_model")
        inv = invoker or get_default_invoker()
        try:
            outcome: SkillInvocationOutcome = await inv.invoke(
                qname, args=dict(arguments or {}),
            )
        except Exception as exc:  # noqa: BLE001 -- defensive
            # SkillInvoker is documented to never raise out, but
            # defense in depth -- some operator handlers might wrap
            # an underlying call that breaks the contract.
            return (
                False, "",
                f"invoker_raised:{type(exc).__name__}:{exc}",
            )
        if not outcome.ok:
            return (
                False, "",
                outcome.error or "invocation_failed",
            )
        # Successful invocation -- the bounded preview is the model-
        # visible output.
        return (True, outcome.result_preview or "", "")
    except Exception as exc:  # noqa: BLE001 -- last-resort
        logger.debug(
            "[SkillVenomBridge] dispatch_skill_tool internal "
            "error: %s", exc,
        )
        return (
            False, "",
            f"skill_dispatch_internal_error:"
            f"{type(exc).__name__}:{exc}",
        )


__all__ = [
    "SKILL_TOOL_PREFIX",
    "SKILL_VENOM_BRIDGE_SCHEMA_VERSION",
    "bridge_enabled",
    "dispatch_skill_tool",
    "extended_manifest_dicts",
    "get_skill_tool_manifest_dict",
    "is_skill_tool_name",
    "list_model_reach_manifests",
    "manifest_to_tool_manifest_dict",
    "model_reach_includes_model",
    "qualified_name_to_tool_name",
    "register_flags",
    "register_shipped_invariants",
    "render_skill_tool_block",
    "tool_name_to_qualified_name",
]


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[SkillVenomBridge] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/skill_venom_bridge.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_SKILL_VENOM_BRIDGE_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_SKILL_VENOM_BRIDGE_ENABLED=true",
            description=(
                "Wire-up sub-flag for the Venom tool-surface "
                "bridge. Independent of "
                "JARVIS_SKILL_TRIGGER_ENABLED. Graduated default-"
                "true 2026-05-02 in Slice 5. When off, skill__* "
                "tool calls fall through to the existing unknown-"
                "tool DENY."
            ),
        ),
        FlagSpec(
            name="JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS",
            type=FlagType.INT, default=4000,
            category=Category.CAPACITY,
            source_file=target,
            example="JARVIS_SKILL_VENOM_PROMPT_MAX_CHARS=8000",
            description=(
                "Char cap for the markdown prompt block listing "
                "model-reach skills. Floor 200, ceiling 16000."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[SkillVenomBridge] register_flags spec %s "
                "skipped: %s", spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 4 invariants: authority allowlist + dispatch tuple
    return shape (no ToolResult import => no circular dep)."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {
        "skill_trigger", "skill_catalog", "skill_manifest",
        "flag_registry", "shipped_code_invariants",
    }
    _FORBIDDEN = {
        "tool_executor",  # bridge MUST NOT import tool_executor
        "orchestrator", "phase_runner", "iron_gate",
        "change_engine", "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "semantic_guardian", "semantic_firewall", "risk_engine",
    }

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"unexpected governance import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 4 MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/skill_venom_bridge.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="skill_venom_bridge_authority",
            target_file=target,
            description=(
                "Slice 4 bridge authority: imports only "
                "skill_trigger / skill_catalog / skill_manifest + "
                "the registration contract (flag_registry / "
                "shipped_code_invariants). MUST NOT import "
                "tool_executor (zero circular-dep contract via the "
                "(ok, output, error) tuple return). No "
                "exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
