"""JARVIS-side signed cross-repo ripple emitter (Slice 97 Stage 1).

Composes the EXISTING crypto substrate; does not rewrite it.

  * Signs via the portable contract's ``sign_ripple`` (which is the
    byte-identical HMAC-SHA256 wire format of ``aegis.lease._encode_token``;
    a cross-compat test proves emitter output verifies under the portable
    ``verify_ripple`` AND that ``aegis.lease._encode_token`` of the same
    canonical dict produces the same token).
  * Resolves the cross-repo PSK from env (``JARVIS_CROSS_REPO_EMIT_PSK``) —
    never module-level.
  * Publishes a durable, immutable receipt to ``.jarvis/cross_repo_ripples.jsonl``
    and best-effort onto the existing ``CrossRepoEventBus``.

PREDICTIONS, NOT REQUESTS: the emitted ripple is a NOTIFICATION carrying
STRINGS (kind/intent/hash), never code.  Consumers verify + decide; they
never execute.

§33.1: master flag ``JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED`` default-FALSE.
When off, ``emit_ripple`` is inert (returns a DISABLED EmitResult, writes
nothing).

Authority-asymmetry: this module imports stdlib + the portable contract +
(lazy) ``aegis.lease`` / ``cross_repo``.  It NEVER imports orchestrator /
iron_gate / policy / change_engine / auto_committer.  Async, never-raises,
best-effort.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.ouroboros.cross_repo_mesh.ripple_contract import (
    RIPPLE_SCHEMA_VERSION,
    RipplePayload,
    VerifyVerdict,
    sign_ripple,
)

logger = logging.getLogger("Ouroboros.CrossRepoMesh.Emitter")


# §33.1 master flag — default FALSE.
_MASTER_FLAG = "JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED"
_PSK_ENV = "JARVIS_CROSS_REPO_EMIT_PSK"
_DEFAULT_TTL_S = 3600.0
_DEFAULT_LEDGER = ".jarvis/cross_repo_ripples.jsonl"


def _emit_enabled() -> bool:
    return os.getenv(_MASTER_FLAG, "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _resolve_psk() -> Optional[bytes]:
    """Resolve the cross-repo PSK from env. ``bytes`` — never module-level.

    Returns ``None`` if unset/empty (emitter then no-ops rather than signing
    with a degenerate key).
    """
    raw = os.getenv(_PSK_ENV, "")
    if not raw:
        return None
    return raw.encode("utf-8")


def _ledger_path() -> Path:
    return Path(os.getenv("JARVIS_CROSS_REPO_RIPPLE_LEDGER", _DEFAULT_LEDGER))


# ---------------------------------------------------------------------------
# Result type.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmitResult:
    """Outcome of an emit attempt. Best-effort; carries provenance only."""

    verdict: VerifyVerdict
    token: Optional[str] = None
    nonce: Optional[str] = None
    ripple_kind: Optional[str] = None
    ledger_written: bool = False
    bus_published: bool = False
    detail: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Builder — deterministic given now_unix; fresh nonce per ripple.
# ---------------------------------------------------------------------------


def _canonical_payload_sha256(payload_obj: Any) -> str:
    """SHA-256 over the canonical JSON of the underlying changed object.

    Deterministic (sorted keys, compact separators). Non-JSON-able objects
    fall back to ``repr`` so the builder never raises.
    """
    try:
        raw = json.dumps(
            payload_obj, separators=(",", ":"), sort_keys=True, default=str
        ).encode("utf-8")
    except (TypeError, ValueError):
        raw = repr(payload_obj).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_ripple(
    kind: Any,
    intent: str,
    payload_obj: Any,
    *,
    now_unix: float,
    source_repo: str = "jarvis",
    ttl_s: float = _DEFAULT_TTL_S,
) -> RipplePayload:
    """Build a ``RipplePayload`` notification.

    Computes ``payload_sha256`` over the canonical underlying object and a
    fresh ``nonce`` (``secrets.token_hex``).  ``now_unix`` is injected (no
    hidden ``time.time()`` nondeterminism, so tests can pin freshness).

    ``kind`` accepts a ``RippleKind`` enum or a plain string — both are
    normalized to the string value (the ripple carries a STRING, never code).
    """
    kind_str = getattr(kind, "value", kind)
    return RipplePayload(
        schema_version=RIPPLE_SCHEMA_VERSION,
        ripple_kind=str(kind_str),
        source_repo=str(source_repo),
        intent=str(intent),
        payload_sha256=_canonical_payload_sha256(payload_obj),
        nonce=secrets.token_hex(16),
        issued_at_unix=float(now_unix),
        ttl_s=float(ttl_s),
    )


# ---------------------------------------------------------------------------
# Durable immutable receipt (append-only JSONL).
# ---------------------------------------------------------------------------


def _write_receipt(token: str, payload: RipplePayload) -> bool:
    """Append an immutable receipt line. Best-effort; never raises."""
    try:
        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "token": token,
            "ripple_kind": payload.ripple_kind,
            "source_repo": payload.source_repo,
            "nonce": payload.nonce,
            "issued_at_unix": payload.issued_at_unix,
            "ttl_s": payload.ttl_s,
            "payload_sha256": payload.payload_sha256,
            "schema_version": payload.schema_version,
            "recorded_at_unix": time.time(),
        }
        line = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ripple receipt write failed: %s", exc)
        return False


async def _publish_bus(token: str, payload: RipplePayload) -> bool:
    """Best-effort publish onto the existing CrossRepoEventBus. Never raises.

    Lazy import — keeps the emitter's import graph free of the file-bus at
    module load and preserves authority asymmetry.
    """
    try:
        from backend.core.ouroboros.cross_repo import (
            CrossRepoEvent,
            CrossRepoEventBus,
            EventType,
            RepoType,
        )

        bus = CrossRepoEventBus()
        event = CrossRepoEvent(
            id=f"ripple_{payload.nonce[:12]}",
            type=EventType.CROSS_REPO_CORRELATION,
            source_repo=RepoType.JARVIS,
            target_repo=None,
            payload={
                "ripple_token": token,
                "ripple_kind": payload.ripple_kind,
                "schema_version": payload.schema_version,
            },
        )
        await bus.emit(event)
        return True
    except Exception as exc:
        logger.debug("ripple bus publish skipped/failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Emit — async, never-raises, best-effort.
# ---------------------------------------------------------------------------


async def emit_ripple(
    payload: RipplePayload, *, publish_bus: bool = False
) -> EmitResult:
    """Sign + persist a ripple NOTIFICATION. Best-effort; never raises.

    §33.1: inert when ``JARVIS_CROSS_REPO_RIPPLE_EMIT_ENABLED`` is off —
    returns a DISABLED result and writes nothing.

    Signs via the portable ``sign_ripple`` (byte-identical to
    ``aegis.lease._encode_token``).  The output verifies under the portable
    ``verify_ripple`` — proven in the cross-compat test.
    """
    if not _emit_enabled():
        return EmitResult(verdict=VerifyVerdict.DISABLED, detail="master_off")

    psk = _resolve_psk()
    if psk is None:
        # No key → cannot sign. Treat as disabled (never raise, never emit
        # an unsigned ripple).
        return EmitResult(
            verdict=VerifyVerdict.DISABLED, detail="psk_unset"
        )

    try:
        token = sign_ripple(payload, psk)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ripple sign failed: %s", exc)
        return EmitResult(verdict=VerifyVerdict.DISABLED, detail=f"sign_error:{exc}")

    ledger_written = _write_receipt(token, payload)

    bus_published = False
    if publish_bus:
        bus_published = await _publish_bus(token, payload)

    return EmitResult(
        verdict=VerifyVerdict.VERIFIED,
        token=token,
        nonce=payload.nonce,
        ripple_kind=payload.ripple_kind,
        ledger_written=ledger_written,
        bus_published=bus_published,
        detail="emitted",
    )


# ---------------------------------------------------------------------------
# Shipped-code invariants (mirrors the repo convention; never raises).
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """AST pins for Slice 97 Stage 1. NEVER raises (returns [] on failure).

    Pins:
      1. ``ripple_contract_stdlib_only`` — the portable contract imports no
         ``backend.*`` (so siblings can vendor it verbatim).
      2. ``ripple_contract_no_exec`` — no eval/exec/subprocess/__import__ in
         the portable contract (predictions, not requests).
      3. ``emitter_authority_asymmetry`` — the emitter never imports
         orchestrator/iron_gate/policy/change_engine/auto_committer.
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:
        return []

    import ast as _ast

    contract_target = (
        "backend/core/ouroboros/cross_repo_mesh/ripple_contract.py"
    )
    emitter_target = (
        "backend/core/ouroboros/cross_repo_mesh/ripple_emitter.py"
    )

    _BANNED_EMITTER_IMPORTS = (
        "orchestrator",
        "iron_gate",
        "policy",
        "change_engine",
        "auto_committer",
    )
    _BANNED_EXEC_NAMES = {"eval", "exec", "compile", "__import__"}

    def _validate_contract_stdlib_only(tree: _ast.AST, source: str) -> tuple:
        del source
        for node in _ast.walk(tree):
            mod = None
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name.startswith("backend"):
                        return (f"contract imports backend.*: {alias.name}",)
                continue
            if mod and mod.startswith("backend"):
                return (f"contract imports backend.*: {mod}",)
        return ()

    def _validate_contract_no_exec(tree: _ast.AST, source: str) -> tuple:
        del source
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = node.func
                if isinstance(fn, _ast.Name) and fn.id in _BANNED_EXEC_NAMES:
                    return (f"contract has exec-family call: {fn.id}",)
                if (
                    isinstance(fn, _ast.Attribute)
                    and isinstance(fn.value, _ast.Name)
                    and fn.value.id == "subprocess"
                ):
                    return ("contract has subprocess call",)
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                names = []
                if isinstance(node, _ast.Import):
                    names = [a.name for a in node.names]
                else:
                    names = [node.module or ""]
                for n in names:
                    if n.split(".")[0] == "subprocess":
                        return ("contract imports subprocess",)
        return ()

    def _validate_emitter_authority(tree: _ast.AST, source: str) -> tuple:
        del source
        for node in _ast.walk(tree):
            mods = []
            if isinstance(node, _ast.ImportFrom):
                mods = [node.module or ""]
            elif isinstance(node, _ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                for banned in _BANNED_EMITTER_IMPORTS:
                    if mod.endswith(banned) or f".{banned}" in mod or mod == banned:
                        return (f"emitter imports banned authority: {mod}",)
        return ()

    return [
        ShippedCodeInvariant(
            invariant_name="ripple_contract_stdlib_only",
            target_file=contract_target,
            description=(
                "Portable ripple contract imports no backend.* (vendorable)."
            ),
            validate=_validate_contract_stdlib_only,
        ),
        ShippedCodeInvariant(
            invariant_name="ripple_contract_no_exec",
            target_file=contract_target,
            description=(
                "Portable ripple contract has no exec/eval/subprocess path "
                "(predictions, not requests)."
            ),
            validate=_validate_contract_no_exec,
        ),
        ShippedCodeInvariant(
            invariant_name="emitter_authority_asymmetry",
            target_file=emitter_target,
            description=(
                "Emitter never imports orchestrator/iron_gate/policy/"
                "change_engine/auto_committer."
            ),
            validate=_validate_emitter_authority,
        ),
    ]


__all__ = [
    "EmitResult",
    "build_ripple",
    "emit_ripple",
    "register_shipped_invariants",
]
