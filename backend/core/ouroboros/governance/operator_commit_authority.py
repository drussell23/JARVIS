# Testing for the IRON GATE - Commit blocked refusal mode revealed a major pain point: the human operator's commit path was completely separate from the autonomous commit path, with its own untracked bash hook and shell env var. The IDE GUI (Cursor Source Control) inherits no shell env, so it was always blocked by the token gate. OCA unifies the human and autonomous paths under one substrate, one verifier, and one verdict taxonomy — closing the seam and making the IDE GUI work without a hack.
"""
Operator Commit Authority (OCA) — unified human + autonomous commit gate
=========================================================================

**Why this exists** — the repo carried TWO commit-authorization paths
with incompatible plumbing:

  1. ``AutoCommitter`` (autonomous) already composes typed substrates:
     :mod:`ledger_sovereignty` (owned-worktree boundary),
     :mod:`governance_manifest` (hash-cap on governance/ drift),
     :mod:`gitignore_guard` (tracked-but-ignored breach).
  2. The human operator went through an *untracked* bash
     ``.git/hooks/pre-commit`` that required a shell env var
     (``JARVIS_AUTHORIZE_COMMIT_TOKEN``). GUI git (Cursor Source
     Control) does not inherit shell env → every IDE commit was
     refused with ``⛔ IRON GATE — Commit blocked``.

That is not a Cursor bug — it is an architectural seam: authorization
logic lived in an untracked bash hook + env var while the rest of O+V
uses typed substrates, append-only ledgers, and graduation flags. OCA
closes the seam: ONE verifier, ONE verdict taxonomy, every channel
(IDE GUI / terminal / REPL / autonomous daemon) routed through it.

Operator workflow (graduated state)
-----------------------------------
1. ``python3 scripts/install_hooks.py install``  (Slice 2 — versioned
   hook chain replaces the untracked bash wrapper).
2. Issue a time-bounded grant before an IDE session, either:
     * Serpent REPL: ``/commit grant 60``  (Slice 3), or
     * CLI: ``python3 -m backend.core.ouroboros.governance\
.commit_authority_cli grant --minutes 60``  (Slice 2).
3. Commit from Cursor freely until the grant TTL expires — no shell
   env export, because the grant lives in a signed file the Python
   verifier reads directly.
4. Autonomous soaks keep using worktree + sovereignty (unchanged);
   the autonomous channel does NOT need an operator grant — it is
   authorized by the :mod:`ledger_sovereignty` marker exactly as
   before.

Composition (zero duplication)
------------------------------
* :mod:`ledger_sovereignty` — autonomous channel ownership decision
  (we delegate, never reimplement).
* :mod:`governance_manifest` — governance/ drift hash-cap, composed
  via ``verify_governance_state`` / ``is_refusal_verdict``.
* :mod:`cross_process_jsonl` — append-only grant ledger writes
  (``flock_append_line``), the canonical cross-process primitive.
* :mod:`roadmap_reader` — HMAC sign/verify (``compute_signature`` /
  ``verify_signature``), the canonical constant-time crypto. A
  hand-edited grant line fails signature verification and is treated
  as no grant. **No parallel HMAC logic** is defined here — if the
  canonical crypto cannot be imported the verifier fails *closed*.
* :mod:`operation_mode` — read-only adaptive TTL (PLAN/ANALYZE modes
  shorten freshly-issued grants).

Authority asymmetry (AST-pinned)
--------------------------------
Substrate imports stdlib + the four composed substrates ONLY. It does
NOT import orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor / tool_executor.
Consumers (Slice 2 hook CLI + AutoCommitter) lazy-import THIS module.

Master flag ``JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED`` default-
**FALSE** per §33.1 — until graduated, ``verify_pre_commit`` returns
the ``DISABLED`` verdict and the hook chain behaves byte-identically
to the pre-substrate world. Pure substrate — public API NEVER raises.

Manifesto: §6 Iron Gate, §8 absolute observability, §33.1 default-
FALSE graduation, Reverse Russian Doll (operator authority at the
outer shell, O+V bounded inside).
"""
from __future__ import annotations

import ast
import enum
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION: str = "operator_commit_authority.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED"
_ENV_DEFAULT_TTL_S = "JARVIS_COMMIT_GRANT_DEFAULT_TTL_S"
_ENV_PLAN_TTL_S = "JARVIS_COMMIT_GRANT_PLAN_TTL_S"
_ENV_GRANTS_PATH = "JARVIS_COMMIT_AUTHORITY_GRANTS_PATH"
_ENV_SECRET_PATH = "JARVIS_COMMIT_AUTHORITY_SECRET_PATH"
_ENV_ENABLE_FILE = "JARVIS_COMMIT_AUTHORITY_ENABLE_FILE"
_ENV_PRESENCE_FILE = "JARVIS_COMMIT_AUTHORITY_PRESENCE_FILE"
_ENV_PRESENCE_TTL_S = "JARVIS_COMMIT_PRESENCE_TTL_S"
# Sovereign Execution Boundary (Stage A) — dedicated default-OFF sub-flag.
# Composes WITH the OCA master (the boundary only operates inside the OCA
# pre-commit path), but is its own switch so enabling OCA is byte-identical
# until the operator opts into the stricter "autonomous never commits in the
# primary tree / on main" rule. Off → the autonomous channel keeps its
# documented legacy contract unchanged.
_ENV_EXECUTION_BOUNDARY = "JARVIS_EXECUTION_BOUNDARY_ENABLED"

_DEFAULT_GRANTS_RELATIVE = ".jarvis/commit_authority/grants.jsonl"
_DEFAULT_SECRET_RELATIVE = ("commit_authority", "secret")  # under ~/.jarvis
_DEFAULT_ENABLE_RELATIVE = ("commit_authority", "enabled")  # under ~/.jarvis
_DEFAULT_PRESENCE_RELATIVE = (
    "commit_authority", "presence.json",
)  # under ~/.jarvis

_DEFAULT_TTL_S = 3600
_MIN_TTL_S = 60
_MAX_TTL_S = 86_400  # 24h ceiling
_DEFAULT_PLAN_TTL_S = 900  # PLAN/ANALYZE modes: shorter grants

# Operator-presence marker lifetime. Short by design: it is a
# "the operator is actively here this session" proof, re-minted
# every time the operator issues a grant / re-enables. An Agent
# cannot forge it (needs the per-machine secret + a deliberate
# operator entry point).
_DEFAULT_PRESENCE_TTL_S = 900  # 15 min
_MIN_PRESENCE_TTL_S = 60
_MAX_PRESENCE_TTL_S = 86_400  # 24h ceiling

# Multi-entry presence store bound. Presence is keyed by
# (repo_fingerprint, branch) so concurrent legitimate actors
# (operator on branch X, an autonomous-ish writer on branch Y,
# a whole-repo "" arming) COEXIST instead of clobbering a single
# global record — the production-lockout root fix. Bounded so the
# store can't grow unboundedly; drop-oldest by issued_at.
_ENV_PRESENCE_MAX_ENTRIES = "JARVIS_COMMIT_PRESENCE_MAX_ENTRIES"
_DEFAULT_PRESENCE_MAX_ENTRIES = 64
_MIN_PRESENCE_MAX_ENTRIES = 1
_MAX_PRESENCE_MAX_ENTRIES = 1000

_GIT_OP_TIMEOUT_S = 5.0

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})

_REVOKE_ALL_TOKEN = "*"


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 opt-in safety gate — default-**FALSE**.

    Master is ON iff EITHER the env flag is truthy OR a valid,
    HMAC-signed persistent enable record exists (Slice 3 #0). The
    persistent path is the load-bearing fix for the Cursor/VS Code
    Source Control button: a GUI git subprocess inherits **no shell
    env**, so an env-only master could never be ON for it — it would
    fall back to the legacy token gate and stay blocked. The
    out-of-repo signed enable file (mirroring how the per-machine
    secret already lives outside the repo) lets the hook subprocess
    resolve master=ON regardless of how git was spawned.

    Default remains **FALSE**: no env flag AND no valid enable record
    → byte-identical to the pre-substrate world. NEVER raises.
    """
    return _flag(_ENV_MASTER, default=False) or persistent_enabled()


def _read_clamped_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def default_ttl_s() -> int:
    """Default grant lifetime. Clamped to [60, 86_400]."""
    return _read_clamped_int(
        _ENV_DEFAULT_TTL_S, _DEFAULT_TTL_S, _MIN_TTL_S, _MAX_TTL_S
    )


def plan_mode_ttl_s() -> int:
    """Grant lifetime when the operator is in PLAN/ANALYZE mode —
    shorter by default (analysis sessions shouldn't leave a long
    commit window open). Clamped to [60, 86_400]."""
    return _read_clamped_int(
        _ENV_PLAN_TTL_S, _DEFAULT_PLAN_TTL_S, _MIN_TTL_S, _MAX_TTL_S
    )


# ===========================================================================
# Path resolution
# ===========================================================================


def _resolve_repo_root() -> Optional[Path]:
    """Walk up from this module to find the .git anchor. NEVER raises."""
    try:
        here = Path(__file__).resolve()
        for ancestor in (here, *here.parents):
            try:
                if (ancestor / ".git").exists():
                    return ancestor
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def grants_path() -> Path:
    """Canonical append-only grant ledger. Operator override via
    ``JARVIS_COMMIT_AUTHORITY_GRANTS_PATH``. NEVER raises."""
    raw = os.environ.get(_ENV_GRANTS_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    root = _resolve_repo_root()
    if root is None:
        return Path(_DEFAULT_GRANTS_RELATIVE)
    return root / _DEFAULT_GRANTS_RELATIVE


def secret_path() -> Path:
    """Per-machine HMAC secret location. Lives OUTSIDE the repo
    (``~/.jarvis/commit_authority/secret``) so it is never committed
    and never depends on a shell env export — that is precisely what
    makes the IDE-GUI path work. Operator override via
    ``JARVIS_COMMIT_AUTHORITY_SECRET_PATH``. NEVER raises."""
    raw = os.environ.get(_ENV_SECRET_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".jarvis" / Path(*_DEFAULT_SECRET_RELATIVE)


def enable_file_path() -> Path:
    """Persistent master-enable record location. Lives OUTSIDE the
    repo (``~/.jarvis/commit_authority/enabled``) so the gate's
    enablement does not depend on a shell env export — that is what
    makes the Cursor/VS Code Source Control button work (GUI git
    inherits no shell env). Operator override via
    ``JARVIS_COMMIT_AUTHORITY_ENABLE_FILE``. NEVER raises."""
    raw = os.environ.get(_ENV_ENABLE_FILE, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".jarvis" / Path(*_DEFAULT_ENABLE_RELATIVE)


# ===========================================================================
# Per-machine HMAC secret (auto-generated on first issuance)
# ===========================================================================


def _read_secret() -> Optional[str]:
    """Read the per-machine secret. Returns ``None`` when absent or
    unreadable — verification then fails *closed* (every grant is
    unverifiable, so no grant authorizes). NEVER raises."""
    target = secret_path()
    try:
        if not target.exists():
            return None
        value = target.read_text(encoding="utf-8").strip()
        return value or None
    except Exception:  # noqa: BLE001
        return None


def _ensure_secret() -> Optional[str]:
    """Read the secret, generating it (0600, O_EXCL) on first use.
    Only the issuance path calls this — verification never creates
    state. Returns the secret, or ``None`` on I/O failure."""
    existing = _read_secret()
    if existing is not None:
        return existing
    target = secret_path()
    new_secret = os.urandom(32).hex()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(target),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, new_secret.encode("utf-8"))
        finally:
            os.close(fd)
        return new_secret
    except FileExistsError:
        # Lost a race — another issuance created it. Re-read.
        return _read_secret()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[operator_commit_authority] secret create failed at %s: "
            "%s — issuance will fail closed",
            target,
            type(exc).__name__,
        )
        return None


def _sign(payload: Mapping[str, Any], secret: str) -> str:
    """Compose the canonical HMAC from :mod:`roadmap_reader`. NEVER
    raises. Returns ``""`` when the canonical crypto is unavailable —
    callers treat an empty signature as a fail-closed condition."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            compute_signature,
        )
        return compute_signature(payload, secret)
    except Exception:  # noqa: BLE001
        return ""


def _verify(payload: Mapping[str, Any], signature_hex: str, secret: str) -> bool:
    """Compose the canonical constant-time verify from
    :mod:`roadmap_reader`. NEVER raises. Returns ``False`` (fail
    closed) when the canonical crypto is unavailable."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            verify_signature,
        )
        return verify_signature(payload, signature_hex, secret)
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Persistent master enable (Slice 3 #0) — the Cursor SCM-button fix
# ===========================================================================
#
# An env-only master flag can never be ON for a GUI git subprocess
# (Cursor / VS Code Source Control inherit no shell env). The signed,
# out-of-repo enable record lets the hook subprocess resolve master=ON
# independent of how git was spawned. Signed with the SAME per-machine
# secret as grants (composes _sign/_verify — zero new crypto): a hand-
# created empty file does NOT flip the gate; tamper is signature-
# evident; absent file/secret → OFF (default-FALSE preserved).


def _enable_signed_payload(
    issued_at_unix: float, operator_label: str,
) -> Dict[str, Any]:
    """Canonical dict the enable HMAC covers. Deterministic."""
    return {
        "enabled": True,
        "issued_at_unix": float(issued_at_unix),
        "operator_label": str(operator_label),
        "schema_version": OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION,
    }


def persistent_enabled() -> bool:
    """Return ``True`` iff a valid, HMAC-signed enable record exists.
    NEVER raises. Missing file, missing secret, malformed JSON, bad
    signature, or ``enabled != True`` all → ``False`` (fail closed to
    the safe default — the legacy token gate still applies)."""
    target = enable_file_path()
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
    secret = _read_secret()
    if not secret:
        return False
    # Recompute the canonical payload from the trusted fields so a
    # tampered/extra field cannot ride along under an old signature.
    payload = _enable_signed_payload(
        issued_at_unix=float(record.get("issued_at_unix", 0.0)),
        operator_label=str(record.get("operator_label", "")),
    )
    return _verify(payload, signature, secret)


def enable_authority(
    operator_label: str,
    *,
    now_unix: Optional[float] = None,
) -> bool:
    """Operator-only: write the signed persistent enable record
    (bootstraps the per-machine secret on first use). Atomic write,
    0600. Returns ``True`` on success. NEVER raises."""
    label = str(operator_label or "").strip()
    if not label:
        return False
    secret = _ensure_secret()
    if not secret:
        return False
    now = float(now_unix) if now_unix is not None else time.time()
    payload = _enable_signed_payload(now, label)
    signature = _sign(payload, secret)
    if not signature:
        return False
    target = enable_file_path()
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
        # Operator-only entry point: also mint a presence marker so
        # an operator channel can be earned without a separate
        # arming step (branch-agnostic — `enable` is a "the
        # operator is here on this machine" act). Best-effort;
        # never blocks enablement.
        try:
            _root, _branch = resolve_repo_root_and_branch(
                _resolve_repo_root() or Path(".")
            )
            mint_operator_presence(
                _root or (_resolve_repo_root() or Path(".")),
                _branch,
                label,
                now_unix=now,
            )
        except Exception:  # noqa: BLE001 — never block enable
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[operator_commit_authority] enable write failed at %s: "
            "%s",
            target,
            type(exc).__name__,
        )
        return False


def disable_authority() -> bool:
    """Operator-only: remove the persistent enable record. Master
    then reverts to env-only (default FALSE). Idempotent. Returns
    ``True`` iff the gate is persistently-disabled afterwards (file
    absent). NEVER raises."""
    target = enable_file_path()
    try:
        if target.exists():
            target.unlink()
        return not target.exists()
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# Operator-presence marker — the structural channel discriminator
# ===========================================================================
#
# Root cause this closes: ``commit_authority_cli`` inferred the
# commit channel from an env var that *hardcoded-defaulted to
# "ide"*. A human clicking Cursor's Commit button and a Cursor
# *Agent* running ``git commit`` are the SAME process tree with
# identical env → both resolved to ``ide`` → both matched the
# operator's interactive ``ide`` grant. The ``autonomous`` channel
# (bounded by :mod:`ledger_sovereignty`) existed but was never
# reached for IDE-spawned commits.
#
# You cannot reliably tell "human SCM commit" from "Agent git
# commit" by ambient environment (they look identical). So the
# authorization must NOT depend on that distinction. Instead, an
# operator channel must be *earned* by a positive, unforgeable
# proof of operator presence: a short-TTL JSON record signed with
# the SAME per-machine secret as grants/enable (composes
# :func:`_sign` / :func:`_verify` / :func:`_read_secret` — ZERO new
# crypto). It is minted ONLY at operator-only entry points
# (:func:`issue_grant`, :func:`enable_authority`). Absent a valid
# marker, :func:`resolve_commit_channel` returns ``AUTONOMOUS`` →
# the existing sovereignty path refuses the commit on a non-owned
# tree (the operator main checkout). Fail-closed, no ambient
# sniffing, no hardcoded default.


def presence_file_path() -> Path:
    """Operator-presence marker location. Lives OUTSIDE the repo
    (``~/.jarvis/commit_authority/presence.json``) for the same
    reason the secret/enable record do — GUI git inherits no shell
    env. Operator override via
    ``JARVIS_COMMIT_AUTHORITY_PRESENCE_FILE``. NEVER raises."""
    raw = os.environ.get(_ENV_PRESENCE_FILE, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / ".jarvis" / Path(*_DEFAULT_PRESENCE_RELATIVE)


def presence_ttl_s() -> int:
    """``JARVIS_COMMIT_PRESENCE_TTL_S`` (default 900s = 15 min,
    floor 60s, ceiling 86400s). Garbage/empty → default. NEVER
    raises."""
    raw = os.environ.get(_ENV_PRESENCE_TTL_S, "").strip()
    try:
        v = int(float(raw)) if raw else _DEFAULT_PRESENCE_TTL_S
    except (TypeError, ValueError):
        v = _DEFAULT_PRESENCE_TTL_S
    return max(_MIN_PRESENCE_TTL_S, min(_MAX_PRESENCE_TTL_S, v))


def _presence_signed_payload(
    issued_at_unix: float,
    ttl_s: int,
    repo_root_sha256: str,
    branch: str,
    operator_label: str,
) -> Dict[str, Any]:
    """Canonical dict the presence HMAC covers. Deterministic —
    recomputed from trusted fields on verify so a tampered/extra
    field cannot ride an old signature (same discipline as the
    enable record)."""
    return {
        "kind": "operator_presence",
        "issued_at_unix": float(issued_at_unix),
        "ttl_s": int(ttl_s),
        "repo_root_sha256": str(repo_root_sha256),
        "branch": str(branch),
        "operator_label": str(operator_label),
        "schema_version": OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION,
    }


def _presence_max_entries() -> int:
    raw = os.environ.get(_ENV_PRESENCE_MAX_ENTRIES, "").strip()
    try:
        v = int(raw) if raw else _DEFAULT_PRESENCE_MAX_ENTRIES
    except (TypeError, ValueError):
        v = _DEFAULT_PRESENCE_MAX_ENTRIES
    return max(
        _MIN_PRESENCE_MAX_ENTRIES,
        min(_MAX_PRESENCE_MAX_ENTRIES, v),
    )


def _presence_entry_key(repo_fp: str, branch: str) -> str:
    """Composite key — (repo fingerprint, branch). ``\\x1f`` (unit
    separator) cannot occur in a fingerprint hex or a git ref, so
    the join is unambiguous."""
    return f"{repo_fp}\x1f{str(branch or '').strip()}"


def _load_presence_store(target: Path) -> Dict[str, Any]:
    """Return ``{"entries": {key: {"record":..,"signature":..}}}``.

    Tolerant: a legacy single-record file
    (``{"record":..,"signature":..}``) is migrated *in memory*
    into one entry so an in-flight operator presence survives the
    multi-entry upgrade (read-side back-compat). NEVER raises."""
    try:
        if not target.exists():
            return {"entries": {}}
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"entries": {}}
    if not isinstance(raw, dict):
        return {"entries": {}}
    entries = raw.get("entries")
    if isinstance(entries, dict):
        return {"entries": entries}
    # Legacy single-record shape → synthesize one entry.
    rec = raw.get("record")
    sig = raw.get("signature")
    if isinstance(rec, dict) and isinstance(sig, str):
        key = _presence_entry_key(
            str(rec.get("repo_root_sha256", "")),
            str(rec.get("branch", "")),
        )
        return {"entries": {key: {"record": rec, "signature": sig}}}
    return {"entries": {}}


def _presence_entry_valid(
    entry: Any,
    *,
    repo_fp: str,
    branch: str,
    now: float,
    secret: str,
) -> bool:
    """Verify one store entry against (repo_fp, branch). A blank
    record-branch == whole-repo arming (matches any branch). NEVER
    raises."""
    if not isinstance(entry, dict):
        return False
    record = entry.get("record")
    signature = entry.get("signature")
    if not isinstance(record, dict) or not isinstance(signature, str):
        return False
    if record.get("kind") != "operator_presence":
        return False
    try:
        issued = float(record.get("issued_at_unix", 0.0))
        ttl = int(record.get("ttl_s", 0))
    except (TypeError, ValueError):
        return False
    rec_repo = str(record.get("repo_root_sha256", ""))
    rec_branch = str(record.get("branch", ""))
    payload = _presence_signed_payload(
        issued, ttl, rec_repo, rec_branch,
        str(record.get("operator_label", "")),
    )
    if not _verify(payload, signature, secret):
        return False
    if ttl <= 0 or now >= issued + float(ttl):
        return False
    if rec_repo and rec_repo != repo_fp:
        return False
    cur_branch = str(branch or "").strip()
    if rec_branch and cur_branch and rec_branch != cur_branch:
        return False
    return True


def mint_operator_presence(
    repo_root: Path,
    branch: str,
    operator_label: str,
    *,
    ttl_s: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> bool:
    """Operator-only: ADD/refresh the signed presence entry for
    ``(repo_root, branch)`` WITHOUT clobbering other entries.

    Production-lockout root fix: presence is a multi-entry store
    keyed by (repo fingerprint, branch). A branch-bound grant on
    one branch no longer wipes the operator's whole-repo or
    other-branch presence (the single-global-last-write-wins
    defect that produced ``denied_sovereignty`` on the operator's
    own commit). Expired entries are pruned and the store is
    drop-oldest bounded on every write. Atomic, 0600. Bootstraps
    the per-machine secret. NEVER raises."""
    label = str(operator_label or "").strip()
    if not label:
        return False
    secret = _ensure_secret()
    if not secret:
        return False
    try:
        fp = repo_root_fingerprint(Path(repo_root))
    except Exception:  # noqa: BLE001
        return False
    now = float(now_unix) if now_unix is not None else time.time()
    eff_ttl = (
        int(ttl_s)
        if (ttl_s is not None and int(ttl_s) > 0)
        else presence_ttl_s()
    )
    eff_ttl = max(_MIN_PRESENCE_TTL_S, min(_MAX_PRESENCE_TTL_S, eff_ttl))
    norm_branch = str(branch or "").strip()
    payload = _presence_signed_payload(
        now, eff_ttl, fp, norm_branch, label,
    )
    signature = _sign(payload, secret)
    if not signature:
        return False
    target = presence_file_path()
    try:
        store = _load_presence_store(target)
        entries: Dict[str, Any] = dict(store.get("entries", {}))
        # Prune expired entries (keeps the store self-cleaning).
        kept: Dict[str, Any] = {}
        for k, v in entries.items():
            try:
                rec = v.get("record", {}) if isinstance(v, dict) else {}
                iss = float(rec.get("issued_at_unix", 0.0))
                t = int(rec.get("ttl_s", 0))
                if t > 0 and now < iss + float(t):
                    kept[k] = v
            except Exception:  # noqa: BLE001
                continue
        kept[_presence_entry_key(fp, norm_branch)] = {
            "record": payload, "signature": signature,
        }
        # Drop-oldest hard cap by issued_at.
        cap = _presence_max_entries()
        if len(kept) > cap:
            ordered = sorted(
                kept.items(),
                key=lambda kv: float(
                    kv[1].get("record", {}).get(
                        "issued_at_unix", 0.0,
                    )
                ),
            )
            kept = dict(ordered[-cap:])
        # Transitional dual-format bridge: pre-multi-entry code
        # reads ``raw["record"]`` (single-record shape). Until all
        # checkouts realign, ALSO emit a signed top-level
        # whole-repo (blank-branch) record so old readers on ANY
        # branch stay satisfied — it is HMAC-signed (unforgeable),
        # and new readers ignore it (entries-first in
        # _load_presence_store). Remove once realignment completes.
        legacy_payload = _presence_signed_payload(
            now, eff_ttl, fp, "", label,
        )
        legacy_sig = _sign(legacy_payload, secret)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "schema_version": (
                        OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION
                    ),
                    "entries": kept,
                    # Old-reader compatibility bridge (signed).
                    "record": legacy_payload,
                    "signature": legacy_sig,
                },
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
            "[operator_commit_authority] presence write failed at "
            "%s: %s",
            target,
            type(exc).__name__,
        )
        return False


def valid_operator_presence(
    repo_root: Path,
    branch: str,
    *,
    now_unix: Optional[float] = None,
) -> bool:
    """Return ``True`` iff a valid, HMAC-signed, unexpired presence
    entry exists for the committing ``(repo_root, branch)`` — the
    exact-branch entry OR a whole-repo (blank-branch) entry.
    Multi-entry: other branches'/repos' entries are independent
    and never cause a false negative here. Fail-closed on missing
    file / secret / malformed JSON / bad signature / expiry / repo
    mismatch. NEVER raises."""
    target = presence_file_path()
    secret = _read_secret()
    if not secret:
        return False
    try:
        fp = repo_root_fingerprint(Path(repo_root))
    except Exception:  # noqa: BLE001
        return False
    now = float(now_unix) if now_unix is not None else time.time()
    store = _load_presence_store(target)
    entries = store.get("entries", {})
    if not isinstance(entries, dict):
        return False
    norm_branch = str(branch or "").strip()
    # Candidate keys: exact (repo, branch) + whole-repo (repo, "").
    for key in (
        _presence_entry_key(fp, norm_branch),
        _presence_entry_key(fp, ""),
    ):
        if key in entries and _presence_entry_valid(
            entries[key],
            repo_fp=fp, branch=norm_branch, now=now, secret=secret,
        ):
            return True
    # Defensive: also scan any entry (covers legacy-migrated single
    # record whose synthesized key used a possibly-empty repo fp).
    for entry in entries.values():
        if _presence_entry_valid(
            entry,
            repo_fp=fp, branch=norm_branch, now=now, secret=secret,
        ):
            return True
    return False


# ===========================================================================
# Closed taxonomies (AST-pinned)
# ===========================================================================


class CommitChannel(str, enum.Enum):
    """Closed 5-value commit channel — bytes-pinned via AST.

    * ``REPL`` — Serpent REPL ``/commit`` issuance + commits.
    * ``CLI`` — ``commit_authority_cli`` operator tool.
    * ``IDE`` — Cursor / VS Code Source Control GUI (the path that
      cannot inherit shell env — OCA's reason to exist).
    * ``DAEMON`` — long-running operator-side issuer (Slice 4 socket).
    * ``AUTONOMOUS`` — O+V ``AutoCommitter``; authorized by the
      :mod:`ledger_sovereignty` marker, NOT an operator grant.
    """

    REPL = "repl"
    CLI = "cli"
    IDE = "ide"
    DAEMON = "daemon"
    AUTONOMOUS = "autonomous"

    @classmethod
    def parse(cls, value: object) -> Optional["CommitChannel"]:
        """Strict parse — returns ``None`` on unknown (caller maps
        that to the ``CHANNEL_UNKNOWN`` verdict). NEVER raises."""
        try:
            return cls(str(value).strip().lower())
        except Exception:  # noqa: BLE001
            return None


def resolve_commit_channel(
    repo_root: Path,
    branch: str,
    *,
    env_channel: str = "",
    now_unix: Optional[float] = None,
) -> CommitChannel:
    """Structural, evidence-based commit-channel resolution.

    Replaces the blind ``or "ide"`` default in
    ``commit_authority_cli`` that let a Cursor *Agent*'s headless
    git commit borrow the operator's interactive ``ide`` grant.

    Rule (fail-closed, no ambient sniffing, no hardcoded default):

      1. Explicit ``autonomous`` → :attr:`CommitChannel.AUTONOMOUS`.
         The safe sink — :func:`verify_pre_commit` delegates it to
         :mod:`ledger_sovereignty` (composed, never reimplemented).
      2. No valid operator-presence marker for ``(repo_root,
         branch)`` → :attr:`CommitChannel.AUTONOMOUS`. A human
         Cursor SCM commit *earns* an operator channel because the
         operator minted a short-TTL signed presence marker
         (:func:`issue_grant` / :func:`enable_authority` do so). An
         Agent cannot forge it. So an Agent's commit on the
         operator main checkout falls here → AUTONOMOUS → the
         sovereignty gate refuses it on the non-owned tree. Exactly
         the design behavior, finally reached.
      3. Valid presence + explicit operator channel
         (``repl``/``cli``/``ide``/``daemon``) → that channel.
      4. Valid presence + unset/unparseable env → ``IDE`` — the
         *earned* interactive default (presence proves the operator
         is here), never a hardcoded blanket default.

    ``env_channel`` is the raw ``JARVIS_COMMIT_CHANNEL`` value. It
    is honored ONLY when operator presence is valid (case 3) or it
    is ``autonomous`` (case 1) — a forged ``JARVIS_COMMIT_CHANNEL=
    ide`` without presence does NOT yield an operator channel.
    NEVER raises.
    """
    raw = str(env_channel or "").strip()
    parsed = CommitChannel.parse(raw) if raw else None
    if parsed is CommitChannel.AUTONOMOUS:
        return CommitChannel.AUTONOMOUS
    if not valid_operator_presence(
        repo_root, branch, now_unix=now_unix,
    ):
        return CommitChannel.AUTONOMOUS
    if parsed in (
        CommitChannel.REPL,
        CommitChannel.CLI,
        CommitChannel.IDE,
        CommitChannel.DAEMON,
    ):
        return parsed  # type: ignore[return-value]
    return CommitChannel.IDE


class CommitAuthorityVerdict(str, enum.Enum):
    """Closed 8-value verdict — bytes-pinned via AST.

    * ``AUTHORIZED`` — commit may proceed (hook chain continues to
      the file-integrity layer).
    * ``DENIED_NO_GRANT`` — no valid signed grant for this
      repo/branch/channel (a hand-edited / forged grant lands here
      because its signature fails to verify).
    * ``DENIED_EXPIRED`` — a matching grant exists but its TTL
      elapsed.
    * ``DENIED_SCOPE`` — a matching unexpired grant exists but its
      path scopes do not cover the staged files.
    * ``DENIED_GOVERNANCE_DRIFT`` — staged files touch governance/
      and drift from the operator-signed manifest without a
      ``governance_amend`` grant.
    * ``DENIED_SOVEREIGNTY`` — autonomous channel committing into a
      tree it does not own (sovereignty master on).
    * ``DISABLED`` — OCA master flag off; gate skipped.
    * ``CHANNEL_UNKNOWN`` — unrecognized channel string (fail
      closed — an unknown channel is never authorized).
    """

    AUTHORIZED = "authorized"
    DENIED_NO_GRANT = "denied_no_grant"
    DENIED_EXPIRED = "denied_expired"
    DENIED_SCOPE = "denied_scope"
    DENIED_GOVERNANCE_DRIFT = "denied_governance_drift"
    DENIED_SOVEREIGNTY = "denied_sovereignty"
    DISABLED = "disabled"
    CHANNEL_UNKNOWN = "channel_unknown"


def is_authorized_verdict(verdict: object) -> bool:
    """Return True iff the verdict permits the commit. Only
    ``AUTHORIZED`` and ``DISABLED`` (gate skipped) pass. Centralized
    so the Slice 2 hook composes ONE predicate (no parallel string
    comparison). NEVER raises."""
    try:
        val = getattr(verdict, "value", None) or str(verdict)
        return val in (
            CommitAuthorityVerdict.AUTHORIZED.value,
            CommitAuthorityVerdict.DISABLED.value,
        )
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class CommitGrant:
    """One time-bounded operator commit grant. The signed payload
    (everything except the out-of-band signature) is what the HMAC
    covers — a hand-edited line fails verification."""

    grant_id: str
    issued_at_unix: float
    expires_at_unix: float
    repo_root_sha256: str
    branch: str  # empty string = any branch
    channel: str  # CommitChannel value
    scopes: Tuple[str, ...]  # repo-relative posix prefixes; () = whole repo
    operator_label: str
    governance_amend: bool = False
    schema_version: str = OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION

    def signed_payload(self) -> Dict[str, Any]:
        """Canonical dict the HMAC is computed over. Deterministic
        (scopes sorted) so re-serialization round-trips."""
        return {
            "grant_id": self.grant_id,
            "issued_at_unix": float(self.issued_at_unix),
            "expires_at_unix": float(self.expires_at_unix),
            "repo_root_sha256": self.repo_root_sha256,
            "branch": self.branch,
            "channel": self.channel,
            "scopes": sorted(self.scopes),
            "operator_label": self.operator_label,
            "governance_amend": bool(self.governance_amend),
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.signed_payload()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["CommitGrant"]:
        """Defensive parser. Returns None on structural violation.
        NEVER raises."""
        try:
            gid = str(raw.get("grant_id", "")).strip()
            if not gid:
                return None
            scopes_raw = raw.get("scopes", []) or []
            scopes = tuple(
                str(s).strip()
                for s in scopes_raw
                if str(s).strip()
            )
            return cls(
                grant_id=gid,
                issued_at_unix=float(raw.get("issued_at_unix", 0.0)),
                expires_at_unix=float(raw.get("expires_at_unix", 0.0)),
                repo_root_sha256=str(
                    raw.get("repo_root_sha256", "")
                ).strip().lower(),
                branch=str(raw.get("branch", "")).strip(),
                channel=str(raw.get("channel", "")).strip().lower(),
                scopes=scopes,
                operator_label=str(raw.get("operator_label", "")),
                governance_amend=bool(raw.get("governance_amend", False)),
                schema_version=str(
                    raw.get(
                        "schema_version",
                        OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION,
                    )
                ),
            )
        except Exception:  # noqa: BLE001
            return None


@dataclass(frozen=True)
class CommitAuthorityContext:
    """Verifier input. Frozen so the verdict can't be racing a
    mutated context."""

    channel: str
    repo_root: str
    branch: str = ""
    staged_files: Tuple[str, ...] = ()
    now_unix: Optional[float] = None

    def effective_now(self) -> float:
        return (
            float(self.now_unix)
            if self.now_unix is not None
            else time.time()
        )


@dataclass(frozen=True)
class CommitAuthorityVerdictResult:
    """Verifier output. NEVER constructed by raising — every public
    failure mode maps to a verdict here."""

    schema_version: str
    verdict: CommitAuthorityVerdict
    channel: str
    matched_grant_id: str
    detail: str
    governance_verdict: str = ""  # ManifestVerdict.value when consulted

    def authorized(self) -> bool:
        return is_authorized_verdict(self.verdict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "channel": self.channel,
            "matched_grant_id": self.matched_grant_id,
            "detail": self.detail[:512],
            "governance_verdict": self.governance_verdict,
        }


@dataclass(frozen=True)
class GrantIssueOutcome:
    """Result of an operator-driven grant issuance."""

    ok: bool
    grant_id: str
    expires_at_unix: float
    grants_path_str: str
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "grant_id": self.grant_id,
            "expires_at_unix": float(self.expires_at_unix),
            "grants_path_str": self.grants_path_str,
            "error": self.error,
        }


def _verdict(
    verdict: CommitAuthorityVerdict,
    channel: str,
    detail: str,
    *,
    matched_grant_id: str = "",
    governance_verdict: str = "",
) -> CommitAuthorityVerdictResult:
    return CommitAuthorityVerdictResult(
        schema_version=OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION,
        verdict=verdict,
        channel=channel,
        matched_grant_id=matched_grant_id,
        detail=detail,
        governance_verdict=governance_verdict,
    )


# ===========================================================================
# Git introspection (subprocess discipline — mirrors gitignore_guard)
# ===========================================================================


def _run_git(
    args: Sequence[str],
    *,
    repo_root: Path,
    timeout_s: float = _GIT_OP_TIMEOUT_S,
) -> Optional[subprocess.CompletedProcess]:
    """Bounded git invocation. Array args only — NEVER ``shell=True``.
    Returns ``None`` on any failure. NEVER raises."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:  # noqa: BLE001
        return None


def resolve_repo_root_and_branch(
    repo_root: Path,
) -> Tuple[Optional[Path], str]:
    """Resolve the canonical toplevel + current branch. Falls back to
    the provided ``repo_root`` when git can't answer. NEVER raises."""
    top = _run_git(
        ["rev-parse", "--show-toplevel"], repo_root=repo_root
    )
    resolved_root: Optional[Path]
    if top is not None and top.returncode == 0 and top.stdout.strip():
        try:
            resolved_root = Path(top.stdout.strip()).resolve()
        except Exception:  # noqa: BLE001
            resolved_root = None
    else:
        try:
            resolved_root = Path(repo_root).resolve()
        except Exception:  # noqa: BLE001
            resolved_root = None

    branch = ""
    head = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], repo_root=repo_root
    )
    if head is not None and head.returncode == 0:
        branch = head.stdout.strip()
    return resolved_root, branch


def repo_root_fingerprint(repo_root: Path) -> str:
    """SHA-256 of the resolved absolute repo-root path. A grant is
    bound to a specific checkout — a grant for the operator's main
    checkout never authorizes commits in a different clone."""
    try:
        resolved = str(Path(repo_root).resolve())
    except Exception:  # noqa: BLE001
        resolved = str(repo_root)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


# ===========================================================================
# Append-only grant ledger (composes cross_process_jsonl)
# ===========================================================================


def _append_record(record: Mapping[str, Any]) -> bool:
    """Append one JSON record to the grant ledger via the canonical
    cross-process primitive. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (
            flock_append_line,
        )
    except Exception:  # noqa: BLE001
        return False
    target = grants_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        return False
    try:
        line = json.dumps(record, sort_keys=True)
    except Exception:  # noqa: BLE001
        return False
    return flock_append_line(target, line)


def _read_ledger() -> List[Dict[str, Any]]:
    """Read every ledger record. Malformed lines are skipped. NEVER
    raises — an unreadable ledger yields ``[]`` (fail closed: no
    grant means no authorization)."""
    target = grants_path()
    try:
        if not target.exists():
            return []
        text = target.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _scope_covers(scopes: Tuple[str, ...], staged: Sequence[str]) -> bool:
    """Empty scopes → whole repo. Otherwise every staged file must
    sit under at least one scope prefix (posix, repo-relative)."""
    if not scopes:
        return True
    norm_scopes = [s.strip().strip("/") for s in scopes if s.strip()]
    if not norm_scopes:
        return True
    for f in staged:
        ff = str(f).replace(os.sep, "/").strip().lstrip("./")
        covered = False
        for sc in norm_scopes:
            if ff == sc or ff.startswith(sc + "/"):
                covered = True
                break
        if not covered:
            return False
    return True


# ===========================================================================
# Public API — verify (NEVER raises)
# ===========================================================================


def _autonomous_verdict(
    ctx: CommitAuthorityContext,
    repo_root: Path,
) -> CommitAuthorityVerdictResult:
    """Autonomous channel: delegate the ownership decision to
    :mod:`ledger_sovereignty` — never reimplement it. No operator
    grant is required for an owned worktree (legacy behavior)."""
    try:
        from backend.core.ouroboros.governance import (
            ledger_sovereignty as ls,
        )
    except Exception:  # noqa: BLE001
        # Cannot consult sovereignty → fail closed for autonomous.
        return _verdict(
            CommitAuthorityVerdict.DENIED_SOVEREIGNTY,
            ctx.channel,
            "ledger_sovereignty unavailable — autonomous commit "
            "refused (fail closed)",
        )
    if ls.master_enabled() and not ls.is_owned(repo_root):
        return _verdict(
            CommitAuthorityVerdict.DENIED_SOVEREIGNTY,
            ctx.channel,
            f"autonomous commit into non-owned tree {repo_root} — "
            "sovereignty marker absent",
        )
    return _verdict(
        CommitAuthorityVerdict.AUTHORIZED,
        ctx.channel,
        "autonomous channel — sovereignty owned (or sovereignty "
        "master off); no operator grant required",
    )


def _execution_boundary_verdict(
    ctx: CommitAuthorityContext,
    repo_root: Path,
) -> Optional[CommitAuthorityVerdictResult]:
    """Sovereign Execution Boundary (Stage A) — refuse an AUTONOMOUS commit
    that targets the operator's PRIMARY checkout or the main/master branch.

    Reached ONLY from the autonomous branch of :func:`verify_pre_commit`,
    so autonomy is already cryptographically established (no valid operator
    presence). This adds the *location* constraint that the bare sovereignty
    marker does not: the loop must commit from an isolated worktree on a
    feature branch, never the operator's primary tree — closing the
    branch/file-mutation revert vector.

    Deny-on-proof, never-on-doubt: composes
    :func:`execution_context.is_primary_checkout` (which returns True only on
    affirmative detection) so a transient git fault can never wedge a
    legitimate worktree commit. Returns a DENY verdict or ``None`` (pass).
    Reuses :attr:`CommitAuthorityVerdict.DENIED_SOVEREIGNTY` (the verdict
    taxonomy is closed/pinned at 8). NEVER raises."""
    # Dedicated default-OFF sub-flag — composes with the OCA master so
    # enabling OCA alone stays byte-identical to the legacy autonomous
    # contract. Off → the boundary is inert.
    if not _flag(_ENV_EXECUTION_BOUNDARY, default=False):
        return None
    try:
        from backend.core.ouroboros.governance import (
            execution_context as _ec,
        )
        primary = bool(_ec.is_primary_checkout(repo_root))
    except Exception:  # noqa: BLE001 — substrate unavailable → don't block
        primary = False
    norm_branch = str(getattr(ctx, "branch", "") or "").strip().lower()
    on_protected = norm_branch in ("main", "master")
    if not (primary or on_protected):
        return None
    where = (
        "the operator PRIMARY checkout"
        if primary
        else f"protected branch '{norm_branch}'"
    )
    return _verdict(
        CommitAuthorityVerdict.DENIED_SOVEREIGNTY,
        ctx.channel,
        "Sovereign Execution Boundary: autonomous commit into "
        f"{where} refused — the loop must commit from an isolated "
        "worktree on a feature branch, not the operator's primary "
        f"tree. repo_root={repo_root}",
    )


def _governance_gate(
    ctx: CommitAuthorityContext,
    grant: Optional[CommitGrant],
) -> Optional[CommitAuthorityVerdictResult]:
    """Compose :mod:`governance_manifest`. Returns a DENY verdict iff
    staged files drift from the operator-signed manifest and the
    matched grant lacks ``governance_amend``. ``None`` = pass."""
    if not ctx.staged_files:
        return None
    try:
        from backend.core.ouroboros.governance import (
            governance_manifest as gm,
        )
        comparison = gm.verify_governance_state(
            target_files=list(ctx.staged_files)
        )
        gov_val = comparison.verdict.value
        if gm.is_refusal_verdict(comparison.verdict):
            if grant is not None and grant.governance_amend:
                return None  # operator explicitly authorized the amend
            return _verdict(
                CommitAuthorityVerdict.DENIED_GOVERNANCE_DRIFT,
                ctx.channel,
                "staged files drift from operator-signed governance "
                "manifest; issue a grant with governance_amend=True "
                "or refresh the manifest",
                matched_grant_id=grant.grant_id if grant else "",
                governance_verdict=gov_val,
            )
        return None
    except Exception:  # noqa: BLE001
        # Manifest substrate unavailable — do NOT block on its
        # absence (it has its own opt-in master flag). Pass through.
        return None


def verify_pre_commit(
    ctx: CommitAuthorityContext,
) -> CommitAuthorityVerdictResult:
    """The single commit-authorization verifier. NEVER raises.

    Off-master → ``DISABLED`` (byte-identical legacy hook chain).
    Autonomous channel → delegated to :mod:`ledger_sovereignty`.
    Operator channels → require a valid signed, unexpired, in-scope
    grant; governance/ drift additionally requires a
    ``governance_amend`` grant.
    """
    if not master_enabled():
        return _verdict(
            CommitAuthorityVerdict.DISABLED,
            str(ctx.channel),
            f"OCA disabled via {_ENV_MASTER}=false — legacy hook "
            "chain proceeds unchanged",
        )

    channel = CommitChannel.parse(ctx.channel)
    if channel is None:
        return _verdict(
            CommitAuthorityVerdict.CHANNEL_UNKNOWN,
            str(ctx.channel),
            f"unrecognized commit channel {ctx.channel!r} — fail "
            "closed (known: repl/cli/ide/daemon/autonomous)",
        )

    try:
        repo_root = Path(ctx.repo_root).resolve()
    except Exception:  # noqa: BLE001
        repo_root = Path(ctx.repo_root or ".")

    # Autonomous channel never needs an operator grant; it is bounded
    # by the sovereignty marker (composed, not reimplemented). The
    # governance hash-cap still applies (autonomous ops touching
    # governance/ must match the signed manifest).
    if channel is CommitChannel.AUTONOMOUS:
        # Sovereign Execution Boundary (Stage A): an autonomous commit must
        # never land in the operator's primary checkout or on main/master —
        # the loop commits from an isolated worktree on a feature branch.
        boundary = _execution_boundary_verdict(ctx, repo_root)
        if boundary is not None:
            return boundary
        sov = _autonomous_verdict(ctx, repo_root)
        if not sov.authorized():
            return sov
        gov = _governance_gate(ctx, grant=None)
        if gov is not None:
            return gov
        return sov

    # Operator channels (repl/cli/ide/daemon): require a valid grant.
    secret = _read_secret()
    if not secret:
        return _verdict(
            CommitAuthorityVerdict.DENIED_NO_GRANT,
            channel.value,
            "no per-machine secret present — cannot verify any "
            "grant (fail closed). Issue a grant first to "
            "bootstrap the secret.",
        )

    fp = repo_root_fingerprint(repo_root)
    now = ctx.effective_now()
    records = _read_ledger()

    # Build revocation/consume sets from the append-only ledger.
    explicitly_revoked: set = set()
    revoke_all_at: float = -1.0
    consumed: set = set()
    for rec in records:
        rtype = str(rec.get("type", "")).strip().lower()
        if rtype == "revoke":
            gid = str(rec.get("grant_id", "")).strip()
            if gid == _REVOKE_ALL_TOKEN:
                try:
                    revoke_all_at = max(
                        revoke_all_at, float(rec.get("at_unix", 0.0))
                    )
                except Exception:  # noqa: BLE001
                    continue
            elif gid:
                explicitly_revoked.add(gid)
        elif rtype == "consume":
            gid = str(rec.get("grant_id", "")).strip()
            if gid:
                consumed.add(gid)

    # Walk grant records newest-last; track the best near-miss so the
    # operator gets the most actionable verdict.
    saw_expired = False
    saw_scope_miss = False
    matched: Optional[CommitGrant] = None
    for rec in records:
        if str(rec.get("type", "")).strip().lower() != "grant":
            continue
        grant = CommitGrant.from_dict(rec.get("grant", {}))
        if grant is None:
            continue
        signature = str(rec.get("signature", ""))
        if not _verify(grant.signed_payload(), signature, secret):
            # Forged / tampered / wrong-secret line — treat as absent.
            continue
        if grant.grant_id in explicitly_revoked:
            continue
        if grant.grant_id in consumed:
            continue
        if revoke_all_at >= 0.0 and grant.issued_at_unix <= revoke_all_at:
            continue
        if grant.repo_root_sha256 and grant.repo_root_sha256 != fp:
            continue
        if grant.branch and ctx.branch and grant.branch != ctx.branch:
            continue
        if grant.channel and grant.channel != channel.value:
            continue
        if grant.expires_at_unix <= now:
            saw_expired = True
            continue
        if not _scope_covers(grant.scopes, ctx.staged_files):
            saw_scope_miss = True
            continue
        matched = grant  # keep walking — latest valid grant wins

    if matched is None:
        if saw_scope_miss:
            return _verdict(
                CommitAuthorityVerdict.DENIED_SCOPE,
                channel.value,
                "a valid unexpired grant exists but its path "
                "scopes do not cover the staged files",
            )
        if saw_expired:
            return _verdict(
                CommitAuthorityVerdict.DENIED_EXPIRED,
                channel.value,
                "the matching grant has expired — issue a fresh "
                "grant (/commit grant <minutes>)",
            )
        return _verdict(
            CommitAuthorityVerdict.DENIED_NO_GRANT,
            channel.value,
            "no valid signed commit grant for this "
            "repo/branch/channel — issue one via /commit grant "
            "or commit_authority_cli",
        )

    gov = _governance_gate(ctx, matched)
    if gov is not None:
        return gov

    return _verdict(
        CommitAuthorityVerdict.AUTHORIZED,
        channel.value,
        f"authorized by grant {matched.grant_id} "
        f"(operator={matched.operator_label!r})",
        matched_grant_id=matched.grant_id,
    )


# ===========================================================================
# Public API — issue / revoke / consume (operator-only; NEVER raises)
# ===========================================================================


def _adaptive_ttl_s() -> int:
    """Default TTL, shortened when the operator is in a PLAN/ANALYZE
    operation mode (read-only compose of :mod:`operation_mode`). A
    long commit window during an analysis session is a footgun."""
    base = default_ttl_s()
    try:
        from backend.core.ouroboros.governance import operation_mode as om
        mode = om.resolve_mode_from_env()
        mode_val = getattr(mode, "value", str(mode)).strip().lower()
        if mode_val in ("plan", "analyze"):
            return min(base, plan_mode_ttl_s())
    except Exception:  # noqa: BLE001
        pass
    return base


def issue_grant(
    *,
    channel: str,
    operator_label: str,
    ttl_s: Optional[int] = None,
    scopes: Sequence[str] = (),
    branch: str = "",
    governance_amend: bool = False,
    repo_root: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> GrantIssueOutcome:
    """Issue a time-bounded operator commit grant. Operator-only
    entry point (REPL/CLI compose this). Bootstraps the per-machine
    secret on first use. NEVER raises."""
    if not operator_label or not str(operator_label).strip():
        return GrantIssueOutcome(
            ok=False,
            grant_id="",
            expires_at_unix=0.0,
            grants_path_str=str(grants_path()),
            error="operator_label required (audit string)",
        )

    parsed = CommitChannel.parse(channel)
    if parsed is None:
        return GrantIssueOutcome(
            ok=False,
            grant_id="",
            expires_at_unix=0.0,
            grants_path_str=str(grants_path()),
            error=(
                f"unknown channel {channel!r} "
                "(repl/cli/ide/daemon/autonomous)"
            ),
        )

    secret = _ensure_secret()
    if not secret:
        return GrantIssueOutcome(
            ok=False,
            grant_id="",
            expires_at_unix=0.0,
            grants_path_str=str(grants_path()),
            error=(
                "could not create/read per-machine secret at "
                f"{secret_path()} — issuance fails closed"
            ),
        )

    root = (
        Path(repo_root)
        if repo_root is not None
        else (_resolve_repo_root() or Path("."))
    )
    fp = repo_root_fingerprint(root)

    effective_ttl = (
        int(ttl_s)
        if ttl_s is not None and int(ttl_s) > 0
        else _adaptive_ttl_s()
    )
    effective_ttl = max(_MIN_TTL_S, min(_MAX_TTL_S, effective_ttl))

    now = float(now_unix) if now_unix is not None else time.time()
    grant = CommitGrant(
        grant_id=uuid.uuid4().hex,
        issued_at_unix=now,
        expires_at_unix=now + float(effective_ttl),
        repo_root_sha256=fp,
        branch=str(branch).strip(),
        channel=parsed.value,
        scopes=tuple(
            str(s).strip() for s in scopes if str(s).strip()
        ),
        operator_label=str(operator_label).strip(),
        governance_amend=bool(governance_amend),
    )
    signature = _sign(grant.signed_payload(), secret)
    if not signature:
        return GrantIssueOutcome(
            ok=False,
            grant_id="",
            expires_at_unix=0.0,
            grants_path_str=str(grants_path()),
            error=(
                "canonical HMAC unavailable (roadmap_reader) — "
                "refusing to write an unsigned grant (fail closed)"
            ),
        )

    record = {
        "type": "grant",
        "at_unix": now,
        "grant": grant.to_dict(),
        "signature": signature,
    }
    if not _append_record(record):
        return GrantIssueOutcome(
            ok=False,
            grant_id="",
            expires_at_unix=0.0,
            grants_path_str=str(grants_path()),
            error="grant ledger append failed",
        )
    # Operator-only entry point: mint a presence marker bound to
    # this grant's repo+branch so resolve_commit_channel can earn
    # an operator channel for the interactive operator. An Agent
    # commit, lacking presence, falls to AUTONOMOUS →
    # ledger_sovereignty. Best-effort: a presence-write failure
    # must not fail an otherwise-successful grant.
    try:
        mint_operator_presence(
            root,
            str(branch).strip(),
            str(operator_label).strip(),
            now_unix=now,
        )
    except Exception:  # noqa: BLE001 — never block a valid grant
        pass
    return GrantIssueOutcome(
        ok=True,
        grant_id=grant.grant_id,
        expires_at_unix=grant.expires_at_unix,
        grants_path_str=str(grants_path()),
    )


def revoke_grants(
    *,
    grant_id: Optional[str] = None,
    revoke_all: bool = False,
    now_unix: Optional[float] = None,
) -> int:
    """Append a revocation tombstone (the ledger is append-only per
    §8). ``revoke_all`` revokes every grant issued at or before now.
    Returns 1 on a successful append, 0 on failure. NEVER raises."""
    now = float(now_unix) if now_unix is not None else time.time()
    if revoke_all:
        ok = _append_record(
            {
                "type": "revoke",
                "at_unix": now,
                "grant_id": _REVOKE_ALL_TOKEN,
            }
        )
        return 1 if ok else 0
    gid = str(grant_id or "").strip()
    if not gid or gid == _REVOKE_ALL_TOKEN:
        return 0
    ok = _append_record(
        {"type": "revoke", "at_unix": now, "grant_id": gid}
    )
    return 1 if ok else 0


def consume_grant(
    grant_id: str,
    *,
    now_unix: Optional[float] = None,
) -> bool:
    """Mark a grant one-shot-consumed (append a consume event). A
    consumed grant no longer authorizes. NEVER raises."""
    gid = str(grant_id or "").strip()
    if not gid:
        return False
    now = float(now_unix) if now_unix is not None else time.time()
    return _append_record(
        {"type": "consume", "at_unix": now, "grant_id": gid}
    )


# ===========================================================================
# §33.3 AST pins
# ===========================================================================


_TARGET_FILE = (
    "backend/core/ouroboros/governance/operator_commit_authority.py"
)


def register_shipped_invariants() -> list:
    """Auto-discovered AST invariant pins."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _EXPECTED_VERDICTS = {
        "authorized",
        "denied_no_grant",
        "denied_expired",
        "denied_scope",
        "denied_governance_drift",
        "denied_sovereignty",
        "disabled",
        "channel_unknown",
    }
    _EXPECTED_CHANNELS = {"repl", "cli", "ide", "daemon", "autonomous"}

    def _enum_values(tree: ast.AST, class_name: str) -> set:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == class_name
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                return found
        return set()

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        found = _enum_values(tree, "CommitAuthorityVerdict")
        if not found:
            return ("CommitAuthorityVerdict class not found",)
        missing = _EXPECTED_VERDICTS - found
        extra = found - _EXPECTED_VERDICTS
        if missing:
            return (f"CommitAuthorityVerdict missing: {sorted(missing)}",)
        if extra:
            return (f"CommitAuthorityVerdict drift: {sorted(extra)}",)
        return ()

    def _validate_channel_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        found = _enum_values(tree, "CommitChannel")
        if not found:
            return ("CommitChannel class not found",)
        missing = _EXPECTED_CHANNELS - found
        extra = found - _EXPECTED_CHANNELS
        if missing:
            return (f"CommitChannel missing: {sorted(missing)}",)
        if extra:
            return (f"CommitChannel drift: {sorted(extra)}",)
        return ()

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(
                            f"forbidden authority import: {alias.name}"
                        )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(..., "
                    "default=False) per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        required = (
            "ledger_sovereignty",
            "governance_manifest",
            "cross_process_jsonl",
            "roadmap_reader",
        )
        missing = [r for r in required if r not in source]
        if missing:
            return (
                "must compose canonical substrates (no parallel "
                f"logic) — missing references: {missing}",
            )
        return ()

    def _validate_subprocess_discipline(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Every subprocess.run call MUST pass a timeout and MUST
        NOT pass shell=True — mirrors gitignore_guard's bounded git
        discipline. A shelled-out / unbounded git call is a hang
        and an injection surface in a security-critical gate."""
        violations: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_subprocess_run = (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            if not is_subprocess_run:
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            if "timeout" not in kwargs:
                violations.append(
                    "subprocess.run missing timeout kwarg"
                )
            shell = kwargs.get("shell")
            if (
                isinstance(shell, ast.Constant)
                and shell.value is True
            ):
                violations.append("subprocess.run uses shell=True")
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_verdict_taxonomy_closed"
            ),
            target_file=_TARGET_FILE,
            description=(
                "CommitAuthorityVerdict 8-value taxonomy bytes-"
                "pinned — the single closed verdict vocabulary "
                "every channel maps onto."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_channel_taxonomy_closed"
            ),
            target_file=_TARGET_FILE,
            description=(
                "CommitChannel 5-value taxonomy bytes-pinned — "
                "repl/cli/ide/daemon/autonomous. An unknown "
                "channel must map to CHANNEL_UNKNOWN, never an "
                "implicit pass."
            ),
            validate=_validate_channel_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_authority_asymmetry"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Substrate purity — OCA MUST NOT import "
                "orchestrator / iron_gate / policy / providers / "
                "candidate_generator / urgency_router / "
                "change_engine / semantic_guardian / "
                "auto_committer / risk_tier_floor / tool_executor. "
                "Consumers (hook CLI + AutoCommitter) lazy-import "
                "OCA, never vice versa."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_master_default_false"
            ),
            target_file=_TARGET_FILE,
            description=(
                "§33.1 — master_enabled() default-FALSE so the "
                "hook chain is byte-identical until the operator "
                "graduates OCA after Slice 2 wiring."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_composes_canonical"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Zero duplication — OCA composes "
                "ledger_sovereignty (ownership), "
                "governance_manifest (drift hash-cap), "
                "cross_process_jsonl (append-only ledger), and "
                "roadmap_reader (canonical constant-time HMAC). "
                "No parallel sovereignty / manifest / flock / "
                "HMAC logic."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "operator_commit_authority_subprocess_discipline"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Every subprocess.run is bounded (timeout kwarg) "
                "and never shell=True — a security-critical gate "
                "must not hang or expose a shell-injection "
                "surface on git introspection."
            ),
            validate=_validate_subprocess_discipline,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Auto-discovered via §33.3. Fail-open per §33.1."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    src = (
        "backend/core/ouroboros/governance/"
        "operator_commit_authority.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Operator Commit Authority master switch. "
                "Default-FALSE per §33.1 — when off, "
                "verify_pre_commit() returns DISABLED and the "
                "commit hook chain is byte-identical to the "
                "pre-substrate world. Graduate only after Slice "
                "2 hook + AutoCommitter wiring."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_DEFAULT_TTL_S,
            type=FlagType.INT,
            default=_DEFAULT_TTL_S,
            description=(
                "Default operator commit-grant lifetime in "
                "seconds. Clamped to [60, 86_400]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_DEFAULT_TTL_S}=3600",
        ),
        FlagSpec(
            name=_ENV_PLAN_TTL_S,
            type=FlagType.INT,
            default=_DEFAULT_PLAN_TTL_S,
            description=(
                "Grant lifetime when the operator is in "
                "PLAN/ANALYZE operation mode — shorter so an "
                "analysis session doesn't leave a long commit "
                "window open. Clamped to [60, 86_400]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_PLAN_TTL_S}=900",
        ),
        FlagSpec(
            name=_ENV_GRANTS_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for the append-only grant "
                "ledger path. Defaults to "
                "<repo>/.jarvis/commit_authority/grants.jsonl."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=f"{_ENV_GRANTS_PATH}=/var/jarvis/grants.jsonl",
        ),
        FlagSpec(
            name=_ENV_SECRET_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for the per-machine HMAC "
                "secret path. Defaults to "
                "~/.jarvis/commit_authority/secret (0600, "
                "out-of-repo, no shell-env dependency)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_SECRET_PATH}=/etc/jarvis/oca.secret",
        ),
        FlagSpec(
            name=_ENV_ENABLE_FILE,
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for the persistent master-"
                "enable record path. Defaults to "
                "~/.jarvis/commit_authority/enabled (signed, "
                "out-of-repo). Its presence (valid signature) is "
                "what makes master ON for GUI git subprocesses "
                "(Cursor/VS Code SCM) that inherit no shell env. "
                "Absent + env unset → master FALSE (§33.1)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=(
                f"{_ENV_ENABLE_FILE}=/etc/jarvis/oca.enabled"
            ),
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "OPERATOR_COMMIT_AUTHORITY_SCHEMA_VERSION",
    "CommitChannel",
    "CommitAuthorityVerdict",
    "CommitGrant",
    "CommitAuthorityContext",
    "CommitAuthorityVerdictResult",
    "GrantIssueOutcome",
    "is_authorized_verdict",
    "master_enabled",
    "default_ttl_s",
    "plan_mode_ttl_s",
    "grants_path",
    "secret_path",
    "enable_file_path",
    "persistent_enabled",
    "enable_authority",
    "disable_authority",
    "presence_file_path",
    "presence_ttl_s",
    "mint_operator_presence",
    "valid_operator_presence",
    "resolve_commit_channel",
    "resolve_repo_root_and_branch",
    "repo_root_fingerprint",
    "verify_pre_commit",
    "issue_grant",
    "revoke_grants",
    "consume_grant",
    "register_shipped_invariants",
    "register_flags",
]
