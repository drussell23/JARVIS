"""Attach Heartbeat — the live pulse for `ov` cockpits, in O+V's own voice.

``🐍··○ Synthesizing… (4m 9s · ↓ 15.9k tokens · DW-397B)``

The pulse glyph is the Ouroboros identity spinner (canonical in
ui/theme.py — the same animation the daemon's REPL toolbar breathes),
not a borrowed aesthetic.

Two pure halves, one schema (``heartbeat.v1``):

* **Daemon composer** — :func:`build_heartbeat_payload` samples the
  EXISTING canonical sources (zero new state, pure pull):
  ``StatusLineBuilder.snapshot()`` → phase / route / provider / active op;
  ``ThinkingProgressObserver.update()`` → verb phrase, elapsed, live
  streamed token counts (StreamRenderer singleton underneath), effort
  band; SerpentFlow's ``_prov()`` → the pretty provider label
  (DW-397B / Claude / J-Prime). The harness publishes the payload on a
  ~1s cadence through ``CockpitAttachBridge.publish_telemetry``.

* **Client formatter** — :func:`format_heartbeat_line` renders the
  toolbar text with a TIME-DRIVEN pulse glyph (any reader at any moment
  derives the same frame from the clock — zero animation tasks; the pt
  Application's ``refresh_interval`` repaint animates it for free) and
  a client-side elapsed advance (the seconds tick smoothly between 1 Hz
  frames). Stale payloads (organism paused/died) render nothing so the
  toolbar falls back to its idle text.

Zero authority; stdlib-only on the client path; NEVER raises anywhere.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

HEARTBEAT_SCHEMA_VERSION = "heartbeat.v1"


def _context_pct(op_id: str) -> float:
    """Fraction of the prompt budget in use for *op_id*, 0.0 if unknown.

    Zero means "no reading", not "empty" — the renderer treats it as absent
    rather than as 0%, because a confident 0% on an op that is actually full
    is worse than saying nothing.
    """
    try:
        from backend.core.ouroboros.governance.tool_executor import (
            context_utilisation,
        )
        return float(context_utilisation(op_id))
    except Exception:  # noqa: BLE001
        return 0.0


def _pending_apply_payload() -> Optional[dict]:
    """Open rejection windows, or None. NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.pending_apply import snapshot
        return snapshot()
    except Exception:  # noqa: BLE001
        return None


def _status_payload(snap: Any) -> Optional[dict]:
    """The status snapshot as a transport dict, or None. NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.status_line import (
            snapshot_to_payload,
        )
        return snapshot_to_payload(snap) or None
    except Exception:  # noqa: BLE001
        return None


def _context_reading(op_id: str, provider: str, snap: Any) -> Optional[dict]:
    """The context meter's payload for this op, or None.

    None when nothing has been measured — a zeroed reading and an unmeasured
    one look identical to a renderer, and only one of them should draw.
    """
    try:
        from backend.core.ouroboros.governance.context_meter import (
            as_payload, read,
        )
        model = str(getattr(snap, "model", "") or "")
        return as_payload(read(op_id, provider=provider, model=model))
    except Exception:  # noqa: BLE001
        return None


def _pulse_glyph(now: float) -> str:
    """The pulse is O+V's OWN identity animation — the Ouroboros spinner
    (snake closing on its tail, bite, reopen), consumed from its ONE
    canonical definition in ui/theme.py (the same frames the daemon's
    REPL spinner renders — every surface animates identically, derived
    purely from the clock). NEVER raises."""
    try:
        from backend.core.ouroboros.ui.theme import ouroboros_frame
        return ouroboros_frame(now)
    except Exception:  # noqa: BLE001
        return "🐍"

#: Phase → gerund fallback when the narrative channel has no verb for
#: the active op (post-GENERATE phases have no thinking stream). These
#: are presentation labels, same class as the tool-icon map.
_PHASE_VERBS = {
    "CLASSIFY": "Classifying",
    "ROUTE": "Routing",
    "CONTEXT_EXPANSION": "Contextualizing",
    "PLAN": "Planning",
    "GENERATE": "Synthesizing",
    "VALIDATE": "Validating",
    "GATE": "Gating",
    "APPROVE": "Awaiting approval",
    "APPLY": "Applying",
    "VERIFY": "Verifying",
    "REPAIR": "Repairing",
}


def flavour_rotate_s() -> float:
    """How long a flavour verb holds before the next one.

    Slow enough to read, fast enough to feel alive. CC changes its word on a
    similar cadence for the same reason: the glyph cannot carry motion on its
    own once it is an emoji.
    """
    try:
        return max(1.0, min(60.0, float(os.environ.get(
            "JARVIS_ATTACH_VERB_ROTATE_S", "") or 4.0)))
    except (TypeError, ValueError):
        return 4.0


def _flavour_verb(op_id: str, now: Optional[float] = None) -> str:
    """A verb for work with no phase to name — O+V's own register.

    WHY NOT A SECOND WORD LIST
    ---------------------------
    `turn_spinner.turn_verbs()` already is one: `Coiling`, `Digesting`,
    `Unwinding`, `Sensing`, `Reckoning` — the organism's voice, extendable by
    the operator through `JARVIS_TURN_VERBS` without touching a renderer.
    Writing a second table here would be two vocabularies drifting apart, and
    the operator's extras would only reach one of them.

    PHASE TRUTH OUTRANKS FLAVOUR
    -----------------------------
    This is the FALLBACK. When the pipeline knows it is in GENERATE, the line
    says `Synthesizing` because that is what is happening — CC picks a whimsical
    gerund because it has nothing more specific to say, and trading a true
    label for a charming one would be a downgrade. Flavour fills the silence;
    it does not replace the signal.

    Stable within a rotation window and seeded by the op, so two ops working
    at once do not chant the same word and one op does not flicker between
    words on consecutive repaints.
    """
    try:
        import time as _time
        from backend.core.ouroboros.battle_test.turn_spinner import turn_verbs
        verbs = turn_verbs()
        if not verbs:
            return ""
        t = _time.monotonic() if now is None else float(now)
        bucket = int(t / flavour_rotate_s())
        seed = sum(ord(c) for c in str(op_id or "")) + bucket
        return verbs[seed % len(verbs)]
    except Exception:  # noqa: BLE001
        return ""


def heartbeat_interval_s() -> float:
    """Publisher cadence; ``0`` disables. Re-read at call time."""
    try:
        return max(0.0, min(30.0, float(os.environ.get(
            "JARVIS_ATTACH_HEARTBEAT_S", "1.0"))))
    except (TypeError, ValueError):
        return 1.0


def heartbeat_stale_after_s() -> float:
    """How long a frame stays believable — THE definition of "lost contact".

    Public because more than the pulse depends on it. Anything rendered from
    a heartbeat frame — the pulse, the agent roster — must retire on the same
    window, or the cockpit ends up showing a dead daemon's agents as running
    underneath an idle pulse, and each surface would be individually correct.
    """
    try:
        return max(2.0, float(os.environ.get(
            "JARVIS_ATTACH_HEARTBEAT_STALE_S", "10")))
    except (TypeError, ValueError):
        return 10.0


def _stale_after_s() -> float:
    return heartbeat_stale_after_s()


# ---------------------------------------------------------------------------
# Daemon side — composer (pure pull over existing organs)
# ---------------------------------------------------------------------------


def build_heartbeat_payload() -> Optional[Dict[str, Any]]:
    """Compose ONE heartbeat frame from the daemon's canonical status
    sources. Returns None when there is nothing to say (no status
    builder registered). NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.status_line import (
            get_status_line_builder,
        )
        b = get_status_line_builder()
        if b is None:
            return None
        snap = b.snapshot()
        phase = (snap.phase or "").strip().upper()
        op_id = snap.primary_op_id or ""

        verb, elapsed_s, tokens_total, effort = "", 0.0, 0, ""
        thinking_active = False
        if op_id:
            try:
                from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
                    get_default_observer,
                )
                tsnap, _ = get_default_observer().update(op_id=op_id)
                if tsnap is not None:
                    verb = tsnap.verb_phrase or ""
                    elapsed_s = float(tsnap.elapsed_s or 0.0)
                    tokens_total = int(tsnap.tokens_total)
                    effort = tsnap.effort_band.value
                    thinking_active = bool(tsnap.is_active)
            except Exception:  # noqa: BLE001
                pass

        phase_verb = _PHASE_VERBS.get(phase, "") or _flavour_verb(op_id)
        active = bool(thinking_active or phase_verb)
        if not verb or not thinking_active:
            verb = phase_verb or verb
        if elapsed_s <= 0.0:
            # Post-GENERATE phases: the builder's detail is "NNs".
            detail = (snap.phase_detail or "").strip()
            if detail.endswith("s") and detail[:-1].isdigit():
                elapsed_s = float(detail[:-1])

        provider = (snap.provider or "").strip()
        provider_label = provider
        if provider:
            try:
                from backend.core.ouroboros.battle_test.serpent_flow import (
                    _prov,
                )
                provider_label = _prov(provider) or provider
            except Exception:  # noqa: BLE001
                provider_label = provider

        # Selectable lanes ride the heartbeat rather than opening a second
        # stream: it already flows at ~1Hz, which is the cadence a deck list
        # needs, and a dedicated lane channel would be a parallel lifecycle
        # to keep in sync for no additional freshness.
        try:
            from backend.core.ouroboros.battle_test.lane_rings import (
                get_lane_registry,
            )
            lanes = get_lane_registry().summary()[:12]
        except Exception:  # noqa: BLE001 — a laneless heartbeat still beats
            lanes = []

        # The agent roster rides here for the same reason lanes do, and for
        # one more: the roster is a module SINGLETON in the daemon, and the
        # cockpit that must draw it is a different process under `ov attach`.
        # A client rendering its own `get_agent_roster()` would draw an empty
        # roster forever — indistinguishable from a system that never
        # dispatches agents. Serialising it here is what makes the agent view
        # true remotely rather than only in-process.
        try:
            from backend.core.ouroboros.battle_test.agent_roster import (
                get_agent_roster, roster_wire_rows,
            )
            # The WIRE window, not the display window. This daemon cannot see
            # its readers' terminals, and two of them may differ by forty
            # rows — serialising only what the smallest could draw would
            # truncate the roomy one by a peer's screen size.
            agents = get_agent_roster().snapshot(max_rows=roster_wire_rows())
        except Exception:  # noqa: BLE001 — a rosterless heartbeat still beats
            agents = None

        return {
            "kind": "heartbeat",
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "lanes": lanes,
            # Additive under heartbeat.v1: an older client that has never
            # heard of `agents` ignores the key, and a newer client that does
            # not receive one renders no roster. Neither is a version error.
            "agents": agents,
            "active": active,
            "verb": verb,
            "phase": phase,
            "elapsed_s": round(elapsed_s, 1),
            "tokens_total": tokens_total,
            # How full the op's context is. Read from the tool loop's OWN
            # gauge — the same number it compares against the compaction
            # threshold — so the status line and the compactor cannot
            # disagree about whether earlier rounds are about to be
            # summarised away.
            "context_pct": _context_pct(op_id),
            # The richer reading: tokens against the PROVIDER's window as
            # well as chars against this process's compaction threshold,
            # with the binding wall named. `context_pct` stays for older
            # clients — additive, never a replacement.
            "context": _context_reading(op_id, provider, snap),
            # The FULL status snapshot. `snap` is already in hand — this
            # composer sampled it at the top for phase/route/provider — so
            # the cockpit's status line costs a serialisation, not a second
            # pull. Without it the attach client would render its own
            # builder, which is empty by construction and would draw an
            # eternally idle organism.
            "status": _status_payload(snap),
            # An apply waiting out its rejection window. Carried here rather
            # than mirrored as text because a countdown is STATE — the panel
            # that draws it locally repaints eight times a second, and the
            # bridge is a line stream.
            "pending_apply": _pending_apply_payload(),
            "effort": effort,
            "provider": provider,
            "provider_label": provider_label,
            "route": snap.route or "",
            "op_id": op_id,
        }
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Client side — formatter (pure; time-driven pulse; NEVER raises)
# ---------------------------------------------------------------------------


def _fmt_elapsed(s: float) -> str:
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _fmt_tokens(n: int) -> str:
    n = max(0, int(n))
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_heartbeat_line(
    payload: Optional[Dict[str, Any]],
    *,
    now_mono: Optional[float] = None,
    arrival_mono: Optional[float] = None,
) -> str:
    """Render the CC-style pulse line from the latest heartbeat frame.
    Empty string when inactive/stale/absent — the caller's toolbar falls
    back to its idle text. Elapsed advances client-side between frames;
    the pulse glyph derives from the clock. NEVER raises."""
    try:
        if not payload or not payload.get("active"):
            return ""
        now = time.monotonic() if now_mono is None else float(now_mono)
        arrived = now if arrival_mono is None else float(arrival_mono)
        age = max(0.0, now - arrived)
        if age > _stale_after_s():
            return ""
        glyph = _pulse_glyph(now)
        verb = str(payload.get("verb") or "Working")
        elapsed = float(payload.get("elapsed_s") or 0.0) + age
        parts = [_fmt_elapsed(elapsed)]
        tokens = int(payload.get("tokens_total") or 0)
        if tokens > 0:
            parts.append(f"↓ {_fmt_tokens(tokens)} tokens")
        # Context headroom, shown only once it MATTERS.
        #
        # Below the threshold this is noise — every op starts near empty and
        # a permanent 3% teaches the operator to ignore the field, which is
        # exactly how it fails to register on the day it says 91%.
        #
        # The METER's line is preferred when present because it names the
        # WALL: "left until compact" and "left of window" call for different
        # actions, and the bare fraction could only ever describe the first.
        ctx_line = ""
        try:
            from backend.core.ouroboros.governance.context_meter import (
                render_payload,
            )
            ctx_line = render_payload(payload.get("context"))
        except Exception:  # noqa: BLE001
            ctx_line = ""
        if ctx_line:
            parts.append(ctx_line)
        else:
            ctx = float(payload.get("context_pct") or 0.0)
            if ctx > 0.0:
                try:
                    from backend.core.ouroboros.governance.tool_executor import (
                        compaction_threshold_fraction,
                    )
                    floor = compaction_threshold_fraction()
                except Exception:  # noqa: BLE001
                    floor = 0.75
                if ctx >= floor:
                    parts.append(f"ctx {int(ctx * 100)}% · compacting")
                elif ctx >= floor * 0.8:
                    parts.append(f"ctx {int(ctx * 100)}%")
        label = str(
            payload.get("provider_label") or payload.get("provider") or ""
        )
        if label:
            parts.append(label)
        return f" {glyph} {verb}… ({' · '.join(parts)})"
    except Exception:  # noqa: BLE001
        return ""
