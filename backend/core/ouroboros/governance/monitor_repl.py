"""Path D.2 — `/monitor` REPL operator surface for L3
ExecutionMonitor.

Closes the §36.6 "6 unwired autonomy modules" entry for
``execution_monitor`` (autonomy/) — the module already ships a
rich read API (`get_failure_rate`, `get_resource_violation_rate`,
`get_status_distribution`, `get_recent_outcomes`, `to_dict`) but
no singleton accessor. Slice ships:

  * :func:`autonomy.execution_monitor.get_default_monitor` —
    first-instance-wins singleton (added in this slice; SafetyNet
    composes it instead of allocating inline)
  * This REPL composes the singleton + canonical read API
  * Companion :mod:`monitor_observability` exposes
    ``GET /observability/execution-monitor`` (auto-mounted via
    §32.11 Slice 3)

Auto-discovered via §32.11 Slice 4 ``repl_dispatch_registry``
naming-cage convention (file ends ``_repl.py`` → verb
``/monitor`` → dispatcher ``dispatch_monitor_command(line)``).
Zero edits to the registry.

Subcommands:

  * ``/monitor``                — current snapshot (failure rate,
                                  resource violation rate, status
                                  distribution)
  * ``/monitor recent [N]``     — last N execution outcomes
  * ``/monitor stats``          — dict-shaped to_dict() projection
  * ``/monitor help``           — bypass-master help

Architectural locks (mirrors `/health`, `/why_changed`,
`/causal`, `/graph`):

  * **Read-only** — REPL composes ONLY read APIs (no
    ``record()`` calls — that's SafetyNet's job).
  * **Authority asymmetry** — substrate purity (no orchestrator
    / iron_gate / policy / providers / candidate_generator
    imports; AST-pinned).
  * **Composes singleton** — REPL composes
    :func:`get_default_monitor` only; no parallel
    ``ExecutionMonitor`` construction (AST-pinned).
  * **NEVER raises** — every dispatch path returns a structured
    result.

Identity preservation: cyan default, yellow elevated rates,
red critical, dim metadata. NO ``bright_green``.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class MonitorReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/monitor — L3 execution-monitor browser"
    f"{_RESET}\n"
    f"  {_DIM}Read-only operator surface for ExecutionMonitor "
    f"(SafetyNet's outcome ledger).{_RESET}\n"
    f"\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/monitor{_RESET}                "
    f"{_DIM}current snapshot{_RESET}\n"
    f"    {_CYAN}/monitor recent [N]{_RESET}     "
    f"{_DIM}last N outcomes (default 10){_RESET}\n"
    f"    {_CYAN}/monitor stats{_RESET}          "
    f"{_DIM}dict-shaped projection{_RESET}\n"
    f"    {_CYAN}/monitor help{_RESET}           "
    f"{_DIM}this message{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/monitor"
        or s == "monitor"
        or s.startswith("/monitor ")
        or s.startswith("monitor ")
    )


def _color_for_rate(rate: float) -> str:
    """Color heuristic — same band ladder shape as cost +
    circuit-breaker observers (closed-table dispatch)."""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return _DIM
    if r >= 0.50:
        return _RED
    if r >= 0.25:
        return _YELLOW
    if r > 0.0:
        return _CYAN
    return _DIM


def _color_for_status(status: str) -> str:
    s = (status or "").upper()
    if s in ("FAILED", "TIMEOUT", "MEMORY_EXCEEDED",
             "DEPTH_EXCEEDED", "ITERATION_EXCEEDED",
             "SECURITY_VIOLATION"):
        return _RED
    if s in ("RUNNING", "PENDING"):
        return _YELLOW
    if s == "COMPLETED":
        return _CYAN
    return _DIM


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


def _get_monitor():
    try:
        from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
            get_default_monitor,
        )
        return get_default_monitor()
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_snapshot() -> str:
    monitor = _get_monitor()
    if monitor is None:
        return (
            f"\n  {_RED}execution_monitor substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        snap = monitor.to_dict()
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}snapshot raised: {exc}{_RESET}\n"
    failure_rate = float(snap.get("failure_rate", 0.0))
    rv_rate = float(snap.get("resource_violation_rate", 0.0))
    total = int(snap.get("total_recorded", 0))
    distribution = snap.get("status_distribution", {}) or {}
    out = [
        f"\n  {_BOLD}{_CYAN}ExecutionMonitor snapshot{_RESET}  "
        f"{_DIM}(lifetime={total}){_RESET}",
        "",
        f"  {_DIM}failure_rate:{_RESET}            "
        f"{_color_for_rate(failure_rate)}"
        f"{failure_rate:.2%}{_RESET}",
        f"  {_DIM}resource_violation_rate:{_RESET} "
        f"{_color_for_rate(rv_rate)}{rv_rate:.2%}{_RESET}",
    ]
    if distribution:
        out.append("")
        out.append(f"  {_BOLD}Status distribution:{_RESET}")
        for status, count in sorted(
            distribution.items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            out.append(
                f"    {_color_for_status(status)}{status:<22}"
                f"{_RESET}{count}"
            )
    out.append("")
    return "\n".join(out) + "\n"


def _render_recent(limit: int = 10) -> str:
    monitor = _get_monitor()
    if monitor is None:
        return (
            f"\n  {_RED}execution_monitor substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        recent = monitor.get_recent_outcomes(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}snapshot raised: {exc}{_RESET}\n"
    out = [
        f"\n  {_BOLD}{_CYAN}Recent execution outcomes{_RESET}  "
        f"{_DIM}(showing up to {limit}){_RESET}",
        "",
    ]
    if not recent:
        out.append(
            f"  {_DIM}No outcomes recorded yet. SafetyNet "
            f"records outcomes as autonomy ops complete."
            f"{_RESET}"
        )
    else:
        for o in recent:
            try:
                status_value = getattr(
                    getattr(o, "status", None),
                    "name", "?",
                )
                op_id = getattr(o, "op_id", "?")[:24]
                duration_ms = getattr(o, "duration_ms", 0.0)
                out.append(
                    f"  {_DIM}{op_id:<24}{_RESET}  "
                    f"{_color_for_status(status_value)}"
                    f"{status_value:<22}{_RESET}  "
                    f"{_DIM}{duration_ms:>6.0f}ms{_RESET}"
                )
            except Exception:  # noqa: BLE001 — defensive
                out.append(
                    f"  {_DIM}(unrenderable outcome row){_RESET}"
                )
    out.append("")
    return "\n".join(out) + "\n"


def _render_stats() -> str:
    monitor = _get_monitor()
    if monitor is None:
        return (
            f"\n  {_RED}execution_monitor substrate "
            f"unavailable{_RESET}\n"
        )
    try:
        snap = monitor.to_dict()
    except Exception as exc:  # noqa: BLE001 — defensive
        return f"\n  {_RED}stats raised: {exc}{_RESET}\n"
    out = [
        f"\n  {_BOLD}{_CYAN}ExecutionMonitor.to_dict(){_RESET}",
        "",
    ]
    for k, v in snap.items():
        if isinstance(v, dict):
            out.append(f"  {_DIM}{k}:{_RESET}")
            for sk, sv in v.items():
                out.append(
                    f"    {_DIM}{sk:<24}{_RESET}{sv}"
                )
        else:
            out.append(f"  {_DIM}{k:<26}{_RESET}{v}")
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_monitor_command(
    line: str,
) -> MonitorReplDispatchResult:
    if not _matches(line):
        return MonitorReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return MonitorReplDispatchResult(
            ok=False, text=f"  /monitor parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")
    if head in ("help", "?"):
        return MonitorReplDispatchResult(ok=True, text=_HELP)
    try:
        if head == "":
            return MonitorReplDispatchResult(
                ok=True, text=_render_snapshot(),
            )
        if head == "stats":
            return MonitorReplDispatchResult(
                ok=True, text=_render_stats(),
            )
        if head == "recent":
            limit = 10
            if len(args) >= 2:
                try:
                    limit = max(1, min(200, int(args[1])))
                except (TypeError, ValueError):
                    limit = 10
            return MonitorReplDispatchResult(
                ok=True, text=_render_recent(limit),
            )
        return MonitorReplDispatchResult(
            ok=False,
            text=(
                f"  /monitor: unknown subcommand "
                f"{head!r} — try /monitor help"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return MonitorReplDispatchResult(
            ok=False,
            text=f"  /monitor: error — {exc}. Try again.",
        )


def register_verbs(registry) -> int:
    try:
        registry.register(
            verb="monitor",
            description=(
                "L3 execution-monitor browser — composes "
                "ExecutionMonitor (SafetyNet's outcome "
                "ledger). Read-only."
            ),
            posture_relevance="RELEVANT",
            since="Path D.2 (PRD §36.6, 2026-05-05)",
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``monitor_repl_authority_read_only`` — REPL never
         calls ``record()`` (SafetyNet's job).
      2. ``monitor_repl_authority_asymmetry`` — substrate purity.
      3. ``monitor_repl_composes_singleton`` — REPL composes
         ``get_default_monitor`` only; no parallel monitor
         construction.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/monitor_repl.py"
    )

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "record"
                    and isinstance(fn.value, ast.Name)
                ):
                    rcv = fn.value.id.lower()
                    # Only flag receivers that look like the
                    # monitor — `logger.record` etc are fine.
                    if (
                        "monitor" in rcv
                        or "execution" in rcv
                    ):
                        violations.append(
                            "monitor_repl.py is read-only; "
                            "MUST NOT call .record() — "
                            "SafetyNet writes to the monitor"
                        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"monitor_repl.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_singleton(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "ExecutionMonitor"
                ):
                    violations.append(
                        "monitor_repl.py MUST NOT construct "
                        "ExecutionMonitor directly — compose "
                        "get_default_monitor()"
                    )
        found_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "execution_monitor" in node.module
                ):
                    for alias in node.names:
                        if alias.name == "get_default_monitor":
                            found_import = True
        if not found_import:
            violations.append(
                "monitor_repl.py MUST compose "
                "execution_monitor.get_default_monitor"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="monitor_repl_authority_read_only",
            target_file=target,
            description=(
                "Path D.2 — REPL is read-only browser."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name="monitor_repl_authority_asymmetry",
            target_file=target,
            description=(
                "Path D.2 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="monitor_repl_composes_singleton",
            target_file=target,
            description=(
                "Path D.2 — single pipeline; composes "
                "get_default_monitor only."
            ),
            validate=_validate_composes_singleton,
        ),
    ]


__all__ = [
    "MonitorReplDispatchResult",
    "dispatch_monitor_command",
    "register_shipped_invariants",
    "register_verbs",
]
