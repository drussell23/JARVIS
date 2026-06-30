from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .dag_capability_token import CapabilityToken

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 1


def _enabled() -> bool:
    return os.environ.get("JARVIS_TOKEN_AUDIT_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _default_path() -> str:
    return os.environ.get("JARVIS_TOKEN_AUDIT_PATH", os.path.join(".jarvis", "token_audit.jsonl"))


def _max() -> int:
    try:
        return int(os.environ.get("JARVIS_TOKEN_AUDIT_MAX", "500"))
    except ValueError:
        return 500


def append_mint(token: CapabilityToken, *, path: Optional[str] = None) -> None:
    """Durably append a token-mint record. Fail-soft -- never raises.

    The HMAC ``sig`` is recorded as audit evidence; the SECRET is never written.
    """
    if not _enabled():
        return
    p = path if path is not None else _default_path()
    record = {
        "ts": time.time(),
        "schema_version": _SCHEMA_VERSION,
        "kind": token.kind.value,
        "op_id": token.op_id,
        "state_binding": token.state_binding,
        "prev_hash": token.prev_hash,
        "sig": token.sig,
        "payload": dict(token.payload),
    }
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        _trim(p)
    except Exception as exc:  # noqa: BLE001 -- audit is best-effort
        logger.warning("[TokenAudit] append failed: %s", exc)


def _trim(p: str) -> None:
    try:
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        cap = _max()
        if len(lines) > cap:
            with open(p, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-cap:])
    except Exception:  # noqa: BLE001
        pass


def read_audit(path: Optional[str] = None) -> List[Dict[str, Any]]:
    p = path if path is not None else _default_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []
