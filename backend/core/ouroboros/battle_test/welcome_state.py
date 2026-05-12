"""
Welcome State + Tutorial Renderer — §41.3 Slice 2 Onboarding
==============================================================

Closes §41.3 Slice 2 items:

* #15 Welcome banner on first launch — sentinel-based detection
  (``.jarvis/welcome_seen.flag``) gates an expanded onboarding
  banner so first-time operators see "what just happened, what
  to try first" rather than the post-graduation minimal banner.
* #17 Tutorial mode (``/tutorial``) — composes the existing
  :func:`repl_completion.discover_verbs` registry (no parallel
  state) to render a category-grouped tour of available slash
  commands with usage examples.

Composition contract:

* :func:`repl_completion.discover_verbs` — single source of truth
  for slash verbs; this substrate NEVER duplicates the registry.
  Both banner and tutorial walk the same descriptor graph.
* :func:`repl_completion.format_verb_help` — renders a single
  verb's detail block; tutorial composes it per-verb under each
  category header.
* :mod:`pathlib` — sentinel write/read; atomic create-if-absent
  so concurrent invocations are safe.

§33.1 master ``JARVIS_WELCOME_STATE_ENABLED`` default-**TRUE** —
welcome surfaces are friendly + advisory (no mutation outside the
sentinel file). Operator can disable for headless / scripted runs
via the flag.

Closed 4-value :class:`WelcomePhase`:

  FIRST_LAUNCH    sentinel absent — show expanded banner
  RETURNING       sentinel present — minimal greeting only
  FORCED          ``JARVIS_WELCOME_FORCE_SHOW=true`` regardless of
                  sentinel state (operator wants the tour again)
  DISABLED        master flag off

NEVER raises across permission errors, read-only filesystems,
missing parent directories, or corrupt sentinel files. The
sentinel write is best-effort; failure to mark "seen" simply
re-shows the banner on next launch (operator can dismiss with the
env flag).

Authority asymmetry (AST-pinned): stdlib only. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


WELCOME_STATE_SCHEMA_VERSION: str = "welcome_state.1"


_ENV_MASTER = "JARVIS_WELCOME_STATE_ENABLED"
_ENV_FORCE_SHOW = "JARVIS_WELCOME_FORCE_SHOW"
_ENV_SENTINEL_PATH = "JARVIS_WELCOME_SENTINEL_PATH"

_DEFAULT_SENTINEL_REL = ".jarvis/welcome_seen.flag"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-**TRUE**.

    Welcome surfaces are advisory only (no mutation outside the
    sentinel file). Default-on so first-time operators get the
    onboarding without flipping a flag.
    """
    return _flag(_ENV_MASTER, default=True)


def force_show() -> bool:
    """Operator override — re-show the expanded banner regardless of
    sentinel state."""
    return _flag(_ENV_FORCE_SHOW, default=False)


def sentinel_path() -> Path:
    raw = os.environ.get(_ENV_SENTINEL_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_SENTINEL_REL)


# Closed taxonomy


class WelcomePhase(str, enum.Enum):
    """Closed 4-value taxonomy — bytes-pinned via AST."""

    FIRST_LAUNCH = "first_launch"
    RETURNING = "returning"
    FORCED = "forced"
    DISABLED = "disabled"


_PHASE_GLYPH = {
    WelcomePhase.FIRST_LAUNCH.value: "🌱",
    WelcomePhase.RETURNING.value: "↻",
    WelcomePhase.FORCED.value: "🔁",
    WelcomePhase.DISABLED.value: "◌",
}


def phase_glyph(phase: object) -> str:
    """NEVER raises."""
    try:
        val = getattr(phase, "value", None)
        key = (
            str(val).strip().lower() if val is not None
            else str(phase or "").strip().lower()
        )
        return _PHASE_GLYPH.get(key, "?")
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifact


@dataclass(frozen=True)
class WelcomeState:
    """Frozen snapshot of welcome-state decision. Audit-safe."""

    phase: WelcomePhase
    sentinel_path: str
    sentinel_existed: bool
    evaluated_at_unix: float
    master_enabled: bool
    schema_version: str = WELCOME_STATE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "sentinel_path": self.sentinel_path[:512],
            "sentinel_existed": bool(self.sentinel_existed),
            "evaluated_at_unix": float(self.evaluated_at_unix),
            "master_enabled": bool(self.master_enabled),
            "schema_version": self.schema_version,
        }

    def should_show_expanded_banner(self) -> bool:
        """True iff caller should render the expanded onboarding
        banner. False for RETURNING + DISABLED."""
        return self.phase in (
            WelcomePhase.FIRST_LAUNCH, WelcomePhase.FORCED,
        )


# Pure-function detectors


def _sentinel_exists(path: Path) -> bool:
    """NEVER raises."""
    try:
        return path.exists() and path.is_file()
    except Exception:  # noqa: BLE001
        return False


def evaluate(
    *,
    now_unix: Optional[float] = None,
    path_override: Optional[Path] = None,
) -> WelcomeState:
    """Top-level decision function. NEVER raises."""
    now = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return WelcomeState(
            phase=WelcomePhase.DISABLED,
            sentinel_path=str(path_override or sentinel_path()),
            sentinel_existed=False,
            evaluated_at_unix=now,
            master_enabled=False,
        )
    target = path_override if path_override else sentinel_path()
    existed = _sentinel_exists(target)
    if force_show():
        return WelcomeState(
            phase=WelcomePhase.FORCED,
            sentinel_path=str(target),
            sentinel_existed=existed,
            evaluated_at_unix=now,
            master_enabled=True,
        )
    if existed:
        return WelcomeState(
            phase=WelcomePhase.RETURNING,
            sentinel_path=str(target),
            sentinel_existed=True,
            evaluated_at_unix=now,
            master_enabled=True,
        )
    return WelcomeState(
        phase=WelcomePhase.FIRST_LAUNCH,
        sentinel_path=str(target),
        sentinel_existed=False,
        evaluated_at_unix=now,
        master_enabled=True,
    )


def mark_seen(
    *,
    path_override: Optional[Path] = None,
) -> bool:
    """Write the sentinel file. NEVER raises.

    Best-effort: returns True on success, False on any failure
    (permission, read-only fs, etc.). Failure simply means the
    banner re-shows on next launch — graceful degradation, not
    a crash."""
    if not master_enabled():
        return False
    target = path_override if path_override else sentinel_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # touch() with exist_ok=True is idempotent + atomic on
        # most filesystems; preserves any prior timestamp metadata.
        target.touch(exist_ok=True)
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[WelcomeState] mark_seen failed for %s", target,
            exc_info=True,
        )
        return False


# Rendering


def render_first_launch_banner(
    verb_registry: Any = None,
) -> str:
    """Render the expanded first-launch onboarding block.

    Composes :func:`repl_completion.discover_verbs` lazily so
    callers without a verb registry on hand still get a useful
    banner (just without the verb-count line). NEVER raises.

    Output shape (operator-facing)::

        🌱 First launch — welcome to Ouroboros + Venom
          Try these to get oriented:
            /tutorial        guided tour of capabilities
            /help            list available commands
            /status          current op + cost + posture
          (banner hidden on next launch — set
           JARVIS_WELCOME_FORCE_SHOW=true to show it again)
    """
    try:
        lines: List[str] = [
            "🌱 First launch — welcome to Ouroboros + Venom",
            "  Try these to get oriented:",
        ]
        starter_verbs = (
            ("/tutorial", "guided tour of capabilities"),
            ("/help", "list available commands"),
            ("/status", "current op + cost + posture"),
        )
        if verb_registry is not None:
            # Verify each starter actually exists in the registry;
            # otherwise fall back to the default description.
            for slash, default_desc in starter_verbs:
                try:
                    found = verb_registry.find(slash)
                    desc = (
                        found.description
                        if found and found.description
                        else default_desc
                    )
                except Exception:  # noqa: BLE001
                    desc = default_desc
                lines.append(f"    {slash:<16} {desc}")
            try:
                total = len(verb_registry)
                lines.append(
                    f"  ({total} verbs total — `/help` for the "
                    f"full palette)"
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            for slash, desc in starter_verbs:
                lines.append(f"    {slash:<16} {desc}")
        lines.append(
            "  (banner hidden on next launch — set "
            "JARVIS_WELCOME_FORCE_SHOW=true to show it again)"
        )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return "🌱 First launch — welcome to Ouroboros + Venom"


def render_tutorial(
    verb_registry: Any,
    *,
    category_filter: Optional[str] = None,
    examples_per_verb: int = 2,
) -> str:
    """Render the category-grouped tutorial. NEVER raises.

    Walks the verb registry, groups by :class:`VerbCategory`, and
    renders each group with a header + per-verb usage line. When
    ``category_filter`` is supplied (a string like ``"lifecycle"``
    or a :class:`VerbCategory` enum value), only that category is
    rendered.

    Composes :func:`repl_completion.format_verb_help` lazily for
    detailed per-verb blocks within each section.
    """
    if verb_registry is None:
        return "tutorial: no verb registry available"
    try:
        from backend.core.ouroboros.battle_test.repl_completion import (  # noqa: E501
            format_verb_help,
        )
    except Exception:  # noqa: BLE001
        format_verb_help = None  # type: ignore[assignment]
    try:
        categories = verb_registry.categories()
    except Exception:  # noqa: BLE001
        return "tutorial: registry has no categories"
    # Normalize filter
    filter_value: Optional[str] = None
    if category_filter is not None:
        try:
            filter_value = (
                category_filter.value
                if hasattr(category_filter, "value")
                else str(category_filter).strip().lower()
            )
        except Exception:  # noqa: BLE001
            filter_value = None
    lines: List[str] = [
        "🎓 Ouroboros + Venom — Operator Tutorial",
        "",
    ]
    rendered_any = False
    for cat in categories:
        if filter_value is not None and cat != filter_value:
            continue
        try:
            verbs = verb_registry.by_category(cat)
        except Exception:  # noqa: BLE001
            continue
        if not verbs:
            continue
        rendered_any = True
        lines.append(f"== {cat.upper()} ==")
        for verb in verbs:
            try:
                if format_verb_help is not None:
                    block = format_verb_help(verb)
                    # Indent the help block under the category
                    indented = "\n".join(
                        f"  {ln}" for ln in block.splitlines()
                    )
                    lines.append(indented)
                else:
                    lines.append(
                        f"  {verb.slash_form}  {verb.description}"
                    )
                # Cap examples shown to ``examples_per_verb``
                if (
                    examples_per_verb > 0
                    and verb.examples
                    and format_verb_help is None
                ):
                    for ex in verb.examples[:examples_per_verb]:
                        lines.append(f"    e.g. {ex}")
            except Exception:  # noqa: BLE001
                continue
            lines.append("")
    if not rendered_any:
        if filter_value is not None:
            return (
                f"tutorial: no verbs in category "
                f"{filter_value!r}"
            )
        return "tutorial: registry contains no verbs"
    lines.append(
        "Tip: append ``--help`` to any verb for its full usage "
        "(e.g. ``/cancel --help``)."
    )
    return "\n".join(lines)


# ===========================================================================
# §41.3 #16 — Setup walkthrough (env-flag tour)
# ===========================================================================
#
# Composes the canonical :mod:`flag_registry` (no parallel registry,
# no hardcoded flag list). Section structure auto-derives from the
# 8-slot ``Category`` enum — adding a new category in flag_registry
# automatically produces a new section here. Per-flag display reads
# from ``FlagSpec`` fields + current env state (via ``os.environ``).
#
# Operator binding: the walkthrough is DESCRIPTIVE only — never
# mutates env state. Operator scrolls through, sees what's
# configurable, decides which to flip in their shell.


# Relevance → glyph table. Keys mirror the closed 3-value
# :class:`flag_registry.Relevance` taxonomy. Operators can theme
# by passing a custom mapping; absent keys fall back to defaults.
_FLAG_RELEVANCE_GLYPHS: Tuple[Tuple[str, str], ...] = (
    ("critical", "🔥"),
    ("relevant", "📌"),
    ("ignored", "·"),
)


# Sentinel marker for flags whose current value matches the
# default — operator sees at a glance which knobs they've
# actually customized.
_FLAG_AT_DEFAULT_MARKER: str = "(default)"
_FLAG_OVERRIDDEN_MARKER: str = "(overridden)"


# Valid scope values for :func:`render_setup_walkthrough`. Closed
# 3-value taxonomy — drift caught by an AST pin in the registry-
# composition validator.
_SETUP_SCOPES: Tuple[str, ...] = ("all", "critical", "relevant")


def _relevance_glyph_for(rel_value: object) -> str:
    """NEVER raises."""
    try:
        key = (
            rel_value.value if hasattr(rel_value, "value")
            else str(rel_value or "").strip().lower()
        )
    except Exception:  # noqa: BLE001
        return "·"
    for k, g in _FLAG_RELEVANCE_GLYPHS:
        if k == key:
            return g
    return "·"


def _read_env_value(name: object, default: Any) -> Tuple[str, bool]:
    """Read the current env value for a flag. Returns
    ``(value_repr, is_overridden)``. NEVER raises."""
    try:
        env_name = str(name or "")
    except Exception:  # noqa: BLE001
        return ("", False)
    if not env_name:
        return ("", False)
    raw = os.environ.get(env_name, "")
    if raw == "":
        # Not set in env → using default
        try:
            return (str(default), False)
        except Exception:  # noqa: BLE001
            return ("", False)
    return (raw, True)


def render_setup_walkthrough(
    flag_registry: Any = None,
    *,
    scope: str = "all",
    posture: Optional[str] = None,
    indent: str = "  ",
    category_filter: Optional[str] = None,
    max_per_section: int = 8,
) -> str:
    """Render the env-flag setup walkthrough. NEVER raises.

    Composes the canonical
    :class:`flag_registry.FlagRegistry` — NO parallel registry,
    NO hardcoded flag list. Section structure auto-derives from
    the 8-slot ``Category`` enum (adding a new category in
    flag_registry automatically produces a new section here).

    Parameters
    ----------
    flag_registry:
        Canonical registry. When None, composes
        ``flag_registry.get_default_registry()`` (which is
        auto-seeded at import time).
    scope:
        One of :data:`_SETUP_SCOPES`:
          * ``"all"`` — every registered flag (default)
          * ``"critical"`` — only flags tagged CRITICAL for the
            posture
          * ``"relevant"`` — flags tagged CRITICAL or RELEVANT
    posture:
        Posture string (e.g., ``"HARDEN"``) used for the
        ``critical`` / ``relevant`` filter. Ignored when
        scope=``"all"``.
    indent:
        Per-line indent string (default ``"  "``).
    category_filter:
        When provided, restrict output to that category's
        section. Useful for ``/tutorial setup safety`` etc.
    max_per_section:
        Cap per-category display to N flags (default 8 — keeps
        the first-launch tour scannable). Pass 0 to disable the
        cap and dump everything (used with ``/tutorial setup
        all`` to see the full registry).
    """
    # Compose the canonical registry — NO parallel state. Uses
    # ``ensure_seeded`` so the discovery walker populates the
    # registry from all registered modules; the operator sees
    # the LIVE flag surface, not a frozen snapshot.
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501  # type: ignore[import-not-found]
            Category,
            Relevance,
            ensure_seeded,
        )
    except Exception:  # noqa: BLE001
        return "setup: flag_registry unavailable"
    registry = (
        flag_registry if flag_registry is not None
        else ensure_seeded()
    )
    if registry is None:
        return "setup: no registry resolved"

    # Normalize inputs.
    scope_clean = (
        str(scope or "all").strip().lower()
    )
    if scope_clean not in _SETUP_SCOPES:
        scope_clean = "all"
    cat_filter: Optional[str] = None
    if category_filter is not None:
        try:
            cat_filter = (
                str(category_filter or "").strip().lower()
            )
        except Exception:  # noqa: BLE001
            cat_filter = None
    posture_clean: Optional[str] = None
    if posture is not None:
        try:
            posture_clean = str(posture).strip().upper()
        except Exception:  # noqa: BLE001
            posture_clean = None

    # Filter set: when scope != "all", compose registry's
    # canonical posture filter. Returns a list of FlagSpec.
    posture_filtered_names: Optional[set] = None
    if scope_clean != "all" and posture_clean:
        try:
            min_rel = (
                Relevance.CRITICAL if scope_clean == "critical"
                else Relevance.RELEVANT
            )
            posture_specs = registry.relevant_to_posture(
                posture_clean, min_relevance=min_rel,
            )
            posture_filtered_names = {
                s.name for s in posture_specs
            }
        except Exception:  # noqa: BLE001
            posture_filtered_names = None

    lines: List[str] = [
        "⚙  Ouroboros + Venom — Operator Setup",
    ]
    if scope_clean == "all":
        lines.append(
            f"{indent}scope: all flags (no posture filter)"
        )
    else:
        lines.append(
            f"{indent}scope: {scope_clean}"
            + (
                f" · posture={posture_clean}"
                if posture_clean else ""
            )
        )
    lines.append("")

    # Walk the Category enum in declaration order — closed 8-value
    # taxonomy is the canonical section structure. Adding a
    # category in flag_registry automatically adds a section here.
    sections_rendered = 0
    flags_rendered = 0
    for cat in Category:
        if cat_filter and cat.value.lower() != cat_filter:
            continue
        try:
            specs_in_cat = registry.list_by_category(cat)
        except Exception:  # noqa: BLE001
            continue
        # Apply posture filter on top of category filter.
        if posture_filtered_names is not None:
            specs_in_cat = [
                s for s in specs_in_cat
                if s.name in posture_filtered_names
            ]
        if not specs_in_cat:
            continue
        sections_rendered += 1
        # Apply per-section cap unless scope=all OR cap=0 OR a
        # category filter is set (operator explicitly drilling
        # into one section — they want to see everything in it).
        cap_active = (
            max_per_section > 0
            and scope_clean != "all"
            and cat_filter is None
        )
        total_in_cat = len(specs_in_cat)
        if cap_active:
            specs_in_cat = specs_in_cat[:max_per_section]
        elided = total_in_cat - len(specs_in_cat)
        lines.append(f"== {cat.value.upper()} ==")
        for spec in specs_in_cat:
            flags_rendered += 1
            value_repr, overridden = _read_env_value(
                spec.name, spec.default,
            )
            # Posture-relevance glyph for the current posture
            # (when set). Falls back to space (no glyph) when
            # the flag has no relevance for this posture.
            rel_glyph = " "
            if posture_clean:
                rel = spec.posture_relevance.get(posture_clean)
                if rel is not None:
                    rel_glyph = _relevance_glyph_for(rel)
            override_marker = (
                _FLAG_OVERRIDDEN_MARKER if overridden
                else _FLAG_AT_DEFAULT_MARKER
            )
            try:
                type_repr = spec.type.value
            except Exception:  # noqa: BLE001
                type_repr = "?"
            # Truncate value for compact display
            value_display = value_repr
            if len(value_display) > 40:
                value_display = value_display[:37] + "..."
            header = (
                f"{indent}{rel_glyph} {spec.name}  "
                f"[{type_repr}]  "
                f"value={value_display!r} {override_marker}"
            )
            lines.append(header)
            try:
                desc = (spec.description or "").strip()
                if desc:
                    lines.append(f"{indent}    {desc[:200]}")
            except Exception:  # noqa: BLE001
                pass
            try:
                ex = (spec.example or "").strip()
                if ex:
                    lines.append(f"{indent}    example: {ex[:200]}")
            except Exception:  # noqa: BLE001
                pass
        if elided > 0:
            lines.append(
                f"{indent}  …+{elided} more in {cat.value.upper()} "
                f"— use `/tutorial setup {cat.value.lower()}` to see all"
            )
        lines.append("")

    if sections_rendered == 0:
        return (
            "setup: no flags matched the current filter "
            f"(scope={scope_clean}"
            + (f", posture={posture_clean}" if posture_clean else "")
            + (f", category={cat_filter}" if cat_filter else "")
            + ")"
        )

    lines.append(
        f"{indent}({flags_rendered} flag(s) across "
        f"{sections_rendered} category section(s))"
    )
    lines.append(
        f"{indent}Tip: set any flag in your shell before "
        f"launching — `export {indent and 'JARVIS_FOO_ENABLED=true'}`"
    )
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501  # type: ignore[import-not-found]
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/battle_test/welcome_state.py"
    )

    _EXPECTED_PHASE = {
        "first_launch", "returning", "forced", "disabled",
    }

    def _validate_phase_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "WelcomePhase"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_PHASE - found
                extra = found - _EXPECTED_PHASE
                if missing:
                    return (f"WelcomePhase missing: {sorted(missing)}",)
                if extra:
                    return (f"WelcomePhase drift: {sorted(extra)}",)
                return ()
        return ("WelcomePhase class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(f"forbidden import: {mod}")
        return tuple(violations)

    def _validate_master_default_true(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Welcome surfaces are advisory + friendly; master
        default-TRUE is the right shape (unlike cognitive
        substrates which default-FALSE). This pin AST-validates
        the chosen default so a future refactor that flips it to
        FALSE is caught."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) with "
                    "default=True (welcome is advisory)",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "repl_completion" not in source:
            violations.append("must compose repl_completion (verb registry)")
        if "format_verb_help" not in source:
            violations.append("must compose format_verb_help")
        if "pathlib" not in source:
            violations.append("must use stdlib pathlib")
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="welcome_state_phase_taxonomy_closed",
            target_file=target,
            description="WelcomePhase 4-value taxonomy bytes-pinned.",
            validate=_validate_phase_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="welcome_state_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — advisory layer. MUST NOT "
                "import orchestrator / iron_gate / policy / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="welcome_state_master_default_true",
            target_file=target,
            description=(
                "Welcome substrate is advisory + friendly; "
                "default-TRUE is the intended shape (unlike "
                "cognitive substrates which default-FALSE)."
            ),
            validate=_validate_master_default_true,
        ),
        ShippedCodeInvariant(
            invariant_name="welcome_state_composes_canonical",
            target_file=target,
            description=(
                "Composes repl_completion verb registry + "
                "format_verb_help (no parallel state); stdlib "
                "pathlib for sentinel I/O."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = "backend/core/ouroboros/battle_test/welcome_state.py"

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Welcome state master. §41.3 Slice 2 #15 + #17. "
                "Default-TRUE (advisory; gates expanded first-"
                "launch banner + /tutorial verb output)."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=false",
        ),
        FlagSpec(
            name=_ENV_FORCE_SHOW,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Force expanded banner regardless of sentinel "
                "state. Useful for re-tour after major upgrades."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_FORCE_SHOW}=true",
        ),
        FlagSpec(
            name=_ENV_SENTINEL_PATH,
            type=FlagType.STR,
            default=_DEFAULT_SENTINEL_REL,
            description=(
                "Path to the first-launch sentinel file. "
                "Default `.jarvis/welcome_seen.flag`."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_SENTINEL_PATH}=~/.jarvis_welcome.flag"
            ),
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "WELCOME_STATE_SCHEMA_VERSION",
    "WelcomePhase",
    "WelcomeState",
    "evaluate",
    "force_show",
    "master_enabled",
    "mark_seen",
    "phase_glyph",
    "register_flags",
    "register_shipped_invariants",
    "render_first_launch_banner",
    "render_setup_walkthrough",
    "render_tutorial",
    "sentinel_path",
]
