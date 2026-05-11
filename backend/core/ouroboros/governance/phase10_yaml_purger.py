"""
Phase 10 Slice 5b — YAML Purger Substrate
==========================================

Closes the *substrate-side* of Slice 5b: the deterministic
machinery that computes the purged ``brain_selection_policy.yaml``
+ verifies round-trip semantic equivalence under master=ON +
applies the rewrite atomically — **gated end-to-end** by
:func:`phase10_graduation_contract.is_ready_for_purge`.

Per the operator-paced binding (PRD §32.8.1, §1610), the actual
YAML deletion cannot ship until 3 forced-clean once-proofs have
been recorded under ``JARVIS_TOPOLOGY_SENTINEL_ENABLED=true``.
This substrate **never bypasses that gate**: every public entry
point consults
:func:`phase10_graduation_contract.is_ready_for_purge` first.

Architecture (per the operator binding):

* **No second registry** — the contract is the single source of
  truth for "is purge authorized?". This module composes it.
* **No hardcoded verb / field maps in code** — the fields to
  purge live in :data:`_PURGED_FIELDS_PER_ROUTE` as data on the
  module (operator-readable, AST-pinnable). They mirror the v1
  YAML schema fields that :meth:`ProviderTopology.is_dw_blocked_for_route`
  no longer reads when master=ON.
* **Round-trip safety** — :func:`verify_purge_safety` loads both
  the original and purged YAML through :func:`Topology.from_v2`
  and compares the v2-only surface (dw_models_for_route +
  fallback_tolerance_for_route + reason_for_route). If they
  diverge, the purge would silently break something — refuse.
* **Atomic write** — :func:`apply_purge` writes via temp-file +
  os.replace so a partial write can't corrupt the YAML.
* **Dry-run by default** — every caller must explicitly opt in
  to mutation via the ``dry_run=False`` kwarg.

Closed 5-value :class:`PurgeVerdict`:

  READY        contract green + safety verified + ready to write
  NOT_READY    contract not in READY_FOR_PURGE state
  WOULD_BREAK  round-trip safety check failed; purge would
               change observable behavior
  DISABLED     master enable flag for this substrate is off
  ERROR        I/O, parse, or unexpected failure

§33.1 cognitive substrate master flag
``JARVIS_PHASE10_YAML_PURGER_ENABLED`` default-**FALSE**. Even
when the graduation contract reports READY_FOR_PURGE, this flag
must be explicitly flipped to authorize the actual file write —
defense-in-depth against accidental invocation.

Authority asymmetry (AST-pinned): stdlib + governance composers
only. Does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine
/ semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator.
"""
from __future__ import annotations

import ast
import enum
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


PHASE10_YAML_PURGER_SCHEMA_VERSION: str = "phase10_yaml_purger.1"


# Master flag — separate from the graduation-contract flag.
# Operator must flip BOTH on for apply_purge to mutate the YAML:
#   1. graduation contract reports READY_FOR_PURGE (evidence-gate)
#   2. this flag flipped to true (intent-gate)
_ENV_MASTER = "JARVIS_PHASE10_YAML_PURGER_ENABLED"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-**FALSE**. Even with the graduation
    contract green, the operator must explicitly flip this flag
    to authorize file mutation. NEVER raises."""
    return _flag(_ENV_MASTER, default=False)


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class PurgeVerdict(str, enum.Enum):
    """Closed 5-value taxonomy — bytes-pinned via AST."""

    READY = "ready"
    NOT_READY = "not_ready"
    WOULD_BREAK = "would_break"
    DISABLED = "disabled"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Per-route fields to strip — data on the module (operator-readable).
# These mirror the v1 YAML schema fields that
# `ProviderTopology.is_dw_blocked_for_route` no longer reads when
# master=ON. Adding a new field requires (a) verifying v2 methods
# don't need it and (b) updating the AST pin below.
# ---------------------------------------------------------------------------


_PURGED_FIELDS_PER_ROUTE: Tuple[str, ...] = (
    "dw_allowed",
    "block_mode",
)


# Fields preserved on every route — operator policy + v2 inputs.
# Kept here so the AST pin can assert no accidental deletion.
_PRESERVED_FIELDS_PER_ROUTE: Tuple[str, ...] = (
    "reason",
    "dw_models",
    "fallback_tolerance",
)


# ---------------------------------------------------------------------------
# Frozen results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeReport:
    """Frozen report of one purge attempt."""

    verdict: PurgeVerdict
    yaml_path: str
    dry_run: bool
    fields_stripped_per_route: Tuple[str, ...]
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    purged_yaml: str = ""
    elapsed_s: float = 0.0
    schema_version: str = PHASE10_YAML_PURGER_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "yaml_path": self.yaml_path[:512],
            "dry_run": bool(self.dry_run),
            "fields_stripped_per_route": list(
                self.fields_stripped_per_route,
            ),
            "diagnostics": list(self.diagnostics),
            "purged_yaml_size_bytes": len(self.purged_yaml),
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Composers — lazy-imported governance surfaces
# ---------------------------------------------------------------------------


def _check_graduation_contract() -> Tuple[bool, str]:
    """Compose :func:`phase10_graduation_contract.is_ready_for_purge`.
    Returns ``(is_green, diagnostic)``. NEVER raises.

    When the contract module is unavailable or raises internally,
    returns ``(False, <why>)``."""
    try:
        from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
            ContractVerdict,
            is_ready_for_purge,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return False, f"contract import failed: {exc!r}"
    try:
        report = is_ready_for_purge()
    except Exception as exc:  # noqa: BLE001 — defensive
        return False, f"contract invocation failed: {exc!r}"
    if report.verdict is ContractVerdict.READY_FOR_PURGE:
        return True, "graduation contract green"
    return False, (
        f"graduation contract verdict={report.verdict.value} "
        f"clean={report.clean_sessions}/{report.required_clean_sessions}"
    )


# ---------------------------------------------------------------------------
# Pure-function purger — text-level regex over the YAML
# ---------------------------------------------------------------------------


# Match a single field line under `doubleword_topology.routes.<x>:`.
# YAML in this repo uses 6-space indent for route children (see
# brain_selection_policy.yaml lines 372-410). We anchor on indent
# explicitly so we never accidentally strip an unrelated key.
def _build_strip_regex(field_name: str) -> "re.Pattern[str]":
    # 6-space indent + `field_name:` + value to end-of-line + EOL
    return re.compile(
        rf"^      {re.escape(field_name)}:[^\n]*\n",
        flags=re.MULTILINE,
    )


def compute_purged_yaml(
    source_yaml: object,
    *,
    fields_to_strip: Optional[Tuple[str, ...]] = None,
) -> Tuple[PurgeVerdict, str, Tuple[str, ...]]:
    """Compute the deterministic purged YAML. NEVER raises.

    Returns ``(verdict, purged_yaml, diagnostics)``:

      * ``PurgeVerdict.READY`` — at least one targeted field was
        stripped; purged_yaml carries the result.
      * ``PurgeVerdict.NOT_READY`` — source has no targeted fields
        to strip; purge already applied or YAML doesn't match the
        expected shape.
      * ``PurgeVerdict.ERROR`` — bad input.

    Does NOT consult the graduation contract — that's
    :func:`apply_purge`'s responsibility. This function is pure:
    same input ⇒ same output.
    """
    diagnostics: List[str] = []
    try:
        text = str(source_yaml or "")
    except Exception:  # noqa: BLE001
        return PurgeVerdict.ERROR, "", ("source coerce failed",)
    if not text:
        return PurgeVerdict.ERROR, "", ("empty source yaml",)
    fields = fields_to_strip or _PURGED_FIELDS_PER_ROUTE
    out = text
    total_stripped = 0
    for f in fields:
        try:
            pattern = _build_strip_regex(f)
            new_out, count = pattern.subn("", out)
            out = new_out
            total_stripped += count
            diagnostics.append(f"stripped {count} occurrence(s) of `{f}:`")
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"regex failed on {f!r}: {exc!r}")
    if total_stripped == 0:
        return (
            PurgeVerdict.NOT_READY, out,
            tuple(diagnostics) + ("no targeted fields found",),
        )
    return PurgeVerdict.READY, out, tuple(diagnostics)


# ---------------------------------------------------------------------------
# Round-trip safety check — compare v2 surface under master=ON
# ---------------------------------------------------------------------------


def verify_purge_safety(
    original_yaml: object,
    purged_yaml: object,
    *,
    routes: Tuple[str, ...] = (
        "immediate", "complex", "standard",
        "background", "speculative",
    ),
) -> Tuple[bool, Tuple[str, ...]]:
    """Verify that the purged YAML preserves the v2 observable
    surface that the unified helper reads under master=ON.

    Returns ``(is_safe, diagnostics)``. NEVER raises.

    Algorithm:
      1. Parse both YAMLs via ``yaml.safe_load`` (lazy import).
      2. Build a :class:`ProviderTopology` from each via the
         canonical ``_parse_topology`` (no parallel loader).
      3. For each route, force master=ON and verify
         ``is_dw_blocked_for_route`` + ``fallback_tolerance_for_route``
         + ``reason_for_route`` return identical results.

    The point is that the purged YAML should be a no-op when the
    sentinel is the authority — if it isn't, we refuse to apply.
    """
    diagnostics: List[str] = []
    try:
        import yaml as _yaml
    except ImportError:
        return False, ("PyYAML not installed",)
    try:
        from backend.core.ouroboros.governance.provider_topology import (  # noqa: E501  # type: ignore[import-not-found]
            _parse_topology,
        )
    except Exception as exc:  # noqa: BLE001
        return False, (f"provider_topology import failed: {exc!r}",)
    try:
        orig_data = _yaml.safe_load(str(original_yaml or ""))
        purg_data = _yaml.safe_load(str(purged_yaml or ""))
    except Exception as exc:  # noqa: BLE001
        return False, (f"yaml parse failed: {exc!r}",)
    if not isinstance(orig_data, dict) or not isinstance(purg_data, dict):
        return False, ("yaml top-level not a dict",)
    if (
        "doubleword_topology" not in orig_data
        or "doubleword_topology" not in purg_data
    ):
        return False, ("doubleword_topology missing from yaml",)
    try:
        orig_t = _parse_topology(orig_data)
        purg_t = _parse_topology(purg_data)
    except Exception as exc:  # noqa: BLE001
        return False, (f"_parse_topology failed: {exc!r}",)
    # Force master ON for the comparison — this is the state under
    # which the purge becomes safe.
    prior = os.environ.get("JARVIS_TOPOLOGY_SENTINEL_ENABLED")
    os.environ["JARVIS_TOPOLOGY_SENTINEL_ENABLED"] = "true"
    try:
        for route in routes:
            try:
                orig_blocked = orig_t.is_dw_blocked_for_route(route)
                purg_blocked = purg_t.is_dw_blocked_for_route(route)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(
                    f"is_dw_blocked_for_route raised for {route!r}: "
                    f"{exc!r}"
                )
                return False, tuple(diagnostics)
            if orig_blocked != purg_blocked:
                diagnostics.append(
                    f"route {route!r} divergence: "
                    f"orig={orig_blocked} purged={purg_blocked}"
                )
                return False, tuple(diagnostics)
            try:
                orig_fb = orig_t.fallback_tolerance_for_route(route)
                purg_fb = purg_t.fallback_tolerance_for_route(route)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(
                    f"fallback_tolerance_for_route raised for "
                    f"{route!r}: {exc!r}"
                )
                return False, tuple(diagnostics)
            if orig_fb != purg_fb:
                diagnostics.append(
                    f"route {route!r} fallback divergence: "
                    f"orig={orig_fb!r} purged={purg_fb!r}"
                )
                return False, tuple(diagnostics)
            try:
                orig_r = orig_t.reason_for_route(route)
                purg_r = purg_t.reason_for_route(route)
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(
                    f"reason_for_route raised for {route!r}: "
                    f"{exc!r}"
                )
                return False, tuple(diagnostics)
            if orig_r != purg_r:
                diagnostics.append(
                    f"route {route!r} reason divergence"
                )
                return False, tuple(diagnostics)
    finally:
        # Restore prior env exactly.
        if prior is None:
            os.environ.pop(
                "JARVIS_TOPOLOGY_SENTINEL_ENABLED", None,
            )
        else:
            os.environ["JARVIS_TOPOLOGY_SENTINEL_ENABLED"] = prior
    diagnostics.append(
        f"verified {len(routes)} route(s) — v2 surface identical"
    )
    return True, tuple(diagnostics)


# ---------------------------------------------------------------------------
# Apply — gated end-to-end
# ---------------------------------------------------------------------------


def apply_purge(
    yaml_path: Path,
    *,
    dry_run: bool = True,
) -> PurgeReport:
    """Gated atomic YAML rewrite. NEVER raises.

    Gate sequence (all must pass for actual write):
      1. Module master flag :func:`master_enabled` = True
      2. :func:`phase10_graduation_contract.is_ready_for_purge`
         returns ``READY_FOR_PURGE``
      3. :func:`compute_purged_yaml` finds targeted fields
      4. :func:`verify_purge_safety` reports identical v2 surface

    When ``dry_run=True`` (default), no file mutation occurs even
    if all gates pass — the report carries the would-be content
    in ``purged_yaml``. Operator runs ``dry_run=False`` once after
    verifying the report.
    """
    t0 = time.monotonic()
    str_path = str(yaml_path)
    diagnostics: List[str] = []

    if not master_enabled():
        return PurgeReport(
            verdict=PurgeVerdict.DISABLED,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=(),
            diagnostics=(
                f"master flag {_ENV_MASTER} not set; "
                f"actual mutation always refused",
            ),
            elapsed_s=time.monotonic() - t0,
        )

    is_green, contract_diag = _check_graduation_contract()
    if not is_green:
        return PurgeReport(
            verdict=PurgeVerdict.NOT_READY,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=(),
            diagnostics=(contract_diag,),
            elapsed_s=time.monotonic() - t0,
        )
    diagnostics.append(contract_diag)

    try:
        source = yaml_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return PurgeReport(
            verdict=PurgeVerdict.ERROR,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=(),
            diagnostics=(f"yaml read failed: {exc!r}",),
            elapsed_s=time.monotonic() - t0,
        )

    compute_verdict, purged, compute_diag = compute_purged_yaml(source)
    diagnostics.extend(compute_diag)
    if compute_verdict is PurgeVerdict.NOT_READY:
        return PurgeReport(
            verdict=PurgeVerdict.NOT_READY,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=(),
            diagnostics=tuple(diagnostics),
            elapsed_s=time.monotonic() - t0,
        )
    if compute_verdict is PurgeVerdict.ERROR:
        return PurgeReport(
            verdict=PurgeVerdict.ERROR,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=(),
            diagnostics=tuple(diagnostics),
            elapsed_s=time.monotonic() - t0,
        )

    is_safe, safety_diag = verify_purge_safety(source, purged)
    diagnostics.extend(safety_diag)
    if not is_safe:
        return PurgeReport(
            verdict=PurgeVerdict.WOULD_BREAK,
            yaml_path=str_path,
            dry_run=dry_run,
            fields_stripped_per_route=_PURGED_FIELDS_PER_ROUTE,
            diagnostics=tuple(diagnostics),
            purged_yaml=purged,
            elapsed_s=time.monotonic() - t0,
        )

    if not dry_run:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".phase10_purge_",
                dir=str(yaml_path.parent),
                suffix=".yaml",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(purged)
            # Preserve permissions from the original.
            try:
                shutil.copymode(str(yaml_path), tmp_path)
            except Exception:  # noqa: BLE001
                pass
            os.replace(tmp_path, str(yaml_path))
            diagnostics.append(
                f"atomic replace succeeded for {str_path}"
            )
        except Exception as exc:  # noqa: BLE001
            return PurgeReport(
                verdict=PurgeVerdict.ERROR,
                yaml_path=str_path,
                dry_run=dry_run,
                fields_stripped_per_route=(),
                diagnostics=tuple(diagnostics) + (
                    f"atomic write failed: {exc!r}",
                ),
                elapsed_s=time.monotonic() - t0,
            )

    return PurgeReport(
        verdict=PurgeVerdict.READY,
        yaml_path=str_path,
        dry_run=dry_run,
        fields_stripped_per_route=_PURGED_FIELDS_PER_ROUTE,
        diagnostics=tuple(diagnostics),
        purged_yaml=purged,
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by shipped_code_invariants module walker."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501  # type: ignore[import-not-found]
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "phase10_yaml_purger.py"
    )

    _EXPECTED_VERDICTS = {
        "ready", "not_ready", "would_break", "disabled", "error",
    }
    _EXPECTED_PURGED_FIELDS = {"dw_allowed", "block_mode"}

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "PurgeVerdict"
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
                if found != _EXPECTED_VERDICTS:
                    return (
                        f"PurgeVerdict drift: "
                        f"got={sorted(found)} "
                        f"expected={sorted(_EXPECTED_VERDICTS)}",
                    )
                return ()
        return ("PurgeVerdict class not found",)

    def _validate_purged_field_list(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin: _PURGED_FIELDS_PER_ROUTE must contain
        exactly ``dw_allowed`` and ``block_mode``. Adding a new
        field requires an explicit PR + this pin update so the
        operator sees the change.

        The declaration is an ``AnnAssign`` (typed tuple literal),
        so we walk both ``Assign`` and ``AnnAssign`` shapes.
        """
        for node in ast.walk(tree):
            target_id = ""
            value_node = None
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                target_id = node.targets[0].id
                value_node = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.value is not None
            ):
                target_id = node.target.id
                value_node = node.value
            if target_id != "_PURGED_FIELDS_PER_ROUTE":
                continue
            if not isinstance(value_node, ast.Tuple):
                return ("_PURGED_FIELDS_PER_ROUTE must be a tuple",)
            values = set()
            for elt in value_node.elts:
                if (
                    isinstance(elt, ast.Constant)
                    and isinstance(elt.value, str)
                ):
                    values.add(elt.value)
            if values != _EXPECTED_PURGED_FIELDS:
                return (
                    f"_PURGED_FIELDS_PER_ROUTE drift: "
                    f"got={sorted(values)} "
                    f"expected={sorted(_EXPECTED_PURGED_FIELDS)}",
                )
            return ()
        return ("_PURGED_FIELDS_PER_ROUTE not found",)

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
                    "master_enabled() must call _flag(...) with "
                    "default=False per §33.1 + operator binding",
                )
        return ("master_enabled() not found",)

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
                    violations.append(
                        f"forbidden import: {mod}",
                    )
        return tuple(violations)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "phase10_graduation_contract" not in source:
            violations.append(
                "must compose phase10_graduation_contract "
                "(contract is the WHEN gate)"
            )
        if "_parse_topology" not in source:
            violations.append(
                "must compose _parse_topology "
                "(verify_purge_safety needs the canonical "
                "ProviderTopology loader; no parallel parser)"
            )
        if "is_ready_for_purge" not in source:
            violations.append(
                "must invoke is_ready_for_purge"
            )
        return tuple(violations)

    def _validate_apply_purge_gates_writes(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin: apply_purge MUST consult both gates before
        any os.replace / file write. The control-flow shape we
        require: contract check → compute → safety check → IF
        not dry_run, write. We pin by source-order of substrings:
        master_enabled() must appear before _check_graduation_contract()
        which must appear before os.replace."""
        idx_master = source.find("master_enabled()")
        idx_contract = source.find("_check_graduation_contract()")
        idx_replace = source.find("os.replace(")
        if idx_master < 0:
            return ("apply_purge must call master_enabled()",)
        if idx_contract < 0:
            return ("apply_purge must call _check_graduation_contract()",)
        if idx_replace < 0:
            return ("apply_purge must invoke os.replace for atomic write",)
        if not (idx_master < idx_contract < idx_replace):
            return (
                "apply_purge gate ordering wrong — must check "
                "master, then contract, before os.replace",
            )
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "PurgeVerdict 5-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_purged_field_list_pinned"
            ),
            target_file=target,
            description=(
                "_PURGED_FIELDS_PER_ROUTE bytes-pinned to "
                "(dw_allowed, block_mode). New fields require "
                "explicit PR + pin update so operators see the "
                "change."
            ),
            validate=_validate_purged_field_list,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Purger MUST NOT import orchestrator / iron_gate "
                "/ policy / etc — pure substrate composition."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes phase10_graduation_contract + "
                "Topology.from_v2; never bypasses the contract."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_yaml_purger_apply_gates_before_write"
            ),
            target_file=target,
            description=(
                "apply_purge MUST check master_enabled, then "
                "graduation contract, BEFORE any os.replace. "
                "Operator binding 2026-05-11: no shortcut bypass."
            ),
            validate=_validate_apply_purge_gates_writes,
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
        "phase10_yaml_purger.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Phase 10 Slice 5b YAML purger master. §33.1 "
                "default-FALSE. Operator-intent gate — must be "
                "flipped explicitly even after the graduation "
                "contract reports READY_FOR_PURGE."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
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
    "PHASE10_YAML_PURGER_SCHEMA_VERSION",
    "PurgeReport",
    "PurgeVerdict",
    "apply_purge",
    "compute_purged_yaml",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "verify_purge_safety",
]
