"""Deterministic traceback resolution for the micro-fix repair lane.

Why this exists
---------------

The micro-fix loop refuses to patch unless it knows *which line* failed, and
it was refusing on every op with ``error_type=UnknownError line=0``. The
existing regex cascade already understood five pytest shapes, so the missing
piece was not pytest literacy. Reproducing the real subprocess found four
separate faults, three of which are worse than the refusal they caused:

1. ``pytest.ini`` carries ``--color=yes``, which forces ANSI escapes even
   into a pipe. Every pattern in the cascade is line-anchored (``^E\\s+``,
   ``^(\\S+):(\\d+):``) and an escape sequence sits in front of the anchor,
   so nothing matched and everything fell through to ``UnknownError``. A
   bare-temp-dir reproduction hides this exactly, because it has no
   ``pytest.ini``.

2. The short-summary branch reported ``line_number=1`` when it could not
   find a real one. The hard guard only rejects ``line <= 0``, so a
   fabricated 1 sailed through it -- the loop would patch the top of the
   file, which is the blind-patching corruption the guard exists to prevent.

3. Frames were taken from the end of the traceback without asking who owns
   them, so a failure resolved into ``pathlib.py`` or ``_pytest/python.py``
   -- the loop would have edited the standard library.

4. The run was unscoped, so pytest collected the whole repository and ``-x``
   stopped at the first ambient failure, which is some other file entirely.
   That is fixed at the call site; this module makes the consequence
   impossible to act on by refusing frames the candidate does not own.

What it guarantees
------------------

A location is returned only when it is a line this repository controls, that
exists, and that carries a statement. Otherwise the answer is ``None`` and
the caller drops the repair. Nothing here asks a model where the failure is:
the traceback already says, and a guess is what produced the corruption this
lane is guarded against.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PytestTraceback")

# CSI sequences, including the private-parameter forms pytest's terminal
# writer emits. Applied before any matching: see fault (1).
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

# A path segment that means "not our source". This is the authority for the
# traceback-ownership question only. Several modules carry a private
# ``_SKIP_DIRS`` for directory walks; those answer "should I descend into
# this while scanning the tree", which is a different question with a
# different correct answer (a walk may skip ``.git``; a traceback never
# names it), so they are deliberately not shared.
_VENDORED_SEGMENTS = frozenset({
    "site-packages", "dist-packages", "node_modules",
    ".venv", "venv", ".tox", ".nox", ".eggs", "__pypackages__",
})

# pytest frame:  tests/foo.py:42: in test_bar
#                tests/foo.py:42: AssertionError
# The function clause is optional because pytest omits it on the final
# frame, which is usually the one that matters.
_PYTEST_FRAME = re.compile(
    r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+):"
    r"(?:\s+in\s+(?P<func>\S+))?\s*(?P<tail>.*)$",
    re.MULTILINE,
)

# stdlib frame:  File "/x/y.py", line 42, in fn
# The optional ``E`` prefix is not cosmetic: pytest reports a collection
# SyntaxError as ``E     File "...", line 1``, and requiring a bare line
# start dropped the single most repairable failure there is -- the engine
# refused it as unattributable and the candidate never got fixed. ``File
# "..."`` is unambiguous enough to read through the marker, which is why the
# pytest frame pattern below still refuses ``E`` lines and this one does not.
# The comma before ``in`` is optional because ``faulthandler`` -- the only
# stack a hang killed at the WALL cap can produce -- prints
# ``File "x.py", line 7 in fn``, where ``traceback`` prints ``line 7, in fn``.
_STDLIB_FRAME = re.compile(
    r'^(?:E\s+)?\s*File "(?P<path>[^"]+)", line (?P<line>\d+)'
    r'(?:,? in (?P<func>\S+))?',
    re.MULTILINE,
)

# pytest's error line:  E   AssertionError: assert 1 == 2
_ERROR_LINE = re.compile(
    r"^E\s+(?P<etype>[A-Za-z_][\w.]*"
    r"(?:Error|Exception|Warning|Failure|Exit|Interrupted|Skipped))"
    r"\s*(?::\s*(?P<msg>.*))?$",
    re.MULTILINE,
)

# Bare terminal exception line:  AssertionError: assert 1 == 2
_BARE_ERROR = re.compile(
    r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception))\s*(?::\s*(?P<msg>.*))?$",
    re.MULTILINE,
)

# A plain failed assertion:  E       assert 1 == 2
# pytest names no exception class for these -- it prints the rewritten
# expression and nothing else -- so without this they arrive as an untyped
# "PytestFailure" carrying no message, and the repair prompt is told a test
# failed without being told what it asserted.
_ASSERT_LINE = re.compile(r"^E\s+(?P<msg>assert\s.*)$", re.MULTILINE)

# pytest's section rule:  ======= FAILURES =======   /   ==== 2 failed in 0.1s ====
# The report is a sequence of these rules, each followed by its body. The
# title is kept verbatim (case-folded as a key) rather than matched against a
# list, so a section this module has never heard of is still a section.
_SECTION_RULE = re.compile(r"^=+ (?P<title>.+?) =+$", re.MULTILINE)

#: The sections that say WHAT failed, in the order pytest prints them.
FAILURE_SECTIONS: Tuple[str, ...] = ("errors", "failures")
#: The section pytest writes one ``FAILED id - message`` line per failure into.
SUMMARY_SECTION = "short test summary info"


@dataclass(frozen=True)
class Frame:
    """One traceback frame, exactly as the traceback stated it."""

    file_path: str
    line_number: int
    function: str = ""


@dataclass(frozen=True)
class ParsedFailure:
    """A failure located on a line this repository owns."""

    error_type: str
    message: str
    file_path: str
    line_number: int
    frames: Tuple[Frame, ...]
    resolution: str
    """How the frame was chosen -- ``candidate``, ``repo``. Carried so an
    operator can tell a frame in the file under repair from one merely
    somewhere in the repo, without re-deriving it from the log."""


def strip_ansi(text: str) -> str:
    """Remove terminal control sequences. NEVER raises."""
    try:
        return _ANSI_RE.sub("", text or "")
    except Exception:  # noqa: BLE001
        return text or ""


def report_sections(output: str) -> Dict[str, str]:
    """pytest's report split on its own ``=== title ===`` rules. NEVER raises.

    Keyed by the case-folded title; the value is the text between that rule
    and the next, ANSI already stripped. A title repeated by pytest (it does
    not, today) keeps both bodies, joined in printed order.
    """
    out: Dict[str, str] = {}
    try:
        clean = strip_ansi(output)
        rules = list(_SECTION_RULE.finditer(clean))
        for i, rule in enumerate(rules):
            end = rules[i + 1].start() if i + 1 < len(rules) else len(clean)
            key = rule.group("title").strip().casefold()
            body = clean[rule.end():end].strip("\n")
            out[key] = f"{out[key]}\n{body}" if key in out else body
    except Exception:  # noqa: BLE001
        logger.debug("[PytestTraceback] section parse degraded", exc_info=True)
    return out


@dataclass(frozen=True)
class FailureEvidence:
    """What a test run said about its failure, in the words a repair needs.

    ``summary`` is one line per failure (pytest's own ``FAILED id - message``
    lines); ``trace`` is the tracebacks behind them. Both ANSI-free and bounded
    by :func:`epistemic_feedback.trace_max_chars`.
    """

    summary: str
    trace: str


def failure_evidence(stdout: str, stderr: str = "") -> FailureEvidence:
    """The failure a test run reported, read from BOTH of its streams. NEVER raises.

    ## Why this exists

    The L2 repair loop told the model what failed with
    ``(stdout + stderr)[:300]`` and ``stderr``. pytest writes its failures to
    STDOUT, after a session header and a progress bar, and ``pytest.ini``
    forces ``--color=yes`` into the pipe: measured through the real
    ``RepairSandbox``, those 300 characters were the header and a red ``FF``,
    and stderr was empty. Every repair iteration in bt-2026-09-21-235603 was
    asked to fix a test without being told what it asserted -- 40 iterations,
    zero converged, seven ops ended ``class_retries_exhausted:test``.

    ## What counts as evidence

    pytest's ``errors``/``failures`` sections when it printed any; otherwise
    (a timeout, an interpreter crash, a collection abort, a non-pytest runner)
    the whole cleaned output, because then the output IS the evidence and
    nothing here can know which part matters. stderr is appended when it says
    anything, never dropped. The summary falls back to the last error the
    output names, then to the trace's last line, so it is never empty while
    the run said something.
    """
    from backend.core.ouroboros.governance import epistemic_feedback as _ef  # noqa: PLC0415

    try:
        out = strip_ansi(stdout or "").strip("\n")
        err = strip_ansi(stderr or "").strip("\n")
        sections = report_sections(out)
        failed = [sections[name] for name in FAILURE_SECTIONS if sections.get(name)]
        if failed:
            trace = "\n\n".join(failed)
            if err.strip():
                trace = f"{trace}\n\n--- stderr ---\n{err}"
        else:
            trace = "\n".join(part for part in (out, err) if part.strip())
        summary = sections.get(SUMMARY_SECTION, "").strip()
        if not summary:
            etype, message = parse_error(f"{out}\n{err}")
            summary = ": ".join(part for part in (etype, message) if part)
        if not summary and trace.strip():
            summary = trace.strip().splitlines()[-1]
        budget = _ef.trace_max_chars()
        return FailureEvidence(
            summary=_ef.truncate_middle(summary, budget),
            trace=_ef.truncate_middle(trace, budget),
        )
    except Exception:  # noqa: BLE001 — evidence is best-effort, never fatal
        logger.debug("[PytestTraceback] failure evidence degraded", exc_info=True)
        raw = "\n".join(part for part in (stdout or "", stderr or "") if part)
        return FailureEvidence(summary="", trace=strip_ansi(raw))


def is_vendored(path: str) -> bool:
    """Whether *path* is third-party or an interpreter's own library.

    Segment-wise rather than substring: a repository legitimately named
    ``.../venv-tooling/...`` is not vendored, and ``in`` on the raw string
    would say it was.
    """
    try:
        parts = set(Path(path).parts)
    except Exception:  # noqa: BLE001
        return True
    if parts & _VENDORED_SEGMENTS:
        return True
    # CPython's own library, which no repo root contains.
    return "/lib/python3." in path.replace("\\", "/")


def _owned(path: str, repo_root: Path) -> bool:
    """Whether *path* is a source file this repository controls."""
    if not path or is_vendored(path):
        return False
    # ``<frozen runpy>``, ``<string>``, ``<stdin>``: code with no file. Joined
    # onto the root as if repo-relative, these "resolved inside the repo" and
    # were owned -- a hang entirely inside the interpreter's import machinery
    # would have been blamed on the candidate.
    if path.startswith("<"):
        return False
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            # pytest prints repo-relative paths; they are ours by definition
            # only if they resolve inside the root.
            candidate = repo_root / candidate
        candidate.resolve().relative_to(repo_root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_frames(output: str) -> Tuple[Frame, ...]:
    """Every frame the output names, outermost first. NEVER raises.

    Both traceback dialects are collected and kept in the order they were
    printed, so "innermost" is simply the last one -- which is what a stack
    walk needs and what neither dialect states explicitly.
    """
    frames: list = []
    try:
        clean = strip_ansi(output)
        seen: set = set()
        for match in _PYTEST_FRAME.finditer(clean):
            # `E   AssertionError: x.py:1: whatever` is error text, not a
            # frame; pytest never indents a real frame line.
            if match.group(0).startswith(("E ", "E\t")):
                continue
            key = (match.start(), match.group("path"))
            if key in seen:
                continue
            seen.add(key)
            frames.append((match.start(), Frame(
                file_path=match.group("path"),
                line_number=int(match.group("line")),
                function=(match.group("func") or "").strip(),
            )))
        for match in _STDLIB_FRAME.finditer(clean):
            frames.append((match.start(), Frame(
                file_path=match.group("path"),
                line_number=int(match.group("line")),
                function=(match.group("func") or "").strip(),
            )))
    except Exception:  # noqa: BLE001
        logger.debug("[PytestTraceback] frame parse degraded", exc_info=True)
    frames.sort(key=lambda pair: pair[0])
    return tuple(frame for _pos, frame in frames)


def parse_error(output: str) -> Tuple[str, str]:
    """``(error_type, message)`` for the LAST error the output names.

    Last rather than first: with ``-x`` the final error is the one that
    stopped the run, and a summary block may repeat earlier ones.
    """
    try:
        clean = strip_ansi(output)
        matches = list(_ERROR_LINE.finditer(clean)) or list(
            _BARE_ERROR.finditer(clean)
        )
        if matches:
            last = matches[-1]
            return (
                (last.group("etype") or "").strip(),
                (last.group("msg") or "").strip(),
            )
        asserts = list(_ASSERT_LINE.finditer(clean))
        if asserts:
            return "AssertionError", asserts[-1].group("msg").strip()
        return "", ""
    except Exception:  # noqa: BLE001
        return "", ""


def resolve_owned_frame(
    frames: Sequence[Frame],
    *,
    repo_root: Path,
    preferred_paths: Iterable[str] = (),
) -> Tuple[Optional[Frame], str]:
    """The innermost frame this repository owns, walking the stack backwards.

    A failure that surfaces in a fixture, a helper, or the standard library
    is still OUR failure, but it is not OUR line: patching ``pathlib.py``
    because ``read_text`` raised there is worse than not patching at all. So
    the walk starts at the innermost frame and moves outwards until it
    reaches code the repo controls.

    Frames in *preferred_paths* -- the files the candidate actually proposes
    -- outrank other repo frames regardless of depth. Without that, a failure
    raised inside a repo-owned conftest or helper would send the repair at a
    file the op never proposed and has no sanction to edit.

    Returns ``(None, reason)`` when nothing qualifies. That is a real answer:
    the caller drops the attempt.
    """
    if not frames:
        return None, "no_frames"
    preferred = {str(p) for p in preferred_paths if p}

    owned: list = []
    for frame in frames:
        if _owned(frame.file_path, repo_root):
            owned.append(frame)
    if not owned:
        return None, "no_repo_owned_frame"

    if preferred:
        for frame in reversed(owned):
            name = frame.file_path
            if name in preferred or any(
                name.endswith(p) or p.endswith(name) for p in preferred
            ):
                return frame, "candidate"
    return owned[-1], "repo"


def _verify_line(path: Path, line_number: int) -> bool:
    """Whether *line_number* is a real statement-bearing line in *path*.

    The traceback can outlive the text it describes -- the repair loop
    rewrites the file between iterations. A line past the end, or one
    carrying only a comment or blank space, means the location is stale and
    a patch aimed at it would land on unrelated code.

    A file that does not parse is NOT rejected: a SyntaxError candidate is
    precisely what the micro-fix should be repairing, and pytest reports its
    line accurately.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    except Exception:  # noqa: BLE001
        return False
    for node in ast.walk(tree):
        start = getattr(node, "lineno", None)
        if start is None:
            continue
        end = getattr(node, "end_lineno", None) or start
        if start <= line_number <= end:
            return True
    return False


async def parse_failure(
    output: str,
    *,
    repo_root: Path,
    preferred_paths: Iterable[str] = (),
) -> Optional[ParsedFailure]:
    """Locate a failure on a line this repository owns, or return ``None``.

    Async because the verification step reads and parses source files: on a
    deep traceback that is real blocking I/O, and this runs inside the
    orchestrator's event loop alongside heartbeats and the control plane,
    which a synchronous read would stall. The parsing itself is pure.
    """
    frames = parse_frames(output)
    frame, resolution = resolve_owned_frame(
        frames, repo_root=repo_root, preferred_paths=preferred_paths,
    )
    if frame is None:
        logger.info(
            "[PytestTraceback] unattributable failure (%s): %d frame(s), "
            "none owned by %s — dropping rather than guessing",
            resolution, len(frames), repo_root,
        )
        return None

    absolute = Path(frame.file_path)
    if not absolute.is_absolute():
        absolute = repo_root / frame.file_path
    try:
        verified = await asyncio.to_thread(
            _verify_line, absolute, frame.line_number,
        )
    except Exception:  # noqa: BLE001
        verified = False
    if not verified:
        logger.info(
            "[PytestTraceback] %s:%d does not carry a statement — stale or "
            "out-of-range location, dropping rather than patching it",
            frame.file_path, frame.line_number,
        )
        return None

    etype, message = parse_error(output)
    return ParsedFailure(
        error_type=etype or "PytestFailure",
        message=message,
        file_path=frame.file_path,
        line_number=frame.line_number,
        frames=frames,
        resolution=resolution,
    )


# ---------------------------------------------------------------------------
# Hangs -- a run cut off at a time cap, attributed to the code that blocked
# ---------------------------------------------------------------------------

#: pytest-timeout's banner. The authority for "this run was killed by the
#: per-test cap" (``test_runner`` reads it from here).
TIMEOUT_BANNER_RE = re.compile(r"\+{3,}\s*Timeout\s*\+{3,}|^Timeout:\s", re.M)

# One thread's stack, in either dialect a hang is reported in:
#   pytest-timeout (per-test cap):   ~~~~ Stack of MainThread (1403...) ~~~~
#   faulthandler   (wall cap):       Current thread 0x7f.. (most recent call first):
_STACK_HEADER = re.compile(
    r"^(?:~+ Stack of .+? ~+"
    r"|(?:Current thread|Thread) 0x[0-9a-fA-F]+ \(most recent call first\):)\s*$",
    re.M,
)
_INNERMOST_FIRST = "most recent call first"


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _rel(path: str, repo_root: Path) -> str:
    """Root-relative when inside the root. Sandboxes are fresh directories per
    run, so only the relative form is stable enough to compare across runs."""
    try:
        return Path(path).resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return path


def _source_at(path: str, line_number: int, repo_root: Path) -> str:
    try:
        target = Path(path)
        if not target.is_absolute():
            target = repo_root / target
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[line_number - 1].strip() if 0 < line_number <= len(lines) else ""
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class HangSite:
    """Where a run that never finished was stuck, in repo terms.

    ``call_site`` is the innermost frame the repository owns -- the call a
    test must mock; ``blocking`` is the innermost frame of all -- what it was
    waiting IN (``select``, ``wait``, ``recv``). Paths are root-relative.
    """

    node_id: str
    collection: bool
    call_site: Frame
    blocking: Frame
    source_line: str

    @property
    def key(self) -> str:
        """Identity of the unmocked call, stable across sandboxes and reruns."""
        return (
            f"{self.call_site.file_path}:{self.call_site.line_number}"
            f"->{self.blocking.function or '?'}"
        )

    def render(self) -> str:
        """A pytest summary block, so every consumer that already reads
        ``FAILED id - message`` (classifier ids, failure evidence, the repair
        prompt) reads the hang without learning a new format."""
        where = (
            f"{self.call_site.file_path}:{self.call_site.line_number}"
            f" in {self.call_site.function or '?'}"
        )
        code = f": `{self.source_line}`" if self.source_line else ""
        outcome = "ERROR" if self.collection else "FAILED"
        return (
            "=========================== short test summary info "
            "============================\n"
            f"{outcome} {self.node_id} - TestHangError: blocked in "
            f"{self.blocking.function or '?'}() "
            f"({Path(self.blocking.file_path).name}:{self.blocking.line_number}) "
            f"via {where}{code} -- the test waited on real I/O (a process, "
            "socket, event or lock) that never completed. Mock that call where "
            f"{self.call_site.file_path} looks it up; a unit test must never "
            "wait on a real process, socket or server."
        )


def _stacks(output: str) -> Tuple[Tuple[str, Tuple[Frame, ...]], ...]:
    """Each thread's stack as ``(header, frames outermost-first)``. A report
    with no thread headers is one stack in printed (outermost-first) order."""
    clean = strip_ansi(output)
    headers = list(_STACK_HEADER.finditer(clean))
    if not headers:
        return (("", parse_frames(clean)),)
    out = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(clean)
        frames = list(parse_frames(clean[header.end():end]))
        if _INNERMOST_FIRST in header.group(0):
            frames.reverse()
        out.append((header.group(0), tuple(frames)))
    return tuple(out)


def hang_site(
    output: str,
    *,
    repo_root: Path,
    preferred_paths: Iterable[str] = (),
) -> Optional[HangSite]:
    """Attribute a hang to the repository code it was stuck in. NEVER raises.

    ``None`` means the stack is purely external -- the interpreter, pytest,
    third-party code -- or absent: the cut-off says nothing about the
    candidate, and the run stays infrastructure. Otherwise the stack that
    passes through *preferred_paths* (the files under test) is chosen, else the
    first that passes through any repo-owned code; the pseudo-frames of
    ``<frozen ...>`` machinery are never owned.
    """
    try:
        root = Path(repo_root)
        preferred = {str(p) for p in preferred_paths if p}
        # Only a HANG REPORT is read: a timeout banner or a thread-stack dump.
        # A run cut off for another reason (memory-pressure shedding) can
        # still hold an ordinary failure traceback printed earlier, and that
        # is not where it was stuck.
        clean = strip_ansi(output)
        if not (_STACK_HEADER.search(clean) or TIMEOUT_BANNER_RE.search(clean)):
            return None

        def _is_preferred(frame: Frame) -> bool:
            name = frame.file_path
            return any(name.endswith(p) or p.endswith(name) for p in preferred)

        chosen: Optional[Tuple[Frame, ...]] = None
        fallback: Optional[Tuple[Frame, ...]] = None
        for _header, frames in _stacks(output):
            if not frames or not any(_owned(f.file_path, root) for f in frames):
                continue
            if preferred and any(_is_preferred(f) for f in frames):
                chosen = frames
                break
            if fallback is None:
                fallback = frames
        frames = chosen or fallback
        if not frames:
            return None

        owned = [f for f in frames if _owned(f.file_path, root)]
        call = owned[-1]
        blocking = frames[-1]
        entry = next(
            (f for f in owned if _is_preferred(f) or _is_test_file(f.file_path)),
            None,
        )
        collection = bool(entry and entry.function == "<module>")
        if entry is None:
            node_id = _rel(call.file_path, root)
        elif collection or not entry.function.startswith("test"):
            node_id = _rel(entry.file_path, root)
        else:
            node_id = f"{_rel(entry.file_path, root)}::{entry.function}"
        return HangSite(
            node_id=node_id,
            collection=collection,
            call_site=Frame(_rel(call.file_path, root), call.line_number, call.function),
            blocking=Frame(_rel(blocking.file_path, root), blocking.line_number, blocking.function),
            source_line=_source_at(call.file_path, call.line_number, root),
        )
    except Exception:  # noqa: BLE001 — attribution is best-effort; None = infra
        logger.debug("[PytestTraceback] hang attribution degraded", exc_info=True)
        return None


__all__ = [
    "Frame",
    "HangSite",
    "TIMEOUT_BANNER_RE",
    "hang_site",
    "ParsedFailure",
    "is_vendored",
    "parse_error",
    "parse_failure",
    "parse_frames",
    "resolve_owned_frame",
    "strip_ansi",
]
