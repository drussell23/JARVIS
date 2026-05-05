"""ToolRenderView — Rich markup composition over the Slice 1-3 substrate.
==========================================================================

Slice 4 of the **Gap #2 closure arc**. This module is the **only** place
in the codebase that composes Rich markup for tool-result rendering.
Everything below it (Slices 1-3) is renderer-agnostic; everything above
it (``serpent_flow``, ``ouroboros_tui``) is a thin caller that hands
strings to ``Console.print``.

Root problem (recap)
--------------------

Two render paths today (``serpent_flow.op_tool_call`` +
``ouroboros_tui.show_tool_call``) hardcode per-tool ``if/elif`` chains
and tool-icon dicts. Slice 4 collapses both paths through a single
adaptive composer.

Slice 4 scope
-------------

* :class:`ComposedToolRender` — frozen output: pre-built Rich-markup
  strings the caller hands directly to ``Console.print`` / ``_op_line``
* :func:`compose` — load-bearing orchestrator: descriptor lookup +
  density resolution + body extraction + body parking + Rich markup
* Master flag :data:`MASTER_FLAG_ENV_VAR` (default ``"false"`` until
  Slice 5 graduation flips it true). When off, callers fall through
  to legacy paths — both paths kept compilable side-by-side.

Authority boundary
------------------

* §1 deterministic — pure markup composition; no LLM, no I/O
* §7 fail-closed — ``compose`` NEVER raises; every input is coerced /
  defaulted; on internal failure returns a minimal render so the
  caller's output is always something displayable
* §8 observable — every :class:`ComposedToolRender` carries the
  resolved :class:`DensityPolicy`; SSE / observability layers in
  Slice 5 can echo "what density was applied here and why"
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from backend.core.ouroboros.battle_test.tool_render_policy import (
    DefaultLayoutModeProvider,
    DefaultPostureProvider,
    DensityPolicy,
    LayoutModeProvider,
    PostureProvider,
    resolve_density_via_providers,
)
from backend.core.ouroboros.battle_test.tool_render_registry import (
    BodyShape,
    ToolStatus,
    get_descriptor,
    render,
)
from backend.core.ouroboros.battle_test.tool_render_store import (
    BoundedBodyStore,
    get_default_store,
)

logger = logging.getLogger("Ouroboros.ToolRenderView")


# ===========================================================================
# Schema + master flag
# ===========================================================================


TOOL_RENDER_VIEW_SCHEMA_VERSION: str = "tool_render_view.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_TOOL_RENDER_REGISTRY_ENABLED"


def is_master_flag_enabled() -> bool:
    """Read the master flag. **Default ``true``** post Slice 5
    graduation (2026-05-04). Operators flip ``=false`` for instant
    rollback to the legacy hardcoded render paths preserved in
    ``serpent_flow.op_tool_call`` + ``ouroboros_tui.show_tool_call``.

    Re-read on every call — flips take effect immediately for the
    next tool render without restart. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ===========================================================================
# Default palette — operators can override per-call
# ===========================================================================
#
# Mirrors the canonical ``_C`` palette in ``serpent_flow.py`` so that
# headless / sandbox callers get a consistent look without depending on
# serpent_flow's privates. Production wiring (Slice 4 edit to serpent_flow)
# passes its own ``_C`` through ``palette=`` for byte-identical output.


_DEFAULT_PALETTE: Mapping[str, str] = {
    "neural": "cyan",
    "file": "blue underline",
    "dim": "dim",
    "death": "red",
    "code_add": "green",
    "code_del": "red",
    "code_hunk": "cyan",
    "heal": "yellow",
}


def _palette_value(palette: Optional[Mapping[str, str]], key: str) -> str:
    """Look up a palette colour, falling back to the default palette."""
    if palette and key in palette:
        return palette[key]
    return _DEFAULT_PALETTE.get(key, "white")


# ===========================================================================
# Frozen composition output
# ===========================================================================


@dataclass(frozen=True)
class ComposedToolRender:
    """Result of :func:`compose` — pre-built Rich markup the caller
    feeds to ``Console.print``.

    Fields
    ------
    * ``header_markup`` — first display line (e.g. CC-style
      ``[cyan]⏺ Read[/cyan]([blue]foo.py[/blue])  [dim]42ms[/dim]``).
    * ``summary_markup`` — body-summary line under the header,
      prefixed with the ``⎿`` continuation glyph; empty string
      when the descriptor has nothing useful to summarize.
    * ``body_lines_markup`` — one markup string per body line. The
      caller emits each via ``_op_line`` so the existing per-op
      indenting + side-rail glyphs apply uniformly.
    * ``expansion_hint`` — when the body was elided, a one-line
      ``[dim]…+N more lines elided · /expand t-12[/dim]`` hint
      so the operator knows recovery is one verb away. Empty string
      when no elision happened OR no expansion ref was issued.
    * ``policy`` — the :class:`DensityPolicy` actually applied;
      observability surfaces in Slice 5 echo this back.
    """

    header_markup: str
    summary_markup: str
    body_lines_markup: Tuple[str, ...]
    expansion_hint: str
    policy: DensityPolicy
    schema_version: str = TOOL_RENDER_VIEW_SCHEMA_VERSION


# ===========================================================================
# Helpers — body-shape-aware markup wrapping
# ===========================================================================


def _wrap_diff_line(
    line: str, palette: Optional[Mapping[str, str]],
) -> str:
    """Style a single diff line per the existing ``_C['code_add']`` /
    ``_C['code_del']`` / ``_C['code_hunk']`` convention."""
    if line.startswith("+++") or line.startswith("---"):
        return f"[{_palette_value(palette, 'dim')}]{_escape(line)}[/{_palette_value(palette, 'dim')}]"
    if line.startswith("+"):
        c = _palette_value(palette, "code_add")
        return f"[{c}]{_escape(line)}[/{c}]"
    if line.startswith("-"):
        c = _palette_value(palette, "code_del")
        return f"[{c}]{_escape(line)}[/{c}]"
    if line.startswith("@@"):
        c = _palette_value(palette, "code_hunk")
        return f"[{c}]{_escape(line)}[/{c}]"
    return _escape(line)


def _wrap_log_line(
    line: str, palette: Optional[Mapping[str, str]],
) -> str:
    """Style a single log/bash output line — uniformly dim."""
    c = _palette_value(palette, "dim")
    return f"[{c}]{_escape(line)}[/{c}]"


def _wrap_text_line(
    line: str, palette: Optional[Mapping[str, str]],
) -> str:
    """Plain multi-line body — light dim styling."""
    c = _palette_value(palette, "dim")
    return f"[{c}]{_escape(line)}[/{c}]"


def _wrap_marker_line(
    line: str, palette: Optional[Mapping[str, str]],
) -> str:
    """Truncation marker (``… +N more lines elided …``)."""
    c = _palette_value(palette, "dim")
    return f"[{c} italic]{_escape(line)}[/{c} italic]"


_BODY_WRAPPERS = {
    BodyShape.DIFF: _wrap_diff_line,
    BodyShape.LOG: _wrap_log_line,
    BodyShape.MULTI_LINE: _wrap_text_line,
    BodyShape.CODE: _wrap_text_line,
    BodyShape.SINGLE_LINE: _wrap_text_line,
    # NONE never gets here — body is empty for header-only descriptors
}


def _escape(text: str) -> str:
    """Defensive escape — Rich treats ``[`` / ``]`` as markup; the
    raw tool output may contain them. We escape *everything* the
    caller hands us so we never inject unintended styles."""
    if not text:
        return ""
    return text.replace("[", "\\[")


# ===========================================================================
# Header composition — CC verb path vs. icon path
# ===========================================================================


def _compose_header(
    rendered_header: str,
    descriptor_cc_verb: Optional[str],
    duration_ms: float,
    status_enum: ToolStatus,
    palette: Optional[Mapping[str, str]],
) -> str:
    """Build the Rich-markup header line.

    For CC-verb descriptors (Read/Update/Write):

      ``[cyan]⏺ Read[/cyan]([blue]foo.py[/blue])  [dim]42ms[/dim]``

    For icon descriptors (everything else):

      ``[cyan]🔍 search_code "pat"[/cyan]  [dim]120ms[/dim]``

    Failure status appends an [red]✗[/red] mark.
    """
    duration_part = ""
    if duration_ms and duration_ms > 0:
        c = _palette_value(palette, "dim")
        if duration_ms < 1000:
            duration_part = f"  [{c}]{duration_ms:.0f}ms[/{c}]"
        else:
            duration_part = f"  [{c}]{duration_ms / 1000:.1f}s[/{c}]"

    status_part = ""
    if status_enum is not ToolStatus.SUCCESS:
        c = _palette_value(palette, "death")
        status_part = f"  [{c}]✗[/{c}]"

    if descriptor_cc_verb:
        # rendered_header looks like "Read(foo.py)" — split into verb
        # + path-in-parens so we can colour each piece independently.
        verb_color = _palette_value(palette, "neural")
        file_color = _palette_value(palette, "file")
        # Find the parens; if not present (defensive), fall back to dim.
        lparen = rendered_header.find("(")
        rparen = rendered_header.rfind(")")
        if 0 < lparen < rparen:
            verb = rendered_header[:lparen]
            inner = rendered_header[lparen + 1 : rparen]
            return (
                f"[{verb_color}]⏺ {verb}[/{verb_color}]"
                f"([{file_color}]{_escape(inner)}[/{file_color}])"
                f"{duration_part}{status_part}"
            )
        # Fall-through: emit as a plain neural-coloured header
        return (
            f"[{verb_color}]⏺ {_escape(rendered_header)}[/{verb_color}]"
            f"{duration_part}{status_part}"
        )

    # Icon path
    c = _palette_value(palette, "neural")
    return (
        f"[{c}]{_escape(rendered_header)}[/{c}]"
        f"{duration_part}{status_part}"
    )


def _compose_summary(
    summary_text: str,
    expansion_ref: Optional[str],
    elided: int,
    body_present: bool,
    palette: Optional[Mapping[str, str]],
) -> Tuple[str, str]:
    """Build (summary_markup, expansion_hint).

    ``summary_markup`` mirrors CC's ``⎿`` continuation glyph.
    ``expansion_hint`` is a separate dim line emitted only when the
    body was actually elided (``elided > 0``) AND a stable ref was
    issued — without a ref the operator has no recovery path, so
    surfacing the hint would be misleading.
    """
    if not summary_text:
        return ("", "")

    c_dim = _palette_value(palette, "dim")
    summary_markup = (
        f"[{c_dim}]⏎  {_escape(summary_text)}[/{c_dim}]"
    )

    expansion_hint = ""
    if elided > 0 and isinstance(expansion_ref, str) and expansion_ref:
        expansion_hint = (
            f"[{c_dim} italic]   … +{elided} more line"
            f"{'s' if elided != 1 else ''} parked · /expand {expansion_ref}"
            f"[/{c_dim} italic]"
        )
    elif body_present and elided == 0:
        # No elision needed; no hint.
        pass

    return (summary_markup, expansion_hint)


# ===========================================================================
# The load-bearing composer
# ===========================================================================


def compose(
    tool_name: str,
    args_str: str,
    result_str: str,
    *,
    status: object = ToolStatus.SUCCESS,
    duration_ms: float = 0.0,
    op_id: str = "",
    round_index: int = 0,
    palette: Optional[Mapping[str, str]] = None,
    posture_provider: Optional[PostureProvider] = None,
    layout_provider: Optional[LayoutModeProvider] = None,
    store: Optional[BoundedBodyStore] = None,
    explicit_density: Optional[DensityPolicy] = None,
) -> ComposedToolRender:
    """Compose Rich markup for one tool call.

    Pipeline:

      1. Resolve the :class:`ToolRenderDescriptor` for ``tool_name``
         (Slice 1; falls back to the default descriptor for unknown
         tools — MCP-forwarded tools land here).
      2. Resolve the :class:`DensityPolicy` from posture × layout ×
         env (Slice 2). Callers that already have a policy (e.g.
         test harnesses) pass ``explicit_density`` to skip resolution.
      3. Park the *full* body in :class:`BoundedBodyStore` (Slice 3)
         when the body shape supports a body block AND the body
         exceeds the density budget. Passing ``store=None`` skips
         parking but everything else still works (graceful degrade).
      4. Run :func:`tool_render_registry.render` to produce the
         bounded :class:`RenderedToolResult`.
      5. Wrap each piece in Rich markup using the supplied palette
         (or the default mirror).

    NEVER raises — every step has a documented degradation.
    """
    # --- 1. Descriptor lookup
    descriptor = get_descriptor(tool_name)

    # --- 2. Density resolution
    policy: DensityPolicy
    if isinstance(explicit_density, DensityPolicy):
        policy = explicit_density
    else:
        policy = resolve_density_via_providers(
            posture_provider or DefaultPostureProvider(),
            layout_provider or DefaultLayoutModeProvider(),
        )

    # --- 3. Park body (issues expansion ref) if eligible
    expansion_ref: Optional[str] = None
    body_eligible = (
        descriptor.body_shape is not BodyShape.NONE
        and policy.show_body
        and bool(result_str)
    )
    if body_eligible and store is not None:
        try:
            stored = store.store(
                op_id=op_id,
                round_index=round_index,
                tool_name=tool_name,
                body=result_str,
                summary="",  # filled-in below; see step 4
                lexer=descriptor.body_lexer,
            )
            expansion_ref = stored.ref
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ToolRenderView] body park failed", exc_info=True,
            )

    # --- 4. Bounded render
    try:
        rendered = render(
            descriptor,
            args_str,
            result_str,
            status,
            max_body_lines=policy.max_body_lines,
            expansion_ref=expansion_ref,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ToolRenderView] render failed", exc_info=True)
        return _empty_render(policy)

    status_enum = ToolStatus.coerce(status)

    # --- 5. Markup composition
    header_markup = _compose_header(
        rendered.header_line,
        descriptor.cc_verb,
        duration_ms,
        status_enum,
        palette,
    )

    body_present = bool(rendered.body_lines)
    summary_markup, expansion_hint = _compose_summary(
        rendered.body_summary,
        expansion_ref,
        rendered.elided_line_count,
        body_present,
        palette,
    )

    # Body lines wrapped per shape
    wrapper = _BODY_WRAPPERS.get(descriptor.body_shape, _wrap_text_line)
    body_lines_markup: Tuple[str, ...] = tuple(
        _wrap_marker_line(ln, palette) if "elided" in ln and ln.lstrip().startswith("…")
        else wrapper(ln, palette)
        for ln in rendered.body_lines
    )

    return ComposedToolRender(
        header_markup=header_markup,
        summary_markup=summary_markup,
        body_lines_markup=body_lines_markup,
        expansion_hint=expansion_hint,
        policy=policy,
    )


def _empty_render(policy: DensityPolicy) -> ComposedToolRender:
    """Defensive minimum so :func:`compose` never raises."""
    return ComposedToolRender(
        header_markup="",
        summary_markup="",
        body_lines_markup=(),
        expansion_hint="",
        policy=policy,
    )


# ===========================================================================
# Convenience: master-flag-aware shim for caller migration
# ===========================================================================


def compose_if_enabled(
    tool_name: str,
    args_str: str,
    result_str: str,
    **kwargs,
) -> Optional[ComposedToolRender]:
    """Return composed markup ONLY if the master flag is on.

    Slice 4 caller migration pattern::

        composed = compose_if_enabled(tool_name, args, result, ...)
        if composed is not None:
            self._op_line(op_id, composed.header_markup)
            if composed.summary_markup:
                self._op_line(op_id, composed.summary_markup)
            for line in composed.body_lines_markup:
                self._op_line(op_id, line)
            if composed.expansion_hint:
                self._op_line(op_id, composed.expansion_hint)
            return  # short-circuit legacy path

    This shim keeps the legacy + new paths cleanly separated; the
    Slice 5 graduation just flips the master flag default.
    """
    if not is_master_flag_enabled():
        return None
    try:
        return compose(tool_name, args_str, result_str, **kwargs)
    except Exception:  # noqa: BLE001
        logger.debug("[ToolRenderView] compose_if_enabled failed", exc_info=True)
        return None


def store_for_view(*, override: Optional[BoundedBodyStore] = None) -> BoundedBodyStore:
    """Resolve the body store: explicit override > default singleton.

    Useful for callers that want to inject a per-session store
    (e.g. cleared on session end) rather than the process singleton.
    """
    if isinstance(override, BoundedBodyStore):
        return override
    return get_default_store()


__all__ = [
    "ComposedToolRender",
    "MASTER_FLAG_ENV_VAR",
    "TOOL_RENDER_VIEW_SCHEMA_VERSION",
    "compose",
    "compose_if_enabled",
    "is_master_flag_enabled",
    "register_flags",
    "register_shipped_invariants",
    "store_for_view",
]


# ===========================================================================
# Slice 5 — FlagRegistry self-registration (auto-discovered via the
# battle_test entry in ``_FLAG_PROVIDER_PACKAGES``)
# ===========================================================================


def register_flags(registry) -> int:
    """Module-owned FlagRegistry registration. Returns count of
    FlagSpecs added. NEVER raises — graduation soak path is fail-open."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master kill switch for the ToolRenderRegistry "
                "rendering path (Gap #2). When false, both "
                "``serpent_flow.op_tool_call`` and "
                "``ouroboros_tui.show_tool_call`` fall through to "
                "the legacy hardcoded ``if/elif`` chains preserved "
                "below the master-flag guards. Default TRUE post "
                "graduation 2026-05-04. Re-read on every render — "
                "flips take effect immediately for the next tool "
                "call without restart."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/battle_test/tool_render_view.py"
            ),
            example="true",
            since="Gap #2 Slice 5 (2026-05-04)",
        ),
        FlagSpec(
            name="JARVIS_TOOL_RENDER_DENSITY",
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for adaptive density resolution. "
                "When set to ``compact`` / ``balanced`` / ``verbose``, "
                "skips the (Posture × LayoutKind) table lookup and "
                "applies the named level directly. Unrecognized / "
                "blank values are silently ignored (table lookup "
                "applies). Useful for screen-recording sessions "
                "(``verbose``) or stabilization sprints (``compact``)."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/battle_test/tool_render_policy.py"
            ),
            example="balanced",
            since="Gap #2 Slice 5 (2026-05-04)",
        ),
        FlagSpec(
            name="JARVIS_TOOL_RENDER_STORE_SIZE",
            type=FlagType.INT,
            default=50,
            description=(
                "Capacity of the BoundedBodyStore (Slice 3) — the "
                "session-scoped ring of full tool-result bodies "
                "parked behind ``/expand <ref>`` recovery hints. "
                "Drop-oldest eviction; clamped to [1, 10_000]. "
                "Increase for deep-explore sessions, decrease for "
                "memory-tight environments."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/battle_test/tool_render_store.py"
            ),
            example="50",
            since="Gap #2 Slice 5 (2026-05-04)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[ToolRenderView] flag registration failed for %s",
                getattr(spec, "name", "?"), exc_info=True,
            )
    return count


# ===========================================================================
# Slice 5 — shipped_code_invariants self-registration (auto-discovered
# via the battle_test entry in ``_INVARIANT_PROVIDER_PACKAGES``)
# ===========================================================================


def register_shipped_invariants() -> list:
    """Module-owned shipped-code AST pins. Three structural
    invariants that lock in Gap #2's correctness-critical surfaces:

      1. ``tool_render_view_public_surface`` — the load-bearing
         exports (``compose``, ``compose_if_enabled``,
         ``is_master_flag_enabled``) MUST remain present. Renaming
         any of them silently breaks the call-site wiring.
      2. ``tool_render_registry_descriptor_completeness`` — the
         ``_DESCRIPTORS`` dict MUST contain entries for every Venom
         tool that production callers can render through. This is
         the structural guarantee behind "no hardcoded ``if/elif``
         chains downstream" — missing a descriptor would silently
         route the tool through the default fallback and lose its
         CC-style verb formatting.
      3. ``tool_render_policy_di_cage`` — ``tool_render_policy.py``
         MUST NOT top-level-import the stateful posture surfaces
         (``posture_observer``, ``posture_store``, ``posture_health``).
         Lazy imports inside ``Default*Provider`` methods are
         allowed; top-level imports would defeat the layered
         design and create import-time circular-dependency risk.

    NEVER raises (returns ``[]`` on import failure). Per house style.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    # ---- Pin 1: public surface of tool_render_view -----------------------

    _REQUIRED_VIEW_EXPORTS = (
        "compose", "compose_if_enabled", "is_master_flag_enabled",
    )

    def _validate_view_public_surface(tree, _source) -> tuple:
        del _source
        seen: set = set()
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef):
                seen.add(node.name)
        missing = [
            name for name in _REQUIRED_VIEW_EXPORTS if name not in seen
        ]
        if missing:
            return (
                f"tool_render_view.py missing required public "
                f"functions: {missing} — call sites in serpent_flow "
                "+ ouroboros_tui depend on these exact names",
            )
        return ()

    # ---- Pin 2: descriptor completeness in tool_render_registry ----------

    _REQUIRED_DESCRIPTORS = frozenset({
        "read_file", "list_symbols", "search_code", "run_tests",
        "get_callers", "glob_files", "list_dir", "git_log", "git_diff",
        "git_blame", "bash", "edit_file", "write_file", "delete_file",
        "type_check", "web_fetch", "web_search", "ask_human",
    })

    def _validate_descriptor_completeness(tree, _source) -> tuple:
        del _source
        # Walk for ``_DESCRIPTORS`` assignment (Mapping[str, ...] dict literal).
        descriptor_keys: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AnnAssign) or isinstance(node, _ast.Assign):
                targets = (
                    [node.target]
                    if isinstance(node, _ast.AnnAssign)
                    else node.targets
                )
                for tgt in targets:
                    if (
                        isinstance(tgt, _ast.Name)
                        and tgt.id == "_DESCRIPTORS"
                    ):
                        value = node.value
                        if isinstance(value, _ast.Dict):
                            for key_node in value.keys:
                                if isinstance(key_node, _ast.Constant) and isinstance(
                                    key_node.value, str,
                                ):
                                    descriptor_keys.add(key_node.value)
        missing = _REQUIRED_DESCRIPTORS - descriptor_keys
        if missing:
            return (
                f"_DESCRIPTORS missing entries for: {sorted(missing)} — "
                "every Venom tool kind must have an explicit descriptor "
                "(Gap #2 Slice 1 contract)",
            )
        return ()

    # ---- Pin 3: DI cage on tool_render_policy ----------------------------

    _FORBIDDEN_TOP_LEVEL_IMPORTS = frozenset({
        "backend.core.ouroboros.governance.posture_observer",
        "backend.core.ouroboros.governance.posture_store",
        "backend.core.ouroboros.governance.posture_health",
    })

    def _validate_policy_di_cage(tree, _source) -> tuple:
        del _source
        violations = []
        # Only inspect TOP-LEVEL import statements; ignore those inside
        # function bodies (lazy imports are intentional and required).
        for node in tree.body:
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if module in _FORBIDDEN_TOP_LEVEL_IMPORTS:
                    violations.append(
                        f"top-level ``from {module} import ...`` "
                        "violates the DI cage — use a lazy import "
                        "inside Default*Provider.current() instead"
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_TOP_LEVEL_IMPORTS:
                        violations.append(
                            f"top-level ``import {alias.name}`` "
                            "violates the DI cage"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="tool_render_view_public_surface",
            target_file=(
                "backend/core/ouroboros/battle_test/tool_render_view.py"
            ),
            description=(
                "tool_render_view.py must expose the load-bearing "
                "compose / compose_if_enabled / is_master_flag_enabled "
                "functions. Renaming any of them silently breaks the "
                "lazy-imported call-site wiring in serpent_flow + "
                "ouroboros_tui."
            ),
            validate=_validate_view_public_surface,
        ),
        ShippedCodeInvariant(
            invariant_name="tool_render_registry_descriptor_completeness",
            target_file=(
                "backend/core/ouroboros/battle_test/tool_render_registry.py"
            ),
            description=(
                "_DESCRIPTORS must cover every Venom tool kind. "
                "Missing a descriptor silently routes that tool "
                "through the default fallback and loses CC-verb "
                "formatting — defeats Gap #2's 'no hardcoded "
                "if/elif chains downstream' contract."
            ),
            validate=_validate_descriptor_completeness,
        ),
        ShippedCodeInvariant(
            invariant_name="tool_render_policy_di_cage",
            target_file=(
                "backend/core/ouroboros/battle_test/tool_render_policy.py"
            ),
            description=(
                "tool_render_policy.py must NOT top-level-import "
                "the stateful posture surfaces "
                "(posture_observer / posture_store / posture_health). "
                "Lazy imports inside Default*Provider methods are "
                "allowed; top-level imports defeat the layered "
                "design and create circular-dependency risk."
            ),
            validate=_validate_policy_di_cage,
        ),
    ]
