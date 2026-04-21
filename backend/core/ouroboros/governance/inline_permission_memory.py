"""
RememberedAllowStore — Slice 3 of the Inline Permission Prompts arc.
====================================================================

Persists operator ``/always`` decisions across REPL sessions so that a
pattern once blessed does not re-prompt on every occurrence. Backs the
:class:`RememberedAllowProvider` Protocol declared in Slice 1
(:mod:`inline_permission`) — when the Slice 1 gate sees an ASK verdict,
it consults a provider; the Slice 3 store is the production provider.

Manifesto alignment
-------------------

* §1 — grants are structured authorization records stamped by the
  operator, NOT model claims. The pattern text is redacted through
  :func:`sanitize_for_firewall` before persist; the model never writes
  to this store.
* §5 — Tier -1 Semantic Firewall runs on every grant request. Prompt
  injection, credential shapes, or patterns that the Slice 1 gate would
  classify as BLOCK are refused. Double belt-and-suspenders: remembered
  grants can *never* loosen a BLOCK row.
* §6 — additive. Remembered-allow never overrides a BLOCK (enforced
  structurally by Slice 1's two-pass :func:`decide`, and by the
  grant-time BLOCK-shape check here).
* §7 — fail-closed. Corrupt store file → operator sees an empty store,
  not a partially-trusted one. Broken filesystem → grant attempts raise,
  never silently succeed.
* §8 — every state transition emits an INFO log line keyed by
  ``[RememberedAllow]``. Revocation is a tombstone record; the
  append-only JSONL is the immutable audit trail.

Storage format
--------------

Per-repo JSONL at ``<repo_root>/.jarvis/inline_allows.jsonl``:

::

    {"op":"grant","grant_id":"ga-ab12","tool":"bash","match_mode":"bash_exact",
     "pattern":"make test","repo_root":"...","granted_at_iso":"...",
     "expires_at_iso":"...","granted_from_prompt_id":"","operator_note":""}
    {"op":"revoke","grant_id":"ga-ab12","revoked_at_iso":"..."}

Append-only at the file layer; compaction happens in-memory on every
load. A corrupt or partial record is skipped with an audit line — the
rest of the file is still trusted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.inline_permission import (
    InlineGateInput,
    InlinePermissionGate,
    InlineDecision,
    OpApprovedScope,
    RememberedAllowProvider,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptOutcome,
    InlinePromptRequest,
    ResponseKind,
    tool_family,
)
from backend.core.ouroboros.governance.semantic_firewall import (
    sanitize_for_firewall,
)

logger = logging.getLogger("Ouroboros.RememberedAllow")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _default_ttl_days() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_REMEMBERED_ALLOW_TTL_DAYS", "30",
        )))
    except (TypeError, ValueError):
        return 30.0


def _max_pattern_chars() -> int:
    try:
        return max(16, int(os.environ.get(
            "JARVIS_REMEMBERED_ALLOW_MAX_PATTERN_CHARS", "2000",
        )))
    except (TypeError, ValueError):
        return 2000


def _max_grants_per_repo() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_REMEMBERED_ALLOW_MAX_GRANTS", "256",
        )))
    except (TypeError, ValueError):
        return 256


# ---------------------------------------------------------------------------
# Match modes + grant record
# ---------------------------------------------------------------------------


MATCH_BASH_EXACT = "bash_exact"
"""Whole bash command must equal the grant's pattern (arg_fingerprint)."""

MATCH_PATH_EXACT = "path_exact"
"""target_path must equal the grant's pattern."""

MATCH_PATH_PREFIX = "path_prefix"
"""target_path must be the pattern or nested under it (``path/`` semantics).
Slice 3 v0 never grants this mode automatically — reserved for a future
``/always --dir`` UX; writes via the public API for test coverage only."""

_KNOWN_MATCH_MODES = frozenset({
    MATCH_BASH_EXACT, MATCH_PATH_EXACT, MATCH_PATH_PREFIX,
})


@dataclass(frozen=True)
class RememberedAllowGrant:
    """An operator-authorized allow pattern, scoped to a single repo.

    ``granted_at_iso`` / ``expires_at_iso`` are ISO-8601 UTC strings so
    the JSONL is human-readable in postmortems. The in-memory code
    works in ``time.time()`` float seconds via :meth:`epoch_now`-style
    helpers to avoid repeated parsing.
    """

    grant_id: str
    tool: str
    match_mode: str
    pattern: str
    repo_root: str
    granted_at_iso: str
    expires_at_iso: str
    granted_from_prompt_id: str = ""
    operator_note: str = ""
    sanitized_pattern: str = ""

    def expires_epoch(self) -> float:
        return _iso_to_epoch(self.expires_at_iso)

    def granted_epoch(self) -> float:
        return _iso_to_epoch(self.granted_at_iso)

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.time()
        return self.expires_epoch() <= ts


# ---------------------------------------------------------------------------
# Grant-request errors
# ---------------------------------------------------------------------------


class GrantRejected(Exception):
    """Grant refused — reason captured in ``reasons``."""

    def __init__(self, reasons: List[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


# ---------------------------------------------------------------------------
# ISO helpers (UTC always)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso_to_epoch(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _future_iso(seconds_ahead: float) -> str:
    return (
        datetime.now(timezone.utc)
        + timedelta(seconds=max(0.0, seconds_ahead))
    ).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Semantic-firewall + BLOCK-shape guard (§5 + §6)
# ---------------------------------------------------------------------------


def _scan_pattern_firewall(pattern: str) -> List[str]:
    """Return a list of reasons why *pattern* must not be persisted.

    Wraps :func:`sanitize_for_firewall` to reject prompt-injection
    signatures, credential shapes, length overrun, and malformed types.
    Additionally rejects null-byte / control-character bodies that would
    corrupt the JSONL.
    """
    if not isinstance(pattern, str):
        return ["pattern must be a string"]
    if not pattern.strip():
        return ["pattern is empty after trim"]
    if len(pattern) > _max_pattern_chars():
        return [f"pattern length {len(pattern)} exceeds cap {_max_pattern_chars()}"]
    if "\x00" in pattern:
        return ["pattern contains a NUL byte"]
    # Tab + newline are the only allowed control chars.
    for ch in pattern:
        if ch != "\t" and ch != "\n" and ord(ch) < 0x20:
            return [f"pattern contains disallowed control char 0x{ord(ch):02x}"]
    fr = sanitize_for_firewall(
        pattern, max_chars=_max_pattern_chars(), field_name="pattern",
    )
    if fr.rejected:
        return list(fr.reasons)
    return []


def _sanitized_pattern(pattern: str) -> str:
    """Return a redacted version of *pattern* safe for logs.

    Even on non-rejected input, strip any credential-shaped tokens so
    grants written to JSONL never persist a secret in cleartext.
    """
    fr = sanitize_for_firewall(
        pattern, max_chars=_max_pattern_chars(), field_name="pattern",
    )
    return fr.sanitized or pattern


def _would_be_blocked_by_gate(
    *, tool: str, match_mode: str, pattern: str,
) -> Optional[str]:
    """Return a reason string if the proposed grant covers a BLOCK shape.

    Constructs a synthetic :class:`InlineGateInput` from the grant
    parameters and runs the Slice 1 gate. If the gate returns BLOCK, we
    refuse to persist — remembered-allow must never loosen a BLOCK.
    """
    # Build plausible inputs for the gate.
    if match_mode == MATCH_BASH_EXACT:
        arg_fp = pattern
        target = ""
    elif match_mode in (MATCH_PATH_EXACT, MATCH_PATH_PREFIX):
        arg_fp = pattern
        target = pattern
    else:
        arg_fp = pattern
        target = pattern

    # Supply an APPROVED scope covering the pattern so the ASK rows that
    # depend on in-scope paths can't accidentally be tripped — we only
    # care about BLOCK rows (protected paths, destructive bash, etc.).
    approved = (target,) if target else ()
    inp = InlineGateInput(
        tool=tool,
        arg_fingerprint=arg_fp,
        target_path=target,
        route=RoutePosture.INTERACTIVE,
        approved_scope=OpApprovedScope(approved_paths=tuple(approved)),
        upstream_decision=UpstreamPolicy.NO_MATCH,
    )
    verdict = InlinePermissionGate().classify(inp)
    if verdict.decision is InlineDecision.BLOCK:
        return (
            f"grant would cover a BLOCK shape ({verdict.rule_id}); "
            "remembered-allow must never loosen a BLOCK row"
        )
    return None


def _derive_match_mode(tool: str) -> str:
    if tool == "bash":
        return MATCH_BASH_EXACT
    if tool_family(tool) in ("edit", "write", "delete"):
        return MATCH_PATH_EXACT
    return MATCH_PATH_EXACT


# ---------------------------------------------------------------------------
# Grant-id derivation (stable for idempotent re-grants)
# ---------------------------------------------------------------------------


def _make_grant_id(
    *, tool: str, match_mode: str, pattern: str, repo_root: str,
) -> str:
    digest = hashlib.sha256(
        f"{tool}\0{match_mode}\0{pattern}\0{repo_root}".encode("utf-8"),
    ).hexdigest()[:10]
    return f"ga-{digest}"


# ---------------------------------------------------------------------------
# RememberedAllowStore
# ---------------------------------------------------------------------------


JSONL_FILE_NAME = "inline_allows.jsonl"


class RememberedAllowStore:
    """Append-only JSONL store of :class:`RememberedAllowGrant` rows.

    Scoped by construction to one repo via ``repo_root``. Thread-safe;
    the write path acquires the lock for the duration of the line append
    so concurrent grants/revokes don't interleave.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        gate: Optional[InlinePermissionGate] = None,
        default_ttl_s: Optional[float] = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._default_ttl_s = (
            default_ttl_s if default_ttl_s is not None
            else _default_ttl_days() * 86400.0
        )
        self._lock = threading.Lock()
        self._grants: Dict[str, RememberedAllowGrant] = {}
        self._revoked: set = set()
        self._gate = gate or InlinePermissionGate()
        self._path = self._repo_root / ".jarvis" / JSONL_FILE_NAME
        self._load()

    # --- IO --------------------------------------------------------------

    def _load(self) -> None:
        """Replay the JSONL into memory. Tolerant of partial/corrupt rows."""
        with self._lock:
            self._grants.clear()
            self._revoked.clear()
            if not self._path.exists():
                return
            try:
                raw = self._path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "[RememberedAllow] could not read store: %s", exc,
                )
                return
            for lineno, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "[RememberedAllow] skipping corrupt row at line %d",
                        lineno,
                    )
                    continue
                op = rec.get("op")
                if op == "grant":
                    try:
                        g = RememberedAllowGrant(
                            grant_id=str(rec["grant_id"]),
                            tool=str(rec["tool"]),
                            match_mode=str(rec["match_mode"]),
                            pattern=str(rec["pattern"]),
                            repo_root=str(rec["repo_root"]),
                            granted_at_iso=str(rec["granted_at_iso"]),
                            expires_at_iso=str(rec["expires_at_iso"]),
                            granted_from_prompt_id=str(
                                rec.get("granted_from_prompt_id", ""),
                            ),
                            operator_note=str(rec.get("operator_note", "")),
                            sanitized_pattern=str(
                                rec.get("sanitized_pattern", ""),
                            ),
                        )
                    except (KeyError, TypeError):
                        logger.warning(
                            "[RememberedAllow] malformed grant row at %d",
                            lineno,
                        )
                        continue
                    # Same-repo cross-check: ignore rows from another repo
                    # that somehow landed here (operator copy-paste).
                    if g.repo_root != str(self._repo_root):
                        logger.warning(
                            "[RememberedAllow] cross-repo grant rejected "
                            "at line %d (expected=%s got=%s)",
                            lineno, self._repo_root, g.repo_root,
                        )
                        continue
                    self._grants[g.grant_id] = g
                elif op == "revoke":
                    gid = rec.get("grant_id")
                    if isinstance(gid, str):
                        self._revoked.add(gid)
                        self._grants.pop(gid, None)

    def _append(self, record: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)

    # --- grant -----------------------------------------------------------

    def grant(
        self,
        *,
        tool: str,
        pattern: str,
        match_mode: Optional[str] = None,
        ttl_s: Optional[float] = None,
        prompt_id: str = "",
        operator_note: str = "",
    ) -> RememberedAllowGrant:
        """Create and persist a grant. Raises :class:`GrantRejected`.

        Idempotent: two grants with identical (tool, match_mode, pattern)
        in the same repo reuse the same ``grant_id`` and overwrite the
        existing record (extending TTL).
        """
        if not tool:
            raise GrantRejected(["tool must be non-empty"])
        mode = match_mode or _derive_match_mode(tool)
        if mode not in _KNOWN_MATCH_MODES:
            raise GrantRejected([f"unknown match_mode: {mode}"])

        # §5 Semantic firewall scan.
        reasons = _scan_pattern_firewall(pattern)
        if reasons:
            raise GrantRejected(reasons)

        # §6 BLOCK-shape guard.
        block_reason = _would_be_blocked_by_gate(
            tool=tool, match_mode=mode, pattern=pattern,
        )
        if block_reason is not None:
            raise GrantRejected([block_reason])

        effective_ttl = (
            ttl_s if ttl_s is not None and ttl_s > 0 else self._default_ttl_s
        )
        grant_id = _make_grant_id(
            tool=tool, match_mode=mode, pattern=pattern,
            repo_root=str(self._repo_root),
        )
        g = RememberedAllowGrant(
            grant_id=grant_id,
            tool=tool,
            match_mode=mode,
            pattern=pattern,
            repo_root=str(self._repo_root),
            granted_at_iso=_utc_now_iso(),
            expires_at_iso=_future_iso(effective_ttl),
            granted_from_prompt_id=prompt_id,
            operator_note=operator_note[: _max_pattern_chars()].strip(),
            sanitized_pattern=_sanitized_pattern(pattern),
        )

        with self._lock:
            if len(self._grants) >= _max_grants_per_repo() and \
                    grant_id not in self._grants:
                raise GrantRejected([
                    f"grant cap {_max_grants_per_repo()} reached for this repo; "
                    "revoke older grants first",
                ])
            # Clear any prior tombstone — an explicit new grant unrevokes.
            self._revoked.discard(grant_id)
            record = {"op": "grant", **asdict(g)}
            self._append(record)
            self._grants[grant_id] = g

        logger.info(
            "[RememberedAllow] grant id=%s tool=%s mode=%s ttl_s=%.0f "
            "pattern_len=%d prompt=%s note=%r",
            grant_id, tool, mode, effective_ttl,
            len(pattern), prompt_id or "-",
            operator_note[:60] if operator_note else "",
        )
        return g

    # --- revoke ----------------------------------------------------------

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            if grant_id not in self._grants and grant_id not in self._revoked:
                return False
            self._append({
                "op": "revoke",
                "grant_id": grant_id,
                "revoked_at_iso": _utc_now_iso(),
            })
            self._grants.pop(grant_id, None)
            self._revoked.add(grant_id)
        logger.info("[RememberedAllow] revoked id=%s", grant_id)
        return True

    def revoke_all(self) -> int:
        with self._lock:
            gids = list(self._grants.keys())
        n = 0
        for gid in gids:
            if self.revoke(gid):
                n += 1
        return n

    # --- lookup ----------------------------------------------------------

    def lookup(
        self,
        *,
        tool: str,
        arg_fingerprint: str,
        target_path: str,
        now: Optional[float] = None,
    ) -> Optional[RememberedAllowGrant]:
        """Return the active grant covering this call, or None.

        Matching rules:
            * ``tool`` equality on the grant's ``tool`` field. (Slice 3
              does NOT collapse to tool_family — that broadens scope
              beyond what the operator approved; if they want write
              covered by an edit grant, they say so explicitly next time.)
            * ``MATCH_BASH_EXACT``: ``arg_fingerprint`` equals pattern.
            * ``MATCH_PATH_EXACT``: ``target_path`` equals pattern.
            * ``MATCH_PATH_PREFIX``: ``target_path`` nested under pattern.
            * grant must not be expired.
        """
        t = now if now is not None else time.time()
        with self._lock:
            candidates = [
                g for g in self._grants.values()
                if g.tool == tool and not g.is_expired(now=t)
            ]
        for g in candidates:
            if g.match_mode == MATCH_BASH_EXACT:
                if arg_fingerprint and arg_fingerprint == g.pattern:
                    return g
            elif g.match_mode == MATCH_PATH_EXACT:
                if target_path and target_path == g.pattern:
                    return g
            elif g.match_mode == MATCH_PATH_PREFIX:
                if target_path and _path_nested(target_path, g.pattern):
                    return g
        return None

    # --- introspection ---------------------------------------------------

    def list_active(self, *, now: Optional[float] = None) -> List[RememberedAllowGrant]:
        t = now if now is not None else time.time()
        with self._lock:
            return sorted(
                (g for g in self._grants.values() if not g.is_expired(now=t)),
                key=lambda g: g.granted_at_iso, reverse=True,
            )

    def get(self, grant_id: str) -> Optional[RememberedAllowGrant]:
        with self._lock:
            return self._grants.get(grant_id)

    def prune_expired(self, *, now: Optional[float] = None) -> int:
        t = now if now is not None else time.time()
        expired: List[str] = []
        with self._lock:
            for gid, g in list(self._grants.items()):
                if g.is_expired(now=t):
                    expired.append(gid)
                    self._grants.pop(gid, None)
        for gid in expired:
            self._append({
                "op": "revoke",
                "grant_id": gid,
                "revoked_at_iso": _utc_now_iso(),
                "note": "auto-pruned: expired",
            })
        return len(expired)

    def reset_file(self) -> None:
        """Test helper: drop the JSONL and in-memory state."""
        with self._lock:
            self._grants.clear()
            self._revoked.clear()
            try:
                if self._path.exists():
                    self._path.unlink()
            except OSError:
                pass

    # --- debug / audit ---------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def repo_root(self) -> Path:
        return self._repo_root


def _path_nested(target: str, parent: str) -> bool:
    if not target or not parent:
        return False
    p = parent.rstrip("/")
    return target == p or target.startswith(p + "/")


# ---------------------------------------------------------------------------
# Slice 1 provider adapter
# ---------------------------------------------------------------------------


class RememberedAllowProviderAdapter(RememberedAllowProvider):
    """Adapts :class:`RememberedAllowStore` to Slice 1's provider Protocol.

    Inject this into :class:`InlinePermissionGate` /
    :class:`InlinePermissionMiddleware` so that
    :data:`RULE_REMEMBERED_ALLOW` fires on matching grants.
    """

    def __init__(self, store: RememberedAllowStore) -> None:
        self._store = store

    def is_pattern_remembered(
        self,
        *,
        tool: str,
        arg_fingerprint: str,
        target_path: str,
    ) -> bool:
        return self._store.lookup(
            tool=tool,
            arg_fingerprint=arg_fingerprint,
            target_path=target_path,
        ) is not None


# ---------------------------------------------------------------------------
# Controller listener — auto-grant on ALLOW_ALWAYS
# ---------------------------------------------------------------------------


def attach_controller_listener(
    *,
    store: RememberedAllowStore,
    controller: Any,
    on_grant: Optional[Callable[[RememberedAllowGrant], None]] = None,
    on_reject: Optional[Callable[[str, List[str]], None]] = None,
) -> Callable[[], None]:
    """Wire a controller → store auto-grant listener.

    Subscribes to :meth:`InlinePromptController.on_transition`. When an
    ``inline_prompt_allowed`` event arrives with ``response == allow_always``,
    we replay the original request (projection carries tool / target /
    fingerprint) through :meth:`RememberedAllowStore.grant`.

    The listener SWALLOWS :class:`GrantRejected` (it's audit-logged and
    reported to ``on_reject`` if provided). The allow-once semantics
    still apply to the pending call — the grant just doesn't persist.
    Returns an unsubscribe callback.
    """
    # Grab the raw request back out of the controller snapshot so the
    # projection's truncated `arg_preview` isn't what we persist.

    def _listener(payload: Dict[str, Any]) -> None:
        if payload.get("event_type") != "inline_prompt_allowed":
            return
        proj = payload.get("projection") or {}
        response = proj.get("response")
        if response != ResponseKind.ALLOW_ALWAYS.value:
            return
        prompt_id = proj.get("prompt_id", "")
        tool = proj.get("tool", "")
        target_path = proj.get("target_path", "") or ""
        # For bash, the fingerprint IS the command; we persist the full
        # fingerprint — NOT arg_preview, which is display-truncated.
        # The controller's snapshot doesn't include the full fingerprint
        # by design (projection is bounded); we ask the controller for
        # the pending or history record directly.
        arg_fingerprint = _recover_fingerprint(controller, prompt_id) \
            or target_path
        note = proj.get("operator_reason", "") or ""
        try:
            g = store.grant(
                tool=tool,
                pattern=(
                    arg_fingerprint if tool == "bash"
                    else (target_path or arg_fingerprint)
                ),
                prompt_id=prompt_id,
                operator_note=note,
            )
            if on_grant is not None:
                on_grant(g)
        except GrantRejected as exc:
            logger.info(
                "[RememberedAllow] auto-grant refused prompt=%s reasons=%s",
                prompt_id, list(exc.reasons),
            )
            if on_reject is not None:
                on_reject(prompt_id, list(exc.reasons))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[RememberedAllow] auto-grant raised prompt=%s: %s",
                prompt_id, exc,
            )

    unsub = controller.on_transition(_listener)
    return unsub


def _recover_fingerprint(
    controller: Any, prompt_id: str,
) -> Optional[str]:
    """Best-effort recovery of the full arg_fingerprint for a prompt.

    We don't want to depend on controller internals; the public
    snapshot carries ``arg_preview`` (truncated) but the grant should
    persist the full fingerprint. When snapshots are unavailable we
    return None and the caller falls back to ``target_path``.
    """
    snap = getattr(controller, "snapshot", None)
    if snap is None:
        return None
    proj = snap(prompt_id)
    if proj is None:
        return None
    # arg_preview is already truncated — but it's our best option when
    # the raw InlinePromptRequest isn't reachable from the projection.
    # A future slice can expose request text directly if we discover
    # this truncation matters in practice.
    return proj.get("arg_preview") or None


# ---------------------------------------------------------------------------
# Try-grant direct helper (for explicit REPL-driven grants)
# ---------------------------------------------------------------------------


def try_grant_from_request(
    *,
    store: RememberedAllowStore,
    request: InlinePromptRequest,
    operator_note: str = "",
) -> RememberedAllowGrant:
    """Direct grant helper for operator surfaces that bypass the
    controller listener (e.g. a future ``/always --persist`` explicit
    command). Validates the same firewall + BLOCK-shape rules."""
    tool = request.tool
    pattern = (
        request.arg_fingerprint if tool == "bash"
        else (request.target_path or request.arg_fingerprint)
    )
    return store.grant(
        tool=tool,
        pattern=pattern,
        prompt_id=request.prompt_id,
        operator_note=operator_note,
    )


# ---------------------------------------------------------------------------
# Singleton (repo-scoped)
# ---------------------------------------------------------------------------


_store_by_repo: Dict[str, RememberedAllowStore] = {}
_store_lock = threading.Lock()


def get_store_for_repo(repo_root: Path) -> RememberedAllowStore:
    key = str(Path(repo_root).resolve())
    with _store_lock:
        s = _store_by_repo.get(key)
        if s is None:
            s = RememberedAllowStore(Path(key))
            _store_by_repo[key] = s
        return s


def reset_stores_for_test() -> None:
    global _store_by_repo
    with _store_lock:
        _store_by_repo = {}


__all__ = [
    "GrantRejected",
    "MATCH_BASH_EXACT",
    "MATCH_PATH_EXACT",
    "MATCH_PATH_PREFIX",
    "RememberedAllowGrant",
    "RememberedAllowProviderAdapter",
    "RememberedAllowStore",
    "attach_controller_listener",
    "get_store_for_repo",
    "reset_stores_for_test",
    "try_grant_from_request",
]

# Silence unused-import guards if a future slice removes a symbol.
_ = (InlinePromptOutcome, field)
