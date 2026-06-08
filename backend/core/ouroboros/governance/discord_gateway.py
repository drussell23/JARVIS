"""Slice 156 — The Sovereign Bidirectional Gateway (interactive Discord control).

The webhook bridge (Slices 142-145) streams O+V's activity OUT (read-only). This
closes the loop with remote ARBITRATION: [APPROVE] / [REJECT] / [STEER] buttons in
Discord that resolve the SAME approval rendezvous the local TUI does — so an Orange
APPROVAL_REQUIRED block can be cleared from your phone, exactly as if typed locally.

Composition (no new control plane, no duplication):
  * [APPROVE]/[REJECT] → the existing ``CLIApprovalProvider`` (per-op asyncio.Event;
    ``approve``/``reject`` set it → the proactive loop resumes).
  * [STEER] → the existing ``ConversationBridge`` (TUI→FSM input spine): the captured
    text enters the next cycle as a prompt constraint; the current op is unblocked.

Security: every interaction is authorized against ``DISCORD_OPERATOR_ID`` (from .env,
never hardcoded). An unauthorized click is DROPPED, logged ``REFUSED_SAFETY``, and
ignored — fail-closed when the operator id is unconfigured (deny all).

Architecture: the COMMAND ROUTER is pure logic (no discord.py) → fully unit-tested.
The discord.py gateway daemon is a thin, lazy-imported, fail-soft adapter around it
(needs a live ``DISCORD_BOT_TOKEN``). Gated ``JARVIS_DISCORD_GATEWAY_ENABLED``
default-FALSE; runs decoupled as a background task so it never blocks the FSM loop.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_DISCORD_GATEWAY_ENABLED"
_ENV_OPERATOR_ID = "DISCORD_OPERATOR_ID"
_ENV_BOT_TOKEN = "DISCORD_BOT_TOKEN"


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.5, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def discord_gateway_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def authorized_operator_id() -> Optional[str]:
    """The sole authorized Discord operator id (from .env), or None if unconfigured."""
    v = (os.getenv(_ENV_OPERATOR_ID, "") or "").strip()
    return v or None


def is_authorized_operator(user_id: Any) -> bool:
    """True iff ``user_id`` matches ``DISCORD_OPERATOR_ID``. FAIL-CLOSED: if the id
    is unconfigured, deny EVERYONE (no implicit-trust). NEVER raises."""
    try:
        configured = authorized_operator_id()
        if not configured:
            return False
        return str(user_id).strip() == configured
    except Exception:  # noqa: BLE001
        return False


# on_refused(user_id=..., action=..., op_id=...) — REFUSED_SAFETY sink (injectable)
_RefusedHook = Callable[..., None]


class GatewayCommandRouter:
    """Pure-logic command router — the security + FSM-resolution core of the bot.

    Composes the existing approval provider + conversation bridge; no discord.py.
    Every command is authorized first (fail-closed). NEVER raises out of ``handle``."""

    def __init__(
        self,
        *,
        approval_provider: Any,
        conversation_bridge: Any = None,
        on_refused: Optional[_RefusedHook] = None,
        on_decision: Optional[Callable[..., None]] = None,
    ) -> None:
        self._provider = approval_provider
        self._bridge = conversation_bridge
        self._on_refused = on_refused
        # on_decision(action=, op_id=, user_id=) — fired ONLY after an AUTHORIZED
        # decision resolves (so REJECTED_BY_OPERATOR can be logged to a channel).
        self._on_decision = on_decision

    async def handle(
        self, action: str, op_id: str, user_id: Any, text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Authorize + dispatch a button/modal action. Returns a result dict.
        Unauthorized → dropped + REFUSED_SAFETY (no side effect)."""
        # ── Biometric authorization guard (fail-closed) ──
        if not is_authorized_operator(user_id):
            logger.warning(
                "[DiscordGateway] REFUSED_SAFETY — unauthorized interaction "
                "user_id=%r action=%r op_id=%r (not DISCORD_OPERATOR_ID); dropped",
                user_id, action, op_id,
            )
            self._fire_refused(user_id=str(user_id), action=action, op_id=op_id)
            return {"ok": False, "refused": True, "reason": "unauthorized"}

        approver = f"discord:{user_id}"
        act = (action or "").strip().lower()
        try:
            if act == "approve":
                res = await self._provider.approve(op_id, approver)
                logger.info("[DiscordGateway] APPROVE op=%s by=%s", op_id, approver)
                self._fire_decision(action="approve", op_id=op_id, user_id=str(user_id))
                return {"ok": True, "action": "approve", "result": res}
            if act == "reject":
                res = await self._provider.reject(op_id, approver, text or "rejected via Discord")
                logger.info("[DiscordGateway] REJECTED_BY_OPERATOR op=%s by=%s", op_id, approver)
                self._fire_decision(action="reject", op_id=op_id, user_id=str(user_id))
                return {"ok": True, "action": "reject", "result": res}
            if act == "steer":
                # Feed the steer text into the FSM input spine (next-cycle constraint),
                # then unblock the current op (reject) so the loop redirects.
                constraint = (text or "").strip()
                if constraint and self._bridge is not None:
                    try:
                        self._bridge.note_tui_user(f"[steer] {constraint}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[DiscordGateway] steer bridge note failed: %s", exc)
                await self._provider.reject(op_id, approver, f"steered via Discord: {constraint}")
                logger.info("[DiscordGateway] STEER op=%s by=%s", op_id, approver)
                self._fire_decision(action="steer", op_id=op_id, user_id=str(user_id))
                return {"ok": True, "action": "steer", "constraint": constraint}
            logger.warning("[DiscordGateway] unknown action=%r op=%s — ignored", action, op_id)
            return {"ok": False, "reason": f"unknown action: {action}"}
        except Exception as exc:  # noqa: BLE001 — a control click never crashes the loop
            logger.warning("[DiscordGateway] handle(%s) failed: %s", act, exc)
            return {"ok": False, "reason": repr(exc)}

    def _fire_refused(self, **kw: Any) -> None:
        if self._on_refused is None:
            return
        try:
            self._on_refused(**kw)
        except Exception:  # noqa: BLE001
            pass

    def _fire_decision(self, **kw: Any) -> None:
        if self._on_decision is None:
            return
        try:
            self._on_decision(**kw)
        except Exception:  # noqa: BLE001
            pass


# Slice 158 — channel dispatch (DMs are a fragile governance surface; route approvals
# to a shared #governance-gates text channel instead). The interaction.user.id guard
# in the View callbacks stays intact, so only the operator's clicks are honored.
def gates_channel_name() -> str:
    """Target channel name for approval dispatch (env-tunable). Default
    'governance-gates'. NEVER raises."""
    return (os.getenv("JARVIS_DISCORD_GATES_CHANNEL", "") or "").strip() or "governance-gates"


def gates_channel_id() -> str:
    """Optional explicit channel-id override (numeric string), or '' to resolve by name."""
    return (os.getenv("JARVIS_DISCORD_GATES_CHANNEL_ID", "") or "").strip()


def pick_gates_channel(channels: Any, *, channel_id: str = "", channel_name: str = "governance-gates") -> Any:
    """Resolve the gates channel from the bot's visible channels: explicit id wins;
    a missing/unknown id falls back to name; returns None if neither matches."""
    if channel_id:
        for ch in channels:
            if str(getattr(ch, "id", "")) == str(channel_id):
                return ch
    for ch in channels:
        if getattr(ch, "name", None) == channel_name:
            return ch
    return None


# Slice 157 — live-fire injection source tag (a genuine, non-github/ci channel source
# → _classify_event's generic branch renders the op description from the event_type).
LIVEFIRE_SOURCE = "livefire"


def build_livefire_payload(task: str) -> Dict[str, Any]:
    """Build the /webhook/generic JSON body for a GENUINE live-fire op. The task text
    rides in ``type`` because EventChannel._classify_event renders the op description
    as '<source> event: <type>' for non-github/ci sources. Includes a dedup signature."""
    return {
        "type": task,
        "source": LIVEFIRE_SOURCE,
        "signature": f"livefire:{task}",
    }


def summarize_pending(req: Dict[str, Any]) -> str:
    """Embed text for a pending approval. Fixes the field mismatch: list_pending()
    returns ``description``/``target_files`` (the gateway previously read only
    summary/reason → a bare op_id). Prefers the real op description + targets."""
    op_id = str(req.get("op_id") or req.get("request_id") or "")
    desc = str(req.get("summary") or req.get("reason") or req.get("description") or "").strip()
    targets = req.get("target_files") or ()
    if not desc:
        return op_id
    if targets:
        try:
            tf = ", ".join(str(t) for t in targets)
        except Exception:  # noqa: BLE001
            tf = str(targets)
        return f"{desc}\n\n**Targets:** {tf}"
    return desc


def build_router_from_gls(
    gls: Any, *,
    on_refused: Optional[_RefusedHook] = None,
    on_decision: Optional[Callable[..., None]] = None,
) -> "GatewayCommandRouter":
    """Compose a router from a live GovernedLoopService (its _approval_provider +
    the ConversationBridge TUI sink). Best-effort — missing pieces degrade gracefully."""
    provider = getattr(gls, "_approval_provider", None)
    bridge = None
    try:
        from backend.core.ouroboros.governance import conversation_bridge as _cb
        # adapter exposing note_tui_user(text) over whatever the bridge offers
        class _BridgeAdapter:
            def note_tui_user(self, text: str) -> None:
                fn = getattr(_cb, "note_tui_user", None) or getattr(_cb, "note", None)
                if fn is not None:
                    fn(text)
        bridge = _BridgeAdapter()
    except Exception:  # noqa: BLE001
        bridge = None
    return GatewayCommandRouter(
        approval_provider=provider, conversation_bridge=bridge,
        on_refused=on_refused, on_decision=on_decision,
    )


async def run_gateway_daemon(gls: Any, *, stop: Any = None) -> None:
    """Thin discord.py adapter: connect the bot, and on each Orange APPROVAL_REQUIRED
    dispatch a Rich embed + a View with [APPROVE]/[REJECT]/[STEER] whose callbacks
    route through :class:`GatewayCommandRouter`. Gated + fail-soft + lazy-imported.

    Requires ``DISCORD_BOT_TOKEN`` (a Discord *bot* app token — distinct from the
    channel webhooks). NEVER raises into the caller. This adapter is intentionally
    thin: all security + FSM-resolution lives in the unit-tested router above."""
    if not discord_gateway_enabled():
        return
    token = (os.getenv(_ENV_BOT_TOKEN, "") or "").strip()
    if not token:
        logger.warning("[DiscordGateway] enabled but DISCORD_BOT_TOKEN unset — not starting")
        return
    if not authorized_operator_id():
        logger.warning("[DiscordGateway] enabled but DISCORD_OPERATOR_ID unset — fail-closed, not starting")
        return
    try:
        import discord  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DiscordGateway] discord.py unavailable (%s) — gateway disabled", exc)
        return

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    # ── Decision broadcast: post the operator's verdict (esp. REJECTED_BY_OPERATOR)
    # to a governance-gates channel. Off the FSM loop; fail-soft; optional channel.
    _gates_channel_id = (os.getenv("JARVIS_DISCORD_GATES_CHANNEL_ID", "") or "").strip()

    async def _post_decision(action: str, op_id: str, user_id: str) -> None:
        if not _gates_channel_id:
            return
        try:
            ch = client.get_channel(int(_gates_channel_id)) \
                or await client.fetch_channel(int(_gates_channel_id))
            verb = {"approve": "✅ APPROVED_BY_OPERATOR",
                    "reject": "🛑 REJECTED_BY_OPERATOR",
                    "steer": "🧭 STEERED_BY_OPERATOR"}.get(action, action.upper())
            await ch.send(embed=discord.Embed(
                title=verb, description=f"op `{op_id}` · operator `{user_id}`",
                color=0xE74C3C if action == "reject" else 0x2ECC71))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DiscordGateway] gates-channel post failed: %s", exc)

    def _on_decision(action: str = "", op_id: str = "", user_id: str = "") -> None:
        try:
            client.loop.create_task(_post_decision(action, op_id, user_id))
        except Exception:  # noqa: BLE001
            pass

    router = build_router_from_gls(gls, on_decision=_on_decision)

    def _make_view(op_id: str) -> Any:
        class _ArbitrationView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

            @discord.ui.button(label="APPROVE", style=discord.ButtonStyle.success)
            async def _approve(self, interaction: Any, _button: Any) -> None:
                r = await router.handle("approve", op_id, interaction.user.id)
                await interaction.response.send_message(
                    "✅ approved" if r.get("ok") else "⛔ refused", ephemeral=True)

            @discord.ui.button(label="REJECT", style=discord.ButtonStyle.danger)
            async def _reject(self, interaction: Any, _button: Any) -> None:
                r = await router.handle("reject", op_id, interaction.user.id)
                await interaction.response.send_message(
                    "🛑 rejected" if r.get("ok") else "⛔ refused", ephemeral=True)

            @discord.ui.button(label="STEER", style=discord.ButtonStyle.primary)
            async def _steer(self, interaction: Any, _button: Any) -> None:
                # Authorize BEFORE showing the modal so intruders can't even type.
                if not is_authorized_operator(interaction.user.id):
                    await interaction.response.send_message("⛔ refused", ephemeral=True)
                    router._fire_refused(user_id=str(interaction.user.id), action="steer", op_id=op_id)
                    return

                class _SteerModal(discord.ui.Modal, title="Steer O+V"):
                    constraint = discord.ui.TextInput(label="Updated constraint / target vector")

                    async def on_submit(self, modal_interaction: Any) -> None:
                        await router.handle("steer", op_id, modal_interaction.user.id,
                                            text=str(self.constraint))
                        await modal_interaction.response.send_message("🧭 steered", ephemeral=True)

                await interaction.response.send_modal(_SteerModal())

        return _ArbitrationView()

    client._jarvis_make_view = _make_view  # type: ignore[attr-defined]

    # ── Outbound dispatch (Slice 158): post a Rich embed + arbitration View for each
    # NEW Orange APPROVAL_REQUIRED to the #governance-gates CHANNEL (not DMs — the
    # account-limit proved DMs are fragile). Polls the EXISTING list_pending()
    # (decoupled). Off the FSM loop. The interaction.user.id guard in the View stays
    # intact, so only the operator's clicks in the shared channel are honored.
    import asyncio as _aio
    _interval = _env_float("JARVIS_DISCORD_GATEWAY_POLL_S", 3.0)
    _seen: set = set()
    _warned_no_channel = {"v": False}

    async def _dispatch_pending() -> None:
        await client.wait_until_ready()
        provider = getattr(gls, "_approval_provider", None)
        while not (stop is not None and getattr(stop, "is_set", lambda: False)()):
            try:
                channel = pick_gates_channel(
                    list(client.get_all_channels()),
                    channel_id=gates_channel_id(),
                    channel_name=gates_channel_name(),
                )
                if channel is None:
                    if not _warned_no_channel["v"]:
                        logger.warning(
                            "[DiscordGateway] gates channel #%s not found (create it + "
                            "invite O+V, or set JARVIS_DISCORD_GATES_CHANNEL_ID) — "
                            "approvals cannot be dispatched", gates_channel_name(),
                        )
                        _warned_no_channel["v"] = True
                else:
                    _warned_no_channel["v"] = False
                    pending = await provider.list_pending() if provider is not None else []
                    for req in pending or []:
                        op_id = str(req.get("op_id") or req.get("request_id") or "")
                        if not op_id or op_id in _seen:
                            continue
                        _seen.add(op_id)
                        embed = discord.Embed(
                            title="🚧 APPROVAL_REQUIRED — O+V awaiting arbitration",
                            description=summarize_pending(req)[:4000],
                            color=0xE67E22,
                        )
                        embed.add_field(name="op", value=op_id[:64], inline=False)
                        embed.set_footer(text=f"only operator {authorized_operator_id()} may decide")
                        await channel.send(embed=embed, view=_make_view(op_id))
            except Exception as exc:  # noqa: BLE001 — never crash the gateway
                logger.debug("[DiscordGateway] dispatch poll error: %s", exc)
            await _aio.sleep(_interval)

    @client.event
    async def on_ready() -> None:  # noqa: ANN001
        logger.warning("[DiscordGateway] connected as %s — arbitration live", client.user)
        client.loop.create_task(_dispatch_pending())

    logger.warning("[DiscordGateway] interactive control gateway connecting (Slice 156)…")
    try:
        await client.start(token)
    except Exception as exc:  # noqa: BLE001 — a gateway failure never crashes the soak
        logger.warning("[DiscordGateway] gateway stopped: %s", exc)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "discord_gateway_enabled",
    "authorized_operator_id",
    "is_authorized_operator",
    "GatewayCommandRouter",
    "build_router_from_gls",
    "run_gateway_daemon",
]
