"""The diff an operator asked to see, rendered without stalling the cockpit.

`DiffArchive` keeps the last ~30 Yellow-tier candidates behind stable ``d-N``
refs, and `DiffPreviewRenderer` knows how to draw one. Between them there was no
way to actually LOOK at a diff on the cockpit: `/expand d-N` printed into a
console, and the gate's own preview is transient. So the archive filled up with
refs the operator was invited to name and could not open.

Where the cost actually is
==========================
The brief said "reading large diff payloads from disk". It is worth being exact,
because designing against the wrong cost produces a fix that protects nothing:
`DiffArchive` is an in-memory RING. ``diff_text`` is already resident, and
fetching it is a dict lookup.

The frame-dropping cost is elsewhere, and it is real:

  * **Rich syntax highlighting.** Pygments-lexing a few thousand diff lines and
    composing them into segments is hundreds of milliseconds of pure CPU. On the
    event loop that is a visible freeze — at 60fps the budget for an entire frame
    is 16ms.
  * **A review branch.** `ArchivedDiff.review_branch` names a real git branch, and
    resolving it is a subprocess against the object store — genuinely I/O, and
    genuinely unbounded.

Both belong off the loop. So `asyncio.to_thread` wraps the RENDER, not a fake
disk read, and the distinction is the difference between a fix and a decoration.

Pure-pull render, out-of-band fill
==================================
A prompt_toolkit render callable is SYNCHRONOUS — `create_content` cannot await —
so an overlay that fetched during render would block the frame no matter which
thread the fetch used. There is no version of "await inside the renderer".

The split that does work is the one `attach_heartbeat` and `MiniCrest` already
use: state is filled asynchronously and out of band; the renderer is a pure,
O(1) read of whatever has landed. :meth:`rows` never blocks, never touches the
archive and never renders — it returns a list of strings that a background task
prepared. Opening the overlay shows an immediate placeholder and invalidates once
the real payload arrives, so the cockpit stays at frame rate through a diff of
any size.

Epochs, because the operator moves faster than the render
=========================================================
Every open bumps an epoch and a completing task whose epoch is stale DISCARDS its
result. Without that:

  * open `d-3`, then `d-7` before the first finishes → whichever thread wins
    decides what is on screen, and it is not necessarily the one asked for last
  * open, then dismiss mid-render → the finishing task resurrects an overlay the
    operator already closed, which is worse than a slow one

Cheaper and stricter than cancellation: a thread already inside Pygments cannot
be interrupted, so the honest move is to let it finish and ignore it.

NEVER raises. An overlay that can break the cockpit it draws over is worse than
no overlay.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger("Ouroboros.DiffOverlay")

DIFF_OVERLAY_SCHEMA_VERSION = "diff_overlay.v1"

#: The name it registers under with `overlay_arbiter`.
OVERLAY_NAME = "diff_preview"


def highlight_diff_body(payload: "dict") -> List[str]:
    """Syntax-highlight a unified diff. THE WORKER — runs in a child process.

    Module-level and taking plain data, because `ProcessPoolExecutor` pickles both
    the callable and its arguments: the obvious `self._render_blocking` cannot
    cross a process boundary at all — it is bound to a controller holding a lock,
    an archive and callbacks, none of which pickle.

    Ships ONLY the expensive half. `asyncio.to_thread` was never wrong about
    wanting this off the loop; it was wrong that a thread achieves it, because
    Pygments is pure Python and holds the GIL for the whole lex. Measured: ~480ms
    of lexing that stalls the loop under load, against 155ms for a child to import
    rich+pygments ONCE and 14ms for this module — so a persistent worker amortises
    its startup after a single render, while a per-render process would cost more
    than the bug.

    The header and the file tree stay in the PARENT deliberately. They are cheap
    (string formatting and one `_build_file_tree` call) and the tree reaches into
    `diff_preview`, which would drag the ouroboros tree into the child and turn a
    155ms warm-up into something far worse. Moving only what is expensive is what
    keeps the child's import surface to `rich`.

    NEVER raises across the boundary: an exception in a worker arrives as a
    `BrokenProcessPool` or an unpicklable traceback at the caller, so failure is
    reported as CONTENT the operator can read instead.
    """
    try:
        from io import StringIO

        from rich.console import Console
        from rich.syntax import Syntax

        text = str(payload.get("diff_text") or "")
        width = int(payload.get("width") or 100)
        if not text.strip():
            return ["    (no diff text archived for this ref)"]
        buf = StringIO()
        Console(file=buf, force_terminal=True, color_system="truecolor",
                width=max(20, width - 4), highlight=False,
                emoji=True).print(
            Syntax(text, "diff", theme="ansi_dark", word_wrap=False,
                   background_color="default"))
        return buf.getvalue().rstrip("\n").split("\n")
    except Exception:  # noqa: BLE001
        # Plain text beats nothing: an operator wanting to read a diff is not
        # served by a highlighter's absence.
        return [f"    {ln}" for ln in
                str(payload.get("diff_text") or "").splitlines()]


_POOL: Any = None
_POOL_LOCK = threading.RLock()
_POOL_BROKEN = False


def _render_pool() -> Any:
    """The persistent highlight pool, or None. NEVER raises.

    ONE worker, not a fleet: renders are serialised by the epoch guard anyway (only
    the newest matters), so extra workers would buy nothing and cost a 155ms import
    each. A single child also keeps the shutdown story simple, which matters because
    `graceful_preemption.halt_child_workers` already SIGTERMs this process's
    children at teardown — this pool must be something that path can kill, not a
    second lifecycle competing with it.

    Once broken, STAYS broken. A pool whose worker was halted raises
    `BrokenProcessPool` on every submit, and retrying per render would pay the
    failure cost forever; the thread fallback is correct from then on.
    """
    global _POOL, _POOL_BROKEN
    if _POOL_BROKEN:
        return None
    with _POOL_LOCK:
        if _POOL_BROKEN:
            return None
        if _POOL is None:
            try:
                from concurrent.futures import ProcessPoolExecutor
                _POOL = ProcessPoolExecutor(max_workers=1)
            except Exception:  # noqa: BLE001 — sandbox, frozen app, no spawn
                logger.debug("[DiffOverlay] render pool unavailable",
                             exc_info=True)
                _POOL_BROKEN = True
                return None
        return _POOL


def shutdown_render_pool(*, wait: bool = False) -> bool:
    """Release the worker. Idempotent. NEVER raises.

    Registered with `atexit` so a cockpit that exits normally does not leave a
    child behind, and callable by name so the existing preemption path can release
    it deliberately rather than having to discover it.

    ``wait=False`` by default: shutdown runs on the operator's exit path, and a
    render in flight is worth abandoning rather than waiting on.
    """
    global _POOL, _POOL_BROKEN
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
        _POOL_BROKEN = True
    if pool is None:
        return False
    try:
        pool.shutdown(wait=wait, cancel_futures=True)
    except TypeError:            # cancel_futures is 3.9+
        try:
            pool.shutdown(wait=wait)
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


try:
    import atexit as _atexit
    _atexit.register(shutdown_render_pool)
except Exception:  # noqa: BLE001
    pass


def reset_render_pool_for_tests() -> None:
    """Forget that the pool was ever retired. NEVER raises.

    `shutdown_render_pool` sets a module-global broken flag ON PURPOSE — a halted
    worker must not be retried per render — but that flag then outlives a single
    test and silently pushes every later one onto the thread path. Which is not
    hypothetical: it made the #70283 stall assertion fail depending on test ORDER,
    reporting a regression in code that had not changed.

    Exposed here rather than reached into from a test, so the reset and the flag
    that needs resetting stay in one module.
    """
    global _POOL, _POOL_BROKEN
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
        _POOL_BROKEN = False
    if pool is not None:
        try:
            pool.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


def _terminal_width(fallback: int = 100) -> int:
    """Resolved per call — a diff wrapped to a stale width is clipped, not
    reflowed, because the canvas draws with ``wrap_lines=False``."""
    try:
        import shutil
        return max(40, int(shutil.get_terminal_size((fallback, 30)).columns))
    except Exception:  # noqa: BLE001
        return fallback


class DiffOverlayController:
    """Owns "which diff is on screen, and what does it look like".

    Deliberately NOT a renderer. It holds a ref, an epoch and a list of lines;
    the drawing is `DiffPreviewRenderer`'s and the mounting is
    `bipartite_layout`'s. Three jobs, three owners — the reason a regression in
    the gate's own preview surfaces here too instead of in a parallel drawing
    that agrees with itself.
    """

    __slots__ = ("_archive", "_invalidate", "_width_fn", "_lock", "_ref",
                 "_rows", "_epoch", "_loading")

    def __init__(
        self,
        *,
        archive: Any,
        invalidate: Optional[Callable[[], None]] = None,
        width_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self._archive = archive
        self._invalidate = invalidate
        self._width_fn = width_fn or _terminal_width
        self._lock = threading.RLock()
        self._ref: Optional[str] = None
        self._rows: List[str] = []
        self._epoch = 0
        self._loading = False

    # -- overlay_arbiter contract ---------------------------------------

    def is_active(self) -> bool:
        """Is the overlay on screen? True from the instant it is opened.

        Deliberately NOT "are there rows yet". The overlay is up while it loads —
        that is what makes `Escape` close it during a slow render, instead of the
        operator pressing it into a void and getting the rewind menu.
        """
        with self._lock:
            return self._ref is not None

    def dismiss(self) -> None:
        """Close it, and orphan any render still in flight. NEVER raises."""
        with self._lock:
            self._ref = None
            self._rows = []
            self._loading = False
            # Bumped so a task that finishes after this cannot resurrect us.
            self._epoch += 1
        self._repaint()

    def rows(self) -> List[str]:
        """The lines to draw. O(1), never blocks, never renders. NEVER raises."""
        with self._lock:
            return list(self._rows)

    # -- opening --------------------------------------------------------

    def open(self, ref: Optional[str] = None) -> bool:
        """Show a diff. Returns whether the overlay is now up. NEVER raises.

        Resolution happens HERE, synchronously, because it is a dict lookup and
        an operator who typed an unknown ref deserves an immediate answer rather
        than a placeholder that later turns into an error.

        ``ref=None`` means the most recent archived diff — the overwhelmingly
        common intent, and it saves an operator reading a ref off the screen only
        to type it back.
        """
        try:
            entry = self._resolve(ref)
            if entry is None:
                with self._lock:
                    self._ref = str(ref or "d-?")
                    self._rows = self._not_found_rows(ref)
                    self._loading = False
                    self._epoch += 1
                self._repaint()
                return True
            with self._lock:
                self._epoch += 1
                epoch = self._epoch
                self._ref = str(getattr(entry, "ref", "") or "")
                self._loading = True
                self._rows = self._loading_rows(self._ref, entry)
            self._repaint()
            self._schedule(entry, epoch)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] open degraded", exc_info=True)
            return False

    def _resolve(self, ref: Optional[str]) -> Any:
        """``d-N`` → entry, or the newest when no ref was named."""
        try:
            if ref:
                text = str(ref).strip()
                found = self._archive.lookup(text)
                if found is not None:
                    return found
                # An operator typing `3` for `d-3` is being reasonable; the
                # prefix is display grammar, not something they should have to
                # remember. Tried only AFTER the literal, so a future ref shape
                # that happens to be numeric is never shadowed.
                if not text.startswith("d-"):
                    return self._archive.lookup(f"d-{text.lstrip('d-')}")
                return None
            recent = list(self._archive.list_recent() or [])
            return recent[0] if recent else None
        except Exception:  # noqa: BLE001
            return None

    def _schedule(self, entry: Any, epoch: int) -> None:
        """Render off the loop when there is a loop; inline when there is not.

        Headless — a test, a CI run, `ov demo` composing a transcript — has no
        frame budget to protect and no loop to schedule on. Rendering inline
        there is not a fallback so much as the correct answer for that context,
        and it keeps this class usable without an event loop at all.
        """
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None:
                self._absorb(epoch, self._render_blocking(
                    entry, self._width_fn()))
                return
            loop.create_task(self._load(entry, epoch))
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] schedule degraded", exc_info=True)

    async def _load(self, entry: Any, epoch: int) -> None:
        """The off-loop render. NEVER raises out — it is a bare task.

        `asyncio.to_thread` rather than a custom executor: the work is
        CPU-and-subprocess bound, releases the GIL inside Pygments and git, and
        the default thread pool is already the loop's. A private pool would be a
        second answer to a question asyncio answers.
        """
        try:
            import asyncio
            width = self._width_fn()
            # Cheap halves stay here: header formatting and one `_build_file_tree`
            # call. Shipping them would drag `diff_preview` into the child and turn
            # a 155ms warm-up into something far worse.
            rows = list(self._header_rows(entry))
            rows.extend(self._tree_rows(entry, width))
            payload = {
                "diff_text": str(getattr(entry, "diff_text", "") or ""),
                "width": width,
            }
            body: List[str] = []
            # Constructed OFF the loop. `ProcessPoolExecutor(...)` spawns a child
            # synchronously, and doing that inline stalled the loop for ~154ms on
            # the very first render — the pool paying for itself out of the frame
            # budget it exists to protect. Warming it in a thread is safe because
            # the construction releases the GIL in the OS spawn, unlike the Pygments
            # work this is all for.
            pool = await asyncio.to_thread(_render_pool)
            if pool is not None:
                try:
                    loop = asyncio.get_running_loop()
                    body = await loop.run_in_executor(
                        pool, highlight_diff_body, payload)
                except Exception:  # noqa: BLE001
                    # BrokenProcessPool (a halted worker), a pickling failure, a
                    # sandbox that forbids spawn — every one of them means the
                    # child is not available NOW, and the operator still wants the
                    # diff. Retire the pool and fall through to the thread.
                    logger.debug("[DiffOverlay] render pool failed; "
                                 "falling back to a thread", exc_info=True)
                    shutdown_render_pool()
                    body = []
            if not body:
                # The thread still holds the GIL — this is the DEGRADED path, kept
                # because losing the diff is worse than a stalled frame, not because
                # a thread was ever adequate.
                body = await asyncio.to_thread(highlight_diff_body, payload)
            rows.extend(body)
            self._absorb(epoch, rows)
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] async render degraded", exc_info=True)
            self._absorb(epoch, ["  diff preview unavailable"])

    def _absorb(self, epoch: int, rows: List[str]) -> None:
        """Accept a render ONLY if it is still the one being waited for."""
        with self._lock:
            if epoch != self._epoch or self._ref is None:
                # Superseded by a newer open, or dismissed while we rendered.
                return
            self._rows = list(rows)
            self._loading = False
        self._repaint()

    # -- rendering (runs on a worker thread) ----------------------------

    def _render_blocking(self, entry: Any, width: int) -> List[str]:
        """Entry → lines. BLOCKING and CPU-heavy by nature. NEVER raises.

        Renders the archive's ``diff_text`` DIRECTLY rather than reconstructing
        `FileChange` old/new content to hand to `DiffPreviewRenderer.build`. The
        archive stores a unified diff; rebuilding both sides so a renderer can
        re-diff them is a lossy round-trip that can only lose fidelity it cannot
        add. The file TREE is still the real `_build_file_tree`, so the reach
        gutter and the tree grammar stay shared with the gate's own preview.
        """
        out: List[str] = []
        try:
            out.extend(self._header_rows(entry))
            out.extend(self._tree_rows(entry, width))
            out.extend(self._diff_rows(entry, width))
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] render degraded", exc_info=True)
            if not out:
                out = ["  diff preview unavailable"]
        return out

    def _header_rows(self, entry: Any) -> List[str]:
        ref = str(getattr(entry, "ref", "") or "d-?")
        op_id = str(getattr(entry, "op_id", "") or "?")
        tier = str(getattr(entry, "risk_tier", "") or "")
        summary = str(getattr(entry, "summary", "") or "")
        head = f"  ◇ {ref} · {op_id}"
        if tier:
            head += f" · {tier}"
        rows = [head]
        if summary:
            rows.append(f"    {summary}")
        # Outcome is the question an operator opening an ARCHIVED diff is
        # actually asking — "did this land?" — and it is the one thing a
        # transient gate preview could never tell them.
        apply_outcome = str(getattr(entry, "apply_outcome", "") or "")
        verify = str(getattr(entry, "verify_outcome", "") or "")
        if apply_outcome or verify:
            rows.append(f"    apply {apply_outcome or '?'} · "
                        f"verify {verify or '?'}")
        branch = str(getattr(entry, "review_branch", "") or "")
        if branch:
            rows.append(f"    branch {branch}")
        rows.append("")
        return rows

    def _tree_rows(self, entry: Any, width: int) -> List[str]:
        """The file tree WITH its reach gutter, from the gate's own renderer."""
        try:
            from backend.core.ouroboros.battle_test.diff_preview import (
                DiffPreviewRenderer, FileChange,
            )
            paths = list(getattr(entry, "file_paths", ()) or ())
            if not paths:
                return []
            changes = [FileChange(path=str(p)) for p in paths]
            tree = DiffPreviewRenderer()._build_file_tree(changes)
            return self._to_lines(tree, width)
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] tree degraded", exc_info=True)
            return []

    def _diff_rows(self, entry: Any, width: int) -> List[str]:
        """The diff body, syntax-highlighted. The expensive half."""
        # Through the same module-level worker the pool calls, so the in-process
        # path and the child path cannot drift into two different renderings.
        return highlight_diff_body({
            "diff_text": str(getattr(entry, "diff_text", "") or ""),
            "width": width,
        })

    def _diff_rows_unused(self, entry: Any, width: int) -> List[str]:
        text = str(getattr(entry, "diff_text", "") or "")
        if not text.strip():
            return ["    (no diff text archived for this ref)"]
        try:
            from rich.syntax import Syntax
            return self._to_lines(
                Syntax(text, "diff", theme="ansi_dark", word_wrap=False,
                       background_color="default"),
                width,
            )
        except Exception:  # noqa: BLE001
            # Plain text beats nothing: an operator wanting to read a diff is
            # not served by a highlighter's absence.
            return [f"    {ln}" for ln in text.splitlines()]

    def _to_lines(self, renderable: Any, width: int) -> List[str]:
        """Rich renderable → ANSI lines, through a headless console.

        The SAME shape `BipartiteLayout._render_to_ansi` uses, so the overlay and
        the deck cannot disagree about how a Rich object becomes terminal text.
        """
        try:
            from io import StringIO

            from rich.console import Console
            buf = StringIO()
            Console(file=buf, force_terminal=True, color_system="truecolor",
                    width=max(20, width - 4), highlight=False,
                    emoji=True).print(renderable)
            return buf.getvalue().rstrip("\n").split("\n")
        except Exception:  # noqa: BLE001
            return []

    # -- helpers --------------------------------------------------------

    def _loading_rows(self, ref: str, entry: Any) -> List[str]:
        """What is on screen while the render runs.

        Names the ref and the file count, both free, so the placeholder confirms
        the operator opened what they meant even before the diff arrives.
        """
        count = len(list(getattr(entry, "file_paths", ()) or ()))
        files = f"{count} file{'s' if count != 1 else ''}" if count else "…"
        return [f"  ◇ {ref} · {files} · rendering…", ""]

    def _not_found_rows(self, ref: Optional[str]) -> List[str]:
        """An unknown ref says so, and says what IS available.

        The archive is a ring, so a ref an operator read minutes ago may have
        been evicted. "No such diff" alone invites them to doubt their typing;
        listing the live refs answers the real question.
        """
        try:
            available = list(self._archive.all_refs() or ())
        except Exception:  # noqa: BLE001
            available = []
        rows = [f"  ◇ no diff archived as {ref or '(latest)'}"]
        if available:
            rows.append(f"    available: {' '.join(str(r) for r in available[-8:])}")
        else:
            rows.append("    the archive is empty — no candidate has been "
                        "previewed yet")
        rows.append("")
        return rows

    def _repaint(self) -> None:
        try:
            if self._invalidate is not None:
                self._invalidate()
        except Exception:  # noqa: BLE001
            pass

    # -- registration ---------------------------------------------------

    def register(self) -> bool:
        """Declare this overlay to `overlay_arbiter`. NEVER raises.

        Below the Iron Gate and the panic: a preview the operator opened
        themselves is the least urgent of the three, so `Escape` closes a crash
        or a pending decision first. Registered rather than hardcoded into the
        arbiter's filter, which is what lets `Escape` become contextually eager
        without the arbiter knowing this class exists.
        """
        try:
            from backend.core.ouroboros.battle_test.overlay_arbiter import (
                Z_DIFF_PREVIEW, register_overlay,
            )
            return register_overlay(
                OVERLAY_NAME, z=Z_DIFF_PREVIEW,
                is_active=self.is_active, dismiss=self.dismiss,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[DiffOverlay] registration degraded", exc_info=True)
            return False


_default_controller: Optional[DiffOverlayController] = None
_singleton_lock = threading.RLock()


def get_default_controller() -> DiffOverlayController:
    """The process-wide controller, constructed lazily. NEVER raises.

    A singleton for the same reason `get_default_archive` is one: the verb that
    OPENS a diff (`/expand d-N`, in the REPL) and the hook that DRAWS it
    (`diff_rows`, in the layout) run in different call stacks and must be talking
    about the same overlay. Two instances would give a verb that reports success
    while the mounted surface never changes — the wired-but-inert shape, arrived
    at from a third direction.

    Bound to `get_default_archive()` rather than taking one, so the controller and
    the archive cannot drift apart: there is exactly one archive per process and
    exactly one overlay reading it.

    Registers with `overlay_arbiter` on FIRST construction so `Escape` closes it
    without any surface remembering to ask. Idempotent — registration is by name.
    """
    global _default_controller
    with _singleton_lock:
        if _default_controller is None:
            from backend.core.ouroboros.battle_test.diff_archive import (
                get_default_archive,
            )
            _default_controller = DiffOverlayController(
                archive=get_default_archive(),
            )
            _default_controller.register()
        return _default_controller


def reset_default_controller_for_tests() -> None:
    global _default_controller
    with _singleton_lock:
        _default_controller = None


def bind_invalidate(invalidate: Callable[[], None]) -> bool:
    """Point the default controller's repaint at a live cockpit. NEVER raises.

    The controller is built before any Application exists — a verb may open a diff
    on a surface that has not mounted yet — so the repaint hook is attached when
    the cockpit appears rather than passed at construction. Without it the rows
    land correctly and nothing redraws until the next unrelated frame, which reads
    as the overlay taking seconds to open.
    """
    try:
        controller = get_default_controller()
        controller._invalidate = invalidate  # noqa: SLF001 — its own module
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "DIFF_OVERLAY_SCHEMA_VERSION",
    "OVERLAY_NAME",
    "DiffOverlayController",
    "bind_invalidate",
    "get_default_controller",
    "reset_default_controller_for_tests",
]
