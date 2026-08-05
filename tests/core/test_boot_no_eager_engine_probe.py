"""Boot must not probe the optimization engines on the event loop.

`OptimizationRouter.discover_engines()` probes each engine by IMPORTING it —
`import torch`, `import onnxruntime`, and for the JIT probe it builds and
traces a small `nn.Module`. Captured by `stall_sampler` as the worst remaining
boot blocker at 14.24s, running inside an `async def` with nothing to yield to.

It never needed to be there:

  * it caches (``if self._discovered: return self._engine_cache``)
  * it is ALREADY called on demand from the router's own routing methods

so the eager call bought nothing. The models were never the issue — Ghost
Proxies already made registration instant. Only the CAPABILITY PROBE was eager.

STRUCTURAL, NOT TIMED, ON PURPOSE
-----------------------------------
The obvious test — "assert boot is faster" — cannot be written honestly here.
Measured on this machine, two runs of IDENTICAL code produced 120s/58.16s
stalled and 68s/27.93s stalled: a 2x spread, because the machine is at HIGH
memory pressure and every run competes with whatever else is resident. A
timing assertion would pass or fail on load, not on code, and would eventually
be "fixed" by loosening the threshold until it asserted nothing.

So this asserts the STRUCTURE that makes the stall impossible: no synchronous
`discover_engines()` on the boot path. That claim is decidable from the source
and stays true regardless of what the machine is doing.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INITIALIZER = REPO / "backend" / "core" / "parallel_initializer.py"


def _init_ai_loader_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(INITIALIZER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_init_ai_loader":
            return node
    raise AssertionError("_init_ai_loader not found — has it been renamed?")


def _calls_in(node: ast.AST) -> list:
    """(func_name, lineno) for every call, excluding nested function bodies.

    Nested functions are excluded deliberately: the fix moves the probe into a
    coroutine that is scheduled, not awaited, so a call appearing THERE is the
    correct outcome rather than a violation.
    """
    out = []
    nested = {n for fn in ast.walk(node)
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn is not node
              for n in ast.walk(fn)}
    for n in ast.walk(node):
        if n in nested or not isinstance(n, ast.Call):
            continue
        f = n.func
        name = getattr(f, "attr", None) or getattr(f, "id", None)
        if name:
            out.append((name, n.lineno))
    return out


def test_boot_does_not_probe_engines_synchronously():
    """The 14.24s blocker, asserted out of existence."""
    calls = _calls_in(_init_ai_loader_node())
    offenders = [(n, ln) for n, ln in calls if n == "discover_engines"]
    assert not offenders, (
        "_init_ai_loader calls discover_engines() directly on the boot path at "
        f"line(s) {[ln for _, ln in offenders]}. That probe imports torch and "
        "onnxruntime and traces an nn.Module — 14.24s on the event loop, with "
        "nothing to yield to. It caches and is already called on demand; the "
        "eager call buys nothing."
    )


def test_the_router_is_still_published_immediately():
    """Deferring the probe must not defer the ROUTER. Callers read
    `app.state.optimization_router`; making them wait for a torch import would
    trade one stall for a different one."""
    body = INITIALIZER.read_text(encoding="utf-8")
    node = _init_ai_loader_node()
    seg = "\n".join(body.split("\n")[node.lineno - 1:node.end_lineno])
    assert "optimization_router" in seg, "the router is no longer published"
    assert "ai_loader_ready" in seg, "readiness flag is no longer set"


def test_the_deferred_probe_is_owned_by_shutdown():
    """An import warming a process it outlives is the unmanaged-task class that
    made shutdown unkillable. The deferred probe must go through the managed
    factory, not a bare create_task."""
    node = _init_ai_loader_node()
    body = INITIALIZER.read_text(encoding="utf-8")
    seg = "\n".join(body.split("\n")[node.lineno - 1:node.end_lineno])
    assert "spawn_managed_task" in seg, (
        "the deferred engine probe is not registered with a shutdown phase")


def test_discover_engines_still_caches():
    """The whole argument for deferring rests on this. If the probe stopped
    caching, deferring it would move a repeated cost rather than remove one."""
    ai_loader = REPO / "backend" / "core" / "ai_loader.py"
    tree = ast.parse(ai_loader.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "discover_engines":
            seg = ast.dump(node)
            assert "_discovered" in seg and "_engine_cache" in seg, (
                "discover_engines no longer short-circuits on a cache — the "
                "premise for deferring it no longer holds")
            return
    raise AssertionError("discover_engines not found")
