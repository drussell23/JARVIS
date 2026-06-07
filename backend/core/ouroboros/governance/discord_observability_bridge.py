"""Slice 142 — The Discord Observability Bridge.

Turns Discord into a live window into the organism. It is an in-process consumer
of the existing ``StreamEventBroker`` (``ide_observability_stream``, ~57 event
types fed by ~40 producers) — it builds NO new event capture. Each event is routed
to a per-channel Discord webhook (#ops / #subagents / #cost-safety / #commits /
#heartbeat), **batched + throttled** so a busy organism never trips Discord's
webhook rate limit (~5/sec, 30/min per hook).

Gated ``JARVIS_DISCORD_BRIDGE_ENABLED`` default-FALSE. Webhook URLs come from env
(``JARVIS_DISCORD_WEBHOOK_<CHANNEL>``), never hardcoded/committed. Fully async +
fail-soft: a dead webhook, a full queue, or a malformed event NEVER perturbs the
soak. The poster + event source are injectable for hermetic tests.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_DISCORD_BRIDGE_ENABLED"
_ENV_FLUSH = "JARVIS_DISCORD_FLUSH_INTERVAL_S"
_ENV_MIN_POST = "JARVIS_DISCORD_MIN_POST_INTERVAL_S"

_DEFAULT_FLUSH = 3.0          # aggregate a 3s window per channel
_DEFAULT_MIN_POST = 2.5       # ≥2.5s between posts to one webhook (well under 30/min)
_DISCORD_MAX_CHARS = 2000     # hard message cap
_MAX_BATCH_LINES = 25         # cap events rendered per message

# event_type → channel key (channel key → env JARVIS_DISCORD_WEBHOOK_<KEY>).
# Grounded in the real ide_observability_stream EVENT_TYPE_* vocabulary. Anything
# not mapped is intentionally dropped (UI/noise events stay out of Discord).
_CHANNEL_MAP: Dict[str, str] = {
    # ── #ops — the working heartbeat ──
    "task_created": "ops", "task_started": "ops", "task_updated": "ops",
    "task_completed": "ops", "task_cancelled": "ops", "plan_generated": "ops",
    "plan_pending": "ops", "plan_approved": "ops", "plan_rejected": "ops",
    "phase_flow_updated": "ops",
    # ── #subagents ──
    "execution_graph_progress": "subagents", "multi_prior_dispatch": "subagents",
    # ── #cost-safety ──
    "budget_action_taken": "cost_safety", "cost_band_crossed": "cost_safety",
    "session_exhausted": "cost_safety", "governor_throttle_applied": "cost_safety",
    "governor_emergency_brake": "cost_safety", "circuit_breaker_tripped": "cost_safety",
    "circuit_breaker_state_change": "cost_safety", "circuit_breaker_approaching": "cost_safety",
    "provider_failure_classified": "cost_safety", "intervention_banner_raised": "cost_safety",
    # ── #commits ──
    "commit_authority_decision_recorded": "commits",
    # ── #heartbeat — the organism pulse ──
    "posture_changed": "heartbeat", "flag_graduated": "heartbeat",
    "belief_revision_recorded": "heartbeat", "postmortem_fused": "heartbeat",
    "second_order_doll_progress_updated": "heartbeat",
}


def discord_bridge_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def channel_for(event_type: str) -> Optional[str]:
    """Map an event type to a Discord channel key, or None to drop it."""
    return _CHANNEL_MAP.get(str(event_type or ""))


def webhook_url_for(channel_key: str) -> Optional[str]:
    """Resolve a channel key → its webhook URL from env (never hardcoded)."""
    url = os.getenv(f"JARVIS_DISCORD_WEBHOOK_{str(channel_key or '').upper()}", "")
    return url.strip() or None


@dataclasses.dataclass
class BridgeEvent:
    """A minimal projection of a StreamEvent (decouples the bridge from the broker
    type for testing). ``from_stream_event`` adapts the real broker event."""

    event_type: str
    op_id: str = ""
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_stream_event(cls, ev: Any) -> "BridgeEvent":
        return cls(
            event_type=str(getattr(ev, "event_type", "") or ""),
            op_id=str(getattr(ev, "op_id", "") or ""),
            payload=dict(getattr(ev, "payload", {}) or {}),
        )


_EMOJI = {
    "ops": "⚙️", "subagents": "🕸️", "cost_safety": "💰",
    "commits": "📝", "heartbeat": "💓",
}


def format_events(channel_key: str, events: List[BridgeEvent]) -> str:
    """Render a batch into one compact Discord message (≤2000 chars). NEVER raises."""
    try:
        head = f"{_EMOJI.get(channel_key, '•')} **{channel_key}** · {len(events)} event(s)"
        lines = [head]
        for ev in events[:_MAX_BATCH_LINES]:
            op = (ev.op_id or "")[:12]
            detail = ""
            for k in ("reason", "status", "tier", "model", "route", "summary", "verdict"):
                v = ev.payload.get(k) if isinstance(ev.payload, Mapping) else None
                if v:
                    detail = f" — {str(v)[:80]}"
                    break
            lines.append(f"• `{ev.event_type}`" + (f" op={op}" if op else "") + detail)
        if len(events) > _MAX_BATCH_LINES:
            lines.append(f"…+{len(events) - _MAX_BATCH_LINES} more")
        msg = "\n".join(lines)
        if len(msg) > _DISCORD_MAX_CHARS:
            msg = msg[: _DISCORD_MAX_CHARS - 1] + "…"
        return msg
    except Exception:  # noqa: BLE001
        return f"**{channel_key}** · {len(events)} event(s)"


def _flush_interval_s() -> float:
    try:
        return max(0.0, float(os.getenv(_ENV_FLUSH, _DEFAULT_FLUSH)))
    except (TypeError, ValueError):
        return _DEFAULT_FLUSH


_Poster = Callable[[str, str], Awaitable[int]]


class DiscordBridge:
    """Buckets events per channel and flushes them as throttled webhook posts."""

    def __init__(self, *, min_post_interval_s: Optional[float] = None,
                 flush_interval_s: Optional[float] = None) -> None:
        self._buckets: Dict[str, List[BridgeEvent]] = {}
        self._last_post: Dict[str, float] = {}
        self._min_post = (min_post_interval_s if min_post_interval_s is not None
                          else _env_float(_ENV_MIN_POST, _DEFAULT_MIN_POST))
        self._flush_interval = (flush_interval_s if flush_interval_s is not None
                                else _flush_interval_s())

    def ingest(self, ev: BridgeEvent) -> None:
        """Route + bucket an event (no I/O). Unknown types are dropped. NEVER raises."""
        try:
            ch = channel_for(ev.event_type)
            if ch is None:
                return
            self._buckets.setdefault(ch, []).append(ev)
        except Exception:  # noqa: BLE001
            pass

    async def flush(self, *, poster: Optional[_Poster] = None) -> int:
        """Post each non-empty, configured, non-throttled channel bucket once.
        Returns the number of channels posted. Fail-soft per channel. NEVER raises."""
        posted = 0
        now = time.monotonic()
        for ch, evs in list(self._buckets.items()):
            if not evs:
                continue
            url = webhook_url_for(ch)
            if not url:
                self._buckets[ch] = []  # no webhook → drop (don't grow unbounded)
                continue
            last = self._last_post.get(ch)
            if last is not None and (now - last) < self._min_post:
                continue  # throttled — keep buffering until the interval elapses
            content = format_events(ch, evs)
            try:
                send = poster or _default_poster
                rc = await send(url, content)
                if 200 <= int(rc) < 300:
                    posted += 1
                    self._buckets[ch] = []
                    self._last_post[ch] = now
                else:
                    logger.warning("[DiscordBridge] %s post rc=%s — dropping batch", ch, rc)
                    self._buckets[ch] = []
                    self._last_post[ch] = now
            except Exception as exc:  # noqa: BLE001 — a dead webhook never crashes the soak
                logger.warning("[DiscordBridge] %s dispatch swallowed: %s", ch, exc)
                self._buckets[ch] = []
                self._last_post[ch] = now
        return posted

    async def run(self, *, source: Any, poster: Optional[_Poster] = None,
                  stop: Any = None) -> None:
        """Drain ``source`` (an asyncio.Queue of BridgeEvent/StreamEvent) into
        buckets and flush every ``flush_interval`` until ``stop`` is set.
        NEVER raises out."""
        import asyncio
        logger.info("[DiscordBridge] started (flush=%.1fs, min_post=%.1fs)",
                    self._flush_interval, self._min_post)
        while True:
            deadline = time.monotonic() + self._flush_interval
            while time.monotonic() < deadline:
                try:
                    ev = await asyncio.wait_for(source.get(),
                                                timeout=max(0.01, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                except Exception:  # noqa: BLE001
                    break
                self.ingest(ev if isinstance(ev, BridgeEvent) else BridgeEvent.from_stream_event(ev))
            await self.flush(poster=poster)
            if stop is not None and getattr(stop, "is_set", lambda: False)():
                await self.flush(poster=poster)  # final drain
                return


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


async def _default_poster(url: str, content: str) -> int:
    """POST a Discord webhook message. Prefers httpx (bundles TLS certs → works on
    macOS + Linux); falls back to stdlib urllib in a thread (Linux containers have
    ca-certificates). Returns the HTTP status (0 on transport error). NEVER raises."""
    payload = {"content": content[:_DISCORD_MAX_CHARS]}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            return int(resp.status_code)
    except Exception:  # noqa: BLE001 — fall back to urllib
        import asyncio
        import json

        def _post() -> int:
            import urllib.request
            try:
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    return int(getattr(r, "status", 204) or 204)
            except Exception:  # noqa: BLE001
                return 0
        return await asyncio.to_thread(_post)


# ── in-process broker tap (the production source) ───────────────────────────
async def run_bridge_against_broker(*, stop: Any = None) -> None:
    """Subscribe to the live StreamEventBroker and bridge its events to Discord.
    Gated + fail-soft. This is what the soak boot starts when the master is on."""
    if not discord_bridge_enabled():
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_stream_broker,
        )
        broker = get_stream_broker()
        sub = broker.subscribe()
        if sub is None:
            logger.warning("[DiscordBridge] broker subscriber cap reached — not bridging")
            return
        bridge = DiscordBridge()
        try:
            await bridge.run(source=sub.queue, stop=stop)
        finally:
            try:
                broker.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DiscordBridge] broker tap unavailable: %s", exc)


__all__ = [
    "discord_bridge_enabled",
    "channel_for",
    "webhook_url_for",
    "BridgeEvent",
    "format_events",
    "DiscordBridge",
    "run_bridge_against_broker",
]
