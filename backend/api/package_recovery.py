"""Dynamic Package Recovery — the Self-Healing engine (Phase 12, Slice E).

When the Loopback Self-Test's fallback provider raises a ``ModuleNotFoundError``
for a missing *transitive* dependency (e.g. a lean hermetic venv that never got
``uuid6``), the supervisor must not immediately degrade. It self-heals: it
intercepts the fault, runs a scoped ephemeral ``pip install`` into the ACTIVE
interpreter (the hermetic venv under ``--headless``), hot-reloads the import
cache (``importlib.invalidate_caches()``), and re-probes the module. Only if the
secondary attempt fails does the caller degrade gracefully.

SECURITY — governed allowlist (mandate 2, advanced edge cases): an autonomous
installer that ``pip install``s an arbitrary string parsed from exception text
is a supply-chain injection vector (typosquat / malformed module name). So a
missing module is resolved to a pip spec through a GOVERNED, env-extensible
allowlist registry — never raw error text. An unknown module is refused
(``NOT_ALLOWED``) and the caller degrades. Resolved specs are additionally
regex-validated before they ever reach the argv, and the subprocess is invoked
with an argv list (never a shell), so no metacharacter can be interpreted.

DRY (mandate 3): mirrors the ``trinity_env.bootstrap_env`` subprocess pattern
exactly — an injectable ``runner`` defaulting to ``subprocess.run`` invoked
``[python, "-m", "pip", "install", …]`` with ``capture_output`` + ``timeout``.
It targets the SAME interpreter the import must succeed in (``sys.executable``),
so a hot-injected package lands where the running process can import it.

Robustness: a once-per-module attempt ledger (no install storms), an
``asyncio.Lock`` (concurrent self-tests can't double-install), a subprocess
timeout, and a top-level guard so every public entry point NEVER raises. The
blocking ``pip`` runs off the event loop via ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger("Jarvis.PackageRecovery")

RECOVERY_TOPIC = "ouroboros.recovery"

#: Parses ``No module named 'uuid6'`` / ``No module named foo.bar`` — the exact
#: shapes ``ModuleNotFoundError`` emits — into the missing module name.
_MODULE_RE = re.compile(
    r"no module named ['\"]?([A-Za-z0-9_][A-Za-z0-9_.]*)['\"]?", re.IGNORECASE)

#: A resolved pip spec must match this before it can reach the argv. Allows a
#: PEP 440 name + optional single version constraint; nothing else.
_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*"
    r"((==|>=|<=|~=|!=|<|>)[A-Za-z0-9_.\-\*]+)?$")

#: The seed governed allowlist (module → pip spec). Env-extensible; the
#: security control is membership here, NOT the error text. ``uuid6`` closes
#: the known transitive gap in the OuroborosDaemon / DoubleWord client stack.
_DEFAULT_ALLOW: Dict[str, str] = {
    "uuid6": "uuid6",
}


class RecoveryState(str, Enum):
    RECOVERED = "recovered"                # installed + re-imported successfully
    NOT_ALLOWED = "not_allowed"            # module absent from governed allowlist
    ALREADY_ATTEMPTED = "already_attempted"  # tried this session, still missing
    INSTALL_FAILED = "install_failed"      # pip subprocess non-zero / errored
    REIMPORT_FAILED = "reimport_failed"    # pip ok but import still fails
    INVALID_SPEC = "invalid_spec"          # resolved spec failed safety regex
    DISABLED = "disabled"                  # master switch off


@dataclass
class RecoveryResult:
    state: RecoveryState
    module: str = ""
    spec: str = ""
    detail: str = ""
    argv: tuple = ()

    @property
    def ok(self) -> bool:
        return self.state is RecoveryState.RECOVERED


# ---------------------------------------------------------------------------
# env-driven config (no hardcoding — every knob overridable)
# ---------------------------------------------------------------------------

def recovery_enabled() -> bool:
    return (os.environ.get("JARVIS_PKG_RECOVERY_ENABLED", "true").strip().lower()
            not in ("0", "false", "no", "off", ""))


def _timeout_s() -> float:
    try:
        return max(10.0, float(os.environ.get("JARVIS_PKG_RECOVERY_TIMEOUT_S", "180")))
    except (TypeError, ValueError):
        return 180.0


def extract_missing_module(text: Optional[str]) -> Optional[str]:
    """Pull the missing module name out of a ``ModuleNotFoundError`` string.
    Returns ``None`` when the text is not a missing-module fault."""
    if not text:
        return None
    m = _MODULE_RE.search(text)
    return m.group(1) if m else None


def load_allowlist() -> Dict[str, str]:
    """The governed module→spec allowlist: seed ∪ ``JARVIS_PKG_RECOVERY_MAP``
    (JSON dict, lets an operator pin versions / add modules) ∪
    ``JARVIS_PKG_RECOVERY_ALLOW`` (comma names, identity spec). NEVER raises."""
    allow = dict(_DEFAULT_ALLOW)
    raw = os.environ.get("JARVIS_PKG_RECOVERY_MAP", "").strip()
    if raw:
        try:
            ext = json.loads(raw)
            if isinstance(ext, dict):
                for k, v in ext.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        allow[k.strip()] = v.strip()
        except Exception:  # noqa: BLE001
            logger.warning("[PkgRecovery] malformed JARVIS_PKG_RECOVERY_MAP JSON "
                           "— ignored", exc_info=True)
    names = os.environ.get("JARVIS_PKG_RECOVERY_ALLOW", "").strip()
    if names:
        for n in names.split(","):
            n = n.strip()
            if n and n not in allow:
                allow[n] = n
    return allow


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class DynamicPackageRecovery:
    """Self-heal a missing transitive dependency by scoped ``pip install`` +
    hot module reload. Fully injectable for tests (``runner`` / ``import_probe``
    / ``python_exe`` / ``allowlist``) so no real network is touched. NEVER
    raises out of :meth:`recover`."""

    def __init__(
        self,
        *,
        runner: Optional[Callable[..., Any]] = None,
        import_probe: Optional[Callable[[str], bool]] = None,
        python_exe: Optional[str] = None,
        allowlist: Optional[Dict[str, str]] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._runner = runner            # late-bind subprocess.run (DRY)
        self._import_probe = import_probe
        self._python = python_exe
        self._allow = allowlist
        self._timeout = timeout_s
        self._attempted: Set[str] = set()
        self._lock = asyncio.Lock()

    # -- resolution helpers --------------------------------------------------

    def _py(self) -> str:
        """The interpreter to install into — the one the import must succeed
        in. Under ``--headless`` ``sys.executable`` IS the hermetic venv."""
        if self._python:
            return str(self._python)
        return sys.executable or "python3"

    def _allowmap(self) -> Dict[str, str]:
        return self._allow if self._allow is not None else load_allowlist()

    def _probe(self, module: str) -> bool:
        """Is ``module`` importable now? Invalidates caches first (hot reload)."""
        if self._import_probe is not None:
            try:
                return bool(self._import_probe(module))
            except Exception:  # noqa: BLE001
                return False
        try:
            importlib.invalidate_caches()
            importlib.import_module(module.split(".")[0])
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- the recovery -------------------------------------------------------

    async def recover(self, module: Optional[str]) -> RecoveryResult:
        """Attempt to self-heal ``module``. NEVER raises."""
        try:
            if not recovery_enabled():
                return RecoveryResult(RecoveryState.DISABLED, module=module or "")
            top = (module or "").split(".")[0].strip()
            if not top:
                return RecoveryResult(RecoveryState.NOT_ALLOWED, module=module or "",
                                      detail="no module name")
            allow = self._allowmap()
            if top not in allow:
                logger.warning("[PkgRecovery] '%s' NOT in governed allowlist — "
                               "refusing autonomous install (supply-chain guard)", top)
                return RecoveryResult(RecoveryState.NOT_ALLOWED, module=top,
                                      detail="module not in governed recovery allowlist")
            spec = allow[top].strip()
            if not _SPEC_RE.match(spec):
                return RecoveryResult(RecoveryState.INVALID_SPEC, module=top, spec=spec,
                                      detail="resolved spec failed safety validation")

            async with self._lock:
                # Idempotency: never install the same module twice per session.
                if top in self._attempted:
                    if self._probe(top):
                        return RecoveryResult(RecoveryState.RECOVERED, module=top,
                                              spec=spec, detail="already present")
                    return RecoveryResult(RecoveryState.ALREADY_ATTEMPTED, module=top, spec=spec)
                self._attempted.add(top)

                argv = [self._py(), "-m", "pip", "install", "--no-input",
                        "--disable-pip-version-check", "--quiet", spec]
                run = self._runner or subprocess.run
                logger.info("[PkgRecovery] self-healing '%s' → %s into %s",
                            top, spec, self._py())
                try:
                    # Off the event loop — pip is blocking (mandate 2/DRY).
                    r = await asyncio.to_thread(
                        run, argv, capture_output=True, text=True,
                        timeout=(self._timeout if self._timeout is not None else _timeout_s()))
                except Exception as exc:  # noqa: BLE001 — timeout / OSError
                    return RecoveryResult(RecoveryState.INSTALL_FAILED, module=top, spec=spec,
                                          detail=f"pip subprocess error: {exc}",
                                          argv=tuple(argv))
                rc = getattr(r, "returncode", 1)
                if rc != 0:
                    err = (getattr(r, "stderr", "") or "")[-300:]
                    return RecoveryResult(RecoveryState.INSTALL_FAILED, module=top, spec=spec,
                                          detail=f"pip rc={rc}: {err}", argv=tuple(argv))

                # Hot module reload + re-probe (mandate 2).
                importlib.invalidate_caches()
                if self._probe(top):
                    logger.info("[PkgRecovery] '%s' healed — import restored", top)
                    return RecoveryResult(RecoveryState.RECOVERED, module=top, spec=spec,
                                          argv=tuple(argv))
                return RecoveryResult(RecoveryState.REIMPORT_FAILED, module=top, spec=spec,
                                      detail="installed but import still failed",
                                      argv=tuple(argv))
        except Exception as exc:  # noqa: BLE001 — belt + braces, never raises
            logger.warning("[PkgRecovery] unexpected recovery fault", exc_info=True)
            return RecoveryResult(RecoveryState.INSTALL_FAILED, module=module or "",
                                  detail=f"unexpected: {exc}")


def default_recovery() -> Optional[DynamicPackageRecovery]:
    """A production recovery engine, or ``None`` when disabled (so callers
    treat a missing dep as an ordinary degrade)."""
    return DynamicPackageRecovery() if recovery_enabled() else None


__all__ = [
    "RECOVERY_TOPIC", "RecoveryState", "RecoveryResult",
    "DynamicPackageRecovery", "extract_missing_module", "load_allowlist",
    "recovery_enabled", "default_recovery",
]
