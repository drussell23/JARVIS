"""ProviderResponseCache — S1 of the Zero-Waste & Predictive Routing arc.

Stop paying for redundant tokens. A persistent, byte-budgeted,
repo-state-keyed cache of provider RESPONSE TRAJECTORIES. An exact
repeat of a request (same assembled prompt + model + route + repo
state) returns the cached :class:`GenerationResult` **without
calling the provider — cost $0.00**.

Composition (extends, never duplicates — PRD audit):
  * :mod:`prompt_cache` — imported as the acknowledged cache-family
    substrate; its canonical ``PromptCache._make_key`` SHA-256
    discipline is REUSED for key hashing, and its RLock + monotonic
    TTL eviction *shape* is mirrored. This module does NOT define a
    second ``PromptCache`` nor a parallel prompt-text cache; it
    adds the dimensions ``prompt_cache`` lacks: a RESPONSE value, a
    **serialized-byte LRU budget** (entry-count is insufficient —
    trajectories are large), and **repo-state-digest
    invalidation**.
  * :func:`cross_process_jsonl.flock_append_line` — the canonical
    persistence primitive; the on-disk append-log is replayed into
    the in-mem byte-LRU ring on first use (survives restart).

Correctness posture (load-bearing):
  * Key = ``SHA-256(prompt || model || route || repo_state_digest)``.
  * ``repo_state_digest`` = ``git HEAD`` + ``SHA-256(git diff HEAD)``
    (tracked staged+working). ANY staged/HEAD change re-keys → a
    real call (no stale-fix application).
  * **Fail-CLOSED on correctness:** if repo state is undeterminable
    the digest is a unique per-call nonce → guaranteed miss. We
    never serve a possibly-stale trajectory when we cannot prove
    the code is unchanged.
  * **Fail-OPEN on availability:** any miss / IO / parse / HMAC
    fault → caller's normal generate path. The cache NEVER raises
    into the provider and NEVER blocks an op.

Authority asymmetry (AST-pinned): imports stdlib +
``prompt_cache`` / ``cross_process_jsonl`` ONLY (the
``GenerationResult`` import is a TYPE_CHECKING/runtime-light dto) —
never ``orchestrator`` / ``iron_gate`` / ``candidate_generator``.

Master ``JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED`` default **FALSE**
— off ⇒ every entry point is a no-op and the provider path is
byte-identical to today. Graduation-gated per the PRD.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ProviderResponseCache")

PROVIDER_RESPONSE_CACHE_SCHEMA_VERSION: str = "provider_response_cache.v1"

_ENV_MASTER = "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED"
_ENV_SHADOW = "JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW"
_ENV_MAX_BYTES = "JARVIS_PROVIDER_CACHE_MAX_BYTES"
_ENV_TTL_S = "JARVIS_PROVIDER_CACHE_TTL_S"
_ENV_PATH = "JARVIS_PROVIDER_CACHE_PATH"

# 256 MiB in-mem default — 16GB-M1-safe. Clamped.
_DEFAULT_MAX_BYTES = 268_435_456
_MIN_MAX_BYTES = 1_048_576           # 1 MiB floor
_MAX_MAX_BYTES = 4_294_967_296       # 4 GiB ceiling
_DEFAULT_TTL_S = 86_400.0            # 24h (monotonic, like prompt_cache)
_DEFAULT_REL_PATH = (".jarvis", "provider_response_cache", "trajectories.jsonl")
# Bounded replay so a huge on-disk log can't blow boot RAM.
_MAX_REPLAY_LINES = 50_000


def response_cache_enabled() -> bool:
    """Master switch. **Graduated to default-TRUE (Slice 131)** — the cache is
    correctness-fail-closed (any git diff re-keys the entry, so a stale repo
    state can never serve a wrong response) and availability-fail-open, so
    default-on only ever ELIMINATES redundant identical-context calls. Re-read
    each call so a flip hot-reverts. NEVER raises."""
    return os.environ.get(_ENV_MASTER, "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def shadow_mode_enabled() -> bool:
    """Independent of master. When BOTH master ON AND shadow ON, a
    cache HIT is logged as ``[PRC] SHADOW_HIT cost_would_have_saved=$X``
    and the request still falls through to ``produce()`` (no behavior
    change to the upstream provider). When master is OFF, shadow is
    a no-op regardless of this flag (master takes precedence).
    Default-FALSE (PRD §10.10). Re-read each call so a flip hot-
    reverts. NEVER raises."""
    return os.environ.get(_ENV_SHADOW, "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def cache_max_bytes() -> int:
    raw = os.environ.get(_ENV_MAX_BYTES, "").strip()
    try:
        v = int(raw) if raw else _DEFAULT_MAX_BYTES
    except (TypeError, ValueError):
        v = _DEFAULT_MAX_BYTES
    return max(_MIN_MAX_BYTES, min(_MAX_MAX_BYTES, v))


def cache_ttl_s() -> float:
    raw = os.environ.get(_ENV_TTL_S, "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_TTL_S
    except (TypeError, ValueError):
        v = _DEFAULT_TTL_S
    return max(1.0, v)


def _cache_path() -> Path:
    raw = os.environ.get(_ENV_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return Path(*_DEFAULT_REL_PATH)


# ---------------------------------------------------------------------------
# Closed lookup taxonomy
# ---------------------------------------------------------------------------


class CacheLookupOutcome(str, enum.Enum):
    """Closed taxonomy. ``SEMANTIC_HIT`` is RESERVED for the S1.x
    semantic tier and is never produced in v1 (exact-only)."""

    EXACT_HIT = "exact_hit"
    SEMANTIC_HIT = "semantic_hit"            # reserved (S1.x)
    MISS = "miss"
    DISABLED = "disabled"
    INVALIDATED_REPO_CHANGE = "invalidated_repo_change"
    FAULT_FAIL_OPEN = "fault_fail_open"
    SHADOW_HIT_PASSTHROUGH = "shadow_hit_passthrough"  # PRD §10.10 — observed but not acted on


# ---------------------------------------------------------------------------
# Repo-state digest (fail-CLOSED on correctness)
# ---------------------------------------------------------------------------


def _run_git(args: List[str], repo_root: Path, timeout_s: float = 5.0):
    try:
        return subprocess.run(
            ["git", *args], cwd=str(repo_root),
            capture_output=True, text=True, timeout=timeout_s,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None


def repo_state_digest(repo_root: Path) -> str:
    """``HEAD`` + ``SHA-256(git diff HEAD)`` (tracked staged+working).
    On ANY failure to determine state → a unique per-call nonce so
    the resulting key can NEVER match a prior entry (fail-closed on
    correctness: a wrong reuse is unacceptable; a miss is safe).
    NEVER raises."""
    try:
        head = _run_git(["rev-parse", "HEAD"], Path(repo_root))
        diff = _run_git(["diff", "HEAD"], Path(repo_root))
        if (
            head is None or head.returncode != 0
            or diff is None or diff.returncode != 0
        ):
            return "UNDETERMINED-" + uuid.uuid4().hex
        h = (head.stdout or "").strip()
        d = hashlib.sha256(
            (diff.stdout or "").encode("utf-8", "replace")
        ).hexdigest()
        return f"{h}:{d}"
    except Exception:  # noqa: BLE001
        return "UNDETERMINED-" + uuid.uuid4().hex


def _prefix_key(prompt: str, model: str, route: str) -> str:
    """Request identity WITHOUT repo state — composes the canonical
    ``prompt_cache`` key discipline (no parallel hasher)."""
    try:
        from backend.core.ouroboros.governance.prompt_cache import (
            PromptCache,
        )
        return PromptCache._make_key(
            str(prompt), f"{model}\x00{route}",
        )
    except Exception:  # noqa: BLE001
        # Last-resort stdlib SHA-256 (same algorithm prompt_cache
        # itself uses); never raise.
        raw = f"{prompt}\x00{model}\x00{route}"
        return hashlib.sha256(
            raw.encode("utf-8", "replace")
        ).hexdigest()


def compute_cache_key(
    prompt: str, model: str, route: str, repo_root: Path,
) -> Tuple[str, str]:
    """Return ``(full_key, prefix_key)``. ``full_key`` binds repo
    state; ``prefix_key`` lets :func:`lookup` distinguish a
    code-changed near-miss (INVALIDATED_REPO_CHANGE) from a true
    MISS. NEVER raises."""
    pref = _prefix_key(prompt, model, route)
    digest = repo_state_digest(repo_root)
    full = hashlib.sha256(
        f"{pref}\x00{digest}".encode("utf-8", "replace")
    ).hexdigest()
    return full, pref


# ---------------------------------------------------------------------------
# Cached trajectory (frozen, lossless roundtrip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedTrajectory:
    """Serializable projection of a GenerationResult. Stores the
    load-bearing payload (candidates + the JSON-able trajectory
    metadata); non-serializable tool objects are dropped (a partial
    replay of `candidates` is the value — fail-open)."""

    full_key: str
    prefix_key: str
    candidates: Tuple[Dict[str, Any], ...]
    provider_name: str
    model_id: str
    is_noop: bool
    prompt_preloaded_files: Tuple[str, ...]
    total_input_tokens: int
    total_output_tokens: int
    n_bytes: int
    # PRD §10.10: original ``cost_usd`` at store time. Powers shadow-mode
    # ``cost_would_have_saved`` telemetry. Additive: default 0.0 means
    # "unknown" (older serialized entries without this field roundtrip
    # cleanly as 0.0; their would-save is reported as $0.000000).
    original_cost_usd: float = 0.0
    created_at: float = field(default_factory=time.monotonic)
    schema_version: str = PROVIDER_RESPONSE_CACHE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "full_key": self.full_key,
            "prefix_key": self.prefix_key,
            "candidates": list(self.candidates),
            "provider_name": self.provider_name,
            "model_id": self.model_id,
            "is_noop": self.is_noop,
            "prompt_preloaded_files": list(self.prompt_preloaded_files),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "n_bytes": self.n_bytes,
            "original_cost_usd": self.original_cost_usd,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["CachedTrajectory"]:
        try:
            return cls(
                full_key=str(d["full_key"]),
                prefix_key=str(d.get("prefix_key", "")),
                candidates=tuple(d.get("candidates", []) or ()),
                provider_name=str(d.get("provider_name", "")),
                model_id=str(d.get("model_id", "")),
                is_noop=bool(d.get("is_noop", False)),
                prompt_preloaded_files=tuple(
                    d.get("prompt_preloaded_files", []) or ()
                ),
                total_input_tokens=int(d.get("total_input_tokens", 0)),
                total_output_tokens=int(d.get("total_output_tokens", 0)),
                n_bytes=int(d.get("n_bytes", 0)),
                # Additive field: missing in legacy entries → 0.0 fallback.
                original_cost_usd=float(d.get("original_cost_usd", 0.0) or 0.0),
                created_at=float(d.get("created_at", time.monotonic())),
                schema_version=str(
                    d.get("schema_version",
                          PROVIDER_RESPONSE_CACHE_SCHEMA_VERSION)
                ),
            )
        except Exception:  # noqa: BLE001
            return None


def _trajectory_from_generation_result(
    full_key: str, prefix_key: str, gr: Any,
) -> Optional[CachedTrajectory]:
    """Project a GenerationResult → CachedTrajectory. NEVER raises;
    returns None if it cannot be serialized (→ caller skips store,
    which is fail-safe: a missed store is just a future miss)."""
    try:
        cands = tuple(getattr(gr, "candidates", ()) or ())
        # Only cache JSON-serializable candidates (drop the rest —
        # partial is fine; correctness > completeness).
        try:
            payload = json.dumps(list(cands))
        except (TypeError, ValueError):
            return None
        return CachedTrajectory(
            full_key=full_key,
            prefix_key=prefix_key,
            candidates=cands,
            provider_name=str(getattr(gr, "provider_name", "")),
            model_id=str(getattr(gr, "model_id", "")),
            is_noop=bool(getattr(gr, "is_noop", False)),
            prompt_preloaded_files=tuple(
                getattr(gr, "prompt_preloaded_files", ()) or ()
            ),
            total_input_tokens=int(
                getattr(gr, "total_input_tokens", 0) or 0
            ),
            total_output_tokens=int(
                getattr(gr, "total_output_tokens", 0) or 0
            ),
            n_bytes=len(payload.encode("utf-8", "replace")),
            # PRD §10.10: capture original spend for shadow telemetry.
            # Clamp negatives to 0.0 defensively (provider should never
            # report negative cost; this guarantees the would-save log
            # never displays a misleading negative).
            original_cost_usd=max(
                0.0, float(getattr(gr, "cost_usd", 0.0) or 0.0),
            ),
        )
    except Exception:  # noqa: BLE001
        return None


def reconstruct_generation_result(traj: CachedTrajectory) -> Optional[Any]:
    """Rebuild a GenerationResult from a cached trajectory with
    ``cost_usd=0.0`` and a cache-served provider tag (telemetry can
    see it was free). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.op_context import (
            GenerationResult,
        )
        return GenerationResult(
            candidates=tuple(traj.candidates),
            provider_name=(traj.provider_name or "unknown") + "+cache",
            generation_duration_s=0.0,
            model_id=traj.model_id,
            is_noop=traj.is_noop,
            prompt_preloaded_files=tuple(traj.prompt_preloaded_files),
            total_input_tokens=traj.total_input_tokens,
            total_output_tokens=traj.total_output_tokens,
            cost_usd=0.0,
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Byte-budget LRU ring (the NEW dimension prompt_cache lacks)
# ---------------------------------------------------------------------------


class ProviderResponseCache:
    """In-mem byte-budgeted LRU of CachedTrajectory. Mirrors
    prompt_cache's RLock + monotonic-TTL discipline; eviction is by
    serialized-BYTE budget (drop-oldest), not entry count. NEVER
    raises out of any public method."""

    def __init__(
        self,
        *,
        max_bytes: Optional[int] = None,
        ttl_s: Optional[float] = None,
    ) -> None:
        self._max_bytes = (
            max_bytes if max_bytes is not None else cache_max_bytes()
        )
        self._ttl_s = ttl_s if ttl_s is not None else cache_ttl_s()
        self._items: "OrderedDict[str, CachedTrajectory]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._loaded = False

    # -- persistence (compose cross_process_jsonl) ----------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                p = _cache_path()
                if not p.exists():
                    return
                lines = p.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                for ln in lines[-_MAX_REPLAY_LINES:]:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        t = CachedTrajectory.from_dict(json.loads(ln))
                    except Exception:  # noqa: BLE001
                        continue
                    if t is not None:
                        self._put_locked(t, persist=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[PRC] replay degraded: %s", exc)

    def _persist(self, t: CachedTrajectory) -> None:
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_append_line,
            )
            p = _cache_path()
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
            flock_append_line(p, json.dumps(t.to_dict(), sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PRC] persist degraded: %s", exc)

    # -- internal -------------------------------------------------------

    def _expired(self, t: CachedTrajectory) -> bool:
        return (time.monotonic() - t.created_at) > self._ttl_s

    def _put_locked(
        self, t: CachedTrajectory, *, persist: bool,
    ) -> None:
        old = self._items.pop(t.full_key, None)
        if old is not None:
            self._bytes -= max(0, old.n_bytes)
        self._items[t.full_key] = t
        self._bytes += max(0, t.n_bytes)
        # Drop-oldest until within the byte budget.
        while self._bytes > self._max_bytes and self._items:
            _k, ev = self._items.popitem(last=False)
            self._bytes -= max(0, ev.n_bytes)
        if persist:
            self._persist(t)

    # -- public API -----------------------------------------------------

    def lookup(
        self, full_key: str, prefix_key: str,
    ) -> Tuple[CacheLookupOutcome, Optional[CachedTrajectory]]:
        try:
            self._ensure_loaded()
            with self._lock:
                t = self._items.get(full_key)
                if t is not None and not self._expired(t):
                    self._items.move_to_end(full_key)  # LRU bump
                    return CacheLookupOutcome.EXACT_HIT, t
                if t is not None:  # expired
                    self._items.pop(full_key, None)
                    self._bytes -= max(0, t.n_bytes)
                # Distinguish "same request, code changed" from a
                # cold miss (forensic; proves no stale-fix served).
                for other in self._items.values():
                    if other.prefix_key == prefix_key:
                        return (
                            CacheLookupOutcome.INVALIDATED_REPO_CHANGE,
                            None,
                        )
                return CacheLookupOutcome.MISS, None
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("[PRC] lookup fault (fail-open): %s", exc)
            return CacheLookupOutcome.FAULT_FAIL_OPEN, None

    def store(self, t: Optional[CachedTrajectory]) -> bool:
        try:
            if t is None:
                return False
            self._ensure_loaded()
            with self._lock:
                self._put_locked(t, persist=True)
            return True
        except Exception as exc:  # noqa: BLE001 — never block
            logger.debug("[PRC] store fault (ignored): %s", exc)
            return False

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._items),
                "bytes": self._bytes,
                "max_bytes": self._max_bytes,
                "ttl_s": self._ttl_s,
            }


_singleton: Optional[ProviderResponseCache] = None
_singleton_lock = threading.Lock()


def get_default_cache() -> ProviderResponseCache:
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ProviderResponseCache()
        return _singleton


def reset_default_cache_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None


# ---------------------------------------------------------------------------
# The ONE composable gate seam (providers call this; no deep surgery)
# ---------------------------------------------------------------------------


async def cached_or_generate(
    *,
    prompt: str,
    model: str,
    route: str,
    repo_root: Path,
    produce,
):
    """Pre-call gate. If disabled → just ``await produce()`` (byte-
    identical). Else: exact-key hit → return the reconstructed
    GenerationResult ($0.00, provider skipped); miss/invalidated/
    fault → ``await produce()`` then best-effort store on success.
    ``produce`` is a 0-arg async callable returning a
    GenerationResult. NEVER raises out of the cache logic — any
    cache fault degrades to ``produce()``.

    Returns ``(GenerationResult, CacheLookupOutcome)``."""
    if not response_cache_enabled():
        return await produce(), CacheLookupOutcome.DISABLED
    full: str = ""
    pref: str = ""
    outcome: CacheLookupOutcome = CacheLookupOutcome.MISS
    # PRD §10.10: shadow-mode flag captured ONCE per call so a mid-call
    # env flip cannot make us return cached on the HIT branch but skip
    # the store on the MISS branch (or vice-versa).
    shadow: bool = shadow_mode_enabled()
    try:
        full, pref = compute_cache_key(
            str(prompt), str(model), str(route), Path(repo_root),
        )
        cache = get_default_cache()
        outcome, traj = cache.lookup(full, pref)
        if outcome is CacheLookupOutcome.EXACT_HIT and traj is not None:
            if shadow:
                # Shadow mode: observe, log, but DO NOT return cached —
                # fall through to produce() so the upstream provider is
                # still hit (application state integrity guaranteed).
                # MISS-path post-store skipped (entry already present).
                logger.info(
                    "[PRC] SHADOW_HIT cost_would_have_saved=$%.6f "
                    "(model=%s route=%s)",
                    float(traj.original_cost_usd or 0.0), model, route,
                )
                outcome = CacheLookupOutcome.SHADOW_HIT_PASSTHROUGH
                # Intentional fall-through: do NOT return here.
            else:
                gr = reconstruct_generation_result(traj)
                if gr is not None:
                    logger.info(
                        "[PRC] EXACT_HIT — provider skipped, $0.00 "
                        "(model=%s route=%s)", model, route,
                    )
                    return gr, CacheLookupOutcome.EXACT_HIT
                # reconstruction failed → fall through to real call
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("[PRC] gate fault (fail-open): %s", exc)
        return await produce(), CacheLookupOutcome.FAULT_FAIL_OPEN
    # Miss / invalidated / reconstruction-failed / SHADOW_HIT_PASSTHROUGH
    # → real generation, then best-effort store (store-omission is
    # fail-safe). On SHADOW_HIT_PASSTHROUGH the entry already exists at
    # this key — skip the store to avoid an idempotent re-write thrash
    # against the LRU.
    gr = await produce()
    try:
        if (
            full
            and gr is not None
            and not getattr(gr, "is_noop", False)
            and outcome is not CacheLookupOutcome.SHADOW_HIT_PASSTHROUGH
        ):
            t = _trajectory_from_generation_result(full, pref, gr)
            get_default_cache().store(t)
    except Exception as exc:  # noqa: BLE001 — store never blocks
        logger.debug("[PRC] post-store skipped: %s", exc)
    return gr, outcome


__all__ = [
    "PROVIDER_RESPONSE_CACHE_SCHEMA_VERSION",
    "CacheLookupOutcome",
    "CachedTrajectory",
    "ProviderResponseCache",
    "response_cache_enabled",
    "shadow_mode_enabled",
    "cache_max_bytes",
    "cache_ttl_s",
    "repo_state_digest",
    "compute_cache_key",
    "reconstruct_generation_result",
    "cached_or_generate",
    "get_default_cache",
    "reset_default_cache_for_tests",
    "register_flags",
    "register_shipped_invariants",
]


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PRC] register_flags degraded: %s", exc)
        return 0
    tgt = "backend/core/ouroboros/governance/provider_response_cache.py"
    specs = [
        FlagSpec(
            name=_ENV_MASTER, type=FlagType.BOOL, default=False,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_MASTER}=true",
            description=(
                "Master for the zero-waste provider response cache. "
                "OFF (default, §33.1) ⇒ provider path byte-identical."
            ),
        ),
        FlagSpec(
            name=_ENV_SHADOW, type=FlagType.BOOL, default=False,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_SHADOW}=true",
            description=(
                "Shadow-mode telemetry (PRD §10.10). Independent of "
                "master. When master ON AND shadow ON, a cache HIT is "
                "logged as `[PRC] SHADOW_HIT cost_would_have_saved=$X` "
                "and the request still falls through to the upstream "
                "provider (no behavior change). Use to gather real-"
                "workload hit-rate evidence before any default-TRUE "
                "flip of the master."
            ),
        ),
        FlagSpec(
            name=_ENV_MAX_BYTES, type=FlagType.INT,
            default=_DEFAULT_MAX_BYTES, category=Category.CAPACITY,
            source_file=tgt, example=f"{_ENV_MAX_BYTES}=268435456",
            description=(
                "In-mem byte budget (LRU drop-oldest). 16GB-M1-safe "
                f"default 256MiB; clamped [{_MIN_MAX_BYTES},"
                f"{_MAX_MAX_BYTES}]."
            ),
        ),
        FlagSpec(
            name=_ENV_TTL_S, type=FlagType.FLOAT,
            default=_DEFAULT_TTL_S, category=Category.TIMING,
            source_file=tgt, example=f"{_ENV_TTL_S}=86400",
            description=(
                "Trajectory TTL seconds (monotonic, sleep-immune — "
                "same discipline as prompt_cache)."
            ),
        ),
    ]
    n = 0
    for s in specs:
        try:
            registry.register(s)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[PRC] seed %s skipped: %s", s.name, exc)
    return n


def register_shipped_invariants() -> list:
    """Pins: composes prompt_cache (no parallel PromptCache / no
    second _make_key), byte-budget LRU present, closed enum,
    authority-asymmetric, NEVER-raises gate."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        v: list = []
        if "prompt_cache" not in source:
            v.append("must compose prompt_cache (no parallel cache)")
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "PromptCache"
            ):
                v.append("must NOT define a parallel class PromptCache")
            if isinstance(node, _ast.FunctionDef) and (
                node.name == "_make_key"
            ):
                v.append(
                    "must NOT re-implement _make_key — reuse "
                    "PromptCache._make_key"
                )
            if isinstance(node, _ast.ImportFrom):
                m = node.module or ""
                for forbidden in (
                    "orchestrator", "iron_gate",
                    "candidate_generator",
                ):
                    if forbidden in m:
                        v.append(
                            f"authority-asymmetry: must not import "
                            f"{forbidden!r}"
                        )
        if "max_bytes" not in source or "_max_bytes" not in source:
            v.append("byte-budget LRU dimension absent")
        required = {
            "EXACT_HIT", "SEMANTIC_HIT", "MISS", "DISABLED",
            "INVALIDATED_REPO_CHANGE", "FAULT_FAIL_OPEN",
            # PRD §10.10 — observed-but-not-acted shadow outcome.
            "SHADOW_HIT_PASSTHROUGH",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "CacheLookupOutcome"
            ):
                seen = {
                    t.id for st in node.body
                    if isinstance(st, _ast.Assign)
                    for t in st.targets
                    if isinstance(t, _ast.Name)
                }
                if required - seen:
                    v.append(
                        f"CacheLookupOutcome missing "
                        f"{sorted(required - seen)}"
                    )
                if seen - required:
                    v.append(
                        f"CacheLookupOutcome unexpected "
                        f"{sorted(seen - required)}"
                    )
        gate = next(
            (n for n in _ast.walk(tree)
             if isinstance(n, _ast.AsyncFunctionDef)
             and n.name == "cached_or_generate"),
            None,
        )
        if gate is None:
            v.append("cached_or_generate gate missing")
        elif not any(
            isinstance(n, _ast.ExceptHandler)
            for n in _ast.walk(gate)
        ):
            v.append("cached_or_generate must be NEVER-raise")
        return tuple(v)

    return [
        ShippedCodeInvariant(
            invariant_name="provider_response_cache_composed_safe",
            target_file=(
                "backend/core/ouroboros/governance/"
                "provider_response_cache.py"
            ),
            description=(
                "S1 cache composes prompt_cache (no parallel "
                "PromptCache/_make_key), keeps the byte-budget LRU "
                "dimension, the closed CacheLookupOutcome taxonomy, "
                "authority-asymmetry, and a NEVER-raise gate."
            ),
            validate=_validate,
        ),
    ]
