"""The arbiter's decision, made into containers.

`viewport_arbiter` answers "which regions fit this terminal, and how" and
holds no widgets on purpose — the whole value of that separation is being
provable at every width without a terminal. This is the other side: the
prompt_toolkit consumer that turns those placements into a real container
tree.

Why this could not be a port
-----------------------------
`split_layout` already renders three regions — in **Rich**. A `rich.Layout`
cannot mount inside a `prompt_toolkit.Application`; they are different
rendering models with different ownership of the screen. So the cockpit's
three-region surface was never "wiring up dead code", and calling it that
was wrong: the Rich implementation belongs to SerpentFlow's own console and
stays there. `LayoutController` — the mode FSM — IS toolkit-agnostic and is
reused unchanged.

The layout is a FUNCTION, not a structure
------------------------------------------
prompt_toolkit builds a container tree once, at Application construction.
The arbiter's answer changes with every resize. Rebuilding the tree and
reassigning `app.layout.container` on each SIGWINCH is the obvious bridge and
it is wrong twice over: it discards focus and scroll state that live on the
widgets, and it races the renderer, which may be midway through a frame that
references the tree being replaced.

`DynamicContainer` resolves it properly — it calls a factory on every render,
so the tree is derived rather than mutated. Width flows in, containers flow
out, and nothing is reassigned. A resize changes what the next frame draws
and touches nothing that a frame in flight is holding.

Floats over splits
------------------
A FLOAT placement draws over the deck rather than beside it, using the same
`FloatContainer` the `/` palette established. One Z-index architecture: a
second overlay mechanism would eventually disagree with the first about what
draws on top, and the operator would meet that disagreement as a menu
rendered underneath a panel.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.RegionLayout")

__all__ = ["build_region_tree", "region_layout_enabled", "RegionSources"]


def region_layout_enabled() -> bool:
    """Default ON. Off, the cockpit keeps its single-canvas layout."""
    return os.environ.get(
        "JARVIS_REGION_LAYOUT_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class RegionSources:
    """Where each region's content comes from.

    Callables, not containers: a region that is HIDDEN this frame must cost
    nothing, and building its widget anyway would defeat the arbiter's whole
    purpose of not drawing what does not fit.
    """

    def __init__(self, **factories: Callable[[], Any]) -> None:
        self._factories: Dict[str, Callable[[], Any]] = dict(factories)

    def get(self, region: str) -> Optional[Any]:
        """Build one region's container, or None. NEVER raises.

        A region whose factory fails is DROPPED rather than allowed to
        propagate: one broken panel must not take the cockpit down with it,
        and the arbiter has already guaranteed the deck survives.
        """
        try:
            factory = self._factories.get(str(region))
            return factory() if factory is not None else None
        except Exception:  # noqa: BLE001
            logger.debug("[RegionLayout] region %r failed to build", region,
                         exc_info=True)
            return None

    @property
    def names(self) -> List[str]:
        return list(self._factories)


def _dimension(cols: int) -> Any:
    """Exact width for a split column.

    EXACT, for the reason the prompt learned the hard way: a ranged dimension
    advertises willingness to absorb slack, and `VSplit` distributes leftover
    by weight to whichever child will take it. The arbiter already decided
    these widths; a range would let the layout quietly overrule it.
    """
    from prompt_toolkit.layout.dimension import Dimension
    return Dimension.exact(max(1, int(cols)))


def build_region_tree(
    placements: Sequence[Any],
    sources: RegionSources,
    *,
    prompt: Optional[Any] = None,
    extra_floats: Optional[Sequence[Any]] = None,
) -> Any:
    """One frame's container tree. NEVER raises.

    Called from a `DynamicContainer` factory, so it runs on every render and
    must stay cheap and total: an exception here is a blank cockpit, and a
    slow path here is a slow cockpit.
    """
    try:
        from prompt_toolkit.layout.containers import (
            Float, FloatContainer, HSplit, VSplit, Window,
        )

        columns: List[Any] = []
        floats: List[Any] = list(extra_floats or ())

        for placement in placements or ():
            kind = getattr(placement, "placement", "")
            region = getattr(placement, "region", "")
            if kind == "hidden":
                continue
            content = sources.get(region)
            if content is None:
                continue
            if kind == "float":
                # Over the deck, using the SAME FloatContainer the palette
                # established. A second overlay mechanism would eventually
                # disagree with the first about what draws on top.
                floats.append(Float(
                    content=content, top=1, right=2,
                    width=_dimension(getattr(placement, "cols", 0) or 40),
                ))
                continue
            # Width is applied by WRAPPING rather than set on the child, so a
            # caller's container keeps whatever internal layout it has.
            columns.append(_sized(content, getattr(placement, "cols", 0)))

        if not columns:
            # The arbiter guarantees the deck is never demoted below FLOAT,
            # so an empty column list means every region floated. Something
            # still has to own the screen underneath them.
            columns = [Window()]

        body: Any = columns[0] if len(columns) == 1 else VSplit(
            columns, padding=1, padding_char="│",
        )
        rows: List[Any] = [body]
        if prompt is not None:
            rows.append(prompt)
        root: Any = HSplit(rows)
        if floats:
            root = FloatContainer(content=root, floats=floats)
        return root
    except Exception:  # noqa: BLE001 — a render must never raise
        logger.debug("[RegionLayout] build degraded", exc_info=True)
        from prompt_toolkit.layout.containers import Window
        return Window()


def _sized(container: Any, cols: int) -> Any:
    """Pin a column's width without disturbing its internals."""
    try:
        from prompt_toolkit.layout.containers import VSplit
        if not cols:
            return container
        return VSplit([container], width=_dimension(cols))
    except Exception:  # noqa: BLE001
        return container


def dynamic_region_container(
    arbiter: Any,
    sources: RegionSources,
    *,
    prompt: Optional[Any] = None,
    size: Optional[Callable[[], Any]] = None,
) -> Any:
    """A container that re-derives its tree on every render. NEVER raises.

    This is the seam that makes the whole design work. prompt_toolkit builds
    a tree once; the arbiter's answer changes with every resize. Rebuilding
    and reassigning `app.layout.container` would discard focus and scroll
    state living on the widgets, and would race a renderer that may be midway
    through a frame referencing the tree being replaced.

    A factory has neither problem: width flows in, containers flow out, and
    nothing is mutated.
    """
    from prompt_toolkit.layout.containers import DynamicContainer

    def _current_size() -> Any:
        if size is not None:
            return size()
        from prompt_toolkit.application.current import get_app
        return get_app().output.get_size()

    def _factory() -> Any:
        try:
            if not region_layout_enabled():
                return sources.get("deck") or _blank()
            dims = _current_size()
            cols = int(getattr(dims, "columns", 0) or 0)
            rows = int(getattr(dims, "rows", 0) or 0)
            return build_region_tree(
                arbiter.arbitrate(cols, rows), sources, prompt=prompt,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[RegionLayout] factory degraded", exc_info=True)
            return sources.get("deck") or _blank()

    return DynamicContainer(_factory)


def _blank() -> Any:
    from prompt_toolkit.layout.containers import Window
    return Window()
