"""Rows for a CONFIRM prompt, so the operator is not guessing.

`intent_journal` halts on an EFFECTFUL step whose outcome is UNKNOWN and asks.
Asking without evidence is only marginally better than replaying blind: "did the
click land before it died?" is unanswerable by looking at the screen afterwards,
because whatever ran next has already overwritten it.

This renders the black box the executor kept: what the machine looked like going
in, what it looked like coming out (or that it never got to look), and what the
step was trying to do.

SAME CONTRACT AS THE OTHER STRIPS
-----------------------------------
``render(payload, *, width) -> List[str]``, mirroring
`pending_apply.render` — a pure function from a payload dict to lines, with no
prompt_toolkit import and no knowledge of where it is mounted. That is what lets
one renderer serve the daemon's own console and a remote `ov attach` client
without the two drifting, and it is why `build_dynamic_rows` can host it: the
strip is exactly as tall as what it currently holds, and zero rows cost zero
lines.

THE LINE THIS MUST NOT CROSS
------------------------------
It renders `changed: None` as "could not determine", never as "no change".
A three-valued fact flattened to a boolean at the render layer would undo the
care taken to preserve it — the operator would read a confident "nothing
happened" that the system never claimed. Same rule as marks being an EXCEPTION
surface in `ui/provenance`: the uncertain case is the one that must be loud.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ForensicDelta")

FORENSIC_DELTA_SCHEMA_VERSION: str = "forensic_delta.v1"

_UNKNOWN = "?"


def _clip(text: Any, limit: int) -> str:
    s = str(text if text is not None else "")
    return s if len(s) <= limit else s[: max(1, limit - 1)] + "…"


def _snapshot_line(label: str, snap: Optional[Dict[str, Any]],
                   width: int) -> str:
    """One side of the delta.

    An absent snapshot is rendered explicitly rather than skipped: for the
    POST side, absence IS the finding — the step died before anything could be
    observed — and a missing row would read as an unremarkable gap.
    """
    if not snap:
        return f"  {label:<7} (never captured — the step did not return)"
    avail = str(snap.get("availability", "observed"))
    if avail != "observed":
        detail = _clip(snap.get("detail", ""), 48)
        return f"  {label:<7} unreadable ({avail}{': ' + detail if detail else ''})"
    app = _clip(snap.get("app") or _UNKNOWN, 28)
    title = snap.get("window_title")
    if title is None:
        # No screen-recording grant, or no titled window. NOT an empty title.
        return f"  {label:<7} {app}  · window title unavailable"
    return f"  {label:<7} {app}  · {_clip(title, max(12, width - len(app) - 22))}"


def render(payload: Optional[Dict[str, Any]], *,
           width: Optional[int] = None) -> List[str]:
    """Rows for the forensic strip, or [] when there is nothing to show.

    NEVER raises — a renderer that can crash the cockpit while reporting a
    crash is not a diagnostic.
    """
    if not payload:
        return []
    w = max(40, int(width or 100))
    try:
        rows: List[str] = []
        idx = payload.get("step_index")
        action = _clip(payload.get("action") or _UNKNOWN, 16)
        target = _clip(payload.get("target") or "", 40)
        step_label = f"step {idx}" if idx is not None else "step"

        rows.append(f"⚠ {step_label} did not report — outcome UNKNOWN")
        detail = f"  action  {action}"
        if target:
            detail += f" → {target}"
        val = _clip(payload.get("value") or "", 40)
        if val:
            detail += f"  value {val!r}"
        rows.append(detail)

        err = _clip(payload.get("error") or "", w - 12)
        if err:
            rows.append(f"  error   {err}")

        rows.append(_snapshot_line("before", payload.get("before"), w))
        rows.append(_snapshot_line("after", payload.get("after"), w))

        changed = payload.get("changed")
        summary = _clip(payload.get("summary") or "", w - 12)
        if changed is None:
            # THE line that must not become "no change".
            rows.append(f"  verdict could not determine whether it applied"
                        f"{' — ' + summary if summary else ''}")
        elif changed:
            rows.append(f"  verdict the machine CHANGED — {summary}")
        else:
            rows.append(f"  verdict no observable change — {summary}")
        # Key hint OUTSIDE the word: `[c]onfirm` splits the word with a
        # bracket, so neither an operator scanning for "confirm" nor a
        # test asserting on it finds anything.
        rows.append("  [c] confirm it completed   ·   [a] abort and re-run")
        return rows
    except Exception:  # noqa: BLE001
        logger.debug("[ForensicDelta] render degraded", exc_info=True)
        return ["⚠ forensic delta unavailable (render failed)"]


def rows_for(payloads: Optional[List[Dict[str, Any]]], *,
             width: Optional[int] = None, limit: int = 3) -> List[str]:
    """Strip provider: the pending confirmations, newest first. NEVER raises.

    Shaped for `build_dynamic_rows`, which takes a callable returning the
    CURRENT lines — so the source (a local singleton, a heartbeat snapshot)
    stays the caller's concern and this never learns which surface it is on.
    """
    try:
        if not payloads:
            return []
        out: List[str] = []
        for p in list(payloads)[-max(1, limit):]:
            out.extend(render(p, width=width))
        return out
    except Exception:  # noqa: BLE001
        return []
