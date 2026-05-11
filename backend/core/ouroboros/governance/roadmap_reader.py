"""
RoadmapReader — Operator-Signed Roadmap → IntentEnvelope Pipeline
==================================================================

Closes §41.4 Phase 1 (PRD v3.0+) — the load-bearing substrate
that bridges from "operator types one task at a time" to
"operator signs a roadmap document; system pursues goals
autonomously". Per the PRD:

  "RoadmapReader substrate | Doesn't exist | ~1-2 weeks |
   Operator-signed .jarvis/roadmap.yaml → parse → emit
   IntentEnvelopes via UnifiedIntakeRouter"

This substrate is a **pure-function governance composer** that:

1. Reads an operator-signed roadmap document from
   ``.jarvis/roadmap.yaml`` (or env-override path).
2. Verifies the operator's HMAC-SHA256 signature using a secret
   supplied via ``JARVIS_ROADMAP_READER_HMAC_SECRET`` env var.
3. Parses the validated document into a tuple of frozen
   :class:`RoadmapGoal` artifacts.
4. Composes :func:`intake.intent_envelope.make_envelope` to
   construct one :class:`IntentEnvelope` per goal with
   ``source="roadmap"`` (already in the canonical
   ``_VALID_SOURCES``).
5. Submits each envelope via
   :meth:`intake.unified_intake_router.UnifiedIntakeRouter.ingest`
   so the goal flows through the canonical 16-sensor pipeline
   exactly like any other intent — UrgencyRouter classification,
   priority queue, dedup, WAL persistence, governance gates.
6. Appends an audit row to ``.jarvis/roadmap_reader_ledger.jsonl``
   for every emit decision (one row per goal regardless of
   ingest success/failure).

The substrate emits NO new authority surface — the roadmap is
just another intent SOURCE. All downstream governance (Iron
Gate, SemanticGuardian, risk_tier_floor, change_engine) applies
exactly as for any other op. The substrate's only authority is
"convert a signed YAML document into a tuple of envelopes the
canonical router accepts" — emphatically NOT "execute the
goals".

YAML format (PyYAML optional; JSON fallback):

  version: 1
  operator_id: "user@example.com"
  signed_at: "2026-05-11T12:00:00Z"
  signature: "<hmac-sha256-hex>"   # over canonical-serialized
                                    # goals payload
  goals:
    - id: "implement-feature-x"
      title: "Implement feature X"
      description: "Long-form description..."
      priority: high                # critical | high | medium | low
      target_files:
        - "backend/x.py"
        - "tests/test_x.py"
      success_criteria: "all tests pass; no regression in test_y"
      depends_on: []                # other goal ids (advisory)
      max_duration_s: 3600          # optional

Closed 4-value :class:`RoadmapVerdict`:

  NO_ROADMAP         file absent — substrate is no-op
  VALID              file present + signature valid + parseable
  INVALID_SIGNATURE  file present but HMAC mismatch (or missing
                     signature when REQUIRE_SIGNATURE=true)
  MALFORMED          file present but YAML/JSON parse failed
                     OR missing required fields

Closed 4-value :class:`GoalPriority`:

  CRITICAL           operator-flagged urgent
  HIGH               normal high-priority work
  MEDIUM             default
  LOW                background

§33.1 ``JARVIS_ROADMAP_READER_ENABLED`` default-FALSE.

Authority asymmetry (AST-pinned): stdlib + lazy-imported
``intake.intent_envelope`` + ``intake.unified_intake_router``
(only for ingest) + ``governance_boundary_gate`` +
``cross_process_jsonl``. Does NOT import orchestrator /
iron_gate / policy / providers / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor / tool_executor.
"""
from __future__ import annotations

import ast
import asyncio
import enum
import hashlib
import hmac
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


ROADMAP_READER_SCHEMA_VERSION: str = "roadmap_reader.1"


_ENV_MASTER = "JARVIS_ROADMAP_READER_ENABLED"
_ENV_PERSIST = "JARVIS_ROADMAP_READER_PERSIST_ENABLED"
_ENV_REQUIRE_SIG = "JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE"
_ENV_HMAC_SECRET = "JARVIS_ROADMAP_READER_HMAC_SECRET"
_ENV_ROADMAP_PATH = "JARVIS_ROADMAP_READER_PATH"
_ENV_MAX_GOALS = "JARVIS_ROADMAP_READER_MAX_GOALS"
_ENV_LEDGER_PATH = "JARVIS_ROADMAP_READER_LEDGER_PATH"
_ENV_DEFAULT_URGENCY = "JARVIS_ROADMAP_READER_DEFAULT_URGENCY"
_ENV_REPO_NAME = "JARVIS_ROADMAP_READER_REPO_NAME"

_DEFAULT_ROADMAP_REL = ".jarvis/roadmap.yaml"
_DEFAULT_LEDGER_REL = ".jarvis/roadmap_reader_ledger.jsonl"
_DEFAULT_MAX_GOALS = 100
_DEFAULT_URGENCY = "normal"
_DEFAULT_REPO_NAME = "jarvis"

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


def require_signature() -> bool:
    """Default TRUE — operator must explicitly opt-out for
    unsigned mode (dev workflow only)."""
    return _flag(_ENV_REQUIRE_SIG, default=True)


def roadmap_path() -> Path:
    raw = os.environ.get(_ENV_ROADMAP_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_ROADMAP_REL)


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


def hmac_secret() -> Optional[str]:
    """HMAC secret from env. None when unset — REQUIRE_SIGNATURE
    must also be false for unsigned-mode to be permitted."""
    raw = os.environ.get(_ENV_HMAC_SECRET, "")
    return raw if raw else None


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


def max_goals() -> int:
    return _read_clamped_int(
        _ENV_MAX_GOALS, _DEFAULT_MAX_GOALS, 1, 10_000,
    )


def default_urgency() -> str:
    """Default urgency for emitted envelopes. Operator can
    override via env; falls back to IntentEnvelope's
    canonical urgency taxonomy."""
    raw = os.environ.get(_ENV_DEFAULT_URGENCY, "").strip().lower()
    return raw if raw else _DEFAULT_URGENCY


def repo_name() -> str:
    raw = os.environ.get(_ENV_REPO_NAME, "").strip()
    return raw if raw else _DEFAULT_REPO_NAME


# Closed taxonomies


class RoadmapVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    NO_ROADMAP = "no_roadmap"
    VALID = "valid"
    INVALID_SIGNATURE = "invalid_signature"
    MALFORMED = "malformed"


class GoalPriority(str, enum.Enum):
    """Closed 4-value priority — bytes-pinned via AST."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_VERDICT_GLYPH: Dict[str, str] = {
    RoadmapVerdict.NO_ROADMAP.value: "◌",
    RoadmapVerdict.VALID.value: "✓",
    RoadmapVerdict.INVALID_SIGNATURE.value: "🔒",
    RoadmapVerdict.MALFORMED.value: "✗",
}


_PRIORITY_GLYPH: Dict[str, str] = {
    GoalPriority.CRITICAL.value: "🔥",
    GoalPriority.HIGH.value: "▲",
    GoalPriority.MEDIUM.value: "◊",
    GoalPriority.LOW.value: "▽",
}


# Priority → urgency mapping (default; operator may add per-
# goal urgency override in the future). Maps to the canonical
# UrgencyRouter taxonomy used by IntentEnvelope.
_PRIORITY_TO_URGENCY: Dict[str, str] = {
    GoalPriority.CRITICAL.value: "critical",
    GoalPriority.HIGH.value: "high",
    GoalPriority.MEDIUM.value: "normal",
    GoalPriority.LOW.value: "low",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def priority_glyph(priority: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(priority, "value"):
            return _PRIORITY_GLYPH.get(str(priority.value), "?")
        return _PRIORITY_GLYPH.get(
            str(priority or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def _coerce_priority(raw: Any) -> GoalPriority:
    """Best-effort coercion; unknown → MEDIUM. NEVER raises."""
    if isinstance(raw, GoalPriority):
        return raw
    try:
        s = str(getattr(raw, "value", raw) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return GoalPriority.MEDIUM
    for p in GoalPriority:
        if p.value == s:
            return p
    return GoalPriority.MEDIUM


# §33.5 frozen artifacts


@dataclass(frozen=True)
class RoadmapGoal:
    """One operator-authored goal."""

    goal_id: str
    title: str
    description: str
    priority: GoalPriority
    target_files: Tuple[str, ...]
    success_criteria: str
    depends_on: Tuple[str, ...]
    max_duration_s: int
    schema_version: str = ROADMAP_READER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id[:128],
            "title": self.title[:256],
            "description": self.description[:2048],
            "priority": self.priority.value,
            "target_files": list(self.target_files),
            "success_criteria": self.success_criteria[:512],
            "depends_on": list(self.depends_on),
            "max_duration_s": int(self.max_duration_s),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RoadmapDocument:
    """Parsed roadmap document. Frozen audit record."""

    version: int
    operator_id: str
    signed_at_iso: str
    signature_hex: str
    signature_valid: bool
    goals: Tuple[RoadmapGoal, ...]
    raw_bytes: int
    schema_version: str = ROADMAP_READER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": int(self.version),
            "operator_id": self.operator_id[:128],
            "signed_at_iso": self.signed_at_iso[:64],
            "signature_hex": self.signature_hex[:128],
            "signature_valid": bool(self.signature_valid),
            "goals": [g.to_dict() for g in self.goals],
            "raw_bytes": int(self.raw_bytes),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class GoalEmitOutcome:
    """One per-goal emit outcome — frozen audit record."""

    goal_id: str
    emitted: bool
    idempotency_key: str
    error: str  # empty when emitted=True
    schema_version: str = ROADMAP_READER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "emit",
            "goal_id": self.goal_id[:128],
            "emitted": bool(self.emitted),
            "idempotency_key": self.idempotency_key[:64],
            "error": self.error[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RoadmapReport:
    """Top-level report — frozen §33.5 artifact."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: RoadmapVerdict
    document: Optional[RoadmapDocument]
    emit_outcomes: Tuple[GoalEmitOutcome, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = ROADMAP_READER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "document": (
                self.document.to_dict() if self.document else None
            ),
            "emit_outcomes": [
                o.to_dict() for o in self.emit_outcomes
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# HMAC signing


def _canonical_serialize_for_signing(payload: Mapping[str, Any]) -> bytes:
    """Deterministic serialization for HMAC computation. NEVER
    raises. Sort keys, no whitespace, UTF-8 bytes."""
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except Exception:  # noqa: BLE001
        return b""


def compute_signature(
    payload: Mapping[str, Any], secret: str,
) -> str:
    """Compute HMAC-SHA256 over the canonical-serialized payload.
    Operator helper for generating signed roadmaps. NEVER raises."""
    try:
        body = _canonical_serialize_for_signing(payload)
        if not body or not secret:
            return ""
        return hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def verify_signature(
    payload: Mapping[str, Any],
    signature_hex: str,
    secret: str,
) -> bool:
    """Compute HMAC + constant-time compare with provided
    signature. NEVER raises."""
    try:
        expected = compute_signature(payload, secret)
        if not expected or not signature_hex:
            return False
        return hmac.compare_digest(expected, signature_hex.strip())
    except Exception:  # noqa: BLE001
        return False


# Parser


def _parse_document_text(raw: str) -> Optional[Dict[str, Any]]:
    """Parse YAML/JSON. NEVER raises. Returns None on failure."""
    if not raw:
        return None
    # Try yaml first; fall back to JSON.
    try:
        import yaml as _yaml  # type: ignore
        parsed = _yaml.safe_load(raw)
        if isinstance(parsed, dict):
            return parsed
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    return None


def _parse_goal_entry(entry: Any) -> Optional[RoadmapGoal]:
    """Parse one goal dict → RoadmapGoal. NEVER raises."""
    if not isinstance(entry, dict):
        return None
    try:
        goal_id = str(entry.get("id", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not goal_id or not title:
            return None
        description = str(entry.get("description", "") or "")
        priority = _coerce_priority(entry.get("priority", "medium"))
        target_files_raw = entry.get("target_files") or ()
        target_files = tuple(
            str(f).strip() for f in target_files_raw
            if isinstance(f, (str, bytes, int, float))
            and str(f).strip()
        )
        success = str(entry.get("success_criteria", "") or "")
        depends_raw = entry.get("depends_on") or ()
        depends_on = tuple(
            str(d).strip() for d in depends_raw
            if isinstance(d, str) and d.strip()
        )
        max_dur = entry.get("max_duration_s", 0)
        try:
            max_dur = int(max_dur or 0)
        except (TypeError, ValueError):
            max_dur = 0
        return RoadmapGoal(
            goal_id=goal_id,
            title=title,
            description=description,
            priority=priority,
            target_files=target_files,
            success_criteria=success,
            depends_on=depends_on,
            max_duration_s=max(0, max_dur),
        )
    except Exception:  # noqa: BLE001
        return None


def parse_roadmap(
    raw: str,
) -> Tuple[Optional[Dict[str, Any]], Tuple[RoadmapGoal, ...]]:
    """Parse raw text → (document_dict, goals_tuple). NEVER
    raises. Returns (None, ()) on parse failure or malformed
    structure."""
    doc = _parse_document_text(raw)
    if doc is None:
        return None, ()
    goals_raw = doc.get("goals")
    if not isinstance(goals_raw, list):
        return doc, ()
    cap = max_goals()
    goals: List[RoadmapGoal] = []
    for entry in goals_raw[:cap]:
        g = _parse_goal_entry(entry)
        if g is not None:
            goals.append(g)
    return doc, tuple(goals)


def _build_signing_payload(
    doc: Mapping[str, Any],
) -> Dict[str, Any]:
    """Canonical fields-to-sign — omits the ``signature`` field
    itself and any other operator-comment fields."""
    return {
        "version": doc.get("version"),
        "operator_id": doc.get("operator_id"),
        "signed_at": doc.get("signed_at"),
        "goals": doc.get("goals", []),
    }


# Composers


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


def _make_envelope_for_goal(
    goal: RoadmapGoal,
) -> Optional[Any]:
    """Compose intake.intent_envelope.make_envelope. NEVER
    raises. Returns None when envelope construction fails."""
    try:
        from backend.core.ouroboros.governance.intake.intent_envelope import (  # noqa: E501
            make_envelope,
        )
    except ImportError:
        return None
    try:
        urgency = _PRIORITY_TO_URGENCY.get(
            goal.priority.value, _DEFAULT_URGENCY,
        )
        env = make_envelope(
            source="roadmap",
            description=(
                f"{goal.title}\n\n{goal.description}"
            ),
            target_files=goal.target_files,
            repo=repo_name(),
            confidence=0.95,
            urgency=urgency,
            evidence={
                "goal_id": goal.goal_id,
                "success_criteria": goal.success_criteria[:512],
                "priority": goal.priority.value,
                "depends_on": list(goal.depends_on),
                "max_duration_s": goal.max_duration_s,
                "signature": goal.goal_id,  # dedup signature
            },
            requires_human_ack=False,
            signal_id=f"roadmap_goal_{goal.goal_id[:64]}",
        )
        return env
    except Exception:  # noqa: BLE001
        return None


# Top-level API


def read_roadmap(
    *,
    path_override: Optional[Path] = None,
    secret_override: Optional[str] = None,
    now_unix: Optional[float] = None,
) -> Tuple[RoadmapVerdict, Optional[RoadmapDocument], str]:
    """Pure read + verify. NEVER raises. Returns
    ``(verdict, document_or_None, diagnostic)``.

    Side-effect-free: no envelopes emitted, no ledger writes.
    Operator may call this to inspect the roadmap state without
    triggering autonomous work."""
    target = path_override or roadmap_path()
    try:
        if not target.exists() or not target.is_file():
            return (
                RoadmapVerdict.NO_ROADMAP,
                None,
                f"roadmap file absent at {target}",
            )
        raw = target.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return (
            RoadmapVerdict.MALFORMED,
            None,
            f"read failed: {exc!r}"[:200],
        )

    doc_dict, goals = parse_roadmap(raw)
    if doc_dict is None:
        return (
            RoadmapVerdict.MALFORMED,
            None,
            "parse failed — not valid YAML or JSON",
        )

    signature_hex = str(doc_dict.get("signature", "") or "")
    signing_payload = _build_signing_payload(doc_dict)

    secret = secret_override if secret_override is not None else hmac_secret()
    require = require_signature()

    sig_valid = False
    if signature_hex and secret:
        sig_valid = verify_signature(signing_payload, signature_hex, secret)

    if require and not sig_valid:
        # Either no signature, no secret, or HMAC mismatch.
        if not signature_hex:
            diagnostic = (
                "REQUIRE_SIGNATURE=true and roadmap has no "
                "'signature' field"
            )
        elif not secret:
            diagnostic = (
                "REQUIRE_SIGNATURE=true and "
                f"{_ENV_HMAC_SECRET} env var is unset"
            )
        else:
            diagnostic = "HMAC mismatch — signature invalid"
        document = RoadmapDocument(
            version=int(doc_dict.get("version", 0) or 0),
            operator_id=str(doc_dict.get("operator_id", "") or ""),
            signed_at_iso=str(doc_dict.get("signed_at", "") or ""),
            signature_hex=signature_hex,
            signature_valid=False,
            goals=goals,
            raw_bytes=len(raw),
        )
        return (
            RoadmapVerdict.INVALID_SIGNATURE,
            document,
            diagnostic,
        )

    document = RoadmapDocument(
        version=int(doc_dict.get("version", 0) or 0),
        operator_id=str(doc_dict.get("operator_id", "") or ""),
        signed_at_iso=str(doc_dict.get("signed_at", "") or ""),
        signature_hex=signature_hex,
        signature_valid=sig_valid,
        goals=goals,
        raw_bytes=len(raw),
    )
    diagnostic = (
        f"valid: {len(goals)} goal(s); "
        f"signature {'verified' if sig_valid else 'unverified-mode'}"
    )
    return RoadmapVerdict.VALID, document, diagnostic


async def emit_roadmap_envelopes(
    document: RoadmapDocument,
    *,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> Tuple[GoalEmitOutcome, ...]:
    """Emit one IntentEnvelope per goal via the router's
    canonical ``ingest`` method. NEVER raises.

    When ``router`` is None, builds envelopes but does NOT
    submit (useful for dry-run / testing). Returns the per-goal
    outcomes regardless of submit path.
    """
    outcomes: List[GoalEmitOutcome] = []
    if document is None or not document.goals:
        return ()
    for goal in document.goals:
        env = _make_envelope_for_goal(goal)
        if env is None:
            outcomes.append(GoalEmitOutcome(
                goal_id=goal.goal_id,
                emitted=False,
                idempotency_key="",
                error="envelope construction failed",
            ))
            continue
        if router is None:
            # Dry-run mode — envelope built successfully but
            # not submitted.
            outcomes.append(GoalEmitOutcome(
                goal_id=goal.goal_id,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error="router not provided (dry-run)",
            ))
            continue
        try:
            # Router.ingest is async; await it.
            result = await router.ingest(env)
            outcomes.append(GoalEmitOutcome(
                goal_id=goal.goal_id,
                emitted=True,
                idempotency_key=str(result or "")[:64],
                error="",
            ))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(GoalEmitOutcome(
                goal_id=goal.goal_id,
                emitted=False,
                idempotency_key=getattr(env, "idempotency_key", ""),
                error=f"ingest failed: {exc!r}"[:200],
            ))
    return tuple(outcomes)


async def process_roadmap(
    *,
    path_override: Optional[Path] = None,
    secret_override: Optional[str] = None,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> RoadmapReport:
    """Top-level: read + verify + emit. NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return RoadmapReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=RoadmapVerdict.NO_ROADMAP,
            document=None,
            emit_outcomes=(),
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )

    verdict, document, diagnostic = read_roadmap(
        path_override=path_override,
        secret_override=secret_override,
        now_unix=started,
    )

    outcomes: Tuple[GoalEmitOutcome, ...] = ()
    if verdict is RoadmapVerdict.VALID and document is not None:
        outcomes = await emit_roadmap_envelopes(
            document, router=router, now_unix=started,
        )

    report = RoadmapReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        document=document,
        emit_outcomes=outcomes,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def process_roadmap_sync(
    *,
    path_override: Optional[Path] = None,
    secret_override: Optional[str] = None,
    router: Any = None,
    now_unix: Optional[float] = None,
) -> RoadmapReport:
    """Sync wrapper. NEVER raises. Returns malformed report when
    called inside a running event loop (caller should use the
    async form)."""
    started = time.time() if now_unix is None else float(now_unix)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        return RoadmapReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=RoadmapVerdict.MALFORMED,
            document=None,
            emit_outcomes=(),
            diagnostic=(
                "sync wrapper invoked inside running event "
                "loop — use process_roadmap() instead"
            ),
            elapsed_s=0.0,
        )
    try:
        return asyncio.run(process_roadmap(
            path_override=path_override,
            secret_override=secret_override,
            router=router,
            now_unix=now_unix,
        ))
    except Exception as exc:  # noqa: BLE001
        return RoadmapReport(
            evaluated_at_unix=started,
            master_enabled=master_enabled(),
            verdict=RoadmapVerdict.MALFORMED,
            document=None,
            emit_outcomes=(),
            diagnostic=f"sync wrapper failed: {exc!r}"[:200],
            elapsed_s=0.0,
        )


def _persist_report(report: RoadmapReport) -> None:
    """§33.4 audit. NEVER raises. Persists VALID + outcome rows,
    INVALID_SIGNATURE + MALFORMED summaries. Skips NO_ROADMAP
    when master off (handled by _flock_append)."""
    if report.verdict is RoadmapVerdict.NO_ROADMAP:
        return
    _flock_append({"kind": "report", "payload": report.to_dict()})
    for outcome in report.emit_outcomes:
        _flock_append(outcome.to_dict())


def _publish_event(report: RoadmapReport) -> None:
    """Best-effort SSE. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict is RoadmapVerdict.NO_ROADMAP:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ROADMAP_PROCESSED,
            publish_task_event,
        )
        emitted_count = sum(
            1 for o in report.emit_outcomes if o.emitted
        )
        publish_task_event(
            EVENT_TYPE_ROADMAP_PROCESSED,
            (
                f"system::roadmap_reader::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "goal_count": (
                    len(report.document.goals)
                    if report.document else 0
                ),
                "emitted_count": emitted_count,
                "signature_valid": (
                    bool(report.document.signature_valid)
                    if report.document else False
                ),
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


# Renderer


def format_roadmap_panel(
    report: Optional[RoadmapReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"roadmap reader: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "roadmap reader: no report"
    if not report.master_enabled:
        return (
            f"roadmap reader: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    lines = [
        f"🗺  Roadmap Reader  {vg} {report.verdict.value}",
    ]
    if report.document is not None:
        d = report.document
        lines.extend([
            f"  operator_id        : {d.operator_id[:64]}",
            f"  signed_at          : {d.signed_at_iso[:32]}",
            f"  signature_valid    : {d.signature_valid}",
            f"  goal_count         : {len(d.goals)}",
        ])
        if d.goals:
            lines.append("  goals:")
            for g in d.goals[:5]:
                pg = priority_glyph(g.priority)
                lines.append(
                    f"    {pg} {g.goal_id[:32]:<32} "
                    f"({g.priority.value}) "
                    f"files={len(g.target_files)}"
                )
            if len(d.goals) > 5:
                lines.append(
                    f"    ... (+{len(d.goals) - 5} more)"
                )
    if report.emit_outcomes:
        emitted = sum(1 for o in report.emit_outcomes if o.emitted)
        lines.append(
            f"  emitted            : {emitted}"
            f"/{len(report.emit_outcomes)}"
        )
    lines.append(f"  diagnostic         : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/roadmap_reader.py"
    )

    _EXPECTED_VERDICTS = {
        "no_roadmap", "valid", "invalid_signature", "malformed",
    }
    _EXPECTED_PRIORITIES = {
        "critical", "high", "medium", "low",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RoadmapVerdict"
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
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"RoadmapVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"RoadmapVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("RoadmapVerdict class not found",)

    def _validate_priority_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "GoalPriority"
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
                missing = _EXPECTED_PRIORITIES - found
                extra = found - _EXPECTED_PRIORITIES
                if missing:
                    return (
                        f"GoalPriority missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"GoalPriority drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("GoalPriority class not found",)

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
        if "intent_envelope" not in source:
            violations.append(
                "must compose intake.intent_envelope "
                "(canonical envelope factory)",
            )
        if "make_envelope" not in source:
            violations.append(
                "must use make_envelope (no parallel "
                "envelope construction)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl "
                "(§33.4 ledger)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_reader_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RoadmapVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_reader_priority_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "GoalPriority 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_priority_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_reader_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — pure roadmap composer. "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / etc / tool_executor. Envelopes are "
                "emitted via the canonical UnifiedIntakeRouter "
                "ingest() — substrate has no opinion on goal "
                "execution."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_reader_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "roadmap_reader_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes intake.intent_envelope."
                "make_envelope (canonical envelope factory) + "
                "cross_process_jsonl (§33.4 ledger). No "
                "parallel envelope construction, no parallel "
                "JSONL."
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
        "backend/core/ouroboros/governance/roadmap_reader.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "RoadmapReader master. §33.1 default-FALSE. "
                "Closes §41.4 Phase 1 (PRD v3.0+) — "
                "operator-signed roadmap.yaml → IntentEnvelope "
                "pipeline. Bridges from 'one task at a time' "
                "to 'system pursues goals autonomously'. "
                "Envelopes flow through canonical "
                "UnifiedIntakeRouter — no parallel cage."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_REQUIRE_SIG,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Require HMAC-SHA256 signature on roadmap. "
                "Default TRUE — operator must explicitly "
                "opt-out for unsigned mode (dev workflow only)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_REQUIRE_SIG}=false",
        ),
        FlagSpec(
            name=_ENV_HMAC_SECRET,
            type=FlagType.STR,
            default="",
            description=(
                "Operator HMAC secret for signature "
                "verification. Env-only (never persisted). "
                "Empty + REQUIRE_SIGNATURE=true → "
                "INVALID_SIGNATURE verdict."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_HMAC_SECRET}=<secret-key>",
        ),
        FlagSpec(
            name=_ENV_ROADMAP_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator-tunable roadmap path. Default "
                ".jarvis/roadmap.yaml."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_ROADMAP_PATH}=/path/to/roadmap.yaml",
        ),
        FlagSpec(
            name=_ENV_MAX_GOALS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_GOALS,
            description=(
                "Max goals processed per roadmap. Default "
                "100. Clamped to [1, 10_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_GOALS}=500",
        ),
        FlagSpec(
            name=_ENV_DEFAULT_URGENCY,
            type=FlagType.STR,
            default=_DEFAULT_URGENCY,
            description=(
                "Default urgency for medium-priority goals. "
                "Must match canonical IntentEnvelope urgency "
                "taxonomy."
            ),
            category=Category.ROUTING,
            source_file=src,
            example=f"{_ENV_DEFAULT_URGENCY}=background",
        ),
        FlagSpec(
            name=_ENV_REPO_NAME,
            type=FlagType.STR,
            default=_DEFAULT_REPO_NAME,
            description=(
                "Repo name used in emitted envelopes. "
                "Default 'jarvis'."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_REPO_NAME}=jarvis-fork",
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
    "ROADMAP_READER_SCHEMA_VERSION",
    "RoadmapVerdict",
    "GoalPriority",
    "RoadmapGoal",
    "RoadmapDocument",
    "GoalEmitOutcome",
    "RoadmapReport",
    "master_enabled",
    "persistence_enabled",
    "require_signature",
    "roadmap_path",
    "ledger_path",
    "hmac_secret",
    "max_goals",
    "default_urgency",
    "repo_name",
    "verdict_glyph",
    "priority_glyph",
    "compute_signature",
    "verify_signature",
    "parse_roadmap",
    "read_roadmap",
    "emit_roadmap_envelopes",
    "process_roadmap",
    "process_roadmap_sync",
    "format_roadmap_panel",
    "register_shipped_invariants",
    "register_flags",
]
