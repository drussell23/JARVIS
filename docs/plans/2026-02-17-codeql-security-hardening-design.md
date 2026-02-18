# CodeQL Security Hardening Design

**Date:** 2026-02-17
**Scope:** Resolve 5,117 open CodeQL code scanning alerts via surgical security hardening
**Approach:** Approach A — Surgical Security Hardening (security-first, then correctness)

## Problem Statement

5,117 open CodeQL alerts on the JARVIS repo (`drussell23/JARVIS`). Breakdown:

| Severity | Count | Composition |
|----------|-------|-------------|
| Error    | 431   | `undefined-export` (311), `wrong-named-argument` (53), `uninitialized-local-variable` (23), `unsafe-cyclic-import` (12), `illegal-raise` (9), `non-iterable-in-for-loop` (7) |
| Warning  | 293   | `multiple-definition` (101), `unreachable-statement` (46), `file-not-closed` (30), `equals-hash-mismatch` (23), `redundant-comparison` (26), `unnecessary-pass` (27) |
| Note     | 4,013 | `unused-import` (~3,500), `unused-global-variable` (~300), `repeated-import` (~200) |

## Root Cause Analysis

Five systemic root causes, not 5,117 independent bugs:

1. **`__init__.py` lazy loading via `__getattr__`**: CodeQL doesn't model Python's `__getattr__` protocol. All `__init__.py` files use intentional dynamic imports for startup performance. This generates ~311 false-positive `py/undefined-export` errors.

2. **No centralized log sanitization enforcement**: `_sanitize_log()` exists in `backend/main.py` but is local to that file. Other files log user input without sanitization, causing ~90 log-injection alerts across Python and JS.

3. **Deprecated files still scanned**: `_deprecated_start_system.py` and `_deprecated_run_supervisor.py` generate ~30+ alerts on dead code.

4. **Test/utility files logging test credentials**: Files like `test_connector.py`, `setup_tts_voices.py`, `check_chirp_voices.py` intentionally log test API keys, generating false-positive `clear-text-logging-sensitive-data` alerts.

5. **No pre-commit security gates**: Nothing prevents new security issues from being introduced.

## Triage Results

Deep code inspection revealed ~74% of alerts are false positives:

### False Positives (~3,800+)
- **311 `py/undefined-export`**: `__getattr__` lazy loading — intentional, correct
- **~3,500 `py/unused-import`**: Side-effect imports, `TYPE_CHECKING` imports — cosmetic
- **Many `py/clear-text-logging-sensitive-data`**: Log secret IDs/names, NOT values (e.g., `"Failed to get 'anthropic-api-key'"`)
- **`py/weak-sensitive-data-hashing`**: SHA-256 flagged as "weak" — correct for API key verification
- **Some `py/log-injection`**: Already mitigated by existing `_sanitize_log()`

### True Positives (~50-80 security alerts)
| Category | ~Count | Example |
|----------|--------|---------|
| `py/overly-permissive-file` | ~10 | `epoch_fencing.py:430` uses `0o644` instead of `0o600` |
| `js/remote-property-injection` | ~6 | `dynamic-websocket.js:256` copies `__proto__` from untrusted data |
| `js/xss` | ~3 | `loading-manager.js:1420` — `source` not escaped in `innerHTML` |
| `py/log-injection` (unmitigated) | ~20 | Files logging user input without `_sanitize_log()` |
| `py/path-injection` | ~1 | `reactor_api_interface.py:430` |
| `py/clear-text-logging-sensitive-data` (real) | ~10 | Password-adjacent logging in keychain/voice unlock |

### Code Correctness Bugs (~230 alerts)
| Category | ~Count | Impact |
|----------|--------|--------|
| `py/uninitialized-local-variable` | 23 | Runtime NameError risk |
| `py/call/wrong-named-class-argument` | 31 | Silent parameter mismatch |
| `py/call/wrong-named-argument` | 22 | Wrong keyword args |
| `py/unsafe-cyclic-import` | 12 | Import-time crash risk |
| `py/illegal-raise` | 9 | Raising non-exceptions |
| `py/multiple-definition` | 101 | Name shadowing bugs |
| `py/unreachable-statement` | 46 | Dead code / logic errors |

## Design

### Phase 1A: CodeQL Configuration

**Edit:** `.github/workflows/codeql-analysis.yml` — extend `paths-ignore` to exclude deprecated and test utility files.

### Phase 1B: Centralized Sanitization Utility

**New file:** `backend/core/secure_logging.py`

Consolidates existing patterns from:
- `backend/main.py:_sanitize_log()` — control character stripping
- `backend/neural_mesh/agents/google_workspace_agent.py:_sanitize_for_logging()` — PII redaction

Provides:
- `sanitize_for_log(val, max_len)` — strip control chars, limit length
- `mask_sensitive(val, visible_prefix)` — mask credential values

Then update ~20 files with true log injection to import and use it.

### Phase 1C: True Security Fixes

1. **File permissions**: All `os.open(..., 0o644)` on sensitive files → `0o600`
2. **Prototype pollution**: Filter `__proto__`, `constructor`, `prototype` in JS object iteration
3. **XSS**: Escape all dynamic values in `innerHTML` contexts
4. **Log injection**: Apply `sanitize_for_log()` to user-controlled inputs
5. **Path injection**: Validate paths against allowed directories

### Phase 1D: Pre-commit Hooks

**New file:** `.pre-commit-config.yaml` with `bandit` for Python security linting.

### Phase 2: Code Correctness Bugs

Fix ~230 alerts in priority order:
1. `py/uninitialized-local-variable` (23) — runtime crash risk
2. `py/call/wrong-named-class-argument` (31) — silent mismatch
3. `py/unsafe-cyclic-import` (12) — import crash risk
4. `py/illegal-raise` (9) — raising non-exceptions
5. `py/multiple-definition` (101) — name shadowing
6. `py/unreachable-statement` (46) — dead code

### Phase 3: CI Gates

Add enforcement to CodeQL workflow: fail on new error-severity alerts.

## Decisions

| Decision | Rationale |
|----------|-----------|
| Extract `secure_logging.py` rather than inline fixes | Root cause is lack of shared utility. One module, all imports. |
| Suppress false positives via CodeQL config, not code changes | `__getattr__` lazy loading is correct Python — changing it to satisfy static analysis would regress startup time. |
| Exclude deprecated files from scanning, not delete them | Git history preserves them, but they shouldn't generate alerts on main. |
| Pre-commit with bandit only, not autoflake | Autoflake risks breaking side-effect imports in a 73K-line monolith. Bandit catches security issues at commit time. |
| Phase security fixes before correctness fixes | Security vulns have external impact. Correctness bugs are internal. |

## Files Modified

### Phase 1 (Security)
- `.github/workflows/codeql-analysis.yml` — extend paths-ignore
- `backend/core/secure_logging.py` — NEW: centralized sanitizer
- `backend/core/epoch_fencing.py` — fix `0o644` → `0o600`
- `backend/static/js/dynamic-websocket.js` — prototype pollution fix
- `backend/websocket/DynamicWebSocketClient.ts` — prototype pollution fix
- `frontend/public/loading-manager.js` — XSS fix (escape in innerHTML)
- ~20 files with unmitigated log injection — add `sanitize_for_log()` calls
- ~10 files with `0o644` permissions — change to `0o600`
- `.pre-commit-config.yaml` — NEW: bandit hook

### Phase 2 (Correctness)
- ~50+ files with uninitialized variables, wrong arguments, cyclic imports, etc.
