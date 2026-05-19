"""PersistentMaster — generic signed, out-of-repo master-enable.
================================================================

The problem this closes (generalized from OCA Slice 3 #0):

  An env-only master flag can NEVER be ON for a GUI git subprocess.
  Cursor / VS Code Source Control inherit no shell env, so any
  ``JARVIS_*_ENABLED`` env flag is invisible to the pre-commit
  hook they spawn. OCA solved this for its own master via a
  signed, out-of-repo enable record. ``ledger_sovereignty`` (and
  any future operator-graduated master) has the SAME need: it must
  be enable-able for the GUI path, not just for a shell that
  happens to export the var.

This module is the single, reusable substrate for that pattern —
factored out so ``ledger_sovereignty`` composes it now and OCA can
adopt it later WITHOUT churning the shipped, safety-critical
``operator_commit_authority`` module.

Design (zero crypto duplication, single source of truth):

  * Crypto: composes :func:`roadmap_reader.compute_signature` /
    :func:`verify_signature` — the canonical constant-time HMAC.
    NO parallel ``hmac`` anywhere (AST-pinned).
  * Secret: composes the ONE per-machine secret OCA already
    bootstraps (``operator_commit_authority._ensure_secret`` /
    ``_read_secret`` at ``~/.jarvis/commit_authority/secret``,
    0600, O_EXCL, out-of-repo). A second secret would be
    duplication AND a second thing to back up — there is exactly
    one machine secret.
  * Record: ``{"record": <canonical payload>, "signature": <hmac>}``
    at ``~/.jarvis/persistent_master/<flag_key>.json`` (0600,
    atomic replace). On verify the canonical payload is recomputed
    from the trusted fields so a tampered/extra field cannot ride
    an old signature (identical discipline to OCA's enable record).
  * Fail-closed: missing file / missing secret / malformed JSON /
    bad signature / ``enabled != True`` all → ``False`` (the
    default-FALSE safety posture is preserved — a hand-created
    empty file does NOT flip a gate).
  * NEVER raises out of any public function.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.PersistentMaster")


PERSISTENT_MASTER_SCHEMA_VERSION: str = "persistent_master.1"

_ENV_DIR = "JARVIS_PERSISTENT_MASTER_DIR"
_DEFAULT_DIR_RELATIVE = ("persistent_master",)  # under ~/.jarvis
_SAFE_KEY = re.compile(r"[^a-z0-9_]+")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _sanitize_key(flag_key: str) -> str:
    """Lowercase, collapse any non ``[a-z0-9_]`` run to a single
    ``_``. Keeps the on-disk filename a stable, injection-free
    token regardless of caller input. NEVER raises."""
    try:
        s = _SAFE_KEY.sub("_", str(flag_key).strip().lower()).strip("_")
        return s or "unnamed"
    except Exception:  # noqa: BLE001
        return "unnamed"


def enable_record_dir() -> Path:
    """Directory holding per-flag enable records. Lives OUTSIDE the
    repo (``~/.jarvis/persistent_master/``) — that out-of-repo,
    no-shell-env property is precisely what makes the GUI-git path
    work. Operator override via ``JARVIS_PERSISTENT_MASTER_DIR``.
    NEVER raises."""
    raw = os.environ.get(_ENV_DIR, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".jarvis" / Path(*_DEFAULT_DIR_RELATIVE)


def enable_record_path(flag_key: str) -> Path:
    """Absolute path of the signed enable record for ``flag_key``.
    NEVER raises."""
    try:
        return enable_record_dir() / f"{_sanitize_key(flag_key)}.json"
    except Exception:  # noqa: BLE001 — NEVER-raise contract
        return Path.home() / ".jarvis" / "persistent_master" / "unnamed.json"


# ---------------------------------------------------------------------------
# Composed canonical crypto + the ONE per-machine secret
# ---------------------------------------------------------------------------


def _read_secret() -> Optional[str]:
    """Compose OCA's single per-machine secret reader (no second
    secret — single source of truth). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.operator_commit_authority import (  # noqa: E501
            _read_secret as _oca_read_secret,
        )
        return _oca_read_secret()
    except Exception:  # noqa: BLE001
        return None


def _ensure_secret() -> Optional[str]:
    """Compose OCA's single per-machine secret bootstrap (0600,
    O_EXCL). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.operator_commit_authority import (  # noqa: E501
            _ensure_secret as _oca_ensure_secret,
        )
        return _oca_ensure_secret()
    except Exception:  # noqa: BLE001
        return None


def _sign(payload: Dict[str, Any], secret: str) -> str:
    """Compose the canonical HMAC. NEVER raises. ``""`` on
    unavailable crypto → callers treat as fail-closed."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            compute_signature,
        )
        return compute_signature(payload, secret)
    except Exception:  # noqa: BLE001
        return ""


def _verify(payload: Dict[str, Any], signature_hex: str, secret: str) -> bool:
    """Compose the canonical constant-time verify. NEVER raises.
    ``False`` (fail closed) on unavailable crypto."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            verify_signature,
        )
        return verify_signature(payload, signature_hex, secret)
    except Exception:  # noqa: BLE001
        return False


def _signed_payload(
    flag_key: str, issued_at_unix: float, operator_label: str,
) -> Dict[str, Any]:
    """Canonical dict the HMAC covers. Deterministic — recomputed
    from trusted fields on verify."""
    return {
        "enabled": True,
        "flag_key": _sanitize_key(flag_key),
        "issued_at_unix": float(issued_at_unix),
        "operator_label": str(operator_label),
        "schema_version": PERSISTENT_MASTER_SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_persistently_enabled(flag_key: str) -> bool:
    """Return ``True`` iff a valid, HMAC-signed enable record
    exists for ``flag_key``. NEVER raises. Missing file / missing
    secret / malformed JSON / bad signature / ``enabled != True``
    / flag-key mismatch all → ``False`` (fail closed; the
    default-FALSE safety posture is preserved)."""
    target = enable_record_path(flag_key)
    try:
        if not target.exists():
            return False
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(raw, dict):
        return False
    record = raw.get("record")
    signature = raw.get("signature")
    if not isinstance(record, dict) or not isinstance(signature, str):
        return False
    if record.get("enabled") is not True:
        return False
    key = _sanitize_key(flag_key)
    if str(record.get("flag_key", "")) != key:
        return False
    secret = _read_secret()
    if not secret:
        return False
    payload = _signed_payload(
        key,
        float(record.get("issued_at_unix", 0.0))
        if _is_number(record.get("issued_at_unix"))
        else 0.0,
        str(record.get("operator_label", "")),
    )
    return _verify(payload, signature, secret)


def enable_persistent_master(
    flag_key: str,
    operator_label: str,
    *,
    now_unix: Optional[float] = None,
) -> bool:
    """Operator-only: write the signed persistent enable record for
    ``flag_key`` (bootstraps the per-machine secret on first use).
    Atomic write, 0600. Returns ``True`` on success. NEVER raises."""
    label = str(operator_label or "").strip()
    if not label:
        return False
    key = _sanitize_key(flag_key)
    secret = _ensure_secret()
    if not secret:
        return False
    now = float(now_unix) if now_unix is not None else time.time()
    payload = _signed_payload(key, now, label)
    signature = _sign(payload, secret)
    if not signature:
        return False
    target = enable_record_path(key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"record": payload, "signature": signature},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except Exception:  # noqa: BLE001 — best effort
            pass
        os.replace(str(tmp), str(target))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PersistentMaster] enable write failed for %s at %s: %s",
            key, target, type(exc).__name__,
        )
        return False


def disable_persistent_master(flag_key: str) -> bool:
    """Operator-only: remove the persistent enable record for
    ``flag_key``. Master then reverts to env-only (default FALSE).
    Idempotent. Returns ``True`` iff the record is absent
    afterwards. NEVER raises."""
    target = enable_record_path(flag_key)
    try:
        if target.exists():
            target.unlink()
        return not target.exists()
    except Exception:  # noqa: BLE001
        return False


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


__all__ = [
    "PERSISTENT_MASTER_SCHEMA_VERSION",
    "enable_record_dir",
    "enable_record_path",
    "is_persistently_enabled",
    "enable_persistent_master",
    "disable_persistent_master",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Module-owned shipped_code_invariants (AST pins)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Pins: composes the canonical roadmap_reader HMAC (NO parallel
    ``hmac``/``hashlib`` signing), composes the ONE OCA per-machine
    secret (no second secret), and every public function is
    NEVER-raise (defensive try/except discipline)."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        _ = source
        violations: list = []

        # (1) No parallel crypto — canonical compute/verify only.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name in ("hmac",):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            "persistent_master must NOT import hmac "
                            "(compose roadmap_reader canonical HMAC)"
                        )
            if isinstance(node, _ast.ImportFrom):
                if (node.module or "") == "hmac":
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        "persistent_master must NOT import from hmac"
                    )

        # (2) Composes the canonical crypto + the OCA secret (the
        #     single source of truth — no second secret/crypto).
        if "compute_signature" not in source:
            violations.append(
                "must compose roadmap_reader.compute_signature"
            )
        if "verify_signature" not in source:
            violations.append(
                "must compose roadmap_reader.verify_signature"
            )
        if "operator_commit_authority" not in source:
            violations.append(
                "must compose the single OCA per-machine secret "
                "(no second machine secret)"
            )

        # (3) Every public function is NEVER-raise: each must
        #     contain at least one ExceptHandler.
        public = {
            "is_persistently_enabled",
            "enable_persistent_master",
            "disable_persistent_master",
            "enable_record_dir",
            "enable_record_path",
        }
        for fnode in _ast.walk(tree):
            if (
                isinstance(fnode, _ast.FunctionDef)
                and fnode.name in public
            ):
                has_try = any(
                    isinstance(n, _ast.ExceptHandler)
                    for n in _ast.walk(fnode)
                )
                if not has_try:
                    violations.append(
                        f"public fn {fnode.name!r} missing "
                        "defensive try/except (NEVER-raise contract)"
                    )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/persistent_master.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="persistent_master_canonical_crypto_single_secret",
            target_file=target,
            description=(
                "persistent_master composes the canonical "
                "roadmap_reader HMAC (no parallel hmac) + the ONE "
                "OCA per-machine secret (no duplicate secret); "
                "every public function is NEVER-raise."
            ),
            validate=_validate,
        ),
    ]
