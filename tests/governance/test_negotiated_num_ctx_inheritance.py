"""One negotiation, every consumer.

The Context-Hardware Negotiator derives a VRAM-safe `num_ctx` from MEASURED
hardware, but the result lived as a local override inside ONE dispatch path.
`LocalConfig.from_env()` reads `JARVIS_LOCAL_NUM_CTX`, deliberately unset on a
host that autodetects -- so every client built outside that path got
`num_ctx=None`, lost the streaming inter-token watchdog, and fell to the legacy
total-duration branch with a cold seed sized for a small model.

Measured, soak bt-2026-08-30-155721: the main path negotiated `num_ctx=32768`
69 times while L2 Repair -- calling the SAME provider object by a different
route -- died at `budget=30000ms warm=False` against a cold 32B, cascading
seven ops into `l2_cancelled`.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.governance import local_inference_director as L

_E = "http://127.0.0.1:11434"


@pytest.fixture(autouse=True)
def _clean():
    L.reset_negotiated_num_ctx_for_tests()
    yield
    L.reset_negotiated_num_ctx_for_tests()


def _client(num_ctx=None, base_url=_E):
    cfg = dataclasses.replace(
        L.LocalConfig.from_env(), base_url=base_url, num_ctx=num_ctx
    )
    c = L.LocalPrimeClient.__new__(L.LocalPrimeClient)
    c._cfg = cfg
    return c


def test_a_client_with_no_window_inherits_the_negotiated_one():
    """The defect. A client built from env alone must not be left on the
    legacy branch when the hardware window is already known."""
    L.publish_negotiated_num_ctx(_E, 32768)
    c = _client(num_ctx=None)
    c._inherit_negotiated_num_ctx()
    assert c._cfg.num_ctx == 32768


def test_an_explicit_window_is_never_overwritten():
    """Inheritance FILLS A GAP. A caller that chose a smaller window keeps it."""
    L.publish_negotiated_num_ctx(_E, 32768)
    c = _client(num_ctx=4096)
    c._inherit_negotiated_num_ctx()
    assert c._cfg.num_ctx == 4096


def test_no_negotiation_leaves_the_client_untouched():
    """Byte-identical to the previous behaviour when nothing was published."""
    c = _client(num_ctx=None)
    c._inherit_negotiated_num_ctx()
    assert c._cfg.num_ctx is None


def test_the_registry_is_per_endpoint():
    """A second node must not inherit the first node's hardware window."""
    L.publish_negotiated_num_ctx(_E, 32768)
    c = _client(num_ctx=None, base_url="http://other:11434")
    c._inherit_negotiated_num_ctx()
    assert c._cfg.num_ctx is None


@pytest.mark.parametrize("bad", [0, -1, None])
def test_a_nonsense_window_is_never_published(bad):
    """A zero or negative window would re-create the survival branch while
    looking configured."""
    L.publish_negotiated_num_ctx(_E, bad)
    assert L.negotiated_num_ctx(_E) is None


def test_publication_never_raises_on_a_bad_endpoint():
    """Publication is additive telemetry; it must never break a dispatch."""
    L.publish_negotiated_num_ctx("", 32768)
    L.publish_negotiated_num_ctx(None, 32768)
    assert L.negotiated_num_ctx("") is None


def test_inheritance_arms_the_streaming_path():
    """The POINT of inheriting: `complete_guarded` chooses streaming (an
    inter-token watchdog, no total-duration cap) only when num_ctx is set.
    Without inheritance a cold 32B is judged by a budget it cannot meet."""
    L.publish_negotiated_num_ctx(_E, 32768)
    c = _client(num_ctx=None)
    c._inherit_negotiated_num_ctx()
    assert bool(c._cfg.num_ctx) and L._streaming_enabled()


def test_the_guarded_path_inherits_before_deciding():
    """WIRING PIN -- inheritance must run BEFORE the streaming branch, or the
    decision is made on a stale window and the fix is inert."""
    import inspect

    src = inspect.getsource(L.LocalPrimeClient.complete_guarded)
    assert "_inherit_negotiated_num_ctx" in src, "inheritance is unwired"
    assert src.index("_inherit_negotiated_num_ctx") < src.index(
        "_streaming_enabled()"
    ), "inheritance must precede the streaming decision"


def test_the_negotiator_publishes_its_result():
    """The other half of the contract: the producer must actually publish."""
    import io as _io

    import backend.core.ouroboros.governance.candidate_generator as CG

    src = _io.open(CG.__file__, encoding="utf-8").read()
    assert "publish_negotiated_num_ctx" in src, "negotiator never publishes"
