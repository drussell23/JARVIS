# The `ov` Awakening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ov` boots as a terminal-native awakening — procedural ouroboros crest animation + Karen live-state boot briefing — cooling into the restrained working surface, while soak/CI output stays byte-identical.

**Architecture:** Presentation-layer split: a `PresentationMode` gate at every harness banner emission source (COCKPIT withholds, SOAK unchanged, fatal paths structurally bypass); a reactive procedural crest (`ui/crest.py`) + thin conductor (`ui/awakening.py`) that mounts the existing `WakeSequenceRenderer`; a boot-briefing composer reusing the Sprint-2 Karen speech pipeline behind a 4s circuit breaker.

**Tech Stack:** Python 3.9+, Rich (Live/Theme), asyncio, pytest + pytest-asyncio, existing `ui/theme.py` tier ladder, existing `karen_synth`/duplex stack.

**Spec:** `docs/superpowers/specs/2026-07-07-ov-awakening-cli-design.md` (approved, §10 resolved). Crest geometry source of truth: `docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py`.

## Global Constraints

- CLI/terminal only; all rendering through `ui/theme.py`; zero escape leakage at `ColorTier.NONE`; TTY checks via `sys.__stdout__` (`real_stdout_isatty` pattern), never `sys.stdout.isatty()`.
- **Mandate 1 (root-cause):** banner suppression by conditional logic at each emission source in `scripts/ouroboros_battle_test.py` / `harness.py`. NO stdout redirect/wrapper/regex filter anywhere. ERROR/CRITICAL + fatal prints must not route through the gate at all.
- **Mandate 2 (reactive geometry):** `ui/crest.py` scales all metrics proportionally from measured console size; clamp cols to `[JARVIS_OV_CREST_MIN_COLS=46, JARVIS_OV_CREST_MAX_COLS=72]`; zero absolute canvas dimensions.
- **Mandate 3 (DRY):** `ui/awakening.py` embeds `WakeSequenceRenderer`/`WakeModel` (`ui/wake_sequence.py`) and subscribes via `BootTimer.add_observer` — no new phase tracker. `cli/ov.py` stays a facade (sets mode env, delegates to `battle_main`).
- **Mandate 4 (bulletproof):** SIGWINCH mid-animation resize test required; briefing breaker (`JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S`, default 4.0) proven to fall back to the local live-state line without stalling.
- Skip keys: `Esc`/`Enter` ONLY; any other typed bytes are buffered and handed to the REPL untouched (never swallowed, never trigger skip).
- Crest requires unicode + tier ≥ STANDARD; otherwise the awakening degrades to plain wake lines.
- `ui/` modules import only stdlib + Rich + `ui.theme` (leaf rule); governance callbacks reach `ui/` via injection.
- `from __future__ import annotations` in every new file; `asyncio.wait_for` (3.9), never `asyncio.timeout`.
- Env flags (exact names): `JARVIS_OV_PRESENTATION`, `JARVIS_OV_AWAKENING_ENABLED` (default true), `JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S` (4.0), `JARVIS_OV_CREST_MIN_COLS` (46), `JARVIS_OV_CREST_MAX_COLS` (72).
- Commit after every task; `git add` only named files (never `-A`/`.`); commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with `python3 -m pytest` from the repo root.

---

### Task 1: PresentationMode + gate the harness banner sources (root cause first)

**Files:**
- Create: `backend/core/ouroboros/ui/presentation_mode.py`
- Create: `tests/ui/test_presentation_mode.py`
- Modify: `scripts/ouroboros_battle_test.py` (emission sites: zombie reaper call ~line 1315, single-flight ~1392, `_print_preflight()` call ~1397, `logging.basicConfig` level ~1402; fatal API-key check extraction from `_print_preflight` ~lines 589–594 and ~630–634)
- Test: `tests/battle_test/test_presentation_gate.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PresentationMode(str, Enum)` with `.COCKPIT`/`.SOAK`; `resolve_presentation_mode(env: Optional[Mapping[str,str]] = None) -> PresentationMode`; `is_cockpit() -> bool`; script-level `_check_api_keys_or_die() -> None` (unconditional fatal check). Later tasks read `resolve_presentation_mode()` and rely on `JARVIS_OV_PRESENTATION=cockpit`.

- [ ] **Step 1: Write the failing mode tests**

```python
# tests/ui/test_presentation_mode.py
"""PresentationMode: default-SOAK fail-safe resolution (spec §3.5)."""
from __future__ import annotations

from backend.core.ouroboros.ui.presentation_mode import (
    PresentationMode, resolve_presentation_mode, is_cockpit,
)


def test_default_is_soak():
    assert resolve_presentation_mode(env={}) is PresentationMode.SOAK


def test_cockpit_value_resolves():
    env = {"JARVIS_OV_PRESENTATION": "cockpit"}
    assert resolve_presentation_mode(env=env) is PresentationMode.COCKPIT


def test_garbage_fails_safe_to_soak():
    env = {"JARVIS_OV_PRESENTATION": "PARTY_MODE"}
    assert resolve_presentation_mode(env=env) is PresentationMode.SOAK


def test_case_and_whitespace_tolerant():
    env = {"JARVIS_OV_PRESENTATION": "  Cockpit "}
    assert resolve_presentation_mode(env=env) is PresentationMode.COCKPIT


def test_is_cockpit_reads_process_env(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    assert is_cockpit() is True
    monkeypatch.delenv("JARVIS_OV_PRESENTATION")
    assert is_cockpit() is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ui/test_presentation_mode.py -v`
Expected: FAIL — `ModuleNotFoundError: ...presentation_mode`

- [ ] **Step 3: Implement the module**

```python
# backend/core/ouroboros/ui/presentation_mode.py
"""backend/core/ouroboros/ui/presentation_mode.py -- COCKPIT vs SOAK skin.

One resolution point for the ov presentation split (spec §3.5). SOAK is the
fail-safe default: every legacy launch path (the battle-test script, ov run,
CI, daemons) keeps byte-identical output unless `ov` cockpit explicitly opts
in via JARVIS_OV_PRESENTATION=cockpit.

Leaf module: stdlib only. The gate this feeds NEVER carries fatal telemetry
(Mandate 1) -- ERROR/CRITICAL paths are emitted unconditionally at their
sources and do not consult this module.
"""
from __future__ import annotations

import enum
import os
from typing import Mapping, Optional

ENV_KEY = "JARVIS_OV_PRESENTATION"


class PresentationMode(str, enum.Enum):
    COCKPIT = "cockpit"   # ov awakening: banners withheld, WARNING logging
    SOAK = "soak"         # legacy verbose harness output (default)


def resolve_presentation_mode(
    env: Optional[Mapping[str, str]] = None,
) -> PresentationMode:
    """Resolve the mode. Unknown/absent values fail safe to SOAK."""
    source = os.environ if env is None else env
    raw = (source.get(ENV_KEY) or "").strip().lower()
    if raw == PresentationMode.COCKPIT.value:
        return PresentationMode.COCKPIT
    return PresentationMode.SOAK


def is_cockpit() -> bool:
    return resolve_presentation_mode() is PresentationMode.COCKPIT


__all__ = ["ENV_KEY", "PresentationMode", "resolve_presentation_mode", "is_cockpit"]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ui/test_presentation_mode.py -v`
Expected: 5 PASS

- [ ] **Step 5: Write the failing gate tests (bypass proof + withholding proof)**

Read `scripts/ouroboros_battle_test.py` around lines 558–644 first: `_print_preflight()` currently contains the fatal No-API-keys check (prints `ERROR: No API keys set.` and `sys.exit(1)`). The gate must NOT be able to suppress that — it gets extracted to `_check_api_keys_or_die()` called unconditionally.

```python
# tests/battle_test/test_presentation_gate.py
"""Presentation gate: COCKPIT withholds banners at the SOURCE; fatal paths
structurally bypass (Mandate 1). SOAK is call-through (legacy regression)."""
from __future__ import annotations

import logging

import pytest

import scripts.ouroboros_battle_test as bt
from backend.core.ouroboros.ui.presentation_mode import PresentationMode


def test_check_api_keys_or_die_exists_and_is_fatal(monkeypatch):
    """The fatal check is its own function -- physically outside the gate."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        bt._check_api_keys_or_die()


def test_check_api_keys_passes_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bt._check_api_keys_or_die()   # no raise


def test_print_preflight_no_longer_contains_fatal_exit(monkeypatch, capsys):
    """_print_preflight is pure presentation now: with no keys it must NOT
    exit -- the fatal lives in _check_api_keys_or_die (bypass proof)."""
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bt._print_preflight()          # must not raise SystemExit


def test_gated_banner_helpers_skip_in_cockpit(monkeypatch):
    """The boot path calls banners through _run_gated_boot_banners(mode);
    COCKPIT skips them, SOAK calls through."""
    calls = []
    monkeypatch.setattr(bt, "_reap_zombies", lambda: calls.append("reap") or set())
    monkeypatch.setattr(bt, "_single_flight_preflight", lambda: calls.append("sf"))
    monkeypatch.setattr(bt, "_print_preflight", lambda: calls.append("pf"))

    bt._run_gated_boot_banners(PresentationMode.COCKPIT, single_flight_enabled=True,
                               reap_enabled=True)
    assert calls == []             # all withheld at the source

    bt._run_gated_boot_banners(PresentationMode.SOAK, single_flight_enabled=True,
                               reap_enabled=True)
    assert calls == ["reap", "sf", "pf"]   # legacy order preserved


def test_resolve_boot_log_level_cockpit_is_warning():
    assert bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=False) == logging.WARNING
    assert bt._resolve_boot_log_level(PresentationMode.SOAK, verbose=False) == logging.INFO
    # verbose ALWAYS wins -- an operator asking for -v is never silenced
    assert bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=True) == logging.DEBUG


def test_error_records_pass_in_cockpit(caplog):
    """WARNING root level still delivers ERROR/CRITICAL -- the bypass is
    structural: the gate only lowers verbosity, it filters nothing."""
    level = bt._resolve_boot_log_level(PresentationMode.COCKPIT, verbose=False)
    logger = logging.getLogger("test.cockpit.fatal")
    with caplog.at_level(level, logger="test.cockpit.fatal"):
        logger.error("initialization collapse")
        logger.critical("fatal")
    messages = [r.message for r in caplog.records]
    assert "initialization collapse" in messages
    assert "fatal" in messages
```

- [ ] **Step 6: Run to verify failure**

Run: `python3 -m pytest tests/battle_test/test_presentation_gate.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_check_api_keys_or_die'` (and `_run_gated_boot_banners`, `_resolve_boot_log_level`)

- [ ] **Step 7: Implement the gate in `scripts/ouroboros_battle_test.py`**

7a. Add near the top (after the existing imports):

```python
from backend.core.ouroboros.ui.presentation_mode import (
    PresentationMode, resolve_presentation_mode,
)
```

7b. Extract the fatal check. Find the No-API-keys block inside `_print_preflight()` (it checks `DOUBLEWORD_API_KEY` / `ANTHROPIC_API_KEY`, prints the red ERROR lines, and calls `sys.exit(1)`). Move the check+exit into a new top-level function directly above `_print_preflight`, keeping the exact print strings:

```python
def _check_api_keys_or_die() -> None:
    """FATAL preflight: no provider keys -> die loudly. Deliberately OUTSIDE
    the presentation gate (Mandate 1): no mode can suppress this."""
    if not os.environ.get("DOUBLEWORD_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"\n  {_RED}{_BOLD}ERROR: No API keys set.{_RESET}")
        print(f"  {_RED}Export DOUBLEWORD_API_KEY or ANTHROPIC_API_KEY.{_RESET}\n")
        sys.exit(1)
```

Inside `_print_preflight()`, replace the moved block with nothing (delete it); if a second key-check/exit exists near the end of `_print_preflight` (~630–634), delete that too. `_print_preflight` becomes pure presentation.

> Note: match the EXACT existing strings/conditions when extracting — read the current function body first; the snippet above shows the shape, the source is authoritative.

7c. Add the two gate helpers (top-level, near `_print_preflight`):

```python
def _run_gated_boot_banners(
    mode: PresentationMode, *, single_flight_enabled: bool, reap_enabled: bool,
) -> None:
    """Nominal boot banners, gated AT THE SOURCE (Mandate 1). COCKPIT
    withholds; detail stays reachable via the /preflight + /organism verbs.
    Fatal telemetry never routes through here."""
    if mode is PresentationMode.COCKPIT:
        return
    if reap_enabled:
        _reap_zombies()
    if single_flight_enabled:
        _single_flight_preflight()
    _print_preflight()


def _resolve_boot_log_level(mode: PresentationMode, *, verbose: bool = False) -> int:
    """COCKPIT quiets the INFO flood to WARNING. -v always wins. ERROR and
    CRITICAL pass at every level -- the gate lowers verbosity, filters nothing."""
    if verbose:
        return logging.DEBUG
    if mode is PresentationMode.COCKPIT:
        return logging.WARNING
    return logging.INFO
```

7d. Rewire `main()`'s boot section. Locate the existing calls (`_reaped = _reap_zombies()` ~1315 with its enable-env check, `_single_flight_preflight()` ~1392 with its enable-env check, `_print_preflight()` ~1397, `log_level = logging.DEBUG if args.verbose else logging.INFO` ~1402) and replace with:

```python
    _mode = resolve_presentation_mode()
    _check_api_keys_or_die()          # fatal path: unconditional, both modes
    _run_gated_boot_banners(
        _mode,
        single_flight_enabled=os.environ.get(
            "JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED", "true"
        ).lower() not in ("false", "0", "no", "off"),
        reap_enabled=_reap_enabled,   # reuse the existing JARVIS_BATTLE_REAP_ZOMBIES resolution
    )
    log_level = _resolve_boot_log_level(_mode, verbose=args.verbose)
```

Important integration details (read the surrounding code and preserve behavior):
- The zombie-reaper block currently also calls `_cleanup_stale_router_lock` / `_reap_stale_jarvis_locks` and keeps the reaped-PID set. Keep the lock-cleanup **functional** work OUTSIDE the gate (it is hygiene, not presentation) — only the banner-printing reaper path is gated. If `_reap_zombies()` mixes both, split: gate its `print(...)` lines on `mode is not COCKPIT` via a new `quiet: bool = False` parameter instead of skipping the reap (zombie reaping must still HAPPEN in cockpit — it is functional). In that case `_run_gated_boot_banners` calls `_reap_zombies(quiet=False)` and `main()` calls `_reap_zombies(quiet=True)` directly in cockpit mode. Choose based on what the code actually does at the call site; the invariant is: **functional side effects always run; only stdout ceremony is gated.**
- Keep the existing noisy-logger suppression list and `JARVIS_LOG_LEVEL` override exactly as-is (they run after `basicConfig` in both modes).

- [ ] **Step 8: Run to verify pass + no regressions**

Run: `python3 -m pytest tests/battle_test/test_presentation_gate.py tests/ui/test_presentation_mode.py -v`
Expected: all PASS
Run: `python3 -m pytest tests/battle_test/ -x -q 2>&1 | tail -5`
Expected: existing battle_test suite green

- [ ] **Step 9: Commit**

```bash
git add backend/core/ouroboros/ui/presentation_mode.py tests/ui/test_presentation_mode.py scripts/ouroboros_battle_test.py tests/battle_test/test_presentation_gate.py
git commit -m "feat(ov): presentation-mode gate at harness banner sources + structural fatal bypass

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `ov` cockpit facade sets the mode

**Files:**
- Modify: `backend/core/ouroboros/cli/ov.py` (the `main()` cockpit/headless dispatch, lines ~139-166)
- Test: `tests/cli/test_ov_presentation.py` (create; `tests/cli/` may already exist from Sprint 1 — check for existing `test_ov*.py` and add alongside)

**Interfaces:**
- Consumes: `ENV_KEY`, `PresentationMode` from Task 1.
- Produces: `ov` cockpit ⇒ `os.environ["JARVIS_OV_PRESENTATION"]="cockpit"` before delegation; `ov run`/`daemon` ⇒ forces `"soak"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_ov_presentation.py
"""ov facade: cockpit action opts into COCKPIT presentation; run/daemon
force SOAK; the facade never does more than set env + delegate (Mandate 3)."""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.cli import ov as ov_cli


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_OV_PRESENTATION", raising=False)


def _capture_delegation(monkeypatch):
    seen = {}

    def fake_battle_main(argv):
        seen["argv"] = list(argv)
        seen["mode_env"] = os.environ.get("JARVIS_OV_PRESENTATION")

    import scripts.ouroboros_battle_test as bt
    monkeypatch.setattr(bt, "main", fake_battle_main)
    return seen


def test_cockpit_sets_cockpit_mode_before_delegating(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main([]) == 0
    assert seen["mode_env"] == "cockpit"
    assert seen["argv"] == []


def test_run_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["run", "--cost-cap", "1.00"]) == 0
    assert seen["mode_env"] == "soak"
    assert seen["argv"] == ["--headless", "--cost-cap", "1.00"]


def test_daemon_forces_soak(monkeypatch):
    seen = _capture_delegation(monkeypatch)
    assert ov_cli.main(["daemon"]) == 0
    assert seen["mode_env"] == "soak"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/cli/test_ov_presentation.py -v`
Expected: FAIL — `seen["mode_env"]` is `None`

- [ ] **Step 3: Implement in `cli/ov.py`**

In `main()`, replace the final dispatch block:

```python
    # cockpit / headless -> the one shared bootstrap (DRY). The facade's ONLY
    # added responsibility: declare the presentation skin (spec §3.4).
    from backend.core.ouroboros.ui.presentation_mode import ENV_KEY, PresentationMode

    os.environ[ENV_KEY] = (
        PresentationMode.COCKPIT.value if inv.action == "cockpit"
        else PresentationMode.SOAK.value
    )
    try:
        from scripts.ouroboros_battle_test import main as battle_main
    except Exception as exc:  # noqa: BLE001
        console.print(f"ov: failed to load bootstrap: {exc}", markup=False)
        return 1
    battle_main(inv.delegate_argv)
    return 0
```

Add `import os` to the module imports.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/cli/ -v`
Expected: PASS (new + any existing ov dispatch tests)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/cli/ov.py tests/cli/test_ov_presentation.py
git commit -m "feat(ov): cockpit facade declares presentation mode, run/daemon force soak

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `ui/crest.py` — reactive procedural geometry (TDD)

**Files:**
- Create: `backend/core/ouroboros/ui/crest.py`
- Test: `tests/ui/test_crest.py`
- Read first (geometry source of truth — port, do not invent): `docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py`

**Interfaces:**
- Consumes: `ui.theme` (`ColorTier`, `style_for`, `Token`, `supports_unicode`).
- Produces (Tasks 4–6 rely on these exact names):

```python
@dataclass(frozen=True)
class CrestCell:
    x: int; y: int; glyph: str; kind: str          # kind in {"coil","head","eye","v"}
    rgb: Tuple[int, int, int]; delay_s: float

@dataclass(frozen=True)
class CrestFrame:
    cols: int; rows: int
    cells: Tuple[CrestCell, ...]
    max_delay_s: float
    unavailable_reason: Optional[str] = None       # non-None => do not animate

def generate_crest(measured_cols: int, measured_rows: int, *,
                   tier: ColorTier, unicode_ok: bool) -> CrestFrame
```

**Porting rule (Mandate 2 applied to the asset):** the asset uses fixed
`CELL_W, CELL_H = 58, 19` and absolute radii. The port replaces every
absolute metric with `scale = clamped_cols / 58.0` applied to the reference
values, where `clamped_cols = max(MIN_COLS, min(measured_cols, MAX_COLS))`
from `JARVIS_OV_CREST_MIN_COLS` (46) / `JARVIS_OV_CREST_MAX_COLS` (72).
Reference metrics (from the asset, verbatim): `R_MID=12.6`, `THICK=2.9`,
`GAP_HALF=20°`, `TAPER_SWEEP=52°` with min-thickness factor `0.34`,
`TAIL_INTRUDE=15°`, `HEAD_LEN=5.4`, `HEAD_W=2.05`, `V_TOP=5.0`, `V_BOT=5.4`,
`V_HALF_SPAN=4.9`, `V_STROKE=2.65`, `ASPECT=1.08`, supersample `SS=3`,
threshold ≥ 0.5 per subpixel, quadrant lookup table, flat-V-top rejection,
isolated-crumb cleanup, eye disc `EYE_R≈0.95` offset inside the head.
Copy the sampling functions (`ang_norm`, `in_gap`, `body_half`/taper with
intrusion, `head_frame`, `sample_head`, `sample_eye`, `seg_dist`, `sample_v`
with the flat top, the quadrant `QUAD` table, `classify`, cleanup pass, and
the gradient `STOPS`/`grad`) from the asset, adapting names/params to the
scaled-geometry dataclass. Delays: `coil = 0.05 + 1.30*frac` (frac = arc
angle from tail tip, 0→1), `head = 1.42`, `eye = 1.55`, `v = 1.78 + 0.5*t`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ui/test_crest.py
"""Crest v5 invariants: solid, crisp, anatomically complete, REACTIVE.
These are the professional-art regression net (spec §9.1)."""
from __future__ import annotations

import math
import os

import pytest

from backend.core.ouroboros.ui.crest import CrestFrame, generate_crest
from backend.core.ouroboros.ui.theme import ColorTier

T = ColorTier.TRUECOLOR


def gen(cols=60, rows=24, tier=T, unicode_ok=True) -> CrestFrame:
    return generate_crest(cols, rows, tier=tier, unicode_ok=unicode_ok)


def cells_by_kind(frame, kind):
    return [c for c in frame.cells if c.kind == kind]


# ---- anatomy invariants ----------------------------------------------------

def test_all_kinds_present():
    f = gen()
    assert f.unavailable_reason is None
    for kind in ("coil", "head", "eye", "v"):
        assert cells_by_kind(f, kind), f"missing {kind} cells"


def test_no_isolated_crumb_cells():
    f = gen()
    occupied = {(c.x, c.y) for c in f.cells}
    dots = set("▘▝▖▗")   # single-quadrant glyphs
    for c in f.cells:
        if c.glyph in dots:
            assert any((c.x + dx, c.y + dy) in occupied
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))), \
                f"isolated crumb at {(c.x, c.y)}"


def test_v_has_flat_top():
    f = gen()
    v = cells_by_kind(f, "v")
    top_y = min(c.y for c in v)
    top_row = [c for c in v if c.y == top_y]
    assert len(top_row) >= 4          # two stroke caps, each >=2 cells wide


def test_coil_is_solid_no_full_block_holes():
    """Between the leftmost and rightmost coil cell of each row band that
    contains full blocks, interior full-block runs are contiguous per flank
    (annulus: at most 2 runs per row -- left flank + right flank)."""
    f = gen()
    coil_rows = {}
    for c in cells_by_kind(f, "coil"):
        if c.glyph == "█":
            coil_rows.setdefault(c.y, []).append(c.x)
    assert coil_rows, "no solid coil interior at all"
    for y, xs in coil_rows.items():
        xs = sorted(xs)
        runs = 1
        for a, b in zip(xs, xs[1:]):
            if b - a > 1:
                runs += 1
        assert runs <= 2, f"row {y}: {runs} solid runs (holes in the body)"


def test_delays_monotonic_tail_to_head_and_bounded():
    f = gen()
    coil = cells_by_kind(f, "coil")
    assert all(0.0 <= c.delay_s <= 1.40 for c in coil)
    assert min(c.delay_s for c in coil) < 0.2       # trace starts at the tail
    head = cells_by_kind(f, "head")
    assert all(c.delay_s > max(c2.delay_s for c2 in coil) - 0.3 for c in head)
    assert f.max_delay_s >= 1.55                     # eye ignition is last


# ---- reactivity + clamping (Mandate 2) --------------------------------------

def test_geometry_scales_with_width():
    small, large = gen(cols=46), gen(cols=72)
    assert small.cols == 46 and large.cols == 72
    assert len(large.cells) > len(small.cells) * 1.5


def test_hard_clamp_at_72():
    f = gen(cols=200)
    assert f.cols == 72
    assert max(c.x for c in f.cells) < 72


def test_env_min_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_MIN_COLS", "50")
    f = generate_crest(48, 24, tier=T, unicode_ok=True)
    assert f.unavailable_reason is not None          # below min -> unavailable


def test_below_default_min_unavailable():
    f = gen(cols=40)
    assert f.unavailable_reason is not None


def test_insufficient_rows_unavailable():
    f = gen(cols=60, rows=8)
    assert f.unavailable_reason is not None


# ---- tier / unicode degradation ---------------------------------------------

def test_no_unicode_unavailable():
    f = gen(unicode_ok=False)
    assert f.unavailable_reason is not None


def test_none_tier_unavailable():
    f = gen(tier=ColorTier.NONE)
    assert f.unavailable_reason is not None


def test_standard_tier_renders_geometry():
    f = gen(tier=ColorTier.STANDARD)
    assert f.unavailable_reason is None
    assert cells_by_kind(f, "coil")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ui/test_crest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `ui/crest.py`**

Module skeleton (port the listed functions from the asset into `_Geometry`-parameterized forms; the asset file is in-repo and authoritative for the math):

```python
# backend/core/ouroboros/ui/crest.py
"""Procedural ouroboros crest (v5) -- reactive, hard-edge, tier-resolved.

Ports docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py
into a reactive generator: every metric scales from the measured terminal
width (Mandate 2 -- zero absolute canvas dimensions), clamped to
[JARVIS_OV_CREST_MIN_COLS, JARVIS_OV_CREST_MAX_COLS]. Rendering is
hard-threshold quadrant rasterization -- solid fill, no anti-aliasing.
Leaf module: stdlib + ui.theme only. NEVER raises: impossible conditions
return CrestFrame(unavailable_reason=...).
"""
from __future__ import annotations

import functools
import math
import os
from dataclasses import dataclass
from typing import Optional, Tuple

from .theme import ColorTier

_REF_COLS = 58.0        # the asset's reference canvas width

# reference metrics -- verbatim from the approved v5 asset
_REF = dict(
    r_mid=12.6, thick=2.9, gap_half=math.radians(20),
    taper_sweep=math.radians(52), taper_min=0.34,
    tail_intrude=math.radians(15),
    head_len=5.4, head_w=2.05, eye_r=0.95,
    v_top=5.0, v_bot=5.4, v_half_span=4.9, v_stroke=2.65,
)
_ASPECT = 1.08
_SS = 3
_GAP_CENTER = math.radians(90)

_STOPS = [
    (0, (125, 255, 106)), (60, (91, 227, 75)), (150, (139, 92, 246)),
    (210, (177, 108, 234)), (285, (212, 192, 74)), (360, (125, 255, 106)),
]
_HEAD_RGB = (125, 255, 106)
_EYE_RGB = (234, 255, 208)
_V_TOP_RGB, _V_BOT_RGB = (192, 132, 252), (157, 78, 220)

_QUAD = {
    0b0000: " ", 0b1000: "▘", 0b0100: "▝", 0b1100: "▀",
    0b0010: "▖", 0b1010: "▌", 0b0110: "▞", 0b1110: "▛",
    0b0001: "▗", 0b1001: "▚", 0b0101: "▐", 0b1101: "▜",
    0b0011: "▄", 0b1011: "▙", 0b0111: "▟", 0b1111: "█",
}


@dataclass(frozen=True)
class CrestCell:
    x: int
    y: int
    glyph: str
    kind: str
    rgb: Tuple[int, int, int]
    delay_s: float


@dataclass(frozen=True)
class CrestFrame:
    cols: int
    rows: int
    cells: Tuple[CrestCell, ...]
    max_delay_s: float
    unavailable_reason: Optional[str] = None


def _clamp_cols(measured: int) -> Tuple[int, int]:
    lo = int(os.environ.get("JARVIS_OV_CREST_MIN_COLS", "46"))
    hi = int(os.environ.get("JARVIS_OV_CREST_MAX_COLS", "72"))
    return lo, min(measured, hi)


# ... [_Geometry dataclass: every _REF metric * scale; center at
#      (cols/2, rows*ASPECT); required_rows derived from scaled radius] ...
# ... [port: _ang_norm, _in_gap, _body_half (taper + taper_min), tail
#      intrusion, _head_frame, _sample_head (wedge + mouth notch),
#      _sample_eye, _seg_dist, _sample_v (flat top), _classify (priority
#      eye>head>coil>v, SS*SS majority >= 0.5), _grad(frac)] ...
# ... [_render_cells: per cell 2x2 subpixels -> QUAD bits; kind from
#      majority; rgb + delay from kind/frac; isolated-crumb cleanup] ...


@functools.lru_cache(maxsize=8)
def _generate_cached(clamped_cols: int, rows: int, tier: int) -> CrestFrame:
    geo = _Geometry.for_size(clamped_cols, rows)
    cells = _render_cells(geo)
    max_delay = max((c.delay_s for c in cells), default=0.0)
    return CrestFrame(cols=clamped_cols, rows=geo.rows_used,
                      cells=tuple(cells), max_delay_s=max(max_delay, 1.55))


def generate_crest(measured_cols: int, measured_rows: int, *,
                   tier: ColorTier, unicode_ok: bool) -> CrestFrame:
    """NEVER raises. Unavailable => plain-wake-lines degradation upstream."""
    try:
        if not unicode_ok:
            return CrestFrame(0, 0, (), 0.0, "unicode required")
        if tier is ColorTier.NONE:
            return CrestFrame(0, 0, (), 0.0, "color tier NONE")
        lo, clamped = _clamp_cols(measured_cols)
        if clamped < lo:
            return CrestFrame(0, 0, (), 0.0, f"width {measured_cols} < min {lo}")
        needed_rows = _Geometry.rows_needed(clamped)
        if measured_rows < needed_rows:
            return CrestFrame(0, 0, (), 0.0,
                              f"rows {measured_rows} < needed {needed_rows}")
        return _generate_cached(clamped, needed_rows, int(tier))
    except Exception:  # noqa: BLE001
        return CrestFrame(0, 0, (), 0.0, "generation error")
```

The `# ... [port: ...] ...` blocks above name every function to port; transcribe
each from the asset (same math, `geo.` parameters instead of module constants).
`_Geometry.for_size(cols, rows)` computes `scale = cols / _REF_COLS` and
multiplies every `_REF` metric; `rows_needed(cols)` returns
`ceil((2*(r_mid+thick/2)*scale) / (2*_ASPECT)) + 2` (the coil's cell height
plus one row of margin top/bottom).

- [ ] **Step 4: Run to verify pass; iterate the port until the invariants hold**

Run: `python3 -m pytest tests/ui/test_crest.py -v`
Expected: 13 PASS. If an anatomy invariant fails, print the silhouette
(`"".join(...)` by rows) in a debug session and compare against the asset's
`preview_cells` output — the port must reproduce the approved v5 silhouette
at cols=58.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/ui/crest.py tests/ui/test_crest.py
git commit -m "feat(ov): reactive procedural crest v5 (hard-edge quadrants, clamped 46-72)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: crest style resolution per tier

**Files:**
- Modify: `backend/core/ouroboros/ui/crest.py`
- Test: `tests/ui/test_crest.py` (append)

**Interfaces:**
- Produces: `style_for_cell(cell: CrestCell, tier: ColorTier) -> str` — Rich style string per cell; TRUECOLOR/C256 ⇒ `f"rgb({r},{g},{b})"` (Rich downgrades for 256 automatically); STANDARD ⇒ coil/head/eye = `style_for(Token.ACCENT, tier)`, v = `"bold " + style_for(Token.ACCENT, tier)`. Task 5 consumes this.

- [ ] **Step 1: Append failing tests**

```python
# append to tests/ui/test_crest.py
from backend.core.ouroboros.ui.crest import style_for_cell
from backend.core.ouroboros.ui.theme import Token, style_for


def test_truecolor_styles_are_rgb():
    f = gen()
    c = cells_by_kind(f, "coil")[0]
    assert style_for_cell(c, ColorTier.TRUECOLOR).startswith("rgb(")


def test_standard_styles_are_accent_mono():
    f = gen(tier=ColorTier.STANDARD)
    accent = style_for(Token.ACCENT, ColorTier.STANDARD)
    coil = cells_by_kind(f, "coil")[0]
    v = cells_by_kind(f, "v")[0]
    assert style_for_cell(coil, ColorTier.STANDARD) == accent
    assert style_for_cell(v, ColorTier.STANDARD) == f"bold {accent}"


def test_cache_hit_same_object():
    a = gen(cols=60)
    b = gen(cols=60)
    assert a is b        # lru_cache identity
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ui/test_crest.py -k "style or cache" -v`
Expected: FAIL — `style_for_cell` not found

- [ ] **Step 3: Implement**

```python
# in ui/crest.py
from .theme import ColorTier, Token, style_for   # extend existing import


def style_for_cell(cell: CrestCell, tier: ColorTier) -> str:
    """Resolve one cell's Rich style for the tier. TRUECOLOR/C256 carry the
    per-cell gradient (Rich downgrades 24-bit for 256 terminals); STANDARD
    collapses to the single accent (geometry + trace unchanged)."""
    if tier >= ColorTier.C256:
        r, g, b = cell.rgb
        return f"rgb({r},{g},{b})"
    accent = style_for(Token.ACCENT, tier)
    if cell.kind == "v":
        return f"bold {accent}"
    return accent
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ui/test_crest.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/ui/crest.py tests/ui/test_crest.py
git commit -m "feat(ov): tier-resolved crest cell styles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `ui/awakening.py` — the conductor (mounts WakeSequenceRenderer)

**Files:**
- Create: `backend/core/ouroboros/ui/awakening.py`
- Test: `tests/ui/test_awakening.py`
- Read first: `backend/core/ouroboros/ui/wake_sequence.py` (the renderer being mounted — Mandate 3), `backend/core/ouroboros/battle_test/stream_renderer.py` (16ms cadence pattern).

**Interfaces:**
- Consumes: `generate_crest`/`style_for_cell`/`CrestFrame` (Tasks 3-4), `WakeSequenceRenderer` (existing), `theme.build_console`/`detect_tier`/`supports_unicode`/`active_tier`.
- Produces (Task 6 and Task 8 rely on these):

```python
class AwakeningConductor:
    def __init__(self, console, *, timer=None,
                 on_ignition: Optional[Callable[[], None]] = None,
                 context_provider: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 key_source: Optional[Callable[[], bytes]] = None) -> None
    typed_prefix: str            # non-skip bytes typed during the ceremony
    async def run(self) -> None  # whole ceremony; NEVER raises; returns after cool-down
    def request_skip(self) -> None

async def run_awakening(console=None, **kwargs) -> "AwakeningConductor"
```

**Behavior contract (from spec §3.2 + §10):**
1. Guards: `JARVIS_OV_AWAKENING_ENABLED` false, non-TTY (`sys.__stdout__` isatty), crest `unavailable_reason` ⇒ plain path: attach `WakeSequenceRenderer` to the timer and flush plain frames until `model.is_live` or a 20s guard timeout, then print the cooled header. No Live.
2. Animated path: Rich `Live` at ≤16ms refresh; frame = `Group(crest_text(elapsed), wake.render_frame())` where `crest_text` reveals cells with `delay_s <= elapsed`.
3. `on_ignition` fires exactly once when `elapsed >= frame.max_delay_s`.
4. After ignition: eye-pulse tick (alternate eye style bright/brighter each ~0.8s) + hold until `wake.model.is_live` (or 20s guard).
5. Cool-down: 3 sub-frames over ~0.4s (full gradient → all-accent tint → collapse), then Live closes and the cooled header prints once: `ov · ouroboros   live` styled line + one muted context line from `context_provider()` (fallback: `""` → header only, but production always passes a provider).
6. Skip: `key_source` (injectable; production reads stdin raw non-blocking via `select` + `os.read` in cbreak mode, restoring termios in `finally`): bytes `\x1b` (Esc) or `\r`/`\n` (Enter) ⇒ `request_skip()` ⇒ jump to cool-down; ALL other bytes append to `typed_prefix` (never swallowed, never skip).
7. NEVER raises; any exception ⇒ plain path fallback; logs DEBUG to `logging.getLogger("Ouroboros.UI.Awakening")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ui/test_awakening.py
"""AwakeningConductor: mounts the EXISTING WakeSequenceRenderer (Mandate 3),
ignites exactly once, skips only on Esc/Enter, buffers typed input, cools
down exactly once, and NEVER raises (spec §3.2, §9.5)."""
from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from backend.core.ouroboros.ui.awakening import AwakeningConductor
from backend.core.ouroboros.ui import theme


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t


class FakeTimer:
    """Duck-typed BootTimer: captures the observer for manual driving."""
    def __init__(self):
        self.observers = []
    def add_observer(self, cb):
        self.observers.append(cb)
    def emit(self, name, in_flight):
        class R:  # PhaseRecord duck
            pass
        r = R(); r.name = name; r.is_in_flight = in_flight
        for cb in self.observers:
            cb(r)


def make_conductor(**kw):
    console = Console(file=open("/dev/null", "w"), force_terminal=True,
                      width=80, color_system="truecolor")
    theme.ensure_theme(console)
    clock = kw.pop("clock", FakeClock())
    timer = kw.pop("timer", FakeTimer())
    c = AwakeningConductor(console, timer=timer, clock=clock, **kw)
    return c, clock, timer


@pytest.mark.asyncio
async def test_ignition_fires_exactly_once():
    fired = []
    c, clock, timer = make_conductor(on_ignition=lambda: fired.append(1))
    timer.emit("sensors online", False)
    task = asyncio.create_task(c.run())
    for _ in range(400):
        clock.t += 0.05
        await asyncio.sleep(0.001)
        if fired:
            break
    clock.t += 30.0                      # blow past hold guard + cool-down
    await asyncio.wait_for(task, timeout=5.0)
    assert fired == [1]


@pytest.mark.asyncio
async def test_esc_skips_and_enter_skips_but_other_keys_buffer():
    for skip_byte in (b"\x1b", b"\r", b"\n"):
        feed = [b"g", b"i", skip_byte]
        c, clock, timer = make_conductor(
            key_source=lambda f=feed: f.pop(0) if f else b"")
        timer.emit("loop armed", False)
        task = asyncio.create_task(c.run())
        for _ in range(200):
            clock.t += 0.05
            await asyncio.sleep(0.001)
            if task.done():
                break
        clock.t += 30.0
        await asyncio.wait_for(task, timeout=5.0)
        assert c.typed_prefix == "gi"     # buffered, not swallowed


@pytest.mark.asyncio
async def test_live_honesty_holds_until_model_live():
    c, clock, timer = make_conductor()
    timer.emit("venom priming", True)     # still in flight
    task = asyncio.create_task(c.run())
    clock.t += 3.0                        # trace done, but NOT live
    await asyncio.sleep(0.01)
    assert not task.done()                # holding (breathing)
    timer.emit("venom priming", False)    # now live
    clock.t += 30.0
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_render_failure_never_raises(monkeypatch):
    c, clock, timer = make_conductor()
    monkeypatch.setattr(c, "_render_crest_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    timer.emit("sensors online", False)
    clock.t += 60.0
    await asyncio.wait_for(c.run(), timeout=5.0)   # must complete silently


@pytest.mark.asyncio
async def test_awakening_disabled_env_goes_plain(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_AWAKENING_ENABLED", "false")
    c, clock, timer = make_conductor()
    timer.emit("sensors online", False)
    clock.t += 60.0
    await asyncio.wait_for(c.run(), timeout=5.0)
    assert c.used_plain_path is True
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ui/test_awakening.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `ui/awakening.py`** per the behavior contract above. Key structure:

```python
# backend/core/ouroboros/ui/awakening.py  (skeleton -- implement fully)
class AwakeningConductor:
    def __init__(self, console, *, timer=None, on_ignition=None,
                 context_provider=None, clock=time.monotonic, key_source=None):
        self._console = console
        self._wake = WakeSequenceRenderer(console)      # MOUNTED, not rebuilt
        if timer is not None:
            self._wake.attach(timer)                    # existing observer API
        ...
        self.typed_prefix = ""
        self.used_plain_path = False

    async def run(self) -> None:
        try:
            frame = self._generate_frame()
            if self._should_go_plain(frame):
                await self._run_plain()
                return
            await self._run_animated(frame)
        except Exception:
            logger.debug("[awakening] falling back to plain", exc_info=True)
            try:
                await self._run_plain()
            except Exception:
                pass
```

Implementation notes:
- `_generate_frame()` measures `self._console.size` and calls
  `generate_crest(size.width, size.height, tier=detect_tier(self._console), unicode_ok=supports_unicode())`.
- `_run_animated`: `with Live(console=..., auto_refresh=False, transient=True) as live:` loop at `await asyncio.sleep(0.016)`; poll `key_source` each tick; compose crest Text (per-cell `Text.append(glyph, style=style_for_cell(cell, tier))` positioned via row assembly) + `self._wake.render_frame()`; ignition when `elapsed >= frame.max_delay_s` (guard `self._ignited` bool; call `on_ignition` in try/except).
- Hold: `while not self._wake.model.is_live and elapsed < ignition_time + 20.0 and not self._skip:` breathe.
- Cool-down: render 2 tint frames then exit Live (transient erases it), then `console.print` the cooled header (accent `ov` · heading `ouroboros` · success `live`) and the muted `context_provider()` line.
- Production `key_source=None` ⇒ construct the termios/cbreak reader ONLY when `sys.__stdout__ is not None and sys.__stdout__.isatty()` and stdin is a tty; wrap all termios calls in try/except (never fatal); restore settings in `finally`.
- `_run_plain`: `self.used_plain_path = True`; loop `flush(now)` on the wake renderer (it prints plain frames) until `model.is_live` or 20s; print cooled header once.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ui/test_awakening.py -v`
Expected: 5 PASS (parametrized skip = 7 asserts total)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/ui/awakening.py tests/ui/test_awakening.py
git commit -m "feat(ov): awakening conductor -- crest trace + mounted wake sequence + Esc/Enter skip

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: SIGWINCH / resize mid-animation (Mandate 4 proof)

**Files:**
- Modify: `backend/core/ouroboros/ui/awakening.py` (per-tick size check + regeneration)
- Test: `tests/ui/test_awakening_resize.py`

**Interfaces:**
- Consumes: Task 5's conductor internals.
- Produces: resize-safe animation; `conductor.regenerations: int` counter for tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ui/test_awakening_resize.py
"""SIGWINCH proof (spec §9.2): mid-trace resize regenerates the crest at the
new measurement, reveal stays monotonic, cool-down intact. Plus a real
POSIX SIGWINCH delivery against a pty-backed console."""
from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest
from rich.console import Console

from backend.core.ouroboros.ui.awakening import AwakeningConductor
from backend.core.ouroboros.ui import theme
from tests.ui.test_awakening import FakeClock, FakeTimer


class ResizableConsole(Console):
    """Console whose reported size we mutate mid-run."""
    _forced = (80, 30)
    @property
    def size(self):
        from rich.console import ConsoleDimensions
        return ConsoleDimensions(*self._forced)


@pytest.mark.asyncio
async def test_mid_trace_resize_regenerates_and_stays_monotonic():
    console = ResizableConsole(file=open("/dev/null", "w"),
                               force_terminal=True, color_system="truecolor")
    theme.ensure_theme(console)
    clock, timer = FakeClock(), FakeTimer()
    c = AwakeningConductor(console, timer=timer, clock=clock)
    timer.emit("sensors online", False)

    revealed_counts = []
    orig = c._render_crest_text
    def spy(frame, elapsed, tier):
        revealed = sum(1 for cell in frame.cells if cell.delay_s <= elapsed)
        revealed_counts.append((frame.cols, revealed / max(1, len(frame.cells))))
        return orig(frame, elapsed, tier)
    c._render_crest_text = spy

    task = asyncio.create_task(c.run())
    for i in range(120):
        clock.t += 0.02
        await asyncio.sleep(0.001)
        if i == 40:
            ResizableConsole._forced = (50, 30)     # SIGWINCH effect
    clock.t += 60.0
    await asyncio.wait_for(task, timeout=5.0)

    assert c.regenerations >= 1
    cols_seen = {cols for cols, _ in revealed_counts}
    assert 50 in cols_seen                            # regenerated at new width
    # monotonic reveal FRACTION across the resize boundary (no flash-back)
    fracs = [f for _, f in revealed_counts]
    assert all(b >= a - 1e-9 for a, b in zip(fracs, fracs[1:]))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal test")
def test_real_sigwinch_does_not_crash_animation():
    """Deliver a real SIGWINCH while the conductor animates on a pty."""
    import pty
    pid, fd = pty.fork()
    if pid == 0:  # child: run a short awakening against the pty
        try:
            import asyncio as aio
            from rich.console import Console as C
            from backend.core.ouroboros.ui.awakening import AwakeningConductor as A
            from backend.core.ouroboros.ui import theme as th
            console = C(force_terminal=True, color_system="truecolor")
            th.ensure_theme(console)
            from tests.ui.test_awakening import FakeTimer as FT
            t = FT()
            c = A(console, timer=t)
            t.emit("sensors online", False)
            aio.get_event_loop().run_until_complete(
                aio.wait_for(c.run(), timeout=15.0))
            os._exit(0)
        except BaseException:
            os._exit(3)
    else:
        import time as _t
        _t.sleep(0.4)
        os.kill(pid, signal.SIGWINCH)                 # the real signal
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/ui/test_awakening_resize.py -v`
Expected: FAIL — no `regenerations` attribute / resize not handled

- [ ] **Step 3: Implement resize handling in the animated loop**

```python
# inside AwakeningConductor._run_animated tick loop:
size = self._console.size
if (size.width, size.height) != self._last_size:
    self._last_size = (size.width, size.height)
    new_frame = self._generate_frame()
    if new_frame.unavailable_reason is None:
        # remap: delays are angle-derived, so identical elapsed reveals the
        # same arc fraction on the new frame -- monotonic by construction
        frame = new_frame
        self.regenerations += 1
    else:
        self._skip = True          # too small now -> graceful cool-down
```

Initialize `self.regenerations = 0` and `self._last_size` in `__init__`.
(No signal handler needed: Rich re-reads the size, and per-tick measurement is
signal-free and thread-safe; the pty test proves real-SIGWINCH safety.)

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/ui/test_awakening_resize.py tests/ui/test_awakening.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/ui/awakening.py tests/ui/test_awakening_resize.py
git commit -m "feat(ov): resize-reactive awakening -- regeneration + monotonic reveal + SIGWINCH proof

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Karen boot briefing + circuit breaker (mathematically proven)

**Files:**
- Create: `backend/core/ouroboros/governance/comms/karen_boot_briefing.py`
- Test: `tests/voice/test_karen_boot_briefing.py`
- Read first: `backend/core/ouroboros/governance/comms/karen_synth/synthesizer.py` (the `synthesize(view) -> AsyncIterator[str]` contract), `ledger_view.py` (`LedgerView.from_payload`, `strip_code`).

**Interfaces:**
- Consumes: `KarenSpeechSynthesizer` duck (injected), `LedgerView.from_payload(phase, payload)`, `strip_code`.
- Produces (Task 8 wires these):

```python
@dataclass
class BriefingVectors:
    last_session: Optional[str] = None
    queued_ops: Optional[int] = None
    posture: Optional[str] = None
    pending_approvals: Optional[int] = None
    @property
    def empty(self) -> bool: ...

async def gather_vectors(*, intake_probe=None, approvals_probe=None) -> BriefingVectors
def compose_local(v: BriefingVectors) -> str
class BootBriefing:
    def __init__(self, *, synthesizer=None, speak_sink=None, text_sink=None,
                 timeout_s: Optional[float] = None,
                 vectors_override: Optional[BriefingVectors] = None) -> None
    async def run(self) -> str        # returns the delivered line; NEVER raises
def build_default_synthesizer() -> Optional[object]
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/voice/test_karen_boot_briefing.py
"""Boot briefing: live-state composition, DW primary, 4s breaker that
provably falls back to the LOCAL live-state line without stalling (spec
§3.3, Mandate 4). No canned strings: every output embeds real vectors."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.comms.karen_boot_briefing import (
    BootBriefing, BriefingVectors, compose_local, gather_vectors,
)


V = BriefingVectors(last_session="apply=AUTO/3 verify=4/4 commit=9f3c21ab",
                    queued_ops=2, posture="EXPLORE", pending_approvals=1)


class SlowSynth:
    """Synthesizer that never completes inside the deadline."""
    def __init__(self):
        self.cancelled = False
    async def synthesize(self, view):
        try:
            await asyncio.sleep(30)
            yield "never"
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class GoodSynth:
    async def synthesize(self, view):
        yield "Right then -- awake."
        yield "Two ops queued overnight."


def test_compose_local_embeds_live_vectors():
    line = compose_local(V)
    assert "2" in line and "EXPLORE".lower() in line.lower()
    assert "1" in line                       # pending approval surfaced


def test_compose_local_empty_state_is_factual_not_boilerplate():
    line = compose_local(BriefingVectors())
    assert "first session" in line.lower()


@pytest.mark.asyncio
async def test_breaker_falls_back_fast_without_stalling():
    """MATHEMATICAL PROOF (Mandate 4): wall-clock bound. timeout=0.2s; a
    30s-hanging LLM must yield the local fallback in < 1.0s."""
    spoken = []
    synth = SlowSynth()
    b = BootBriefing(synthesizer=synth, speak_sink=spoken.append,
                     timeout_s=0.2, vectors_override=V)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    line = await b.run()
    elapsed = loop.time() - t0
    assert elapsed < 1.0                     # CLI init NOT stalled
    assert "2" in line                       # fallback is the LIVE-state line
    assert spoken and spoken[-1] == line
    await asyncio.sleep(0.05)
    assert synth.cancelled is True           # hung task actually cancelled


@pytest.mark.asyncio
async def test_primary_path_speaks_sentences():
    spoken = []
    b = BootBriefing(synthesizer=GoodSynth(), speak_sink=spoken.append,
                     timeout_s=2.0, vectors_override=V)
    line = await b.run()
    assert spoken == ["Right then -- awake.", "Two ops queued overnight."]
    assert "awake" in line.lower()


@pytest.mark.asyncio
async def test_no_synthesizer_goes_straight_to_local():
    spoken = []
    b = BootBriefing(synthesizer=None, speak_sink=spoken.append,
                     vectors_override=V)
    line = await b.run()
    assert "2" in line and spoken == [line]


@pytest.mark.asyncio
async def test_voice_off_text_sink_only():
    texts = []
    b = BootBriefing(synthesizer=None, speak_sink=None, text_sink=texts.append,
                     vectors_override=V)
    line = await b.run()
    assert texts == [line]


@pytest.mark.asyncio
async def test_run_never_raises(monkeypatch):
    class ExplodingSynth:
        async def synthesize(self, view):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover
    b = BootBriefing(synthesizer=ExplodingSynth(),
                     speak_sink=lambda s: (_ for _ in ()).throw(RuntimeError("sink!")),
                     vectors_override=V)
    line = await b.run()                     # absolutely must not raise
    assert isinstance(line, str)


@pytest.mark.asyncio
async def test_gather_vectors_is_fault_isolated(monkeypatch):
    def bad_probe():
        raise RuntimeError("intake exploded")
    v = await gather_vectors(intake_probe=bad_probe, approvals_probe=None)
    assert v.queued_ops is None              # absent, not raised
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/voice/test_karen_boot_briefing.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the module**

```python
# backend/core/ouroboros/governance/comms/karen_boot_briefing.py
"""Karen's boot briefing -- the awakening's voice (spec §3.3).

Live vectors -> Sprint-2 speech pipeline (LedgerView filters -> persona ->
DW -> sentence chunks -> arbiter) behind a hard asyncio.wait_for breaker.
Fallback is a LOCAL deterministic composition over the SAME vectors --
state-driven prose, never canned boilerplate. Fire-and-forget by contract:
run() NEVER raises and never blocks boot beyond its own awaited deadline.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from .karen_synth.ledger_view import LedgerView, strip_code

logger = logging.getLogger("Ouroboros.Karen.BootBriefing")

_TIMEOUT_ENV = "JARVIS_KAREN_BOOT_BRIEF_TIMEOUT_S"


@dataclass
class BriefingVectors:
    last_session: Optional[str] = None
    queued_ops: Optional[int] = None
    posture: Optional[str] = None
    pending_approvals: Optional[int] = None

    @property
    def empty(self) -> bool:
        return all(v is None for v in
                   (self.last_session, self.queued_ops, self.posture,
                    self.pending_approvals))


async def gather_vectors(*, intake_probe: Optional[Callable[[], Optional[int]]] = None,
                         approvals_probe: Optional[Callable[[], Optional[int]]] = None,
                         ) -> BriefingVectors:
    """Best-effort, read-only, per-vector fault isolation. NEVER raises."""
    v = BriefingVectors()
    loop = asyncio.get_event_loop()
    try:
        from backend.core.ouroboros.governance.last_session_summary import (
            get_default_summary,
        )
        v.last_session = await loop.run_in_executor(
            None, get_default_summary().format_for_prompt_sync)
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] last_session vector unavailable", exc_info=True)
    try:
        from backend.core.ouroboros.governance.posture_palette import (
            read_current_posture_safe,
        )
        reading = await loop.run_in_executor(None, read_current_posture_safe)
        if reading is not None:
            v.posture = str(getattr(reading, "posture", "") or "") or None
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] posture vector unavailable", exc_info=True)
    for probe, attr in ((intake_probe, "queued_ops"),
                        (approvals_probe, "pending_approvals")):
        if probe is None:
            continue
        try:
            setattr(v, attr, probe())
        except Exception:  # noqa: BLE001
            logger.debug("[briefing] %s vector unavailable", attr, exc_info=True)
    return v


def compose_local(v: BriefingVectors) -> str:
    """Deterministic prose over LIVE vectors (the breaker's landing pad).
    Never a canned greeting: every clause carries real state."""
    if v.empty:
        return "Awake. First session on this repo."
    parts: List[str] = ["Awake."]
    if v.queued_ops:
        plural = "s" if v.queued_ops != 1 else ""
        parts.append(f"{v.queued_ops} op{plural} queued overnight.")
    if v.pending_approvals:
        plural = "s" if v.pending_approvals != 1 else ""
        parts.append(f"{v.pending_approvals} awaiting your sign-off.")
    if v.posture:
        parts.append(f"Posture {v.posture}.")
    if v.last_session and len(parts) == 1:
        parts.append(f"Last session: {strip_code(v.last_session)[:120]}.")
    return " ".join(parts)


class BootBriefing:
    def __init__(self, *, synthesizer: Optional[object] = None,
                 speak_sink: Optional[Callable[[str], None]] = None,
                 text_sink: Optional[Callable[[str], None]] = None,
                 timeout_s: Optional[float] = None,
                 vectors_override: Optional[BriefingVectors] = None) -> None:
        self._synth = synthesizer
        self._speak = speak_sink
        self._text = text_sink
        self._timeout = timeout_s if timeout_s is not None else float(
            os.environ.get(_TIMEOUT_ENV, "4.0"))
        self._vectors_override = vectors_override

    async def run(self) -> str:
        """Deliver the briefing. NEVER raises. Bounded by the breaker."""
        try:
            v = self._vectors_override or await gather_vectors()
            line = ""
            if self._synth is not None:
                line = await self._primary(v)
            if not line:
                line = compose_local(v)
                self._deliver(line)
            return line
        except Exception:  # noqa: BLE001
            logger.debug("[briefing] run failed", exc_info=True)
            return ""

    async def _primary(self, v: BriefingVectors) -> str:
        """DW path under the breaker. Returns "" on any failure/timeout."""
        payload = {
            "summary": compose_local(v),
            "queued_ops": v.queued_ops, "posture": v.posture,
            "pending_approvals": v.pending_approvals,
            "last_session": v.last_session,
        }
        view = LedgerView.from_payload("boot", payload)
        spoken: List[str] = []

        async def _consume() -> None:
            async for sentence in self._synth.synthesize(view):
                safe = strip_code(sentence).strip()
                if safe:
                    spoken.append(safe)
                    self._deliver(safe)

        task = asyncio.get_event_loop().create_task(_consume())
        try:
            await asyncio.wait_for(task, timeout=self._timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if not spoken:
                logger.debug("[briefing] breaker tripped -> local fallback")
                return ""
        return " ".join(spoken)

    def _deliver(self, line: str) -> None:
        for sink in (self._speak, self._text):
            if sink is None:
                continue
            try:
                sink(line)
            except Exception:  # noqa: BLE001
                logger.debug("[briefing] sink failed", exc_info=True)
            return                        # speak wins; text is the fallback


def build_default_synthesizer() -> Optional[object]:
    """Best-effort production synthesizer (DW -> Karen). None on any failure
    -- the breaker's local path covers it."""
    try:
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )
        from .karen_synth.speech_provider import DWSpeechProvider
        from .karen_synth.synthesizer import KarenSpeechSynthesizer
        if not os.environ.get("DOUBLEWORD_API_KEY"):
            return None
        return KarenSpeechSynthesizer(DWSpeechProvider(DoublewordProvider()))
    except Exception:  # noqa: BLE001
        logger.debug("[briefing] default synthesizer unavailable", exc_info=True)
        return None


__all__ = ["BriefingVectors", "BootBriefing", "gather_vectors",
           "compose_local", "build_default_synthesizer"]
```

> Check `KarenSpeechSynthesizer.__init__` (synthesizer.py:20) for its exact
> parameters before wiring `build_default_synthesizer` — pass the provider
> as it expects (positional `provider` or keyword). Adjust the one call site.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/voice/test_karen_boot_briefing.py -v`
Expected: 8 PASS — including the wall-clock breaker proof (< 1.0s with a 30s-hanging synth) and the cancellation assertion.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/karen_boot_briefing.py tests/voice/test_karen_boot_briefing.py
git commit -m "feat(karen): boot briefing -- live vectors, DW primary, proven 4s breaker fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: production wiring — default Karen sink + harness cockpit hook

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/duplex/karen_duplex_factory.py` (default-handle singleton, mirroring `voice_command_sensor.set_default_voice_sensor`)
- Modify: `backend/audio/audio_pipeline_bootstrap.py` (register/clear the default handle at Karen mount/stop, ~lines 195-202 and the shutdown path)
- Modify: `backend/core/ouroboros/battle_test/harness.py` (cockpit branch at the boot-banner block, ~lines 1212-1259; REPL `initial_text` handoff at ~1366)
- Test: `tests/voice/duplex/test_default_karen_handle.py`, `tests/battle_test/test_awakening_hook.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: `set_default_karen(handle) / get_default_karen()` in `karen_duplex_factory`; harness helper `build_awakening_for_cockpit(console, *, intake_probe, approvals_probe) -> AwakeningConductor` (module-level in `harness.py`, unit-testable).

- [ ] **Step 1: Write the failing singleton tests**

```python
# tests/voice/duplex/test_default_karen_handle.py
"""Default-Karen singleton -- the same late-binding pattern as
voice_command_sensor.set_default_voice_sensor (Sprint 4)."""
from __future__ import annotations

from backend.core.ouroboros.governance.comms.duplex import karen_duplex_factory as kdf


def test_default_handle_roundtrip():
    sentinel = object()
    kdf.set_default_karen(sentinel)
    try:
        assert kdf.get_default_karen() is sentinel
    finally:
        kdf.set_default_karen(None)
    assert kdf.get_default_karen() is None
```

- [ ] **Step 2: Run to verify failure, then implement**

Run: `python3 -m pytest tests/voice/duplex/test_default_karen_handle.py -v` → FAIL.

In `karen_duplex_factory.py` add (module level, bottom):

```python
_DEFAULT_KAREN: Optional[KarenDuplexHandle] = None


def set_default_karen(handle: Optional["KarenDuplexHandle"]) -> None:
    """Publish the mounted duplex handle for late-binding consumers (boot
    briefing). Same pattern as intake's set_default_voice_sensor."""
    global _DEFAULT_KAREN
    _DEFAULT_KAREN = handle


def get_default_karen() -> Optional["KarenDuplexHandle"]:
    return _DEFAULT_KAREN
```

In `audio_pipeline_bootstrap.py`: after `handle.karen = build_karen_duplex(handle.tts_engine)` + `await handle.karen.start()` add `set_default_karen(handle.karen)` (import alongside `build_karen_duplex`); in the `except` that sets `handle.karen = None` and in `shutdown()` where Karen stops, call `set_default_karen(None)`.

Run: `python3 -m pytest tests/voice/duplex/test_default_karen_handle.py tests/voice/duplex/ -q` → PASS (existing duplex suite still green).

- [ ] **Step 3: Write the failing harness-hook test**

```python
# tests/battle_test/test_awakening_hook.py
"""Cockpit hook: harness builds the conductor with briefing wired to
on_ignition; SOAK builds nothing (spec §3.5)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.harness import build_awakening_for_cockpit
from backend.core.ouroboros.ui.awakening import AwakeningConductor
from rich.console import Console
from backend.core.ouroboros.ui import theme


def test_builder_returns_wired_conductor(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    console = Console(file=open("/dev/null", "w"), force_terminal=True,
                      width=80, color_system="truecolor")
    theme.ensure_theme(console)
    conductor = build_awakening_for_cockpit(
        console, intake_probe=lambda: 2, approvals_probe=lambda: 0)
    assert isinstance(conductor, AwakeningConductor)
    assert conductor._on_ignition is not None      # briefing wired


def test_builder_returns_none_in_soak(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
    console = Console(file=open("/dev/null", "w"))
    assert build_awakening_for_cockpit(console, intake_probe=None,
                                       approvals_probe=None) is None
```

- [ ] **Step 4: Run to verify failure, then implement the harness hook**

Run: `python3 -m pytest tests/battle_test/test_awakening_hook.py -v` → FAIL.

4a. Add to `harness.py` (module level, near the top-level helpers):

```python
def build_awakening_for_cockpit(console, *, intake_probe=None, approvals_probe=None):
    """COCKPIT boot skin factory. Returns None in SOAK (spec §3.5). The
    briefing rides on_ignition fire-and-forget; its failure can never
    touch boot (breaker + NEVER-raises contract in the composer)."""
    from backend.core.ouroboros.ui.presentation_mode import is_cockpit
    if not is_cockpit():
        return None
    import asyncio
    from backend.core.ouroboros.ui.awakening import AwakeningConductor
    from backend.core.ouroboros.battle_test.boot_timing import get_default_timer
    from backend.core.ouroboros.governance.comms.karen_boot_briefing import (
        BootBriefing, build_default_synthesizer, compose_local, gather_vectors,
    )

    briefing_tasks = []

    def _speak_sink(line: str) -> None:
        try:
            from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (
                get_default_karen,
            )
            karen = get_default_karen()
            if karen is not None:
                karen.submit_speech(line)
                return
        except Exception:
            pass
        console.print(f"  \U0001f4ad Karen ▸ “{line}”",
                      style="muted", markup=False)

    def _on_ignition() -> None:
        briefing = BootBriefing(
            synthesizer=build_default_synthesizer(),
            speak_sink=_speak_sink,
        )
        briefing_tasks.append(asyncio.get_event_loop().create_task(briefing.run()))

    def _context_line() -> str:
        import datetime
        parts = [f"awakened {datetime.datetime.now().strftime('%H:%M')}"]
        try:
            n = intake_probe() if intake_probe else None
            if n:
                parts.append(f"{n} ops queued")
        except Exception:
            pass
        return " · ".join(parts)

    conductor = AwakeningConductor(
        console, timer=get_default_timer(),
        on_ignition=_on_ignition, context_provider=_context_line,
    )
    conductor._briefing_tasks = briefing_tasks     # keep references (no GC)
    return conductor
```

4b. In `harness.run()` locate the compact-boot-banner block (~1212-1259,
`self._serpent_flow.boot_banner(...)`). Branch (SerpentFlow owns the themed
console — `self._serpent_flow.console`, built via `theme.build_console` at
serpent_flow.py:679):

```python
    _awakening = build_awakening_for_cockpit(
        self._serpent_flow.console,
        intake_probe=self._intake_pending_probe,      # see 4c
        approvals_probe=None,
    )
    if _awakening is not None:
        self._awakening_task = asyncio.create_task(_awakening.run())
        self._awakening = _awakening
    else:
        # legacy SOAK banner path -- UNCHANGED
        self._serpent_flow.boot_banner(...)
```

4c. Add a small probe method on the harness class (it owns the intake layer):

```python
    def _intake_pending_probe(self) -> Optional[int]:
        try:
            router = getattr(self, "_intake_router", None)
            if router is not None and hasattr(router, "pending_ack_count"):
                return int(router.pending_ack_count())
        except Exception:
            pass
        return None
```

(Read the harness to find the real attribute holding the intake router /
layer — grep `intake` in `boot_intake()` (~line 3156) and use the actual
attribute name; `pending_ack_count` exists on `unified_intake_router`.)

4d. REPL handoff: where `SerpentREPL(` is constructed (~1366), pass
`initial_text=getattr(self, "_awakening", None) and self._awakening.typed_prefix or ""`
— add an `initial_text: str = ""` parameter to `SerpentREPL.__init__` that
pre-fills the first `prompt_async(default=initial_text)` call. Before the
REPL starts, `await self._awakening_task` if set (the ceremony must finish
before the prompt appears; it self-bounds at ~20s worst case).

- [ ] **Step 5: Run the tests + full affected suites**

Run: `python3 -m pytest tests/battle_test/test_awakening_hook.py tests/voice/ tests/ui/ -q 2>&1 | tail -5`
Expected: all PASS
Run: `python3 -m pytest tests/battle_test/ -q 2>&1 | tail -5`
Expected: green (SOAK regression intact)

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/karen_duplex_factory.py backend/audio/audio_pipeline_bootstrap.py backend/core/ouroboros/battle_test/harness.py tests/voice/duplex/test_default_karen_handle.py tests/battle_test/test_awakening_hook.py
git commit -m "feat(ov): cockpit awakening wired into harness boot -- briefing on ignition, REPL prefix handoff

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: guard extensions + full regression + ledger

**Files:**
- Modify: `tests/ui/test_theme_guard.py` (extend the banned-pattern net)
- Modify: `.superpowers/sdd/progress.md` (ledger entry at execution time)

- [ ] **Step 1: Extend the guard test**

Append to the guard's banned-pattern scan (matching its existing structure —
read it first) a check over `scripts/ouroboros_battle_test.py`:

```python
def test_boot_banners_only_called_via_gate():
    """The three banner functions may be CALLED only inside
    _run_gated_boot_banners (Mandate 1 made permanent)."""
    import pathlib, re
    src = pathlib.Path("scripts/ouroboros_battle_test.py").read_text()
    gate_body = src.split("def _run_gated_boot_banners", 1)[1].split("\ndef ", 1)[0]
    for fn in ("_print_preflight()", "_single_flight_preflight()"):
        calls_outside = src.count(fn) - gate_body.count(fn)
        assert calls_outside == 0, f"{fn} called outside the gate"
```

(If `_reap_zombies` took the `quiet=` route in Task 1, assert instead that
every call site passes an explicit `quiet=` argument.)

- [ ] **Step 2: Run everything**

Run: `python3 -m pytest tests/ui/ tests/cli/ tests/voice/ tests/battle_test/ -q 2>&1 | tail -8`
Expected: full green.

- [ ] **Step 3: Manual smoke (cockpit + soak skins)**

```bash
JARVIS_OV_PRESENTATION=cockpit python3 -c "
from backend.core.ouroboros.ui.crest import generate_crest
from backend.core.ouroboros.ui.theme import ColorTier
f = generate_crest(60, 24, tier=ColorTier.TRUECOLOR, unicode_ok=True)
print('crest cells:', len(f.cells), 'max_delay:', f.max_delay_s)"
```
Expected: a cell count > 200 and max_delay ≥ 1.55.
Then eyeball `ov run --help`-level SOAK output unchanged (no code path change).

- [ ] **Step 4: Commit**

```bash
git add tests/ui/test_theme_guard.py
git commit -m "test(ov): guard -- banner calls locked inside the presentation gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Acceptance (after all tasks): the live-mic gate

Local, interactive, on real hardware (spec §9.8) — this is the sprint's
finish line, run by the human:

1. `ov` → crest animates (trace → eye → V), cools to header + prompt.
2. Karen speaks a briefing referencing real state (queued ops / last session).
3. Speak over her mid-briefing → she stops (barge-in preemption).
4. Speak a build command → a real op appears in the governed loop.
5. Karen's own voice does not retrigger VAD (AEC holds).
6. `ov run` in a second terminal → full SOAK banners, byte-familiar.
7. `Esc` during a fresh `ov` boot → instant cooled prompt; typing `sta` during
   the animation leaves `sta` in the first prompt.
