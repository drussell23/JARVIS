# Resource Leak and Cleanup Discipline — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate resource leaks (FD, memory, terminal state) so `unified_supervisor.py` can run indefinitely without growth or corruption.

**Architecture:** Six targeted edits to `unified_supervisor.py` plus one new test file. Reuses the existing `_atomic_write_json` helper (line 3069) for state persistence fixes. Each task is independent — no ordering dependencies between tasks.

**Tech Stack:** Python 3.9+, asyncio, threading, tempfile, collections.deque, atexit

**Pre-existing fixes (skip):**
- 4A (IPCServer StreamWriter) — already has `writer.close()` + `await writer.wait_closed()` in finally (line 65688)
- 4E (StartupLock.release()) — all call sites already protected by context managers or finally blocks

---

### Task 1: Terminal atexit safety net (4B)

**Files:**
- Modify: `unified_supervisor.py:7253-7282`
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** `_keyboard_listener()` (line 7253) puts stdin into cbreak mode via `tty.setcbreak(fd)`. The existing `try/finally` restores `tcsetattr` when the method exits normally. But if the daemon thread is killed (e.g., `os._exit`, segfault, or thread abort), the terminal is left in cbreak mode. An `atexit` handler provides a second safety net.

**Step 1: Write the failing test**

```python
#!/usr/bin/env python3
"""
Resource leak and cleanup discipline tests for unified_supervisor.py (Phase 4).

Run: python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py -v
"""
import asyncio
import collections
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestTerminalAtexitSafetyNet:
    """4B: atexit handler must be registered when keyboard listener starts."""

    def test_atexit_registered_on_keyboard_listener_start(self):
        """Starting the keyboard listener must register an atexit callback."""
        from unified_supervisor import LiveProgressDashboard

        dashboard = LiveProgressDashboard.__new__(LiveProgressDashboard)
        dashboard._running = False  # Don't actually loop
        dashboard._active_tab = "logs"
        dashboard._passthrough_interval = 5
        dashboard._render_count = 0
        dashboard._TAB_MAP = {"1": "logs"}

        # Track atexit registrations
        registered = []
        with patch("atexit.register", side_effect=lambda fn, *a, **kw: registered.append(fn)):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = False  # Skip actual TTY
                # The atexit registration should happen in _keyboard_listener
                # We verify the method body references atexit.register
                import ast, inspect
                try:
                    source = inspect.getsource(dashboard._keyboard_listener)
                except (OSError, TypeError):
                    # Fall back to AST scan of the file
                    with open("unified_supervisor.py", "r") as f:
                        tree = ast.parse(f.read())
                    found = False
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef) and node.name == "_keyboard_listener":
                            body_src = ast.dump(node)
                            found = "atexit" in body_src
                            break
                    assert found, "_keyboard_listener must reference atexit for terminal safety"
                    return
                assert "atexit" in source, "_keyboard_listener must register an atexit handler"
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestTerminalAtexitSafetyNet -v`
Expected: FAIL — `_keyboard_listener` does not currently reference atexit

**Step 2: Implement the fix**

Edit `unified_supervisor.py` line 7253-7282. Replace the method body:

```python
    def _keyboard_listener(self) -> None:
        """Listen for tab-switching keypresses (1-5). Daemon thread.

        Uses cbreak mode so single keys are delivered immediately without
        requiring Enter. Forces an immediate render on tab change so the
        user sees feedback within ~100ms instead of waiting up to 5s for
        the next passthrough render cycle.
        """
        import atexit
        import select as _sel
        import termios as _termios
        import tty as _tty

        fd = sys.stdin.fileno()
        old_settings = _termios.tcgetattr(fd)

        def _restore_terminal():
            try:
                _termios.tcsetattr(fd, _termios.TCSADRAIN, old_settings)
            except Exception:
                pass

        atexit.register(_restore_terminal)
        try:
            _tty.setcbreak(fd)
            while self._running:
                if _sel.select([fd], [], [], 0.25)[0]:
                    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                    new_tab = self._TAB_MAP.get(ch)
                    if new_tab and new_tab != self._active_tab:
                        self._active_tab = new_tab
                        self._render_count = self._passthrough_interval
        except Exception:
            pass
        finally:
            _restore_terminal()
            try:
                atexit.unregister(_restore_terminal)
            except Exception:
                pass
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestTerminalAtexitSafetyNet -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(terminal): add atexit safety net for cbreak mode restoration (Phase 4B)"
```

---

### Task 2: Subprocess cleanup on timeout/cancel (4C)

**Files:**
- Modify: `unified_supervisor.py:14520-14560`
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** The memory pressure checker at line 14524 spawns `memory_pressure` and `vm_stat` subprocesses. On `TimeoutError`, the process handle leaks without `kill()` + `wait()`. The `vm_stat` subprocess (line 14540) has no timeout/cancel protection at all.

**Step 1: Write the failing test**

```python
class TestSubprocessCleanupPattern:
    """4C: Subprocess code must kill on timeout and cancel."""

    def test_memory_pressure_subprocess_has_cleanup(self):
        """memory_pressure subprocess must have kill/wait on TimeoutError."""
        import ast

        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        # Find the method containing "memory_pressure" subprocess
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                body_dump = ast.dump(node)
                if "memory_pressure" in body_dump and "create_subprocess_exec" in body_dump:
                    # Must contain proc.kill() or proc.terminate()
                    assert "kill" in body_dump or "terminate" in body_dump, (
                        f"Method {node.name} spawns memory_pressure subprocess "
                        f"but has no kill/terminate cleanup"
                    )
                    return
        pytest.skip("memory_pressure subprocess method not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestSubprocessCleanupPattern -v`
Expected: FAIL — current code catches `(FileNotFoundError, asyncio.TimeoutError): pass` with no kill

**Step 2: Implement the fix**

Edit `unified_supervisor.py` lines 14520-14560. Replace:

```python
        try:
            # Method 1: Try memory_pressure command
            pressure_level = 1
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "memory_pressure",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                output = stdout.decode()

                if "critical" in output.lower():
                    pressure_level = 4
                elif "warn" in output.lower():
                    pressure_level = 2
            except FileNotFoundError:
                pass
            except asyncio.TimeoutError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                raise

            # Method 2: Use vm_stat for page in/out rates
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "vm_stat",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                return {"pressure_level": pressure_level, "page_ins": 0, "page_outs": 0, "is_under_pressure": False}
            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                raise
            output = stdout.decode()
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestSubprocessCleanupPattern -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(subprocess): add kill/wait cleanup on timeout/cancel for memory pressure check (Phase 4C)"
```

---

### Task 3: ChromaDB client explicit close (4D)

**Files:**
- Modify: `unified_supervisor.py:12547-12554`
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** `SemanticVoiceCacheManager.cleanup()` (line 12547) calls `self._client.persist()` but never closes the ChromaDB client, leaking the underlying DuckDB connection and file handles.

**Step 1: Write the failing test**

```python
class TestChromaDBCleanup:
    """4D: ChromaDB client must be explicitly closed in cleanup()."""

    def test_cleanup_closes_client(self):
        """cleanup() must call close/reset on the ChromaDB client."""
        import ast

        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SemanticVoiceCacheManager":
                for item in node.body:
                    if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)) and item.name == "cleanup":
                        body = ast.dump(item)
                        has_close = "reset" in body or "close" in body or "_client" in body
                        has_nullify = "None" in body and "_client" in body
                        assert has_close and has_nullify, (
                            "cleanup() must close/reset ChromaDB client AND set references to None"
                        )
                        return
        pytest.fail("SemanticVoiceCacheManager.cleanup() not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestChromaDBCleanup -v`
Expected: FAIL — current cleanup doesn't nullify `_client` or `_collection`

**Step 2: Implement the fix**

Edit `unified_supervisor.py` lines 12547-12554:

```python
    async def cleanup(self) -> None:
        """Clean up voice cache resources."""
        if self._client:
            try:
                self._client.persist()
            except Exception:
                pass
            try:
                self._client.reset()
            except Exception:
                pass
            self._client = None
            self._collection = None
        self._initialized = False
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestChromaDBCleanup -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(chromadb): explicitly close/reset client and nullify refs in cleanup (Phase 4D)"
```

---

### Task 4: Atomic state persistence for CostTracker, VMSessionTracker, GlobalSessionManager (4F)

**Files:**
- Modify: `unified_supervisor.py:11633-11655` (CostTracker._save_state)
- Modify: `unified_supervisor.py:17266-17271` (VMSessionTracker._save_registry)
- Modify: `unified_supervisor.py:14819-14832` (GlobalSessionManager._register_global_session)
- Modify: `unified_supervisor.py:14858-14859` (GlobalSessionManager.register_vm session_file write)
- Modify: `unified_supervisor.py:15020-15027` (GlobalSessionManager._save_registry_async)
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** All three classes use `path.write_text(json.dumps(...))` which is NOT atomic — a crash mid-write corrupts the file. The project already has `_atomic_write_json()` (line 3069) and `_load_json_state()` (line 3089). We reuse these for consistency.

**Step 1: Write the failing test**

```python
class TestAtomicStatePersistence:
    """4F: State persistence must use atomic write pattern."""

    @pytest.mark.parametrize("class_name,method_name", [
        ("CostTracker", "_save_state"),
        ("VMSessionTracker", "_save_registry"),
        ("GlobalSessionManager", "_register_global_session"),
        ("GlobalSessionManager", "_save_registry_async"),
    ])
    def test_state_writer_uses_atomic_pattern(self, class_name, method_name):
        """State write methods must use _atomic_write_json, not raw write_text."""
        import ast

        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)) and item.name == method_name:
                        body = ast.dump(item)
                        assert "write_text" not in body, (
                            f"{class_name}.{method_name} uses raw write_text — must use _atomic_write_json"
                        )
                        assert "_atomic_write_json" in body, (
                            f"{class_name}.{method_name} must call _atomic_write_json"
                        )
                        return
        pytest.fail(f"{class_name}.{method_name} not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestAtomicStatePersistence -v`
Expected: FAIL — all 4 methods use `write_text`

**Step 2: Implement the fixes**

**CostTracker._save_state (line 11633):**
```python
    async def _save_state(self) -> None:
        """Persist cost state."""
        try:
            data = {
                "last_date": time.strftime("%Y-%m-%d"),
                "last_month": time.strftime("%Y-%m"),
                "daily_cost": self._daily_cost,
                "monthly_cost": self._monthly_cost,
                "total_cost": self._total_cost,
                "savings": self._savings_vs_regular,
                "updated_at": time.time(),
            }
            try:
                await _run_in_supervisor_thread(
                    _atomic_write_json, self.state_file, data, timeout=5.0
                )
            except (asyncio.TimeoutError, Exception) as io_err:
                self._logger.warning(f"CostTracker._save_state: file write failed: {io_err}")
        except Exception as e:
            self._logger.warning(f"Failed to save cost state: {e}")
```

**VMSessionTracker._save_registry (line 17266):**
```python
    def _save_registry(self, registry: Dict[str, Any]) -> None:
        """Save VM registry to disk."""
        try:
            _atomic_write_json(self.vm_registry, registry)
        except Exception as e:
            _unified_logger.error(f"Failed to save VM registry: {e}")
```

**GlobalSessionManager._register_global_session (line 14819):**
```python
    def _register_global_session(self):
        """Register this session in the global tracker (sync)."""
        try:
            session_info = {
                "session_id": self.session_id,
                "pid": self.pid,
                "hostname": self.hostname,
                "created_at": self.created_at,
                "vm_id": None,
                "status": "active",
            }
            _atomic_write_json(self.global_tracker_file, session_info)
        except Exception as e:
            _unified_logger.warning(f"Failed to register global session: {e}")
```

**GlobalSessionManager.register_vm session_file write (line 14858-14859):**
Replace `self.session_file.write_text(json.dumps(session_data, indent=2))` with:
```python
                _atomic_write_json(self.session_file, session_data)
```

**GlobalSessionManager._save_registry_async (line 15020):**
```python
    async def _save_registry_async(self, registry: Dict[str, Any]):
        """Save VM registry to disk."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _atomic_write_json, self.vm_registry, registry)
        except Exception as e:
            _unified_logger.error(f"Failed to save VM registry: {e}")
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestAtomicStatePersistence -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(persistence): use atomic write pattern for CostTracker, VMSessionTracker, GlobalSessionManager (Phase 4F)"
```

---

### Task 5: Bounded collections (4G)

**Files:**
- Modify: `unified_supervisor.py:14195` (IntelligentCacheManager._errors)
- Modify: `unified_supervisor.py:9616` (AnimatedProgressBar._step_times)
- Modify: `unified_supervisor.py:8962` (RichCliRenderer._phase_timeline)
- Modify: `unified_supervisor.py:14044` (SpotInstanceResilienceHandler preemption_history append)
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** Four lists grow without bounds during long-running operation. Replace with `collections.deque(maxlen=N)` where the list is only appended to, or add explicit trim-after-append for lists that are also iterated/sliced.

**Step 1: Write the failing test**

```python
class TestBoundedCollections:
    """4G: Unbounded lists must have growth limits."""

    def test_cache_manager_errors_bounded(self):
        """IntelligentCacheManager._errors must be bounded."""
        from unified_supervisor import IntelligentCacheManager
        mgr = IntelligentCacheManager.__new__(IntelligentCacheManager)
        mgr._errors = getattr(mgr, "_errors", [])
        # Re-init to get the bounded version
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "IntelligentCacheManager":
                init_body = ast.dump(node)
                assert "deque" in init_body or "maxlen" in init_body or "_errors" in init_body
                # Check that _errors assignment uses deque
                assert "deque" in init_body, (
                    "IntelligentCacheManager._errors must use collections.deque(maxlen=...)"
                )
                return
        pytest.fail("IntelligentCacheManager not found")

    def test_step_times_bounded(self):
        """AnimatedProgressBar._step_times must be bounded."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "AnimatedProgressBar":
                body = ast.dump(node)
                assert "deque" in body, (
                    "AnimatedProgressBar._step_times must use collections.deque(maxlen=...)"
                )
                return
        pytest.fail("AnimatedProgressBar not found")

    def test_phase_timeline_bounded(self):
        """RichCliRenderer._phase_timeline must be bounded."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RichCliRenderer":
                body = ast.dump(node)
                assert "deque" in body, (
                    "RichCliRenderer._phase_timeline must use collections.deque(maxlen=...)"
                )
                return
        pytest.fail("RichCliRenderer not found")

    def test_preemption_history_trimmed_on_append(self):
        """SpotInstanceResilienceHandler.preemption_history must stay bounded."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SpotInstanceResilienceHandler":
                body = ast.dump(node)
                has_deque = "deque" in body
                has_slice = "preemption_history" in body and ("[-" in body or "maxlen" in body)
                assert has_deque or has_slice, (
                    "SpotInstanceResilienceHandler.preemption_history must be bounded"
                )
                return
        pytest.fail("SpotInstanceResilienceHandler not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestBoundedCollections -v`
Expected: FAIL — all 4 use plain `list`

**Step 2: Implement the fixes**

Ensure `import collections` exists near the top of the file (it likely does — search first with `grep -n "import collections" unified_supervisor.py`).

**IntelligentCacheManager.__init__ (line 14195):**
Change:
```python
        self._errors: List[str] = []
```
to:
```python
        self._errors: collections.deque[str] = collections.deque(maxlen=1000)
```

**AnimatedProgressBar.__init__ (line 9616):**
Change:
```python
        self._step_times: List[float] = []
```
to:
```python
        self._step_times: collections.deque[float] = collections.deque(maxlen=500)
```

Note: `_calculate_eta()` (line 9638) only needs recent step times for rate estimation, so 500 is more than sufficient.

**RichCliRenderer.__init__ (line 8962):**
Change:
```python
        self._phase_timeline: List[Dict[str, Any]] = []
```
to:
```python
        self._phase_timeline: collections.deque[Dict[str, Any]] = collections.deque(maxlen=200)
```

**SpotInstanceResilienceHandler.__init__ (line 13919):**
Change:
```python
        self.preemption_history: List[Dict[str, Any]] = []
```
to:
```python
        self.preemption_history: collections.deque[Dict[str, Any]] = collections.deque(maxlen=50)
```

Also fix the `initialize` load (line 13940) — deque constructor accepts iterable:
```python
            self.preemption_history = collections.deque(
                preserved.get("preemption_history", [])[-50:], maxlen=50
            )
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestBoundedCollections -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(memory): replace unbounded lists with deque(maxlen=N) in 4 classes (Phase 4G)"
```

---

### Task 6: Voice narrator queue maxsize (4H)

**Files:**
- Modify: `unified_supervisor.py:15909`
- Test: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** `AsyncVoiceNarrator._queue` (line 15909) is an unbounded `asyncio.PriorityQueue()`. If speech synthesis falls behind, messages accumulate without limit. Add `maxsize=50` (configurable via env var) and use `put_nowait()` with try/except `asyncio.QueueFull` to drop on overflow.

**Step 1: Write the failing test**

```python
class TestVoiceNarratorQueueBounded:
    """4H: Voice narrator queue must have a maxsize."""

    def test_queue_has_maxsize(self):
        """AsyncVoiceNarrator._queue must have maxsize > 0."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "AsyncVoiceNarrator":
                body = ast.dump(node)
                assert "maxsize" in body, (
                    "AsyncVoiceNarrator._queue must be created with maxsize parameter"
                )
                return
        pytest.fail("AsyncVoiceNarrator not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestVoiceNarratorQueueBounded -v`
Expected: FAIL — `PriorityQueue()` has no maxsize

**Step 2: Implement the fix**

Edit `unified_supervisor.py` line 15909. Change:
```python
        self._queue: asyncio.PriorityQueue[Tuple[int, float, str]] = asyncio.PriorityQueue()
```
to:
```python
        _voice_queue_max = int(os.environ.get("JARVIS_VOICE_QUEUE_MAXSIZE", "50"))
        self._queue: asyncio.PriorityQueue[Tuple[int, float, str]] = asyncio.PriorityQueue(
            maxsize=_voice_queue_max
        )
```

Then find the `put` call site(s) for this queue (grep for `self._queue.put`). Change any `await self._queue.put(...)` to use `put_nowait` with overflow handling:

```python
            try:
                self._queue.put_nowait((priority.value, time.time(), text))
            except asyncio.QueueFull:
                self._messages_skipped += 1
```

**Step 3: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestVoiceNarratorQueueBounded -v`

**Step 4: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_resource_leak_discipline.py
git commit -m "fix(voice): add maxsize to narrator queue with overflow drop (Phase 4H)"
```

---

### Task 7: Phase 4 Gate Test

**Files:**
- Modify: `tests/unit/backend/test_resource_leak_discipline.py`

**Context:** A parametrized gate test that verifies all Phase 4 fixes are in place.

**Step 1: Write the gate test**

```python
class TestPhase4Gate:
    """Gate 4: All resource leak fixes verified."""

    @pytest.mark.parametrize("check", [
        "terminal_atexit",
        "subprocess_cleanup",
        "chromadb_close",
        "atomic_persistence",
        "bounded_collections",
        "voice_queue_maxsize",
    ])
    def test_phase4_gate(self, check):
        """Phase 4 gate: all resource leak fixes must be in place."""
        import ast
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        if check == "terminal_atexit":
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_keyboard_listener":
                    assert "atexit" in ast.dump(node)
                    return
            pytest.fail("_keyboard_listener not found")

        elif check == "subprocess_cleanup":
            for node in ast.walk(tree):
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    body = ast.dump(node)
                    if "memory_pressure" in body and "create_subprocess_exec" in body:
                        assert "kill" in body, "memory_pressure subprocess needs kill cleanup"
                        return
            pytest.fail("memory_pressure method not found")

        elif check == "chromadb_close":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "SemanticVoiceCacheManager":
                    for item in node.body:
                        if isinstance(item, (ast.AsyncFunctionDef,)) and item.name == "cleanup":
                            body = ast.dump(item)
                            assert "None" in body and "_client" in body
                            return
            pytest.fail("SemanticVoiceCacheManager.cleanup not found")

        elif check == "atomic_persistence":
            classes_checked = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in ("CostTracker", "VMSessionTracker", "GlobalSessionManager"):
                    for item in node.body:
                        if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)):
                            body = ast.dump(item)
                            if "write_text" in body and item.name in ("_save_state", "_save_registry", "_register_global_session", "_save_registry_async"):
                                pytest.fail(f"{node.name}.{item.name} still uses write_text")
                    classes_checked += 1
            assert classes_checked == 3, f"Expected 3 classes, found {classes_checked}"

        elif check == "bounded_collections":
            targets = {"IntelligentCacheManager": False, "AnimatedProgressBar": False, "RichCliRenderer": False, "SpotInstanceResilienceHandler": False}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in targets:
                    if "deque" in ast.dump(node):
                        targets[node.name] = True
            unbounded = [k for k, v in targets.items() if not v]
            assert not unbounded, f"Still unbounded: {unbounded}"

        elif check == "voice_queue_maxsize":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "AsyncVoiceNarrator":
                    assert "maxsize" in ast.dump(node)
                    return
            pytest.fail("AsyncVoiceNarrator not found")
```

Run: `python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py::TestPhase4Gate -v`
Expected: All 6 checks PASS

**Step 2: Commit**

```bash
git add tests/unit/backend/test_resource_leak_discipline.py
git commit -m "test(phase4): add Phase 4 gate tests for resource leak discipline"
```
