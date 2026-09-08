"""Capability assurance — no op runs weaker than the envelope promised.

Every capability in this organism degrades QUIETLY. `single_file_diff_schema_
enabled()` returns False and the model is asked for a whole file instead of a
diff; `ctx.telemetry` is None and `_ctx_schema_capability` answers
`full_content_only`; `ctx.target_symbols` is empty and the declared-symbol
refusal has nothing to refuse. In every one of those cases the pipeline still
runs, still produces a candidate, and still reports success — it just produces
the WEAKER thing, and the only evidence is a mangled docstring in a landed
diff, days later.

That is the failure this module exists to make impossible. The envelope
(`production_envelope`) states what the organism can do; this asserts the
running op actually has it, and REFUSES rather than silently delivering less.

## Two layers, because the questions are different

**Preflight** (`preflight_verdict`) runs before an op is dispatched — at
`/goal sanction` and `/goal inject`. There is no `OperationContext` yet, so it
asks what can be known statically: are the capability flags armed, does the
signed goal declare symbols, is the scope single-file. A failure here means
"do not dispatch", and the operator sees why in the TUI before anything runs.

**Runtime** (`assert_generation_capability`) runs at the seam where the schema
decision is actually made, with the real context in hand. It answers the one
question preflight cannot: *given this op's served model and telemetry, is the
organism about to ask for the schema it promised?* Full content is legitimate
for a multi-file op, or a model that cannot produce diffs — those are not
degradation. Degradation is: the flag is on, the served model IS diff-capable,
the scope IS single-file, and the decision still came out `full_content`. That
combination means something upstream lost the capability, and it is the exact
shape of `capability=? brain=-` (`ctx.telemetry is None`, 89a9166e05 /
ec6bb92c9a) — a routing admission that never happened.

Both return a verdict; neither raises. The CALLER decides what a failure means,
because "refuse to dispatch" and "abort a running op" are different acts with
different blast radii, and a module that made that choice for both would be
wrong for one of them.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CapabilityAssurance")

__all__ = [
    "CapabilityVerdict",
    "assert_generation_capability",
    "enforcement_enabled",
    "preflight_verdict",
]

_ENV_ENFORCE = "JARVIS_CAPABILITY_ASSURANCE_ENFORCE"


def enforcement_enabled() -> bool:
    """Whether a failed assurance ABORTS, or is merely reported.

    Default ON. The whole point is that degradation must not pass silently;
    an operator who wants to run degraded on purpose says so explicitly.
    NEVER raises.
    """
    raw = (os.environ.get(_ENV_ENFORCE, "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class CapabilityVerdict:
    """What the organism can do right now, and what it was promised."""

    ok: bool
    reason: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: Dict[str, str] = field(default_factory=dict)
    #: Whether a failure here may ABORT the op, as opposed to being reported.
    #:
    #: The envelope promises capability for the operator's SANCTIONED work. It
    #: says nothing about the many legitimate contexts that build a prompt with
    #: no routing admission — an ambient tool call, a probe, a unit test. The
    #: first version of this module aborted on all of them, which turned a
    #: diagnostic into a hard failure on paths that were previously fine: seven
    #: prompt-building tests began raising ``capability_degraded`` the moment
    #: another test left the diff flag armed in the environment.
    #:
    #: An op is enforceable when it carries a signed-goal pointer — i.e. it is
    #: exactly the work the operator sanctioned and for which the capability
    #: was actually promised.
    enforceable: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok

    def render(self) -> str:
        """One operator-facing line per check — the TUI's telemetry error."""
        marks = ", ".join(
            f"{'ok' if v else 'FAIL'}:{k}" for k, v in sorted(self.checks.items())
        )
        head = "capability assurance PASSED" if self.ok else (
            f"capability assurance FAILED — {self.reason}"
        )
        return f"{head} [{marks}]" if marks else head


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Preflight — before an op exists
# ---------------------------------------------------------------------------


def preflight_verdict(
    *,
    target_files: Sequence[str] = (),
    target_symbols: Sequence[str] = (),
    require_symbols: bool = True,
) -> CapabilityVerdict:
    """Can a goal with this shape run at full capability? NEVER raises.

    Checked before dispatch, so a degraded run is refused at the keystroke
    rather than discovered in the diff.
    """
    try:
        files = tuple(str(f) for f in (target_files or ()) if str(f).strip())
        symbols = tuple(str(s) for s in (target_symbols or ()) if str(s).strip())

        checks: Dict[str, bool] = {}
        detail: Dict[str, str] = {}

        # 1. The output schema. FALSE means every candidate is a whole-file
        #    re-emission — the fidelity ceiling, and where the mangle lives.
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            single_file_diff_schema_enabled,
        )
        checks["diff_schema"] = bool(single_file_diff_schema_enabled())
        detail["diff_schema"] = "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED"

        # 2. The L3 fan-out. Off, the parent-inheritance path engages only
        #    when an authoritative multi-node PLAN DAG drives it.
        checks["fanout"] = _flag("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED")
        detail["fanout"] = "JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED"

        # 3. The AST-signature anchor: a goal that declares no symbol cannot
        #    have a hallucinated no-op refused, because there is nothing the
        #    candidate is required to have CHANGED.
        checks["declared_symbols"] = bool(symbols) or not require_symbols
        detail["declared_symbols"] = (
            f"{len(symbols)} declared" if symbols else "NONE declared"
        )

        # 4. Single-file scope is a PRECONDITION of the diff schema
        #    (single_file_diff_requested refuses len(target_files) != 1).
        checks["single_file_scope"] = len(files) == 1
        detail["single_file_scope"] = f"{len(files)} target file(s)"

        # 5. The air-gap is a capability too — an op that can reach origin
        #    from an interactive session is not the op the operator asked for.
        from backend.core.ouroboros.governance.remote_push_guard import (  # noqa: PLC0415
            airgap_engaged,
        )
        checks["airgap"] = bool(airgap_engaged())
        detail["airgap"] = "JARVIS_REMOTE_PUSH_AIRGAP"

        failed = sorted(k for k, v in checks.items() if not v)
        if failed:
            return CapabilityVerdict(
                False,
                "degraded: " + ", ".join(
                    f"{k} ({detail.get(k, '')})" for k in failed
                ),
                checks, detail,
            )
        return CapabilityVerdict(True, "full capability", checks, detail)
    except Exception as exc:  # noqa: BLE001
        # An assurance that cannot run is itself a failure — fail CLOSED.
        logger.warning("[CapabilityAssurance] preflight degraded: %r", exc)
        return CapabilityVerdict(
            False, f"assurance_unavailable:{type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# Runtime — with the real context, at the schema decision
# ---------------------------------------------------------------------------


def assert_generation_capability(
    ctx: Any, *, force_full_content: bool,
) -> CapabilityVerdict:
    """Is this op about to be asked for the schema it was promised?

    Returns OK for every case where full content is the CORRECT answer —
    multi-file scope, a model that cannot produce diffs, the flag deliberately
    off. It fails only on genuine degradation: the flag on, the served model
    diff-capable, the scope single-file, and the decision still full_content.

    NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.providers import (  # noqa: PLC0415
            _ctx_schema_capability,
            single_file_diff_requested,
            single_file_diff_schema_enabled,
        )

        # Only a SANCTIONED op may be aborted — see CapabilityVerdict.enforceable.
        sanctioned = False
        try:
            from backend.core.ouroboros.governance.autonomy.parent_inheritance import (  # noqa: E501,PLC0415
                goal_pointer_for,
            )
            sanctioned = bool(goal_pointer_for(ctx))
        except Exception:  # noqa: BLE001
            sanctioned = False

        files = tuple(getattr(ctx, "target_files", ()) or ())
        capability = str(_ctx_schema_capability(ctx) or "")
        served = ""
        try:
            ri = getattr(getattr(ctx, "telemetry", None), "routing_intent", None)
            served = str(getattr(ri, "served_model", "") or "")
        except Exception:  # noqa: BLE001
            served = ""

        checks = {
            "diff_schema_flag": bool(single_file_diff_schema_enabled()),
            "telemetry_present": getattr(ctx, "telemetry", None) is not None,
            "single_file_scope": len(files) == 1,
        }
        detail = {
            "capability": capability or "?",
            "served_model": served or "-",
            "target_files": str(len(files)),
        }

        # Not applicable: these are correct outcomes, not degradation.
        if len(files) != 1:
            return CapabilityVerdict(
                True, "multi-file scope — full content is correct",
                checks, detail, enforceable=sanctioned,
            )
        if not checks["diff_schema_flag"]:
            return CapabilityVerdict(
                True, "diff schema deliberately off",
                checks, detail, enforceable=sanctioned,
            )
        if capability != "full_content_and_diff":
            # The served model genuinely cannot produce diffs -> correct.
            # But a MISSING telemetry is not the model saying no; it is nobody
            # having asked, and that IS the degradation — for a SANCTIONED op.
            # For anything else (an ambient tool call, a probe, a unit test)
            # a bare context is ordinary and must not be treated as a fault.
            if not checks["telemetry_present"]:
                return CapabilityVerdict(
                    False,
                    "routing admission missing: ctx.telemetry is None, so the "
                    "served model's schema capability was never resolved and "
                    "the 2b.1-diff schema is structurally unreachable "
                    "(capability=? brain=-)",
                    checks, detail, enforceable=sanctioned,
                )
            return CapabilityVerdict(
                True, f"served model is {capability} — full content is correct",
                checks, detail, enforceable=sanctioned,
            )

        if single_file_diff_requested(ctx, force_full_content=force_full_content):
            return CapabilityVerdict(
                True, "2b.1-diff in play", checks, detail, enforceable=sanctioned,
            )

        return CapabilityVerdict(
            False,
            "silent capability degradation: the diff schema is armed and "
            f"{served or 'the served model'} is diff-capable on a single-file "
            "op, but the schema decision came out full_content",
            checks, detail, enforceable=sanctioned,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CapabilityAssurance] runtime check degraded: %r", exc)
        return CapabilityVerdict(
            False, f"assurance_unavailable:{type(exc).__name__}",
        )
