"""
REPL Onboarding — UX Polish Slice 2
====================================

Closes §41.3 / §41.8 Phase 0 UX Polish Slice 2 (PRD v3.0+).
Bundle of operator-onboarding + discoverability features:

* **First-launch detection** — looks for a marker file at
  ``.jarvis/onboarded_marker``. Operator-side decides whether
  to show welcome banner.
* **Welcome banner** — structural composition of repo metadata
  (cwd, version, flag-count, etc.) — NO hardcoded content;
  template path is operator-tunable, falls back to a minimal
  structural banner.
* **Tutorial mode** — reads operator-authored
  ``.jarvis/tutorial.yaml`` (or env-override). Substrate
  ships ZERO default content — operator wires the tutorial
  source.
* **"Did you mean?" verb suggester** — composes
  :func:`flag_registry.levenshtein_distance` over the
  auto-discovered verb registry from
  :mod:`battle_test.repl_completion`.
* **Inline command examples** — composes
  :class:`battle_test.repl_completion.VerbDescriptor`'s
  doc-derived help to format error-with-example.

Composition contract:

* :func:`battle_test.repl_completion.discover_verbs` — verb
  inventory + their docstrings.
* :func:`governance.flag_registry.levenshtein_distance` — typo
  distance for "did you mean?".
* :func:`governance.cross_process_jsonl.flock_append_line` —
  §33.4 onboarding-state ledger at
  ``.jarvis/onboarding_state.jsonl``.

NEVER raises. Empty registry / missing tutorial yaml /
malformed verb input all degrade to ``DISABLED`` /
``NO_SUGGESTION`` results.

Closed 4-value :class:`OnboardingStage`:

  NEW_USER       no marker file exists
  IN_TUTORIAL    marker exists + tutorial state mid-stream
  GRADUATED      marker exists + tutorial complete OR skipped
  DISABLED       master flag off

Closed 4-value :class:`HintKind`:

  VERB_TYPO      input starts with ``/`` and resembles a known
                 verb within Levenshtein threshold
  MISSING_ARG    verb exists but the input is missing a
                 required argument (per VerbDescriptor)
  OUT_OF_SCOPE   no close verb match; suggest ``/help``
  NONE           no actionable suggestion

§33.1 ``JARVIS_REPL_ONBOARDING_ENABLED`` default-FALSE.

Authority asymmetry (AST-pinned): stdlib + lazy-imported
``repl_completion`` + ``flag_registry`` + ``cross_process_jsonl``.
Does NOT import orchestrator / iron_gate / policy / providers
/ candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
serpent_flow (substrate is consumed by REPL wiring, not
vice-versa).
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


REPL_ONBOARDING_SCHEMA_VERSION: str = "repl_onboarding.1"


_ENV_MASTER = "JARVIS_REPL_ONBOARDING_ENABLED"
_ENV_PERSIST = "JARVIS_REPL_ONBOARDING_PERSIST_ENABLED"
_ENV_AUTO_WELCOME = "JARVIS_REPL_ONBOARDING_AUTO_WELCOME"
_ENV_MARKER_PATH = "JARVIS_REPL_ONBOARDING_MARKER_PATH"
_ENV_TUTORIAL_YAML = "JARVIS_REPL_ONBOARDING_TUTORIAL_PATH"
_ENV_TYPO_MAX_DISTANCE = "JARVIS_REPL_ONBOARDING_TYPO_MAX_DISTANCE"
_ENV_BANNER_TEMPLATE = "JARVIS_REPL_ONBOARDING_BANNER_TEMPLATE"
_ENV_LEDGER_PATH = "JARVIS_REPL_ONBOARDING_LEDGER_PATH"

_DEFAULT_MARKER_REL = ".jarvis/onboarded_marker"
_DEFAULT_TUTORIAL_REL = ".jarvis/tutorial.yaml"
_DEFAULT_LEDGER_REL = ".jarvis/onboarding_state.jsonl"
_DEFAULT_TYPO_DISTANCE = 2

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def auto_welcome_enabled() -> bool:
    """Sub-flag — auto-emit welcome banner on first launch when
    master on. Default TRUE."""
    return _flag(_ENV_AUTO_WELCOME, default=True)


def marker_path() -> Path:
    raw = os.environ.get(_ENV_MARKER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_MARKER_REL)


def tutorial_yaml_path() -> Path:
    raw = os.environ.get(_ENV_TUTORIAL_YAML, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_TUTORIAL_REL)


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


def typo_max_distance() -> int:
    raw = os.environ.get(_ENV_TYPO_MAX_DISTANCE, "").strip()
    if not raw:
        return _DEFAULT_TYPO_DISTANCE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TYPO_DISTANCE
    return max(1, min(10, n))


def banner_template_path() -> Optional[Path]:
    """Operator-supplied banner template path. None when unset —
    substrate returns a minimal structural banner instead."""
    raw = os.environ.get(_ENV_BANNER_TEMPLATE, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except Exception:  # noqa: BLE001
        return None


# Closed taxonomies


class OnboardingStage(str, enum.Enum):
    """Closed 4-value stage — bytes-pinned via AST."""

    NEW_USER = "new_user"
    IN_TUTORIAL = "in_tutorial"
    GRADUATED = "graduated"
    DISABLED = "disabled"


class HintKind(str, enum.Enum):
    """Closed 4-value hint — bytes-pinned via AST."""

    VERB_TYPO = "verb_typo"
    MISSING_ARG = "missing_arg"
    OUT_OF_SCOPE = "out_of_scope"
    NONE = "none"


_STAGE_GLYPH: Dict[str, str] = {
    OnboardingStage.NEW_USER.value: "👋",
    OnboardingStage.IN_TUTORIAL.value: "📚",
    OnboardingStage.GRADUATED.value: "🎓",
    OnboardingStage.DISABLED.value: "◌",
}


_HINT_GLYPH: Dict[str, str] = {
    HintKind.VERB_TYPO.value: "🔤",
    HintKind.MISSING_ARG.value: "❓",
    HintKind.OUT_OF_SCOPE.value: "💭",
    HintKind.NONE.value: "·",
}


def stage_glyph(stage: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(stage, "value"):
            return _STAGE_GLYPH.get(str(stage.value), "?")
        return _STAGE_GLYPH.get(
            str(stage or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def hint_glyph(hint: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(hint, "value"):
            return _HINT_GLYPH.get(str(hint.value), "?")
        return _HINT_GLYPH.get(
            str(hint or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class WelcomeBanner:
    """Operator-facing welcome banner. Structural — no
    hardcoded content beyond the format shape."""

    title: str
    body_lines: Tuple[str, ...]
    is_first_launch: bool
    cwd: str
    flag_count: int
    verb_count: int
    rendered_at_unix: float
    schema_version: str = REPL_ONBOARDING_SCHEMA_VERSION

    def render(self) -> str:
        """Operator-facing single-string render."""
        lines = [self.title]
        lines.extend(self.body_lines)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title[:256],
            "body_lines": list(self.body_lines),
            "is_first_launch": bool(self.is_first_launch),
            "cwd": self.cwd[:256],
            "flag_count": int(self.flag_count),
            "verb_count": int(self.verb_count),
            "rendered_at_unix": float(self.rendered_at_unix),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class TutorialStep:
    """One operator-authored tutorial step."""

    step_id: str
    prompt_text: str
    action_required: str
    completion_marker: str  # substring in output that marks done
    schema_version: str = REPL_ONBOARDING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id[:64],
            "prompt_text": self.prompt_text[:1024],
            "action_required": self.action_required[:256],
            "completion_marker": self.completion_marker[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ErrorSuggestion:
    """Inline error hint with command example."""

    input_text: str
    hint_kind: HintKind
    suggestions: Tuple[str, ...]
    example_command: str
    diagnostic: str
    schema_version: str = REPL_ONBOARDING_SCHEMA_VERSION

    def render(self) -> str:
        """Operator-facing single-string render."""
        if self.hint_kind is HintKind.NONE:
            return self.diagnostic
        glyph = hint_glyph(self.hint_kind)
        lines = [f"{glyph} {self.diagnostic}"]
        if self.suggestions:
            top = self.suggestions[0]
            lines.append(f"  Did you mean: {top}?")
            if len(self.suggestions) > 1:
                others = ", ".join(self.suggestions[1:3])
                lines.append(f"  Other candidates: {others}")
        if self.example_command:
            lines.append(f"  Example: {self.example_command}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_text": self.input_text[:256],
            "hint_kind": self.hint_kind.value,
            "suggestions": list(self.suggestions),
            "example_command": self.example_command[:256],
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class OnboardingState:
    """Persisted onboarding state."""

    stage: OnboardingStage
    current_step_id: str
    completed_step_ids: Tuple[str, ...]
    started_at_unix: float
    schema_version: str = REPL_ONBOARDING_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "onboarding_state",
            "stage": self.stage.value,
            "current_step_id": self.current_step_id[:64],
            "completed_step_ids": list(self.completed_step_ids),
            "started_at_unix": float(self.started_at_unix),
            "schema_version": self.schema_version,
        }


# Composers (all lazy-imported)


def _flock_append(payload: Mapping[str, Any]) -> bool:
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _levenshtein(a: str, b: str) -> int:
    """Compose flag_registry.levenshtein_distance — single
    canonical implementation. NEVER raises (returns max-int on
    failure to fall outside any threshold)."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            levenshtein_distance,
        )
        return int(levenshtein_distance(a, b))
    except Exception:  # noqa: BLE001
        return 999999


def _discover_verbs(repl_instance: Any) -> Tuple[str, ...]:
    """Compose repl_completion.discover_verbs. NEVER raises."""
    if repl_instance is None:
        return ()
    try:
        from backend.core.ouroboros.battle_test.repl_completion import (  # noqa: E501
            discover_verbs,
        )
        registry = discover_verbs(repl_instance)
        verbs: List[str] = []
        for d in getattr(registry, "verbs", ()) or ():
            try:
                slash = getattr(d, "slash_form", "") or ""
                if slash:
                    verbs.append(slash)
            except Exception:  # noqa: BLE001
                continue
        return tuple(verbs)
    except Exception:  # noqa: BLE001
        return ()


def _verb_descriptor(
    repl_instance: Any, slash_name: str,
) -> Optional[Any]:
    """Look up one descriptor. NEVER raises."""
    if repl_instance is None or not slash_name:
        return None
    try:
        from backend.core.ouroboros.battle_test.repl_completion import (  # noqa: E501
            discover_verbs,
        )
        registry = discover_verbs(repl_instance)
        for d in getattr(registry, "verbs", ()) or ():
            if getattr(d, "slash_form", "") == slash_name:
                return d
        # Builtins live under VerbRegistry.builtin_verbs / similar
        return None
    except Exception:  # noqa: BLE001
        return None


def _count_flags() -> int:
    """Count registered flags. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            get_default_registry,
        )
        return len(get_default_registry().list_all())
    except Exception:  # noqa: BLE001
        return 0


# First-launch detection


def is_first_launch() -> bool:
    """True iff the marker file is absent. NEVER raises."""
    if not master_enabled():
        return False
    try:
        return not marker_path().exists()
    except Exception:  # noqa: BLE001
        return False


def mark_onboarded(*, op_id: str = "") -> bool:
    """Write the marker file. Idempotent. NEVER raises.

    Returns True on successful write, False on failure or when
    master is off."""
    if not master_enabled():
        return False
    try:
        p = marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(
                json.dumps({
                    "marker_version": REPL_ONBOARDING_SCHEMA_VERSION,
                    "marked_at_unix": time.time(),
                    "op_id": str(op_id or "")[:128],
                }),
                encoding="utf-8",
            )
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_onboarded() -> bool:
    """Remove the marker file. Operator-side reset. NEVER raises."""
    try:
        p = marker_path()
        if p.exists():
            p.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


# Welcome banner


def build_welcome_banner(
    *,
    repl_instance: Any = None,
    now_unix: Optional[float] = None,
) -> WelcomeBanner:
    """Build the operator-facing welcome banner. NEVER raises.

    When ``JARVIS_REPL_ONBOARDING_BANNER_TEMPLATE`` is set to a
    readable file path, that file's contents (truncated to 4KB)
    become the banner body. Otherwise we emit a minimal
    structural banner derived from repo metadata.
    """
    rendered = time.time() if now_unix is None else float(now_unix)
    is_first = is_first_launch()
    try:
        cwd = str(Path.cwd())
    except Exception:  # noqa: BLE001
        cwd = ""
    flag_count = _count_flags()
    verbs = _discover_verbs(repl_instance) if repl_instance else ()
    verb_count = len(verbs)

    title = (
        "👋 Welcome to Ouroboros + Venom (first launch)"
        if is_first
        else "Ouroboros + Venom"
    )

    body_lines: List[str] = []
    template_path = banner_template_path()
    if template_path is not None:
        try:
            if template_path.exists() and template_path.is_file():
                content = template_path.read_text(encoding="utf-8")
                if content:
                    body_lines.extend(content[:4096].splitlines())
        except Exception:  # noqa: BLE001
            pass

    if not body_lines:
        # Minimal structural banner — composes repo metadata.
        body_lines = [
            f"  cwd       : {cwd[:80]}",
            f"  flags     : {flag_count} registered",
            f"  verbs     : {verb_count} discovered",
        ]
        if is_first:
            body_lines.append(
                "  hint      : run /help for verb list "
                "or /tutorial for guided walkthrough"
            )

    return WelcomeBanner(
        title=title,
        body_lines=tuple(body_lines),
        is_first_launch=is_first,
        cwd=cwd,
        flag_count=flag_count,
        verb_count=verb_count,
        rendered_at_unix=rendered,
    )


# Tutorial mode


def load_tutorial_steps(
    *, path_override: Optional[Path] = None,
) -> Tuple[TutorialStep, ...]:
    """Load operator-authored tutorial steps from yaml. NEVER
    raises. Returns empty tuple when yaml absent / unparseable /
    PyYAML unavailable — substrate ships ZERO default content.
    Format expected (when yaml available):
      steps:
        - id: hello
          prompt: "Try /help to see available commands"
          action: "/help"
          marker: "Available verbs"
    Falls back to JSON when ``.json`` extension is used (stdlib).
    """
    target = path_override or tutorial_yaml_path()
    try:
        if not target.exists() or not target.is_file():
            return ()
        raw = target.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ()
    parsed: Any = None
    suffix = target.suffix.lower()
    if suffix in (".json", ".jsonl"):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
    else:
        # Try yaml; fall back to JSON parse so substrate works
        # without PyYAML dependency.
        try:
            import yaml as _yaml  # type: ignore
            parsed = _yaml.safe_load(raw)
        except ImportError:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
        except Exception:  # noqa: BLE001
            parsed = None
    if not isinstance(parsed, dict):
        return ()
    steps_raw = parsed.get("steps")
    if not isinstance(steps_raw, list):
        return ()
    out: List[TutorialStep] = []
    for entry in steps_raw:
        if not isinstance(entry, dict):
            continue
        try:
            step = TutorialStep(
                step_id=str(entry.get("id", "")).strip(),
                prompt_text=str(entry.get("prompt", "")).strip(),
                action_required=str(entry.get("action", "")).strip(),
                completion_marker=str(entry.get("marker", "")).strip(),
            )
        except Exception:  # noqa: BLE001
            continue
        if step.step_id and step.prompt_text:
            out.append(step)
    return tuple(out)


def current_tutorial_step(
    *,
    completed_ids: Sequence[str] = (),
    steps: Optional[Sequence[TutorialStep]] = None,
) -> Optional[TutorialStep]:
    """Return the first not-completed step. NEVER raises."""
    if not master_enabled():
        return None
    s = steps if steps is not None else load_tutorial_steps()
    if not s:
        return None
    done = set(completed_ids or ())
    for step in s:
        if step.step_id not in done:
            return step
    return None


def advance_tutorial(
    state: OnboardingState,
    *,
    steps: Optional[Sequence[TutorialStep]] = None,
    now_unix: Optional[float] = None,
) -> OnboardingState:
    """Mark current step complete + return updated state. NEVER
    raises. Persists to §33.4 ledger when persistence enabled."""
    if not master_enabled():
        return state
    completed = tuple(state.completed_step_ids)
    if state.current_step_id and state.current_step_id not in completed:
        completed = completed + (state.current_step_id,)
    next_step = current_tutorial_step(
        completed_ids=completed, steps=steps,
    )
    new_stage = (
        OnboardingStage.IN_TUTORIAL
        if next_step is not None
        else OnboardingStage.GRADUATED
    )
    new_state = OnboardingState(
        stage=new_stage,
        current_step_id=(
            next_step.step_id if next_step else ""
        ),
        completed_step_ids=completed,
        started_at_unix=state.started_at_unix,
    )
    _flock_append(new_state.to_dict())
    return new_state


def start_tutorial(
    *,
    steps: Optional[Sequence[TutorialStep]] = None,
    now_unix: Optional[float] = None,
) -> OnboardingState:
    """Initialize state with first step (or GRADUATED if no
    tutorial). NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return OnboardingState(
            stage=OnboardingStage.DISABLED,
            current_step_id="",
            completed_step_ids=(),
            started_at_unix=started,
        )
    first = current_tutorial_step(steps=steps)
    if first is None:
        # No tutorial available → graduate immediately.
        return OnboardingState(
            stage=OnboardingStage.GRADUATED,
            current_step_id="",
            completed_step_ids=(),
            started_at_unix=started,
        )
    state = OnboardingState(
        stage=OnboardingStage.IN_TUTORIAL,
        current_step_id=first.step_id,
        completed_step_ids=(),
        started_at_unix=started,
    )
    _flock_append(state.to_dict())
    return state


# Did-you-mean / typo suggester


def suggest_for_typo(
    input_text: str,
    *,
    repl_instance: Any = None,
    verbs_override: Optional[Sequence[str]] = None,
    max_distance_override: Optional[int] = None,
) -> ErrorSuggestion:
    """Compose Levenshtein over verb registry. NEVER raises.

    Parameters
    ----------
    input_text:
        Operator's input line. Expected to start with ``/``;
        plain text returns OUT_OF_SCOPE.
    repl_instance:
        Caller-injected SerpentREPL (or compatible) instance
        from which verbs are auto-discovered. None disables
        VERB_TYPO classification.
    verbs_override:
        Testing seam — pass a verb list directly.
    """
    text = str(input_text or "").strip()
    if not master_enabled():
        return ErrorSuggestion(
            input_text=text,
            hint_kind=HintKind.NONE,
            suggestions=(),
            example_command="",
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
        )
    if not text:
        return ErrorSuggestion(
            input_text="",
            hint_kind=HintKind.NONE,
            suggestions=(),
            example_command="",
            diagnostic="empty input",
        )
    if not text.startswith("/"):
        return ErrorSuggestion(
            input_text=text,
            hint_kind=HintKind.OUT_OF_SCOPE,
            suggestions=("/help",),
            example_command="/help",
            diagnostic=(
                "not a slash command — type /help to list "
                "available verbs"
            ),
        )
    # Extract verb head (everything up to first space).
    parts = text.split(None, 1)
    head = parts[0]
    args_present = len(parts) > 1 and bool(parts[1].strip())

    verbs = (
        tuple(verbs_override)
        if verbs_override is not None
        else _discover_verbs(repl_instance)
    )
    if not verbs:
        return ErrorSuggestion(
            input_text=text,
            hint_kind=HintKind.NONE,
            suggestions=(),
            example_command="",
            diagnostic="no verb registry available",
        )
    # Exact match → MISSING_ARG if args missing AND descriptor expects them.
    if head in verbs:
        # Check VerbDescriptor for example / args; if missing args, hint.
        desc = _verb_descriptor(repl_instance, head) if repl_instance else None
        example = ""
        if desc is not None:
            example = str(getattr(desc, "example", "") or "")
        if not args_present and example and " " in example:
            return ErrorSuggestion(
                input_text=text,
                hint_kind=HintKind.MISSING_ARG,
                suggestions=(head,),
                example_command=example,
                diagnostic=f"verb {head} expects arguments",
            )
        return ErrorSuggestion(
            input_text=text,
            hint_kind=HintKind.NONE,
            suggestions=(head,),
            example_command=example,
            diagnostic="verb recognized",
        )
    # Typo path — Levenshtein-rank verbs.
    threshold = (
        max_distance_override
        if max_distance_override is not None
        else typo_max_distance()
    )
    ranked: List[Tuple[str, int]] = []
    for v in verbs:
        d = _levenshtein(head.upper(), v.upper())
        if d <= threshold:
            ranked.append((v, d))
    ranked.sort(key=lambda kv: (kv[1], kv[0]))
    if not ranked:
        return ErrorSuggestion(
            input_text=text,
            hint_kind=HintKind.OUT_OF_SCOPE,
            suggestions=("/help",),
            example_command="/help",
            diagnostic=(
                f"no close match for {head}; type /help to "
                "list all verbs"
            ),
        )
    top_suggestions = tuple(v for v, _ in ranked[:3])
    # Surface descriptor.example for the top suggestion.
    desc = (
        _verb_descriptor(repl_instance, ranked[0][0])
        if repl_instance else None
    )
    example = ""
    if desc is not None:
        example = str(getattr(desc, "example", "") or "")
    return ErrorSuggestion(
        input_text=text,
        hint_kind=HintKind.VERB_TYPO,
        suggestions=top_suggestions,
        example_command=example or top_suggestions[0],
        diagnostic=f"unknown verb {head!r}",
    )


# Renderer


def format_onboarding_panel(
    *,
    state: Optional[OnboardingState] = None,
    banner: Optional[WelcomeBanner] = None,
    suggestion: Optional[ErrorSuggestion] = None,
) -> str:
    """Operator-facing panel — renders whichever artifact is
    supplied. NEVER raises."""
    if not master_enabled():
        return f"repl onboarding: disabled ({_ENV_MASTER}=false)"
    parts: List[str] = []
    if banner is not None:
        parts.append(banner.render())
    if state is not None:
        sg = stage_glyph(state.stage)
        parts.append(
            f"{sg} stage={state.stage.value} "
            f"current={state.current_step_id or '(none)'} "
            f"completed={len(state.completed_step_ids)}"
        )
    if suggestion is not None:
        parts.append(suggestion.render())
    if not parts:
        return "repl onboarding: nothing to render"
    return "\n\n".join(parts)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/repl_onboarding.py"
    )

    _EXPECTED_STAGES = {
        "new_user", "in_tutorial", "graduated", "disabled",
    }
    _EXPECTED_HINTS = {
        "verb_typo", "missing_arg", "out_of_scope", "none",
    }

    def _validate_stage_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "OnboardingStage"
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
                missing = _EXPECTED_STAGES - found
                extra = found - _EXPECTED_STAGES
                if missing:
                    return (
                        f"OnboardingStage missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"OnboardingStage drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("OnboardingStage class not found",)

    def _validate_hint_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "HintKind"
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
                missing = _EXPECTED_HINTS - found
                extra = found - _EXPECTED_HINTS
                if missing:
                    return (
                        f"HintKind missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"HintKind drift: {sorted(extra)}",
                    )
                return ()
        return ("HintKind class not found",)

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
            "backend.core.ouroboros.battle_test.serpent_flow",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
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
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "repl_completion" not in source:
            violations.append(
                "must compose battle_test.repl_completion "
                "(verb registry source)",
            )
        if "levenshtein_distance" not in source:
            violations.append(
                "must compose flag_registry.levenshtein_distance "
                "(no parallel typo distance)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl "
                "(§33.4 ledger)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="repl_onboarding_stage_taxonomy_closed",
            target_file=target,
            description=(
                "OnboardingStage 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_stage_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="repl_onboarding_hint_taxonomy_closed",
            target_file=target,
            description=(
                "HintKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_hint_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="repl_onboarding_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate purity — pure REPL onboarding "
                "composer. MUST NOT import orchestrator / "
                "iron_gate / etc / serpent_flow (substrate "
                "is consumed by REPL wiring, not vice-versa)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="repl_onboarding_master_default_false",
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="repl_onboarding_composes_canonical",
            target_file=target,
            description=(
                "Composes battle_test.repl_completion + "
                "governance.flag_registry.levenshtein_distance "
                "+ cross_process_jsonl. No parallel verb "
                "registry, no parallel Levenshtein, no "
                "parallel JSONL."
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

    src = (
        "backend/core/ouroboros/governance/repl_onboarding.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "REPL onboarding master. §33.1 default-FALSE. "
                "Closes §41 Phase 0 UX Polish Slice 2 (PRD "
                "v3.0+). When on: first-launch detection + "
                "welcome banner + tutorial mode + did-you-mean "
                "verb suggester + inline command examples."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — gate §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_AUTO_WELCOME,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — auto-emit welcome banner on first "
                "launch (when master on). Default TRUE."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_AUTO_WELCOME}=false",
        ),
        FlagSpec(
            name=_ENV_MARKER_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-tunable marker file path. Default "
                ".jarvis/onboarded_marker."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_MARKER_PATH}=/custom/path/marker"
            ),
        ),
        FlagSpec(
            name=_ENV_TUTORIAL_YAML,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-tunable tutorial yaml path. Default "
                ".jarvis/tutorial.yaml. Substrate ships ZERO "
                "default content — operator authors the steps."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_TUTORIAL_YAML}=/repo/docs/tutorial.yaml"
            ),
        ),
        FlagSpec(
            name=_ENV_TYPO_MAX_DISTANCE,
            type=FlagType.INT,
            default=_DEFAULT_TYPO_DISTANCE,
            description=(
                "Levenshtein distance threshold for verb typo "
                "suggester. Default 2. Clamped to [1, 10]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_TYPO_MAX_DISTANCE}=3",
        ),
        FlagSpec(
            name=_ENV_BANNER_TEMPLATE,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-tunable banner template path. "
                "Default unset — substrate emits minimal "
                "structural banner."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_BANNER_TEMPLATE}=/repo/banner.txt"
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
    "REPL_ONBOARDING_SCHEMA_VERSION",
    "OnboardingStage",
    "HintKind",
    "WelcomeBanner",
    "TutorialStep",
    "ErrorSuggestion",
    "OnboardingState",
    "master_enabled",
    "persistence_enabled",
    "auto_welcome_enabled",
    "marker_path",
    "tutorial_yaml_path",
    "ledger_path",
    "typo_max_distance",
    "banner_template_path",
    "stage_glyph",
    "hint_glyph",
    "is_first_launch",
    "mark_onboarded",
    "reset_onboarded",
    "build_welcome_banner",
    "load_tutorial_steps",
    "current_tutorial_step",
    "advance_tutorial",
    "start_tutorial",
    "suggest_for_typo",
    "format_onboarding_panel",
    "register_shipped_invariants",
    "register_flags",
]
