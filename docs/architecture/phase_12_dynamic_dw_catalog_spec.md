# Phase 12 — Dynamic DW Catalog Discovery

**Status:** SPEC (DRAFT) — not yet implemented
**Mandate:** [ARCHITECTURAL DIRECTIVE: DYNAMIC CAPABILITY DISCOVERY (O+V)] (2026-04-27)
**Purges:** the hardcoded `dw_models:` ranked arrays in `brain_selection_policy.yaml` (lines 374–414)
**Replaces with:** live `/models` polling + algorithmic route assignment + adaptive probing

## 1. Why this exists

The Phase 11 graduation soak surfaced that across 6 consecutive battle-test sessions, `dw_completes=0` — every BACKGROUND/SPECULATIVE op was YAML-blocked with a stale 2026-04-14 reason text ("Gemma 4 31B stream-stalls"), even though Gemma is no longer in any generative ranked list. The static lists are 8 model_ids, hand-curated, last touched 2026-04-27. DoubleWord exposes a larger live catalog. **A First-Order sovereign system discovers its own capabilities; it does not consult a human-edited array.**

This spec replaces the *catalog* (which models exist) while preserving the *policy* (`fallback_tolerance`, `block_mode`, `dw_allowed`) — those remain operator-authored because they encode value judgments (cost contract, blast radius), not facts about the world.

## 2. Existing seams (no rewrite needed)

| Surface | File:line | Current behavior | Phase 12 change |
|---|---|---|---|
| `/models` HTTP call | `doubleword_provider.py:1918` | Binary `health_probe()` — discards body | Parse body into `List[ModelCard]` |
| Per-route ranked list | `provider_topology.py:352` `dw_models_for_route()` | Reads YAML `routes[route].dw_models` | Reads `_dynamic_catalog[route]` when discovery enabled, falls back to YAML when stale/disabled |
| Sentinel dispatch | `candidate_generator.py:1985–1995` walks `topology.dw_models_for_route()` | Iterates ranked list, attempts each non-OPEN model | **No change** — same iteration, different source |
| Sentinel preflight | `topology_sentinel.py:1697` `preflight_check()` | Synchronous shape check | Adds optional async `discover_catalog()` step that hydrates `_dynamic_catalog` |

The dispatch path stays untouched — the entire change is in the *catalog source*, not the *cascade logic*.

## 3. New components

### 3.1 `dw_catalog_client.py` (new module, ~200 lines)

```python
@dataclass(frozen=True)
class ModelCard:
    model_id: str                          # e.g. "moonshotai/Kimi-K2.6"
    family: str                            # parsed from prefix: "moonshotai", "qwen", "google", "zai-org"
    parameter_count_b: float | None        # 397.0 for Qwen3.5-397B; None when unparseable
    context_window: int | None             # from API metadata when available
    pricing_in_per_m_usd: float | None
    pricing_out_per_m_usd: float | None
    supports_streaming: bool               # default True; lowered if API metadata says otherwise
    raw_metadata: Dict[str, Any]           # preserved for downstream classifiers

@dataclass(frozen=True)
class CatalogSnapshot:
    fetched_at_unix: float
    models: Tuple[ModelCard, ...]
    schema_version: str = "dw_catalog.1"
    fetch_latency_ms: int = 0
    fetch_failure_reason: str | None = None  # populated when this is a fallback-to-cache snapshot

class DwCatalogClient:
    """Async client over DoublewordProvider's existing aiohttp session.
    Owns: fetch, parse, cache, refresh.
    Does NOT own: route assignment (that's the classifier's job)."""

    async def fetch(self) -> CatalogSnapshot: ...
    def cached(self) -> CatalogSnapshot | None: ...   # returns last good snapshot or None
    def stale(self, *, max_age_s: float) -> bool: ...
```

**Parsing contract for unknown response shape**: DW's `/models` follows the OpenAI-compatible `{"data": [...]}` envelope. Each model object has at least `id`. We tolerate missing optional fields (`context_window`, pricing) — the classifier in §3.2 is responsible for handling Nones gracefully (conservative downgrade). Param count parsed from `id` heuristic when not in API: regex `(\d+(?:\.\d+)?)B` on the trailing portion.

**Cache discipline**:
- Successful fetch → in-memory `CatalogSnapshot` + write-through to `.jarvis/dw_catalog.json` (atomic temp+rename, mirrored from `posture_store.py`)
- Boot reads disk snapshot first; `fetched_at_unix` decides whether to refresh
- Refresh interval: `JARVIS_DW_CATALOG_REFRESH_S` (default 1800s = 30 min)
- Failed fetch → return last good cached snapshot with `fetch_failure_reason` populated; sentinel logs warn-once

### 3.2 `dw_catalog_classifier.py` (new module, ~250 lines)

Deterministic, zero-LLM ranking. Classifier maps each `ModelCard` to a per-route score; per-route ranked lists are `top_k` of (route, score) sorted desc.

```python
@dataclass(frozen=True)
class RouteAssignment:
    route: str                              # "complex" | "standard" | "background" | "speculative"
    ranked_model_ids: Tuple[str, ...]       # output of classifier, replaces YAML dw_models

class DwCatalogClassifier:
    def classify(self, snapshot: CatalogSnapshot) -> Dict[str, RouteAssignment]: ...
```

**Ranking signals** (deterministic, env-tunable weights):

| Signal | Weight | Source | Notes |
|---|---|---|---|
| `parameter_count_b` | 1.0 | parsed from id, fallback None=0 | Bigger model = higher score for COMPLEX/STANDARD |
| `pricing_out_per_m_usd` | -1.0 | API metadata | Cheaper = higher score for BG/SPEC |
| `context_window` | 0.3 | API metadata | Larger = better for COMPLEX (long-horizon coding) |
| `family_bonus` | 0.5 | family prefix lookup | Operator-tunable: `JARVIS_DW_FAMILY_PREFERENCE` env (e.g. "moonshotai:1.0,zai-org:0.8") |

**Per-route eligibility gates** (hard filters before scoring):

| Route | Min params | Max output $/M | Other |
|---|---|---|---|
| COMPLEX | ≥ 30B | none | ctx ≥ 100k preferred |
| STANDARD | ≥ 14B | ≤ $2.0/M | |
| BACKGROUND | none | ≤ $0.5/M | cheap-first |
| SPECULATIVE | none | ≤ $0.1/M | ultra-cheap |

Hard filters are env-overridable via `JARVIS_DW_CLASSIFIER_<ROUTE>_*` knobs. **Defaults are conservative** — wider gates can be opened post-soak.

**Zero-Trust quarantine for ambiguous metadata** (operator-mandated 2026-04-27):
A model with `parameter_count_b is None` AND `pricing_out_per_m_usd is None` is **mathematically unsafe** to slot into BACKGROUND. BG runs continuously and high-volume; an unpriced 400B model in that lane could bankrupt the system on a single sustained sensor cycle. Such models are slotted **EXCLUSIVELY** into SPECULATIVE — the route with the strictest cost/queue governors and the smallest blast radius (queue-only fallback, ultra-cheap-only ranking, isolated from the core operational loop).

Promotion from quarantine is **latency-driven**, not metadata-driven (see §4.5 "Prove-It Promotion Ledger"): a quarantined model graduates to BACKGROUND only after demonstrating consistent sub-200ms latency across 10 successful operations. Latency is the proxy for size — small models respond faster — and we trust observed performance over self-reported metadata. Until it earns its weight, it stays quarantined.

### 3.3 `provider_topology.py` augment (~60 lines added)

```python
class ProviderTopology:
    def __init__(self, ...):
        ...
        self._dynamic_catalog: Dict[str, RouteAssignment] | None = None  # populated by sentinel
        self._dynamic_catalog_fetched_at: float | None = None

    def set_dynamic_catalog(
        self, assignments: Dict[str, RouteAssignment], fetched_at: float,
    ) -> None:
        """Sentinel-injected catalog. Authoritative when present and fresh."""

    def dw_models_for_route(self, route: str) -> Tuple[str, ...]:
        if self._dynamic_catalog and self._catalog_fresh():
            assn = self._dynamic_catalog.get(route)
            if assn and assn.ranked_model_ids:
                return assn.ranked_model_ids
        # Fall through to YAML (legacy / fallback path)
        ...existing logic...
```

**Catalog freshness**: `_catalog_fresh()` checks `time.time() - fetched_at < JARVIS_DW_CATALOG_MAX_AGE_S` (default 7200s = 2h). Stale catalog → fall back to YAML, log warn-once. **The YAML stays as the safety net throughout Phase 12** — it's only purged in Slice D after 3 clean soak sessions on the dynamic source.

### 3.4 Sentinel preflight extension (~50 lines)

```python
async def preflight_check(...) -> SentinelPreflightResult:
    ...existing checks...

    # Phase 12 — Dynamic catalog discovery (gated by JARVIS_DW_CATALOG_DISCOVERY_ENABLED)
    if catalog_discovery_enabled():
        try:
            client = DwCatalogClient(provider=...)
            snapshot = await client.fetch()
            classifier = DwCatalogClassifier()
            assignments = classifier.classify(snapshot)
            topology.set_dynamic_catalog(assignments, snapshot.fetched_at_unix)
            diagnostics.append(
                f"catalog_loaded:models={len(snapshot.models)}:"
                f"routes_assigned={sum(1 for a in assignments.values() if a.ranked_model_ids)}"
            )
        except Exception as exc:
            diagnostics.append(f"catalog_fetch_failed:{type(exc).__name__}:{str(exc)[:80]}")
            # NOT a failed_assertion — discovery failure falls back to YAML, system stays healthy
```

Discovery failure is a *diagnostic*, not a failed assertion — preflight stays healthy and the YAML safety net handles the gap.

### 3.5 Adaptive refresh task (~80 lines)

Background `asyncio.Task` owned by `TopologySentinel`. Every `JARVIS_DW_CATALOG_REFRESH_S` (default 1800s):
1. Fetch fresh catalog
2. Diff against previous snapshot
3. New `model_id` detected → `sentinel.register_endpoint(model_id)` + classifier slots it into a route
4. Removed `model_id` → leave breaker state intact (DW might re-add it); just stop using it
5. Emit `dw_catalog_updated` event over `TrinityEventBus` for IDE observability

**No SSE bridge yet in Phase 12** — only `TrinityEventBus` publish. SSE bridge is a follow-up if operators want live observability.

### 3.6 Prove-It Promotion Ledger (`dw_promotion_ledger.py`, ~150 lines)

**Owns:** observed-latency tracking + quarantine state + graduation decisions.
**Authority:** read-only telemetry consumed by classifier; classifier writes promotion events.

```python
@dataclass(frozen=True)
class PromotionRecord:
    model_id: str
    quarantine_origin: str          # "ambiguous_metadata" | "unranked_new" | "operator_demoted"
    success_latencies_ms: Tuple[int, ...]   # bounded ring buffer, length 10
    failure_count: int                       # cumulative failures while in quarantine
    promoted: bool = False
    promoted_at_unix: float | None = None

class PromotionLedger:
    def record_success(self, model_id: str, latency_ms: int) -> None: ...
    def record_failure(self, model_id: str) -> None: ...
    def is_eligible_for_promotion(self, model_id: str) -> bool: ...
    def promote(self, model_id: str) -> None: ...
    def quarantined_models(self) -> Tuple[str, ...]: ...
```

**Promotion criteria (ALL must hold):**
- ≥ `JARVIS_DW_PROMOTION_MIN_SUCCESSES` (default 10) successful ops recorded
- **Every** recorded latency ≤ `JARVIS_DW_PROMOTION_MAX_LATENCY_MS` (default 200) — strict, not P95
- Zero failures since last `record_success` (`failure_count == 0` for the current ring window)

**Demotion criteria** (back to quarantine from BG):
- Single failure during BG operation → resets the ring buffer; model returns to SPECULATIVE
- `JARVIS_DW_PROMOTION_DEMOTION_FAIL_THRESHOLD` (default 1) — zero-tolerance by default

**Persistence**: `.jarvis/dw_promotion_ledger.json`, atomic temp+rename. Ledger survives restart so quarantine state isn't reset on every boot.

**Wiring**: classifier in §3.2 reads the ledger when assembling per-route assignments. If `model_id` is in `quarantined_models()`, it's pinned to SPECULATIVE regardless of metadata. If it's in `promoted_models()`, the classifier may consider it for BACKGROUND (subject to other eligibility gates). The classifier never short-circuits the gates — promotion enables consideration, doesn't guarantee placement.

**Observability**: every promotion/demotion fires a `dw_model_promoted` / `dw_model_demoted` event over `TrinityEventBus` with the full `PromotionRecord` snapshot. Operators can audit the entire quarantine→promotion lifecycle from the IDE observability stream.

**Cost ceiling on quarantine itself**: SPECULATIVE has a hardcoded `cost_cap_usd` per op enforced by `cost_governor.py`. Even if a quarantined ghost-model returns 100M tokens, the cost is capped at the SPECULATIVE budget profile (~$0.001/op). The user's "unpriced 400B model bankrupts BG" failure mode is structurally impossible from quarantine.

## 4. Failure modes & cost contract

| Scenario | Behavior | Cost contract impact |
|---|---|---|
| `/models` returns 200 but empty `data` | Snapshot recorded with `len(models)=0`; classifier returns empty ranked lists; sentinel falls through to YAML | Same as today — YAML fully authoritative |
| `/models` 5xx or timeout | Returns last cached snapshot; if no cache, falls through to YAML | Same as today |
| Catalog fresh but classifier finds 0 eligible models for a route | Empty ranked list → sentinel sees empty → falls through to YAML for that route | YAML still authoritative for that one route |
| New model auto-assigned but immediately fails | Sentinel circuit breaker trips that model_id alone; remaining ranked list still tried | BG/SPEC routes still respect `fallback_tolerance: "queue"` after exhaustion → no Claude cascade |
| Classifier misranks (e.g. tiny model in COMPLEX) | Real failures observed, breaker trips, lower-ranked models tried; eventually cascades to Claude per existing `fallback_tolerance: "cascade_to_claude"` | Same blast radius as today's COMPLEX-on-Claude path |
| Pricing metadata missing on a model | Classifier conservatively slots to BACKGROUND only | No cost surprise — BG `fallback_tolerance: "queue"` |

**The cost contract is preserved structurally** because `fallback_tolerance` lives in YAML, not the catalog. The catalog only changes *which models* try; *what happens when they fail* is operator-authored policy.

## 5. Slicing plan (5 slices, all defaults false until Slice E)

### Slice A — Catalog client + tests (no integration)
- `dw_catalog_client.py` + `test_dw_catalog_client.py`
- Mock `aiohttp` responses for: clean fetch, 5xx, timeout, malformed JSON, missing optional fields
- Disk cache atomic write/read tests (mirror `posture_store` test pattern)
- Master flag `JARVIS_DW_CATALOG_DISCOVERY_ENABLED` (default false) added but not consumed yet

### Slice B — Classifier + tests
- `dw_catalog_classifier.py` + `test_dw_catalog_classifier.py`
- Deterministic ranking pinned: same input → same per-route lists
- Edge cases: missing metadata, empty catalog, exotic family prefix, env-tuned weights

### Slice C — Sentinel preflight wiring (shadow mode)
- `preflight_check()` calls discovery when flag on; populates `_dynamic_catalog` but `dw_models_for_route()` still reads YAML (one-flag-gated comparison phase)
- Diagnostic field `catalog_yaml_diff` — list of model_ids in YAML but missing from catalog and vice versa
- Manual operator review of diagnostic before flipping authority

### Slice D — Authority handoff
- `dw_models_for_route()` reads `_dynamic_catalog` first when fresh + discovery enabled
- YAML becomes fallback only
- Adaptive refresh task active
- Three forced-clean soak sessions required before Slice E

### Slice E — YAML purge + graduation
- Remove `dw_models:` arrays from `brain_selection_policy.yaml` (lines 374, 385, 401, 412)
- Keep `fallback_tolerance`, `block_mode`, `dw_allowed`, `reason` (those are policy, not catalog)
- Refresh stale 2026-04-14 reason strings to current truth
- Flip `JARVIS_DW_CATALOG_DISCOVERY_ENABLED` default to true
- Graduation pin suite: catalog-fresh path, catalog-stale fallback, discovery-disabled fallback, new-model auto-detection, classifier determinism

## 6. Env flag surface (all default false until Slice E)

| Flag | Default | Owner |
|---|---|---|
| `JARVIS_DW_CATALOG_DISCOVERY_ENABLED` | false → true at Slice E | Master flag |
| `JARVIS_DW_CATALOG_REFRESH_S` | 1800 | Refresh cadence |
| `JARVIS_DW_CATALOG_MAX_AGE_S` | 7200 | Freshness threshold |
| `JARVIS_DW_CLASSIFIER_<ROUTE>_MIN_PARAMS_B` | per §3.2 table | Eligibility gates |
| `JARVIS_DW_CLASSIFIER_<ROUTE>_MAX_OUT_PRICE` | per §3.2 table | |
| `JARVIS_DW_FAMILY_PREFERENCE` | unset (no bonus) | Operator family bias |

## 7. Out of scope (explicitly)

- **Capability tagging beyond size/price/family.** DW's `/models` may not expose benchmark scores, agentic-vs-chat capability, etc. Phase 12 ranks on universally-available metadata. Post-Phase-12, an `EvidenceLedger` of observed per-model success rates per route could feed back into ranking — that's a separate arc.
- **Cross-provider catalog merge.** Phase 12 is DW-only. Anthropic + Prime catalogs are static and well-known.
- **Self-healing of misclassifications via op-completion telemetry.** Tempting but defers to a follow-up that wires the existing `OpsDigestObserver` outputs into the classifier as a re-ranking feedback loop.
- **Operator override of dynamic ranking.** If needed, the YAML stays as a fallback that an operator can re-enable by flipping `JARVIS_DW_CATALOG_DISCOVERY_ENABLED=false` — that's the hot-revert path, not a parallel override.

## 8. Verification protocol

Each slice closes with:
1. Unit tests green
2. Combined regression (existing topology_sentinel + provider_topology + candidate_generator suites)
3. Live preflight against the actual DW endpoint (Slice A onward)
4. One battle-test session per Slice C / D with sentinel-on, observing the diagnostic delta

Slice E graduation requires the same forced-clean cadence as Phase 11.7: 3 consecutive sessions with `dw_completes ≥ 1` and zero `catalog_fetch_failed` on the diagnostic.
