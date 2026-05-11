"""
REPL Smart Completion — UX Polish Slice 3
==========================================

Closes §41.3 / §41.8 Phase 0 UX Polish Slice 3 (PRD v3.0+).
Bundle of operator-completion + output-polish features:

* **Per-verb `--help` formatter** — composes
  :class:`battle_test.repl_completion.VerbDescriptor` to emit
  structured per-verb help (description + handler method +
  derived example). Returns a frozen :class:`VerbHelp` artifact.
* **Fuzzy slash palette ranking** — given a partial slash input,
  returns top-N verbs ranked by prefix match (highest priority)
  then Levenshtein distance (composes flag_registry helper).
  Each result carries the verb's description for inline display.
* **Pretty-printed JSON tool outputs** — pure-function detector
  + formatter. Recognizes JSON-shaped tool output (object /
  array at root), pretty-prints with stable key ordering +
  configurable indent. Bounded by output length cap.
* **Fast-path Q&A detection** — pure-function heuristic that
  classifies an input as `FAST_PATH` (no slash, no @mention,
  short, no code-fence) — callers can route these to a
  simpler/cheaper provider chain.

Composition contract:

* :func:`battle_test.repl_completion.discover_verbs` — verb
  registry source (auto-discovered).
* :func:`governance.flag_registry.levenshtein_distance` —
  canonical typo distance (single implementation).
* Stdlib ``json`` — JSON detection + pretty-print.

NEVER raises. Empty registry / malformed input / unparseable
JSON all degrade to safe defaults.

Closed 4-value :class:`CompletionKind`:

  VERB           prefix-match against /verb names
  MENTION        @-mention completion (deferred to follow-up)
  ARGUMENT       in-progress (operator's verb is recognized,
                 cursor is mid-argument)
  NONE           no completion available

Closed 4-value :class:`OutputFormat`:

  PLAIN          input not JSON-shaped — return as-is
  PRETTY_JSON    valid JSON detected + pretty-printed
  TRUNCATED      pretty-print result exceeded length cap;
                 returned with ellipsis marker
  DISABLED       master flag off

§33.1 ``JARVIS_REPL_SMART_COMPLETION_ENABLED`` default-FALSE.

Authority asymmetry (AST-pinned): stdlib + lazy-imported
``repl_completion`` + ``flag_registry``. Does NOT import
orchestrator / iron_gate / policy / providers / etc /
serpent_flow.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
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


REPL_SMART_COMPLETION_SCHEMA_VERSION: str = "repl_smart_completion.1"


_ENV_MASTER = "JARVIS_REPL_SMART_COMPLETION_ENABLED"
_ENV_PRETTY_JSON_ENABLED = (
    "JARVIS_REPL_SMART_COMPLETION_PRETTY_JSON_ENABLED"
)
_ENV_FAST_PATH_ENABLED = (
    "JARVIS_REPL_SMART_COMPLETION_FAST_PATH_ENABLED"
)
_ENV_FAST_PATH_MAX_LEN = (
    "JARVIS_REPL_SMART_COMPLETION_FAST_PATH_MAX_LEN"
)
_ENV_PALETTE_MAX_RESULTS = (
    "JARVIS_REPL_SMART_COMPLETION_PALETTE_MAX_RESULTS"
)
_ENV_PALETTE_DISTANCE = (
    "JARVIS_REPL_SMART_COMPLETION_PALETTE_DISTANCE"
)
_ENV_JSON_INDENT = (
    "JARVIS_REPL_SMART_COMPLETION_JSON_INDENT"
)
_ENV_OUTPUT_BOUND = (
    "JARVIS_REPL_SMART_COMPLETION_OUTPUT_BOUND"
)

_DEFAULT_FAST_PATH_MAX_LEN = 200
_DEFAULT_PALETTE_MAX_RESULTS = 8
_DEFAULT_PALETTE_DISTANCE = 3
_DEFAULT_JSON_INDENT = 2
_DEFAULT_OUTPUT_BOUND = 8192

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def pretty_json_enabled() -> bool:
    return _flag(_ENV_PRETTY_JSON_ENABLED, default=True)


def fast_path_enabled() -> bool:
    return _flag(_ENV_FAST_PATH_ENABLED, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def fast_path_max_len() -> int:
    return _read_clamped_int(
        _ENV_FAST_PATH_MAX_LEN, _DEFAULT_FAST_PATH_MAX_LEN,
        1, 10_000,
    )


def palette_max_results() -> int:
    return _read_clamped_int(
        _ENV_PALETTE_MAX_RESULTS, _DEFAULT_PALETTE_MAX_RESULTS,
        1, 100,
    )


def palette_distance_threshold() -> int:
    return _read_clamped_int(
        _ENV_PALETTE_DISTANCE, _DEFAULT_PALETTE_DISTANCE, 1, 20,
    )


def json_indent() -> int:
    return _read_clamped_int(
        _ENV_JSON_INDENT, _DEFAULT_JSON_INDENT, 0, 8,
    )


def output_bound() -> int:
    return _read_clamped_int(
        _ENV_OUTPUT_BOUND, _DEFAULT_OUTPUT_BOUND, 256, 1_000_000,
    )


# Closed taxonomies


class CompletionKind(str, enum.Enum):
    """Closed 4-value completion kind — bytes-pinned via AST."""

    VERB = "verb"
    MENTION = "mention"
    ARGUMENT = "argument"
    NONE = "none"


class OutputFormat(str, enum.Enum):
    """Closed 4-value output format — bytes-pinned via AST."""

    PLAIN = "plain"
    PRETTY_JSON = "pretty_json"
    TRUNCATED = "truncated"
    DISABLED = "disabled"


_KIND_GLYPH: Dict[str, str] = {
    CompletionKind.VERB.value: "/",
    CompletionKind.MENTION.value: "@",
    CompletionKind.ARGUMENT.value: "›",
    CompletionKind.NONE.value: "·",
}


_FORMAT_GLYPH: Dict[str, str] = {
    OutputFormat.PLAIN.value: "·",
    OutputFormat.PRETTY_JSON.value: "{",
    OutputFormat.TRUNCATED.value: "✂",
    OutputFormat.DISABLED.value: "◌",
}


def kind_glyph(kind: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(kind, "value"):
            return _KIND_GLYPH.get(str(kind.value), "?")
        return _KIND_GLYPH.get(
            str(kind or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def format_glyph(fmt: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(fmt, "value"):
            return _FORMAT_GLYPH.get(str(fmt.value), "?")
        return _FORMAT_GLYPH.get(
            str(fmt or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class VerbHelp:
    """Per-verb help artifact."""

    slash_form: str
    description: str
    handler_method: str
    example_command: str
    schema_version: str = REPL_SMART_COMPLETION_SCHEMA_VERSION

    def render(self) -> str:
        """Operator-facing render. NEVER raises."""
        lines = [f"Usage:  {self.slash_form}"]
        if self.description:
            lines.append(f"  {self.description}")
        if self.example_command and self.example_command != self.slash_form:
            lines.append(f"  example: {self.example_command}")
        if self.handler_method:
            lines.append(f"  handler: {self.handler_method}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slash_form": self.slash_form[:64],
            "description": self.description[:512],
            "handler_method": self.handler_method[:128],
            "example_command": self.example_command[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CompletionMatch:
    """One palette completion match."""

    slash_form: str
    description: str
    score: int  # 0 = exact prefix, > 0 = Levenshtein distance
    kind: CompletionKind
    schema_version: str = REPL_SMART_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slash_form": self.slash_form[:64],
            "description": self.description[:256],
            "score": int(self.score),
            "kind": self.kind.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PaletteResult:
    """Slash palette ranked results."""

    input_text: str
    matches: Tuple[CompletionMatch, ...]
    diagnostic: str
    elapsed_ms: float
    schema_version: str = REPL_SMART_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_text": self.input_text[:256],
            "matches": [m.to_dict() for m in self.matches],
            "diagnostic": self.diagnostic[:512],
            "elapsed_ms": float(self.elapsed_ms),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class FormattedOutput:
    """JSON-pretty-print result."""

    format: OutputFormat
    body: str
    original_bytes: int
    formatted_bytes: int
    diagnostic: str
    schema_version: str = REPL_SMART_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format.value,
            "body_preview": self.body[:512],
            "original_bytes": int(self.original_bytes),
            "formatted_bytes": int(self.formatted_bytes),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class FastPathClassification:
    """Fast-path Q&A heuristic result."""

    is_fast_path: bool
    input_length: int
    has_slash: bool
    has_mention: bool
    has_code_fence: bool
    diagnostic: str
    schema_version: str = REPL_SMART_COMPLETION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_fast_path": bool(self.is_fast_path),
            "input_length": int(self.input_length),
            "has_slash": bool(self.has_slash),
            "has_mention": bool(self.has_mention),
            "has_code_fence": bool(self.has_code_fence),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


# Composers


def _discover_verb_descriptors(
    repl_instance: Any,
) -> Tuple[Any, ...]:
    """Compose repl_completion.discover_verbs. NEVER raises."""
    if repl_instance is None:
        return ()
    try:
        from backend.core.ouroboros.battle_test.repl_completion import (  # noqa: E501
            discover_verbs,
        )
        registry = discover_verbs(repl_instance)
        return tuple(getattr(registry, "verbs", ()) or ())
    except Exception:  # noqa: BLE001
        return ()


def _levenshtein(a: str, b: str) -> int:
    """Compose canonical Levenshtein. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            levenshtein_distance,
        )
        return int(levenshtein_distance(a, b))
    except Exception:  # noqa: BLE001
        return 999999


def _extract_example_from_description(
    description: str, slash_form: str,
) -> str:
    """Pure parser — looks for ``example: ...`` substring in
    the description (case-insensitive). Falls back to
    slash_form. NEVER raises."""
    if not description:
        return slash_form
    try:
        m = re.search(
            r"example\s*:\s*(.+)$",
            description,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()[:256]
    except Exception:  # noqa: BLE001
        pass
    return slash_form


# Public API


def build_verb_help(
    slash_form: str,
    *,
    repl_instance: Any = None,
    descriptors_override: Optional[Sequence[Any]] = None,
) -> Optional[VerbHelp]:
    """Per-verb help. NEVER raises. Returns None when verb not
    found OR master is off."""
    if not master_enabled():
        return None
    target = str(slash_form or "").strip()
    if not target:
        return None
    if not target.startswith("/"):
        target = "/" + target
    descs = (
        tuple(descriptors_override)
        if descriptors_override is not None
        else _discover_verb_descriptors(repl_instance)
    )
    for d in descs:
        try:
            if getattr(d, "slash_form", "") == target:
                description = str(
                    getattr(d, "description", "") or "",
                )
                return VerbHelp(
                    slash_form=target,
                    description=description,
                    handler_method=str(
                        getattr(d, "handler_method", "") or "",
                    ),
                    example_command=(
                        _extract_example_from_description(
                            description, target,
                        )
                    ),
                )
        except Exception:  # noqa: BLE001
            continue
    return None


def rank_palette(
    input_text: str,
    *,
    repl_instance: Any = None,
    descriptors_override: Optional[Sequence[Any]] = None,
    max_results_override: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> PaletteResult:
    """Fuzzy slash palette. NEVER raises.

    Ranking:
      1. Exact prefix matches (score 0), alphabetical
      2. Levenshtein matches within threshold (score = distance)

    When ``input_text`` doesn't start with ``/``, returns empty
    matches with a diagnostic. Empty input returns ALL verbs
    truncated to max_results (helps initial palette display)."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return PaletteResult(
            input_text=str(input_text or ""),
            matches=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_ms=0.0,
        )
    text = str(input_text or "").strip()
    descs = (
        tuple(descriptors_override)
        if descriptors_override is not None
        else _discover_verb_descriptors(repl_instance)
    )
    cap = (
        max_results_override
        if max_results_override is not None
        else palette_max_results()
    )

    if not descs:
        return PaletteResult(
            input_text=text,
            matches=(),
            diagnostic="no verb registry available",
            elapsed_ms=(time.time() - started) * 1000.0,
        )

    if not text:
        # Empty input — return all verbs alphabetically as
        # "browse mode" palette.
        verbs_sorted = sorted(
            descs,
            key=lambda d: str(getattr(d, "slash_form", "")),
        )
        matches: List[CompletionMatch] = []
        for d in verbs_sorted[:cap]:
            matches.append(CompletionMatch(
                slash_form=str(getattr(d, "slash_form", "") or ""),
                description=str(getattr(d, "description", "") or ""),
                score=0,
                kind=CompletionKind.VERB,
            ))
        return PaletteResult(
            input_text="",
            matches=tuple(matches),
            diagnostic=f"browse mode: {len(matches)} verb(s)",
            elapsed_ms=(time.time() - started) * 1000.0,
        )

    if not text.startswith("/"):
        return PaletteResult(
            input_text=text,
            matches=(),
            diagnostic="non-slash input — no verb completion",
            elapsed_ms=(time.time() - started) * 1000.0,
        )

    target = text.lower()
    threshold = palette_distance_threshold()
    prefix_matches: List[CompletionMatch] = []
    fuzzy_matches: List[CompletionMatch] = []
    for d in descs:
        try:
            slash = str(getattr(d, "slash_form", "") or "")
            desc = str(getattr(d, "description", "") or "")
            if not slash:
                continue
            sl = slash.lower()
            if sl.startswith(target):
                prefix_matches.append(CompletionMatch(
                    slash_form=slash,
                    description=desc,
                    score=0,
                    kind=CompletionKind.VERB,
                ))
                continue
            d_score = _levenshtein(target, sl)
            if d_score <= threshold:
                fuzzy_matches.append(CompletionMatch(
                    slash_form=slash,
                    description=desc,
                    score=d_score,
                    kind=CompletionKind.VERB,
                ))
        except Exception:  # noqa: BLE001
            continue

    prefix_matches.sort(key=lambda m: m.slash_form)
    fuzzy_matches.sort(key=lambda m: (m.score, m.slash_form))
    all_matches = prefix_matches + fuzzy_matches
    final = tuple(all_matches[:cap])

    return PaletteResult(
        input_text=text,
        matches=final,
        diagnostic=(
            f"{len(prefix_matches)} prefix + "
            f"{len(fuzzy_matches)} fuzzy match(es); "
            f"returned {len(final)} (cap={cap})"
        ),
        elapsed_ms=(time.time() - started) * 1000.0,
    )


# JSON pretty-print


_JSON_LIKELY_START: FrozenSet[str] = frozenset({"{", "["})


def is_json_shaped(text: str) -> bool:
    """Heuristic — first non-whitespace char is { or [.
    NEVER raises."""
    try:
        s = str(text or "").lstrip()
        return bool(s) and s[0] in _JSON_LIKELY_START
    except Exception:  # noqa: BLE001
        return False


def pretty_print_json(
    text: str,
    *,
    indent_override: Optional[int] = None,
    bound_override: Optional[int] = None,
) -> FormattedOutput:
    """Pure-function JSON detector + pretty-printer. NEVER
    raises.

    When the input is not JSON-shaped OR fails to parse,
    returns PLAIN with the original body unchanged. When it
    parses, returns PRETTY_JSON with indent=N, sort_keys=True.
    If pretty result exceeds the output bound, returns
    TRUNCATED with an ellipsis marker."""
    body = str(text or "")
    if not master_enabled() or not pretty_json_enabled():
        return FormattedOutput(
            format=(
                OutputFormat.DISABLED
                if not master_enabled()
                else OutputFormat.PLAIN
            ),
            body=body,
            original_bytes=len(body),
            formatted_bytes=len(body),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
                if not master_enabled()
                else "pretty_json disabled by sub-flag"
            ),
        )
    if not is_json_shaped(body):
        return FormattedOutput(
            format=OutputFormat.PLAIN,
            body=body,
            original_bytes=len(body),
            formatted_bytes=len(body),
            diagnostic="not JSON-shaped — unchanged",
        )
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError) as exc:
        return FormattedOutput(
            format=OutputFormat.PLAIN,
            body=body,
            original_bytes=len(body),
            formatted_bytes=len(body),
            diagnostic=f"JSON parse failed: {exc!r}"[:200],
        )
    indent = (
        indent_override
        if indent_override is not None
        else json_indent()
    )
    bound = (
        bound_override
        if bound_override is not None
        else output_bound()
    )
    try:
        pretty = json.dumps(
            parsed, indent=indent, sort_keys=True,
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        return FormattedOutput(
            format=OutputFormat.PLAIN,
            body=body,
            original_bytes=len(body),
            formatted_bytes=len(body),
            diagnostic=f"JSON dump failed: {exc!r}"[:200],
        )
    if len(pretty) > bound:
        truncated = pretty[:bound] + "\n... [truncated]"
        return FormattedOutput(
            format=OutputFormat.TRUNCATED,
            body=truncated,
            original_bytes=len(body),
            formatted_bytes=len(truncated),
            diagnostic=(
                f"pretty result exceeded bound ({bound}); "
                "truncated with ellipsis"
            ),
        )
    return FormattedOutput(
        format=OutputFormat.PRETTY_JSON,
        body=pretty,
        original_bytes=len(body),
        formatted_bytes=len(pretty),
        diagnostic=(
            f"pretty-printed JSON (indent={indent}, "
            f"sort_keys=True)"
        ),
    )


# Fast-path Q&A


_CODE_FENCE_RE = re.compile(r"```")
_MENTION_RE = re.compile(r"(^|\s)@\S")


def classify_fast_path(
    input_text: str,
) -> FastPathClassification:
    """Pure-function classifier. NEVER raises.

    Fast-path criteria (ALL must hold):
      * Not a slash command (no leading ``/``)
      * No @-mention substring
      * No code fence (```)
      * Length below ``fast_path_max_len``

    Callers can route fast-path inputs to a cheaper provider
    chain. Routing decision stays operator-side."""
    text = str(input_text or "")
    length = len(text)
    has_slash = text.lstrip().startswith("/")
    has_mention = bool(_MENTION_RE.search(text))
    has_code_fence = bool(_CODE_FENCE_RE.search(text))
    if not master_enabled() or not fast_path_enabled():
        return FastPathClassification(
            is_fast_path=False,
            input_length=length,
            has_slash=has_slash,
            has_mention=has_mention,
            has_code_fence=has_code_fence,
            diagnostic=(
                f"disabled via {_ENV_MASTER}=false"
                if not master_enabled()
                else "fast_path disabled by sub-flag"
            ),
        )
    cap = fast_path_max_len()
    is_fast = (
        length > 0
        and length <= cap
        and not has_slash
        and not has_mention
        and not has_code_fence
    )
    reasons: List[str] = []
    if has_slash:
        reasons.append("slash command")
    if has_mention:
        reasons.append("@mention")
    if has_code_fence:
        reasons.append("code fence")
    if length > cap:
        reasons.append(f"len={length}>cap={cap}")
    if not text.strip():
        reasons.append("empty")
    diagnostic = (
        "fast-path" if is_fast
        else f"not fast-path: {', '.join(reasons) or 'unknown'}"
    )
    return FastPathClassification(
        is_fast_path=is_fast,
        input_length=length,
        has_slash=has_slash,
        has_mention=has_mention,
        has_code_fence=has_code_fence,
        diagnostic=diagnostic,
    )


# Renderer


def format_completion_panel(
    *,
    palette: Optional[PaletteResult] = None,
    verb_help: Optional[VerbHelp] = None,
    output: Optional[FormattedOutput] = None,
    classification: Optional[FastPathClassification] = None,
) -> str:
    """Combined operator panel. NEVER raises."""
    if not master_enabled():
        return (
            f"repl smart completion: disabled "
            f"({_ENV_MASTER}=false)"
        )
    parts: List[str] = []
    if verb_help is not None:
        parts.append(verb_help.render())
    if palette is not None and palette.matches:
        lines = ["📋 Palette:"]
        for m in palette.matches[:10]:
            scoretag = (
                "" if m.score == 0
                else f" (d={m.score})"
            )
            short_desc = m.description[:60]
            lines.append(
                f"  {kind_glyph(m.kind)} {m.slash_form}"
                f"{scoretag} — {short_desc}"
            )
        parts.append("\n".join(lines))
    if output is not None and output.format is OutputFormat.PRETTY_JSON:
        parts.append("📦 JSON:")
        parts.append(output.body[:2000])
    if classification is not None:
        fg = "⚡" if classification.is_fast_path else "🐢"
        parts.append(
            f"{fg} fast_path={classification.is_fast_path} "
            f"(len={classification.input_length})"
        )
    if not parts:
        return "repl smart completion: nothing to render"
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
        "backend/core/ouroboros/governance/"
        "repl_smart_completion.py"
    )

    _EXPECTED_KINDS = {
        "verb", "mention", "argument", "none",
    }
    _EXPECTED_FORMATS = {
        "plain", "pretty_json", "truncated", "disabled",
    }

    def _validate_kind_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CompletionKind"
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
                missing = _EXPECTED_KINDS - found
                extra = found - _EXPECTED_KINDS
                if missing:
                    return (
                        f"CompletionKind missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"CompletionKind drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("CompletionKind class not found",)

    def _validate_format_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "OutputFormat"
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
                missing = _EXPECTED_FORMATS - found
                extra = found - _EXPECTED_FORMATS
                if missing:
                    return (
                        f"OutputFormat missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"OutputFormat drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("OutputFormat class not found",)

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
                "must compose flag_registry."
                "levenshtein_distance (canonical typo "
                "distance)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "repl_smart_completion_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CompletionKind 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_kind_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "repl_smart_completion_format_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "OutputFormat 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_format_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "repl_smart_completion_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure REPL completion "
                "composer. MUST NOT import orchestrator / "
                "iron_gate / etc / serpent_flow."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "repl_smart_completion_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "repl_smart_completion_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes battle_test.repl_completion + "
                "governance.flag_registry.levenshtein_distance. "
                "No parallel verb registry, no parallel "
                "typo distance."
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
        "backend/core/ouroboros/governance/"
        "repl_smart_completion.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "REPL smart completion master. §33.1 "
                "default-FALSE. Closes §41 Phase 0 UX Polish "
                "Slice 3 (PRD v3.0+). When on: per-verb "
                "/help formatter + fuzzy slash palette + "
                "JSON pretty-print + fast-path Q&A "
                "classification."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PRETTY_JSON_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — pretty-print JSON tool output. "
                "Default TRUE."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_PRETTY_JSON_ENABLED}=false",
        ),
        FlagSpec(
            name=_ENV_FAST_PATH_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — classify simple Q&A inputs for "
                "fast-path routing. Default TRUE."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_FAST_PATH_ENABLED}=false",
        ),
        FlagSpec(
            name=_ENV_FAST_PATH_MAX_LEN,
            type=FlagType.INT,
            default=_DEFAULT_FAST_PATH_MAX_LEN,
            description=(
                "Max input length for fast-path classification. "
                "Default 200 chars."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FAST_PATH_MAX_LEN}=500",
        ),
        FlagSpec(
            name=_ENV_PALETTE_MAX_RESULTS,
            type=FlagType.INT,
            default=_DEFAULT_PALETTE_MAX_RESULTS,
            description=(
                "Cap on palette ranked results. Default 8."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_PALETTE_MAX_RESULTS}=12",
        ),
        FlagSpec(
            name=_ENV_PALETTE_DISTANCE,
            type=FlagType.INT,
            default=_DEFAULT_PALETTE_DISTANCE,
            description=(
                "Levenshtein threshold for fuzzy palette "
                "matches. Default 3."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_PALETTE_DISTANCE}=2",
        ),
        FlagSpec(
            name=_ENV_JSON_INDENT,
            type=FlagType.INT,
            default=_DEFAULT_JSON_INDENT,
            description=(
                "JSON pretty-print indent. Default 2. "
                "Clamped to [0, 8]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_JSON_INDENT}=4",
        ),
        FlagSpec(
            name=_ENV_OUTPUT_BOUND,
            type=FlagType.INT,
            default=_DEFAULT_OUTPUT_BOUND,
            description=(
                "Cap on pretty-printed output bytes. Default "
                "8192. Larger results → TRUNCATED."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_OUTPUT_BOUND}=16384",
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
    "REPL_SMART_COMPLETION_SCHEMA_VERSION",
    "CompletionKind",
    "OutputFormat",
    "VerbHelp",
    "CompletionMatch",
    "PaletteResult",
    "FormattedOutput",
    "FastPathClassification",
    "master_enabled",
    "pretty_json_enabled",
    "fast_path_enabled",
    "fast_path_max_len",
    "palette_max_results",
    "palette_distance_threshold",
    "json_indent",
    "output_bound",
    "kind_glyph",
    "format_glyph",
    "build_verb_help",
    "rank_palette",
    "is_json_shaped",
    "pretty_print_json",
    "classify_fast_path",
    "format_completion_panel",
    "register_shipped_invariants",
    "register_flags",
]
