"""
OutputContract — Slice 1 of the Rich Formatted Output Control arc.
===================================================================

Declarative spec for what a model's output is *supposed to* look
like. An :class:`OutputContract` bundles:

* An :class:`OutputFormat` (the top-level shape — JSON, markdown with
  required sections, CSV, YAML, code-fenced block, plain prose).
* An optional :class:`OutputSchema` — a narrow JSON-schema-ish dialect
  describing the fields the output MUST contain.
* Required markdown sections (header-keyed blocks the validator will
  look for).
* An optional fence language (``json`` / ``yaml`` / ``python`` / ...).
* Extractor hints (regex anchors) that help the extractor find
  structured fragments inside mixed prose.

Contracts are authored by operators / the orchestrator at runtime and
handed to the candidate generator / tool loop / REPL renderer. Slice 2
implements the validator + extractor; Slice 3 implements the repair
loop; Slice 4 wires the renderer + a ``/format`` REPL; Slice 5
graduates.

Manifesto alignment
-------------------

* §1 — contracts are operator / orchestrator data; the model writes
  against them but cannot define them.
* §5 — deterministic. Every validation decision is reached by
  string / regex / typed accessor operations. No LLM ever decides
  "is this well-formed."
* §7 — fail-closed. Construction validates every field; a contract
  with an unknown format or malformed schema raises at build time,
  not at apply time.
* §8 — contracts project to a dict for audit; Slice 4 feeds this
  to SSE + GET endpoints.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.OutputContract")


OUTPUT_CONTRACT_SCHEMA_VERSION: str = "output_contract.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutputContractError(ValueError):
    """Raised when a contract is malformed at construction."""


# ---------------------------------------------------------------------------
# OutputFormat enum
# ---------------------------------------------------------------------------


class OutputFormat(str, enum.Enum):
    """Top-level shape the output must take.

    Kept narrow on purpose — extending requires a deliberate review
    moment (new format = new validation + extraction path).
    """

    JSON = "json"
    """Output is a single JSON document (object or array)."""

    MARKDOWN_SECTIONS = "markdown_sections"
    """Output is markdown with named top-level sections. Validator
    checks each required section exists (``## <name>``) and extracts
    its body."""

    CSV = "csv"
    """Comma-separated values with a header row."""

    YAML = "yaml"
    """A YAML document."""

    CODE_BLOCK = "code_block"
    """A single ```fence``` block with a specified language tag."""

    PLAIN = "plain"
    """Plain prose, no structural guarantees beyond length caps."""


_ALL_FORMATS: FrozenSet[str] = frozenset(f.value for f in OutputFormat)


# ---------------------------------------------------------------------------
# OutputSchema — narrow JSON-schema-ish dialect for FIELDS
# ---------------------------------------------------------------------------


# Allowed primitive types for schema fields.
_ALLOWED_FIELD_TYPES: FrozenSet[str] = frozenset({
    "string", "integer", "number", "boolean", "array", "object",
})


# Allowed schema field-spec keys. Unknown keys rejected for dialect
# stability.
_ALLOWED_FIELD_SPEC_KEYS: FrozenSet[str] = frozenset({
    "type", "description", "required", "default", "enum",
    "minimum", "maximum", "min_length", "max_length",
    "regex",      # for strings — must match this pattern
    "items",      # for arrays — element type / constraints
})


_FIELD_NAME_RX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class OutputSchema:
    """Frozen schema describing the required shape of structured output.

    Field specs live in ``fields`` keyed by field name. Each spec is a
    mapping with at minimum a ``type`` and optionally ``required``,
    ``description``, ``enum``, bounds, and per-type extras.
    """

    fields: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    strict: bool = True
    """When True, presence of unknown fields is a validation failure.
    When False, unknown fields are tolerated — useful for forward-
    compatibility against plugins that emit extras."""

    schema_version: str = OUTPUT_CONTRACT_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OutputSchema":
        """Build + validate from a dict. Raises :class:`OutputContractError`."""
        if not isinstance(data, Mapping):
            raise OutputContractError(
                f"schema must be a mapping; got {type(data).__name__}"
            )
        fields_raw = data.get("fields", {})
        if not isinstance(fields_raw, Mapping):
            raise OutputContractError(
                "schema.fields must be a mapping of field_name -> spec"
            )
        strict = data.get("strict", True)
        if not isinstance(strict, bool):
            raise OutputContractError(
                "schema.strict must be a boolean"
            )
        validated_fields: Dict[str, Mapping[str, Any]] = {}
        for name, spec in fields_raw.items():
            _validate_field_spec(name, spec)
            validated_fields[name] = dict(spec)
        return cls(fields=validated_fields, strict=strict)

    # Convenience accessors
    @property
    def required_field_names(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                n for n, s in self.fields.items()
                if s.get("required", False)
            )
        )

    @property
    def field_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self.fields.keys()))


def _validate_field_spec(name: str, spec: Any) -> None:
    if not isinstance(name, str) or not _FIELD_NAME_RX.match(name):
        raise OutputContractError(
            f"schema: field name {name!r} must match {_FIELD_NAME_RX.pattern}"
        )
    if not isinstance(spec, Mapping):
        raise OutputContractError(
            f"schema.{name} must be a mapping; got {type(spec).__name__}"
        )
    field_type = spec.get("type")
    if field_type not in _ALLOWED_FIELD_TYPES:
        raise OutputContractError(
            f"schema.{name}.type must be one of {sorted(_ALLOWED_FIELD_TYPES)}; "
            f"got {field_type!r}"
        )
    for key in spec.keys():
        if key not in _ALLOWED_FIELD_SPEC_KEYS:
            raise OutputContractError(
                f"schema.{name}: unknown key {key!r} "
                f"(allowed: {sorted(_ALLOWED_FIELD_SPEC_KEYS)})"
            )
    # Validate sub-keys that have rigid shapes
    if "required" in spec and not isinstance(spec["required"], bool):
        raise OutputContractError(
            f"schema.{name}.required must be a boolean"
        )
    if "enum" in spec:
        enum_vals = spec["enum"]
        if not isinstance(enum_vals, (list, tuple)) or not enum_vals:
            raise OutputContractError(
                f"schema.{name}.enum must be a non-empty list"
            )
    if "regex" in spec:
        rx = spec["regex"]
        if not isinstance(rx, str) or not rx:
            raise OutputContractError(
                f"schema.{name}.regex must be a non-empty string"
            )
        try:
            re.compile(rx)
        except re.error as exc:
            raise OutputContractError(
                f"schema.{name}.regex is invalid: {exc}"
            ) from exc
    if "items" in spec:
        items = spec["items"]
        if not isinstance(items, Mapping):
            raise OutputContractError(
                f"schema.{name}.items must be a mapping"
            )
        # items can name a type alone (partial spec)
        items_type = items.get("type")
        if items_type is not None and items_type not in _ALLOWED_FIELD_TYPES:
            raise OutputContractError(
                f"schema.{name}.items.type must be one of "
                f"{sorted(_ALLOWED_FIELD_TYPES)}; got {items_type!r}"
            )


# ---------------------------------------------------------------------------
# Markdown section spec
# ---------------------------------------------------------------------------


_SECTION_NAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-]{0,63}$")


@dataclass(frozen=True)
class MarkdownSection:
    """One required top-level section in a markdown_sections output.

    ``heading_level`` controls which ``#`` prefix matches at
    extraction time — default ``2`` (``## Title``). ``min_body_chars``
    guards against empty-stub sections.
    """

    name: str
    heading_level: int = 2
    required: bool = True
    min_body_chars: int = 0
    description: str = ""


def _validate_markdown_section(section: Any) -> MarkdownSection:
    if not isinstance(section, MarkdownSection):
        raise OutputContractError(
            "sections must be MarkdownSection instances"
        )
    if not isinstance(section.name, str) \
            or not _SECTION_NAME_RX.match(section.name):
        raise OutputContractError(
            f"markdown_section name {section.name!r} must match "
            f"{_SECTION_NAME_RX.pattern}"
        )
    if not (1 <= section.heading_level <= 6):
        raise OutputContractError(
            "markdown_section.heading_level must be between 1 and 6"
        )
    if section.min_body_chars < 0:
        raise OutputContractError(
            "markdown_section.min_body_chars must be >= 0"
        )
    return section


# ---------------------------------------------------------------------------
# OutputContract
# ---------------------------------------------------------------------------


_CONTRACT_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_\-:]{0,63}$")


@dataclass(frozen=True)
class OutputContract:
    """Declarative spec for a single model output.

    Build via :meth:`from_mapping` for validation; direct construction
    is allowed but bypasses checks (reserved for internals).
    """

    name: str
    format: OutputFormat
    description: str = ""
    schema: Optional[OutputSchema] = None
    sections: Tuple[MarkdownSection, ...] = ()
    fence_language: str = ""   # for CODE_BLOCK
    max_length_chars: int = 100_000
    min_length_chars: int = 0
    extractor_hints: Tuple[str, ...] = ()
    """Optional regex patterns that validators can use to locate
    structured fragments inside mixed prose (Slice 2 consumes these)."""

    schema_version: str = OUTPUT_CONTRACT_SCHEMA_VERSION

    # --- construction ---------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OutputContract":
        """Build + validate from a dict. Raises :class:`OutputContractError`."""
        if not isinstance(data, Mapping):
            raise OutputContractError(
                f"contract must be a mapping; got {type(data).__name__}"
            )

        def _req_str(key: str) -> str:
            val = data.get(key)
            if not isinstance(val, str) or not val.strip():
                raise OutputContractError(
                    f"contract.{key} must be a non-empty string"
                )
            return val.strip()

        name = _req_str("name")
        if not _CONTRACT_NAME_RX.match(name):
            raise OutputContractError(
                f"contract.name {name!r} must match {_CONTRACT_NAME_RX.pattern}"
            )

        format_raw = data.get("format")
        if format_raw is None:
            raise OutputContractError("contract.format is required")
        if isinstance(format_raw, OutputFormat):
            fmt = format_raw
        elif isinstance(format_raw, str):
            if format_raw not in _ALL_FORMATS:
                raise OutputContractError(
                    f"contract.format {format_raw!r} must be one of "
                    f"{sorted(_ALL_FORMATS)}"
                )
            fmt = OutputFormat(format_raw)
        else:
            raise OutputContractError(
                f"contract.format must be str or OutputFormat; "
                f"got {type(format_raw).__name__}"
            )

        description = ""
        if "description" in data:
            raw_desc = data["description"]
            if not isinstance(raw_desc, str):
                raise OutputContractError(
                    "contract.description must be a string if present"
                )
            if len(raw_desc) > 2000:
                raise OutputContractError(
                    "contract.description must be ≤ 2000 chars"
                )
            description = raw_desc.strip()

        schema: Optional[OutputSchema] = None
        if "schema" in data and data["schema"] is not None:
            schema = OutputSchema.from_mapping(data["schema"])

        sections_raw = data.get("sections", ())
        if not isinstance(sections_raw, (list, tuple)):
            raise OutputContractError(
                "contract.sections must be a list of MarkdownSection "
                "or mappings"
            )
        sections: List[MarkdownSection] = []
        for s in sections_raw:
            if isinstance(s, Mapping):
                sections.append(_validate_markdown_section(
                    MarkdownSection(
                        name=s.get("name", ""),
                        heading_level=int(s.get("heading_level", 2)),
                        required=bool(s.get("required", True)),
                        min_body_chars=int(s.get("min_body_chars", 0)),
                        description=str(s.get("description", "")).strip(),
                    ),
                ))
            elif isinstance(s, MarkdownSection):
                sections.append(_validate_markdown_section(s))
            else:
                raise OutputContractError(
                    f"section entry must be MarkdownSection or mapping; "
                    f"got {type(s).__name__}"
                )

        fence_language = ""
        if "fence_language" in data and data["fence_language"] is not None:
            raw_lang = data["fence_language"]
            if not isinstance(raw_lang, str):
                raise OutputContractError(
                    "contract.fence_language must be a string"
                )
            if not re.match(r"^[a-z0-9+.\-]{0,32}$", raw_lang):
                raise OutputContractError(
                    f"contract.fence_language {raw_lang!r} has disallowed chars"
                )
            fence_language = raw_lang

        max_len = int(data.get("max_length_chars", 100_000))
        min_len = int(data.get("min_length_chars", 0))
        if max_len <= 0:
            raise OutputContractError(
                "contract.max_length_chars must be > 0"
            )
        if min_len < 0:
            raise OutputContractError(
                "contract.min_length_chars must be >= 0"
            )
        if min_len > max_len:
            raise OutputContractError(
                "contract.min_length_chars must be <= max_length_chars"
            )

        extractor_hints_raw = data.get("extractor_hints", ())
        if not isinstance(extractor_hints_raw, (list, tuple)):
            raise OutputContractError(
                "contract.extractor_hints must be a list of regex patterns"
            )
        hints: List[str] = []
        for h in extractor_hints_raw:
            if not isinstance(h, str) or not h:
                raise OutputContractError(
                    "extractor_hints entries must be non-empty strings"
                )
            try:
                re.compile(h)
            except re.error as exc:
                raise OutputContractError(
                    f"extractor_hint {h!r} is not a valid regex: {exc}"
                ) from exc
            hints.append(h)

        # Cross-field validation: format-specific requirements
        if fmt is OutputFormat.MARKDOWN_SECTIONS and not sections:
            raise OutputContractError(
                "MARKDOWN_SECTIONS contract must declare at least one section"
            )
        if fmt is OutputFormat.CODE_BLOCK and not fence_language:
            raise OutputContractError(
                "CODE_BLOCK contract must declare a fence_language"
            )
        if fmt is OutputFormat.JSON and schema is None:
            # A JSON contract without a schema is a very weak contract;
            # we allow it but log at DEBUG. Slice 2's validator will
            # still check the output is parseable JSON.
            logger.debug(
                "[OutputContract] JSON contract %s has no schema — will "
                "only check parseability", name,
            )

        contract = cls(
            name=name,
            format=fmt,
            description=description,
            schema=schema,
            sections=tuple(sections),
            fence_language=fence_language,
            max_length_chars=max_len,
            min_length_chars=min_len,
            extractor_hints=tuple(hints),
        )
        logger.info(
            "[OutputContract] built name=%s format=%s schema_fields=%d "
            "sections=%d",
            contract.name, contract.format.value,
            len(contract.schema.fields) if contract.schema else 0,
            len(contract.sections),
        )
        return contract

    # --- projection ------------------------------------------------------

    def project(self) -> Dict[str, Any]:
        """JSON-safe projection for SSE / REPL / GET endpoints."""
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "format": self.format.value,
            "description": self.description,
            "schema": (
                {
                    "field_names": list(self.schema.field_names),
                    "required_field_names": list(
                        self.schema.required_field_names
                    ),
                    "strict": self.schema.strict,
                }
                if self.schema is not None else None
            ),
            "sections": [
                {
                    "name": s.name,
                    "heading_level": s.heading_level,
                    "required": s.required,
                    "min_body_chars": s.min_body_chars,
                    "description": s.description,
                }
                for s in self.sections
            ],
            "fence_language": self.fence_language,
            "max_length_chars": self.max_length_chars,
            "min_length_chars": self.min_length_chars,
            "extractor_hints_count": len(self.extractor_hints),
        }


# ---------------------------------------------------------------------------
# Helpers exposed for Slice 2's validator
# ---------------------------------------------------------------------------


def known_field_types() -> FrozenSet[str]:
    """Public accessor for the whitelist — Slice 2 validator reuses."""
    return _ALLOWED_FIELD_TYPES


def known_formats() -> FrozenSet[str]:
    return _ALL_FORMATS


__all__ = [
    "OUTPUT_CONTRACT_SCHEMA_VERSION",
    "MarkdownSection",
    "OutputContract",
    "OutputContractError",
    "OutputFormat",
    "OutputSchema",
    "known_field_types",
    "known_formats",
]

_ = (field,)  # silence unused-import guard
