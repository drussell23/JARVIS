"""Silent boot — terminal stays clean during boot; logs go to file.

Closes the "boot log spam" UX gap. Pre-substrate, ~25+ INFO log lines
from Oracle / Integration / Controller / etc. dump to the terminal
during boot (per the operator's screenshot 2026-05-03 21:01:51-52),
drowning out the carefully-designed boot banner. Claude Code's boot
is essentially silent — only the banner + a tip render before the
prompt. This substrate gives O+V the same discipline.

Architecture (each pillar load-bearing):

  1. **Single point of configuration** — :func:`configure_silent_boot`
     is the one function callers invoke; it owns the entire root-
     logger surgery. No scattered ``logging.basicConfig`` calls; no
     module-level handler installations elsewhere should compete.
  2. **Idempotent** — multiple calls are safe. We tag our handler
     with a marker attribute (``_silent_boot_installed``) and detect
     prior installation. Re-call from a re-entrant boot path is a
     no-op. Critical for tests + interactive ``ipython -i`` workflows.
  3. **Defensive everywhere** — if the file path can't be created,
     if the root logger is in a weird state, if a competing handler
     is installed, we degrade to "terminal stays as-is" rather than
     crashing the boot. Boot is NEVER blocked by logging glue.
  4. **Hot-revertible** — ``JARVIS_SILENT_BOOT_ENABLED=false``
     restores legacy "all logs to terminal" behavior. Operators
     debugging boot issues flip the flag without touching code.
  5. **Configurable terminal threshold** — defaults to ``WARNING``
     so error-level events still reach the operator, but the noisy
     INFO chatter goes only to the file. Operators can override via
     ``JARVIS_SILENT_BOOT_TERMINAL_LEVEL`` (DEBUG/INFO/WARNING/ERROR).

Authority invariants (AST-pinned):

  * No imports of ``rich`` / ``rich.*``.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / cancel_token / conversation_bridge.
  * ``configure_silent_boot`` symbol present at module level.
  * ``register_flags`` + ``register_shipped_invariants`` symbols
    present (auto-discovery contract).
  * Cross-file pin: ``harness.py`` MUST contain
    ``configure_silent_boot`` call (catches a refactor that drops
    the boot wire and lets log spam return).

Kill switches:

  * ``JARVIS_SILENT_BOOT_ENABLED`` — master gate. Default ``true``.
    Hot-revert via ``=false`` returns to pre-substrate behavior
    (all log levels to terminal).
  * ``JARVIS_SILENT_BOOT_TERMINAL_LEVEL`` — terminal handler threshold.
    Default ``WARNING``. Closed taxonomy: ``DEBUG`` / ``INFO`` /
    ``WARNING`` / ``ERROR`` / ``CRITICAL``. Unknown values fall back
    to ``WARNING``.
  * ``JARVIS_SILENT_BOOT_LOG_FILENAME`` — filename inside the supplied
    session_dir. Default ``debug.log``. Operators may use ``boot.log``
    if they want a separate file from the existing debug.log.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional


logger = logging.getLogger(__name__)


SILENT_BOOT_SCHEMA_VERSION: str = "silent_boot.1"


_FLAG_SILENT_BOOT_ENABLED = "JARVIS_SILENT_BOOT_ENABLED"
_FLAG_SILENT_BOOT_TERMINAL_LEVEL = "JARVIS_SILENT_BOOT_TERMINAL_LEVEL"
_FLAG_SILENT_BOOT_LOG_FILENAME = "JARVIS_SILENT_BOOT_LOG_FILENAME"


# Marker attribute used to identify our installed handler. Lets
# :func:`configure_silent_boot` detect prior installation and skip
# re-installing — idempotency by construction.
_HANDLER_MARKER: str = "_silent_boot_installed"


# Closed taxonomy of allowed terminal-handler thresholds. Mirrors
# Python's logging level vocabulary; AST-pinned.
_LEVEL_NAMES: tuple = (
    "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
)


# Module-level lock to serialize concurrent configure_silent_boot
# calls. Logging configuration is not natively thread-safe across
# concurrent re-installs; the lock prevents interleaved handler
# manipulation from leaving the root logger in a broken state.
_CONFIG_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Flag accessors
# ---------------------------------------------------------------------------


def _get_registry() -> Any:
    try:
        from backend.core.ouroboros.governance import flag_registry as _fr
        return _fr.ensure_seeded()
    except Exception:  # noqa: BLE001 — defensive
        return None


def is_enabled() -> bool:
    """Master gate. Default ``true`` — silent boot is the desired
    default per the operator screenshot. Hot-revert: ``=false``
    returns to legacy behavior (every log level reaches the
    terminal)."""
    reg = _get_registry()
    if reg is None:
        return True
    return reg.get_bool(_FLAG_SILENT_BOOT_ENABLED, default=True)


def terminal_level() -> int:
    """Resolve the terminal-handler threshold from the flag.
    Defaults to ``WARNING``. Unknown values fall back to ``WARNING``.
    Closed taxonomy: ``DEBUG / INFO / WARNING / ERROR / CRITICAL``."""
    reg = _get_registry()
    if reg is None:
        return logging.WARNING
    raw = reg.get_str(
        _FLAG_SILENT_BOOT_TERMINAL_LEVEL, default="WARNING",
    ).strip().upper()
    if raw not in _LEVEL_NAMES:
        return logging.WARNING
    return getattr(logging, raw, logging.WARNING)


def log_filename() -> str:
    """Filename inside the supplied session_dir. Default
    ``debug.log``. Operators using a separate boot log set
    ``JARVIS_SILENT_BOOT_LOG_FILENAME=boot.log`` etc."""
    reg = _get_registry()
    if reg is None:
        return "debug.log"
    name = reg.get_str(
        _FLAG_SILENT_BOOT_LOG_FILENAME, default="debug.log",
    ).strip()
    return name or "debug.log"


# ---------------------------------------------------------------------------
# Public API: configure_silent_boot
# ---------------------------------------------------------------------------


def configure_silent_boot(
    session_dir: Any,
    *,
    terminal_threshold: Optional[int] = None,
    log_filename_override: Optional[str] = None,
) -> Optional[logging.FileHandler]:
    """Reconfigure the root logger so boot-time INFO/DEBUG goes to
    ``session_dir/debug.log`` and only WARNING+ surfaces on terminal.

    Returns the installed :class:`FileHandler` so callers (the
    harness) can retain a reference for explicit close on shutdown.
    Returns ``None`` when disabled, when setup fails, or when an
    existing :func:`configure_silent_boot` call has already
    installed the handler (idempotency).

    Defensive contract:
      * Master flag off → returns None, root logger untouched.
      * session_dir creation failure → returns None, root logger
        untouched.
      * Existing installed file handler with our marker → returns
        the existing handler (no-op).
      * Per-handler removal failure → swallowed; we proceed with
        whatever handlers remain.
      * NEVER raises. Boot is not blocked by logging glue.

    The terminal handler installed at the configured threshold
    (default WARNING) means error-level events still surface to the
    operator, but the noisy INFO chatter goes only to the file.
    """
    if not is_enabled():
        return None

    with _CONFIG_LOCK:
        try:
            return _configure_locked(
                session_dir=session_dir,
                terminal_threshold=terminal_threshold,
                log_filename_override=log_filename_override,
            )
        except Exception:  # noqa: BLE001 — never block boot
            logger.debug(
                "[silent_boot] configure failed", exc_info=True,
            )
            return None


def _configure_locked(
    *,
    session_dir: Any,
    terminal_threshold: Optional[int],
    log_filename_override: Optional[str],
) -> Optional[logging.FileHandler]:
    """Internal — actual configuration logic, runs under the module
    lock. Same defensive contract as the public wrapper."""
    root = logging.getLogger()

    # Idempotency: if our marker handler is already installed,
    # return it. Caller may have called us a second time during a
    # re-entrant boot; no-op + return the existing handle.
    for h in root.handlers:
        if getattr(h, _HANDLER_MARKER, False) and isinstance(
            h, logging.FileHandler,
        ):
            return h

    # Resolve session_dir + log path. Failures here mean we can't
    # safely redirect — return None, leave the root logger as-is.
    try:
        sdir = Path(session_dir)
        sdir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[silent_boot] session_dir creation failed",
            exc_info=True,
        )
        return None

    fname = (
        log_filename_override if log_filename_override
        else log_filename()
    )
    log_path = sdir / fname

    # Install file handler at DEBUG level — full fidelity in the file.
    try:
        file_handler = logging.FileHandler(
            str(log_path), encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        # Mark so future re-calls skip re-install.
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[silent_boot] file handler install failed",
            exc_info=True,
        )
        return None

    # Remove all OTHER stream handlers from root (the noisy ones
    # that dump INFO chatter to terminal). We keep our file handler.
    # Per-handler removal failure is swallowed — we proceed with
    # whatever handlers remain.
    for h in list(root.handlers):
        if h is file_handler:
            continue
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler,
        ):
            try:
                root.removeHandler(h)
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[silent_boot] removeHandler failed",
                    exc_info=True,
                )

    # Install a NEW terminal stream handler at the configured
    # threshold (default WARNING). This is the operator-facing
    # surface — error/warning events still reach them; INFO chatter
    # goes only to the file.
    try:
        threshold = (
            terminal_threshold if terminal_threshold is not None
            else terminal_level()
        )
        term_handler = logging.StreamHandler(stream=sys.stderr)
        term_handler.setLevel(threshold)
        term_handler.setFormatter(logging.Formatter(
            # Terse format — matches CC's restraint
            "%(levelname)s %(message)s",
        ))
        setattr(term_handler, _HANDLER_MARKER, True)
        root.addHandler(term_handler)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[silent_boot] terminal handler install failed",
            exc_info=True,
        )

    # Ensure root logger's own level allows DEBUG to flow to the
    # file handler. If root level is WARNING, INFO records never
    # propagate. Set root to DEBUG so file handler sees everything.
    root.setLevel(logging.DEBUG)

    return file_handler


def restore_legacy_terminal_logging() -> int:
    """Test / debug helper — undoes silent boot. Removes our marked
    handlers + restores a default StreamHandler at INFO. Returns
    count of marked handlers removed. NEVER raises."""
    with _CONFIG_LOCK:
        root = logging.getLogger()
        removed = 0
        for h in list(root.handlers):
            if getattr(h, _HANDLER_MARKER, False):
                try:
                    root.removeHandler(h)
                    removed += 1
                except Exception:  # noqa: BLE001 — defensive
                    pass
        # Re-install a default stream handler so callers see something.
        try:
            default_handler = logging.StreamHandler(stream=sys.stderr)
            default_handler.setLevel(logging.INFO)
            root.addHandler(default_handler)
        except Exception:  # noqa: BLE001 — defensive
            pass
        return removed


# ---------------------------------------------------------------------------
# FlagRegistry registration — auto-discovered
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
            Relevance,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    all_postures_relevant = {
        "EXPLORE": Relevance.RELEVANT,
        "CONSOLIDATE": Relevance.RELEVANT,
        "HARDEN": Relevance.RELEVANT,
        "MAINTAIN": Relevance.RELEVANT,
    }
    specs = [
        FlagSpec(
            name=_FLAG_SILENT_BOOT_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master gate for silent boot (D1 substrate). When "
                "true, boot-time INFO/DEBUG logs route to "
                "session_dir/debug.log and the terminal sees only "
                "WARNING+ — banner + prompt render cleanly. Hot-"
                "revert: false → legacy 'all logs to terminal' "
                "behavior."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/silent_boot.py"
            ),
            example="true",
            since="v1.0",
            posture_relevance=all_postures_relevant,
        ),
        FlagSpec(
            name=_FLAG_SILENT_BOOT_TERMINAL_LEVEL,
            type=FlagType.STR,
            default="WARNING",
            description=(
                "Terminal stream handler threshold. Default "
                "'WARNING' — only warnings/errors surface on terminal "
                "during boot. Closed taxonomy: DEBUG / INFO / "
                "WARNING / ERROR / CRITICAL. Unknown values fall "
                "back to WARNING."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/silent_boot.py"
            ),
            example="WARNING",
            since="v1.0",
        ),
        FlagSpec(
            name=_FLAG_SILENT_BOOT_LOG_FILENAME,
            type=FlagType.STR,
            default="debug.log",
            description=(
                "Filename inside the supplied session_dir for the "
                "file handler. Default 'debug.log' (matches existing "
                "harness convention). Operators using a separate "
                "boot log: set to 'boot.log' or similar."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/silent_boot.py"
            ),
            example="debug.log",
            since="v1.0",
        ),
    ]
    registry.bulk_register(specs, override=True)
    return len(specs)


# ---------------------------------------------------------------------------
# AST invariants — auto-discovered
# ---------------------------------------------------------------------------


_FORBIDDEN_RICH_PREFIX: tuple = ("rich",)
_FORBIDDEN_AUTHORITY_MODULES: tuple = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.risk_tier",
    "backend.core.ouroboros.governance.risk_tier_floor",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.gate",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
    "backend.core.ouroboros.governance.cancel_token",
    "backend.core.ouroboros.governance.conversation_bridge",
)


def _imported_modules(tree: Any) -> List:
    import ast
    out: List = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod:
                out.append((node.lineno, mod))
    return out


def _validate_no_rich_import(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        for forbidden in _FORBIDDEN_RICH_PREFIX:
            if mod == forbidden or mod.startswith(forbidden + "."):
                violations.append(
                    f"line {lineno}: forbidden rich import: {mod!r}"
                )
    return tuple(violations)


def _validate_no_authority_imports(tree: Any, source: str) -> tuple:
    del source
    violations: List[str] = []
    for lineno, mod in _imported_modules(tree):
        if mod in _FORBIDDEN_AUTHORITY_MODULES:
            violations.append(
                f"line {lineno}: forbidden authority import: {mod!r}"
            )
    return tuple(violations)


def _validate_configure_symbol_present(
    tree: Any, source: str,
) -> tuple:
    """``configure_silent_boot`` MUST be a module-level function so
    callers can import it directly."""
    del source
    import ast
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "configure_silent_boot":
                return ()
    return ("configure_silent_boot symbol not found at module level",)


def _validate_discovery_symbols_present(
    tree: Any, source: str,
) -> tuple:
    del source
    import ast
    needed = {"register_flags", "register_shipped_invariants"}
    found: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in needed:
                found.add(node.name)
    missing = needed - found
    if missing:
        return (f"missing discovery symbols: {sorted(missing)}",)
    return ()


def _validate_harness_calls_silent_boot(
    tree: Any, source: str,
) -> tuple:
    """Cross-file pin: harness.py MUST contain BOTH the import path
    AND a call to ``configure_silent_boot(``. Substring-of-name alone
    is too loose (a comment "without configure_silent_boot" would
    match); require the import + the call-syntax open-paren so the
    pin catches an actual orphan."""
    del tree
    required_tokens = (
        "from backend.core.ouroboros.governance.silent_boot",
        "configure_silent_boot(",
    )
    missing = [t for t in required_tokens if t not in source]
    if missing:
        return (
            f"harness.py missing silent_boot wiring tokens: {missing} "
            f"— boot log spam will return",
        )
    return ()


_TARGET_FILE = (
    "backend/core/ouroboros/governance/silent_boot.py"
)
_HARNESS_TARGET = (
    "backend/core/ouroboros/battle_test/harness.py"
)


def register_shipped_invariants() -> List:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except Exception:  # noqa: BLE001 — defensive
        return []
    return [
        ShippedCodeInvariant(
            invariant_name="silent_boot_no_rich_import",
            target_file=_TARGET_FILE,
            description=(
                "silent_boot.py MUST NOT import rich.* — the "
                "substrate is pure stdlib logging surgery; rendering "
                "is downstream's concern."
            ),
            validate=_validate_no_rich_import,
        ),
        ShippedCodeInvariant(
            invariant_name="silent_boot_no_authority_imports",
            target_file=_TARGET_FILE,
            description=(
                "silent_boot.py MUST NOT import any authority module. "
                "Logging configuration is descriptive only — never a "
                "control-flow surface."
            ),
            validate=_validate_no_authority_imports,
        ),
        ShippedCodeInvariant(
            invariant_name="silent_boot_configure_symbol_present",
            target_file=_TARGET_FILE,
            description=(
                "configure_silent_boot MUST be a module-level "
                "function so the harness can import + call it. "
                "Pinned so future refactors don't accidentally hide "
                "it inside a class or rename it."
            ),
            validate=_validate_configure_symbol_present,
        ),
        ShippedCodeInvariant(
            invariant_name="silent_boot_discovery_symbols_present",
            target_file=_TARGET_FILE,
            description=(
                "register_flags + register_shipped_invariants must "
                "be module-level so dynamic discovery picks them up."
            ),
            validate=_validate_discovery_symbols_present,
        ),
        ShippedCodeInvariant(
            invariant_name="harness_calls_silent_boot",
            target_file=_HARNESS_TARGET,
            description=(
                "Cross-file pin: harness.py MUST contain a call to "
                "configure_silent_boot. Without this, the substrate "
                "is orphaned and boot log spam returns. Catches a "
                "refactor that silently drops the wire — a regression "
                "that's invisible until an operator complains about "
                "the noisy boot."
            ),
            validate=_validate_harness_calls_silent_boot,
        ),
    ]


__all__ = [
    "SILENT_BOOT_SCHEMA_VERSION",
    "configure_silent_boot",
    "is_enabled",
    "log_filename",
    "register_flags",
    "register_shipped_invariants",
    "restore_legacy_terminal_logging",
    "terminal_level",
]
