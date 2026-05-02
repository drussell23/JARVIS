"""
SkillManifest — Slice 1 of the First-Class Skill System arc.
=============================================================

CC-parity primitive for describing an invokable skill. A skill is a
named unit of operator-blessed behavior with:

* **Discovery metadata** — ``name``, ``plugin_namespace``, ``description``
  (one-line tagline), ``trigger`` (narrative "when to use this"), and
  ``usage`` (narrative "how to invoke").
* **Invocation contract** — ``entrypoint`` (dotted import path), an
  ``args_schema`` (JSON-schema-ish bounded dict), and an explicit
  ``permissions`` allowlist (``read_only`` / ``filesystem`` / etc.).
* **Provenance** — ``version``, ``author``, and optional ``path`` where
  the manifest was loaded from.

What this module is NOT
-----------------------

* An invoker. Slice 3's :class:`SkillInvoker` resolves entrypoints and
  runs them.
* A registry. Slice 2's :class:`SkillCatalog` holds loaded manifests.
* A marketplace. Slice 4's :class:`SkillMarketplace` discovers /
  installs / removes.

Manifesto alignment
-------------------

* §1 — skill manifests are authored by operators / plugin publishers;
  the Slice 2 catalog enforces that registration goes through an
  authoritative :class:`HandlerSource`-style tag. The model can
  INVOKE skills (if the catalog exposes them) but cannot author or
  install manifests.
* §5 — YAML parse + schema validation are pure code. Zero LLM.
* §7 — fail-closed. Malformed manifest → :class:`SkillManifestError`
  at parse time, not at invocation time.
* §8 — every load emits an INFO log line with the full
  ``{qualified_name, version, path}`` tuple for audit.
"""
from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

# Slice 1 of the SkillRegistry-AutonomousReach arc: typed reach
# vocabulary + structured trigger primitive (pure-stdlib). Imported
# here so SkillManifest can carry the new additive fields with
# strict-dialect validation. Keeps the existing arc backward-compat:
# manifests without `reach` / `trigger_specs` keep their current
# behavior because reach defaults to OPERATOR_PLUS_MODEL and
# trigger_specs defaults to ().
from backend.core.ouroboros.governance.skill_trigger import (
    SkillReach,
    SkillTriggerError,
    SkillTriggerSpec,
    parse_reach,
    parse_trigger_specs_list,
)

logger = logging.getLogger("Ouroboros.SkillManifest")


SKILL_MANIFEST_SCHEMA_VERSION: str = "skill_manifest.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillManifestError(ValueError):
    """Raised when a manifest is malformed or fails validation."""


# ---------------------------------------------------------------------------
# Name + namespace validation (CC-style)
# ---------------------------------------------------------------------------


# Names: lowercase + digits + hyphen + underscore, 1-64 chars.
# Matches CC's skill naming convention closely.
_NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

# Version: semver-lite — digits.dots. Allows v1, 1.0, 1.0.0, 0.2.1-alpha.
_VERSION_RX = re.compile(
    r"^v?\d+(\.\d+){0,2}(-[A-Za-z0-9.\-]+)?$"
)

# Allowed permission tokens. Whitelist, not blacklist — an unknown
# permission is rejected rather than silently ignored.
_ALLOWED_PERMISSIONS: FrozenSet[str] = frozenset({
    "read_only",        # no filesystem writes, no network
    "filesystem_read",  # may read files
    "filesystem_write", # may write files
    "network",          # may make outbound network calls
    "subprocess",       # may spawn subprocesses
    "env_read",         # may read environment variables
    # Keep this list narrow; widening is a deliberate review moment.
})


# ---------------------------------------------------------------------------
# Arg schema validation
# ---------------------------------------------------------------------------


# Allowed arg types — bounded JSON-schema-ish dialect.
# Keeping it narrow avoids dragging in jsonschema + its transitive deps.
_ALLOWED_ARG_TYPES: FrozenSet[str] = frozenset({
    "string", "integer", "number", "boolean",
    "array",  # element type checked at validate time
    "object", # value type checked at validate time
})


def _validate_arg_schema(
    schema: Mapping[str, Any], *, path: str = "args_schema",
) -> None:
    """Validate a skill's arg-schema dict. Raises on malformed shape."""
    if not isinstance(schema, Mapping):
        raise SkillManifestError(
            f"{path} must be a mapping; got {type(schema).__name__}"
        )
    for arg_name, arg_spec in schema.items():
        if not isinstance(arg_name, str) or not arg_name:
            raise SkillManifestError(
                f"{path}: arg names must be non-empty strings; "
                f"got {arg_name!r}"
            )
        if not _NAME_RX.match(arg_name):
            raise SkillManifestError(
                f"{path}: arg name {arg_name!r} must match "
                f"[a-z0-9][a-z0-9_\\-]{{0,63}}"
            )
        if not isinstance(arg_spec, Mapping):
            raise SkillManifestError(
                f"{path}.{arg_name} must be a mapping; got "
                f"{type(arg_spec).__name__}"
            )
        type_token = arg_spec.get("type")
        if type_token not in _ALLOWED_ARG_TYPES:
            raise SkillManifestError(
                f"{path}.{arg_name}.type must be one of "
                f"{sorted(_ALLOWED_ARG_TYPES)}; got {type_token!r}"
            )
        # Optional keys we allow; unknowns forbidden to keep the
        # dialect narrow.
        allowed_keys = {
            "type", "description", "required", "default", "enum",
            "minimum", "maximum", "min_length", "max_length",
        }
        for key in arg_spec.keys():
            if key not in allowed_keys:
                raise SkillManifestError(
                    f"{path}.{arg_name}: unknown key {key!r} "
                    f"(allowed: {sorted(allowed_keys)})"
                )
        # Basic sanity: required must be bool if present
        if "required" in arg_spec and not isinstance(
            arg_spec["required"], bool,
        ):
            raise SkillManifestError(
                f"{path}.{arg_name}.required must be bool"
            )
        # enum must be a list of primitive values
        if "enum" in arg_spec:
            enum_vals = arg_spec["enum"]
            if not isinstance(enum_vals, (list, tuple)) or not enum_vals:
                raise SkillManifestError(
                    f"{path}.{arg_name}.enum must be a non-empty list"
                )


# ---------------------------------------------------------------------------
# Entrypoint validation — dotted module path + callable
# ---------------------------------------------------------------------------


_ENTRYPOINT_RX = re.compile(
    # Two accepted shapes:
    #   1. dotted module path, optional :callable suffix
    #      e.g. "pkg.mod.fn"  or  "pkg.mod:fn"
    #   2. single-segment module + REQUIRED :callable
    #      e.g. "mymodule:fn"  (the colon disambiguates from a bare var)
    r"^[a-zA-Z_][a-zA-Z0-9_]*"
    r"(?:"
    r"  (?:\.[a-zA-Z_][a-zA-Z0-9_]*)+"
    r"  (?::[a-zA-Z_][a-zA-Z0-9_]*)?"
    r"  |"
    r"  :[a-zA-Z_][a-zA-Z0-9_]*"
    r")$",
    re.VERBOSE,
)


def _validate_entrypoint(entrypoint: str) -> None:
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise SkillManifestError("entrypoint must be a non-empty string")
    if not _ENTRYPOINT_RX.match(entrypoint):
        raise SkillManifestError(
            f"entrypoint {entrypoint!r} must be a dotted module path "
            "(e.g. 'my_plugin.skills.my_skill:run' or "
            "'my_plugin.skills.my_skill.run')"
        )


# ---------------------------------------------------------------------------
# SkillManifest dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillManifest:
    """Frozen skill metadata record.

    Use :meth:`from_mapping` (raw dict) or :meth:`from_yaml_file` (disk)
    to construct — both run full validation. Direct construction works
    but bypasses validation and should be reserved for internals /
    tests.
    """

    name: str
    description: str
    trigger: str
    entrypoint: str
    plugin_namespace: Optional[str] = None
    usage: str = ""
    args_schema: Mapping[str, Any] = field(default_factory=dict)
    permissions: Tuple[str, ...] = ()
    version: str = "0.0.0"
    author: str = ""
    path: Optional[Path] = None
    # ----- Slice 1 (SkillRegistry-AutonomousReach) additive fields -----
    # Backward-compat: ``reach`` defaults to OPERATOR_PLUS_MODEL (the
    # CC-equivalent surface, matches every pre-arc manifest's implicit
    # behavior). ``trigger_specs`` defaults to empty tuple so existing
    # manifests are autonomous-inert. The Slice 3 observer will only
    # fire skills whose ``trigger_specs`` declare matching preconditions
    # AND whose ``reach`` includes AUTONOMOUS.
    reach: SkillReach = SkillReach.OPERATOR_PLUS_MODEL
    trigger_specs: Tuple[SkillTriggerSpec, ...] = ()
    schema_version: str = SKILL_MANIFEST_SCHEMA_VERSION

    # --- identity --------------------------------------------------------

    @property
    def qualified_name(self) -> str:
        """Canonical lookup name: ``plugin:skill`` if namespaced, else ``skill``."""
        if self.plugin_namespace:
            return f"{self.plugin_namespace}:{self.name}"
        return self.name

    # --- factories -------------------------------------------------------

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], *, source_path: Optional[Path] = None,
    ) -> "SkillManifest":
        """Build + validate from a dict. Raises :class:`SkillManifestError`."""
        if not isinstance(data, Mapping):
            raise SkillManifestError(
                f"manifest must be a mapping; got {type(data).__name__}"
            )

        def _req_str(key: str) -> str:
            val = data.get(key)
            if not isinstance(val, str) or not val.strip():
                raise SkillManifestError(
                    f"manifest: {key!r} must be a non-empty string"
                )
            return val.strip()

        def _opt_str(key: str, default: str = "") -> str:
            val = data.get(key, default)
            if val is None:
                return default
            if not isinstance(val, str):
                raise SkillManifestError(
                    f"manifest: {key!r} must be a string if present"
                )
            return val.strip()

        name = _req_str("name")
        if not _NAME_RX.match(name):
            raise SkillManifestError(
                f"manifest: name {name!r} must match {_NAME_RX.pattern}"
            )

        description = _req_str("description")
        if len(description) > 500:
            raise SkillManifestError(
                "manifest: description must be ≤ 500 chars"
            )
        trigger = _req_str("trigger")
        if len(trigger) > 2000:
            raise SkillManifestError(
                "manifest: trigger must be ≤ 2000 chars"
            )
        usage = _opt_str("usage")
        if len(usage) > 4000:
            raise SkillManifestError(
                "manifest: usage must be ≤ 4000 chars"
            )

        entrypoint = _req_str("entrypoint")
        _validate_entrypoint(entrypoint)

        plugin_namespace = _opt_str("plugin_namespace") or None
        if plugin_namespace is not None and not _NAME_RX.match(
            plugin_namespace,
        ):
            raise SkillManifestError(
                f"manifest: plugin_namespace {plugin_namespace!r} must "
                f"match {_NAME_RX.pattern}"
            )

        version = _opt_str("version", "0.0.0")
        if not _VERSION_RX.match(version):
            raise SkillManifestError(
                f"manifest: version {version!r} must be semver-lite "
                "(e.g. 1.2.3 or v0.1.0-alpha)"
            )
        author = _opt_str("author")
        if len(author) > 200:
            raise SkillManifestError(
                "manifest: author must be ≤ 200 chars"
            )

        # Permissions — allowlist
        raw_permissions = data.get("permissions", ())
        if raw_permissions is None:
            raw_permissions = ()
        if not isinstance(raw_permissions, (list, tuple)):
            raise SkillManifestError(
                "manifest: permissions must be a list of strings"
            )
        permissions: List[str] = []
        for p in raw_permissions:
            if not isinstance(p, str):
                raise SkillManifestError(
                    "manifest: every permission must be a string"
                )
            if p not in _ALLOWED_PERMISSIONS:
                raise SkillManifestError(
                    f"manifest: unknown permission {p!r} "
                    f"(allowed: {sorted(_ALLOWED_PERMISSIONS)})"
                )
            if p not in permissions:
                permissions.append(p)

        # args_schema
        args_schema = data.get("args_schema") or {}
        _validate_arg_schema(args_schema)

        # ----- Slice 1 (SkillRegistry-AutonomousReach) additive parse -----
        # Both fields are OPTIONAL. Unknown reach value or malformed
        # trigger_spec => loud SkillManifestError (re-raised from
        # SkillTriggerError so the existing dialect-error contract
        # holds). Missing fields => safe defaults (OPERATOR_PLUS_MODEL,
        # empty tuple) per backward-compat.
        try:
            if "reach" in data and data["reach"] is not None:
                reach = parse_reach(data["reach"])
            else:
                reach = SkillReach.OPERATOR_PLUS_MODEL
        except SkillTriggerError as exc:
            raise SkillManifestError(
                f"manifest: {exc}"
            ) from exc
        try:
            trigger_specs = parse_trigger_specs_list(
                data.get("trigger_specs"),
                path="trigger_specs",
            )
        except SkillTriggerError as exc:
            raise SkillManifestError(
                f"manifest: {exc}"
            ) from exc

        manifest = cls(
            name=name,
            description=description,
            trigger=trigger,
            entrypoint=entrypoint,
            plugin_namespace=plugin_namespace,
            usage=usage,
            args_schema=dict(args_schema),
            permissions=tuple(permissions),
            version=version,
            author=author,
            path=Path(source_path).resolve() if source_path else None,
            reach=reach,
            trigger_specs=trigger_specs,
        )
        logger.info(
            "[SkillManifest] loaded qualified=%s version=%s "
            "entrypoint=%s perms=%s path=%s",
            manifest.qualified_name, manifest.version,
            manifest.entrypoint, list(manifest.permissions),
            str(manifest.path) if manifest.path else "-",
        )
        return manifest

    @classmethod
    def from_yaml_file(cls, path: Path) -> "SkillManifest":
        """Load + validate a manifest from a YAML file on disk."""
        p = Path(path)
        if not p.exists():
            raise SkillManifestError(
                f"manifest: file does not exist: {p}"
            )
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SkillManifestError(
                "PyYAML required to load manifest files"
            ) from exc
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise SkillManifestError(
                f"manifest: could not parse {p}: {exc}"
            ) from exc
        if raw is None:
            raise SkillManifestError(
                f"manifest: {p} is empty"
            )
        return cls.from_mapping(raw, source_path=p)

    # --- projection for SSE / REPL --------------------------------------

    def project(self) -> Dict[str, Any]:
        """Bounded, JSON-safe projection.

        ``args_schema`` is echoed as a shallow dict; ``path`` is
        stringified. Deliberately excludes nothing — the manifest IS
        the public surface.
        """
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "plugin_namespace": self.plugin_namespace,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "trigger": self.trigger,
            "usage": self.usage,
            "entrypoint": self.entrypoint,
            "version": self.version,
            "author": self.author,
            "permissions": list(self.permissions),
            "args_schema": dict(self.args_schema),
            "path": str(self.path) if self.path else None,
            # Slice 1 additive fields -- always present in projection
            # so SSE / REPL clients see the full surface.
            "reach": self.reach.value,
            "trigger_specs": [
                spec.to_dict() for spec in self.trigger_specs
            ],
        }


# ---------------------------------------------------------------------------
# Arg validator — runtime check that caller-supplied args match schema
# ---------------------------------------------------------------------------


class SkillArgsError(ValueError):
    """Raised when supplied args don't match the manifest's schema."""


def validate_args(
    schema: Mapping[str, Any],
    args: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate + normalise ``args`` against ``schema``.

    Applies defaults for missing non-required args; rejects unknown
    arg names; checks basic types. Returns a new dict with normalised
    values — never mutates the caller's dict.

    Tight dialect by design: operators write simple shapes; complex
    JSON-schema features (oneOf / patternProperties / ...) are out of
    scope and should land as additive extensions, not hidden defaults.
    """
    if not isinstance(schema, Mapping):
        raise SkillArgsError("schema must be a mapping")
    if not isinstance(args, Mapping):
        raise SkillArgsError(
            f"args must be a mapping; got {type(args).__name__}"
        )
    out: Dict[str, Any] = {}
    unknown = set(args.keys()) - set(schema.keys())
    if unknown:
        raise SkillArgsError(
            f"unknown arg(s): {sorted(unknown)}"
        )
    for arg_name, spec in schema.items():
        required = bool(spec.get("required", False))
        present = arg_name in args
        if not present:
            if required:
                raise SkillArgsError(
                    f"missing required arg: {arg_name!r}"
                )
            if "default" in spec:
                out[arg_name] = spec["default"]
            continue
        value = args[arg_name]
        _check_arg_type(arg_name, spec, value)
        # enum check
        if "enum" in spec and value not in spec["enum"]:
            raise SkillArgsError(
                f"{arg_name!r} must be one of {spec['enum']!r}; "
                f"got {value!r}"
            )
        # bounds
        _check_arg_bounds(arg_name, spec, value)
        out[arg_name] = value
    return out


def _check_arg_type(
    name: str, spec: Mapping[str, Any], value: Any,
) -> None:
    expected = spec.get("type")
    type_map = {
        "string":  (str,),
        "integer": (int,),
        "number":  (int, float),
        "boolean": (bool,),
        "array":   (list, tuple),
        "object":  (dict,),
    }
    # bool is a subclass of int in Python — exclude it from 'integer'/'number'
    if expected in ("integer", "number") and isinstance(value, bool):
        raise SkillArgsError(
            f"{name!r} must be {expected}, got bool"
        )
    allowed = type_map.get(expected, ())
    if not isinstance(value, allowed):
        raise SkillArgsError(
            f"{name!r} must be {expected}, got {type(value).__name__}"
        )


def _check_arg_bounds(
    name: str, spec: Mapping[str, Any], value: Any,
) -> None:
    if "minimum" in spec and isinstance(value, (int, float)):
        if value < spec["minimum"]:
            raise SkillArgsError(
                f"{name!r} = {value} < minimum {spec['minimum']}"
            )
    if "maximum" in spec and isinstance(value, (int, float)):
        if value > spec["maximum"]:
            raise SkillArgsError(
                f"{name!r} = {value} > maximum {spec['maximum']}"
            )
    if "min_length" in spec and hasattr(value, "__len__"):
        if len(value) < spec["min_length"]:
            raise SkillArgsError(
                f"{name!r} length {len(value)} < min_length "
                f"{spec['min_length']}"
            )
    if "max_length" in spec and hasattr(value, "__len__"):
        if len(value) > spec["max_length"]:
            raise SkillArgsError(
                f"{name!r} length {len(value)} > max_length "
                f"{spec['max_length']}"
            )


__all__ = [
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "SkillArgsError",
    "SkillManifest",
    "SkillManifestError",
    # Slice 1 additive re-exports -- callers can import the new
    # vocabulary from skill_manifest without a separate import.
    "SkillReach",
    "SkillTriggerError",
    "SkillTriggerSpec",
    "validate_args",
]

_ = enum  # silence unused-import guard
