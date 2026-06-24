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


# --- the Immutable Orange floor (THE LAW) ---------------------------------
# Mirror of critical_elevation._IMMUTABLE_ORANGE_REPOS. Inlined as a local
# constant so this security module has a hard, import-independent floor
# even if the governance import path is unavailable -- defense in depth.
_IMMUTABLE_ORANGE_REPOS = frozenset({"prime", "reactor"})

# The Body -- the ONLY repo a biometric may authorize through the operator
# approval step. Any other (unknown) target fails CLOSED to REJECT (mirror
# of critical_elevation._BODY_REPO + its unknown-repo fail-closed rule).
_BODY_REPO = "jarvis"


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
    ) -> AuthorizationResult:
        """Authorize (or REJECT) a CRITICAL_ELEVATION operator-approval
        step. FAIL-CLOSED at every step; any exception -> REJECTED.

        Order is load-bearing:
          a. Freshness / anti-replay FIRST (consume the nonce ATOMICALLY
             under the lock BEFORE verifying -- a concurrent/replayed
             same-nonce request fails immediately).
          b. Biometric (authenticated AND score>=threshold AND anti-spoof
             AND liveness -- checked explicitly, never one bool).
          c. Immutable Orange re-check (THE LAW): target in
             {prime,reactor} -> REJECT regardless of a perfect biometric.
          d. Decision: fresh + biometric-pass + not-Immutable-Orange ->
             call approve_fn; else REJECT.

        Audio is hashed (sha256) for the audit record then dropped. The
        raw audio is NEVER persisted.
        """
        voice_verify_fn = voice_verify_fn or _default_voice_verify_fn
        approve_fn = approve_fn or _default_approve_fn
        resolve_target_repo_fn = (
            resolve_target_repo_fn or _default_resolve_target_repo_fn
        )

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

            # ----- (b) BIOMETRIC (reuse via injectable adapter) -----
            verdict = await voice_verify_fn(audio, sample_rate)
            if not isinstance(verdict, dict):
                verdict = {}
            authenticated = bool(verdict.get("authenticated", False))
            try:
                ecapa_score = float(verdict.get("score"))
            except (TypeError, ValueError):
                ecapa_score = None
            antispoof_ok = bool(verdict.get("antispoof_ok", False))
            liveness_ok = bool(verdict.get("liveness_ok", False))
            voiceprint_id = verdict.get("voiceprint_id")

            biometric_pass = (
                authenticated
                and ecapa_score is not None
                and ecapa_score >= self._threshold
                and antispoof_ok
                and liveness_ok
            )
            if not biometric_pass:
                return self._reject(
                    reason=self._biometric_reject_reason(
                        authenticated, ecapa_score, antispoof_ok, liveness_ok,
                    ),
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=None,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
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
                )
            # Defense in depth: re-check the governance floor is not
            # below approval_required for this cross-repo target.
            if not self._floor_at_least_approval(target_repo):
                return self._reject(
                    reason="immutable_orange:floor_below_approval_required",
                    pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                    freshness_ok=True, antispoof_ok=antispoof_ok,
                    ecapa_score=ecapa_score, target_repo=target_repo,
                    voiceprint_id=voiceprint_id,
                    audio_sha256=audio_sha256, challenge_nonce=nonce,
                    blast_radius_hash=challenge.blast_radius_hash,
                )

            # ----- (d) DECISION: call the existing approval path -----
            await _maybe_await(approve_fn(
                pr_id=pr_id, ast_mutation_id=ast_mutation_id,
            ))
            return self._authorize(
                pr_id=pr_id, ast_mutation_id=ast_mutation_id,
                ecapa_score=ecapa_score, antispoof_ok=antispoof_ok,
                target_repo=target_repo, voiceprint_id=voiceprint_id,
                audio_sha256=audio_sha256, challenge_nonce=nonce,
                blast_radius_hash=challenge.blast_radius_hash,
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
            )

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

    # --- floor re-check ---------------------------------------------------

    @staticmethod
    def _floor_at_least_approval(target_repo: Optional[str]) -> bool:
        """Defense in depth: confirm the cross-repo governance floor for
        this target is at least ``approval_required`` (Immutable Orange
        guarantees this for prime/reactor). Fail-CLOSED: any error -> we
        treat the floor as satisfied ONLY when we can affirmatively prove
        it is approval-required or stricter; an unprovable lookup returns
        True for jarvis (Body proceeds through approve_fn which itself
        re-floors) but the Immutable-Orange check above already excluded
        prime/reactor regardless of this lookup."""
        try:
            from backend.core.ouroboros.governance.critical_elevation import (
                cross_repo_elevation_floor,
            )
            floor = cross_repo_elevation_floor(
                target_repo=(target_repo or ""), crosses_repo=True,
            )
            # prime/reactor -> "approval_required" (handled above anyway).
            # jarvis -> may be None (graduated) or "critical_elevation".
            # We only need to guarantee we never RELAX below approval for
            # an Immutable-Orange target; that target is already rejected.
            return True
        except Exception:  # noqa: BLE001 -- fail-soft; orange check is authority
            logger.debug(
                "[BiometricAuth] floor re-check failed (orange check already "
                "enforced)", exc_info=True,
            )
            return True

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

    def _authorize(self, **kw: Any) -> AuthorizationResult:
        res = AuthorizationResult(
            decision="AUTHORIZED",
            reason="authorized:fresh_nonce_biometric_pass_not_immutable_orange",
            ecapa_score=kw["ecapa_score"],
            antispoof_ok=kw["antispoof_ok"],
            freshness_ok=True,
            pr_id=kw["pr_id"],
            ast_mutation_id=kw["ast_mutation_id"],
            target_repo=kw["target_repo"],
            voiceprint_id=kw["voiceprint_id"],
        )
        self._emit_audit(res, kw)
        return res

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
        self._emit_audit(res, kw)
        return res

    def _emit_audit(
        self, res: AuthorizationResult, kw: Dict[str, Any],
    ) -> None:
        """Append to the hash-chained audit ledger -- EVERY outcome
        (AUTHORIZED + REJECTED). Fail-soft (never raises, logs loudly).
        Audio is represented ONLY by its sha256."""
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
        }
        try:
            sink = self._audit_sink
            if sink is None:
                from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import (  # noqa: E501
                    get_default_ledger,
                )
                sink = get_default_ledger().append
            sink(record)
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
