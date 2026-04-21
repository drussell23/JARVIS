"""
OutputValidator + Extractor + Repair + Renderer + REPL — Slices 2/3/4.
=======================================================================

Five tightly-coupled primitives for the Rich Formatted Output Control
arc, bundled in one module because they form a single pipeline:

    raw text
        |
        v
    OutputExtractor  — pull structured fragments out of mixed prose
        |
        v
    OutputValidator  — check against the OutputContract
        |
        v
    [if valid]                             [if invalid]
    OutputRenderer                         OutputRepairLoop
    (format-to-surface)                    (bounded repair prompt)
                                                |
                                         (re-run model, re-pipe)

The model NEVER decides "is this output well-formed." Every stage is
pure code.

Exports
-------

* :class:`OutputValidator` / :class:`ValidationResult` — validate raw
  text against a :class:`OutputContract`.
* :class:`OutputExtractor` — parse JSON / YAML / CSV / code-fenced
  blocks / markdown sections out of mixed text.
* :class:`OutputRepairPrompt` / :class:`OutputRepairLoop` — construct
  deterministic repair prompts; run bounded repair cycles.
* :class:`OutputRenderer` + :class:`OutputRendererRegistry` —
  map validated output to surface-specific formatted strings.
* :func:`dispatch_format_command` — ``/format`` REPL dispatcher.
"""
from __future__ import annotations

import csv
import enum
import io
import json
import logging
import re
import shlex
import textwrap
import threading
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.output_contract import (
    MarkdownSection,
    OutputContract,
    OutputFormat,
    OutputSchema,
)

logger = logging.getLogger("Ouroboros.OutputValidator")


OUTPUT_VALIDATOR_SCHEMA_VERSION: str = "output_validator.v1"


# ===========================================================================
# ValidationResult — what the pipeline emits
# ===========================================================================


@dataclass(frozen=True)
class ValidationIssue:
    """One validation failure point with enough detail to repair."""

    path: str                # dotted field path or section name
    code: str                # stable error code
    message: str             # human-readable
    severity: str = "error"  # "error" | "warning"


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of :meth:`OutputValidator.validate`.

    ``extracted`` carries the structured data parsed out — the raw
    JSON / CSV rows / section bodies / etc. — so downstream consumers
    don't need to re-parse.
    """

    ok: bool
    contract_name: str
    format: str
    issues: Tuple[ValidationIssue, ...] = ()
    extracted: Mapping[str, Any] = field(default_factory=dict)
    raw_length: int = 0
    schema_version: str = OUTPUT_VALIDATOR_SCHEMA_VERSION


# ===========================================================================
# OutputExtractor — pure parsers
# ===========================================================================


_FENCE_RX = re.compile(
    r"```([A-Za-z0-9+.\-]*)\s*\n(.*?)\n```",
    re.DOTALL,
)


class OutputExtractor:
    """Deterministic parsers for each :class:`OutputFormat`.

    Static methods; no state. Each ``parse_*`` returns a structured
    payload or raises :class:`ValueError` on parse failure — the
    validator turns that exception into a :class:`ValidationIssue`.
    """

    # --- JSON -----------------------------------------------------------

    @staticmethod
    def parse_json(raw: str) -> Any:
        """Parse the first JSON document in ``raw``.

        Tolerant of a leading/trailing fence block: if ``raw`` wraps
        the document in ``” ``json ... ```” we unwrap first.
        """
        trimmed = OutputExtractor._unfence_if_any(raw, lang="json")
        return json.loads(trimmed)

    # --- YAML -----------------------------------------------------------

    @staticmethod
    def parse_yaml(raw: str) -> Any:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ValueError(f"PyYAML not available: {exc}") from exc
        trimmed = OutputExtractor._unfence_if_any(raw, lang="yaml")
        # PyYAML's scanner errors are YAMLError subclasses, NOT
        # ValueError — wrap so our validator catches a uniform type.
        try:
            return yaml.safe_load(trimmed)
        except yaml.YAMLError as exc:
            raise ValueError(f"yaml parse failed: {exc}") from exc

    # --- CSV ------------------------------------------------------------

    @staticmethod
    def parse_csv(raw: str) -> Dict[str, Any]:
        """Parse CSV with a header row.

        Returns ``{"header": [...], "rows": [[...], [...]], "row_dicts": [{...}]}``.
        """
        trimmed = OutputExtractor._unfence_if_any(raw, lang="csv")
        reader = csv.reader(io.StringIO(trimmed.strip()))
        rows = list(reader)
        if not rows:
            raise ValueError("CSV has no rows")
        header = rows[0]
        data_rows = rows[1:]
        return {
            "header": header,
            "rows": data_rows,
            "row_dicts": [
                {h: v for h, v in zip(header, row)}
                for row in data_rows
            ],
        }

    # --- Code block -----------------------------------------------------

    @staticmethod
    def parse_code_block(
        raw: str, *, expected_language: str = "",
    ) -> Dict[str, Any]:
        """Extract the first fenced code block.

        Returns ``{"language": <str>, "body": <str>}``. Raises
        ``ValueError`` if no fence found or the language doesn't match
        when ``expected_language`` is given.
        """
        m = _FENCE_RX.search(raw)
        if m is None:
            raise ValueError("no ```fenced``` code block found")
        lang = m.group(1).strip().lower()
        body = m.group(2)
        if expected_language and lang != expected_language.lower():
            raise ValueError(
                f"fence language {lang!r} does not match expected "
                f"{expected_language!r}"
            )
        return {"language": lang, "body": body}

    # --- Markdown sections ----------------------------------------------

    @staticmethod
    def parse_markdown_sections(
        raw: str, sections: Sequence[MarkdownSection],
    ) -> Dict[str, str]:
        """Extract each declared section's body.

        Builds a section map ``{section.name: body_text}``. Missing
        sections are omitted from the map — the validator treats
        missing required sections as issues; this parser only
        parses.
        """
        found: Dict[str, str] = {}
        for section in sections:
            header_marker = "#" * section.heading_level
            # ``## Section Name`` anywhere in the doc; body continues
            # until the next same-level or shallower heading or EOF.
            # Accept optional whitespace + tail characters on the
            # section line (e.g. "## Summary  ").
            pattern = (
                rf"^{re.escape(header_marker)}\s+"
                rf"{re.escape(section.name)}\s*$"
            )
            start_rx = re.compile(pattern, re.MULTILINE)
            m = start_rx.search(raw)
            if m is None:
                continue
            body_start = m.end()
            # Find the next heading at this level or shallower.
            stop_pattern = (
                rf"^#{{1,{section.heading_level}}}\s+\S"
            )
            stop_rx = re.compile(stop_pattern, re.MULTILINE)
            stop_m = stop_rx.search(raw, pos=body_start)
            body_end = stop_m.start() if stop_m else len(raw)
            body = raw[body_start:body_end].strip()
            found[section.name] = body
        return found

    # --- Extractor hints (user-supplied regex anchors) ------------------

    @staticmethod
    def apply_extractor_hints(
        raw: str, patterns: Sequence[str],
    ) -> Dict[str, List[str]]:
        """For every caller-supplied regex, return all capture groups.

        Deterministic: patterns are compiled case-insensitively; each
        match contributes its ``.group(1)`` (or the whole match if no
        group) to ``out[pattern]``. Malformed patterns are silently
        skipped (the contract already validates patterns at construct
        time; malformed-at-runtime means a caller bypassed the
        contract — we don't crash).
        """
        out: Dict[str, List[str]] = {}
        for pat in patterns:
            try:
                rx = re.compile(pat, re.DOTALL | re.IGNORECASE)
            except re.error:
                continue
            matches: List[str] = []
            for m in rx.finditer(raw):
                if m.groups():
                    matches.append(m.group(1))
                else:
                    matches.append(m.group(0))
            out[pat] = matches
        return out

    # --- shared helpers -------------------------------------------------

    @staticmethod
    def _unfence_if_any(raw: str, *, lang: str) -> str:
        """If ``raw`` is (or contains) a single fenced block whose
        language matches ``lang``, return the fence body; else return
        ``raw`` unchanged."""
        m = _FENCE_RX.search(raw)
        if m is None:
            return raw
        got_lang = m.group(1).strip().lower()
        if got_lang and got_lang != lang.lower():
            return raw
        return m.group(2)


# ===========================================================================
# OutputValidator — check against a contract
# ===========================================================================


class OutputValidator:
    """Pure validator. Call :meth:`validate` with a contract + raw text."""

    def validate(
        self,
        contract: OutputContract,
        raw: str,
    ) -> ValidationResult:
        if not isinstance(raw, str):
            return ValidationResult(
                ok=False,
                contract_name=contract.name,
                format=contract.format.value,
                issues=(ValidationIssue(
                    path="$",
                    code="non_string_output",
                    message=f"output is not a string ({type(raw).__name__})",
                ),),
                raw_length=0,
            )
        issues: List[ValidationIssue] = []
        extracted: Dict[str, Any] = {}

        # Length caps — run first; applies to every format
        length = len(raw)
        if length < contract.min_length_chars:
            issues.append(ValidationIssue(
                path="$length",
                code="under_min_length",
                message=(
                    f"output length {length} < min_length_chars "
                    f"{contract.min_length_chars}"
                ),
            ))
        if length > contract.max_length_chars:
            issues.append(ValidationIssue(
                path="$length",
                code="over_max_length",
                message=(
                    f"output length {length} > max_length_chars "
                    f"{contract.max_length_chars}"
                ),
            ))

        # Format-specific validation
        fmt = contract.format
        if fmt is OutputFormat.JSON:
            self._validate_json(contract, raw, issues, extracted)
        elif fmt is OutputFormat.MARKDOWN_SECTIONS:
            self._validate_markdown_sections(contract, raw, issues, extracted)
        elif fmt is OutputFormat.CSV:
            self._validate_csv(contract, raw, issues, extracted)
        elif fmt is OutputFormat.YAML:
            self._validate_yaml(contract, raw, issues, extracted)
        elif fmt is OutputFormat.CODE_BLOCK:
            self._validate_code_block(contract, raw, issues, extracted)
        elif fmt is OutputFormat.PLAIN:
            # Plain = only length-cap enforcement; no structural parse.
            pass

        # Extractor hints — always run regardless of format; Slice 4
        # renderers may consume them.
        if contract.extractor_hints:
            extracted["hints"] = OutputExtractor.apply_extractor_hints(
                raw, contract.extractor_hints,
            )

        ok = not any(i.severity == "error" for i in issues)
        return ValidationResult(
            ok=ok,
            contract_name=contract.name,
            format=fmt.value,
            issues=tuple(issues),
            extracted=extracted,
            raw_length=length,
        )

    # --- per-format validators ------------------------------------------

    def _validate_json(
        self, contract: OutputContract, raw: str,
        issues: List[ValidationIssue], extracted: Dict[str, Any],
    ) -> None:
        try:
            value = OutputExtractor.parse_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            issues.append(ValidationIssue(
                path="$", code="json_parse_error",
                message=f"could not parse JSON: {exc}",
            ))
            return
        extracted["value"] = value
        schema = contract.schema
        if schema is None:
            return
        if not isinstance(value, Mapping):
            issues.append(ValidationIssue(
                path="$", code="json_not_object",
                message=(
                    "JSON schema expects a top-level object, got "
                    f"{type(value).__name__}"
                ),
            ))
            return
        self._validate_against_schema(value, schema, issues)

    def _validate_markdown_sections(
        self, contract: OutputContract, raw: str,
        issues: List[ValidationIssue], extracted: Dict[str, Any],
    ) -> None:
        found = OutputExtractor.parse_markdown_sections(raw, contract.sections)
        extracted["sections"] = found
        for section in contract.sections:
            if section.name not in found:
                if section.required:
                    issues.append(ValidationIssue(
                        path=f"sections.{section.name}",
                        code="missing_section",
                        message=(
                            f"required section {section.name!r} "
                            f"(## level {section.heading_level}) is missing"
                        ),
                    ))
                continue
            body = found[section.name]
            if len(body) < section.min_body_chars:
                issues.append(ValidationIssue(
                    path=f"sections.{section.name}",
                    code="section_body_too_short",
                    message=(
                        f"section {section.name!r} body length "
                        f"{len(body)} < min_body_chars "
                        f"{section.min_body_chars}"
                    ),
                ))

    def _validate_csv(
        self, contract: OutputContract, raw: str,
        issues: List[ValidationIssue], extracted: Dict[str, Any],
    ) -> None:
        try:
            parsed = OutputExtractor.parse_csv(raw)
        except ValueError as exc:
            issues.append(ValidationIssue(
                path="$", code="csv_parse_error",
                message=f"could not parse CSV: {exc}",
            ))
            return
        extracted.update(parsed)
        # If a schema is declared, require its fields to be in the header
        schema = contract.schema
        if schema is None:
            return
        header = [h.strip() for h in parsed["header"]]
        for name, spec in schema.fields.items():
            if spec.get("required", False) and name not in header:
                issues.append(ValidationIssue(
                    path=f"header.{name}",
                    code="missing_csv_column",
                    message=f"required column {name!r} missing from CSV header",
                ))
        if schema.strict:
            for h in header:
                if h and h not in schema.fields:
                    issues.append(ValidationIssue(
                        path=f"header.{h}",
                        code="unknown_csv_column",
                        message=(
                            f"CSV column {h!r} is not declared in schema "
                            "(strict mode)"
                        ),
                    ))

    def _validate_yaml(
        self, contract: OutputContract, raw: str,
        issues: List[ValidationIssue], extracted: Dict[str, Any],
    ) -> None:
        try:
            value = OutputExtractor.parse_yaml(raw)
        except ValueError as exc:
            issues.append(ValidationIssue(
                path="$", code="yaml_parse_error",
                message=f"could not parse YAML: {exc}",
            ))
            return
        extracted["value"] = value
        schema = contract.schema
        if schema is None:
            return
        if not isinstance(value, Mapping):
            issues.append(ValidationIssue(
                path="$", code="yaml_not_mapping",
                message=(
                    "YAML schema expects a top-level mapping, got "
                    f"{type(value).__name__}"
                ),
            ))
            return
        self._validate_against_schema(value, schema, issues)

    def _validate_code_block(
        self, contract: OutputContract, raw: str,
        issues: List[ValidationIssue], extracted: Dict[str, Any],
    ) -> None:
        try:
            block = OutputExtractor.parse_code_block(
                raw, expected_language=contract.fence_language,
            )
        except ValueError as exc:
            issues.append(ValidationIssue(
                path="$", code="code_block_error",
                message=str(exc),
            ))
            return
        extracted["block"] = block

    # --- schema enforcement --------------------------------------------

    def _validate_against_schema(
        self, value: Mapping[str, Any], schema: OutputSchema,
        issues: List[ValidationIssue],
    ) -> None:
        for name, spec in schema.fields.items():
            required = bool(spec.get("required", False))
            present = name in value
            if not present:
                if required:
                    issues.append(ValidationIssue(
                        path=f"fields.{name}",
                        code="missing_required_field",
                        message=f"required field {name!r} missing",
                    ))
                continue
            self._check_field(name, spec, value[name], issues)
        if schema.strict:
            known = set(schema.fields.keys())
            for k in value.keys():
                if k not in known:
                    issues.append(ValidationIssue(
                        path=f"fields.{k}",
                        code="unknown_field",
                        message=(
                            f"field {k!r} is not declared "
                            "in schema (strict mode)"
                        ),
                    ))

    def _check_field(
        self, name: str, spec: Mapping[str, Any], value: Any,
        issues: List[ValidationIssue],
    ) -> None:
        expected = spec.get("type")
        type_map = {
            "string":  (str,),
            "integer": (int,),
            "number":  (int, float),
            "boolean": (bool,),
            "array":   (list, tuple),
            "object":  (Mapping,),
        }
        # bool is a subclass of int — guard explicitly for integer/number
        if expected in ("integer", "number") and isinstance(value, bool):
            issues.append(ValidationIssue(
                path=f"fields.{name}", code="wrong_type",
                message=f"field {name!r} must be {expected}, got bool",
            ))
            return
        allowed = type_map.get(expected, ())
        if not isinstance(value, allowed):
            issues.append(ValidationIssue(
                path=f"fields.{name}", code="wrong_type",
                message=(
                    f"field {name!r} must be {expected}, "
                    f"got {type(value).__name__}"
                ),
            ))
            return
        # enum
        if "enum" in spec and value not in spec["enum"]:
            issues.append(ValidationIssue(
                path=f"fields.{name}", code="enum_violation",
                message=(
                    f"field {name!r} must be one of "
                    f"{spec['enum']!r}; got {value!r}"
                ),
            ))
        # minimum / maximum
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                issues.append(ValidationIssue(
                    path=f"fields.{name}", code="under_minimum",
                    message=f"field {name!r} = {value} < minimum "
                            f"{spec['minimum']}",
                ))
            if "maximum" in spec and value > spec["maximum"]:
                issues.append(ValidationIssue(
                    path=f"fields.{name}", code="over_maximum",
                    message=f"field {name!r} = {value} > maximum "
                            f"{spec['maximum']}",
                ))
        # length
        if hasattr(value, "__len__"):
            if "min_length" in spec and len(value) < spec["min_length"]:
                issues.append(ValidationIssue(
                    path=f"fields.{name}", code="under_min_length",
                    message=f"field {name!r} length {len(value)} < "
                            f"min_length {spec['min_length']}",
                ))
            if "max_length" in spec and len(value) > spec["max_length"]:
                issues.append(ValidationIssue(
                    path=f"fields.{name}", code="over_max_length",
                    message=f"field {name!r} length {len(value)} > "
                            f"max_length {spec['max_length']}",
                ))
        # regex (strings only)
        if isinstance(value, str) and "regex" in spec:
            try:
                if not re.search(spec["regex"], value):
                    issues.append(ValidationIssue(
                        path=f"fields.{name}", code="regex_mismatch",
                        message=f"field {name!r} does not match regex "
                                f"{spec['regex']!r}",
                    ))
            except re.error:
                # Pre-validated at contract construction; unreachable
                # except via bypass.
                pass


# ===========================================================================
# OutputRepairPrompt + OutputRepairLoop
# ===========================================================================


@dataclass(frozen=True)
class OutputRepairPrompt:
    """Deterministic repair instruction for a failing output.

    The prompt is PURE-CODE generated from the :class:`ValidationResult`
    — the model is told exactly what it violated and how to fix it,
    but the REPAIR LOGIC itself is computed by :func:`build_repair_prompt`.
    """

    text: str
    contract_name: str
    issues: Tuple[ValidationIssue, ...]
    attempt: int
    schema_version: str = OUTPUT_VALIDATOR_SCHEMA_VERSION


def build_repair_prompt(
    *, contract: OutputContract, previous_raw: str,
    result: ValidationResult, attempt: int,
) -> OutputRepairPrompt:
    """Construct a repair prompt that names every failure.

    The structure is deterministic so callers can pin exact wording
    in tests and model providers can cache prefix-identical parts.
    """
    lines: List[str] = []
    lines.append(
        f"Your previous output did not conform to the required format "
        f"`{contract.name}` (format={contract.format.value})."
    )
    lines.append("Issues found:")
    for issue in result.issues:
        if issue.severity == "error":
            lines.append(
                f"  - [{issue.code}] at {issue.path}: {issue.message}"
            )
    lines.append("")
    lines.append(
        "Return ONLY a corrected output that conforms to the contract. "
        "Do not explain the fix; just emit the corrected output."
    )
    # Per-format reminders — short, deterministic
    fmt = contract.format
    if fmt is OutputFormat.JSON:
        lines.append("Required format: a single JSON document (object or array).")
        if contract.schema:
            required = contract.schema.required_field_names
            if required:
                lines.append(
                    f"Required fields: {', '.join(required)}"
                )
    elif fmt is OutputFormat.MARKDOWN_SECTIONS:
        for s in contract.sections:
            marker = "#" * s.heading_level
            req = "required" if s.required else "optional"
            lines.append(
                f"  - {marker} {s.name}  ({req}, min_body_chars={s.min_body_chars})"
            )
    elif fmt is OutputFormat.CSV:
        lines.append(
            "Required format: CSV with a header row. "
            "First row lists column names."
        )
    elif fmt is OutputFormat.YAML:
        lines.append(
            "Required format: a single YAML document (top-level mapping)."
        )
    elif fmt is OutputFormat.CODE_BLOCK:
        lines.append(
            "Required format: a single ```"
            f"{contract.fence_language}" "``` fenced code block."
        )
    elif fmt is OutputFormat.PLAIN:
        lines.append(
            "Required format: plain text within length bounds "
            f"(min={contract.min_length_chars}, max={contract.max_length_chars})."
        )
    text = "\n".join(lines)
    return OutputRepairPrompt(
        text=text,
        contract_name=contract.name,
        issues=tuple(result.issues),
        attempt=attempt,
    )


@dataclass(frozen=True)
class RepairLoopOutcome:
    """Composite result of a bounded repair loop."""

    converged: bool
    attempts: int
    final_validation: ValidationResult
    repair_prompts: Tuple[OutputRepairPrompt, ...] = ()


class OutputRepairLoop:
    """Bounded iterative repair.

    The caller supplies a ``model_fn`` that takes (original_prompt,
    repair_prompt) and returns a new raw output. The loop is purely
    mechanical: validate → construct repair → call model → re-validate,
    up to ``max_attempts``.

    The caller controls model invocation — this class is agnostic to
    which provider / protocol runs. Tests inject a synchronous function
    that simulates model behavior.
    """

    def __init__(
        self,
        *,
        validator: Optional[OutputValidator] = None,
        max_attempts: int = 2,
    ) -> None:
        self._validator = validator or OutputValidator()
        self._max_attempts = max(0, max_attempts)

    def run(
        self,
        *,
        contract: OutputContract,
        original_prompt: str,
        initial_raw: str,
        model_fn: Callable[[str, OutputRepairPrompt], str],
    ) -> RepairLoopOutcome:
        prompts: List[OutputRepairPrompt] = []
        current_raw = initial_raw
        validation = self._validator.validate(contract, current_raw)
        attempts = 0
        while not validation.ok and attempts < self._max_attempts:
            attempts += 1
            repair_prompt = build_repair_prompt(
                contract=contract,
                previous_raw=current_raw,
                result=validation,
                attempt=attempts,
            )
            prompts.append(repair_prompt)
            try:
                current_raw = model_fn(original_prompt, repair_prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[OutputRepairLoop] model_fn raised on attempt %d: %s",
                    attempts, exc,
                )
                break
            validation = self._validator.validate(contract, current_raw)
        return RepairLoopOutcome(
            converged=validation.ok,
            attempts=attempts,
            final_validation=validation,
            repair_prompts=tuple(prompts),
        )


# ===========================================================================
# OutputRenderer — surface-specific formatting
# ===========================================================================


class RenderSurface(str, enum.Enum):
    REPL = "repl"
    IDE = "ide"
    SSE = "sse"
    PLAIN = "plain"


_AUTHORITATIVE_RENDER_SOURCES: frozenset = frozenset({
    "operator", "orchestrator",
})


Renderer = Callable[[ValidationResult, Mapping[str, Any]], str]


class OutputRendererRegistry:
    """Per-process registry of renderers keyed by ``(format, surface)``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._renderers: Dict[Tuple[str, str], Renderer] = {}

    def register(
        self,
        *,
        format: OutputFormat,
        surface: RenderSurface,
        renderer: Renderer,
    ) -> None:
        if not callable(renderer):
            raise TypeError("renderer must be callable")
        key = (format.value, surface.value)
        with self._lock:
            self._renderers[key] = renderer

    def get(
        self,
        format: OutputFormat, surface: RenderSurface,
    ) -> Optional[Renderer]:
        key = (format.value, surface.value)
        with self._lock:
            return self._renderers.get(key)

    def render(
        self,
        *,
        result: ValidationResult,
        format: OutputFormat,
        surface: RenderSurface,
        opts: Optional[Mapping[str, Any]] = None,
    ) -> str:
        r = self.get(format, surface)
        if r is None:
            # Default: compact JSON dump of the extracted payload
            payload = dict(result.extracted)
            return json.dumps(
                {
                    "ok": result.ok,
                    "contract": result.contract_name,
                    "format": result.format,
                    "extracted": _json_safe(payload),
                },
                indent=2, default=str, sort_keys=True,
            )
        return r(result, dict(opts or {}))

    def reset(self) -> None:
        with self._lock:
            self._renderers.clear()


def _json_safe(value: Any) -> Any:
    """Deeply convert a value to JSON-safe form (tuples → lists, mappings → dicts)."""
    if isinstance(value, Mapping):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(x) for x in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# Built-in renderers registered by default
def _register_defaults(reg: OutputRendererRegistry) -> None:
    def _markdown_repl(result: ValidationResult, _opts: Mapping[str, Any]) -> str:
        sections = result.extracted.get("sections", {})
        lines: List[str] = [f"  validated {result.contract_name} ({result.format})"]
        for name, body in sections.items():
            lines.append(f"")
            lines.append(f"## {name}")
            lines.append(body)
        return "\n".join(lines)

    def _markdown_ide(result: ValidationResult, _opts: Mapping[str, Any]) -> str:
        sections = result.extracted.get("sections", {})
        return json.dumps({
            "ok": result.ok,
            "sections": {
                k: (v if len(v) <= 2000 else v[:2000] + "...<truncated>")
                for k, v in sections.items()
            },
        }, indent=2, default=str)

    def _json_repl(result: ValidationResult, _opts: Mapping[str, Any]) -> str:
        value = result.extracted.get("value", {})
        return json.dumps(_json_safe(value), indent=2, default=str)

    def _plain_repl(result: ValidationResult, _opts: Mapping[str, Any]) -> str:
        return f"  validated {result.contract_name} ({result.format}) — ok={result.ok}"

    reg.register(
        format=OutputFormat.MARKDOWN_SECTIONS,
        surface=RenderSurface.REPL, renderer=_markdown_repl,
    )
    reg.register(
        format=OutputFormat.MARKDOWN_SECTIONS,
        surface=RenderSurface.IDE, renderer=_markdown_ide,
    )
    reg.register(
        format=OutputFormat.JSON,
        surface=RenderSurface.REPL, renderer=_json_repl,
    )
    reg.register(
        format=OutputFormat.PLAIN,
        surface=RenderSurface.REPL, renderer=_plain_repl,
    )


_default_registry: Optional[OutputRendererRegistry] = None
_registry_lock = threading.Lock()


def get_default_renderer_registry() -> OutputRendererRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = OutputRendererRegistry()
            _register_defaults(_default_registry)
        return _default_registry


def reset_default_renderer_registry() -> None:
    global _default_registry
    with _registry_lock:
        if _default_registry is not None:
            _default_registry.reset()
        _default_registry = None


# ===========================================================================
# Contract registry (for `/format` REPL to look up + list)
# ===========================================================================


class OutputContractRegistry:
    """Per-process store of named :class:`OutputContract` objects."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contracts: Dict[str, OutputContract] = {}

    def register(self, contract: OutputContract) -> None:
        with self._lock:
            if contract.name in self._contracts:
                raise KeyError(
                    f"contract already registered: {contract.name}"
                )
            self._contracts[contract.name] = contract

    def get(self, name: str) -> Optional[OutputContract]:
        with self._lock:
            return self._contracts.get(name)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._contracts.pop(name, None) is not None

    def list_all(self) -> List[OutputContract]:
        with self._lock:
            return sorted(
                self._contracts.values(), key=lambda c: c.name,
            )

    def reset(self) -> None:
        with self._lock:
            self._contracts.clear()


_default_contract_registry: Optional[OutputContractRegistry] = None
_contract_registry_lock = threading.Lock()


def get_default_contract_registry() -> OutputContractRegistry:
    global _default_contract_registry
    with _contract_registry_lock:
        if _default_contract_registry is None:
            _default_contract_registry = OutputContractRegistry()
        return _default_contract_registry


def reset_default_contract_registry() -> None:
    global _default_contract_registry
    with _contract_registry_lock:
        if _default_contract_registry is not None:
            _default_contract_registry.reset()
        _default_contract_registry = None


# ===========================================================================
# REPL dispatcher
# ===========================================================================


@dataclass
class FormatDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_FORMAT_HELP = textwrap.dedent(
    """
    Output format control
    ---------------------
      /format                          — list registered contracts
      /format list                     — same as above
      /format show <name>              — full contract detail
      /format validate <name> <raw>    — validate a string against a contract
      /format help                     — this text
    """
).strip()


_COMMANDS = frozenset({"/format"})


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_format_command(
    line: str,
    *,
    contract_registry: Optional[OutputContractRegistry] = None,
    validator: Optional[OutputValidator] = None,
) -> FormatDispatchResult:
    if not _matches(line):
        return FormatDispatchResult(ok=False, text="", matched=False)
    # For `/format validate`, preserve the raw tail VERBATIM (shlex
    # strips JSON-interior quotes and mangles the payload). For other
    # subcommands, shlex is fine.
    stripped = line.strip()
    if stripped.startswith("/format validate"):
        rest = stripped[len("/format validate"):].strip()
        if not rest:
            return FormatDispatchResult(
                ok=False,
                text='  /format validate <contract-name> <raw-output>',
            )
        name_part, _, raw_tail = rest.partition(" ")
        if not raw_tail.strip():
            return FormatDispatchResult(
                ok=False,
                text='  /format validate <contract-name> <raw-output>',
            )
        reg = contract_registry or get_default_contract_registry()
        val = validator or OutputValidator()
        return _format_validate(reg, val, name_part, raw_tail)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return FormatDispatchResult(
            ok=False, text=f"  /format parse error: {exc}",
        )
    if not tokens:
        return FormatDispatchResult(ok=False, text="", matched=False)
    reg = contract_registry or get_default_contract_registry()
    args = tokens[1:]
    if not args or args[0] == "list":
        return _format_list(reg)
    head = args[0]
    if head == "help":
        return FormatDispatchResult(ok=True, text=_FORMAT_HELP)
    if head == "show":
        if len(args) < 2:
            return FormatDispatchResult(
                ok=False, text="  /format show <contract-name>",
            )
        return _format_show(reg, args[1])
    # Short form: /format <name> → show
    return _format_show(reg, head)


def _format_list(reg: OutputContractRegistry) -> FormatDispatchResult:
    contracts = reg.list_all()
    if not contracts:
        return FormatDispatchResult(
            ok=True, text="  (no contracts registered)",
        )
    lines = [f"  Registered contracts ({len(contracts)}):"]
    for c in contracts:
        lines.append(
            f"  - {c.name:<24} format={c.format.value:<18} "
            f"{c.description[:50]}"
        )
    return FormatDispatchResult(ok=True, text="\n".join(lines))


def _format_show(
    reg: OutputContractRegistry, name: str,
) -> FormatDispatchResult:
    c = reg.get(name)
    if c is None:
        return FormatDispatchResult(
            ok=False, text=f"  /format: unknown contract: {name}",
        )
    p = c.project()
    lines = [
        f"  Contract {c.name}",
        f"    format        : {p['format']}",
        f"    description   : {p['description']}",
        f"    schema        : {p['schema']}",
        f"    sections      : {[s['name'] for s in p['sections']]}",
        f"    fence_language: {p['fence_language']}",
        f"    max_length    : {p['max_length_chars']}",
        f"    min_length    : {p['min_length_chars']}",
        f"    hints_count   : {p['extractor_hints_count']}",
    ]
    return FormatDispatchResult(ok=True, text="\n".join(lines))


def _format_validate(
    reg: OutputContractRegistry,
    validator: OutputValidator,
    name: str,
    raw: str,
) -> FormatDispatchResult:
    c = reg.get(name)
    if c is None:
        return FormatDispatchResult(
            ok=False, text=f"  /format: unknown contract: {name}",
        )
    result = validator.validate(c, raw)
    lines = [
        f"  {c.name} ({c.format.value}): ok={result.ok} "
        f"issues={len(result.issues)}",
    ]
    for issue in result.issues[:10]:
        lines.append(
            f"    [{issue.code}] {issue.path}: {issue.message}"
        )
    return FormatDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "OUTPUT_VALIDATOR_SCHEMA_VERSION",
    "FormatDispatchResult",
    "OutputContractRegistry",
    "OutputExtractor",
    "OutputRendererRegistry",
    "OutputRepairLoop",
    "OutputRepairPrompt",
    "OutputValidator",
    "RenderSurface",
    "RepairLoopOutcome",
    "ValidationIssue",
    "ValidationResult",
    "build_repair_prompt",
    "dispatch_format_command",
    "get_default_contract_registry",
    "get_default_renderer_registry",
    "reset_default_contract_registry",
    "reset_default_renderer_registry",
]
