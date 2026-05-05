"""PresentationRestraint — CC-style minimal boot for O+V (with emojis kept).
============================================================================

Slice 1 of the **Gap #7 closure arc** (presentation restraint).

Root problem
------------

O+V's boot screen is a **dense dashboard** (~30 lines): preflight
checklist + shutdown-diagnostics startup logs + ASCII banner +
session metadata + 6-Layer Organism block + activity ribbon. Claude
Code's boot is **~5 lines** of essential context — the operator
pulls deeper info on demand via verbs (``/help``, ``/status``).

The dense dashboard isn't *wrong* — O+V is a more complex system
than CC and operators need that visibility. But pushing it ALL at
boot, on EVERY boot, is information overload that obscures the
prompt operators need to interact with. The fix is **pull-on-demand
restraint**: keep the rich content, surface it via REPL verbs
instead of forcing it into the boot stream.

Slice 1 scope
-------------

* :class:`MinimalWelcomePayload` frozen record (welcome text + the
  one-shot context line that follows it)
* :func:`render_minimal_welcome` — the CC-style panel renderer.
  Operator's emojis (``🐍``, ``⚙️ ``, etc.) are KEPT inside the panel
  — restraint is about **count and density**, not erasing identity.
* :func:`render_preflight` / :func:`render_organism` — the moved
  content. Same rendering used by ``/preflight`` / ``/organism`` REPL
  verbs.
* :func:`suppress_diagnostic_logs` — turns OFF propagation of
  ``jarvis.shutdown.diagnostics`` INFO logs to the root logger so
  the boot screen isn't littered with internal forensic startup
  noise (the file handler at DEBUG level still captures everything
  for post-hoc analysis).
* In-process **layer capture** — :func:`set_captured_layers` /
  :func:`get_captured_layers` so ``/organism`` re-renders the same
  data the harness computed at boot (avoids re-running expensive
  feature-detection logic).

Master flag :data:`MASTER_FLAG_ENV_VAR` defaults **false** during
this slice; Slice 5 graduation flips to true.

Architectural reuse — zero duplication
---------------------------------------

* ``Rich.Panel`` for the welcome panel (already used by `diff_preview`
  and `ouroboros_tui` — same primitive).
* :class:`StatusLineBuilder` snapshot for the context line below
  the panel (Gap #1+5 substrate).
* ``logging.Logger.propagate`` — stdlib mechanism for the diag log
  fix, not a parallel filter framework.
* House style: frozen dataclass, ``schema_version``, module-owned
  ``register_flags`` / ``register_shipped_invariants`` (Slice 5).

Authority boundary
------------------

* §1 deterministic — pure rendering + state read; no LLM, no I/O on
  the hot path
* §7 fail-closed — every helper degrades silently on bad input;
  rendering NEVER raises into the boot path
* §8 observable — captured-layers store is queryable for
  ``GET /observability/organism`` (Slice 5 follow-up)

What this module does NOT do
----------------------------

* Change palettes or emojis (Slice 2 owns palette discipline; emojis
  are kept by operator decision).
* Rewrite the legacy verbose render — kept verbatim under the
  master-flag-OFF branch for byte-identical rollback.
* Surface the new ``/preflight`` / ``/organism`` REPL handlers —
  those live in ``serpent_flow.py``'s :class:`SerpentREPL` and are
  added by the same slice.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PresentationRestraint")


# ===========================================================================
# Schema + master flag
# ===========================================================================


PRESENTATION_RESTRAINT_SCHEMA_VERSION: str = "presentation_restraint.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_PRESENTATION_RESTRAINT_ENABLED"


def is_restraint_enabled() -> bool:
    """``JARVIS_PRESENTATION_RESTRAINT_ENABLED``. Default false during
    this slice. Slice 5 graduation flips to true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class MinimalWelcomePayload:
    """Frozen content of the minimal welcome surface.

    Two lines of essential context wrapped in one ``Rich.Panel``.
    The operator's emojis are preserved — restraint is about
    information density, not identity erasure.
    """

    title: str  # e.g. "🐍 OUROBOROS + VENOM"
    subtitle: str  # e.g. "autonomous coding organism"
    verb_hints: Tuple[Tuple[str, str], ...]
    # Each tuple is (verb, description), e.g. ("/preflight", "provider + venom + L2 status")
    cwd_str: str
    branch: str
    cost_cap_str: str  # e.g. "$0.50"
    idle_timeout_str: str  # e.g. "600s"
    mode_str: str
    schema_version: str = PRESENTATION_RESTRAINT_SCHEMA_VERSION


_DEFAULT_VERB_HINTS: Tuple[Tuple[str, str], ...] = (
    ("/help", "commands & verbs"),
    ("/preflight", "provider + venom + L2 status"),
    ("/organism", "6-layer trinity status"),
    ("/expand <ref>", "recover artifact (t-/d-/o-/n-)"),
)


# ===========================================================================
# In-process layer capture — backs /organism
# ===========================================================================


_captured_layers: Optional[Tuple[Tuple[str, str, bool, str], ...]] = None
_capture_lock = threading.Lock()


def set_captured_layers(
    layers: Optional[Sequence[Tuple[Any, ...]]],
) -> None:
    """Capture the harness-computed 6-layer status tuple so ``/organism``
    can re-render without re-running feature detection. Called by
    :func:`render_minimal_welcome` at boot. NEVER raises — bad input
    coerces to ``None`` (the verb then prints a graceful "no layers
    captured yet" hint)."""
    global _captured_layers
    if layers is None:
        with _capture_lock:
            _captured_layers = None
        return
    safe: List[Tuple[str, str, bool, str]] = []
    try:
        for entry in layers:
            if not isinstance(entry, (tuple, list)) or len(entry) < 4:
                continue
            icon = str(entry[0]) if entry[0] is not None else ""
            name = str(entry[1]) if entry[1] is not None else ""
            is_on = bool(entry[2])
            detail = str(entry[3]) if entry[3] is not None else ""
            safe.append((icon, name, is_on, detail))
    except (TypeError, ValueError):
        with _capture_lock:
            _captured_layers = None
        return
    with _capture_lock:
        _captured_layers = tuple(safe)


def get_captured_layers() -> Optional[Tuple[Tuple[str, str, bool, str], ...]]:
    """Return the most recently captured layers, or ``None`` if no
    boot has run yet (e.g. the REPL was started without a harness
    boot, like a smoke test)."""
    with _capture_lock:
        return _captured_layers


def clear_captured_layers_for_tests() -> None:
    """Test isolation hook."""
    global _captured_layers
    with _capture_lock:
        _captured_layers = None


# ===========================================================================
# Diagnostic-log suppression — boot-noise reduction
# ===========================================================================


_diag_propagation_originally: Optional[bool] = None


def suppress_diagnostic_logs() -> bool:
    """Stop the ``jarvis.shutdown.diagnostics`` logger from propagating
    INFO messages to the root logger (which has a default StreamHandler
    spilling forensic startup noise onto the operator's screen).

    The diagnostics module's own file handler keeps writing at DEBUG
    level — full forensics are preserved at
    ``~/.jarvis/trinity/shutdown_diagnostics.log``. Only the
    *operator-facing* INFO leak to stderr is suppressed.

    Returns ``True`` on success. Idempotent — calling twice is a no-op.
    NEVER raises.
    """
    global _diag_propagation_originally
    try:
        diag_logger = logging.getLogger("jarvis.shutdown.diagnostics")
        if _diag_propagation_originally is None:
            _diag_propagation_originally = diag_logger.propagate
        diag_logger.propagate = False
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PresentationRestraint] suppress_diagnostic_logs failed",
            exc_info=True,
        )
        return False


def restore_diagnostic_logs_for_tests() -> None:
    """Test isolation: restore the original propagation state."""
    global _diag_propagation_originally
    if _diag_propagation_originally is None:
        return
    try:
        diag_logger = logging.getLogger("jarvis.shutdown.diagnostics")
        diag_logger.propagate = _diag_propagation_originally
    except Exception:  # noqa: BLE001
        pass
    _diag_propagation_originally = None


# ===========================================================================
# Render helpers — Rich-based, defensive
# ===========================================================================


def render_minimal_welcome(
    console: object,
    *,
    session_id: str = "",
    branch: str = "",
    cost_cap: float = 0.0,
    idle_timeout_s: float = 0.0,
    mode_str: str = "",
    cwd_str: str = "",
    verb_hints: Optional[Sequence[Tuple[str, str]]] = None,
    title: str = "🐍 OUROBOROS + VENOM",
    subtitle: str = "autonomous coding organism",
) -> bool:
    # ``session_id`` is accepted for caller compatibility (the harness
    # passes it) but deliberately NOT shown at boot — CC restraint:
    # operators retrieve it via ``/status``. Suppress unused-arg warnings.
    del session_id
    """Render the CC-style minimal welcome panel.

    Two output regions:

      * A bordered ``Rich.Panel`` containing title + subtitle +
        verb hints (3-4 lines).
      * A flat context line below the panel: ``cwd · branch ·
        budget · idle · mode``.

    NEVER raises. Returns ``True`` on success, ``False`` if the
    console / Rich primitives aren't available.
    """
    print_fn = getattr(console, "print", None)
    if not callable(print_fn):
        return False

    try:
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        # Fallback: print plain lines without panel (still respects the
        # restraint contract — no multi-section dashboard).
        try:
            print_fn(f"\n  {title}")
            print_fn(f"  {subtitle}")
            for verb, desc in (verb_hints or _DEFAULT_VERB_HINTS):
                print_fn(f"    {verb}  {desc}")
            print_fn()
            print_fn(_format_context_line(
                cwd_str, branch, cost_cap, idle_timeout_s, mode_str,
            ))
            print_fn()
        except Exception:  # noqa: BLE001
            return False
        return True

    hints = verb_hints if verb_hints is not None else _DEFAULT_VERB_HINTS

    # Build the panel body. Rich accepts plain text or a Text instance.
    body = Text()
    body.append(f"{title}\n", style="bold cyan")
    body.append(f"{subtitle}\n", style="dim")
    body.append("\n")
    # Verb hints — 2-space indent per CC's pattern.
    for verb, desc in hints:
        body.append(f"  {verb:<14s}", style="bright_blue")
        body.append(f"{desc}\n", style="dim")

    try:
        panel = Panel(
            body,
            border_style="dim",
            padding=(0, 2),
            expand=False,
        )
        print_fn()
        print_fn(panel)
        # Context line below the panel — matches CC's "cwd:" format.
        print_fn(_format_context_line(
            cwd_str, branch, cost_cap, idle_timeout_s, mode_str,
        ))
        print_fn()
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PresentationRestraint] panel render failed",
            exc_info=True,
        )
        return False


def _format_context_line(
    cwd_str: str, branch: str,
    cost_cap: float, idle_timeout_s: float, mode_str: str,
) -> str:
    """One-line context summary. Plain Rich markup string."""
    parts: List[str] = []
    if cwd_str:
        parts.append(f"[dim]cwd:[/dim] {cwd_str}")
    if branch:
        parts.append(f"[dim]branch:[/dim] {branch}")
    parts.append(f"[dim]budget:[/dim] $0.00 / ${cost_cap:.2f}")
    if idle_timeout_s > 0:
        parts.append(f"[dim]idle:[/dim] 0s / {idle_timeout_s:.0f}s")
    if mode_str:
        parts.append(f"[dim]mode:[/dim] {mode_str}")
    return "  " + "  [dim]·[/dim]  ".join(parts)


def render_preflight(
    console: object,
    *,
    checks: Optional[Sequence[Mapping[str, Any]]] = None,
) -> bool:
    """Render the preflight checklist on demand. Used by ``/preflight``
    REPL verb.

    When ``checks`` is ``None``, the function re-runs the standard
    env-based feature detection (matches what
    ``scripts/ouroboros_battle_test.py:_print_preflight`` checks).

    Each check dict has keys: ``label`` (str), ``env_key`` (str),
    ``detail`` (str). The function reads ``os.environ`` per call so
    operators get a current snapshot, not a stale boot-time view.
    """
    print_fn = getattr(console, "print", None)
    if not callable(print_fn):
        return False

    if checks is None:
        checks = _default_preflight_checks()

    try:
        print_fn()
        print_fn("[bold cyan]  Preflight Checklist[/bold cyan]")
        print_fn("[dim]  " + "─" * 52 + "[/dim]")
        for check in checks:
            label = str(check.get("label", "?"))
            env_key = str(check.get("env_key", ""))
            detail = str(check.get("detail", ""))
            is_on = bool(os.environ.get(env_key, ""))
            mark = (
                "[bright_green]ON[/bright_green]"
                if is_on else "[dim]OFF[/dim]"
            )
            print_fn(
                f"  [{mark}] {label:<30s} [dim]{detail}[/dim]"
            )
        rounds = os.environ.get("JARVIS_GOVERNED_TOOL_MAX_ROUNDS", "10")
        print_fn(
            f"\n[dim]  Tool rounds: {rounds} "
            "(deadline-based, safety ceiling)[/dim]"
        )
        # API-key warnings (matches the legacy script's behavior).
        has_dw = bool(os.environ.get("DOUBLEWORD_API_KEY"))
        has_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not has_dw and not has_claude:
            print_fn(
                "  [bold red]ERROR: No API keys set. "
                "Export DOUBLEWORD_API_KEY or ANTHROPIC_API_KEY.[/bold red]"
            )
        elif not has_claude:
            print_fn(
                "  [yellow]WARNING: ANTHROPIC_API_KEY not set "
                "— no Claude fallback.[/yellow]"
            )
        elif not has_dw:
            print_fn(
                "  [yellow]WARNING: DOUBLEWORD_API_KEY not set "
                "— Claude only (expensive).[/yellow]"
            )
        print_fn()
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PresentationRestraint] render_preflight failed",
            exc_info=True,
        )
        return False


def _default_preflight_checks() -> Tuple[Mapping[str, str], ...]:
    """Default preflight check tuple — matches the legacy
    ``_print_preflight`` content in the battle-test script.

    Single source of truth for the boot AND the ``/preflight`` REPL
    verb, so adding a new check requires editing one place.
    """
    l2_iters = os.environ.get("JARVIS_L2_MAX_ITERS", "5")
    l2_timebox = os.environ.get("JARVIS_L2_TIMEBOX_S", "120")
    return (
        {
            "label": "Provider: DoubleWord 397B",
            "env_key": "DOUBLEWORD_API_KEY",
            "detail": "$0.10/$0.40/M (Tier 0 PRIMARY)",
        },
        {
            "label": "Provider: Claude Sonnet",
            "env_key": "ANTHROPIC_API_KEY",
            "detail": "$3/$15/M (Tier 1 FALLBACK)",
        },
        {
            "label": "Venom: Tool Loop",
            "env_key": "JARVIS_GOVERNED_TOOL_USE_ENABLED",
            "detail": "read_file, search_code, get_callers, list_symbols",
        },
        {
            "label": "Venom: Bash (100+ cmds)",
            "env_key": "JARVIS_BASH_TOOL_ENABLED",
            "detail": "python, git, docker, curl, terraform...",
        },
        {
            "label": "Venom: Web Search",
            "env_key": "JARVIS_WEB_TOOL_ENABLED",
            "detail": "DuckDuckGo / Brave / Google CSE",
        },
        {
            "label": "Venom: Run Tests",
            "env_key": "JARVIS_TOOL_RUN_TESTS_ALLOWED",
            "detail": "pytest in sandbox during generation",
        },
        {
            "label": "L2 Repair Engine",
            "env_key": "JARVIS_L2_ENABLED",
            "detail": f"max {l2_iters} iters, {l2_timebox}s timebox",
        },
        {
            "label": "Trinity Consciousness",
            "env_key": "JARVIS_CONSCIOUSNESS_ENABLED",
            "detail": "Memory + Prophecy + Health",
        },
    )


def render_organism(
    console: object,
    *,
    layers: Optional[Sequence[Tuple[Any, ...]]] = None,
) -> bool:
    """Render the 6-layer organism block on demand. Used by
    ``/organism`` REPL verb.

    When ``layers`` is ``None``, queries :func:`get_captured_layers`
    for the harness-computed boot snapshot. If no snapshot is
    available, prints a graceful hint.
    """
    print_fn = getattr(console, "print", None)
    if not callable(print_fn):
        return False

    if layers is None:
        layers = get_captured_layers()

    if not layers:
        try:
            print_fn(
                "[dim]  No organism state captured yet — "
                "harness boot has not completed.[/dim]"
            )
        except Exception:  # noqa: BLE001
            return False
        return True

    try:
        print_fn()
        print_fn("[bold]── 6-Layer Organism ──[/bold]")
        for entry in layers:
            if not isinstance(entry, (tuple, list)) or len(entry) < 4:
                continue
            icon = str(entry[0]) if entry[0] is not None else ""
            name = str(entry[1]) if entry[1] is not None else ""
            is_on = bool(entry[2])
            detail = str(entry[3]) if entry[3] is not None else ""
            mark = (
                "[bright_green]ON[/bright_green]"
                if is_on else "[dim]OFF[/dim]"
            )
            print_fn(
                f"  {icon}  {name:<24s} {mark}  [dim]{detail}[/dim]"
            )
        print_fn()
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PresentationRestraint] render_organism failed",
            exc_info=True,
        )
        return False


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "MinimalWelcomePayload",
    "PRESENTATION_RESTRAINT_SCHEMA_VERSION",
    "chrome_color",
    "clear_captured_layers_for_tests",
    "format_idle_breadcrumb",
    "get_captured_layers",
    "is_restraint_enabled",
    "real_stdout_isatty",
    "render_minimal_welcome",
    "render_organism",
    "render_preflight",
    "restore_diagnostic_logs_for_tests",
    "set_captured_layers",
    "suppress_diagnostic_logs",
]


# ===========================================================================
# Slice 2 — chrome color discipline + idle status content + TTY gate fix
# ===========================================================================


def chrome_color(default: str = "bright_green") -> str:
    """Return ``"dim"`` when presentation restraint is enabled,
    otherwise ``default``.

    **Rationale (Constraint: green = success outcomes only).** The
    legacy palette uses ``bright_green`` (``_C['life']``) for both
    *outcomes* (✨ evolved, ✓ test passed, 📝 committed) AND *chrome*
    (event-stream activity ribbon, "🔋 Organism alive" line, section
    headers). Operators can't distinguish "this op succeeded" from
    "boot decoration" at a glance.

    This helper lets callers thread a master-flag-aware color choice
    into a single seam without changing the legacy palette dict (which
    is shared across many other rendering paths). When restraint is
    on: chrome turns dim; outcomes (which call sites pass their own
    explicit color, not via this helper) stay bright_green.

    NEVER raises.
    """
    if is_restraint_enabled():
        return "dim"
    return default


def real_stdout_isatty() -> bool:
    """Check the **unpatched** stdout's TTY status.

    ``prompt_toolkit.patch_stdout`` replaces ``sys.stdout`` with a
    non-TTY proxy during the REPL's lifetime. Code that checks
    ``sys.stdout.isatty()`` during REPL operation gets ``False``
    even on real interactive terminals — that's why the live status
    line (Gap #1+5) never surfaced during normal use.

    This helper checks ``sys.__stdout__`` (Python's saved reference
    to the original stdout, untouched by patch_stdout). Falls back
    to the patched ``sys.stdout`` only if ``__stdout__`` is ``None``
    (rare: Windows pythonw, daemonized processes). NEVER raises.
    """
    import sys
    primary = getattr(sys, "__stdout__", None)
    if primary is not None:
        try:
            return bool(primary.isatty())
        except Exception:  # noqa: BLE001
            pass
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def format_idle_breadcrumb(
    *,
    branch: str = "",
    cost_spent: float = 0.0,
    cost_budget: float = 0.0,
    posture: str = "",
    op_id: str = "",
) -> str:
    """One-line breadcrumb for the IDLE phase — replaces the silent
    empty status line that today leaves operators wondering whether
    O+V is alive.

    Returns a plain text string the caller wraps with their own
    Rich markup at the emission seam (matches ``status_line.py``'s
    library-agnostic policy — the substrate produces strings, the
    consumer applies styling).

    Format: ``IDLE · main · $0.04/$0.50 · EXPLORE`` — only includes
    fields the caller supplies. NEVER raises.
    """
    parts: List[str] = ["IDLE"]
    if isinstance(branch, str) and branch:
        parts.append(branch)
    if cost_budget > 0.0:
        parts.append(f"${cost_spent:.2f}/${cost_budget:.2f}")
    elif cost_spent > 0.0:
        parts.append(f"${cost_spent:.2f}")
    if isinstance(posture, str) and posture:
        parts.append(posture)
    if isinstance(op_id, str) and op_id:
        # Last-completed op tail (8 chars) so operators see "previous
        # work" hint when scrolling away from the receipt
        tail = op_id.split("-")[-1][:8] if "-" in op_id else op_id[:8]
        parts.append(f"prev:{tail}")
    return " · ".join(parts)
