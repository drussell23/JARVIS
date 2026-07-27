"""Test doubles that cannot drift from the functions they stand in for.

`test_attaches_when_the_incumbent_simply_finishes_booting` failed for 24
seconds and looked exactly like IPC contention with a live daemon. It was a
fake returning `state["n"] > 2` — a bool — where the real ``probe_socket``
returns a classification string, so the production comparison ``== "live"``
could never be true.

Nothing could catch that. A fake IS the boundary in a unit test, so a fake
that lies about the contract makes the test agree with it. The failure surfaces
somewhere else entirely, wearing a disguise, and the more plausible the
disguise the more expensive it gets — that one cost a hermetic-socket-sandbox
design before the type mismatch was spotted.

The fix has to be structural. "Remember to update the fakes" is a policy
whose enforcement mechanism is human memory, which is the thing that already
failed.

    probe = contract_fake(tc.probe_socket, lambda _p, **_k: "live")
    monkeypatch.setattr(tc, "probe_socket", probe)

The wrapper checks what the fake RETURNS against the real function's declared
return type, at call time, and raises ``ContractMismatchError`` the moment
they disagree.

Checked at call time, not at wrap time
--------------------------------------
A fake is usually a lambda or a closure with no annotations of its own, so
there is nothing to inspect statically — and a fake whose return type varies
by call (the retry-then-succeed shape this very bug lived in) is exactly the
kind worth checking on every call rather than once.

Deliberately not autospec alone
-------------------------------
``create_autospec(spec_set=True)`` enforces the SIGNATURE — how a double is
called — and that is genuinely useful, so it is offered here too. It does not
constrain what a double RETURNS, which is the half that broke. The two are
complementary, and only one of them was missing.
"""
from __future__ import annotations

import functools
import inspect
import typing
from typing import Any, Callable, Optional

__all__ = [
    "ContractMismatchError",
    "autospec_fake",
    "contract_fake",
    "returns_match",
]


class ContractMismatchError(TypeError):
    """A test double returned something the real function cannot return."""


def _declared_return(real: Any) -> Any:
    """The real function's annotated return type, or None if it has none."""
    try:
        target = inspect.unwrap(real)
        hints = typing.get_type_hints(target)
        return hints.get("return")
    except Exception:  # noqa: BLE001 — an unannotated target is not an error
        return None


def returns_match(value: Any, declared: Any) -> bool:
    """Is *value* an acceptable instance of the *declared* return type?

    Handles the shapes that actually appear on these boundaries: ``Any``,
    ``None``, ``Optional[X]`` / unions, and plain classes. Anything exotic
    (generics with parameters, protocols) is accepted rather than guessed at —
    a contract checker that produces false alarms gets switched off, and then
    it protects nothing.
    """
    try:
        if declared is None or declared is Any or declared is inspect.Signature.empty:
            return True
        if declared is type(None):
            return value is None

        origin = typing.get_origin(declared)
        if origin is typing.Union:
            return any(returns_match(value, arg)
                       for arg in typing.get_args(declared))
        if origin is not None:
            # A parameterised generic (List[str], Dict[str, int], ...):
            # check the container, not its contents. Element-wise checking
            # would mean walking arbitrarily large structures on every call.
            return isinstance(value, origin) if inspect.isclass(origin) else True

        if not inspect.isclass(declared):
            return True

        # bool is a subclass of int, so `Optional[int]` would silently accept
        # True. That is precisely the confusion this module exists to catch,
        # so the two are treated as distinct.
        if declared is int and isinstance(value, bool):
            return False
        return isinstance(value, declared)
    except Exception:  # noqa: BLE001
        return True


def _describe(declared: Any) -> str:
    return getattr(declared, "__name__", None) or str(declared)


def contract_fake(real: Callable[..., Any], fake: Callable[..., Any]) -> Any:
    """Wrap *fake* so it must return what *real* declares it returns.

    Preserves async-ness: an async real function yields an async wrapper, so
    the double still awaits the way production does. Returns *fake* unchanged
    when the real function carries no return annotation — there is nothing to
    enforce, and pretending otherwise would give false confidence.
    """
    declared = _declared_return(real)
    if declared is None:
        return fake

    name = getattr(real, "__qualname__", getattr(real, "__name__", repr(real)))

    def _check(value: Any) -> Any:
        if not returns_match(value, declared):
            raise ContractMismatchError(
                f"fake for {name}() returned {value!r} "
                f"({type(value).__name__}), but {name}() is declared to "
                f"return {_describe(declared)}. The production code branches "
                f"on that type — a double that disagrees makes the test agree "
                f"with the double instead of with the system."
            )
        return value

    if inspect.iscoroutinefunction(real):
        @functools.wraps(fake)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = fake(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return _check(result)
        return _async_wrapper

    @functools.wraps(fake)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        return _check(fake(*args, **kwargs))
    return _wrapper


def autospec_fake(
    real: Callable[..., Any], side_effect: Optional[Callable[..., Any]] = None,
) -> Any:
    """``create_autospec(spec_set=True)`` PLUS return-type enforcement.

    Signature enforcement and return enforcement catch different halves of
    mock drift — being called wrongly, and answering wrongly — and this bug
    was the second. Composing them here means a call site gets both from one
    helper instead of remembering to apply two.
    """
    from unittest.mock import create_autospec

    spec = create_autospec(real, spec_set=True)
    if side_effect is not None:
        spec.side_effect = contract_fake(real, side_effect)
    return spec
