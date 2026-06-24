"""Biometric Edge-Gate -- the operator write-path into governance.

`POST /authorize-elevation` is the FIRST operator write-path into the
Ouroboros governance loop. It authorizes the *operator-approval step* of
a CRITICAL_ELEVATION cross-repo PR via an ECAPA-TDNN voice biometric.

THE INVIOLABLE LAW
==================
The biometric is **NECESSARY, never SUFFICIENT**. A valid voice match
does NOT bypass any backend law. The existing CRITICAL_ELEVATION approval
path + the Immutable Orange floor (``frozenset({prime, reactor})``)
compose UNDERNEATH -- a valid biometric on a ``prime`` / ``reactor``
(Mind / Nerves) PR STILL cannot merge it. This middleware AUTHORIZES the
operator-approval step; it NEVER overrides the quarantine.

FAIL-CLOSED ABSOLUTE
====================
Every step rejects on uncertainty. Any exception anywhere in the
freshness / biometric / Immutable-Orange / approval chain -> REJECTED.
There is no code path that AUTHORIZES on an error.

Reuse, not reimplementation
===========================
The ECAPA verification, anti-spoof, and liveness all live in
``backend/voice_unlock`` (``jarvis_proximity_integration.authenticate`` /
``unified_voice_cache_manager.verify_voice_from_audio``). We reuse them
via a thin injectable ``voice_verify_fn`` adapter that is **lazy-imported
inside the verify call** -- so this module imports cleanly in a bare test
env with no heavy voice deps. The CRITICAL_ELEVATION approval is reused
via an injectable ``approve_fn``.

Audio is processed in-memory only. We retain ONLY ``sha256(audio)`` for
the audit ledger. The raw audio is NEVER persisted.

Security fixes (Phase 2 security review)
=========================================
M2 -- Audit-then-approve ordering:
  The AUTHORIZED path now writes the audit record DURABLY (fsync confirmed)
  BEFORE calling approve_fn. If the ledger append raises (AuditWriteError),
  the authorization is REJECTED with reason "fail_closed:audit_unavailable"
  and approve_fn is NEVER called. No merge can outrun its immutable record.
  REJECTED outcomes stay fail-soft (best-effort audit, no blocking).

M1 -- Real floor enforcement (kill the dead no-op):
  ``_floor_at_least_approval`` now returns True ONLY when the cross-repo
  governance floor is "approval_required" or "critical_elevation" (or
  stricter). A relaxed floor (safe_auto / notify_apply / None / unknown)
  returns False -> caller rejects with reason "fail_closed:floor_relaxed".
  Any import/runtime error -> False (fail-CLOSED). This is defense in depth
  beneath the Immutable Orange + jarvis-allowlist (which stay authoritative).

H1 -- CLOSED (Phase 3): Biometric-Semantic Binding
  ``JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH`` now defaults to **true** --
  phrase-match is MANDATORY by default. The spoken content is bound to the
  audio: the operator must utter the live challenge phrase, transcribed by
  the LOCAL Whisper ASR and WER-matched (>= 90% by default) against the
  expected phrase. The ECAPA voice biometric (``voice_verify_fn``) and the
  ASR phrase-match (``phrase_match_fn`` -> ``asr_phrase_match``) run
  CONCURRENTLY (``asyncio.gather(..., return_exceptions=True)``) on the SAME
  audio buffer. The fail-CLOSED gate: **BOTH must pass**. Either side
  failing / raising -> REJECT. An explicit ``=false`` disables phrase-match
  for a degraded / loopback-only mode and THEN logs the signal-level-only
  [SECURITY] warning. When the default real ``asr_phrase_match`` is used,
  the old ``phrase_match_required_but_unavailable`` reject now only fires if
  someone both REQUIRES it AND breaks the ASR (e.g. injects a None fn while
  requiring).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("CommandNode.BiometricAuth")

# --- one-time phrase-match status log ----------------------------------------
_PHRASE_MATCH_STATUS_LOGGED = False


def _maybe_log_phrase_match_status(*, require_pm: bool) -> None:
    """Phase 3: emit a one-time status log on the FIRST authorize_elevation.

    When phrase-match is ACTIVE (the default), confirm the semantic binding
    is in force. When someone has EXPLICITLY disabled it
    (``JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH=false``), emit the loud
    [SECURITY] warning that replay defense now relies solely on signal-level
    anti-spoof / liveness (degraded / loopback-only mode)."""
    global _PHRASE_MATCH_STATUS_LOGGED
    if _PHRASE_MATCH_STATUS_LOGGED:
        return
    _PHRASE_MATCH_STATUS_LOGGED = True
    if require_pm:
        logger.info(
            "[CommandNode] Biometric-Semantic Binding ACTIVE -- local ASR "
            "WER phrase-match runs CONCURRENTLY with the ECAPA biometric; "
            "both must pass (H1 closed)."
        )
    else:
        logger.warning(
            "[SECURITY] phrase-match EXPLICITLY DISABLED "
            "(JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH=false) -- biometric "
            "replay defense relies solely on signal-level anti-spoof/liveness "
            "(degraded / loopback-only mode). Re-enable before any "
            "attacker-reachable (non-loopback) deployment."
        )


# --- env knobs (no hardcoding) --------------------------------------------


def _challenge_ttl_s() -> int:
    try:
        return max(0, int(os.environ.get(
            "JARVIS_COMMAND_NODE_CHALLENGE_TTL_S", "90",
        )))
    except (TypeError, ValueError):
        return 90


def _auth_threshold() -> float:
    try:
        return float(os.environ.get(
            "JARVIS_COMMAND_NODE_AUTH_THRESHOLD", "0.85",
        ))
    except (TypeError, ValueError):
        return 0.85


def is_command_node_auth_enabled() -> bool:
    """Master switch -- ``JARVIS_COMMAND_NODE_AUTH_ENABLED`` (default
    **false**). The write-path service is gated OFF by default; the
    read-only dashboard works without it. Only an explicit ``"true"``
    (case-insensitive) enables the write-path routes."""
    return os.environ.get(
        "JARVIS_COMMAND_NODE_AUTH_ENABLED", "false",
    ).strip().lower() == "true"


def _require_phrase_match() -> bool:
    """H1 (Phase 3 -- CLOSED) -- ``JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH``
    (default **true**). When true (the default), the ASR phrase-match runs
    CONCURRENTLY with the ECAPA biometric and BOTH must pass. Only an
    explicit ``"false"`` (case-insensitive) disables it (degraded /
    loopback-only mode)."""
    return os.environ.get(
        "JARVIS_COMMAND_NODE_REQUIRE_PHRASE_MATCH", "true",
    ).strip().lower() != "false"


# --- the Immutable Orange floor (THE LAW) ---------------------------------
# Mirror of critical_elevation._IMMUTABLE_ORANGE_REPOS. Inlined as a local
# constant so this security module has a hard, import-independent floor
# even if the governance import path is unavailable -- defense in depth.
_IMMUTABLE_ORANGE_REPOS = frozenset({"prime", "reactor"})

# The Body -- the ONLY repo a biometric may authorize through the operator
# approval step. Any other (unknown) target fails CLOSED to REJECT (mirror
# of critical_elevation._BODY_REPO + its unknown-repo fail-closed rule).
_BODY_REPO = "jarvis"

# M1 -- floors that are strict enough to allow the biometric path.
_STRICT_FLOORS = frozenset({"approval_required", "critical_elevation"})


def _is_immutable_orange(target_repo: str) -> bool:
    """True iff the target repo is Mind (``prime``) or Nerves
    (``reactor``). Normalised, evaluated WITHOUT any env input -- this is
    the Sovereign Law re-checked at the write-path (defense in depth)."""
    return (target_repo or "").strip().lower() in _IMMUTABLE_ORANGE_REPOS


# --- challenge phrase pool ------------------------------------------------
# A static enrollment recording can't answer a randomized live phrase.
_PHRASE_POOL = (
    "the sovereign organism authorizes this mutation",
    "voice gate open, blast radius acknowledged",
    "operator presence confirmed for cross repo elevation",
    "I authorize this elevation with my voice",
    "the immutable orange protocol still holds",
    "this is a fresh and live authorization",
    "command node verify operator identity now",
    "elevation requested, speaking the challenge phrase",
    "biometric edge gate, fresh nonce, live capture",
    "sovereign command node authorize elevation step",
    "the body may merge, the mind never will",
    "live voice, fresh nonce, single use authorization",
)


def _select_phrase(nonce: str) -> str:
    """Deterministic-from-nonce phrase selection so it differs per
    request (the nonce is fresh per request). The pool + nonce-derived
    index means the operator must speak THIS phrase."""
    # Use the nonce's leading hex as an integer seed; never raises.
    try:
        seed = int(nonce[:8], 16)
    except (TypeError, ValueError):
        seed = secrets.randbelow(1_000_000)
    return _PHRASE_POOL[seed % len(_PHRASE_POOL)]


# --- dataclasses ----------------------------------------------------------


@dataclass
class Challenge:
    """A single-use, TTL-bounded challenge bound to a specific PR + AST
    mutation. The nonce is the anti-replay token: consumed atomically on
    use."""

    nonce: str
    phrase: str
    pr_id: str
    ast_mutation_id: str
    blast_radius_hash: str
    issued_at: float
    ttl_s: int
    consumed: bool = False

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        return (now - self.issued_at) > self.ttl_s

    def to_public_dict(self) -> Dict[str, Any]:
        """Wire shape for the challenge-issue response. The nonce IS
        returned (the client echoes it back on authorize); it is single-
        use + TTL-bounded so disclosure to the local operator is safe."""
        return {
            "nonce": self.nonce,
            "phrase": self.phrase,
            "pr_id": self.pr_id,
            "ast_mutation_id": self.ast_mutation_id,
            "blast_radius_hash": self.blast_radius_hash,
            "issued_at": self.issued_at,
            "ttl_s": self.ttl_s,
        }


@dataclass
class AuthorizationResult:
    """The verdict returned to the caller (and projected to the wire)."""

    decision: str  # "AUTHORIZED" | "REJECTED"
    reason: str
    ecapa_score: Optional[float]
    antispoof_ok: bool
    freshness_ok: bool
    pr_id: str
    ast_mutation_id: str
    target_repo: Optional[str] = None
    voiceprint_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "ecapa_score": self.ecapa_score,
            "antispoof_ok": self.antispoof_ok,
            "freshness_ok": self.freshness_ok,
            "pr_id": self.pr_id,
            "ast_mutation_id": self.ast_mutation_id,
            "target_repo": self.target_repo,
        }


# --- the default (real) adapters (lazy -- never imported at module load) --


async def _default_voice_verify_fn(
    audio: bytes, sample_rate: int,
) -> Dict[str, Any]:
    """Thin adapter over the EXISTING voice pipeline.

    Lazy-imports the heavy voice deps INSIDE the call so the middleware
    module imports cleanly in a bare test env. Normalizes whichever
    pipeline is wired into the verdict shape the middleware consumes::

        {authenticated: bool, score: float, antispoof_ok: bool,
         liveness_ok: bool, voiceprint_id: str}

    Fail-CLOSED: any import / runtime error -> a REJECTING verdict
    (authenticated False, score 0.0, antispoof_ok False)."""
    try:
        # Reuse the raw-bytes entry point (runs ECAPA + anti-spoof +
        # liveness + replay-detection inside the existing pipeline).
        from backend.voice_unlock.jarvis_proximity_integration import (  # noqa: E501
            JarvisProximityIntegration,
        )
        integ = JarvisProximityIntegration()
        if hasattr(integ, "initialize"):
            try:
                await integ.initialize()
            except Exception:  # noqa: BLE001 -- best effort
                pass
        raw = await integ.authenticate(audio_data=audio, sample_rate=sample_rate)
        return _normalize_voice_verdict(raw)
    except Exception:  # noqa: BLE001 -- FAIL-CLOSED
        logger.error(
            "[BiometricAuth] real voice_verify adapter raised -- "
            "fail-closed REJECT verdict",
            exc_info=True,
        )
        return {
            "authenticated": False,
            "score": 0.0,
            "antispoof_ok": False,
            "liveness_ok": False,
            "voiceprint_id": None,
        }


def _normalize_voice_verdict(raw: Any) -> Dict[str, Any]:
    """Map a heterogeneous pipeline result into the middleware verdict
    shape. Conservative: anything ambiguous -> the REJECTING value."""
    if not isinstance(raw, dict):
        return {
            "authenticated": False, "score": 0.0,
            "antispoof_ok": False, "liveness_ok": False,
            "voiceprint_id": None,
        }
    authenticated = bool(raw.get("authenticated", raw.get("success", False)))
    # Prefer an explicit voice/biometric score; fall back to combined.
    score = raw.get("score")
    if score is None:
        score = raw.get("voice_score")
    if score is None:
        score = raw.get("combined_score")
    try:
        score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    # Anti-spoof / liveness: present-and-true => pass; absent => fail
    # closed (we never assume an unprovided check passed).
    antispoof_ok = bool(raw.get("antispoof_ok", raw.get("anti_spoof_passed", False)))
    liveness_ok = bool(raw.get("liveness_ok", raw.get("liveness_passed", False)))
    return {
        "authenticated": authenticated,
        "score": score,
        "antispoof_ok": antispoof_ok,
        "liveness_ok": liveness_ok,
        "voiceprint_id": raw.get("voiceprint_id", "owner"),
    }


async def _default_approve_fn(*, pr_id: str, ast_mutation_id: str) -> Any:
    """Thin adapter over the EXISTING CRITICAL_ELEVATION approval path.

    Lazy-imported. NOTE: the Immutable Orange floor is ALSO enforced
    inside the real approval path -- this middleware re-checks it BEFORE
    ever reaching here (defense in depth), so a Mind/Nerves PR never
    reaches this function."""
    from backend.core.ouroboros.governance import critical_elevation  # noqa: F401
    # The concrete merge-approve wiring is supplied by the harness at
    # mount time; absent an injected approve_fn we record the intent and
    # defer to the existing CLI approval path (fail-soft, never silently
    # auto-merges anything).
    logger.info(
        "[BiometricAuth] approve intent pr_id=%s ast_mutation_id=%s "
        "(deferring to CRITICAL_ELEVATION approval path)",
        pr_id, ast_mutation_id,
    )
    return {"approved": True, "pr_id": pr_id, "ast_mutation_id": ast_mutation_id}


def _default_resolve_target_repo_fn(pr_id: str) -> str:
    """Resolve a PR's target repo. Default: fail-CLOSED to a non-jarvis
    sentinel so an unresolved PR never accidentally looks like the Body.
    The harness injects the real resolver (the PR/elevation record carries
    ``target_repo``)."""
    return "__unresolved__"


# --- the middleware -------------------------------------------------------


class BiometricAuthMiddleware:
    """Challenge issuer + fail-CLOSED authorize-elevation orchestrator.

    Holds a bounded, TTL-expiring, single-use in-memory challenge store.
    Thread-safe consume under an asyncio lock (atomic anti-replay)."""

    # Bound the challenge store (defends against challenge-spam DoS).
    MAX_CHALLENGES = 1024

    def __init__(
        self,
        *,
        challenge_ttl_s: Optional[int] = None,
        auth_threshold: Optional[float] = None,
        audit_sink: Optional[Callable[[Dict[str, Any]], Any]] = None,
        floor_check_fn: Optional[Callable[[Optional[str]], bool]] = None,
    ) -> None:
        self._ttl_s = (
            challenge_ttl_s if challenge_ttl_s is not None else _challenge_ttl_s()
        )
        self._threshold = (
            auth_threshold if auth_threshold is not None else _auth_threshold()
        )
        # nonce -> Challenge
        self._store: Dict[str, Challenge] = {}
        self._lock = asyncio.Lock()
        # Injectable audit sink (default = the durable hash-chained
        # ledger, lazy-resolved so tests can pass a fake).
        self._audit_sink = audit_sink
        # M1 injectable floor check (default = real cross_repo_elevation_floor
        # lookup; tests may inject a stub so they don't need governance imports).
        self._floor_check_fn = floor_check_fn

    # --- challenge issuance ----------------------------------------------

    def issue_challenge(
        self,
        *,
        pr_id: str,
        ast_mutation_id: str,
        blast_radius_hash: str,
    ) -> Challenge:
        """Mint a single-use, TTL-bounded challenge bound to THIS PR +
        AST mutation. The phrase is randomized per request (nonce-derived
        selection from the pool)."""
        self._evict_expired()
        # Hard bound: if somehow full of live challenges, drop the oldest.
        if len(self._store) >= self.MAX_CHALLENGES:
            oldest = min(
                self._store.values(), key=lambda c: c.issued_at, default=None,
            )
            if oldest is not None:
                self._store.pop(oldest.nonce, None)
        nonce = secrets.token_hex(32)  # 256-bit, 64 hex chars
        ch = Challenge(
            nonce=nonce,
            phrase=_select_phrase(nonce),
            pr_id=str(pr_id),
            ast_mutation_id=str(ast_mutation_id),
            blast_radius_hash=str(blast_radius_hash),
            issued_at=time.monotonic(),
            ttl_s=self._ttl_s,
            consumed=False,
        )
        self._store[nonce] = ch
        return ch

    def _evict_expired(self) -> None:
        now = time.monotonic()
        dead = [n for n, c in self._store.items() if c.is_expired(now=now)]
        for n in dead:
            self._store.pop(n, None)

    # --- the write-path ---------------------------------------------------

    async def authorize_elevation(
        self,
        *,
        pr_id: str,
        nonce: str,
        ast_mutation_id: str,
        audio: bytes,
        sample_rate: int,
        voice_verify_fn: Optional[
            Callable[[bytes, int], Awaitable[Dict[str, Any]]]
        ] = None,
        approve_fn: Optional[Callable[..., Any]] = None,
        resolve_target_repo_fn: Optional[Callable[[str], str]] = None,
        phrase_match_fn: Optional[Callable[..., Any]] = None,
    ) -> AuthorizationResult:
        """Authorize (or REJECT) a CRITICAL_ELEVATION operator-approval
        step. FAIL-CLOSED at every step; any exception -> REJECTED.

        Order is load-bearing:
          a. Freshness / anti-replay FIRST (consume the nonce ATOMICALLY
             under the lock BEFORE verifying -- a concurrent/replayed
             same-nonce request fails immediately).
          b. CONCURRENT validation gate (Phase 3 -- Biometric-Semantic
             Binding): the ECAPA biometric (``voice_verify_fn``) and the ASR
             phrase-match (``phrase_match_fn`` -> ``asr_phrase_match``) run
             CONCURRENTLY via ``asyncio.gather(..., return_exceptions=True)``
             on the SAME audio buffer. The fail-CLOSED gate: BOTH must pass.
             ECAPA-pass + WER-pass -> biometric_pass. ECAPA-pass + WER-fail
             -> REJECT phrase_mismatch. WER-pass + ECAPA-fail -> REJECT
             biometric_mismatch (the specific biometric sub-reason). Either
             future RAISING -> that side False -> REJECT fail_closed:...
          c. Immutable Orange re-check (THE LAW): target in
             {prime,reactor} -> REJECT regardless of a perfect biometric.
          d. M1 floor re-check: governance floor must be approval_required
             or stricter; relaxed floor -> REJECT fail_closed:floor_relaxed.
          e. M2 audit-then-approve: write audit record DURABLY (fsync)
             BEFORE calling approve_fn. Audit write failure -> REJECT
             fail_closed:audit_unavailable; approve_fn is NEVER called.
          f. Decision: call approve_fn; return AUTHORIZED.

        Audio is hashed (sha256) for the audit record then dropped. The
        transcript is NEVER persisted -- only ``sha256(transcript)`` reaches
        the audit ledger. The raw audio is NEVER persisted.

        phrase_match_fn (Phase 3):
          By default (REQUIRE_PHRASE_MATCH=true) and with no fn injected, the
          real ``asr_phrase_match`` (local Whisper + WER) is used. An injected
          fn may be either the ``asr_phrase_match`` form (awaitable, takes
          ``audio`` / ``sample_rate`` / ``expected_phrase`` kwargs, returns
          ``(passed, info)``) or a legacy zero-arg predicate (returns truthy).
          If REQUIRE is true but ``phrase_match_fn`` is explicitly None AND the
          default cannot be bound -> REJECT
          fail_closed:phrase_match_required_but_unavailable.
        """
        voice_verify_fn = voice_verify_fn or _default_voice_verify_fn
        approve_fn = approve_fn or _default_approve_fn
        resolve_target_repo_fn = (
            resolve_target_repo_fn or _default_resolve_target_repo_fn
        )

        # Phase 3 -- one-time status log (semantic binding ACTIVE, or the
        # inverse [SECURITY] warning if someone explicitly disabled it).
        require_pm = _require_phrase_match()
        if is_command_node_auth_enabled():
            _maybe_log_phrase_match_status(require_pm=require_pm)

        # Audio in-memory only -- retain ONLY its hash.
        try:
            audio_sha256 = hashlib.sha256(audio or b"").hexdigest()
        except Exception:  # noqa: BLE001 -- never let hashing crash us
            audio_sha256 = ""

        target_repo: Optional[str] = None
        voiceprint_id: Optional[str] = None
        ecapa_score: Optional[float] = None
        antispoof_ok = False
        challenge: Optional[Challenge] = None
        wer_value: Optional[float] = None
        transcript_hash: Optional[str] = None
        phrase_match_ok = False

        try:
            # ----- (a) FRESHNESS / ANTI-REPLAY (atomic consume) -----
            challenge = await self._consume_nonce_atomic(
                nonce=nonce, pr_id=pr_id, ast_mutation_id=ast_mutation_id,
            )
            if challenge is None:
                return self._reject(
                    reason="freshness:nonce_invalid_expired_or_replayed",
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=False, antispoof_ok=False,
                    ecapa_score=None, target_repo=None, voiceprint_id=None,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=None,
                )

            # ----- (b) CONCURRENT VALIDATION GATE (Phase 3) -----
            # The ECAPA biometric and the ASR phrase-match run CONCURRENTLY
            # on the SAME audio buffer. The fail-CLOSED gate: BOTH must pass.
            #
            # If phrase-match is REQUIRED (default) but no fn is wired AND the
            # real asr_phrase_match cannot be bound -> fail-CLOSED before any
            # work (preserves the H1 "required_but_unavailable" reject only for
            # the someone-broke-the-ASR case).
            effective_pm_fn = phrase_match_fn
            if require_pm and effective_pm_fn is None:
                effective_pm_fn = self._resolve_default_phrase_match_fn()
                if effective_pm_fn is None:
                    return self._reject(
                        reason="fail_closed:phrase_match_required_but_unavailable",
                        pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                        freshness_ok=True, antispoof_ok=False,
                        ecapa_score=None, target_repo=None,
                        voiceprint_id=None,
                        audio_sha256=audio_sha256, challenge_nonce=nonce,
                        blast_radius_hash=challenge.blast_radius_hash,
                    )

            # Build the two awaitables. They are scheduled CONCURRENTLY via
            # asyncio.gather(return_exceptions=True): a raise in either side
            # is captured (not propagated) and mapped to a fail-CLOSED False.
            ecapa_coro = self._run_ecapa(voice_verify_fn, audio, sample_rate)
            phrase_coro = self._run_phrase_match(
                effective_pm_fn,
                audio=audio,
                sample_rate=sample_rate,
                expected_phrase=challenge.phrase,
                require_pm=require_pm,
            )
            ecapa_res, phrase_res = await asyncio.gather(
                ecapa_coro, phrase_coro, return_exceptions=True,
            )

            # ----- ECAPA side -----
            if isinstance(ecapa_res, BaseException):
                logger.error(
                    "[BiometricAuth] ECAPA future raised in gather -- "
                    "fail-closed", exc_info=ecapa_res,
                )
                ecapa_pass = False
                ecapa_score = None
                antispoof_ok = False
                liveness_ok = False
                voiceprint_id = None
                ecapa_reason = "fail_closed:biometric_error"
            else:
                (ecapa_pass, ecapa_score, antispoof_ok, liveness_ok,
                 voiceprint_id, ecapa_reason) = ecapa_res

            # ----- Phrase-match side -----
            if isinstance(phrase_res, BaseException):
                logger.error(
                    "[BiometricAuth] phrase-match future raised in gather -- "
                    "fail-closed", exc_info=phrase_res,
                )
                phrase_pass = False
            else:
                phrase_pass, wer_value, transcript_hash = phrase_res

            phrase_match_ok = bool(phrase_pass)

            # ----- the BOTH-MUST-PASS fail-CLOSED gate -----
            # Decision precedence on a partial pass: a biometric (speaker)
            # failure is the stronger signal -> report biometric_mismatch
            # first; otherwise a phrase failure -> phrase_mismatch.
            if not (ecapa_pass and phrase_match_ok):
                if not ecapa_pass:
                    reason = ecapa_reason or "biometric_mismatch"
                else:
                    reason = "phrase_mismatch"
                return self._reject(
                    reason=reason,
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=None,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
                    wer=wer_value, transcript_hash=transcript_hash,
                    phrase_match_ok=phrase_match_ok,
                )

            # ----- (c) IMMUTABLE ORANGE re-check (THE LAW) -----
            target_repo = resolve_target_repo_fn(pr_id)
            if _is_immutable_orange(target_repo):
                return self._reject(
                    reason="immutable_orange:mind_nerves_never_auto_merge",
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=target_repo,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
                    wer=wer_value, transcript_hash=transcript_hash,
                    phrase_match_ok=phrase_match_ok,
                )
            # Fail-CLOSED: a biometric may authorize ONLY the Body
            # (jarvis). Any unknown / unresolved target -> REJECT (never
            # relax on an unrecognised repo -- mirrors the cross-repo
            # floor's unknown-repo fail-closed rule).
            if (target_repo or "").strip().lower() != _BODY_REPO:
                return self._reject(
                    reason="fail_closed:unknown_target_repo",
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=target_repo,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
                    wer=wer_value, transcript_hash=transcript_hash,
                    phrase_match_ok=phrase_match_ok,
                )
            # ----- (d) M1 REAL FLOOR RE-CHECK -----
            _floor_ok = (
                self._floor_check_fn(target_repo)
                if self._floor_check_fn is not None
                else self._floor_at_least_approval(target_repo)
            )
            if not _floor_ok:
                return self._reject(
                    reason="fail_closed:floor_relaxed",
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=target_repo,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
                    wer=wer_value, transcript_hash=transcript_hash,
                    phrase_match_ok=phrase_match_ok,
                )

            # ----- (e) M2 AUDIT-THEN-APPROVE (durable write FIRST) -----
            # Build the audit record dict for the AUTHORIZED outcome.
            _auth_record = {
                "pr_id": pr_id,
                "target_repo": target_repo,
                "ast_mutation_id": ast_mutation_id,
                "blast_radius_hash": challenge.blast_radius_hash,
                "challenge_nonce": nonce,
                "voiceprint_id": voiceprint_id,
                "ecapa_score": ecapa_score,
                "antispoof_verdict": antispoof_ok,
                "freshness_ok": True,
                "decision": "AUTHORIZED",
                "audio_sha256": audio_sha256,
                # Phase 3 -- Biometric-Semantic Binding evidence. The
                # transcript itself is NEVER persisted; only its sha256.
                "wer": wer_value,
                "transcript_hash": transcript_hash,
                "phrase_match_ok": phrase_match_ok,
            }
            # Write durably BEFORE approve_fn. If the durable write fails
            # (fsync not confirmed) -> REJECT with audit_unavailable and
            # NEVER call approve_fn. No merge can outrun its record.
            try:
                self._emit_audit_authorized(_auth_record)
            except Exception:  # noqa: BLE001 -- AuditWriteError or any exc
                logger.error(
                    "[BiometricAuth] M2 audit durable-write FAILED for "
                    "pr_id=%s -- fail-closed REJECT, approve_fn NOT called",
                    pr_id, exc_info=True,
                )
                return AuthorizationResult(
                    decision="REJECTED",
                    reason="fail_closed:audit_unavailable",
                    ecapa_score=ecapa_score,
                    antispoof_ok=antispoof_ok,
                    freshness_ok=True,
                    pr_id=pr_id,
                    ast_mutation_id=ast_mutation_id,
                    target_repo=target_repo,
                    voiceprint_id=voiceprint_id,
                )

            # ----- (f) DECISION: call the existing approval path -----
            await _maybe_await(approve_fn(
                pr_id=pr_id, ast_mutation_id=ast_mutation_id,
            ))
            return AuthorizationResult(
                decision="AUTHORIZED",
                reason="authorized:fresh_nonce_biometric_pass_not_immutable_orange",
                ecapa_score=ecapa_score,
                antispoof_ok=antispoof_ok,
                freshness_ok=True,
                pr_id=pr_id,
                ast_mutation_id=ast_mutation_id,
                target_repo=target_repo,
                voiceprint_id=voiceprint_id,
            )

        except Exception:  # noqa: BLE001 -- FAIL-CLOSED ABSOLUTE
            logger.error(
                "[BiometricAuth] authorize_elevation raised -- fail-closed "
                "REJECT (pr_id=%s)", pr_id, exc_info=True,
            )
            return self._reject(
                reason="fail_closed:internal_error",
                pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                freshness_ok=(challenge is not None),
                antispoof_ok=antispoof_ok, ecapa_score=ecapa_score,
                target_repo=target_repo, voiceprint_id=voiceprint_id,
                audio_sha256=audio_sha256, challenge_nonce=nonce,
                blast_radius_hash=(
                    challenge.blast_radius_hash if challenge else None
                ),
                wer=wer_value, transcript_hash=transcript_hash,
                phrase_match_ok=phrase_match_ok,
            )

    # --- Phase 3: concurrent-gate workers --------------------------------

    async def _run_ecapa(
        self,
        voice_verify_fn: Callable[[bytes, int], Awaitable[Dict[str, Any]]],
        audio: bytes,
        sample_rate: int,
    ) -> Any:
        """Run the ECAPA biometric and reduce it to a tuple
        ``(pass, score, antispoof_ok, liveness_ok, voiceprint_id, reason)``.

        Scheduled inside ``asyncio.gather(return_exceptions=True)`` -- a
        raised exception here is captured by gather (NOT swallowed) and
        mapped to a fail-CLOSED False by the caller. The biometric pass
        requires authenticated AND score>=threshold AND anti-spoof AND
        liveness -- never one bool."""
        verdict = await voice_verify_fn(audio, sample_rate)
        if not isinstance(verdict, dict):
            verdict = {}
        authenticated = bool(verdict.get("authenticated", False))
        try:
            score: Optional[float] = float(verdict.get("score"))
        except (TypeError, ValueError):
            score = None
        antispoof_ok = bool(verdict.get("antispoof_ok", False))
        liveness_ok = bool(verdict.get("liveness_ok", False))
        voiceprint_id = verdict.get("voiceprint_id")
        ecapa_pass = (
            authenticated
            and score is not None
            and score >= self._threshold
            and antispoof_ok
            and liveness_ok
        )
        reason = (
            "" if ecapa_pass
            else self._biometric_reject_reason(
                authenticated, score, antispoof_ok, liveness_ok,
            )
        )
        return (ecapa_pass, score, antispoof_ok, liveness_ok,
                voiceprint_id, reason)

    async def _run_phrase_match(
        self,
        phrase_match_fn: Optional[Callable[..., Any]],
        *,
        audio: bytes,
        sample_rate: int,
        expected_phrase: str,
        require_pm: bool,
    ) -> Any:
        """Run the ASR phrase-match and reduce it to a tuple
        ``(pass, wer, transcript_hash)``.

        When phrase-match is NOT required and no fn is wired, this is a
        no-op PASS (legacy / degraded mode) -- the BOTH-must-pass gate then
        reduces to the ECAPA result alone.

        Supports two ``phrase_match_fn`` shapes:
          * the ``asr_phrase_match`` form -- awaitable, accepts
            ``audio`` / ``sample_rate`` / ``expected_phrase`` kwargs, returns
            ``(passed, {transcript, wer, threshold})``;
          * a legacy zero-arg predicate returning truthy / falsy.

        The transcript is hashed (sha256) here and DROPPED -- only its hash
        leaves this method. Scheduled inside
        ``asyncio.gather(return_exceptions=True)``.
        """
        if phrase_match_fn is None:
            # Not required + not wired -> no-op PASS (the ECAPA term still
            # governs the gate). If it WERE required, the caller already
            # fail-CLOSED before scheduling this coroutine.
            return (True, None, None)

        # Inspect the callable to decide how to invoke it. The asr_phrase_match
        # form takes keyword args; the legacy predicate takes none.
        import inspect

        try:
            sig = inspect.signature(phrase_match_fn)
            takes_kwargs = bool(sig.parameters)
        except (TypeError, ValueError):
            takes_kwargs = True  # builtins / C funcs -> assume the rich form

        if takes_kwargs:
            raw = phrase_match_fn(
                audio=audio,
                sample_rate=sample_rate,
                expected_phrase=expected_phrase,
            )
            result = await _maybe_await(raw)
            # asr_phrase_match returns (passed, info-dict).
            if isinstance(result, tuple) and len(result) == 2:
                passed, info = result
                wer = None
                transcript = ""
                if isinstance(info, dict):
                    wer = info.get("wer")
                    transcript = info.get("transcript", "") or ""
                t_hash = (
                    hashlib.sha256(transcript.encode("utf-8")).hexdigest()
                    if transcript else None
                )
                return (bool(passed), wer, t_hash)
            # A bare truthy / falsy return is also accepted.
            return (bool(result), None, None)

        # Legacy zero-arg predicate.
        raw = phrase_match_fn()
        result = await _maybe_await(raw)
        return (bool(result), None, None)

    @staticmethod
    def _resolve_default_phrase_match_fn() -> Optional[Callable[..., Any]]:
        """Bind the REAL local-Whisper ``asr_phrase_match`` as the default
        phrase-match verifier (lazy import so the middleware imports cleanly
        without the ASR module's deps). Returns None on any import failure
        -> the caller fail-CLOSES with ``phrase_match_required_but_unavailable``
        (only fires if someone REQUIRES phrase-match AND the ASR is broken)."""
        try:
            from backend.core.ouroboros.governance.command_node.semantic_phrase_match import (  # noqa: E501
                asr_phrase_match,
            )
            return asr_phrase_match
        except Exception:  # noqa: BLE001 -- fail-CLOSED
            logger.error(
                "[BiometricAuth] could not bind default asr_phrase_match -- "
                "fail-closed (phrase_match_required_but_unavailable)",
                exc_info=True,
            )
            return None

    # --- atomic nonce consume --------------------------------------------

    async def _consume_nonce_atomic(
        self, *, nonce: str, pr_id: str, ast_mutation_id: str,
    ) -> Optional[Challenge]:
        """Atomically validate + consume the nonce. Returns the Challenge
        on success, or ``None`` (REJECT) if the nonce is unknown, already
        consumed, expired, or bound to a different pr_id/ast_mutation_id.

        The consume happens UNDER the lock BEFORE biometric verification,
        so a concurrent/replayed same-nonce request finds it already spent
        and fails immediately (single-use anti-replay)."""
        async with self._lock:
            self._evict_expired()
            ch = self._store.get(nonce)
            if ch is None:
                return None
            if ch.consumed:
                return None
            if ch.is_expired():
                # Spent its life -- drop + reject.
                self._store.pop(nonce, None)
                return None
            # Binding: the nonce only authorizes the PR + mutation it was
            # issued for.
            if ch.pr_id != str(pr_id) or ch.ast_mutation_id != str(ast_mutation_id):
                return None
            # Consume NOW (single-use). Mark consumed AND remove so a
            # replay can never re-read it.
            ch.consumed = True
            self._store.pop(nonce, None)
            return ch

    # --- M1 real floor re-check ------------------------------------------

    @staticmethod
    def _floor_at_least_approval(target_repo: Optional[str]) -> bool:
        """M1 -- Defense in depth: confirm the cross-repo governance floor
        for this target is at least ``approval_required``.

        Returns True ONLY when we can affirmatively prove the floor is
        ``approval_required`` or ``critical_elevation`` (or stricter).
        A relaxed floor (``safe_auto`` / ``notify_apply`` / None / unknown /
        import failure) -> False (fail-CLOSED).

        The Immutable Orange check above already excluded prime/reactor
        regardless of this lookup. This check fires ONLY for Body (jarvis)
        PRs and is defense-in-depth beneath the Immutable Orange +
        jarvis-allowlist (which stay authoritative).
        """
        try:
            from backend.core.ouroboros.governance.critical_elevation import (
                cross_repo_elevation_floor,
            )
            floor = cross_repo_elevation_floor(
                target_repo=(target_repo or ""), crosses_repo=True,
            )
            # Only strict floors pass. None / unknown / relaxed -> False.
            return (floor in _STRICT_FLOORS)
        except Exception:  # noqa: BLE001 -- fail-CLOSED
            logger.debug(
                "[BiometricAuth] M1 floor re-check raised -- fail-CLOSED "
                "(treating floor as relaxed, rejecting)", exc_info=True,
            )
            return False

    # --- result builders + audit -----------------------------------------

    @staticmethod
    def _biometric_reject_reason(
        authenticated: bool, score: Optional[float],
        antispoof_ok: bool, liveness_ok: bool,
    ) -> str:
        if not authenticated:
            return "biometric:not_authenticated"
        if score is None:
            return "biometric:no_score"
        if not antispoof_ok:
            return "biometric:antispoof_failed"
        if not liveness_ok:
            return "biometric:liveness_failed"
        return "biometric:score_below_threshold"

    def _emit_audit_authorized(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """M2 -- Write the AUTHORIZED audit record DURABLY (raise_on_write_failure=True).
        Raises AuditWriteError if the fsync-confirmed write fails. The
        caller (authorize_elevation) must NOT call approve_fn if this raises.
        """
        from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (  # noqa: E501
            AuditWriteError,
        )
        sink = self._audit_sink
        if sink is None:
            from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (  # noqa: E501
                get_default_ledger,
            )
            ledger = get_default_ledger()
            # Call the ledger directly with raise_on_write_failure=True.
            return ledger.append(record, raise_on_write_failure=True)
        else:
            # Injectable sink (used in tests). The sink must return the
            # record or raise to simulate failure. We wrap it:
            # if the sink raises AuditWriteError we re-raise; any other
            # exception we treat as a durable-write failure and raise
            # AuditWriteError (fail-CLOSED).
            try:
                result = sink(record)
                # Return the record dict (sink may or may not return one).
                return record if result is None else (result if isinstance(result, dict) else record)
            except AuditWriteError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AuditWriteError(
                    'audit sink raised for AUTHORIZED pr_id=' + repr(record.get('pr_id')) + ': ' + str(exc)
                ) from exc

    def _reject(self, *, reason: str, **kw: Any) -> AuthorizationResult:
        res = AuthorizationResult(
            decision="REJECTED",
            reason=reason,
            ecapa_score=kw.get("ecapa_score"),
            antispoof_ok=kw.get("antispoof_ok", False),
            freshness_ok=kw.get("freshness_ok", False),
            pr_id=kw["pr_id"],
            ast_mutation_id=kw["ast_mutation_id"],
            target_repo=kw.get("target_repo"),
            voiceprint_id=kw.get("voiceprint_id"),
        )
        # REJECTED outcomes: fail-soft audit (best-effort, never raises).
        self._emit_audit_soft(res, kw)
        return res

    def _emit_audit_soft(
        self, res: AuthorizationResult, kw: Dict[str, Any],
    ) -> None:
        """Append to the hash-chained audit ledger for REJECTED outcomes.
        Fail-soft (never raises, logs loudly). Audio is represented ONLY
        by its sha256."""
        record = {
            "pr_id": res.pr_id,
            "target_repo": res.target_repo,
            "ast_mutation_id": res.ast_mutation_id,
            "blast_radius_hash": kw.get("blast_radius_hash"),
            "challenge_nonce": kw.get("challenge_nonce"),
            "voiceprint_id": res.voiceprint_id,
            "ecapa_score": res.ecapa_score,
            "antispoof_verdict": res.antispoof_ok,
            "freshness_ok": res.freshness_ok,
            "decision": res.decision,
            "audio_sha256": kw.get("audio_sha256"),
            # Phase 3 -- semantic-binding evidence (transcript NEVER stored;
            # only its sha256). Present even on REJECTED so the ledger shows
            # whether the phrase-match passed / failed / was unavailable.
            "wer": kw.get("wer"),
            "transcript_hash": kw.get("transcript_hash"),
            "phrase_match_ok": kw.get("phrase_match_ok"),
        }
        try:
            sink = self._audit_sink
            if sink is None:
                from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (  # noqa: E501
                    get_default_ledger,
                )
                # fail-soft: raise_on_write_failure=False (default)
                get_default_ledger().append(record)
            else:
                try:
                    sink(record)
                except Exception:  # noqa: BLE001 -- fail-soft for REJECTED
                    pass
        except Exception:  # noqa: BLE001 -- fail-soft, log LOUDLY
            logger.error(
                "[BiometricAuth] audit emit FAILED for pr_id=%s decision=%s",
                res.pr_id, res.decision, exc_info=True,
            )


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable; else return it. Lets approve_fn
    be sync OR async."""
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


# Process-default singleton (lazy).
_DEFAULT_MIDDLEWARE: Optional[BiometricAuthMiddleware] = None


def get_default_middleware() -> BiometricAuthMiddleware:
    global _DEFAULT_MIDDLEWARE
    if _DEFAULT_MIDDLEWARE is None:
        _DEFAULT_MIDDLEWARE = BiometricAuthMiddleware()
    return _DEFAULT_MIDDLEWARE


__all__ = [
    "AuthorizationResult",
    "BiometricAuthMiddleware",
    "Challenge",
    "get_default_middleware",
    "is_command_node_auth_enabled",
]
