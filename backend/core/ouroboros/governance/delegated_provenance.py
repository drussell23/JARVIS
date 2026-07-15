"""Slice 20 — Delegated Provenance: cryptographic operator intent → the cage.

The first armed perception soak (``bt-2026-07-15-063421``) proved the gap: 70
ops blocked ``self_modification_unsanctioned_source`` because trust was keyed
on the *source label* — a string any sensor self-asserts, carrying no proof a
directive was authored by the human operator rather than injected/hallucinated
by O+V in a prior cycle. A blanket ``roadmap →  sanctioned`` whitelist would
authorize the forgeries as readily as the genuine article.

This module closes the gap by CONNECTING two things that already exist:

  * ``roadmap_reader`` (Slice ~200) — the operator-signed ``.jarvis/roadmap.yaml``
    with HMAC-SHA256 over a canonical payload, verified via
    ``hmac.compare_digest`` (timing-safe), secret in
    ``JARVIS_ROADMAP_READER_HMAC_SECRET`` — signed by the OPERATOR via the
    ``strategy_signer`` CLI (never autonomously).
  * ``risk_engine._self_protection_verdict`` — the Iron-Gate-adjacent
    self-protection floor that grades governance self-modification.

A sensor may attach a provenance CLAIM to its signal evidence — but a claim is
only a POINTER (``{kind, goal_id}``). Nothing in it is trusted. At classify
time :func:`verify_provenance_claim` re-derives EVERYTHING from ground truth:

  1. re-reads ``.jarvis/roadmap.yaml`` from disk (mtime-cached),
  2. re-verifies the operator HMAC (``signature_valid`` MUST be True — the
     reader's unsigned dev-mode is NOT accepted for delegation),
  3. requires the claimed goal to EXIST in the verified document
     (a hallucinated goal has no entry → fail),
  4. requires the signature to be fresh (``JARVIS_PROVENANCE_MAX_AGE_S``),
  5. requires every file the op touches to be INSIDE the signed goal's
     declared ``target_files`` scope (kills scope-laundering: a signed goal
     naming ``README.md`` can never authorize editing ``risk_engine.py``).

Verification outcome NEVER exceeds ``APPROVAL_REQUIRED`` — the branch in
``_self_protection_verdict`` sits AFTER the unconditional kernel/security
blocks and returns the same human-gated tier as the ``architecture`` path.
Auto-apply on a governance surface is structurally unreachable, token or not.

Fail-closed contract (mandate 4): master flag off, malformed claim, missing
roadmap, invalid/expired signature, unknown goal, out-of-scope target, or ANY
internal fault → ``ProvenanceVerdict(valid=False)`` → the caller falls through
to the exact pre-Slice-20 block. This module NEVER raises.

Honest residual risk (documented, not hidden): HMAC is symmetric — the
verifying process holds the same secret that mints. If generated code exfiltrated
``JARVIS_ROADMAP_READER_HMAC_SECRET`` it could forge a roadmap. Mitigations:
(a) the ceiling — a forged token still lands at APPROVAL_REQUIRED, a human
sees the diff before anything applies; (b) the secret is operator-installed
env state, never persisted by the organism (strategy_signer contract);
(c) follow-up slice: scrub the secret from Venom bash child environments.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.DelegatedProvenance")

DELEGATED_PROVENANCE_SCHEMA_VERSION: str = "delegated_provenance.v1"
CLAIM_KIND_ROADMAP_READER: str = "roadmap_reader"

# ── Env knobs (all adaptive; no hardcoded authority) ─────────────────────────
ENV_ENABLED = "JARVIS_DELEGATED_PROVENANCE_ENABLED"
ENV_SOURCES = "JARVIS_DELEGATED_PROVENANCE_SOURCES"
ENV_MAX_AGE_S = "JARVIS_PROVENANCE_MAX_AGE_S"

_TRUE = {"1", "true", "yes", "on"}
_DEFAULT_SOURCES = "roadmap"
_DEFAULT_MAX_AGE_S = 30 * 24 * 3600.0  # 30 days


def delegated_provenance_enabled() -> bool:
    """Master flag — default FALSE (§33.1 shadow-first). NEVER raises."""
    try:
        return os.environ.get(ENV_ENABLED, "false").strip().lower() in _TRUE
    except Exception:  # noqa: BLE001
        return False


def _permitted_sources() -> Tuple[str, ...]:
    """The Delegated Authority Matrix's sensor axis: which ``profile.source``
    values may PRESENT a provenance token. Presenting ≠ being believed — the
    verifier is the gate. Default: ``roadmap`` only. NEVER raises."""
    try:
        raw = os.environ.get(ENV_SOURCES, "").strip() or _DEFAULT_SOURCES
        return tuple(
            s.strip().lower() for s in raw.split(",") if s.strip()
        )
    except Exception:  # noqa: BLE001
        return (_DEFAULT_SOURCES,)


def _max_age_s() -> float:
    """Signature freshness window. ``<=0`` disables expiry (operator's
    explicit choice); default 30 days. NEVER raises."""
    try:
        raw = os.environ.get(ENV_MAX_AGE_S, "").strip()
        return float(raw) if raw else _DEFAULT_MAX_AGE_S
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE_S


# ── Verdict ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProvenanceVerdict:
    """Outcome of one ground-truth verification. Frozen audit record."""

    valid: bool
    reason: str
    goal_id: str = ""
    signer: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": DELEGATED_PROVENANCE_SCHEMA_VERSION,
            "valid": bool(self.valid),
            "reason": self.reason[:120],
            "goal_id": self.goal_id[:128],
            "signer": self.signer[:128],
        }


def _refuse(reason: str, goal_id: str = "") -> ProvenanceVerdict:
    logger.info(
        "[DelegatedProvenance] REFUSED reason=%s goal_id=%s",
        reason, goal_id or "<none>",
    )
    return ProvenanceVerdict(valid=False, reason=reason, goal_id=goal_id)


# ── Verified-roadmap cache (keeps classify sub-ms) ──────────────────────────
#
# read_roadmap() re-reads + re-verifies from disk. The roadmap is small and
# classify can be hot, so cache the (verdict, document) keyed on the file's
# (mtime_ns, size). Any change — including deletion — invalidates. The HMAC
# secret is read inside read_roadmap at call time, so a secret rotation takes
# effect on the next mtime change or cache reset (operator rotating the secret
# re-signs the file, which bumps mtime — the natural workflow).

_cache_lock = threading.Lock()
_roadmap_cache: Dict[str, Tuple[int, int, Any, Any]] = {}


def reset_provenance_cache_for_tests() -> None:
    """Drop the verified-roadmap cache. NEVER raises."""
    try:
        with _cache_lock:
            _roadmap_cache.clear()
    except Exception:  # noqa: BLE001
        pass


def _verified_roadmap() -> Tuple[Any, Any]:
    """(verdict, document) from the live signed roadmap, mtime-cached.
    NEVER raises — any fault returns ``(None, None)`` (→ fail-closed)."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            read_roadmap, roadmap_path,
        )
        path = roadmap_path()
        try:
            st = path.stat()
            key = str(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None, None  # no roadmap on disk
        with _cache_lock:
            hit = _roadmap_cache.get(key)
            if hit is not None and (hit[0], hit[1]) == stamp:
                return hit[2], hit[3]
        verdict, doc, _diag = read_roadmap()
        with _cache_lock:
            _roadmap_cache[key] = (stamp[0], stamp[1], verdict, doc)
        return verdict, doc
    except Exception:  # noqa: BLE001
        logger.debug("[DelegatedProvenance] roadmap read degraded", exc_info=True)
        return None, None


# ── Scope binding ────────────────────────────────────────────────────────────


def _norm(p: str) -> str:
    """Normalize to comparable posix form. A path containing ``..`` refuses
    to normalize (returns a sentinel that can never match) — parent-escapes
    must not be able to alias their way into a signed goal's scope; the
    pre-GATE write-escape clamp rejects them anyway, this keeps the scope
    check independently airtight."""
    s = str(p).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    if s.endswith("/"):
        s = s[:-1]
    if ".." in s.split("/"):
        return "\x00unnormalizable\x00"
    return s


def _file_in_goal_scope(file_str: str, goal_targets: Sequence[str]) -> bool:
    """True iff *file_str* is inside the signed goal's declared targets:
    exact match, path-suffix match (absolute op path vs repo-relative goal
    target), or under a goal target declared as a directory."""
    f = _norm(file_str)
    for t in goal_targets:
        tn = _norm(t)
        if not tn:
            continue
        if f == tn or f.endswith("/" + tn):
            return True
        # A goal target may declare a directory scope.
        if f.startswith(tn + "/") or ("/" + tn + "/") in ("/" + f):
            return True
    return False


def _signed_at_fresh(signed_at_iso: str) -> bool:
    """Freshness per ``JARVIS_PROVENANCE_MAX_AGE_S``. Unparseable → stale
    (fail-closed). NEVER raises."""
    ttl = _max_age_s()
    if ttl <= 0:
        return True  # operator explicitly disabled expiry
    try:
        raw = (signed_at_iso or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = time.time() - dt.timestamp()
        return 0 <= age <= ttl or (-300 <= age < 0)  # small clock-skew grace
    except Exception:  # noqa: BLE001
        return False


# ── The verifier (called by risk_engine at classify) ────────────────────────


def verify_provenance_claim(
    claim: Any,
    *,
    source: str,
    file_strs: Sequence[str],
) -> ProvenanceVerdict:
    """Ground-truth verification of a provenance CLAIM. The claim contributes
    ONLY a lookup key (``goal_id``); every authority-bearing fact is re-derived
    live: file → parse → HMAC (``compare_digest`` inside the reader) →
    goal existence → freshness → per-file scope. NEVER raises."""
    try:
        if not delegated_provenance_enabled():
            return _refuse("master_disabled")
        if not isinstance(claim, Mapping):
            return _refuse("malformed_claim")
        if str(claim.get("kind", "")).strip() != CLAIM_KIND_ROADMAP_READER:
            return _refuse("unknown_claim_kind")
        goal_id = str(claim.get("goal_id", "")).strip()
        if not goal_id:
            return _refuse("malformed_claim")
        src = (source or "").strip().lower()
        if src not in _permitted_sources():
            return _refuse("source_not_delegated", goal_id)
        if not file_strs:
            return _refuse("no_files", goal_id)

        verdict, doc = _verified_roadmap()
        if verdict is None or doc is None:
            return _refuse("roadmap_unavailable", goal_id)
        # Demand the cryptographic property itself, NOT merely the reader's
        # verdict — the reader permits an unsigned dev-mode
        # (REQUIRE_SIGNATURE=false) that must never confer delegation.
        if str(getattr(verdict, "value", verdict)) != "valid":
            return _refuse("roadmap_unverified", goal_id)
        if not bool(getattr(doc, "signature_valid", False)):
            return _refuse("signature_invalid", goal_id)
        if not _signed_at_fresh(str(getattr(doc, "signed_at_iso", ""))):
            return _refuse("signature_expired", goal_id)

        goal = None
        for g in getattr(doc, "goals", ()) or ():
            if str(getattr(g, "goal_id", "")).strip() == goal_id:
                goal = g
                break
        if goal is None:
            # The hallucination kill-switch: a directive O+V invented has no
            # entry inside the operator-signed document.
            return _refuse("goal_not_found", goal_id)

        targets = tuple(getattr(goal, "target_files", ()) or ())
        if not targets:
            # A goal with no declared scope cannot delegate authority over
            # ANY file — scope is the contract.
            return _refuse("goal_has_no_scope", goal_id)
        for f in file_strs:
            if not _file_in_goal_scope(str(f), targets):
                return _refuse("target_out_of_scope", goal_id)

        signer = str(getattr(doc, "operator_id", "") or "")
        logger.info(
            "[DelegatedProvenance] VERIFIED goal_id=%s signer=%s files=%d "
            "→ ceiling APPROVAL_REQUIRED (never auto-apply)",
            goal_id, signer or "<unset>", len(file_strs),
        )
        return ProvenanceVerdict(
            valid=True, reason="verified", goal_id=goal_id, signer=signer,
        )
    except Exception:  # noqa: BLE001 — any fault is a refusal, never a raise
        logger.debug(
            "[DelegatedProvenance] verify degraded → refuse", exc_info=True,
        )
        return ProvenanceVerdict(valid=False, reason="verifier_fault")


# ── Claim minting helpers (sensor side — pointers only, zero authority) ─────


def claim_for_goal(goal_id: str) -> Optional[Dict[str, Any]]:
    """A claim POINTER for a roadmap goal (used by roadmap_reader's envelope
    composer). Carries no authority — verification re-derives everything.
    None when the feature is off or goal_id is empty. NEVER raises."""
    try:
        if not delegated_provenance_enabled():
            return None
        gid = str(goal_id or "").strip()
        if not gid:
            return None
        return {
            "schema_version": DELEGATED_PROVENANCE_SCHEMA_VERSION,
            "kind": CLAIM_KIND_ROADMAP_READER,
            "goal_id": gid,
        }
    except Exception:  # noqa: BLE001
        return None


def claim_for_targets(
    target_files: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """Cross-reference a work item's resolved targets against the VERIFIED
    signed roadmap: if one signed goal's scope covers EVERY target, return a
    claim pointer at that goal (used by ``WorkOrderSensor`` so a progress.md
    item that mirrors a signed goal inherits its provenance). The claim is
    still fully re-verified at classify. None when unmatched/off. NEVER
    raises."""
    try:
        if not delegated_provenance_enabled() or not target_files:
            return None
        verdict, doc = _verified_roadmap()
        if doc is None or not bool(getattr(doc, "signature_valid", False)):
            return None
        if str(getattr(verdict, "value", verdict)) != "valid":
            return None
        for g in getattr(doc, "goals", ()) or ():
            targets = tuple(getattr(g, "target_files", ()) or ())
            if not targets:
                continue
            if all(
                _file_in_goal_scope(str(f), targets) for f in target_files
            ):
                return claim_for_goal(str(getattr(g, "goal_id", "")))
        return None
    except Exception:  # noqa: BLE001
        logger.debug(
            "[DelegatedProvenance] claim_for_targets degraded", exc_info=True,
        )
        return None


# ── Profile threading helper (orchestrator side) ────────────────────────────


def extract_claim_from_evidence_json(
    intake_evidence_json: str,
) -> Optional[Dict[str, Any]]:
    """Pull the ``provenance`` claim out of ``ctx.intake_evidence_json`` (the
    existing evidence pipe). None on absence/malformation/feature-off. NEVER
    raises."""
    try:
        if not delegated_provenance_enabled():
            return None
        raw = (intake_evidence_json or "").strip()
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        claim = data.get("provenance")
        return claim if isinstance(claim, dict) else None
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "CLAIM_KIND_ROADMAP_READER",
    "DELEGATED_PROVENANCE_SCHEMA_VERSION",
    "ENV_ENABLED",
    "ENV_MAX_AGE_S",
    "ENV_SOURCES",
    "ProvenanceVerdict",
    "claim_for_goal",
    "claim_for_targets",
    "delegated_provenance_enabled",
    "extract_claim_from_evidence_json",
    "reset_provenance_cache_for_tests",
    "verify_provenance_claim",
]
