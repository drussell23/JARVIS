---
title: Project Followup F5 Touches Security Surface Precision
modules: [backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/cancellation_token.py, backend/core/ouroboros/governance/risk_engine.py, token_bucket.py, authoring_helpers.py, credentials_format.py, secret_santa_generator.py, encryption_format_detector.py, secret_store.py]
status: historical
source: project_followup_f5_touches_security_surface_precision.md
---

## Status

- **Stub only.** Memory file is not the long-term home.
- **Not prioritized yet.** Operator directive 2026-04-24: "file in real tracker if possible; memory-only is okay as a stub but not the long-term home."
- **Out of scope for F1 branch** until explicitly widened. Fixture-side swaps (not code changes to `touches_security_surface`) remain the remediation path during F1 live graduation cadence.

## Problem

`backend/core/ouroboros/governance/orchestrator.py:8131-8134` `_build_profile` detects security-surface changes via naive substring match on path strings:

```python
touches_security = any(
    any(kw in str(p).lower() for kw in ("auth", "secret", "cred", "token", "encrypt"))
    for p in target_paths
)
```

Enforced by `risk_engine.py:326-330` as unconditional `RiskTier.BLOCKED`.

**False-positive class**: any file whose *filename* coincidentally contains one of these 5 substrings gets BLOCKED regardless of content. Confirmed false positive: `backend/core/ouroboros/cancellation_token.py` (asyncio cooperative cancellation primitive; "token" is the standard concurrency-primitive term, zero security relevance).

**Other illustrative false positives waiting to happen** (non-exhaustive):
- `token_bucket.py` — rate limiting
- `cancellation_token.py` — asyncio concurrency (confirmed)
- `authoring_helpers.py` — content authoring
- `credentials_format.py` — credential *format/parsing* (ambiguous but possibly benign)
- `secret_santa_generator.py` — literal Secret Santa game
- `encryption_format_detector.py` — detecting encryption format (ambiguous)

## Candidate fix approaches (to scope later, NOT in this stub)

### Option A — Path-segment match (narrow)

Replace substring-in-path with segment-in-path:

```python
segments = str(p).lower().split("/")
touches_security = any(
    any(kw == seg or seg.startswith(f"{kw}_") or seg.endswith(f"_{kw}.py")
        for kw in SECURITY_KW)
    for p in target_paths for seg in str(p).lower().split("/")
)
```

Catches `auth/handler.py`, `core/auth.py`, `secret_store.py`. Misses `cancellation_token.py` (the "token" is part of a longer word, not a segment).

### Option B — Explicit allowlist for known-benign substrings

Add an allowlist of specific filenames that contain false-positive substrings:

```python
KNOWN_BENIGN = {"cancellation_token.py", "token_bucket.py", "authoring_helpers.py", ...}
touches_security = any(
    Path(p).name not in KNOWN_BENIGN
    and any(kw in str(p).lower() for kw in SECURITY_KW)
    for p in target_paths
)
```

Brittle; requires maintenance as new files are added.

### Option C — Content-based detection (broader)

Replace path-regex with AST-level detection — scan candidate files for imports of known security modules (`cryptography`, `jwt`, `passlib`, `secrets`, `hashlib.pbkdf2_*`, etc.) and known security APIs.

Stronger signal, higher implementation cost. Manifesto §6 Iron Gate scope change — needs its own arc.

### Option D — Combine A + narrow denylist

Path-segment match PLUS a short denylist of truly benign-suffix filenames. Probably the best cost/benefit.

## Cross-links

- `project_f1_slice4_triage_a1_a2_a3.md` §A3 — original false-positive trace (bt-2026-04-24-091016)
- `backend/core/ouroboros/governance/orchestrator.py:8131-8134` — offending regex
- `backend/core/ouroboros/governance/risk_engine.py:326-330` — unconditional BLOCK enforcement

## Scope freeze

- **No code changes on F1 branch.**
- **Fixture-side swap is the remediation during F1 live graduation.**
- **When prioritized**: file in real tracker (GitHub issue / internal issue tracker), scope as its own arc with tests + slice doc, same graduation discipline as F1/F2.

## Open questions (for real-tracker scoping, not for this stub)

1. Is the "B" allowlist a maintenance drag we can accept, or is "C" worth the cost?
2. What is the right test surface — unit tests on `_build_profile` + `risk_engine.classify`, integration tests on live battle-test ops, or both?
3. Should the allowlist live in code or in a config file (YAML)?
4. Are there other naive-substring gates elsewhere in the governance surface? (Worth a grep for similar patterns.)
