"""CommitAuthorityDaemon — Slice 4 #1: one-click IDE grant refresh.

The operator ritual under active sovereignty is "issue/refresh an
``ide`` grant + presence before a commit session". From a shell
that is ``commit_authority_cli grant``; from the IDE there is no
shell. This daemon exposes that single ritual over a **local Unix
domain socket** so an IDE button (or a tiny helper) can refresh
the grant with no shell env — the same structural reason OCA's
secret/enable/presence live out-of-repo.

Why a Unix socket (not the TCP ``EventChannelServer``):
  * **No network surface at all.** ``AF_UNIX`` has no port; it
    cannot be reached off-box even by accident.
  * **Filesystem-permission-gated auth.** The socket is created
    ``0o600`` inside a ``0o700`` dir — only the owning uid can
    ``connect()``. That IS the authentication (the standard,
    robust per-user-socket model); no tokens to leak.

Hard security posture (all enforced here):
  * Master-flag default-FALSE (§33.1) — absent flag = no socket.
  * Bounded line protocol: one JSON request line (≤ cap), one
    JSON response, connection closed. No streaming, no sessions,
    no arbitrary verbs.
  * Closed verb taxonomy: ``refresh`` / ``status`` ONLY. No exec,
    no path traversal beyond the requested repo (verified via the
    canonical fingerprint), no revoke (kept on the deliberate
    CLI/REPL surface).
  * Per-connection timeout; oversized/malformed input → structured
    error + close; the server NEVER raises and survives every
    client.
  * Composes ONLY :mod:`operator_commit_authority` public surface
    (``issue_grant`` / ``verify_pre_commit`` /
    ``valid_operator_presence`` / ``master_enabled``) — ZERO
    parallel auth, ZERO parallel crypto. Archives ``grant_issue``
    via the Slice 3 #2 ring (best-effort).

Authority asymmetry (AST-pinned): no orchestrator / iron_gate /
providers / change_engine import; the closed verb set; the
``0o600`` socket chmod; no homegrown ``hmac`` / ``_sign``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("Ouroboros.CommitAuthorityDaemon")


COMMIT_AUTHORITY_DAEMON_SCHEMA_VERSION: str = (
    "commit_authority_daemon.v1"
)

_ENV_MASTER = "JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED"
_ENV_SOCK = "JARVIS_COMMIT_AUTHORITY_SOCK"
_ENV_CONN_TIMEOUT_S = "JARVIS_COMMIT_AUTHORITY_DAEMON_TIMEOUT_S"

_DEFAULT_SOCK_RELATIVE = ("commit_authority", "daemon.sock")
_DEFAULT_CONN_TIMEOUT_S = 5.0
_MIN_CONN_TIMEOUT_S = 0.5
_MAX_CONN_TIMEOUT_S = 30.0
_MAX_REQUEST_BYTES = 8192  # one bounded JSON line

# Closed verb taxonomy — anything else is rejected unread.
_VERBS = frozenset({"refresh", "status"})


# ---------------------------------------------------------------------------
# Config (env-driven; NEVER raises)
# ---------------------------------------------------------------------------


def daemon_enabled() -> bool:
    """Master switch, default-FALSE (§33.1). Absent flag → the
    daemon never binds a socket."""
    return os.environ.get(_ENV_MASTER, "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def socket_path() -> Path:
    """Out-of-repo socket location. Operator override via
    ``JARVIS_COMMIT_AUTHORITY_SOCK``. NEVER raises."""
    raw = os.environ.get(_ENV_SOCK, "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".jarvis" / Path(*_DEFAULT_SOCK_RELATIVE)


def conn_timeout_s() -> float:
    raw = os.environ.get(_ENV_CONN_TIMEOUT_S, "").strip()
    try:
        v = float(raw) if raw else _DEFAULT_CONN_TIMEOUT_S
    except (TypeError, ValueError):
        v = _DEFAULT_CONN_TIMEOUT_S
    return max(_MIN_CONN_TIMEOUT_S, min(_MAX_CONN_TIMEOUT_S, v))


# ---------------------------------------------------------------------------
# Request handling — pure composition of the OCA substrate
# ---------------------------------------------------------------------------


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": str(msg)[:300],
            "schema_version": COMMIT_AUTHORITY_DAEMON_SCHEMA_VERSION}


def _archive(kind: str, detail: dict) -> None:
    try:
        from backend.core.ouroboros.governance import (
            commit_authority_archive as _arch,
        )
        _arch.record(kind=kind, detail=detail)
    except Exception:  # noqa: BLE001
        pass


def handle_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pure request→response. Composes ONLY the OCA public
    surface. NEVER raises (caller wraps too, defense in depth)."""
    try:
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        return _err(f"OCA substrate unavailable: {type(exc).__name__}")

    verb = str(payload.get("verb", "")).strip().lower()
    if verb not in _VERBS:
        return _err(
            f"unknown verb {verb!r} (closed set: "
            f"{sorted(_VERBS)})"
        )

    repo_raw = str(payload.get("repo_root", "")).strip()
    if not repo_raw:
        return _err("repo_root required")
    try:
        repo_root = Path(repo_raw)
    except Exception:  # noqa: BLE001
        return _err("repo_root not a valid path")
    branch = str(payload.get("branch", "")).strip()

    if verb == "status":
        try:
            master = oca.master_enabled()
            presence_ok = oca.valid_operator_presence(
                repo_root, branch,
            )
            ch = oca.resolve_commit_channel(
                repo_root, branch, env_channel="",
            )
            verdict = oca.verify_pre_commit(
                oca.CommitAuthorityContext(
                    channel=ch.value,
                    repo_root=str(repo_root),
                    branch=branch,
                )
            )
            return {
                "ok": True, "verb": "status",
                "master_enabled": bool(master),
                "presence_valid": bool(presence_ok),
                "resolved_channel": ch.value,
                "dry_verdict": verdict.verdict.value,
                "schema_version":
                    COMMIT_AUTHORITY_DAEMON_SCHEMA_VERSION,
            }
        except Exception as exc:  # noqa: BLE001
            return _err(f"status failed: {type(exc).__name__}")

    # verb == "refresh": branch-bound ide grant + presence (the
    # ritual). branch defaults to the repo's current branch; an
    # empty whole-repo grant is refused structurally.
    try:
        if not branch:
            _root, branch = oca.resolve_repo_root_and_branch(
                repo_root
            )
        if not branch:
            return _err(
                "cannot resolve branch and none provided — "
                "refusing an empty whole-repo grant"
            )
        minutes = payload.get("minutes")
        ttl_s: Optional[int] = None
        if minutes is not None:
            try:
                ttl_s = max(1, int(minutes)) * 60
            except (TypeError, ValueError):
                return _err(f"bad minutes {minutes!r}")
        out = oca.issue_grant(
            channel="ide",
            operator_label="ide-daemon-refresh",
            ttl_s=ttl_s,
            branch=branch,
            repo_root=repo_root,
        )
        if not getattr(out, "ok", False):
            return _err(
                f"grant refused: {getattr(out, 'error', '?')}"
            )
        _archive("grant_issue", {
            "grant_id": out.grant_id, "channel": "ide",
            "branch": branch, "via": "daemon",
        })
        return {
            "ok": True, "verb": "refresh",
            "grant_id": out.grant_id,
            "channel": "ide", "branch": branch,
            "expires_at_unix": float(out.expires_at_unix),
            "schema_version":
                COMMIT_AUTHORITY_DAEMON_SCHEMA_VERSION,
        }
    except Exception as exc:  # noqa: BLE001
        return _err(f"refresh failed: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------


async def _handle_conn(
    reader: "asyncio.StreamReader",
    writer: "asyncio.StreamWriter",
) -> None:
    """One bounded request → one response → close. NEVER raises;
    a misbehaving client cannot take the server down."""
    try:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=conn_timeout_s(),
            )
        except asyncio.TimeoutError:
            resp = _err("request timeout")
            raw = b""
        else:
            if not raw:
                resp = _err("empty request")
            elif len(raw) > _MAX_REQUEST_BYTES:
                resp = _err("request too large")
            else:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        resp = _err("request must be a JSON object")
                    else:
                        resp = handle_request(payload)
                except Exception:  # noqa: BLE001
                    resp = _err("malformed JSON request")
        try:
            writer.write(
                (json.dumps(resp) + "\n").encode("utf-8")
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — server must survive
        logger.debug("[CommitAuthorityDaemon] conn error: %s", exc)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _prepare_socket_path() -> Optional[Path]:
    """Ensure a clean, 0o700-dir socket path. Removes a stale
    socket (a dead daemon's leftover) so restart is robust.
    Returns the path, or None on failure (NEVER raises)."""
    try:
        sp = socket_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(sp.parent, 0o700)
        except OSError:
            pass
        if sp.exists():
            # Stale only if nothing is listening — probe, then
            # unlink. A live daemon's socket answers connect().
            stale = True
            try:
                c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                c.settimeout(0.5)
                c.connect(str(sp))
                c.close()
                stale = False  # someone is listening — do NOT clobber
            except OSError:
                stale = True
            if not stale:
                logger.warning(
                    "[CommitAuthorityDaemon] socket already live "
                    "at %s — not rebinding", sp,
                )
                return None
            try:
                sp.unlink()
            except OSError:
                return None
        return sp
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CommitAuthorityDaemon] prepare path failed: %s", exc,
        )
        return None


async def serve(
    *, ready_evt: "Optional[asyncio.Event]" = None,
) -> Optional["asyncio.AbstractServer"]:
    """Bind the Unix socket (0o600) and serve until cancelled.
    Master-gated. Returns the server (or None when disabled /
    bind failed). NEVER raises."""
    if not daemon_enabled():
        logger.debug(
            "[CommitAuthorityDaemon] disabled (%s) — no socket",
            _ENV_MASTER,
        )
        return None
    sp = _prepare_socket_path()
    if sp is None:
        return None
    try:
        # Restrict the socket inode to the owning uid: umask during
        # bind + explicit chmod after (belt + suspenders).
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(
                _handle_conn, path=str(sp),
            )
        finally:
            os.umask(old_umask)
        try:
            os.chmod(sp, 0o600)
        except OSError as exc:
            # If we cannot lock the socket down, refuse to serve —
            # an unauthenticated authority socket is unacceptable.
            logger.error(
                "[CommitAuthorityDaemon] cannot chmod 0o600 %s: "
                "%s — refusing to serve", sp, exc,
            )
            server.close()
            try:
                sp.unlink()
            except OSError:
                pass
            return None
        logger.info(
            "[CommitAuthorityDaemon] listening at %s (0o600)", sp,
        )
        if ready_evt is not None:
            ready_evt.set()
        return server
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CommitAuthorityDaemon] serve failed: %s", exc,
        )
        return None


async def shutdown(
    server: Optional["asyncio.AbstractServer"],
) -> None:
    """Close the server + unlink the socket. Idempotent. NEVER
    raises."""
    try:
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        sp = socket_path()
        if sp.exists():
            try:
                sp.unlink()
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CommitAuthorityDaemon] shutdown error: %s", exc,
        )


__all__ = [
    "COMMIT_AUTHORITY_DAEMON_SCHEMA_VERSION",
    "daemon_enabled",
    "socket_path",
    "conn_timeout_s",
    "handle_request",
    "serve",
    "shutdown",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CommitAuthorityDaemon] register_flags degraded: %s",
            exc,
        )
        return 0
    tgt = (
        "backend/core/ouroboros/governance/commit_authority_daemon.py"
    )
    specs = [
        FlagSpec(
            name=_ENV_MASTER, type=FlagType.BOOL, default=False,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_MASTER}=true",
            description=(
                "Master switch for the commit-authority Unix-"
                "socket daemon (one-click IDE grant refresh). "
                "Default-FALSE per §33.1 — absent flag = no "
                "socket bound."
            ),
        ),
        FlagSpec(
            name=_ENV_SOCK, type=FlagType.STR,
            default="~/.jarvis/commit_authority/daemon.sock",
            category=Category.INTEGRATION, source_file=tgt,
            example=f"{_ENV_SOCK}=/run/user/1000/oca.sock",
            description=(
                "Override the daemon's Unix socket path "
                "(out-of-repo; created 0o600 in a 0o700 dir)."
            ),
        ),
        FlagSpec(
            name=_ENV_CONN_TIMEOUT_S, type=FlagType.FLOAT,
            default=_DEFAULT_CONN_TIMEOUT_S,
            category=Category.TIMING, source_file=tgt,
            example=f"{_ENV_CONN_TIMEOUT_S}=10.0",
            description=(
                "Per-connection read timeout (s). Floor 0.5, "
                "ceiling 30.0."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommitAuthorityDaemon] seed %s skipped: %s",
                spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Shipped-code invariants (AST pins)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Pins: composes the OCA substrate (no parallel auth/crypto —
    no ``hmac`` import, no ``_sign``/``_verify``/``compute_
    signature`` redefinition), closed verb taxonomy, the 0o600
    socket chmod is present, authority-asymmetric."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        violations: list = []
        forbidden_mods = (
            "orchestrator", "iron_gate", "providers",
            "change_engine", "candidate_generator",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name == "hmac":
                        violations.append(
                            "must NOT import hmac — compose the "
                            "OCA substrate (no parallel crypto)"
                        )
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                for f in forbidden_mods:
                    if f in mod:
                        violations.append(
                            f"authority-asymmetry violation: "
                            f"daemon must not import {f!r}"
                        )
            if isinstance(node, _ast.FunctionDef) and node.name in (
                "_sign", "_verify", "compute_signature",
            ):
                violations.append(
                    f"must NOT redefine crypto {node.name!r} — "
                    "compose operator_commit_authority"
                )
        # Composes the canonical grant/verify surface.
        if "issue_grant" not in source:
            violations.append(
                "refresh must compose issue_grant (no parallel "
                "grant logic)"
            )
        if "verify_pre_commit" not in source:
            violations.append(
                "status must compose verify_pre_commit"
            )
        # Closed verb taxonomy literal present + locked.
        if '"refresh"' not in source or '"status"' not in source:
            violations.append(
                "closed verb taxonomy {refresh,status} missing"
            )
        # The socket MUST be locked to the owning uid.
        if "0o600" not in source:
            violations.append(
                "socket must be chmod 0o600 — an unauthenticated "
                "authority socket is unacceptable"
            )
        return tuple(violations)

    tgt = (
        "backend/core/ouroboros/governance/commit_authority_daemon.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="commit_authority_daemon_compose_and_lock",
            target_file=tgt,
            description=(
                "Daemon composes the OCA substrate (no parallel "
                "auth/crypto, no hmac, no _sign/_verify redef), "
                "stays authority-asymmetric, keeps the closed "
                "{refresh,status} verb set, and chmod-0o600 locks "
                "the socket to the owning uid."
            ),
            validate=_validate,
        ),
    ]
