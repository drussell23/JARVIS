"""Autonomous Wake-and-Execute (AWE) Trigger — the Midnight-Recovery reflex.

The systemic blind spot this closes: if DoubleWord stabilizes and the Sentinel
flips ``provider_state`` → ``HEALTHY`` at 3:00 AM, the queued definitive Agentic
Swarm soak would sit idle waiting for a human to type a CLI command — potentially
missing the entire stability window. The root cause is a *passive human
bottleneck* on the recovery path. The fix is a reactive, event-driven launch.

Architecture (Manifesto §3 asynchronous tendrils + §7 absolute observability):

  * **Asynchronous broker watcher** — subscribes IN-PROCESS to the same
    :class:`StreamEventBroker` the ``/observability/stream`` SSE uses, draining
    ``provider_state_changed`` frames. The producer is the existing
    ``ProviderStateWatcher`` (``start_provider_state_watcher``), so this adds a
    consumer, not a second poll loop (DRY). Fires only on the DEGRADED→HEALTHY
    *edge* (``state == HEALTHY and previous_state != HEALTHY``).

  * **Atomic execution lock** — the instant HEALTHY is seen, the trigger claims
    the ``soak_execution_lock`` in the SAME ``.jarvis/chunk_strategy.db`` via the
    established guarded-UPDATE compare-and-swap (see :mod:`soak_execution_lock`).
    A rapid HEALTHY↔DEGRADED↔HEALTHY flap during swarm startup finds a fresh
    claim inside the cooldown → the loser's UPDATE matches zero rows → suppressed.
    No double-launch. A genuinely new outage cycle hours later re-arms.

  * **SIGHUP-resilient detached launch** — once the lock is won, the injected
    ``launch_fn`` (the REAL soak path — nothing is cloned here) is detached via
    ``asyncio.create_task`` so it runs to completion without blocking the watcher
    or the REPL event loop. Every step publishes a breadcrumb event so the
    autonomous decision is visible in the TUI ``/breadcrumbs`` feed.

The trigger is pure orchestration: WHAT it launches is an injected seam, so it
duplicates no launch logic and is trivially testable. Master gate
``JARVIS_AWE_TRIGGER_ENABLED`` (default OFF — arming an autonomous soak launcher
is deliberately opt-in). Never raises on the hot path. Fable is never referenced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Optional

from backend.core.ouroboros.governance.post_soak_verification import (
    _default_git_porcelain,
)
from backend.core.ouroboros.governance.soak_execution_lock import (
    DEFAULT_LOCK_NAME,
    release_soak_lock,
    try_claim_soak_lock,
)

logger = logging.getLogger("Ouroboros.AWETrigger")

# run_id -> awaitable result. The trigger only ever awaits this; it neither
# knows nor cares how the soak is realized.
LaunchFn = Callable[[str], Awaitable[Any]]
BreadcrumbFn = Callable[[str, dict], None]
DbFactory = Callable[[], Any]


def awe_enabled() -> bool:
    """Master gate. Default OFF — an autonomous soak launcher is opt-in."""
    return os.environ.get(
        "JARVIS_AWE_TRIGGER_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes", "on")


def _default_db_factory():
    """Open the shared ``.jarvis/chunk_strategy.db`` (same DB as the forecaster /
    provider-state / strategy-outcome tables). ``None`` on failure — the trigger
    fail-closes (never launches) when it cannot prove a lock claim."""
    try:
        from backend.core.ouroboros.governance.dw_outage_forecaster import (
            open_forecast_db,
        )
        return open_forecast_db()
    except Exception:  # noqa: BLE001
        return None


def _default_breadcrumb_fn(event_type: str, payload: dict) -> None:
    """Publish an AWE breadcrumb onto the broker so the unified event router
    surfaces it in ``/breadcrumbs``. Best-effort; never raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_task_event,
        )
        op_id = str(payload.get("provider", "doubleword")) or "doubleword"
        publish_task_event(event_type, op_id, dict(payload))
    except Exception:  # noqa: BLE001
        pass


class AWETrigger:
    """Watches for the DW recovery edge and autonomously launches the soak once."""

    def __init__(
        self,
        *,
        launch_fn: LaunchFn,
        db_factory: Optional[DbFactory] = None,
        provider: str = "doubleword",
        lock_name: str = DEFAULT_LOCK_NAME,
        cooldown_s: Optional[float] = None,
        breadcrumb_fn: Optional[BreadcrumbFn] = None,
        run_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._launch_fn = launch_fn
        self._db_factory = db_factory or _default_db_factory
        self._provider = provider
        self._lock_name = lock_name
        self._cooldown_s = cooldown_s
        self._breadcrumb_fn = breadcrumb_fn or _default_breadcrumb_fn
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._watch_task: Optional[asyncio.Task] = None
        self._soak_task: Optional[asyncio.Task] = None
        # Set once the broker subscription is live — lets a caller/test publish
        # deterministically without racing the subscribe.
        self._subscribed: asyncio.Event = asyncio.Event()
        self._launch_count = 0  # observable: how many soaks this process fired

    # -- breadcrumbs ----------------------------------------------------

    def _emit(self, event_type: str, payload: dict) -> None:
        try:
            self._breadcrumb_fn(event_type, payload)
        except Exception:  # noqa: BLE001 — a breadcrumb never breaks the trigger
            logger.debug("[AWE] breadcrumb emit failed", exc_info=True)

    # -- edge detection + atomic claim ----------------------------------

    async def on_state_event(self, payload: dict) -> bool:
        """Handle one ``provider_state_changed`` payload. Returns ``True`` iff a
        soak was launched by THIS event. Idles (returns ``False``) on any state
        that is not a transition INTO ``HEALTHY``. Never raises."""
        try:
            state = str((payload or {}).get("state", "")).upper()
            prev = str((payload or {}).get("previous_state", "")).upper()
        except Exception:  # noqa: BLE001
            return False
        # Idle while DEGRADED/UNKNOWN, and on a HEALTHY→HEALTHY non-edge.
        if state != "HEALTHY" or prev == "HEALTHY":
            return False
        return await self._try_launch(dict(payload or {}))

    async def _try_launch(self, payload: dict) -> bool:
        conn = None
        try:
            conn = self._db_factory()
        except Exception:  # noqa: BLE001
            conn = None
        run_id = self._run_id_factory()
        won = try_claim_soak_lock(
            conn, run_id, lock_name=self._lock_name, cooldown_s=self._cooldown_s,
        )
        if not won:
            # The flap guard: a redundant recovery edge inside the cooldown.
            self._emit("awe_soak_suppressed", {
                "provider": self._provider, "run_id": run_id,
                "reason": "lock held (flap guard)",
            })
            logger.info("[AWE] recovery edge suppressed by flap guard (lock held)")
            return False
        self._launch_count += 1
        self._emit("awe_soak_launched", {
            "provider": self._provider, "run_id": run_id,
            "previous_state": payload.get("previous_state", ""),
            "reason": "DEGRADED→HEALTHY — arming definitive soak",
        })
        logger.info("[AWE] DW recovery edge — soak lock claimed run_id=%s; detaching soak", run_id)
        # SIGHUP-resilient: detach so a long soak never blocks the watcher / REPL.
        try:
            self._soak_task = asyncio.create_task(self._run_soak(run_id))
        except Exception:  # noqa: BLE001 — could not even START → don't wedge the lock
            logger.debug("[AWE] soak task creation failed; releasing lock", exc_info=True)
            try:
                release_soak_lock(self._db_factory(), lock_name=self._lock_name)
            except Exception:  # noqa: BLE001
                pass
            self._emit("awe_soak_failed", {
                "provider": self._provider, "run_id": run_id,
                "reason": "launch task creation failed",
            })
            return False
        return True

    async def _run_soak(self, run_id: str) -> Any:
        """Await the injected real soak launcher to completion, breadcrumbing its
        terminal. A successful soak HOLDS the lock (one-shot per cooldown); a
        failure RELEASES it so a later recovery may retry. Never raises out."""
        try:
            result = await self._launch_fn(run_id)
            self._emit("awe_soak_complete", {
                "provider": self._provider, "run_id": run_id,
                "result": str(result)[:200] if result is not None else "",
            })
            logger.info("[AWE] soak run_id=%s reached terminal", run_id)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AWE] soak run_id=%s failed: %s", run_id, exc)
            self._emit("awe_soak_failed", {
                "provider": self._provider, "run_id": run_id, "reason": str(exc)[:200],
            })
            try:
                release_soak_lock(self._db_factory(), lock_name=self._lock_name)
            except Exception:  # noqa: BLE001
                pass
            return None

    # -- the async watcher ----------------------------------------------

    async def watch(self, *, max_state_events: Optional[int] = None) -> None:
        """Subscribe to the broker and drain ``provider_state_changed`` frames,
        dispatching each to :meth:`on_state_event`. Mirrors the established
        best-effort subscribe/stream_iter/finally-unsubscribe pattern used by the
        other in-process breadcrumb listeners. ``max_state_events`` bounds the
        loop for deterministic tests. Never raises."""
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_PROVIDER_STATE_CHANGED,
            get_default_broker,
        )

        broker = None
        sub = None
        seen = 0
        try:
            broker = get_default_broker()
            sub = broker.subscribe(op_id_filter=self._provider)
            if sub is None:
                logger.debug("[AWE] broker subscribe returned None (cap?) — trigger idle")
                self._subscribed.set()
                return
            self._subscribed.set()
            async for event in broker.stream_iter(sub, heartbeat_s=0):
                if getattr(event, "event_type", "") != EVENT_TYPE_PROVIDER_STATE_CHANGED:
                    continue
                try:
                    payload = dict(getattr(event, "payload", {}) or {})
                    await self.on_state_event(payload)
                except Exception:  # noqa: BLE001 — one bad event never kills the loop
                    logger.debug("[AWE] on_state_event raised", exc_info=True)
                seen += 1
                if max_state_events is not None and seen >= max_state_events:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the watcher is strictly best-effort
            logger.debug("[AWE] watch loop exited on error", exc_info=True)
            return
        finally:
            self._subscribed.set()
            try:
                if broker is not None and sub is not None:
                    broker.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass

    # -- lifecycle ------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Spawn the watch loop as a background task and return it."""
        self._watch_task = asyncio.ensure_future(self.watch())
        return self._watch_task

    async def stop(self) -> None:
        """Cancel the watch loop and any in-flight detached soak. Never raises."""
        for attr in ("_watch_task", "_soak_task"):
            t = getattr(self, attr, None)
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Launch-fn bindings — DRY seams onto the REAL soak paths (nothing cloned)
# ---------------------------------------------------------------------------


def build_swarm_coroutine_launch_fn(
    *,
    client: Any,
    source: str,
    file_path: str,
    symbols: list,
    repo_root: str = ".",
    op_id_prefix: str = "awe",
) -> LaunchFn:
    """Bind the in-process big-file Agentic Swarm — awaits the SAME public egress
    (``intercept_full_content``) the generation hot path uses in
    ``candidate_generator._maybe_swarm_short_circuit``, constructing the
    ``ProductionAgentTurnFn`` identically. This is the literal 4-agent
    chunk→repair→stitch fan-out/fan-in the operator described. DRY: it reuses the
    real egress rather than duplicating swarm logic."""

    async def _launch(run_id: str) -> Any:
        from backend.core.ouroboros.governance.full_content_interceptor import (
            intercept_full_content,
        )
        from backend.core.ouroboros.governance.agent_turn_adapter import (
            ProductionAgentTurnFn,
        )
        op_id = f"{op_id_prefix}-{run_id}"
        agent = ProductionAgentTurnFn(
            client=client,
            tool_backend=None,
            repo_root=repo_root,
            op_id=op_id,
            model_name=getattr(client, "_model", "") or "",
            system_prompt="",
            parse_fn=lambda raw: None,
            max_turns=1,
        )
        return await intercept_full_content(
            source, file_path, list(symbols or []), agent, op_id=op_id,
        )

    return _launch


def build_soak_subprocess_launch_fn(
    *,
    script: str = "scripts/ouroboros_battle_test.py",
    args: Optional[list] = None,
    extra_env: Optional[dict] = None,
) -> LaunchFn:
    """Bind the definitive battle-test soak as a detached subprocess with
    ``JARVIS_SWARM_ROUTING_ENABLED=true`` (the flag the swarm route reads). The
    ``_run_soak`` coroutine awaits the subprocess to completion — in-process
    awaited, detached, non-blocking, and returning its exit code. This is the
    self-contained "definitive Agentic Swarm soak" launch when no live client is
    threaded into the trigger's process."""
    import sys

    async def _launch(run_id: str) -> int:
        env = dict(os.environ)
        env["JARVIS_SWARM_ROUTING_ENABLED"] = "true"
        env.setdefault("OUROBOROS_BATTLE_HEADLESS", "1")
        env["JARVIS_AWE_SOAK_RUN_ID"] = run_id
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        argv = [sys.executable, script] + list(args or ["--headless"])
        proc = await asyncio.create_subprocess_exec(*argv, env=env)
        rc = await proc.wait()
        return rc

    return _launch


def _default_client_factory() -> Any:
    """Lazily construct the DW-primary client the checkpointed swarm drives.

    Same lazy-handle pattern ``moltbook_garnish`` uses for its DW call (DRY —
    one construction idiom for "I need a provider handle right now"). Returns
    ``None`` on any failure, which the dispatcher treats as "cannot run the
    manifest in-process" and routes to the subprocess fallback rather than
    fabricating a client."""
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )
        return DoublewordProvider()
    except Exception:  # noqa: BLE001
        return None


class DirtyTreeRefusal(RuntimeError):
    """AWE refused to launch because the working tree carried uncommitted work.

    Raised (not returned) on purpose: :meth:`AWETrigger._run_soak` already
    treats an exception as "soak did not happen" and RELEASES the execution
    lock, so a later recovery edge against a clean tree can still run. A
    silent return would hold the lock for the whole cooldown and swallow the
    recovery window."""


def _clean_tree_required() -> bool:
    """Whether AWE refuses to launch onto a dirty working tree. Default ON.

    An autonomous soak mutates real files and (with auto-commit armed) commits
    them. Starting that on top of uncommitted human work is how a machine
    commit swallows an operator's in-flight edit — the #70033 shape. Cheap
    insurance: one read-only ``git status --porcelain``."""
    return os.environ.get(
        "JARVIS_AWE_REQUIRE_CLEAN_TREE", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def _clean_tree_allowlist() -> tuple:
    """Path prefixes permitted to be dirty, from ``JARVIS_AWE_CLEAN_TREE_ALLOW``
    (comma-separated). Empty by default — strict. Exists so an operator can
    permit e.g. the soak's own output directory on a resume without disarming
    the invariant wholesale."""
    raw = (os.environ.get("JARVIS_AWE_CLEAN_TREE_ALLOW", "") or "").strip()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _dirty_paths(git_status_fn: Callable[[], Any]) -> tuple:
    """Porcelain lines → the offending paths, minus the allowlist. Fail-CLOSED
    is wrong here and fail-open is wrong too, so: a git read that errors returns
    [] from the shared helper (its documented behaviour), which we treat as
    clean — the invariant is a guard against KNOWN dirt, not a proof of
    cleanliness, and refusing every launch because git is unreadable would wedge
    the recovery path entirely."""
    allow = _clean_tree_allowlist()
    out = []
    try:
        for line in git_status_fn() or []:
            path = str(line)[3:].strip().strip('"')
            if not path:
                continue
            if any(path.startswith(p) for p in allow):
                continue
            out.append(path)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(out)


def build_manifest_aware_launch_fn(
    *,
    db_factory: Optional[DbFactory] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    fallback_fn: Optional[LaunchFn] = None,
    breadcrumb_fn: Optional[BreadcrumbFn] = None,
    max_priority: Optional[int] = None,
    git_status_fn: Optional[Callable[[], Any]] = None,
) -> LaunchFn:
    """The substrate-aware dispatcher: inspect state, THEN choose the strategy.

    Closes the orphan-``run_pending_soak`` gap. Before this, AWE's only launch
    strategy was a hardcoded ``scripts/ouroboros_battle_test.py`` spawn, so a
    queued checkpoint manifest (e.g. the canary's 7 chunks) survived the
    recovery edge untouched — the intent stayed ``pending`` forever while a
    generic, un-manifested soak ran in its place.

    On each recovery edge:

      * **pending intent WITH a manifest** → :func:`run_pending_soak`, resuming
        that intent's chunks against the real swarm. Crash-resumable: every
        chunk commits atomically, so a second edge picks up where this left off.
      * **empty queue, no manifest, or no resolvable client** → the existing
        subprocess soak, unchanged.

    ``repo_root`` for the manifest path is the intent's ``target``, not the
    repository root — see :class:`PendingIntent`. Both strategies breadcrumb
    which way they went, so the choice is visible in ``/breadcrumbs`` rather
    than inferred.

    Failure policy: every *dispatch* failure degrades to the fallback rather
    than raising — EXCEPT the clean-tree invariant, which raises
    :class:`DirtyTreeRefusal` by design so the trigger releases its execution
    lock and a later edge against a clean tree can still run."""
    _db = db_factory or _default_db_factory
    _client = client_factory or _default_client_factory
    _fallback = fallback_fn or build_soak_subprocess_launch_fn()
    _crumb = breadcrumb_fn or _default_breadcrumb_fn

    def _emit(strategy: str, detail: dict) -> None:
        try:
            _crumb("awe_launch_strategy", {"strategy": strategy, **detail})
        except Exception:  # noqa: BLE001
            pass

    async def _launch(run_id: str) -> Any:
        from backend.core.ouroboros.governance.soak_intent import next_pending_intent

        # ── Clean-tree invariant ──────────────────────────────────────────
        # Gate the WHOLE dispatch, not just the manifest branch: the generic
        # soak mutates the repo too, so "don't run an autonomous mutator on
        # top of uncommitted human work" applies identically to both.
        if _clean_tree_required():
            _git = git_status_fn or _default_git_porcelain
            dirty = _dirty_paths(_git)
            if dirty:
                shown = ", ".join(dirty[:5]) + (" …" if len(dirty) > 5 else "")
                _emit("refused_dirty_tree", {
                    "run_id": run_id, "dirty_count": len(dirty),
                    "dirty_sample": list(dirty[:5]),
                })
                logger.warning(
                    "[AWE] REFUSING launch run_id=%s — working tree dirty "
                    "(%d path(s): %s). Commit or stash, then the next recovery "
                    "edge will run.", run_id, len(dirty), shown,
                )
                try:
                    from backend.core.ouroboros.governance.moltbook import (
                        post_molt_nowait,
                    )
                    post_molt_nowait(
                        "@first-responder", "distress",
                        facts={
                            "what": "AWE refused the soak launch",
                            "why": "working tree is dirty",
                            "dirty_count": len(dirty),
                            "sample": shown,
                            "run_id": run_id,
                        },
                    )
                except Exception:  # noqa: BLE001 — the agora is never load-bearing
                    pass
                raise DirtyTreeRefusal(
                    f"working tree dirty ({len(dirty)} path(s)): {shown}"
                )

        conn = None
        try:
            conn = _db()
        except Exception:  # noqa: BLE001
            conn = None

        intent = None
        try:
            intent = next_pending_intent(conn, max_priority=max_priority)
        except Exception:  # noqa: BLE001
            intent = None

        if intent is None or not intent.has_manifest:
            _emit("subprocess_fallback", {
                "run_id": run_id,
                "reason": "no pending intent" if intent is None else "intent carries no manifest",
                "intent_id": getattr(intent, "intent_id", ""),
            })
            logger.info(
                "[AWE] dispatch → generic soak (run_id=%s, %s)",
                run_id, "queue empty" if intent is None else "no manifest",
            )
            return await _fallback(run_id)

        client = None
        try:
            client = _client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            # Honest degradation: we know there IS a manifest but cannot drive
            # it in-process. Run the generic soak rather than silently dropping
            # the recovery edge — and say so.
            _emit("subprocess_fallback", {
                "run_id": run_id, "intent_id": intent.intent_id,
                "reason": "no resolvable client for in-process manifest run",
            })
            logger.warning(
                "[AWE] manifest intent %s pending but no client resolvable — "
                "falling back to generic soak; intent stays pending",
                intent.intent_id,
            )
            return await _fallback(run_id)

        from backend.core.ouroboros.governance.checkpoint_manifest import (
            run_pending_soak,
        )
        _emit("manifest", {
            "run_id": run_id, "intent_id": intent.intent_id,
            "kind": intent.kind, "target": intent.target,
        })
        logger.info(
            "[AWE] dispatch → checkpointed manifest soak intent=%s kind=%s target=%s",
            intent.intent_id, intent.kind, intent.target,
        )
        return await run_pending_soak(
            conn, intent.intent_id, client=client,
            # The manifest's chunk file_paths are relative to `target`.
            repo_root=intent.target or ".",
        )

    return _launch


def start_awe_trigger(
    *,
    launch_fn: Optional[LaunchFn] = None,
    provider: str = "doubleword",
    **kwargs: Any,
) -> Optional[AWETrigger]:
    """Construct + start the AWE trigger IFF ``JARVIS_AWE_TRIGGER_ENABLED``.
    Returns the live :class:`AWETrigger` (whose ``.stop()`` the caller tears down)
    or ``None`` when disabled / on any error. Defaults ``launch_fn`` to the
    substrate-aware dispatcher, which resumes a queued checkpoint manifest when
    one exists and otherwise runs the self-contained subprocess soak. Never
    raises."""
    if not awe_enabled():
        return None
    try:
        trigger = AWETrigger(
            launch_fn=launch_fn or build_manifest_aware_launch_fn(
                db_factory=kwargs.get("db_factory"),
            ),
            provider=provider,
            **kwargs,
        )
        trigger.start()
        logger.info("[AWE] armed — watching for %s DEGRADED→HEALTHY recovery edge", provider)
        return trigger
    except Exception:  # noqa: BLE001
        logger.debug("[AWE] start failed", exc_info=True)
        return None


__all__ = [
    "AWETrigger",
    "awe_enabled",
    "DirtyTreeRefusal",
    "build_manifest_aware_launch_fn",
    "build_soak_subprocess_launch_fn",
    "build_swarm_coroutine_launch_fn",
    "start_awe_trigger",
]
