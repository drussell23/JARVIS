"""Phase 1 Slice 1.1 — Deterministic Entropy Substrate.

Single source of truth for randomness across the Ouroboros pipeline.
Replaces ad-hoc ``random.random()``, ``secrets.token_bytes()``,
``uuid.uuid4()`` calls scattered across phases with a session-scoped,
replay-safe entropy stream.

Architectural rationale (PRD §24.10 Critical Path #1):

  Without deterministic entropy, no decision can be replayed. Bug
  reproduction is best-effort. Counterfactual analysis is impossible.
  RSI convergence proofs (Wang's Markov-chain framework) require
  determinism as a foundation. This module gives every operation a
  reproducible entropy stream keyed on (session_seed, op_id) — same
  inputs forever produce the same byte stream.

Three layers:

  1. **SessionEntropy** — per-session 64-bit seed. Auto-derived from
     ``os.urandom`` at session start, OR pinned via env override
     ``OUROBOROS_DETERMINISM_SEED``. The seed is persisted to
     ``.jarvis/determinism/<session-id>/seed.json`` (atomic
     temp+rename) so ``--replay <session-id>`` can restore it.

  2. **DeterministicEntropy** — per-op entropy stream. Derived from
     ``(session_seed, op_id)`` via stable BLAKE2b hash. Same op_id
     within a session always produces the same byte stream. Provides
     ``random()``, ``uniform(a, b)``, ``randint(a, b)``,
     ``choice(seq)``, ``randbytes(n)``, ``uuid4()``.

  3. **entropy_for(op_id)** — accessor. NEVER raises. Lazy
     instantiation. When the master flag is off, returns a
     non-deterministic ``random.Random()`` instance so legacy code
     that asks for entropy still works (gradient rollout).

Key invariants:

  * ``entropy_for(same_op_id)`` returns the SAME entropy state
    object within a process — calling ``.random()`` twice advances
    the stream. To rewind, call ``reset_for_op(op_id)``.
  * Cross-session reproducibility: same env-pinned seed + same
    op_id sequence + same call sequence → bit-for-bit identical
    output.
  * Master flag ``JARVIS_DETERMINISM_ENTROPY_ENABLED`` (default
    ``false`` until graduation). When off: ``entropy_for`` returns a
    fresh ``random.Random`` per call, replay impossible (legacy).

Authority invariants (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * NEVER raises out of any public method.
  * Pure stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random as _random
import secrets
import struct
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables (re-read at call time)
# ---------------------------------------------------------------------------


def entropy_enabled() -> bool:
    """``JARVIS_DETERMINISM_ENTROPY_ENABLED`` (default ``true`` —
    graduated in Phase 1 Slice 1.5).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_DETERMINISM_ENTROPY_ENABLED=false`` returns ``entropy_for``
    to fresh non-deterministic streams (legacy bit-for-bit behavior).

    When ``true``: ``entropy_for(op_id)`` returns a deterministic
    stream. Replay-safe. When ``false``: returns a fresh non-
    deterministic ``random.Random`` instance per call."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_ENTROPY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _seed_state_dir() -> Path:
    """``JARVIS_DETERMINISM_STATE_DIR`` (default
    ``.jarvis/determinism``). Per-session seed lives at
    ``<state_dir>/<session-id>/seed.json``."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_STATE_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(raw)


def _env_session_seed() -> Optional[int]:
    """``OUROBOROS_DETERMINISM_SEED`` operator override.

    When set, replaces the per-session auto-derived seed. Critical
    for replay sessions: ``OUROBOROS_DETERMINISM_SEED=<seed> python
    ouroboros_battle_test.py --replay <session-id>`` reproduces the
    exact entropy stream of the recorded session.

    Accepts decimal or hex (with ``0x`` prefix). Invalid values fall
    through to auto-seed (logs warning)."""
    raw = os.environ.get("OUROBOROS_DETERMINISM_SEED", "").strip()
    if not raw:
        return None
    try:
        if raw.lower().startswith("0x"):
            return int(raw, 16)
        return int(raw, 10)
    except (ValueError, TypeError):
        logger.warning(
            "[determinism] OUROBOROS_DETERMINISM_SEED=%r is not a "
            "valid integer; falling through to auto-seed", raw,
        )
        return None


# Schema version for the seed file — bump when shape changes
SEED_SCHEMA_VERSION = "session_seed.1"


# ---------------------------------------------------------------------------
# Atomic disk I/O (mirrors posture_store / dw_promotion_ledger pattern)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via temp+rename. NEVER
    raises out of the wrapper — caller's defensive try/except will
    catch transient OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# SessionEntropy — per-session 64-bit seed, persisted
# ---------------------------------------------------------------------------


class SessionEntropy:
    """Per-session seed manager.

    Lifecycle:
      1. ``ensure_seed(session_id)`` — first call derives or reads.
         Auto-derived from ``os.urandom(8)`` OR pinned via env.
         Persisted to disk for replay.
      2. ``seed_for_session(session_id)`` — fast accessor (cached).
      3. ``forget(session_id)`` — drops in-memory cache (test hook).

    Thread-safe via ``RLock``. NEVER raises from public methods —
    disk faults degrade to in-memory-only seed (replay impossible
    until next clean save)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Dict[str, int] = {}

    def ensure_seed(self, session_id: str) -> int:
        """Idempotent. Returns the seed for this session_id, deriving
        + persisting on first call. NEVER raises."""
        if not session_id or not session_id.strip():
            return 0
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
            # Try env override first (replay path)
            seed = _env_session_seed()
            if seed is None:
                # Try disk (recovering across process boundaries)
                seed = self._read_disk(session_id)
            if seed is None:
                # Auto-derive from os.urandom — 64 bits
                seed = struct.unpack(">Q", secrets.token_bytes(8))[0]
                self._write_disk(session_id, seed)
            self._cache[session_id] = seed
            return seed

    def seed_for_session(self, session_id: str) -> int:
        """Fast accessor. Calls ``ensure_seed`` if not cached.
        NEVER raises."""
        return self.ensure_seed(session_id)

    def forget(self, session_id: str) -> None:
        """Drop the cached seed. Test hook + operator-driven reset."""
        with self._lock:
            self._cache.pop(session_id, None)

    def reset_all_for_tests(self) -> None:
        """Test hook — clears the cache entirely. Production code
        MUST NOT call this."""
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _seed_path(self, session_id: str) -> Path:
        return _seed_state_dir() / session_id / "seed.json"

    def _read_disk(self, session_id: str) -> Optional[int]:
        p = self._seed_path(session_id)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(
                "[determinism] seed file corrupt at %s — re-deriving "
                "(%s)", p, exc,
            )
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != SEED_SCHEMA_VERSION:
            return None
        seed = payload.get("seed")
        if isinstance(seed, int) and seed >= 0:
            return seed
        return None

    def _write_disk(self, session_id: str, seed: int) -> None:
        p = self._seed_path(session_id)
        try:
            _atomic_write(p, json.dumps({
                "schema_version": SEED_SCHEMA_VERSION,
                "session_id": session_id,
                "seed": seed,
                # Do NOT persist the seed in hex too — operators who
                # cat the file shouldn't need to convert. Single source.
            }, sort_keys=True, indent=2))
        except OSError as exc:
            logger.warning(
                "[determinism] seed persist failed at %s: %s — replay "
                "for this session will be impossible until disk recovers",
                p, exc,
            )


# Module-level singleton — operators get one SessionEntropy per process.
# Tests should call ``_session_entropy_singleton.reset_all_for_tests()``
# via the test hook, NOT instantiate their own.
_session_entropy_singleton = SessionEntropy()


def get_session_entropy() -> SessionEntropy:
    """Public accessor for the singleton. Useful for tests + operator
    surfaces (e.g., ``/determinism`` REPL command)."""
    return _session_entropy_singleton


# ---------------------------------------------------------------------------
# DeterministicEntropy — per-op stream
# ---------------------------------------------------------------------------


class DeterministicEntropy:
    """Per-operation deterministic random stream.

    Wraps a seeded ``random.Random`` instance + adds UUID generation.
    Same construction inputs (seed) always produce the same byte
    stream. Stateful: calling ``.random()`` advances the cursor.

    Direct construction is supported but the canonical entry point
    is ``entropy_for(op_id)`` which derives the per-op seed from
    the session seed via stable hash."""

    __slots__ = ("_rng", "_seed")

    def __init__(self, seed: int) -> None:
        # Clamp + normalize to 64-bit unsigned space
        self._seed = int(seed) & 0xFFFF_FFFF_FFFF_FFFF
        self._rng = _random.Random(self._seed)

    @property
    def seed(self) -> int:
        """The original seed (immutable). Useful for tests + replay
        manifests."""
        return self._seed

    # --- Stream-advancing methods (all wrap _random.Random) ---

    def random(self) -> float:
        """Float in [0.0, 1.0). NEVER raises."""
        return self._rng.random()

    def uniform(self, a: float, b: float) -> float:
        """Float in [a, b]. NEVER raises on a > b — Python's
        random.uniform tolerates this."""
        try:
            return self._rng.uniform(a, b)
        except (TypeError, ValueError):
            return float(a)

    def randint(self, a: int, b: int) -> int:
        """Integer in [a, b]. NEVER raises on bad input — clamps."""
        try:
            return self._rng.randint(int(a), int(b))
        except (TypeError, ValueError):
            return int(a)

    def choice(self, seq: Any) -> Any:
        """Pick one element. NEVER raises on empty — returns None."""
        try:
            if not seq:
                return None
            return self._rng.choice(seq)
        except (TypeError, IndexError):
            return None

    def randbytes(self, n: int) -> bytes:
        """``n`` random bytes. NEVER raises on negative — clamps to 0."""
        n = max(0, int(n))
        try:
            # Python 3.9+ random.Random.randbytes
            return self._rng.randbytes(n)
        except AttributeError:
            # Fallback for older Pythons (defensive)
            return bytes(self._rng.getrandbits(8) for _ in range(n))

    def uuid4(self) -> uuid.UUID:
        """Deterministic UUID4 from the stream. Same stream position
        → same UUID. NEVER raises."""
        # uuid4 is just 16 random bytes with version+variant bits set
        # per RFC 4122. We reproduce that pattern from our stream.
        b = bytearray(self.randbytes(16))
        # Set version (4) — high nibble of byte 6
        b[6] = (b[6] & 0x0F) | 0x40
        # Set variant (RFC 4122) — high bits of byte 8
        b[8] = (b[8] & 0x3F) | 0x80
        return uuid.UUID(bytes=bytes(b))

    # --- Adapter for callers expecting a random.Random ---

    def as_random(self) -> _random.Random:
        """Return the underlying ``random.Random``. Useful for code
        that already accepts an injectable rng (e.g.,
        ``full_jitter_backoff_s(rng=...)``)."""
        return self._rng


# ---------------------------------------------------------------------------
# Per-op entropy derivation + accessor
# ---------------------------------------------------------------------------


def _derive_op_seed(session_seed: int, op_id: str) -> int:
    """Stable per-op seed via BLAKE2b. Independent of dict ordering,
    string interning, etc. Same inputs forever produce the same
    output. NEVER raises."""
    h = hashlib.blake2b(digest_size=8)
    h.update(struct.pack(">Q", int(session_seed) & 0xFFFF_FFFF_FFFF_FFFF))
    h.update(b"\x00")  # separator so concatenation is unambiguous
    h.update(str(op_id).encode("utf-8", errors="replace"))
    return struct.unpack(">Q", h.digest())[0]


# Per-process cache of (session_id, op_id) → DeterministicEntropy.
# Calling entropy_for the same op_id returns the SAME stateful object
# so the stream advances naturally across phase boundaries.
_op_entropy_cache: Dict[tuple, DeterministicEntropy] = {}
_op_entropy_lock = threading.RLock()


def entropy_for(
    op_id: str,
    *,
    session_id: Optional[str] = None,
) -> DeterministicEntropy:
    """Get the entropy stream for ``op_id``.

    When the master flag is on:
      * Returns a deterministic stream keyed on ``(session_seed,
        op_id)``. Same op_id within a session → same stream.

    When the master flag is off:
      * Returns a fresh non-deterministic ``DeterministicEntropy``
        seeded from ``os.urandom`` (legacy behavior preserved —
        callers don't break, just lose replay).

    Session resolution:
      * ``session_id=None`` → reads from
        ``OUROBOROS_BATTLE_SESSION_ID`` env (set by the harness),
        falls back to ``"default"`` if unset.
      * Explicit ``session_id`` overrides env.

    NEVER raises. Garbage ``op_id`` → returns a stream seeded on
    ``"unknown"``."""
    safe_op = (str(op_id).strip() if op_id else "") or "unknown"

    if not entropy_enabled():
        # Legacy mode: fresh non-deterministic stream every call.
        # Stream is independent per call (operators get the old
        # behavior bit-for-bit).
        return DeterministicEntropy(
            struct.unpack(">Q", secrets.token_bytes(8))[0]
        )

    # Determine session_id
    if session_id is None or not session_id.strip():
        session_id = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"

    cache_key = (session_id, safe_op)
    with _op_entropy_lock:
        cached = _op_entropy_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            session_seed = _session_entropy_singleton.ensure_seed(session_id)
        except Exception:  # noqa: BLE001 — defensive
            session_seed = struct.unpack(">Q", secrets.token_bytes(8))[0]
        op_seed = _derive_op_seed(session_seed, safe_op)
        ent = DeterministicEntropy(op_seed)
        _op_entropy_cache[cache_key] = ent
        return ent


def reset_for_op(
    op_id: str,
    *,
    session_id: Optional[str] = None,
) -> None:
    """Drop the cached entropy for ``op_id`` so the next
    ``entropy_for`` call rebuilds the stream from seed (rewinds).
    Useful for retry semantics where each retry should see the same
    stream prefix. NEVER raises."""
    safe_op = (str(op_id).strip() if op_id else "") or "unknown"
    if session_id is None or not session_id.strip():
        session_id = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"
    with _op_entropy_lock:
        _op_entropy_cache.pop((session_id, safe_op), None)


def reset_all_for_tests() -> None:
    """Drop ALL cached entropy + ALL cached session seeds. Production
    code MUST NOT call this."""
    with _op_entropy_lock:
        _op_entropy_cache.clear()
    _session_entropy_singleton.reset_all_for_tests()


__all__ = [
    "SEED_SCHEMA_VERSION",
    "DeterministicEntropy",
    "SessionEntropy",
    "entropy_enabled",
    "entropy_for",
    "get_session_entropy",
    "reset_all_for_tests",
    "reset_for_op",
]
