"""`unified_supervisor` must exit cleanly, not segfault.

The defect (macOS crash report, 2026-07-24):

    EXC_BAD_ACCESS · KERN_INVALID_ADDRESS at 0x0
      type_dealloc -> insertdict -> _PyModule_ClearDict ->
      finalize_modules -> Py_FinalizeEx -> Py_Exit ->
      handle_system_exit -> _PyErr_PrintEx

`sys.exit()` raises SystemExit; argparse raises it directly for `--help`.
That reaches Py_FinalizeEx, and a C extension null-derefs while its module
dict is torn down. Result: `--help` — which does nothing but print usage —
took 9.8s and terminated with signal 11.

The fix is not to find the guilty extension (there are dozens, and any future
one could join them) but to make the fault UNREACHABLE: `os._exit()` skips
interpreter finalization entirely. The work is already done by then;
finalization only tidies memory the OS reclaims wholesale a microsecond later.
This mirrors `battle_test/harness.py`, which already applies the same remedy
for the same documented class.

These are subprocess tests on purpose. A segfault kills the interpreter, so
nothing in-process can observe it — only a child's return code can.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SUPERVISOR = _REPO / "unified_supervisor.py"

pytestmark = pytest.mark.skipif(
    not _SUPERVISOR.is_file(), reason="unified_supervisor.py not present",
)


def _run(*args, timeout=180):
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(_SUPERVISOR), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(_REPO),
    )
    return proc, time.time() - t0


def test_help_exits_zero_without_segfaulting():
    """THE REGRESSION: rc was -11 (SIGSEGV). It must now be 0."""
    proc, elapsed = _run("--help")

    assert proc.returncode != -11, (
        "SIGSEGV during interpreter finalization has returned — "
        "Py_FinalizeEx is running again on the exit path"
    )
    assert proc.returncode == 0, f"--help exited {proc.returncode}"
    assert "usage:" in (proc.stdout + proc.stderr).lower()
    # It used to take ~9.8s; the crash was part of that cost.
    assert elapsed < 60, f"--help took {elapsed:.1f}s"


def test_help_output_survives_the_hard_exit():
    """os._exit skips the flush normal shutdown performs, so stdio is flushed
    explicitly. If that were forgotten, --help would exit 0 printing NOTHING —
    a silent regression a return-code check alone would miss."""
    proc, _ = _run("--help")
    combined = proc.stdout + proc.stderr
    assert combined.strip(), "hard exit swallowed all output — flush is missing"
    assert "unified_supervisor" in combined.lower()


def test_status_does_not_segfault():
    """Any early-exit path reaches the same finalization; --status is a second
    witness that the fix is at the exit and not special-cased to --help."""
    proc, _ = _run("--status")
    assert proc.returncode != -11, "SIGSEGV on the --status path"
    assert proc.returncode in (0, 1), f"unexpected rc={proc.returncode}"


def test_no_negative_return_code_on_any_early_exit():
    """A negative rc means death by signal. No early-exit path may die that
    way — that is the whole property being defended."""
    for flag in ("--help", "--status"):
        proc, _ = _run(flag)
        assert proc.returncode >= 0, (
            f"{flag} was killed by signal {-proc.returncode}"
        )


# ---------------------------------------------------------------------------
# structural pins — the fix must not be silently reverted
# ---------------------------------------------------------------------------


def test_entry_point_hard_exits_instead_of_sys_exit():
    src = _SUPERVISOR.read_text(encoding="utf-8", errors="replace")
    tail = src[src.index('if __name__ == "__main__":'):][:2500]
    assert "os._exit(" in tail, "entry point no longer hard-exits"
    assert "sys.exit(main())" not in tail, (
        "sys.exit(main()) restored — that is the crashing path"
    )


def test_entry_point_catches_systemexit_from_argparse():
    """argparse raises SystemExit for --help BEFORE main() returns; if that
    escapes, Py_FinalizeEx runs and the segfault comes back."""
    src = _SUPERVISOR.read_text(encoding="utf-8", errors="replace")
    tail = src[src.index('if __name__ == "__main__":'):][:2500]
    assert "except SystemExit" in tail, "SystemExit would escape to Py_Exit"


def test_entry_point_flushes_stdio_before_hard_exit():
    src = _SUPERVISOR.read_text(encoding="utf-8", errors="replace")
    tail = src[src.index('if __name__ == "__main__":'):][:2500]
    assert "flush()" in tail, "os._exit without a flush loses buffered output"


# ---------------------------------------------------------------------------
# the reflex budget this unblocks
# ---------------------------------------------------------------------------


def test_reflex_budget_matches_measured_boot():
    """90s was invented, never measured. Measured: --status 2.9s, --help 4.2s.
    A budget an order of magnitude above reality is a UI that looks hung."""
    from backend.core.ouroboros.cli.audio_daemon_reflex import _boot_budget_s

    assert _boot_budget_s() <= 15.0, "budget drifted back above measured boot"


def test_boot_progress_events_ride_the_guaranteed_lane():
    """The socket binds before the mic is armed, so a connected cockpit needs
    an explicit edge to tell warming from armed-and-quiet. Losing it would
    strand the UI in a warming state, so it must NOT be droppable telemetry."""
    from backend.core.ouroboros.governance.comms.duplex import audio_state_ipc as ipc

    assert ipc.EVENT_SYSTEM_WARMING in ipc.EVENT_KINDS
    assert ipc.EVENT_SYSTEM_READY in ipc.EVENT_KINDS
    assert ipc.MSG_RMS_LEVEL not in ipc.EVENT_KINDS


def test_warming_precedes_ready_at_the_existing_bind_site():
    """Ordering pin, and a pin that the socket did NOT move: both edges are
    emitted from audio_pipeline_bootstrap, where the server already lived."""
    src = (_REPO / "backend" / "audio" / "audio_pipeline_bootstrap.py").read_text()
    assert "EVENT_SYSTEM_WARMING" in src and "EVENT_SYSTEM_READY" in src
    assert src.index("EVENT_SYSTEM_WARMING") < src.index("EVENT_SYSTEM_READY")
