"""LessonMemory — cross-op, module-keyed lesson recall over the failure-mode memory.

Why this exists (2026-09-07)
----------------------------
The organism relearned the same lessons on every op: writing a test for
``model_physics`` it hallucinated the API, then the payload shape, then the
expected values — six anchor upgrades later it landed, and the next module
(``production_oracle``) started from zero again. The PRD §31.4 failure-mode
memory arc was built for exactly this and is graduated — but its recording
side, :func:`failure_mode_memory.record_postmortem`, has NO production
caller, its retriever compared the candidate file set against *itself*, and
records carried no target files, so module-keyed recall was impossible as
shipped. The store never existed on disk.

This module is a COMPOSITION layer, not a parallel store:

* **Recording** — every VALIDATE failure and VERIFY regression becomes a
  :class:`failure_mode_memory.FailureModeRecord` through the arc's own
  primitives (situation classification, closed failure-mode taxonomy,
  signature hash, flock'd dedup/weight merge, decay) plus the open-set
  ``error_class`` taxonomy below (API signature mismatch, input-shape
  mismatch, expected-value mismatch, ...) and an evidence excerpt — the
  structured lesson. Persisted in the arc's durable JSONL store and
  indexed as a human-readable ``LESSONS.md`` beside it (§7 absolute
  observability: every memory is a plain file a human can read or delete).
* **Retrieval (RAG)** — :func:`inject_lessons` runs asynchronously in
  ``CandidateGenerator.generate`` BEFORE the provider builds its prompt and
  AST-Signature Anchor. Lessons are ranked by module overlap (test/impl
  stems unified: ``test_model_physics.py`` ↔ ``model_physics.py``), same
  situation kind as a cross-module fallback, recency half-life and
  recurrence weight, then composed into an immutable ``## LESSONS LEARNED``
  block on the existing ``strategic_memory_prompt`` channel the providers
  already inject.
* **Resilience** — a locked or corrupt store, a slow disk, or any fault
  degrades to the base prompt: retrieval is bounded by a timeout, every
  failure logs a telemetry warning, and the generation pipeline never sees
  an exception. Corrupt lines are skipped by the arc's reader; the dedup
  merge holds the arc's cross-process flock.

Env (all optional): JARVIS_LESSON_MEMORY_ENABLED (true),
JARVIS_LESSON_MEMORY_TOP_K (4), JARVIS_LESSON_MEMORY_MAX_CHARS (2400),
JARVIS_LESSON_MEMORY_TIMEOUT_S (2.5), JARVIS_LESSON_MEMORY_MIN_WEIGHT (1),
JARVIS_LESSON_MEMORY_HALFLIFE_DAYS (30), JARVIS_LESSON_MEMORY_EVIDENCE_CHARS
(280), JARVIS_LESSON_MEMORY_SITUATION_RELEVANCE (0.4).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.LessonMemory")

_ENV_ENABLED = "JARVIS_LESSON_MEMORY_ENABLED"
_ENV_TOP_K = "JARVIS_LESSON_MEMORY_TOP_K"
_ENV_MAX_CHARS = "JARVIS_LESSON_MEMORY_MAX_CHARS"
_ENV_TIMEOUT = "JARVIS_LESSON_MEMORY_TIMEOUT_S"
_ENV_MIN_WEIGHT = "JARVIS_LESSON_MEMORY_MIN_WEIGHT"
_ENV_HALFLIFE = "JARVIS_LESSON_MEMORY_HALFLIFE_DAYS"
_ENV_EVIDENCE = "JARVIS_LESSON_MEMORY_EVIDENCE_CHARS"
_ENV_SITUATION_REL = "JARVIS_LESSON_MEMORY_SITUATION_RELEVANCE"
_ENV_BOOST = "JARVIS_LESSON_MEMORY_BOOST"
_ENV_ESCALATE_AFTER = "JARVIS_LESSON_MEMORY_ESCALATE_AFTER"
_ENV_MAX_SEVERITY = "JARVIS_LESSON_MEMORY_MAX_SEVERITY"
_ENV_ENTRY_CHARS = "JARVIS_LESSON_MEMORY_ENTRY_CHARS"
_ENV_REGISTRY_MAX = "JARVIS_LESSON_MEMORY_REGISTRY_MAX"

SECTION_HEADER = "## LESSONS LEARNED (cross-op memory — do not repeat these failures)"
INDEX_FILENAME = "LESSONS.md"
INTENT_ID = "lesson-memory-v1"


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def _int(name: str, default: int, lo: int = 0) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return v if v >= lo else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except ValueError:
        return default


def top_k() -> int:
    return _int(_ENV_TOP_K, 4, 1)


def max_chars() -> int:
    return _int(_ENV_MAX_CHARS, 2400, 200)


def timeout_s() -> float:
    return _float(_ENV_TIMEOUT, 2.5)


def min_weight() -> int:
    return _int(_ENV_MIN_WEIGHT, 1, 1)


def halflife_days() -> float:
    return _float(_ENV_HALFLIFE, 30.0)


def evidence_chars() -> int:
    return _int(_ENV_EVIDENCE, 280, 40)


def situation_relevance() -> float:
    v = _float(_ENV_SITUATION_REL, 0.4)
    return min(1.0, v)


def boost() -> int:
    """Weight added to a lesson when an injection is followed by a pass."""
    return _int(_ENV_BOOST, 2, 1)


def escalate_after() -> int:
    """Consecutive unresolved injections before the severity modifier
    rises one level."""
    return _int(_ENV_ESCALATE_AFTER, 2, 1)


def max_severity() -> int:
    """Hard clamp on the severity modifier (prompt-starvation guard)."""
    return _int(_ENV_MAX_SEVERITY, 3, 1)


def entry_chars() -> int:
    """Per-lesson render cap inside the block budget."""
    return _int(_ENV_ENTRY_CHARS, 600, 120)


def registry_max() -> int:
    return _int(_ENV_REGISTRY_MAX, 512, 16)


# ---------------------------------------------------------------------------
# Error-class taxonomy (open-set; the closed failure-mode enum stays closed)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: Ordered (class, pattern) chain — first match wins. Patterns describe the
#: SHAPE of a failure, never a module or symbol name.
_ERROR_CLASSES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("no_tests_collected", re.compile(r"no tests ran|collected 0 items|found no collectors|no tests collected", re.I)),
    ("syntax_error", re.compile(r"\b(SyntaxError|IndentationError)\b")),
    ("import_error", re.compile(r"\b(ModuleNotFoundError|ImportError: No module)\b")),
    ("api_signature_mismatch", re.compile(
        r"TypeError: .*?(unexpected keyword|positional argument|takes \d+|missing \d+ required|not callable)"
        r"|AttributeError: .*has no attribute|NameError: name .* is not defined|ImportError: cannot import name", re.S)),
    ("verify_regression", re.compile(r"verify_regression|regression gate", re.I)),
    ("input_shape_mismatch", re.compile(r"assert None is not None|assert .*? is not None|returned None|is None\b.*assert", re.S)),
    # operands may carry spaces (enum reprs, lists): match the whole clause
    ("expected_value_mismatch", re.compile(r"\bassert .+? (?:==|!=|<=|>=|<|>) .+")),
    ("timeout", re.compile(r"\b(timed out|Timeout)\b", re.I)),
    ("assertion_failure", re.compile(r"AssertionError|\bassert\b")),
)

#: Guidance per error class — what to do INSTEAD. Generic by construction:
#: the module-specific facts live in the record's evidence excerpt.
_MITIGATIONS: Dict[str, str] = {
    "no_tests_collected": "Name test functions `test_*` in a `test_*.py` under the tests tree and import the real module; a file that collects nothing fails VALIDATE.",
    "syntax_error": "Emit complete, syntactically valid Python; re-read the file end to end before returning it.",
    "import_error": "Import only modules that exist in this repo (check the AUTHORITATIVE API SIGNATURES block); never invent packages.",
    "api_signature_mismatch": "Call the API EXACTLY as listed in the AUTHORITATIVE API SIGNATURES block — same names, argument count/order, return shape. Do not invent parameters or attributes.",
    "verify_regression": "The change broke previously-passing tests: keep the existing behaviour intact and add, never alter, semantics outside the declared target.",
    "input_shape_mismatch": "Build inputs from the `# input shape:` skeleton and `# reads:` keys — exact nesting, literal dotted keys — so the parser returns a value instead of None.",
    "expected_value_mismatch": "Compute expected values INSIDE the test from the `# returns:` formulas and your own inputs; never hand-compute a numeric literal.",
    "timeout": "Keep tests fast and free of network/subprocess/sleep; the sandbox budget is bounded.",
    "assertion_failure": "Assert only on documented behaviour you can derive from the signatures, docstrings and formulas provided.",
    "exception": "Read the previous failure evidence carefully before retrying; do not repeat the same construction.",
}


def classify_error(text: str) -> str:
    """Open-set error class for failure evidence. ``exception`` when nothing
    more specific matches. NEVER raises."""
    try:
        clean = _ANSI_RE.sub("", text or "")
        for name, pat in _ERROR_CLASSES:
            if pat.search(clean):
                return name
    except Exception:  # noqa: BLE001
        pass
    return "exception"


def evidence_excerpt(*parts: str, limit: Optional[int] = None) -> str:
    """The most informative lines of the failure evidence, single-line,
    bounded — the lesson body a future op reads."""
    cap = limit or evidence_chars()
    lines: List[str] = []
    for part in parts:
        for raw in _ANSI_RE.sub("", part or "").splitlines():
            s = " ".join(raw.split())
            if not s or s.startswith(("=====", "-----", "INFO", "DEBUG")):
                continue
            if s not in lines:
                lines.append(s)
    text = " | ".join(lines)
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# Module keys — unify test and implementation stems
# ---------------------------------------------------------------------------

def module_keys(paths: Iterable[str]) -> frozenset:
    """``tests/governance/test_model_physics.py`` and
    ``backend/.../model_physics.py`` both yield ``model_physics`` — the key a
    lesson about a module is retrieved by, regardless of which side of the
    test/impl boundary the op touches."""
    out = set()
    for p in paths or ():
        try:
            stem = Path(str(p)).stem.lower()
        except Exception:  # noqa: BLE001
            continue
        if not stem:
            continue
        for prefix in ("test_", "tests_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
        for suffix in ("_test", "_tests"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem and stem not in ("__init__", "conftest"):
            out.add(stem)
    return frozenset(out)


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b)) if inter else 0.0


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def build_lesson_record(
    *, op_id: str, target_files: Sequence[str], phase: str, failure_class: str,
    error_text: str, summary: str = "", now_ts: Optional[float] = None,
) -> Optional[Any]:
    """A :class:`FailureModeRecord` carrying the structured lesson. ``None``
    when the arc is unavailable. NEVER raises."""
    try:
        from backend.core.ouroboros.governance import failure_mode_memory as fmm
        files = tuple(str(f) for f in target_files if f)
        situation = fmm.classify_situation_from_ctx(target_files=files)
        error_class = classify_error((error_text or "") + "\n" + (summary or ""))
        kind = fmm._classify_failure_mode(root_cause=error_text or summary or "")
        mitigation = _MITIGATIONS.get(error_class, _MITIGATIONS["exception"])
        if kind is not fmm.FailureModeKind.OTHER:
            mitigation = fmm._derive_mitigation(kind) + " " + mitigation
        action = f"{(phase or 'validate').lower()}:{error_class}"
        return fmm.FailureModeRecord(
            signature_hash=fmm.compute_signature_hash(
                situation_kind=situation, attempted_action_kind=action, target_files=files,
            ),
            situation_kind=situation,
            attempted_action_kind=action,
            failure_mode_kind=kind,
            mitigation_summary=mitigation,
            observed_at_unix=float(now_ts if now_ts is not None else time.time()),
            op_id=str(op_id or ""),
            weight=1,
            target_files=files,
            error_class=error_class,
            lesson=evidence_excerpt(summary, error_text),
            phase=(phase or "VALIDATE").upper(),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[LessonMemory] build_lesson_record degraded", exc_info=True)
        return None


def record_lesson_sync(record: Any) -> str:
    """Persist through the arc's flock'd dedup/weight merge, then refresh the
    human-readable index. Returns the arc's outcome value. NEVER raises."""
    try:
        from backend.core.ouroboros.governance import failure_mode_memory as fmm
        outcome = fmm.record_failure_mode(record)
        try:
            render_index()
        except Exception:  # noqa: BLE001
            logger.debug("[LessonMemory] index render degraded", exc_info=True)
        return getattr(outcome, "value", str(outcome))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LessonMemory] record degraded (%s) — lesson not persisted", exc)
        return "error"


async def record_lesson(
    *, op_id: str, target_files: Sequence[str], phase: str, failure_class: str,
    error_text: str, summary: str = "",
) -> str:
    """Async, bounded, fail-soft recording seam for the orchestrator."""
    if not enabled():
        return "disabled"
    rec = build_lesson_record(
        op_id=op_id, target_files=target_files, phase=phase, failure_class=failure_class,
        error_text=error_text, summary=summary,
    )
    if rec is None:
        return "unavailable"
    try:
        outcome = await asyncio.wait_for(asyncio.to_thread(record_lesson_sync, rec), timeout=timeout_s())
        logger.info(
            "[LessonMemory] recorded %s lesson class=%s modules=%s outcome=%s op=%s",
            rec.phase, rec.error_class, ",".join(sorted(module_keys(rec.target_files))) or "-",
            outcome, str(op_id)[:12],
        )
        return outcome
    except asyncio.TimeoutError:
        logger.warning("[LessonMemory] record timed out after %.1fs (store busy) — lesson may be lost", timeout_s())
        return "timeout"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LessonMemory] record degraded (%s)", exc)
        return "error"


# ---------------------------------------------------------------------------
# Retrieval (RAG)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LessonMatch:
    record: Any
    relevance: float
    recency: float
    weight_score: float

    @property
    def score(self) -> float:
        return self.relevance * self.recency * self.weight_score


def retrieve_lessons_sync(
    target_files: Sequence[str], *, situation_kind: Any = None,
    now_ts: Optional[float] = None,
) -> Tuple[LessonMatch, ...]:
    """Ranked lessons for an op: module overlap first, same situation kind
    as the cross-module fallback (never the UNKNOWN sentinel), recency
    half-life and recurrence weight. Deduped per (error_class, modules)."""
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    files = tuple(str(f) for f in target_files if f)
    keys = module_keys(files)
    if situation_kind is None:
        situation_kind = fmm.classify_situation_from_ctx(target_files=files)
    now = float(now_ts if now_ts is not None else time.time())
    hl = halflife_days()
    sit_rel = situation_relevance()
    unknown = getattr(fmm.SituationKind, "UNKNOWN", None)
    best: Dict[Tuple[str, frozenset], LessonMatch] = {}
    for rec in fmm.read_failure_mode_history():
        if int(getattr(rec, "weight", 1)) < min_weight():
            continue
        rec_keys = module_keys(getattr(rec, "target_files", ()) or ())
        rel = _overlap(keys, rec_keys)
        if rel <= 0.0 and situation_kind is not None and rec.situation_kind is situation_kind and situation_kind is not unknown:
            rel = sit_rel
        if rel <= 0.0:
            continue
        age_s = max(0.0, now - float(rec.observed_at_unix))
        recency = fmm._recency_weight(age_s, hl)
        m = LessonMatch(record=rec, relevance=rel, recency=recency, weight_score=fmm._weight_score(rec.weight))
        k = (getattr(rec, "error_class", "") or rec.failure_mode_kind.value, rec_keys)
        if k not in best or m.score > best[k].score:
            best[k] = m
    ranked = sorted(best.values(), key=lambda m: m.score, reverse=True)
    return tuple(ranked[: top_k()])


# ---------------------------------------------------------------------------
# Lesson Confidence Scorer — reinforcement + escalation on the flock store
# ---------------------------------------------------------------------------

#: op_id -> (signature hashes injected, monotonic ts). The context stamped in
#: CandidateGenerator.generate is a local copy, so VALIDATE learns which
#: lessons this op saw from here. Bounded (registry_max), oldest evicted.
_INJECTED: Dict[str, Tuple[Tuple[str, ...], float]] = {}

_ASSERT_EQ_RE = re.compile(r"assert (.+?) (==|!=|<=|>=|<|>) (.+?)(?: \||$)")


def _remember_injection(op_id: str, sigs: Sequence[str]) -> None:
    if not op_id:
        return
    _INJECTED[op_id] = (tuple(sigs), time.monotonic())
    cap = registry_max()
    while len(_INJECTED) > cap:
        oldest = min(_INJECTED, key=lambda k: _INJECTED[k][1])
        _INJECTED.pop(oldest, None)


def injected_signatures(op_id: str) -> Tuple[str, ...]:
    return _INJECTED.get(op_id, ((), 0.0))[0]


def _note_injection_sync(sig: str) -> Optional[int]:
    """Durable: injections+1, unresolved_streak+1, escalate when the streak
    reaches ``escalate_after`` (clamped at ``max_severity``). Returns the
    new severity or None."""
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    from dataclasses import replace as _replace
    out: Dict[str, int] = {}

    def _mut(rec):
        streak = int(rec.unresolved_streak) + 1
        sev = int(rec.severity)
        if streak >= escalate_after():
            sev = min(max_severity(), sev + 1)
            streak = 0  # each escalation restarts the streak
        out["sev"] = sev
        return _replace(rec, injections=int(rec.injections) + 1, unresolved_streak=streak, severity=sev)

    return out.get("sev") if fmm.update_failure_mode(sig, _mut) else None


def _reinforce_sync(sig: str) -> bool:
    """Durable: a VALIDATE pass after injection — weight += boost,
    resolutions+1, streak reset, severity decays one level."""
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    from dataclasses import replace as _replace
    return fmm.update_failure_mode(sig, lambda rec: _replace(
        rec, weight=int(rec.weight) + boost(), resolutions=int(rec.resolutions) + 1,
        unresolved_streak=0, severity=max(0, int(rec.severity) - 1),
    ))


async def note_injections(op_id: str, sigs: Sequence[str]) -> Dict[str, int]:
    """Async, bounded, fail-soft. Returns ``{sig: new_severity}``."""
    _remember_injection(op_id, sigs)
    out: Dict[str, int] = {}
    for sig in sigs:
        try:
            sev = await asyncio.wait_for(asyncio.to_thread(_note_injection_sync, sig), timeout=timeout_s())
            if sev is not None:
                out[sig] = sev
        except asyncio.TimeoutError:
            logger.warning("[LessonMemory] injection bookkeeping timed out (store busy) for %s", sig[:12])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[LessonMemory] injection bookkeeping degraded (%s)", exc)
    return out


async def reinforce_validation_pass(op_id: str) -> int:
    """VALIDATE passed for an op that had lessons injected: boost those
    lessons' confidence in the flock store. Returns the count boosted.
    NEVER raises."""
    if not enabled():
        return 0
    sigs = injected_signatures(op_id)
    if not sigs:
        return 0
    n = 0
    for sig in sigs:
        try:
            if await asyncio.wait_for(asyncio.to_thread(_reinforce_sync, sig), timeout=timeout_s()):
                n += 1
        except asyncio.TimeoutError:
            logger.warning("[LessonMemory] reinforcement timed out (store busy) for %s", sig[:12])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[LessonMemory] reinforcement degraded (%s)", exc)
    _INJECTED.pop(op_id, None)
    if n:
        logger.info("[LessonMemory] reinforced %d lesson(s) after VALIDATE pass op=%s", n, str(op_id)[:12])
    return n


def negative_constraint(rec: Any) -> str:
    """The hard negative constraint derived from the lesson's evidence: for
    an assertion mismatch, the literal expected value that was wrong and
    the value actually observed; otherwise the evidence itself."""
    lesson = (getattr(rec, "lesson", "") or "").strip()
    m = _ASSERT_EQ_RE.search(lesson)
    if m:
        actual, op, expected = m.group(1).strip(), m.group(2), m.group(3).strip()
        return (
            f"NEVER assert `{op} {expected}` here — the value actually observed was `{actual}`. "
            f"Do not hardcode that expectation; assert on the observed shape/type, or stub the dependency."
        )
    return f"NEVER repeat this construction: {lesson}" if lesson else "NEVER repeat the recorded failure."


def _render_entry(rec: Any, now: float) -> str:
    mods = ",".join(sorted(module_keys(getattr(rec, "target_files", ()) or ()))) or rec.situation_kind.value
    cls = getattr(rec, "error_class", "") or rec.failure_mode_kind.value
    lesson = (getattr(rec, "lesson", "") or "").strip()
    age = _age_label(max(0.0, now - float(rec.observed_at_unix)))
    sev = min(max_severity(), int(getattr(rec, "severity", 0) or 0))
    mitigation = (rec.mitigation_summary or "").strip()
    if sev >= 2:
        entry = (
            f"- ⛔ HARD CONSTRAINT [{cls}] {mods} ({getattr(rec, 'phase', '') or 'VALIDATE'}, seen x{rec.weight}, "
            f"injected {int(getattr(rec, 'injections', 0) or 0)}x without resolution, {age}): {negative_constraint(rec)}"
            f"\n  A candidate that violates this is REJECTED. Then: {mitigation}"
        )
    elif sev == 1:
        entry = (
            f"- REQUIRED [{cls}] {mods} ({getattr(rec, 'phase', '') or 'VALIDATE'}, seen x{rec.weight}, {age})"
            + (f": {lesson}" if lesson else "") + f"\n  You MUST: {mitigation}"
        )
    else:
        entry = (
            f"- [{cls}] {mods} ({getattr(rec, 'phase', '') or 'VALIDATE'}, seen x{rec.weight}, {age})"
            + (f": {lesson}" if lesson else "") + f"\n  Do instead: {mitigation}"
        )
    cap = entry_chars()
    return entry if len(entry) <= cap else entry[: cap - 1].rstrip() + "…"


def _age_label(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def compose_lessons_block(matches: Iterable[LessonMatch], *, budget: Optional[int] = None, now_ts: Optional[float] = None) -> str:
    """The immutable prompt block. ``""`` when nothing qualifies."""
    items = list(matches)
    if not items:
        return ""
    cap = budget or max_chars()
    now = float(now_ts if now_ts is not None else time.time())
    lines = [
        SECTION_HEADER, "",
        "These are REAL failures recorded from earlier operations on the same modules "
        "or situation. Each line states what failed and what to do instead. Treat them "
        "as constraints, not suggestions.", "",
    ]
    used = sum(len(l) + 1 for l in lines)
    n = 0
    # Escalated lessons first: a hard constraint must survive the budget
    # clamp ahead of advisories. Within a level, best score first.
    items.sort(key=lambda m: (-min(max_severity(), int(getattr(m.record, "severity", 0) or 0)), -m.score))
    for m in items:
        entry = _render_entry(m.record, now)
        if used + len(entry) + 1 > cap:
            continue  # try a smaller later entry; the budget is the clamp
        lines.append(entry)
        used += len(entry) + 1
        n += 1
    return "\n".join(lines) if n else ""


async def retrieve_lessons(target_files: Sequence[str], *, situation_kind: Any = None) -> Tuple[LessonMatch, ...]:
    """Bounded async retrieval. NEVER raises; degrades to ``()`` with a
    telemetry warning."""
    if not enabled():
        return ()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(retrieve_lessons_sync, target_files, situation_kind=situation_kind),
            timeout=timeout_s(),
        )
    except asyncio.TimeoutError:
        logger.warning("[LessonMemory] retrieval timed out after %.1fs (store busy/locked) — generation proceeds with the base anchor", timeout_s())
        return ()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LessonMemory] retrieval degraded (%s) — generation proceeds with the base anchor", exc)
        return ()


async def inject_lessons(context: Any) -> Any:
    """The Memory RAG hook: return *context* with the lessons block stamped
    on its ``strategic_memory_prompt`` channel (the providers already
    inject that channel into the codegen prompt), or the SAME context when
    nothing qualifies or anything degrades. NEVER raises."""
    if not enabled() or context is None:
        return context
    try:
        existing = getattr(context, "strategic_memory_prompt", "") or ""
        if SECTION_HEADER in existing:
            return context
        matches = await retrieve_lessons(tuple(getattr(context, "target_files", ()) or ()))
        block = compose_lessons_block(matches)
        if not block:
            return context
        prompt = (existing + "\n\n" + block) if existing.strip() else block
        fact_ids = tuple(getattr(context, "strategic_memory_fact_ids", ()) or ()) + tuple(
            "lesson:" + (m.record.signature_hash or "")[:16] for m in matches
        )
        stamped = context.with_strategic_memory_context(
            strategic_intent_id=getattr(context, "strategic_intent_id", "") or INTENT_ID,
            strategic_memory_fact_ids=fact_ids,
            strategic_memory_prompt=prompt,
            strategic_memory_digest=hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest(),
        )
        # Confidence scorer: count this injection durably; escalate lessons
        # that keep being injected without resolving the failure.
        escalated = await note_injections(
            str(getattr(context, "op_id", "") or ""),
            [m.record.signature_hash for m in matches if m.record.signature_hash],
        )
        for _sig, _sev in escalated.items():
            if _sev >= 2:
                logger.warning("[LessonMemory] lesson %s escalated to severity %d (hard constraint) — unresolved after repeated injection", _sig[:12], _sev)
        logger.info(
            "[LessonMemory] injected %d lesson(s) (%d chars) for modules=%s op=%s",
            len(matches), len(block), ",".join(sorted(module_keys(getattr(context, "target_files", ()) or ()))) or "-",
            str(getattr(context, "op_id", ""))[:12],
        )
        return stamped
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LessonMemory] injection degraded (%s) — generation proceeds with the base anchor", exc)
        return context


# ---------------------------------------------------------------------------
# Local memory layer index (human-readable)
# ---------------------------------------------------------------------------

def index_path() -> Path:
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    return fmm.history_dir() / INDEX_FILENAME


def render_index(limit: int = 200) -> Optional[Path]:
    """Regenerate ``LESSONS.md`` from the store — newest first, one line per
    lesson — so a human can read, audit or delete the organism's beliefs."""
    from backend.core.ouroboros.governance import failure_mode_memory as fmm
    recs = list(fmm.read_failure_mode_history())
    if not recs:
        return None
    recs.sort(key=lambda r: r.observed_at_unix, reverse=True)
    out = [
        "# O+V Lessons Learned (auto-generated index of the failure-mode memory)", "",
        f"{len(recs)} lesson(s). Source of truth: `{fmm.history_path()}`. Delete a line there to forget it.", "",
    ]
    for r in recs[:limit]:
        mods = ",".join(sorted(module_keys(getattr(r, "target_files", ()) or ()))) or r.situation_kind.value
        ts = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(r.observed_at_unix)))
        cls = getattr(r, "error_class", "") or r.failure_mode_kind.value
        out.append(f"- {ts} **{cls}** `{mods}` x{r.weight} ({getattr(r, 'phase', '') or 'VALIDATE'}): {getattr(r, 'lesson', '') or r.mitigation_summary}")
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p
