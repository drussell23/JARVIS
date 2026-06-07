"""Slice 132 — The Sovereign Shadow Graduation Harness.

Replaces the manual "graduation runbook" with an executable, asynchronous harness
that runs LIVE, bounded integration assertions for a gated cost flag and, on pass,
**autonomously flips it FALSE→TRUE** — no human toggle, no terminal config edit.

THE LOAD-BEARING BOUND (recursion safety). An autonomous organism that could flip
its own kill-switches is no longer bounded — so it cannot. This harness:
  * auto-flips ONLY flags on an explicit **cost-candidate allowlist**
    (``_COST_CANDIDATES`` — the Slice-131 cost tiers, all ROUTING/TUNING class);
  * **fail-closed**: anything not on the allowlist (unknown OR SAFETY-class) is
    REFUSED → routed to the operator (advisory), never auto-granted;
  * composes — does not bypass — the existing Tiered Authority
    (``graduation_override_ledger``, which itself structurally refuses any
    non-STANDARD tier) for the audit receipt.

It also honors operator env-precedence (an explicit ``=0`` is never overridden)
and persists only via a **bounded ``.env`` writer that refuses credential-shaped
keys and never touches lines other than the target flag**.

Assertions are injectable so the harness is unit-tested without a funded Anthropic
key; the live run (synthetic prompt → 200 + tool_use; CAI cascade; etc.) is the
operator-invoked funded execution.
"""
from __future__ import annotations

import dataclasses
import enum
import inspect
import logging
import os
import pathlib
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_GRADUATION_ORCHESTRATOR_ENABLED"

# The ONLY flags this harness may autonomously flip — the Slice-131 cost tiers
# (all ROUTING/TUNING class, never SAFETY). Everything else is fail-closed.
_COST_CANDIDATES = frozenset({
    "JARVIS_SEMANTIC_CACHE_ENABLED",
    "JARVIS_CAI_ROUTER_ENABLED",
    "JARVIS_BATCH_ROUTING_ENABLED",
    "JARVIS_PROMPT_PREFIX_CACHE_ENABLED",
    "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED",
    "JARVIS_ECONOMIC_ROUTER_ENABLED",
})

# Substrings that mark a key as credential-shaped — NEVER persisted by this harness.
_CREDENTIAL_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "_KEY")

_OFF_VALUES = ("0", "false", "no", "off")


def graduation_orchestrator_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass(frozen=True)
class AssertionResult:
    flag: str
    passed: bool
    detail: str = ""


class GraduationAction(str, enum.Enum):
    GRADUATED = "graduated"                       # assertion passed → flag flipped
    REFUSED_DISABLED = "refused_disabled"         # master off
    REFUSED_SAFETY = "refused_safety"             # the recursion bound — operator only
    HELD_ASSERTION_FAILED = "held_assertion_failed"
    HELD_OPERATOR_PRECEDENCE = "held_operator_precedence"  # explicit =0 not overridden


@dataclasses.dataclass
class GraduationOutcome:
    flag: str
    action: GraduationAction
    flipped: bool = False
    detail: str = ""


def is_safety_flag(flag: str) -> bool:
    """Default safety classifier (fail-CLOSED). A flag is auto-flippable ONLY if it
    is on the explicit cost-candidate allowlist; everything else — unknown flags
    AND anything the FlagRegistry marks SAFETY — is treated as safety (refused).
    NEVER raises."""
    if flag in _COST_CANDIDATES:
        # Defense-in-depth: still reject if the registry explicitly says SAFETY.
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                Category, get_registry,
            )
            spec = get_registry().get(flag)
            if spec is not None and spec.category == Category.SAFETY:
                return True
        except Exception:  # noqa: BLE001 — registry optional; allowlist governs
            pass
        return False
    return True  # fail-closed: not a known cost substrate → operator territory


def persist_flag_to_env(
    flag: str, value: str, *, env_path: Optional[pathlib.Path] = None,
) -> bool:
    """Bounded ``.env`` writer. Updates ONLY the ``flag=value`` line (append if
    absent); every other line — especially credentials — is preserved byte-for-
    byte. REFUSES credential-shaped keys outright. Never logs the value. Returns
    True on write, False on refusal/error. NEVER raises."""
    up = flag.upper()
    if any(m in up for m in _CREDENTIAL_MARKERS):
        logger.warning("[GraduationOrchestrator] refusing to persist credential-shaped key")
        return False
    try:
        path = env_path or (pathlib.Path(".") / ".env")
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out: List[str] = []
        found = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key == flag:
                    out.append(f"{flag}={value}")
                    found = True
                    continue
            out.append(line)
        if not found:
            out.append(f"{flag}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationOrchestrator] .env persist skipped: %s", exc)
        return False


async def _default_assertion(flag: str) -> AssertionResult:
    """Best-effort live integration assertion per flag. Offline-checkable tiers
    (semantic cache, CAI router) run real composition checks here; the live-API
    tiers (prefix cache, batch) require a funded Anthropic lane and report that
    honestly (operator passes a live ``assertion``). NEVER raises."""
    try:
        if flag == "JARVIS_SEMANTIC_CACHE_ENABLED":
            from backend.core.ouroboros.governance import semantic_cache as SC
            os.environ["JARVIS_SEMANTIC_CACHE_ENABLED"] = "1"

            class _Emb:
                def embed(self, texts):
                    return [[1.0, 0.0, 0.0] for _ in texts]

            c = SC.SemanticResponseCache(embedder=_Emb())
            await c.store("q", object(), None, repo_digest="R")
            hit = await c.lookup("q", None, repo_digest="R")
            return AssertionResult(flag, hit is not None, "semantic write-through+near-match")
        if flag == "JARVIS_CAI_ROUTER_ENABLED":
            from backend.core.ouroboros.governance import cai_router as CR
            os.environ["JARVIS_CAI_ROUTER_ENABLED"] = "1"
            d = await CR.decide(
                "trivial doc tweak", type("C", (), {"task_complexity": "low"})(),
                classifier=lambda p, c: CR.CAIClassification("low", 0.95),
                sai_probe=lambda: CR.SituationalSignal("nominal"),
            )
            ok = d is not None and d.tier == "doubleword" and not d.escalated
            return AssertionResult(flag, ok, "CAI cascaded low-urgency → cheapest tier")
        return AssertionResult(
            flag, False,
            "live Anthropic lane required (operator must pass a funded assertion)",
        )
    except Exception as exc:  # noqa: BLE001
        return AssertionResult(flag, False, f"assertion error: {exc}")


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def graduate(
    flag: str,
    *,
    assertion: Optional[Callable[[str], Awaitable[AssertionResult]]] = None,
    is_safety: Optional[Callable[[str], bool]] = None,
    persist: bool = False,
    env_path: Optional[pathlib.Path] = None,
) -> GraduationOutcome:
    """Run the integration assertion for ``flag`` and, on pass, autonomously flip
    it — bounded by the safety classifier + operator precedence. NEVER raises."""
    if not graduation_orchestrator_enabled():
        return GraduationOutcome(flag, GraduationAction.REFUSED_DISABLED, False, "master off")

    safety = is_safety or is_safety_flag
    try:
        if safety(flag):
            logger.info("[GraduationOrchestrator] REFUSED_SAFETY %s — operator only", flag)
            return GraduationOutcome(
                flag, GraduationAction.REFUSED_SAFETY, False,
                "safety/unknown class — autonomous flip refused (recursion bound)",
            )
    except Exception:  # noqa: BLE001 — fail-closed: any classifier error → refuse
        return GraduationOutcome(flag, GraduationAction.REFUSED_SAFETY, False, "classifier error → refused")

    # Operator env-precedence: never override an explicit disable.
    cur = (os.environ.get(flag, "") or "").strip().lower()
    if cur in _OFF_VALUES:
        return GraduationOutcome(
            flag, GraduationAction.HELD_OPERATOR_PRECEDENCE, False,
            "operator explicitly disabled — honored",
        )

    try:
        res = await _await_maybe((assertion or _default_assertion)(flag))
    except Exception as exc:  # noqa: BLE001
        return GraduationOutcome(flag, GraduationAction.HELD_ASSERTION_FAILED, False, f"assertion raised: {exc}")
    if not isinstance(res, AssertionResult) or not res.passed:
        detail = res.detail if isinstance(res, AssertionResult) else "no result"
        return GraduationOutcome(flag, GraduationAction.HELD_ASSERTION_FAILED, False, detail)

    # Earned → autonomous flip (os.environ; optional bounded .env persist).
    os.environ[flag] = "1"
    if persist:
        persist_flag_to_env(flag, "1", env_path=env_path)
    _record_receipt(flag, res.detail)
    logger.info("[GraduationOrchestrator] GRADUATED %s (assertion: %s)", flag, res.detail)
    return GraduationOutcome(flag, GraduationAction.GRADUATED, True, res.detail)


def _record_receipt(flag: str, detail: str) -> None:
    """Best-effort STANDARD-tier audit receipt via the existing override ledger
    (which itself refuses any non-STANDARD tier). NEVER raises."""
    try:
        from backend.core.ouroboros.governance import graduation_override_ledger as OL
        rec = OL.OverrideRecord(flag_name=flag, tier="STANDARD", disposition="auto_flip",
                                detail=detail) if hasattr(OL, "OverrideRecord") else None
        if rec is not None and hasattr(OL, "record_graduation"):
            OL.record_graduation(rec)
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass


async def graduate_all(
    flags: List[str],
    *,
    assertion_for: Optional[Callable[[str], Callable[[str], Awaitable[AssertionResult]]]] = None,
    is_safety: Optional[Callable[[str], bool]] = None,
    persist: bool = False,
    env_path: Optional[pathlib.Path] = None,
) -> List[GraduationOutcome]:
    """Run the harness across many flags. ``assertion_for(flag)`` yields the
    per-flag assertion (defaults to the built-in). Sequential (each flip mutates
    os.environ). NEVER raises out."""
    outcomes: List[GraduationOutcome] = []
    for flag in flags:
        assertion = assertion_for(flag) if assertion_for else None
        outcomes.append(await graduate(
            flag, assertion=assertion, is_safety=is_safety,
            persist=persist, env_path=env_path,
        ))
    return outcomes


__all__ = [
    "graduation_orchestrator_enabled",
    "AssertionResult",
    "GraduationAction",
    "GraduationOutcome",
    "is_safety_flag",
    "persist_flag_to_env",
    "graduate",
    "graduate_all",
    "_COST_CANDIDATES",
]
