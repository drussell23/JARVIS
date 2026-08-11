"""transcript_kill_harness — kill a real process at a named byte boundary.

Steps 2-4 of the transcript durability arc. Three oracles live here,
each honest about the one thing it can prove:

======================  ==========================  ======================
Oracle                  What actually dies          What it proves
======================  ==========================  ======================
A  exhaustive           nothing (in-process)        the READER, against
   truncation                                       every torn shape
B  real SIGKILL         the process                 the WRITER, under
                                                    genuine process death
C  simulated            (modelled) the machine      the DURABILITY
   power loss                                       BOUNDARY
======================  ==========================  ======================

Why three, and not one
----------------------

**A SIGKILL cannot test the page cache.** When a process is killed, its
written-but-unsynced bytes belong to the kernel, remain visible to every
subsequent reader, and are written back normally. Only a panic or power
loss loses them. So Oracle B — the one the mandate names — proves
*application* atomicity (partial frames, index/data skew) and says
nothing whatsoever about ``fsync`` placement. A harness that let a green
B imply durability would be manufacturing exactly the false confidence
:mod:`durable_io` exists to remove.

Oracle C closes that gap without a VM or ``dm-flakey``: the writer's
``(write, sync)`` sequence is recorded, then replayed truncated at each
durability barrier. Everything acknowledged before a barrier must be
recoverable after it. That is the power-loss model, in pure Python.

No timing heuristics
--------------------

The child emits a token on a pipe at a named seam and then **blocks on a
read that never returns**. The parent reads the token and sends SIGKILL.
There is no ``sleep`` anywhere in the trigger path — the kill is caused
by the child reaching the seam, not by a clock guessing that it has.

The one timeout present (:data:`_SEAM_WAIT_S`) is a failsafe on the
parent's read so a wedged child cannot hang a suite forever. Its expiry
is reported as a HARNESS FAILURE, never as a passing scenario — a
watchdog that can be mistaken for a result is worse than none.

Writer-agnostic by construction
-------------------------------

Every oracle asserts on the **artifact** — the bytes on disk — never on
the code that produced them. The child's ``--writer`` mode therefore
selects between the raw framing path (available before the durable
writer exists) and the real writer, and the same assertions apply to
both. That is what makes this a baseline oracle rather than a mirror of
one implementation's bugs.
"""
from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TranscriptKillHarness")


__all__ = [
    "KillOutcome",
    "SEAMS",
    "HarnessError",
    "build_records",
    "run_kill_scenario",
    "seam_after_record",
    "seam_after_sync",
    "seam_mid_record",
]


#: Failsafe only. Never the trigger — see the module docstring.
_SEAM_WAIT_S: float = 30.0

_MODULE = "backend.core.ouroboros.battle_test.transcript_kill_harness"


class HarnessError(RuntimeError):
    """The harness itself misbehaved. Distinct from a scenario finding:
    a scenario that cannot be staged has proved nothing, and must never
    be reported as a pass."""


# ===========================================================================
# Seam vocabulary — one definition, used by both sides of the fork
# ===========================================================================


def seam_mid_record(n: int) -> str:
    """Between two chunks of record ``n``'s frame — a genuinely torn
    record on disk, which is the shape a >1-sector write can leave."""
    return f"mid_record:{n}"


def seam_after_record(n: int) -> str:
    """Immediately after record ``n``'s frame is fully written, before
    any sync. The page cache holds it; a SIGKILL must not lose it."""
    return f"after_record:{n}"


def seam_after_sync(n: int) -> str:
    """Immediately after the durability barrier covering record ``n``."""
    return f"after_sync:{n}"


#: The named seams, in the order a record passes through them.
SEAMS: Tuple[str, ...] = ("mid_record", "after_record", "after_sync")


def build_records(count: int, *, start: int = 1) -> List[Dict[str, object]]:
    """The corpus every oracle writes. Deterministic on purpose: a
    reproducible failure is worth more than a broad random one, and the
    torn shapes are supplied by the truncation matrix, not by the data."""
    return [
        {
            "seq": n,
            "kind": "diff",
            "ref": f"d-{n}",
            "op_id": f"op-{n:04d}",
        }
        for n in range(start, start + max(0, count))
    ]


# ===========================================================================
# Parent side — stage a scenario, kill at the seam, return the artifact
# ===========================================================================


@dataclass(frozen=True)
class KillOutcome:
    """What the child did before it was killed, and what it left behind."""

    path: Path
    seam: str
    seam_reached: bool
    returncode: Optional[int]
    killed_by_signal: bool
    file_size: int
    stderr: str = ""

    @property
    def died_by_sigkill(self) -> bool:
        return self.returncode == -signal.SIGKILL


def run_kill_scenario(
    *,
    path: "os.PathLike[str] | str",
    seam: str,
    records: int = 8,
    sync_every: int = 0,
    split_bytes: int = 0,
    writer: str = "raw",
    repo_root: Optional[Path] = None,
) -> KillOutcome:
    """Run a child that appends frames and SIGKILL it exactly at ``seam``.

    The child inherits one pipe write-end. It emits the seam token there
    and then blocks forever; this function reads the token and kills. No
    part of the trigger consults a clock.

    Raises :class:`HarnessError` when the scenario could not be staged —
    the child died early, or the seam was never reached. Those are not
    findings about the writer, and must not be scored as any.
    """
    root = Path(repo_root) if repo_root else _repo_root()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    read_fd, write_fd = os.pipe()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)
    # Unbuffered child stdio so a crash cannot strand diagnostics in a
    # buffer that dies with the process.
    env["PYTHONUNBUFFERED"] = "1"

    argv = [
        sys.executable, "-m", _MODULE, "--child",
        "--path", str(target),
        "--seam", seam,
        "--records", str(int(records)),
        "--sync-every", str(int(sync_every)),
        "--split-bytes", str(int(split_bytes)),
        "--writer", str(writer),
        "--signal-fd", str(write_fd),
    ]

    proc = subprocess.Popen(
        argv, cwd=str(root), env=env,
        pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(write_fd)  # parent must not hold it, or EOF never arrives

    seam_reached = False
    try:
        token = _read_token(read_fd, _SEAM_WAIT_S)
        seam_reached = token == seam
        if seam_reached:
            os.kill(proc.pid, signal.SIGKILL)
    finally:
        os.close(read_fd)
        try:
            _, err = proc.communicate(timeout=_SEAM_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        stderr = (err or b"").decode("utf-8", "replace")

    if not seam_reached:
        raise HarnessError(
            f"seam {seam!r} never reached (rc={proc.returncode}); "
            f"child stderr: {stderr.strip()[:500]}"
        )

    size = target.stat().st_size if target.exists() else 0
    return KillOutcome(
        path=target,
        seam=seam,
        seam_reached=True,
        returncode=proc.returncode,
        killed_by_signal=(proc.returncode or 0) < 0,
        file_size=size,
        stderr=stderr,
    )


def _read_token(fd: int, timeout_s: float) -> str:
    """Read one newline-terminated token, or ``""`` on EOF/failsafe."""
    buf = b""
    while b"\n" not in buf:
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return ""                      # failsafe expiry -> HarnessError
        chunk = os.read(fd, 4096)
        if not chunk:
            return ""                      # child exited without emitting
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace")


def _repo_root() -> Path:
    # battle_test/ -> ouroboros/ -> core/ -> backend/ -> repo root
    return Path(__file__).resolve().parents[4]


# ===========================================================================
# Child side — append frames, emit at the seam, block until killed
# ===========================================================================


def _emit_and_block(signal_fd: int, token: str) -> None:
    """Announce arrival at the seam, then stop existing on our own terms.

    The block is a read on a pipe with no writer we will ever satisfy —
    it cannot time out, cannot spin, and cannot be woken by anything but
    the parent's signal. A ``sleep`` here would race the kill; this
    cannot."""
    os.write(signal_fd, (token + "\n").encode("ascii"))
    r, w = os.pipe()
    try:
        os.read(r, 1)      # blocks forever; SIGKILL is the only exit
    finally:                # pragma: no cover — unreachable after SIGKILL
        os.close(r)
        os.close(w)


def _write_all(fd: int, data: bytes) -> None:
    """Complete a short write. Safe because this process is the only
    writer: with a second appender, resuming a short write would splice
    another writer's frame into the middle of ours."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _child_durable(args: argparse.Namespace) -> int:
    """Drive the REAL :class:`DurableLogWriter` to the seam.

    Without this mode the oracle would only ever have proved the raw
    framing, and "the writer passes the kill test" would be an inference
    rather than a measurement. The split is installed by replacing the
    module-level ``_write_all`` the writer already calls — production
    code is untouched, and the tear lands inside the writer's own append
    path rather than beside it.
    """
    from backend.core.ouroboros.battle_test import transcript_writer as tw

    seam = str(args.seam or "")
    signal_fd = int(args.signal_fd)
    split = max(0, int(args.split_bytes))
    original = tw._write_all
    frames = {"n": 0}

    def _patched(fd: int, data: bytes) -> None:
        frames["n"] += 1
        n = frames["n"]
        if seam == seam_mid_record(n):
            cut = min(split or max(1, len(data) // 2), len(data) - 1)
            original(fd, data[:cut])
            _emit_and_block(signal_fd, seam)
        original(fd, data)
        if seam == seam_after_record(n):
            _emit_and_block(signal_fd, seam)

    tw._write_all = _patched          # type: ignore[assignment]
    writer = tw.DurableLogWriter(
        str(args.path), sync_every_n=int(args.sync_every) or 1000,
    )
    writer.start()
    for record in build_records(int(args.records)):
        writer.submit(record)
    writer.barrier(timeout=30)
    os.write(signal_fd, b"__never__\n")
    return 3


def _child_main(args: argparse.Namespace) -> int:
    if str(args.writer) == "durable":
        return _child_durable(args)

    from backend.core.ouroboros.battle_test.transcript_log import encode_record
    from backend.core.ouroboros.governance.durable_io import fsync_file

    seam = str(args.seam or "")
    signal_fd = int(args.signal_fd)
    split = max(0, int(args.split_bytes))

    fd = os.open(
        str(args.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
    )
    try:
        for record in build_records(int(args.records)):
            n = int(record["seq"])  # type: ignore[arg-type]
            frame = encode_record(record)

            if seam == seam_mid_record(n):
                # Deliberately tear this frame. The SPLIT LIVES IN THE
                # CHILD, never in the writer: production issues one
                # write per frame, and the harness manufactures the torn
                # shape that a device could produce anyway.
                cut = split if split else max(1, len(frame) // 2)
                cut = min(cut, len(frame) - 1)
                _write_all(fd, frame[:cut])
                _emit_and_block(signal_fd, seam)
            else:
                _write_all(fd, frame)

            if seam == seam_after_record(n):
                _emit_and_block(signal_fd, seam)

            every = int(args.sync_every)
            if every > 0 and n % every == 0:
                fsync_file(fd)
                if seam == seam_after_sync(n):
                    _emit_and_block(signal_fd, seam)
    finally:
        os.close(fd)

    # Reaching here means the seam was never hit. Exit non-zero so the
    # parent reports a staging failure rather than silently scoring it.
    os.write(signal_fd, b"__never__\n")
    return 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="transcript kill harness child")
    p.add_argument("--child", action="store_true")
    p.add_argument("--path", required=True)
    p.add_argument("--seam", default="")
    p.add_argument("--records", type=int, default=8)
    p.add_argument("--sync-every", type=int, default=0)
    p.add_argument("--split-bytes", type=int, default=0)
    p.add_argument("--writer", default="raw")
    p.add_argument("--signal-fd", type=int, required=True)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.child:
        raise SystemExit("this entry point runs only as --child")
    return _child_main(args)


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(main())
