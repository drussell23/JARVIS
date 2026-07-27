"""Mock drift is caught by the machine, not by remembering.

The bug this closes cost a 24-second timeout, a wrong diagnosis (IPC bleed
from a live daemon), and a designed-but-unbuilt hermetic socket sandbox —
before anyone noticed the fake returned a bool where the real
``probe_socket`` returns a classification string.

Nothing could have caught it. A fake IS the boundary in a unit test, so a
fake that lies makes the test agree with the lie; the failure surfaces
elsewhere wearing a disguise.

"Remember to update the fakes" is a policy enforced by human memory, which is
the thing that already failed. These tests pin the enforcement instead.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from tests.contract_fakes import (
    ContractMismatchError,
    autospec_fake,
    contract_fake,
    returns_match,
)


# --------------------------------------------------------------------------
# 1. the exact historical bug
# --------------------------------------------------------------------------

async def test_the_probe_socket_drift_is_caught() -> None:
    """MANDATE 4, against the real production signature.

    ``probe_socket`` is annotated ``-> str`` and the caller compares
    ``== "live"``. A bool can never satisfy that, and this is where it stops.
    """
    from backend.core.ouroboros.cli import thin_client as tc

    state = {"n": 0}

    def _drifted(_p: Any, **_k: Any) -> Any:
        state["n"] += 1
        return state["n"] > 2          # the original defect, verbatim

    probe = contract_fake(tc.probe_socket, _drifted)
    with pytest.raises(ContractMismatchError) as caught:
        await probe("/tmp/x.sock", deep=True)
    assert "probe_socket" in str(caught.value)
    assert "str" in str(caught.value)


async def test_the_corrected_fake_passes_unobstructed() -> None:
    """The fix that made the suite green must not now be blocked."""
    from backend.core.ouroboros.cli import thin_client as tc

    state = {"n": 0}

    def _correct(_p: Any, **_k: Any) -> Any:
        state["n"] += 1
        return "live" if state["n"] > 2 else "stale"

    probe = contract_fake(tc.probe_socket, _correct)
    assert await probe("/tmp/x.sock") == "stale"
    assert await probe("/tmp/x.sock") == "stale"
    assert await probe("/tmp/x.sock") == "live"


def test_optional_int_boundaries_are_enforced() -> None:
    """``_live_incumbent -> Optional[int]``: a PID or nothing."""
    from backend.core.ouroboros.cli import thin_client as tc

    assert contract_fake(tc._live_incumbent, lambda: 999)() == 999
    assert contract_fake(tc._live_incumbent, lambda: None)() is None
    with pytest.raises(ContractMismatchError):
        contract_fake(tc._live_incumbent, lambda: "999")()


# --------------------------------------------------------------------------
# 2. the type rules
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,declared,ok", [
    ("live", str, True),
    (True, str, False),
    (1, str, False),
    (None, Optional[int], True),
    (5, Optional[int], True),
    ("5", Optional[int], False),
    ([], List[str], True),
    ({}, Dict[str, int], True),
    ("x", Any, True),
    (None, None, True),
])
def test_the_matcher(value: Any, declared: Any, ok: bool) -> None:
    assert returns_match(value, declared) is ok


def test_a_bool_is_not_accepted_as_an_int() -> None:
    """``bool`` IS a subclass of ``int`` in Python, so ``Optional[int]`` would
    silently accept ``True`` — precisely the confusion this exists to catch."""
    assert returns_match(True, int) is False
    assert returns_match(True, Optional[int]) is False
    assert returns_match(1, int) is True


def test_a_bool_return_is_still_allowed_where_bool_is_declared() -> None:
    """The rule above must not make honest bools unrepresentable."""
    from backend.core.ouroboros.cli import thin_client as tc

    assert contract_fake(tc.ensure_daemon, lambda **_k: True) is not None
    assert returns_match(True, bool) is True


# --------------------------------------------------------------------------
# 3. it stays out of the way
# --------------------------------------------------------------------------

def test_an_unannotated_target_is_passed_through_untouched() -> None:
    """No annotation means nothing to enforce. Wrapping anyway would give
    false confidence — the checker would look installed and check nothing."""
    def unannotated(x):
        return x

    fake = lambda x: x                                   # noqa: E731
    assert contract_fake(unannotated, fake) is fake


def test_async_targets_yield_awaitable_doubles() -> None:
    """The double must await the way production does, or the call site has to
    know it is talking to a test."""
    import inspect

    from backend.core.ouroboros.cli import thin_client as tc

    assert inspect.iscoroutinefunction(
        contract_fake(tc.probe_socket, lambda *_a, **_k: "live")
    )
    assert not inspect.iscoroutinefunction(
        contract_fake(tc._live_incumbent, lambda: None)
    )


async def test_an_async_fake_is_awaited_before_checking() -> None:
    """Fakes are written both ways; both must be enforced."""
    from backend.core.ouroboros.cli import thin_client as tc

    async def _async_fake(*_a: Any, **_k: Any) -> Any:
        return True                                      # wrong type

    with pytest.raises(ContractMismatchError):
        await contract_fake(tc.probe_socket, _async_fake)("/tmp/x")


def test_arguments_reach_the_fake_unchanged() -> None:
    from backend.core.ouroboros.cli import thin_client as tc

    seen: List[Any] = []

    def _fake(path: Any, **kwargs: Any) -> Any:
        seen.append((path, kwargs))
        return None

    contract_fake(tc._live_incumbent, _fake)("/tmp/x", deep=True)
    assert seen == [("/tmp/x", {"deep": True})]


def test_an_exotic_annotation_does_not_produce_a_false_alarm() -> None:
    """A checker that cries wolf gets switched off, and then protects
    nothing. Unknown shapes are accepted rather than guessed at."""
    from typing import Callable as C

    def weird() -> C[[int], str]:            # type: ignore[empty-body]
        ...

    contract_fake(weird, lambda: "anything")()      # must not raise


# --------------------------------------------------------------------------
# 4. composed with signature enforcement
# --------------------------------------------------------------------------

def test_autospec_catches_the_other_half_of_drift() -> None:
    """Signature enforcement catches being CALLED wrongly; return enforcement
    catches ANSWERING wrongly. This bug was the second, and only the first
    had a tool."""
    from backend.core.ouroboros.cli import thin_client as tc

    spec = autospec_fake(tc._live_incumbent, lambda: None)
    spec()
    with pytest.raises(TypeError):
        spec("an argument _live_incumbent does not take")


async def test_autospec_still_enforces_the_return_type() -> None:
    from backend.core.ouroboros.cli import thin_client as tc

    spec = autospec_fake(tc.probe_socket, lambda *_a, **_k: False)
    with pytest.raises(ContractMismatchError):
        await spec("/tmp/x")
