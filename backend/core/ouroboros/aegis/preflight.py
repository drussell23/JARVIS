"""Harness preflight — spawn Aegis subprocess + scrub JARVIS env.

This module is the SINGLE seam by which the harness wires up Aegis.
The outer entry point (``scripts/ouroboros_battle_test.py``) calls
:func:`aegis_preflight` **before** importing any provider module
(otherwise providers would already have captured the credential env
vars into module-level constants — e.g.,
``doubleword_provider.py:43`` reads ``DOUBLEWORD_API_KEY`` at import
time).

Bootstrap dance (matches the §43 spine):

  1. Generate a unique bootstrap-out path under :func:`bootstrap_dir`.
  2. Snapshot upstream credentials currently in the harness env.
  3. Spawn the Aegis daemon subprocess (``python -m
     backend.core.ouroboros.aegis.daemon``) with credentials in
     **its** env. Use ``close_fds=True`` and ``stdin=DEVNULL`` so the
     subprocess starts clean.
  4. Wait (poll-with-backoff) for the daemon to write its bootstrap
     payload to the chosen path. Timeout via
     ``JARVIS_AEGIS_BOOTSTRAP_TIMEOUT_S``.
  5. Read the payload, unlink it (forensic-trace-free).
  6. Validate ``expires_at`` is still in the future (rejects stale).
  7. Scrub the upstream credential env vars from the harness env.
  8. Assert post-scrub absence (binding-correction #6 hard invariant).
  9. Return the preflight result for the harness to consume.

Default-off: if :func:`flags.is_enabled` returns False, the function
short-circuits with a SKIPPED result and zero behavior change.
"""
from __future__ import annotations

import asyncio
import enum
import importlib.util
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple

from backend.core.ouroboros.aegis.bootstrap import (
    BootstrapPayload,
    read_and_unlink_payload,
)
from backend.core.ouroboros.aegis.credential_registry import (
    upstream_credential_env_vars,
)
from backend.core.ouroboros.aegis.env_scrub import (
    UpstreamCredentialPresentError,
    assert_no_upstream_credentials,
    scrub_upstream_credentials,
)
from backend.core.ouroboros.aegis.flags import (
    bootstrap_dir,
    bootstrap_timeout_s,
    is_enabled,
    register_aegis_flags,
)

logger = logging.getLogger(__name__)


PREFLIGHT_SCHEMA_VERSION: str = "aegis_preflight.1"


class PreflightOutcome(str, enum.Enum):
    """Closed 6-value outcome taxonomy for the preflight."""

    SKIPPED_DISABLED = "skipped_disabled"
    READY = "ready"
    FAILED_SPAWN = "failed_spawn"
    FAILED_BOOTSTRAP_TIMEOUT = "failed_bootstrap_timeout"
    FAILED_CREDENTIAL_SCRUB = "failed_credential_scrub"
    FAILED_DEPENDENCY_DRIFT = "failed_dependency_drift"


class DependencyStatus(str, enum.Enum):
    """Closed outcome taxonomy for the dependency-validation step."""

    SKIPPED = "skipped"          # step disabled
    INTACT = "intact"            # every declared package importable
    RECONCILED = "reconciled"    # drift found AND repaired in-process
    DRIFT = "drift"              # drift found, not repaired (reconcile off/failed)


def _dep_validation_enabled() -> bool:
    """Detect-and-report. Cheap (``find_spec`` only, no import, no network)."""
    return os.environ.get(
        "JARVIS_AEGIS_DEP_VALIDATION_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _dep_reconcile_enabled() -> bool:
    """Whether Aegis may ``pip install`` drift away during boot.

    Default OFF, deliberately. Reconciliation is the one part of this step that
    reaches the network and mutates the interpreter every other process shares,
    so arming it by default converts a transient PyPI outage into a failed boot
    — and this repo has no venv (``sys.prefix == sys.base_prefix``), so a bad
    resolution lands on the global pyenv, not a disposable sandbox. Detection is
    what makes drift *visible*; repair stays an explicit operator choice, and
    graduates the way every other flag here does once it has soaked."""
    return os.environ.get(
        "JARVIS_AEGIS_DEP_RECONCILE_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes", "on")


def _dep_strict() -> bool:
    """Whether unreconciled drift is fatal (FAILED_DEPENDENCY_DRIFT) rather than
    a warning the boot proceeds through. Default OFF — silent degradation is the
    bug we are fixing; a hard-down boot is not obviously better than a loud LITE
    tier, so the operator opts in."""
    return os.environ.get(
        "JARVIS_AEGIS_DEP_STRICT", "false",
    ).strip().lower() in ("1", "true", "yes", "on")


def _dep_reconcile_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("JARVIS_AEGIS_DEP_RECONCILE_TIMEOUT_S", "300")))
    except (TypeError, ValueError):
        return 300.0


# import_name -> pip requirement specifier. Packages whose absence degrades the
# organism SILENTLY rather than loudly — the class this step exists to catch.
# `sentence_transformers` is the charter member: without it EmbeddingService
# logs one WARNING and runs on the LITE tier indefinitely.
_DEFAULT_CRITICAL_DEPS: Tuple[Tuple[str, str], ...] = (
    ("sentence_transformers", "sentence-transformers==2.3.0"),
)


def _critical_deps() -> Tuple[Tuple[str, str], ...]:
    """Declared critical packages, overridable via ``JARVIS_AEGIS_DEP_CRITICAL``
    as a comma-separated ``import_name=pip_spec`` list. Malformed entries are
    skipped rather than raising — a typo must not brick boot."""
    raw = os.environ.get("JARVIS_AEGIS_DEP_CRITICAL", "").strip()
    if not raw:
        return _DEFAULT_CRITICAL_DEPS
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, spec = item.partition("=")
        name = name.strip()
        if name:
            out.append((name, (spec.strip() or name)))
    return tuple(out) or _DEFAULT_CRITICAL_DEPS


@dataclass(frozen=True)
class DependencyValidationStep:
    """Result of the boot-time dependency reconciliation step.

    ``missing`` is what was absent on entry; ``reconciled`` is what this step
    actually repaired; ``unresolved`` is what remains broken on exit. A step
    that repaired nothing and found nothing is ``INTACT``."""

    status: DependencyStatus = DependencyStatus.SKIPPED
    checked: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    reconciled: Tuple[str, ...] = ()
    unresolved: Tuple[str, ...] = ()
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "checked": list(self.checked),
            "missing": list(self.missing),
            "reconciled": list(self.reconciled),
            "unresolved": list(self.unresolved),
            "detail": self.detail,
        }


def _importable(module_name: str) -> bool:
    """True iff ``module_name`` can be located WITHOUT importing it. Import cost
    for something like torch is seconds; ``find_spec`` is microseconds and this
    runs on the boot path. Any resolution error counts as missing."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
        return False


def validate_dependencies() -> DependencyValidationStep:
    """Inspect declared critical packages and, when armed, reconcile drift.

    Root-cause step for the silent-degradation class: an absent package that
    the organism swallows into a permanently-degraded tier. Detection is always
    honest — the step reports what it found even when it cannot repair it.
    Never raises."""
    if not _dep_validation_enabled():
        return DependencyValidationStep(
            status=DependencyStatus.SKIPPED,
            detail="JARVIS_AEGIS_DEP_VALIDATION_ENABLED is false",
        )

    deps = _critical_deps()
    checked = tuple(name for name, _ in deps)
    missing = tuple(name for name, _ in deps if not _importable(name))

    if not missing:
        logger.info(
            "[AegisPreflight] dependency validation: INTACT (%d checked)", len(checked),
        )
        return DependencyValidationStep(
            status=DependencyStatus.INTACT, checked=checked,
        )

    specs = {name: spec for name, spec in deps}
    if not _dep_reconcile_enabled():
        logger.warning(
            "[AegisPreflight] dependency DRIFT: %s absent — reconciliation is "
            "disarmed (JARVIS_AEGIS_DEP_RECONCILE_ENABLED=false). The organism "
            "will run degraded. Repair with: pip install %s",
            ", ".join(missing), " ".join(specs[m] for m in missing),
        )
        return DependencyValidationStep(
            status=DependencyStatus.DRIFT, checked=checked, missing=missing,
            unresolved=missing,
            detail="reconciliation disarmed; run pip install manually",
        )

    reconciled = []
    unresolved = []
    for name in missing:
        spec = specs[name]
        logger.warning(
            "[AegisPreflight] dependency DRIFT: %s absent — reconciling (%s)", name, spec,
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-input", spec],
                capture_output=True, text=True, timeout=_dep_reconcile_timeout_s(),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            unresolved.append(name)
            logger.warning("[AegisPreflight] reconcile of %s errored: %s", name, exc)
            continue
        # `pip` exiting 0 is necessary but NOT sufficient — re-probe the actual
        # import path, because that is the thing the organism depends on.
        importlib.invalidate_caches()
        if proc.returncode == 0 and _importable(name):
            reconciled.append(name)
            logger.info("[AegisPreflight] reconciled %s", name)
        else:
            unresolved.append(name)
            logger.warning(
                "[AegisPreflight] reconcile of %s FAILED (rc=%s): %s",
                name, proc.returncode, (proc.stderr or "")[-400:],
            )

    if unresolved:
        return DependencyValidationStep(
            status=DependencyStatus.DRIFT, checked=checked, missing=missing,
            reconciled=tuple(reconciled), unresolved=tuple(unresolved),
            detail=f"{len(unresolved)} package(s) still unresolved after reconcile",
        )
    return DependencyValidationStep(
        status=DependencyStatus.RECONCILED, checked=checked, missing=missing,
        reconciled=tuple(reconciled),
    )


@dataclass(frozen=True)
class AegisPreflightResult:
    """Frozen preflight result. Lossless §33.5 to_dict/from_dict.

    On non-READY outcomes, ``aegis_url`` / ``bootstrap_psk`` are None
    and ``subprocess_pid`` is None. The harness inspects ``outcome``
    to decide whether to proceed (READY) or abort the session
    (any FAILED_* — Aegis is enabled but unhealthy is fatal per the
    operator's binding directive "Aegis death = session ends").
    """

    outcome: PreflightOutcome
    aegis_url: Optional[str] = None
    bootstrap_psk: Optional[str] = None
    subprocess_pid: Optional[int] = None
    detail: Optional[str] = None
    schema_version: str = PREFLIGHT_SCHEMA_VERSION
    # Additive (schema stays aegis_preflight.1 — new optional field, no
    # existing key changed meaning). None on paths that ran before the step.
    dependencies: Optional[DependencyValidationStep] = field(default=None)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "aegis_url": self.aegis_url,
            # NEVER include bootstrap_psk in to_dict output — it's a
            # credential. Caller has access via the dataclass field
            # directly; we don't put it in any payload that might be
            # logged or persisted.
            "subprocess_pid": self.subprocess_pid,
            "detail": self.detail,
            "schema_version": self.schema_version,
            "dependencies": (
                self.dependencies.to_dict() if self.dependencies is not None else None
            ),
        }


def _unique_bootstrap_path(dir_: Path) -> Path:
    """Generate a fresh bootstrap-out path with high-entropy suffix.

    Random suffix means concurrent harness invocations (e.g., parallel
    test runs) never collide on the O_EXCL atomic write.
    """
    dir_.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = secrets.token_hex(8)
    return dir_ / f"aegis-{os.getpid()}-{suffix}.json"


def _spawn_daemon(
    *,
    bootstrap_out: Path,
    credentials: Mapping[str, str],
    bind_host_override: Optional[str] = None,
) -> subprocess.Popen:
    """Spawn the Aegis daemon subprocess. Caller owns the lifecycle."""
    # Build the subprocess env: start from the current env (so PATH,
    # PYTHONPATH, etc. survive) BUT inject the credentials (which the
    # harness env is about to lose).
    #
    # ov cockpit silence (Slice 2 Task 2) -- presentation-mode survival:
    # ``JARVIS_OV_PRESENTATION`` is NOT an upstream credential (it isn't
    # in credential_registry.upstream_credential_env_vars()), so
    # scrub_upstream_credentials() below never touches it, AND this
    # spawn happens BEFORE that scrub call runs (see aegis_preflight's
    # step ordering) against a full ``dict(os.environ)`` snapshot -- so
    # the daemon subprocess already inherits it structurally, with no
    # allowlist entry needed. Carrying it in BootstrapPayload instead
    # was considered and rejected: that payload is a ONE-WAY handshake
    # (daemon writes -> harness reads, see bootstrap.py) -- there is no
    # channel on it for the harness to hand settings TO the daemon, so
    # it cannot carry a harness-side var into the child at all. Pinned
    # by tests/aegis/test_daemon_presentation.py.
    sub_env = dict(os.environ)
    sub_env.update(credentials)

    # Make ``-m backend.core.ouroboros.aegis.daemon`` resolve regardless of the
    # parent's cwd. Production runs the organism from the repo root (so cwd is on
    # sys.path), but the isomorphic soak deliberately runs from a DISJOINT cwd to
    # surface exactly this class of fragility -- without an explicit repo-root on
    # PYTHONPATH the daemon dies with ModuleNotFoundError: No module named
    # 'backend'. Prepend the repo root (the dir containing ``backend/``) so the
    # spawn is cwd-independent in every environment.
    _repo_root = str(Path(__file__).resolve().parents[4])
    _existing_pp = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = (
        _repo_root + (os.pathsep + _existing_pp if _existing_pp else "")
    )

    cmd = [
        sys.executable,
        "-m",
        "backend.core.ouroboros.aegis.daemon",
        "--bootstrap-out",
        str(bootstrap_out),
        # Slice 22 — ship the spawner PID so the daemon can arm its
        # WorkerLifeline against the real parent (race-safe orphan self-exit).
        "--parent-pid",
        str(os.getpid()),
    ]
    if bind_host_override:
        cmd.extend(["--bind-host", bind_host_override])

    proc = subprocess.Popen(
        cmd,
        env=sub_env,
        stdin=subprocess.DEVNULL,
        # stdout/stderr go to the harness's tty/log — operator can
        # tail them. The daemon's logging format is prefixed
        # `aegis-daemon` for easy grep.
        close_fds=True,
    )
    # Slice 22 — register the daemon for parent-side cascade teardown. Belt to
    # the daemon's own lifeline braces: the reaper is the fast graceful/pre-exit
    # path, the lifeline the pure-SIGKILL backstop. Fail-soft.
    try:
        from backend.core.ouroboros.governance.child_reaper import register_child
        register_child(proc.pid, role="aegis_daemon")
    except Exception:  # noqa: BLE001 — never break the spawn
        pass
    return proc


async def _await_bootstrap_payload(
    path: Path,
    *,
    timeout_s: int,
    proc: subprocess.Popen,
) -> Optional[BootstrapPayload]:
    """Poll for the bootstrap-payload file to appear.

    Returns the parsed payload on success, None on timeout or
    subprocess death.

    Polls with exponential backoff capped at 100ms — fast enough
    that a quick boot (<50ms) is observed promptly, slow enough
    that a hung boot doesn't burn CPU.
    """
    deadline = time.monotonic() + float(timeout_s)
    backoff = 0.010  # 10ms initial
    max_backoff = 0.100

    while time.monotonic() < deadline:
        # Subprocess crashed?
        ret = proc.poll()
        if ret is not None:
            logger.error(
                "[AegisPreflight] daemon subprocess exited prematurely "
                "with code %d before writing payload", ret,
            )
            return None

        if path.exists():
            try:
                payload = read_and_unlink_payload(path)
                return payload
            except (FileNotFoundError, ValueError, OSError) as exc:
                logger.warning(
                    "[AegisPreflight] payload at %s failed to parse: %s",
                    path, exc,
                )
                # Don't loop on a malformed payload — return None
                # so the caller can treat as failure.
                return None

        await asyncio.sleep(backoff)
        backoff = min(max_backoff, backoff * 1.5)

    return None


async def aegis_preflight(
    *,
    env: Optional[dict] = None,
    bind_host_override: Optional[str] = None,
) -> AegisPreflightResult:
    """Run the full Aegis preflight handshake.

    ``env`` defaults to ``os.environ``. When passed explicitly (tests),
    the scrub mutates that dict only — handy for asserting "JARVIS
    env is empty of upstream creds post-preflight" without touching
    the real environment.

    Always async (we await the bootstrap-payload poll). On success,
    returns READY with ``aegis_url`` + ``bootstrap_psk`` + ``pid``.
    On any failure, returns a FAILED_* outcome with ``detail``.
    Never raises.
    """
    target_env = os.environ if env is None else env

    register_aegis_flags()  # idempotent

    # Dependency validation runs BEFORE the enablement gate and before the
    # daemon spawn: it is a property of the interpreter the whole organism is
    # about to run on, not of Aegis itself, so an operator who disabled Aegis
    # still gets told their environment drifted. Cheap (find_spec only) unless
    # reconciliation is explicitly armed.
    deps = validate_dependencies()

    if not is_enabled():
        return AegisPreflightResult(
            outcome=PreflightOutcome.SKIPPED_DISABLED,
            detail="JARVIS_AEGIS_ENABLED is false (Slice 1 default)",
            dependencies=deps,
        )

    if deps.status is DependencyStatus.DRIFT and _dep_strict():
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_DEPENDENCY_DRIFT,
            detail=(
                f"unresolved dependency drift: {', '.join(deps.unresolved)} "
                f"(JARVIS_AEGIS_DEP_STRICT is on)"
            ),
            dependencies=deps,
        )

    # Slice 125 — ROOT INVARIANT: the funded provider keys from the operator-
    # approved .env must be in the env BEFORE we snapshot. Otherwise the daemon
    # is spawned with an absent key, injects nothing, and the upstream bills the
    # request against its free ($0) tier → a misleading 402 "balance too low"
    # that masquerades as out-of-credits but is really a credential-injection
    # gap. The loader is allowlist-only, never overwrites an explicit export,
    # never source-execs .env, and logs only redacted fingerprints.
    try:
        from backend.core.ouroboros.aegis.credential_env_loader import (
            format_report,
            load_provider_credentials,
        )

        _cred_report = load_provider_credentials(env=target_env)
        logger.info("%s", format_report(_cred_report))
    except Exception as exc:  # noqa: BLE001 - never block boot on the loader
        logger.debug("[AegisPreflight] credential bootstrap skipped: %s", exc.__class__.__name__)

    # Snapshot credentials — we need them to hand to the subprocess
    # BEFORE we strip them from our env.
    creds = {
        name: target_env[name]
        for name in upstream_credential_env_vars()
        if name in target_env
    }

    bootstrap_out = _unique_bootstrap_path(bootstrap_dir())

    try:
        proc = _spawn_daemon(
            bootstrap_out=bootstrap_out,
            credentials=creds,
            bind_host_override=bind_host_override,
        )
    except (OSError, ValueError) as exc:
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_SPAWN,
            detail=f"subprocess spawn failed: {exc}",
            dependencies=deps,
        )

    payload = await _await_bootstrap_payload(
        bootstrap_out, timeout_s=bootstrap_timeout_s(), proc=proc,
    )
    if payload is None:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_BOOTSTRAP_TIMEOUT,
            subprocess_pid=proc.pid,
            detail=(
                f"daemon did not write bootstrap payload within "
                f"{bootstrap_timeout_s()}s"
            ),
            dependencies=deps,
        )

    # Defense: reject a stale payload (a previous boot's leftover the
    # daemon somehow re-served). Should never happen given O_EXCL +
    # unique-path, but cheap to check.
    if time.time() >= payload.expires_at:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_BOOTSTRAP_TIMEOUT,
            subprocess_pid=proc.pid,
            detail="bootstrap payload expired before harness read it",
            dependencies=deps,
        )

    # Scrub credentials from the harness env. ``creds`` already holds
    # the values; the env is now safe to lose them.
    scrub_upstream_credentials(target_env)
    try:
        assert_no_upstream_credentials(target_env)
    except UpstreamCredentialPresentError as exc:
        try:
            proc.terminate()
        except OSError:
            pass
        return AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_CREDENTIAL_SCRUB,
            subprocess_pid=proc.pid,
            detail=str(exc),
            dependencies=deps,
        )

    # Expose Aegis coordinates to JARVIS via env. Slice 2's provider
    # rewrite will consume these.
    target_env["JARVIS_AEGIS_URL"] = payload.aegis_url
    target_env["JARVIS_AEGIS_BOOTSTRAP_PSK"] = payload.bootstrap_psk

    return AegisPreflightResult(
        outcome=PreflightOutcome.READY,
        aegis_url=payload.aegis_url,
        bootstrap_psk=payload.bootstrap_psk,
        subprocess_pid=payload.daemon_pid,
        detail=f"daemon pid={payload.daemon_pid}",
        dependencies=deps,
    )


__all__ = [
    "AegisPreflightResult",
    "DependencyStatus",
    "DependencyValidationStep",
    "PREFLIGHT_SCHEMA_VERSION",
    "PreflightOutcome",
    "aegis_preflight",
    "validate_dependencies",
]
