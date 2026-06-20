"""Calibration seed — a blast-radius-1, fully-isolated logic flaw used ONLY as the
deterministic dispatch system test for the Sovereign Cloud calibration run.

Nothing imports this module (blast radius = 1), it touches no production code, and
it is greenlit by the OperationAdvisor ONLY under JARVIS_CALIBRATION_MODE_ENABLED
(see governance/calibration_context.py). The failing assertion below is the
TestFailure signal that drives O+V to repair ``_add`` — the smallest possible
end-to-end exercise of the DW dispatch → generate → validate → apply path.
"""


def _add(a: int, b: int) -> int:
    # SEED DEFECT: should be ``a + b``. The single, isolated flaw O+V must repair.
    return a - b


def test_seed_defect_add() -> None:
    assert _add(2, 3) == 5
