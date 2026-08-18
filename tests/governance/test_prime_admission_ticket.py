"""The admission claim must reach the seam that settles it.

`PrimeProvider.generate` ran local-model admission and bound two locals:
`_brain_for_adm` and a one-slot cell `_adm_reservation`. Their only consumer
is `_generate_raw`, which is nested inside `_generate_impl` — a SIBLING
method (5627..6153), not inside `generate` (5516..5624). There was no
closure, so every `record_load_outcome` / `release_reservation` call raised
`NameError` into an `except Exception: pass`.

The comments recorded the right technique for the wrong topology: "a one-slot
cell rather than a bare name: `_generate_raw` closes over it", and
`_brain_for_adm` was pre-initialised specifically to avoid "a bare NameError
the moment the binding block raises early". Correct defensive reasoning,
applied in a scope the consumer cannot see.

The cost was not telemetry. `release_reservation` hands back the soft VRAM
claim, and its own comment says the ledger self-heals "only after a settle
window during which every other worker is throttled by memory this one has
definitively stopped wanting". So the claim was never returned early and
local admission throttled workers against bytes nobody was using.

A value that must cross a call boundary travels ALONG it.
"""
from __future__ import annotations

import inspect

import pytest

import backend.core.ouroboros.governance.providers as providers


class TestTicket:
    def test_is_frozen(self):
        t = providers._AdmissionTicket(brain_id="b", reservation_id="r")
        with pytest.raises(Exception):
            t.brain_id = "other"  # type: ignore[misc]

    def test_defaults_are_empty_and_unsettleable(self):
        """Admission is best-effort. A ticket that claimed otherwise would
        make the ledger record a load it never admitted."""
        t = providers._AdmissionTicket()
        assert t.brain_id is None and t.reservation_id is None
        assert t.settleable is False

    @pytest.mark.parametrize("kw", [
        {"brain_id": "b"},
        {"reservation_id": "r"},
        {"brain_id": "b", "reservation_id": "r"},
    ])
    def test_settleable_when_either_half_is_present(self, kw):
        assert providers._AdmissionTicket(**kw).settleable is True


class TestItTravelsAlongTheCall:
    """The wiring pin. `_generate_impl` accepting a ticket nobody passes
    would be the wired-but-inert shape this repo names most often."""

    def test_generate_impl_accepts_the_ticket(self):
        sig = inspect.signature(providers.PrimeProvider._generate_impl)
        assert "admission" in sig.parameters
        assert sig.parameters["admission"].default is None, (
            "a required parameter would break any caller that has no "
            "admission to hand over"
        )

    def test_generate_passes_it(self):
        """Structural, because driving the real `generate` needs a live
        client. The call site is what regressed; the call site is pinned."""
        src = inspect.getsource(providers.PrimeProvider.generate)
        assert "_generate_impl(" in src
        assert "admission=_AdmissionTicket(" in src, (
            "generate() must hand the ticket to _generate_impl — without it "
            "the settle seam is unreachable again"
        )

    def test_the_dead_cross_scope_names_are_gone(self):
        """`_generate_raw` may not reference names from `generate`'s scope."""
        src = inspect.getsource(providers.PrimeProvider._generate_impl)
        assert "_adm_reservation[0]" not in src
        assert "record_load_outcome(\n                        _brain_for_adm" not in src

    def test_the_settle_seam_reads_the_ticket(self):
        src = inspect.getsource(providers.PrimeProvider._generate_impl)
        assert "admission.brain_id" in src
        assert "admission.reservation_id" in src

    def test_the_settle_seam_is_guarded_against_no_ticket(self):
        """No admission taken means nothing to settle — not a crash."""
        src = inspect.getsource(providers.PrimeProvider._generate_impl)
        assert "if admission is not None:" in src


def test_no_undefined_names_remain_in_the_generation_path():
    """The measurement that opened this: four executable-position findings in
    `providers.py`, all of them these two names."""
    pytest.importorskip("pyflakes")
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ci.lint_gate import undefined_names

    path = Path(inspect.getfile(providers))
    real = [f for f in undefined_names(path) if not f.inert]
    assert not real, f"executable-position undefined names remain: {real}"
