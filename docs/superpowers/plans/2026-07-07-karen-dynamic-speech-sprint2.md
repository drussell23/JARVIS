# Karen Full-Duplex Voice — Sprint 2: Dynamic LLM Speech + Persona — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the 13 static `narrator_script.py` templates with a `KarenSpeechSynthesizer` that dynamically synthesizes concise, persona-driven speech from the live `OperationContext` FSM ledger via the DoubleWord provider, spoken through the Sprint-1 arbiter with zero-latency local fillers masking the LLM round-trip.

**Architecture:** A compression-first pipeline: `OperationContext` → `LedgerView` (safe, code-stripped projection) → persona system prompt → `DWSpeechProvider.complete()` (DW `complete_sync`, full-payload) → provider-agnostic sentence chunker → sentences submitted to the `VoiceDuplexArbiter`. The arbiter fires a **local** filler ("On it") the instant synthesis starts, independent of the network call. The chunker accepts either a single payload (DW today) or a token stream (Claude later) — stream-ready abstraction, DW-primary now.

**Tech Stack:** Python 3.9+, asyncio, existing `DoublewordProvider.complete_sync`, `jarvis_personality_adapter`, the Sprint-1 `VoiceDuplexArbiter`. No new network deps.

## Global Constraints

- Python 3.9+, `from __future__ import annotations` every file. Pure asyncio, no blocking calls.
- **DW-primary (Option A):** speech text comes from `DoublewordProvider.complete_sync(prompt, *, system_prompt, caller_id, max_tokens, ...)`. The `SpeechProvider` abstraction MUST be stream-ready (a future streaming impl yields tokens) — do NOT hardcode the pipeline to full-payload behavior.
- **Fillers are local + LLM-free:** fired by `VoiceDuplexArbiter`, a small rotating set, interruptible (ordinary SpeechRequests). NEVER generate fillers via the LLM.
- **Compression is mandatory (mandate #4):** `LedgerView` strips code blocks / stack traces; root_cause → first line only; files → basenames; every field length-capped. The system prompt hard-forbids reading code/tracebacks aloud and caps output to ~2 short sentences.
- **Eradicate `narrator_script.py` entirely** — no legacy template wrapper survives. Its one production consumer (`voice_narrator.py:_narrate_one`), 2 `__init__` re-exports, and 3 test files are the full blast radius (from the Sprint-2 integration map).
- Fault isolation: synth failure → arbiter speaks nothing (silence), never raises into the FSM.
- Kill switch: `JARVIS_KAREN_SYNTH_ENABLED` (default false during build).

---

## File Structure

New package `backend/core/ouroboros/governance/comms/karen_synth/`:
- `ledger_view.py` — `LedgerView` safe/compressed projection of `OperationContext` + `CommMessage` payload.
- `persona.py` — persona system-prompt + safety/compression rules; `build_prompt(view) -> (system, user)`.
- `sentence_chunker.py` — provider-agnostic `stream_sentences(async_source)`.
- `speech_provider.py` — `SpeechProvider` protocol + `DWSpeechProvider` (wraps `complete_sync`).
- `synthesizer.py` — `KarenSpeechSynthesizer` orchestrator.
- `safe_say_playback.py` — minimal `PlaybackHandle` backed by `safe_say` (bridges the arbiter to audio; Sprint 3 upgrades to streaming TTS).

Modified:
- `backend/core/ouroboros/governance/comms/voice_narrator.py` — `_narrate_one` uses the synthesizer + arbiter, not `format_narration`.
- `backend/core/ouroboros/governance/comms/duplex/arbiter.py` — add `fire_filler()`.
- `backend/core/ouroboros/governance/comms/__init__.py`, `governance/__init__.py` — remove `format_narration`/`SCRIPTS` re-exports.
- Delete: `narrator_script.py` + `tests/governance/comms/test_narrator_script.py`; fix `test_exports.py`, `test_daemon_narrator_wiring.py`.

---

### Task 1: `LedgerView` — safe, compressed FSM projection

**Files:** Create `.../karen_synth/__init__.py`, `.../karen_synth/ledger_view.py`; Test `tests/governance/comms/karen_synth/test_ledger_view.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class LedgerView`: `phase: str`, `goal: str=""`, `files: Tuple[str,...]=()`, `risk_tier: str=""`, `provider: str=""`, `outcome: str=""`, `root_cause: str=""`
- `@classmethod from_payload(cls, phase: str, payload: Mapping) -> LedgerView`
- `def to_context_line(self) -> str` — compact, code-free
- module helpers: `strip_code(text) -> str`, `first_line(text) -> str`

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/karen_synth/test_ledger_view.py
from __future__ import annotations
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import (
    LedgerView, strip_code, first_line,
)

def test_strip_code_removes_fences_and_traceback():
    raw = "It failed.\n```\nTraceback (most recent call last):\n  File x\n```\nmore"
    out = strip_code(raw)
    assert "Traceback" not in out and "```" not in out and "File x" not in out
    assert "It failed." in out

def test_first_line_only():
    assert first_line("line one\nline two\nline three") == "line one"

def test_from_payload_basenames_and_compresses_root_cause():
    v = LedgerView.from_payload("postmortem", {
        "root_cause": "AssertionError in test\n```\nassert 1==2\n```\nstack...",
        "target_files": ["backend/core/auth/login.py", "backend/util/x.py"],
        "risk_tier": "notify_apply",
    })
    assert v.files == ("login.py", "x.py")            # basenames
    assert "```" not in v.root_cause                  # code stripped
    assert "\n" not in v.root_cause                   # single line
    assert v.phase == "postmortem"

def test_to_context_line_has_no_code_or_newlines():
    v = LedgerView.from_payload("decision", {"outcome": "applied", "target_files": ["a/b.py"]})
    line = v.to_context_line()
    assert "```" not in line and "\n" not in line
    assert "b.py" in line and "applied" in line
```
- [ ] **Step 2: run → FAIL** (`ModuleNotFoundError`). `python3 -m pytest tests/governance/comms/karen_synth/test_ledger_view.py -q`
- [ ] **Step 3: implement**
```python
# backend/core/ouroboros/governance/comms/karen_synth/__init__.py
"""Karen dynamic speech synthesis (Sprint 2). Replaces narrator_script.py's
static templates with LLM-synthesized, persona-driven, compressed speech."""
from __future__ import annotations
```
```python
# backend/core/ouroboros/governance/comms/karen_synth/ledger_view.py
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Tuple

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)
_MAX_FIELD = 160


def strip_code(text: str) -> str:
    """Remove fenced code blocks + Python tracebacks so they are never spoken
    (mandate #4 — a stack trace would freeze the TTS buffer). NEVER raises."""
    try:
        t = _FENCE.sub(" ", text or "")
        t = _TRACEBACK.sub(" ", t)
        return " ".join(t.split())
    except Exception:  # noqa: BLE001
        return ""


def first_line(text: str) -> str:
    try:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s:
                return s
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _basename(p: str) -> str:
    return os.path.basename(str(p)) if p else ""


def _cap(s: str) -> str:
    s = strip_code(s)
    return s[:_MAX_FIELD].rstrip()


@dataclass(frozen=True)
class LedgerView:
    phase: str
    goal: str = ""
    files: Tuple[str, ...] = ()
    risk_tier: str = ""
    provider: str = ""
    outcome: str = ""
    root_cause: str = ""

    @classmethod
    def from_payload(cls, phase: str, payload: Mapping) -> "LedgerView":
        p = payload or {}
        files = tuple(_basename(f) for f in (p.get("target_files") or ()) if f)
        return cls(
            phase=str(phase or ""),
            goal=_cap(str(p.get("goal", ""))),
            files=files[:5],
            risk_tier=str(p.get("risk_tier", "") or ""),
            provider=str(p.get("provider", "") or ""),
            outcome=str(p.get("outcome", "") or ""),
            root_cause=_cap(first_line(str(p.get("root_cause", "")))),
        )

    def to_context_line(self) -> str:
        parts = [f"phase={self.phase}"]
        if self.goal:
            parts.append(f"goal={self.goal}")
        if self.files:
            parts.append("files=" + ",".join(self.files))
        if self.risk_tier:
            parts.append(f"risk={self.risk_tier}")
        if self.outcome:
            parts.append(f"outcome={self.outcome}")
        if self.root_cause:
            parts.append(f"cause={self.root_cause}")
        return " ".join(parts)
```
- [ ] **Step 4: run → PASS**
- [ ] **Step 5: commit** — `git add` the two source files + test; `git commit -m "feat(karen): LedgerView — safe compressed FSM projection (Sprint 2 Task 1)"`

---

### Task 2: Persona + prompt builder

**Files:** Create `.../karen_synth/persona.py`; Test `tests/governance/comms/karen_synth/test_persona.py`.

**Interfaces — Consumes:** `LedgerView` (Task 1). **Produces:** `build_prompt(view: LedgerView, persona_ctx: Mapping | None = None) -> tuple[str, str]` (system, user).

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/karen_synth/test_persona.py
from __future__ import annotations
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import LedgerView
from backend.core.ouroboros.governance.comms.karen_synth.persona import build_prompt

def test_system_prompt_encodes_persona_and_safety_rules():
    sys, _ = build_prompt(LedgerView(phase="generate"))
    low = sys.lower()
    assert "australian" in low
    assert "concise" in low or "brief" in low
    # mandate #4 hard rules present:
    assert "never" in low and ("code" in low and ("stack" in low or "traceback" in low))
    assert "two sentences" in low or "2 sentences" in low or "one or two" in low

def test_user_prompt_carries_ledger_context_no_code():
    sys, user = build_prompt(LedgerView.from_payload(
        "postmortem", {"root_cause": "boom ```code```", "target_files": ["a/x.py"]}))
    assert "x.py" in user
    assert "```" not in user and "```" not in sys

def test_persona_ctx_injected_when_present():
    sys, _ = build_prompt(LedgerView(phase="intent"), {"user_name": "Derek", "time_of_day": "evening"})
    assert "Derek" in sys and "evening" in sys
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement**
```python
# backend/core/ouroboros/governance/comms/karen_synth/persona.py
from __future__ import annotations

from typing import Mapping, Optional, Tuple

from .ledger_view import LedgerView

_PERSONA = (
    "You are Karen, the spoken voice of an autonomous engineering organism. "
    "You are an Australian senior engineer: concise, dryly witty, highly "
    "technical, zero fluff, maximal signal-to-noise. You respect the "
    "listener's time."
)

_SAFETY = (
    "HARD RULES. Output ONE or TWO short spoken sentences, nothing more. "
    "NEVER read code, file contents, stack traces, tracebacks, hashes, or long "
    "identifiers aloud — summarise them in plain words. No markdown, no lists, "
    "no code fences. Speak as if talking, not writing. If there is nothing "
    "worth saying, reply with a single short phrase."
)


def build_prompt(
    view: LedgerView, persona_ctx: Optional[Mapping] = None,
) -> Tuple[str, str]:
    """Return (system_prompt, user_prompt). System encodes persona + the
    mandate-#4 safety rules; user carries the compressed ledger context."""
    ctx_bits = []
    if persona_ctx:
        for k in ("user_name", "time_of_day", "mode"):
            v = persona_ctx.get(k)
            if v:
                ctx_bits.append(f"{k}={v}")
    ctx = (" Context: " + ", ".join(ctx_bits) + ".") if ctx_bits else ""
    system = f"{_PERSONA}{ctx} {_SAFETY}"
    user = (
        "Narrate this development event to the listener in your voice: "
        + view.to_context_line()
    )
    return system, user
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): persona + safety-constrained prompt builder (Task 2)`

---

### Task 3: Provider-agnostic sentence chunker

**Files:** Create `.../karen_synth/sentence_chunker.py`; Test `tests/governance/comms/karen_synth/test_sentence_chunker.py`.

**Interfaces — Produces:** `async def stream_sentences(source: AsyncIterable[str]) -> AsyncIterator[str]`; `async def single_payload(text: str) -> AsyncIterator[str]`.

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/karen_synth/test_sentence_chunker.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.sentence_chunker import (
    stream_sentences, single_payload,
)

async def _tokens(chunks):
    for c in chunks:
        yield c

@pytest.mark.asyncio
async def test_token_stream_emits_complete_sentences():
    src = _tokens(["Fix ", "applied", ". Tests ", "green", "!"])
    got = [s async for s in stream_sentences(src)]
    assert got == ["Fix applied.", "Tests green!"]

@pytest.mark.asyncio
async def test_single_payload_splits_into_sentences():
    got = [s async for s in stream_sentences(single_payload("One thing. Two things?"))]
    assert got == ["One thing.", "Two things?"]

@pytest.mark.asyncio
async def test_trailing_text_without_terminator_is_flushed():
    got = [s async for s in stream_sentences(_tokens(["no period here"]))]
    assert got == ["no period here"]
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement**
```python
# backend/core/ouroboros/governance/comms/karen_synth/sentence_chunker.py
from __future__ import annotations

import re
from typing import AsyncIterable, AsyncIterator

_BOUNDARY = re.compile(r"(.+?[.!?])(\s+|$)", re.DOTALL)


async def single_payload(text: str) -> AsyncIterator[str]:
    """Adapt a full-payload string (DW) into a one-shot async source."""
    yield text


async def stream_sentences(source: AsyncIterable[str]) -> AsyncIterator[str]:
    """Accumulate incoming text chunks and yield complete sentences as their
    boundaries arrive. Works for a token stream (Claude) or a single payload
    (DW). Flushes any trailing non-terminated remainder at the end."""
    buf = ""
    async for chunk in source:
        buf += chunk or ""
        while True:
            m = _BOUNDARY.match(buf)
            if not m:
                break
            sentence = m.group(1).strip()
            buf = buf[m.end():]
            if sentence:
                yield sentence
    tail = buf.strip()
    if tail:
        yield tail
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): provider-agnostic sentence chunker (Task 3)`

---

### Task 4: `SpeechProvider` protocol + `DWSpeechProvider`

**Files:** Create `.../karen_synth/speech_provider.py`; Test `tests/governance/comms/karen_synth/test_speech_provider.py`.

**Interfaces — Produces:**
- `class SpeechProvider(Protocol)`: `def source(self, *, system_prompt: str, user_prompt: str) -> AsyncIterator[str]` (returns an async source of text chunks — one chunk for DW, many for a future streaming impl).
- `class DWSpeechProvider`: `__init__(self, dw_provider, *, max_tokens: int = 80)`; wraps `dw_provider.complete_sync(...)` and yields the single result string. Fault-isolated (yields nothing on failure).

- [ ] **Step 1: failing test** (fake DW with `complete_sync` returning an object with `.content`)
```python
# tests/governance/comms/karen_synth/test_speech_provider.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.speech_provider import DWSpeechProvider

class _Res: 
    def __init__(self, content): self.content = content

class _FakeDW:
    def __init__(self, content="Fix applied. Tests green.", boom=False):
        self.content, self.boom, self.calls = content, boom, []
    async def complete_sync(self, prompt, *, system_prompt, caller_id, max_tokens=512, **kw):
        self.calls.append((prompt, system_prompt, max_tokens))
        if self.boom: raise RuntimeError("dw down")
        return _Res(self.content)

@pytest.mark.asyncio
async def test_dw_provider_yields_completion_text():
    dw = _FakeDW()
    sp = DWSpeechProvider(dw, max_tokens=80)
    out = [c async for c in sp.source(system_prompt="sys", user_prompt="usr")]
    assert out == ["Fix applied. Tests green."]
    assert dw.calls[0][1] == "sys" and dw.calls[0][2] == 80   # system prompt + cap forwarded

@pytest.mark.asyncio
async def test_dw_provider_failure_yields_nothing():
    sp = DWSpeechProvider(_FakeDW(boom=True))
    assert [c async for c in sp.source(system_prompt="s", user_prompt="u")] == []
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement**
```python
# backend/core/ouroboros/governance/comms/karen_synth/speech_provider.py
from __future__ import annotations

import logging
from typing import AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger("Ouroboros.Karen.Synth")


@runtime_checkable
class SpeechProvider(Protocol):
    def source(self, *, system_prompt: str, user_prompt: str) -> AsyncIterator[str]: ...


class DWSpeechProvider:
    """DW-primary (Option A). Wraps DoublewordProvider.complete_sync (full
    payload) and exposes it as a one-chunk async source — the sentence chunker
    then splits it. Stream-ready by contract: a future StreamingSpeechProvider
    yields many chunks from the same `source()` shape. Never raises."""

    def __init__(self, dw_provider: object, *, max_tokens: int = 80) -> None:
        self._dw = dw_provider
        self._max_tokens = max_tokens

    async def source(self, *, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        try:
            res = await self._dw.complete_sync(
                user_prompt,
                system_prompt=system_prompt,
                caller_id="karen_synth",
                max_tokens=self._max_tokens,
            )
            content = getattr(res, "content", "") or ""
            if content.strip():
                yield content
        except Exception:  # noqa: BLE001
            logger.debug("[KarenSynth] DW completion failed", exc_info=True)
            return
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): SpeechProvider protocol + DWSpeechProvider (Task 4)`

---

### Task 5: `KarenSpeechSynthesizer`

**Files:** Create `.../karen_synth/synthesizer.py`; Test `tests/governance/comms/karen_synth/test_synthesizer.py`.

**Interfaces — Consumes:** Tasks 1-4. **Produces:** `class KarenSpeechSynthesizer(__init__(self, provider: SpeechProvider, *, persona_ctx_fn=None))`; `async def synthesize(self, view: LedgerView) -> AsyncIterator[str]` — builds prompt → provider.source → chunker → yields sentences. Never raises; empty on failure.

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/karen_synth/test_synthesizer.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.karen_synth.ledger_view import LedgerView
from backend.core.ouroboros.governance.comms.karen_synth.synthesizer import KarenSpeechSynthesizer

class _FakeProvider:
    def __init__(self, text): self.text, self.seen = text, {}
    async def source(self, *, system_prompt, user_prompt):
        self.seen = {"system": system_prompt, "user": user_prompt}
        yield self.text

@pytest.mark.asyncio
async def test_synthesize_yields_sentences_from_ledger():
    prov = _FakeProvider("Fix applied. Tests green.")
    synth = KarenSpeechSynthesizer(prov, persona_ctx_fn=lambda: {"user_name": "Derek"})
    view = LedgerView.from_payload("decision", {"outcome": "applied", "target_files": ["a/x.py"]})
    out = [s async for s in synth.synthesize(view)]
    assert out == ["Fix applied.", "Tests green."]
    assert "Derek" in prov.seen["system"]        # persona injected
    assert "x.py" in prov.seen["user"]           # ledger injected

@pytest.mark.asyncio
async def test_synthesize_never_speaks_code():
    prov = _FakeProvider("It broke. Rolled back.")
    synth = KarenSpeechSynthesizer(prov)
    view = LedgerView.from_payload("postmortem", {"root_cause": "x ```raw code``` y"})
    out = [s async for s in synth.synthesize(view)]
    assert all("```" not in s for s in out)
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement**
```python
# backend/core/ouroboros/governance/comms/karen_synth/synthesizer.py
from __future__ import annotations

import logging
from typing import AsyncIterator, Callable, Mapping, Optional

from .ledger_view import LedgerView
from .persona import build_prompt
from .sentence_chunker import stream_sentences
from .speech_provider import SpeechProvider

logger = logging.getLogger("Ouroboros.Karen.Synth")


class KarenSpeechSynthesizer:
    """OperationContext ledger → persona prompt → provider → sentences.
    Provider-agnostic (DW payload today, token stream later). Never raises —
    on any failure it yields nothing and the arbiter stays silent."""

    def __init__(
        self,
        provider: SpeechProvider,
        *,
        persona_ctx_fn: Optional[Callable[[], Mapping]] = None,
    ) -> None:
        self._provider = provider
        self._persona_ctx_fn = persona_ctx_fn

    async def synthesize(self, view: LedgerView) -> AsyncIterator[str]:
        try:
            ctx = self._persona_ctx_fn() if self._persona_ctx_fn else None
        except Exception:  # noqa: BLE001
            ctx = None
        try:
            system, user = build_prompt(view, ctx)
            source = self._provider.source(system_prompt=system, user_prompt=user)
            async for sentence in stream_sentences(source):
                if sentence.strip():
                    yield sentence
        except Exception:  # noqa: BLE001
            logger.debug("[KarenSynth] synthesize failed", exc_info=True)
            return
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): KarenSpeechSynthesizer orchestrator (Task 5)`

---

### Task 6: Arbiter `fire_filler()` — local, LLM-free, interruptible

**Files:** Modify `.../duplex/arbiter.py`; Test `tests/voice/duplex/test_arbiter.py` (append).

**Interfaces — Produces (adds to `VoiceDuplexArbiter`):** `def fire_filler(self) -> None` — submits a short rotating local filler as an ordinary `SpeechRequest` (priority `USER_RESPONSE`, `coalesce_key="filler"` so repeats coalesce). Rotates via an internal counter to avoid repetition. Gated on `config.enabled`.

- [ ] **Step 1: failing test** (append; uses existing `_ON`/`_until`/`_shutdown`)
```python
_FILLER_TEXTS = {"On it.", "Checking.", "Right.", "One sec.", "Hmm."}

@pytest.mark.asyncio
async def test_fire_filler_speaks_a_local_ack():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.fire_filler()
        await _until(lambda: len(fp.played) == 1)
        assert fp.played[0] in _FILLER_TEXTS         # a local filler, no LLM
    finally:
        await _shutdown(arb, task)

@pytest.mark.asyncio
async def test_fillers_rotate_not_repeat_consecutively():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    seen = [arb._next_filler() for _ in range(3)]
    assert len(set(seen)) == 3                        # three distinct in a row
```
- [ ] **Step 2: run → FAIL** (`AttributeError: fire_filler` / `_next_filler`)
- [ ] **Step 3: implement** — add to `arbiter.py`:
  - In `__init__`: `self._filler_idx = 0`
  - class constant near top: `_FILLERS = ("On it.", "Checking.", "Right.", "One sec.", "Hmm.")`
  - methods:
```python
    def _next_filler(self) -> str:
        f = self._FILLERS[self._filler_idx % len(self._FILLERS)]
        self._filler_idx += 1
        return f

    def fire_filler(self) -> None:
        """Speak a short LOCAL acknowledgment (LLM-free) to mask synth latency.
        Ordinary interruptible SpeechRequest; repeats coalesce. Never raises."""
        if not self._config.enabled:
            return
        try:
            self.submit(SpeechRequest(
                self._next_filler(), Priority.USER_RESPONSE, coalesce_key="filler",
            ))
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] fire_filler failed", exc_info=True)
```
- [ ] **Step 4: run → PASS** (whole `tests/voice/duplex/` green, `--timeout=20`)  — [ ] **Step 5: commit** `feat(karen): arbiter local filler system (Task 6)`

---

### Task 7: Rewire `VoiceNarrator` + eradicate `narrator_script.py`

**Files:** Modify `voice_narrator.py`, `comms/__init__.py`, `governance/__init__.py`; Create `.../karen_synth/safe_say_playback.py`; Delete `narrator_script.py` + `tests/governance/comms/test_narrator_script.py`; Fix `tests/governance/comms/test_exports.py`, `tests/governance/test_daemon_narrator_wiring.py`; Test `tests/governance/comms/test_voice_narrator_dynamic.py`.

**Interfaces — Produces:** `class SafeSayPlayback` (PlaybackHandle backed by `safe_say`) — minimal bridge so synth output is audible now; Sprint 3 replaces with streaming TTS. `VoiceNarrator._narrate_one` rewired: build `LedgerView.from_payload(self._map_phase(msg), context)` → `arbiter.fire_filler()` → `async for s in synth.synthesize(view): arbiter.submit(SpeechRequest(s, Priority.PROACTIVE_INFO))`. `format_narration` no longer imported anywhere.

- [ ] **Step 1: failing test** — assert `narrator_script` is gone and the narrator now drives the synthesizer:
```python
# tests/governance/comms/test_voice_narrator_dynamic.py
from __future__ import annotations
import importlib, pytest

def test_narrator_script_module_is_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.core.ouroboros.governance.comms.narrator_script")

def test_format_narration_not_reexported():
    comms = importlib.import_module("backend.core.ouroboros.governance.comms")
    assert not hasattr(comms, "format_narration")
    assert not hasattr(comms, "SCRIPTS")
```
(Plus a synthesizer-driven `_narrate_one` test with a fake synth + fake arbiter asserting `fire_filler` is called and each sentence is `submit`ted. The implementer writes this against the rewired signature.)
- [ ] **Step 2: run → FAIL** (module still exists / still re-exported)
- [ ] **Step 3: implement** — delete `narrator_script.py`; remove the `format_narration`/`SCRIPTS` re-export lines from both `__init__.py` files; rewrite `_narrate_one` to use an injected `KarenSpeechSynthesizer` + `VoiceDuplexArbiter` (inject both via `__init__`, default-constructed from `DWSpeechProvider(get_dw_provider())` + a module-level arbiter with `SafeSayPlayback`, gated on `JARVIS_KAREN_SYNTH_ENABLED`); write `SafeSayPlayback` (async `play(text)` → `await safe_say(text, voice="Karen", source="ouroboros")`; `preempt()` → best-effort no-op for Sprint 2, real kill in Sprint 3; `is_active` tracks a flag); delete `test_narrator_script.py`; fix `test_exports.py` (drop the two symbols) and `test_daemon_narrator_wiring.py` (replace the 2 `format_narration` calls with the synthesizer path or remove if obsolete).
- [ ] **Step 4: run → PASS** — run `tests/governance/comms/ -q` and `tests/voice/duplex/ -q`; confirm no import of `narrator_script`/`format_narration` remains: `! grep -rn "narrator_script\|format_narration" backend/ | grep -v karen_synth`.
- [ ] **Step 5: commit** `feat(karen): eradicate narrator_script; VoiceNarrator drives KarenSpeechSynthesizer (Task 7)`

---

## Sprint 2 Definition of Done
- `narrator_script.py` deleted; zero `format_narration`/`SCRIPTS` references remain in `backend/`.
- `KarenSpeechSynthesizer` produces persona-driven, code-free, ≤2-sentence speech from `LedgerView`, DW-primary, stream-ready.
- Arbiter fires local LLM-free interruptible fillers.
- All `karen_synth` + `duplex` + `comms` tests green.

## Not in Sprint 2 (later)
- Sprint 3: real StreamingSTT + streaming TTS + full barge-in from the mic (replaces `SafeSayPlayback`), always-listening loop.
- Sprint 4: voice→build intake.
- **Parked research:** whether DW/J-Prime models can serve TTS/voice directly (could replace local Piper in Sprint 3).
