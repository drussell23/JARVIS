"""The governed loop's brain selector is the RouteDecisionService facade;
the served-capability verdict lives on the BrainSelector it wraps.
``_resolve_served_capability`` probes the facade for the method by name, so
an un-forwarded verdict silently stamps the slot's declaration."""
from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.governance.route_decision_service import RouteDecisionService


class _Inner:
    def __init__(self) -> None:
        self.calls: list = []
        self.daily_spend = 0.0

    def effective_schema_capability(self, *, declared, served_model):
        self.calls.append((declared, served_model))
        return SimpleNamespace(served_model=served_model, declared=declared,
                               capability="full_content_and_diff", changed=True, reason="policy")


def test_the_facade_forwards_the_served_capability_verdict():
    inner = _Inner()
    facade = RouteDecisionService(brain_selector=inner)
    v = facade.effective_schema_capability(declared="full_content_only", served_model="qwen3-coder-ov:30b")
    assert inner.calls == [("full_content_only", "qwen3-coder-ov:30b")]
    assert v.capability == "full_content_and_diff" and v.changed


def test_the_governed_loop_probe_finds_it_on_the_facade():
    """The exact probe the governed loop performs before trusting a verdict."""
    facade = RouteDecisionService(brain_selector=_Inner())
    assert hasattr(facade, "effective_schema_capability")
