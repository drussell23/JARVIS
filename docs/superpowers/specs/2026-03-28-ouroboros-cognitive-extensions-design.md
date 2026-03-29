# Ouroboros Cognitive Extensions: Roadmap Sensor + Feature Synthesis Engine

**Date:** 2026-03-28
**Status:** Design approved, pending implementation
**Depends on:** Ouroboros Daemon (Zone 7.0) — merged to main
**Scope:** First of two specs (this: Roadmap + Synthesis; follow-up: Architecture Reasoning Agent)

---

## Preamble

The Ouroboros Daemon (Zone 7.0) is a proactive self-healing maintenance daemon. It finds bugs, dead code, unwired components, and fixes them autonomously. But it only responds to **what's wrong** — it cannot reason about **what's missing**.

This spec adds two cognitive layers that give Ouroboros the ability to understand WHERE the system is going (Roadmap Sensor) and WHAT capabilities are missing (Feature Synthesis Engine). Together they transform Ouroboros from a maintenance daemon into a development-aware organism.

### Governing Philosophy

**The Symbiotic AI-Native Manifesto v2 — Boundary Mandate:**

- **Deterministic skeleton:** Crawl paths, parse frontmatter, hash content, compose snapshots, check caches, detect staleness. Zero model calls.
- **Agentic nervous system:** Interpret roadmap evidence, identify capability gaps, synthesize hypotheses about what should exist but doesn't. Intelligence deployed where ignorance exists.

---

## Architecture: Two-Clock Model

The Roadmap Sensor and Feature Synthesis Engine operate on two independent clocks. REM Sleep consumes their cached artifacts.

```
CLOCK 1: RoadmapSnapshot (deterministic, frequent)
  Trigger: P0/P1 source change (file mtime/hash delta)
           OR scheduled (every 1h default)
  Work:    Crawl sources -> parse -> hash -> merge -> cache
  Cost:    Zero tokens (pure I/O + hashing)
  Output:  RoadmapSnapshot (versioned, cached at
           ~/.jarvis/ouroboros/roadmap/snapshot.json)
  Does NOT: Emit IntentEnvelopes. Does NOT call models.
            Optionally triggers Clock 2 on content_hash change.

CLOCK 2: Feature Synthesis (agentic, infrequent)
  Trigger: snapshot.content_hash changed since last synthesis
           AND min_interval elapsed (6h default)
           OR scheduled daily cron (UTC)
           OR REM epoch requests fresh synthesis (stale hypotheses)
  Work:    Tier 0 deterministic gap hints (no model)
           -> Doubleword 397B batch (compositional reasoning)
           -> Optional J-Prime dedup/validation
           -> Claude fallback on failure
  Cost:    $0.10-0.50 per synthesis pass (Doubleword)
  Output:  List[FeatureHypothesis] (cached at
           ~/.jarvis/ouroboros/roadmap/hypotheses.json)
  Guard:   Single-flight (asyncio.Lock). Coalesces rapid triggers.

CONSUMER: REM Sleep Epoch
  Reads:   Latest cached snapshot + hypotheses
  Merges:  FeatureHypotheses into ranked findings (prioritization only)
  Routes:  Hypotheses BYPASS ANALYZING (already synthesized by Clock 2)
           -> Direct to PATCHING via hypothesis_envelope_factory
  Does NOT: Re-run synthesis. Re-call 397B. Uses cache only.
  Staleness: If synthesized_for_snapshot_hash != current snapshot hash
             OR now - synthesized_at > 24h TTL
             -> Fire-and-forget trigger to Clock 2 (non-blocking)
```

### Boundary Principle Applied

- Clock 1 is deterministic skeleton: crawl, hash, merge, persist. No interpretation.
- Tier 0 gap hints are deterministic: spec says X, Oracle has no Y, emit hypothesis.
- Clock 2 synthesis is agentic: 397B reasons about gaps. Intelligence creates leverage.
- REM is consumer: reads cache, ranks findings, routes to governance. Never drives synthesis.

---

## Source Tiers

| Tier | Sources | Default | Role |
|------|---------|---------|------|
| P0 | `docs/superpowers/specs/`, `docs/superpowers/plans/`, `.jarvis/backlog.json`, workspace `memory/*.md`, `CLAUDE.md`, `AGENTS.md` | Always on | Authoritative intent + explicit queue |
| P1 | Bounded `git log` (last N commits, M days), diff stats | On with bounds | Execution trajectory — drift vs plans |
| P2 | GitHub issues per Trinity repo (JARVIS, Prime, Reactor) | Off (needs network) | External work queue — bugs/requests |
| P3 | `~/.claude/projects/.../memory/` | Off unless allowlisted | Cross-machine personal memory (privacy risk) |

Each source produces `SnapshotFragment` entries with explicit tier attribution. Conflict resolution: spec beats issue comment; backlog beats inferred; higher tier (lower number) wins.

---

## Schemas

### SnapshotFragment

```python
@dataclass(frozen=True)
class SnapshotFragment:
    """Single ingested source document with provenance."""
    source_id: str          # stable ID: "spec:ouroboros-daemon-design", "git:jarvis:bounded"
    uri: str                # "docs/superpowers/specs/2026-03-28-ouroboros-daemon-design.md"
    tier: int               # 0, 1, 2, 3
    content_hash: str       # SHA256 of file content
    fetched_at: float       # UTC epoch seconds (wall clock, not monotonic)
    mtime: float            # file modification time (UTC epoch seconds)
    title: str              # from frontmatter or first heading
    summary: str            # first 500 chars or frontmatter description
    fragment_type: str      # "spec", "plan", "backlog", "memory", "commit_log", "issue"
```

### RoadmapSnapshot

```python
@dataclass
class RoadmapSnapshot:
    """Versioned, cached organism self-awareness."""
    version: int                        # monotonic, increments iff content_hash changes
    content_hash: str                   # canonical: sha256("\n".join(sorted(
                                        #   f"{sf.source_id}\t{sf.content_hash}" for sf in fragments)))
    created_at: float                   # UTC epoch seconds
    fragments: Tuple[SnapshotFragment, ...]
    tier_counts: Dict[int, int]         # {0: 12, 1: 50, 2: 8}

    # Persistence: ~/.jarvis/ouroboros/roadmap/snapshot.json
```

**Version invariant:** `version` increments iff root `content_hash` changes after merge. Same hash = no new version.

**Canonical hash composition:** `sha256("\n".join(sorted(f"{sf.source_id}\t{sf.content_hash}" for sf in fragments)))` — explicit, deterministic, collision-resistant.

**Synthetic fragments** (git log, GitHub aggregate) use stable `source_id`s: `git:jarvis:bounded`, `github:issues:JARVIS-AI-Agent`.

### FeatureHypothesis

```python
@dataclass
class FeatureHypothesis:
    """A gap between where the system is going and where it is."""
    hypothesis_id: str                  # UUID (storage identity)
    hypothesis_fingerprint: str         # deterministic dedup key:
                                        # hash(normalized_description + sorted_evidence + gap_type)
    description: str                    # "The Manifesto specifies app-specific agents; none exist"
    evidence_fragments: Tuple[str, ...] # source_ids from snapshot
    gap_type: str                       # "missing_capability", "incomplete_wiring",
                                        # "stale_implementation", "manifesto_violation"
    confidence: float                   # 0-1
    confidence_rule_id: str             # "spec_symbol_miss", "model_inference", etc.
    urgency: str                        # critical, high, normal, low
    suggested_scope: str                # "backend/neural_mesh/agents/"
    suggested_repos: Tuple[str, ...]    # ("jarvis",) or ("jarvis", "jarvis-prime")
    provenance: str                     # "deterministic", "model:doubleword-397b", "model:claude"

    # Synthesis metadata (caching + staleness)
    synthesized_for_snapshot_hash: str  # which snapshot this was computed against
    synthesized_at: float               # UTC epoch seconds
    synthesis_input_fingerprint: str    # hash of fragment hashes sent to model +
                                        # prompt_version + model_id

    # Lifecycle
    status: str = "active"              # "active", "superseded", "implemented", "rejected"
```

**Dedup:** `hypothesis_fingerprint` is the merge key across synthesis runs. UUIDs are for storage; fingerprints are for identity.

**Staleness:** Hypotheses are stale when `synthesized_for_snapshot_hash != current_snapshot.content_hash` OR `now - synthesized_at > OUROBOROS_SYNTHESIS_TTL_S` (default 86400 = 24h). These conditions are OR, not AND — a hash mismatch means hypotheses are already inconsistent.

**Invalidation:** Cache entries are dropped/downgraded on: snapshot hash mismatch, prompt version bump, model change, manual `force_synthesis`, or governance outcome that supersedes a hypothesis (implemented/rejected).

---

## Clock 1: RoadmapSensor

### Purpose

Deterministic materialization of all enabled roadmap sources into a versioned `RoadmapSnapshot`. Zero model calls. Runs as a background task, not as an IntakeLayerService sensor (does not emit IntentEnvelopes — its job is to keep the snapshot fresh and optionally trigger Clock 2).

### Refresh Logic

1. Crawl all enabled source paths (P0 always, P1-P3 per config)
2. For each file: compute `content_hash` (SHA256). If unchanged from last snapshot -> reuse fragment.
3. For git log: bounded query (`git log --oneline -N`), hash the output. Stable `source_id`.
4. For GitHub issues: fetch with ETag caching, hash the response. Stable `source_id`.
5. Compose `content_hash` of snapshot via canonical formula.
6. If `content_hash` unchanged -> no-op. Return cached snapshot.
7. If changed -> persist new snapshot, increment version.
8. If synthesis trigger conditions met (hash changed + min_interval elapsed) -> signal Clock 2.

### Telemetry

Emits to TelemetryBus/logs (not IntakeRouter):
- `roadmap.snapshot.refreshed` (version, fragment_count, tier_counts, duration_s)
- `roadmap.snapshot.unchanged` (cached version reused)
- `roadmap.synthesis.triggered` (reason: delta/schedule/forced)

---

## Clock 2: FeatureSynthesisEngine

### Purpose

Agentic interpretation of the `RoadmapSnapshot` to produce `FeatureHypothesis` records. Runs on schedule + delta trigger. Guarded by single-flight lock and minimum interval.

### Single-Flight + Debounce

```python
class FeatureSynthesisEngine:
    def __init__(self, ...):
        self._synthesis_lock = asyncio.Lock()
        self._last_synthesis_at: float = 0.0

    async def synthesize(self, snapshot: RoadmapSnapshot, *, force: bool = False) -> List[FeatureHypothesis]:
        if self._synthesis_lock.locked():
            return self._load_cached()  # synthesis already in flight

        if not force:
            elapsed = time.time() - self._last_synthesis_at
            if elapsed < self._config.synthesis_min_interval_s:
                return self._load_cached()  # too soon

        async with self._synthesis_lock:
            return await self._run_synthesis(snapshot)
```

### Synthesis Pipeline

```
1. CHECK CACHE
   - Compute synthesis_input_fingerprint:
     hash(snapshot.content_hash + prompt_version + model_id)
   - If fingerprint matches cached -> return cached hypotheses (zero cost)

2. TIER 0: DETERMINISTIC GAP HINTS (zero tokens)
   - Parse P0 spec docs for capability references
     (heuristic keyword extraction: "agent", "sensor", "integration")
   - Cross-reference Oracle graph:
     * Spec says "WhatsApp agent", Oracle has no whatsapp symbol
       -> emit hypothesis(gap_type="missing_capability",
                          provenance="deterministic",
                          confidence=0.95,
                          confidence_rule_id="spec_symbol_miss")
     * Spec says "PredictivePlanningAgent", Oracle shows zero callers
       -> emit hypothesis(gap_type="incomplete_wiring",
                          provenance="deterministic",
                          confidence=0.90,
                          confidence_rule_id="spec_zero_callers")
   - Evidence: point to spec source_id + heading/line range where possible

3. TIER 1: DOUBLEWORD 397B BATCH (primary model)
   - Input: changed P0 fragments + Tier 0 hints + Oracle neighborhoods
   - Prompt: "Given this roadmap evidence, identify capability gaps
     between stated intent and current implementation.
     Return structured JSON array of hypotheses."
   - Submit via DoublewordProvider.submit_batch()
   - Poll via poll_and_retrieve()
   - Parse structured JSON -> List[FeatureHypothesis]
   - Each: provenance="model:doubleword-397b"

4. FALLBACK: CLAUDE API (on 397B failure/timeout/parse error)
   - Same prompt, synchronous call
   - provenance="model:claude-api"

5. OPTIONAL: J-PRIME 7B HYGIENE PASS
   - Dedup near-duplicate hypotheses (by hypothesis_fingerprint)
   - Validate evidence_fragments cite real snapshot source_ids
   - Severity bucketing
   - Does not change provenance (hygiene, not synthesis)

6. MERGE
   - Combine Tier 0 + model hypotheses
   - Dedup on hypothesis_fingerprint (deterministic wins for same gap)
   - Record synthesized_for_snapshot_hash, synthesized_at, synthesis_input_fingerprint
   - Persist to hypotheses.json
```

### Per-Fragment Delta (Cost Optimization)

When snapshot changes incrementally (one spec edited), synthesis can:
1. Identify which fragment hashes changed vs last synthesis
2. Re-run 397B only on changed fragments
3. Merge new hypotheses with unchanged ones (by hypothesis_fingerprint)
4. `synthesis_input_fingerprint` includes which fragment hashes were sent to the model (Merkle summary), not only global snapshot hash — prevents false-cache when only un-sent fragments changed.

---

## REM Epoch Integration

### Hypothesis Consumption (No Double 397B)

Hypotheses are already the output of Tier 0 + 397B + merge. They MUST NOT enter ANALYZING again.

```
REM Epoch flow (updated):

EXPLORING
  |-- Oracle checks (dead code, circular deps)     existing
  |-- ExplorationFleet deploy                       existing
  |-- Load cached FeatureHypotheses                 NEW
  |      Convert to RankedFinding for prioritization
  |      (blast_radius from gap_type heuristic,
  |       confidence from hypothesis, source_check tagged)
  |
  v
merge_and_rank(all_findings)
  |
  v
PATCHING (split into two streams)
  |
  |-- Exploration findings -> findings_to_envelopes()     existing
  |      (source="exploration", may go through ANALYZING in pipeline)
  |
  |-- Roadmap hypotheses -> hypotheses_to_envelopes()     NEW
  |      (source="roadmap", already synthesized by Clock 2)
  |      These envelopes carry evidence["analysis_complete"]=True
  |      so the governance pipeline skips re-analysis
  |
  v
IntakeRouter.ingest() for both streams
```

**blast_radius heuristic for hypothesis gap_types:**
- `missing_capability` = 0.5
- `incomplete_wiring` = 0.3
- `stale_implementation` = 0.2
- `manifesto_violation` = 0.7

### Staleness Check in REM

```python
def _hypotheses_are_stale(self, hypotheses, current_snapshot) -> bool:
    if not hypotheses:
        return True
    latest = max(h.synthesized_at for h in hypotheses)
    hash_mismatch = any(
        h.synthesized_for_snapshot_hash != current_snapshot.content_hash
        for h in hypotheses
    )
    age_exceeded = (time.time() - latest) > self._config.synthesis_ttl_s
    return hash_mismatch or age_exceeded  # OR, not AND
```

If stale: fire-and-forget trigger to Clock 2 (non-blocking). Use cached hypotheses for this epoch.

---

## IntentEnvelope Bridge

### New Source: "roadmap"

Add `"roadmap"` to `_VALID_SOURCES` in `intent_envelope.py`.

Priority in `_PRIORITY_MAP`:
```python
"exploration": 4,
"roadmap": 4,      # same priority tier, tie-break by created_at (deterministic)
```

Tie-breaking: when priority integers are equal, router uses `envelope.submitted_at` (earliest wins). This is deterministic and stable.

### RiskEngine Rules

`source="roadmap"` inherits the same governance tier as `source="exploration"`:
- Cannot modify kernel (unified_supervisor.py)
- Cannot self-modify Ouroboros code
- Cannot modify security surface
- blast_radius > 3 -> APPROVAL_REQUIRED
- `source` field visible in all audit/telemetry for future stricter rules.

### hypothesis_envelope_factory.py

```python
def hypotheses_to_envelopes(
    hypotheses: List[FeatureHypothesis],
    *,
    snapshot_version: int,
) -> List[IntentEnvelope]:
    """Convert feature hypotheses into IntentEnvelopes.

    source="roadmap". evidence includes hypothesis_id, provenance,
    analysis_complete=True (bypasses ANALYZING in governance pipeline).
    """
```

---

## Ownership and Wiring

### Where FeatureSynthesisEngine Lives

GLS starts the synthesis scheduler task. The engine itself is **stateless aside from cache files**. It is NOT a governance operation — it does not run inside a governed transaction.

```python
# In GovernedLoopService.start(), after existing scheduler wiring:
self._synthesis_engine = FeatureSynthesisEngine(
    oracle=self._oracle,
    doubleword=self._doubleword_ref,
    config=synthesis_config,
)
self._roadmap_sensor = RoadmapSensor(
    config=roadmap_config,
    repo_registry=self._repo_registry,
    on_snapshot_changed=self._synthesis_engine.trigger,
)
```

### REM Access

OuroborosDaemon passes cached hypotheses path to RemSleepDaemon. REM reads the file, never calls the engine directly.

---

## File Structure

### New Files

```
backend/core/ouroboros/roadmap/
  __init__.py
  snapshot.py              # RoadmapSnapshot, SnapshotFragment schemas
  hypothesis.py            # FeatureHypothesis schema + fingerprinting
  sensor.py                # RoadmapSensor (Clock 1)
  source_crawlers.py       # Tier-specific crawlers (P0 specs, P1 git, P2 issues)
  tier0_hints.py           # Deterministic gap detection (zero tokens)
  synthesis_engine.py      # FeatureSynthesisEngine (Clock 2)
  hypothesis_cache.py      # Exact fingerprint + per-fragment delta cache
  hypothesis_envelope_factory.py  # FeatureHypothesis -> IntentEnvelope

tests/core/ouroboros/roadmap/
  test_snapshot.py
  test_hypothesis.py
  test_sensor.py
  test_source_crawlers.py
  test_tier0_hints.py
  test_synthesis_engine.py
  test_hypothesis_cache.py
  test_hypothesis_envelope_factory.py
  test_integration.py
```

### Modified Files

| File | Change |
|------|--------|
| `intent_envelope.py` | Add `"roadmap"` to `_VALID_SOURCES` |
| `unified_intake_router.py` | Add `"roadmap"` to `_PRIORITY_MAP` (priority 4, tie-break by submitted_at) |
| `risk_engine.py` | Add `source="roadmap"` to exploration-level rules |
| `rem_epoch.py` | Add `_load_cached_hypotheses()` + hypothesis -> RankedFinding conversion |
| `daemon_config.py` | Add roadmap/synthesis env vars |
| `governed_loop_service.py` | Wire RoadmapSensor + FeatureSynthesisEngine |
| `daemon.py` | Pass hypothesis cache path to RemSleepDaemon |

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_ROADMAP_ENABLED` | `true` | Master toggle for roadmap sensor |
| `OUROBOROS_ROADMAP_REFRESH_S` | `3600` | Snapshot refresh interval (seconds) |
| `OUROBOROS_ROADMAP_P1_ENABLED` | `true` | Git log ingestion |
| `OUROBOROS_ROADMAP_P1_COMMIT_LIMIT` | `50` | Max commits to ingest |
| `OUROBOROS_ROADMAP_P1_DAYS` | `30` | Max days of git history |
| `OUROBOROS_ROADMAP_P2_ENABLED` | `false` | GitHub issues (needs network + token) |
| `OUROBOROS_ROADMAP_P3_ENABLED` | `false` | Personal memory (opt-in) |
| `OUROBOROS_SYNTHESIS_ENABLED` | `true` | Feature synthesis toggle |
| `OUROBOROS_SYNTHESIS_MIN_INTERVAL_S` | `21600` | Min 6h between synthesis runs |
| `OUROBOROS_SYNTHESIS_SCHEDULE_CRON` | `0 6 * * *` | Daily at 6 AM UTC |
| `OUROBOROS_SYNTHESIS_TTL_S` | `86400` | Hypothesis freshness TTL (24h) |
| `OUROBOROS_SYNTHESIS_PROMPT_VERSION` | `1` | Cache invalidation on prompt changes |

---

## Testing Strategy

- **Snapshot schemas:** Unit tests for hashing, version invariant, canonical composition
- **Source crawlers:** Unit tests with tmp_path fixtures (mock spec files, git output)
- **Tier 0 hints:** Unit tests with mock Oracle graph (symbol exists/missing/zero callers)
- **Synthesis engine:** Mock Doubleword, verify structured output parsing, cache hit/miss, single-flight
- **Hypothesis cache:** Property tests for fingerprint determinism, invalidation conditions
- **REM integration:** Mock cached hypotheses, verify they bypass ANALYZING, test staleness check
- **Envelope factory:** Same pattern as exploration_envelope_factory tests
- **End-to-end:** Full Clock 1 -> Clock 2 -> REM consumption with mocked providers

---

## Day 1 Capabilities

Once implemented, Ouroboros can:

| What | How | Example |
|------|-----|---------|
| Know the roadmap | Snapshot ingests specs + plans + backlog | "The Manifesto v2 has 7 pillars" |
| Detect missing capabilities | Tier 0 cross-references Oracle | "Spec says WhatsApp agent, none exists" |
| Detect incomplete wiring | Tier 0 finds zero-caller symbols | "PredictivePlanningAgent exists but voice bypasses it" |
| Synthesize gap hypotheses | 397B reasons about intent vs reality | "12 dormant autonomy files need a coordinator" |
| Route gaps to governance | hypothesis -> envelope -> pipeline | Git PR: "Wire PredictivePlanningAgent into voice" |
| Cache efficiently | Exact fingerprint + per-fragment delta | Daily synthesis costs ~$0.10-0.50 |

## Future: Architecture Reasoning Agent (Separate Spec)

The Architecture Reasoning Agent is the next extension. It consumes stable artifacts from this system (IntentEnvelopes, FeatureHypotheses, Oracle graph neighborhoods) and produces multi-file, multi-repo design documents — not just patches. It will be designed as a separate spec after this system is implemented and validated.
