# Slice 20 — Delegated Provenance for Sanctioned Self-Modification

**Status:** BUILT 2026-07-15 — 36 adversarial tests green + E2E through the real
`Orchestrator._build_profile` → `RiskEngine.classify` chain. **As-built deviation
from §3 below (operator-mandated + DRY):** the provenance chain composes the
EXISTING `roadmap_reader` HMAC-SHA256 substrate (operator-signed
`.jarvis/roadmap.yaml`, `hmac.compare_digest`, signed via the `strategy_signer`
CLI — Slice 202) instead of git-commit signatures. The claim is a pointer
`{kind: roadmap_reader, goal_id}`; `delegated_provenance.verify_provenance_claim`
re-derives signature + goal existence + freshness (`JARVIS_PROVENANCE_MAX_AGE_S`)
+ per-file scope binding from the live signed document at classify time.
Files: NEW `governance/delegated_provenance.py` + `tests/governance/
test_delegated_provenance.py`; extended `risk_engine.py` (`OperationProfile.
provenance` + token-aware `_self_protection_verdict`), `orchestrator._build_profile`
(flag-gated source+claim threading), `work_order_sensor` + `roadmap_reader`
(claim attach). Master `JARVIS_DELEGATED_PROVENANCE_ENABLED` default-FALSE.
Honest residual: HMAC is symmetric — a leaked secret is forgeable; mitigated by
the APPROVAL_REQUIRED ceiling (a human sees every delegated diff) and follow-up:
scrub the secret from Venom bash child envs.

**Author:** O+V architecture session, 2026-07-14
**Origin:** the armed perception soak (`bt-2026-07-15-063421`) proved the perception
layer feeds substantive roadmap work, but every governance-touching repair was
blocked `self_modification_unsanctioned_source`. This closes the trust-delegation
gap between `WorkOrderSensor` and `risk_engine` — **without** weakening the cage.

---

## 1. The gap (observed live)

`WorkOrderSensor` (P0.2) stamps its signals `source="roadmap"`. In
`risk_engine.classify → _self_protection_verdict` (`risk_engine.py:318`), a
governance-class self-modification is graded:

| source | verdict |
|---|---|
| `architecture` | `APPROVAL_REQUIRED` — the one sanctioned Order-2 path |
| `exploration`, `roadmap` (`_UNTRUSTED_SELF_MOD_SOURCES`) | `BLOCKED` `exploration_self_modification` |
| any other / empty default | `BLOCKED` `self_modification_unsanctioned_source` |

Trust is keyed on the **source label** — a string the (untrusted) sensor
self-asserts. The label carries no proof the directive was authored by a human.

## 2. Root cause (Mandate 1 — no blanket whitelist)

The defect is **not** "roadmap should be trusted." A `roadmap` directive may be
(a) genuinely operator-authored, (b) tampered after authoring, or (c)
autonomously injected by O+V in a prior cycle and then "obeyed" this cycle — a
self-modification-laundering attack. A blanket `roadmap → sanctioned` whitelist
would authorize (b) and (c) as readily as (a).

The root cause is that **authority is delegated by an unverifiable label instead
of by verifiable provenance.** The fix is a provenance chain that
cryptographically/deterministically proves a *specific directive's exact text*
was authored by the human operator — and is structurally unforgeable by O+V.

## 3. The provenance chain — git-signed commit lineage (DRY; deterministic + cryptographic)

`.superpowers/sdd/progress.md` directives already live in git, and
`auto_committer.ov_signature_substring()` (`auto_committer.py:105`) **already
exists** to distinguish O+V-authored commits (`Co-Authored-By: Ouroboros+Venom
<ouroboros@jarvis.trinity>`) from human commits. We compose these, adding no new
storage.

Provenance derivation for a directive:

1. **`git blame`** the directive line → the commit that introduced its current text.
2. **O+V-absence:** that commit must NOT contain `ov_signature_substring()`
   (else it is O+V-authored → reject). Kills laundering case (c).
3. **Human signature:** `git verify-commit <sha>` passes against a key in the
   operator's trusted set (GPG/SSH signing). Config fallback
   (`JARVIS_PROVENANCE_REQUIRE_SIGNED_COMMITS=false`) accepts operator-identity
   authorship where commit-signing isn't enabled — but signature is the default.
4. **Content binding:** hash the directive's exact text → `directive_sha256`;
   the token binds to this hash, so any post-signing tampering (case b) breaks it.

The operator's signing key is **never in O+V's reach** — it lives in the
operator keyring / an env the Aegis env-scrubber pops exactly as it already pops
`ANTHROPIC_API_KEY`/`DOUBLEWORD_API_KEY` (`aegis/env_scrub.py`). O+V therefore
*structurally cannot mint* a valid signature.

## 4. The Provenance Token (Mandate 2 — the signed attribution context)

A schema-versioned, self-describing claim (mirrors the Slice 6
`build_attribution_evidence` evidence-block discipline):

```
provenance = {
  "schema_version":    "delegated_provenance.v1",
  "directive_id":      "<stable progress.md anchor>",
  "directive_sha256":  "<hash of the EXACT directive text>",
  "origin_commit":     "<sha from git blame>",
  "signer_identity":   "<verified key id / author>",
  "signature_verified": true|false,
  "ov_signature_absent": true|false,
  "proof_kind":        "git_signed_commit",
  "delegated_class":   "governance",          # never kernel / security
  "max_tier":          "APPROVAL_REQUIRED",    # never SAFE_AUTO
}
```

**Mint vs. verify separation (load-bearing):** `WorkOrderSensor` *packages* the
claim (it points at the directive + commit) but **asserts nothing about
validity** — a sensor is untrusted and cannot forge git state. The
**`ProvenanceVerifier` re-derives every field from ground truth** (live
`git blame`, live `git verify-commit`, live re-hash of the directive) at the
authority (`risk_engine`), treating the packaged claim only as a pointer. This is
the "verify at the authority, never trust the source" principle already used for
test-source attribution (Slice 6).

## 5. The Delegated Authority Matrix (Mandate 2 — dynamic, no hardcoded bypass)

A config-driven matrix (env/file) declaring which **high-fidelity sensors** MAY
present a provenance token, and the ceiling they may delegate to:

```
delegated_authority_matrix:
  - sensor: work_order
    provenance_required:     git_signed_commit
    eligible_selfmod_classes: [governance]        # NOT kernel, NOT security
    max_delegated_tier:      APPROVAL_REQUIRED      # human still confirms
```

Invariants:

- **Eligibility ≠ authorization.** Matrix membership only permits *presenting* a
  token; the `ProvenanceVerifier` is the real gate.
- **No file paths, no labels, no per-op bypass.** Keyed on sensor identity +
  verified provenance *class* only.
- **Ceiling-bounded.** The matrix can never grant `SAFE_AUTO` and never name the
  `kernel`/`security` self-mod classes — those remain unconditional-BLOCK for
  every source, delegated or not. The best a token achieves is
  `APPROVAL_REQUIRED` (identical to the `architecture` path — the human still
  confirms).
- **Additive-only**, like the existing env-additive sentinels
  (`_env_extra_sentinels`): grants eligibility, can never lower a floor.

## 6. Extending `_self_protection_verdict` (Mandate 3 — one gate, token-aware; no second bypass fn)

Add one field to the existing `OperationProfile` frozen dataclass
(`risk_engine.py:137`): `provenance: Optional[Mapping] = None` — the claim rides
the profile exactly as `source` already does. The orchestrator's profile builder
threads `evidence.provenance → profile.provenance` (the same seam that already
carries `source`).

Extend the **existing** gate (no new function):

```python
# inside _self_protection_verdict, governance self-mod branch, AFTER the
# kernel + security unconditional blocks and the `architecture` sanction:
if self._matches_any(file_strs, self._self_mod_sentinels()):
    if source in self._SANCTIONED_SELF_MOD_SOURCES:
        return APPROVAL_REQUIRED, "architecture_self_modification"

    # ── NEW: delegated-provenance path ──
    if provenance is not None and delegated_provenance_enabled():
        verdict = ProvenanceVerifier.verify(provenance, file_strs, matrix)
        if verdict.valid and matrix.permits(source, selfmod_class="governance"):
            tier = min_tier(verdict.max_tier, APPROVAL_REQUIRED)  # clamp
            return tier, "delegated_provenance_self_modification"
        # invalid / unpermitted → FALL THROUGH (no bypass)

    if source in self._UNTRUSTED_SELF_MOD_SOURCES:
        return BLOCKED, "exploration_self_modification"
    return BLOCKED, "self_modification_unsanctioned_source"
```

The kernel + security gates *above* this branch are untouched: provenance is
only ever consulted **inside** the `governance` self-mod class, and only ever
raises `BLOCKED → APPROVAL_REQUIRED`.

## 7. Bulletproof / fail-closed (Mandate 4)

Every non-happy path collapses to the **exact current block** — there is no code
path where a present-but-unverified token yields anything other than a block:

| Attack / condition | Why it fails | Resulting verdict |
|---|---|---|
| Hallucinated directive (never committed) | no `origin_commit` from blame | `BLOCKED self_modification_unsanctioned_source` |
| O+V-injected directive (prior cycle) | `ov_signature_absent=False` | `BLOCKED` |
| Tampered directive text | `directive_sha256` mismatch on re-hash | `BLOCKED` |
| Forged / wrong-key signature | `git verify-commit` fails | `BLOCKED` |
| Valid token, kernel/security file | kernel/security gate blocks *before* provenance is read | `BLOCKED` (unconditional) |
| Valid token claiming `SAFE_AUTO` | clamped to `APPROVAL_REQUIRED` | `APPROVAL_REQUIRED` |
| Token absent / master flag off | branch skipped | byte-identical to today |

Genuine operator-signed roadmap directive → governance repair reaches
`APPROVAL_REQUIRED` (human confirms), never auto-apply.

## 8. Flags (default-off — byte-identical rollback)

- `JARVIS_DELEGATED_PROVENANCE_ENABLED` — master, default **false**.
- `JARVIS_DELEGATED_AUTHORITY_MATRIX_PATH` — matrix config location.
- `JARVIS_PROVENANCE_REQUIRE_SIGNED_COMMITS` — default **true** (require the
  cryptographic signature; `false` accepts operator-identity authorship where
  signing isn't configured).
- Composes with the risk-tier floor (`risk_tier_floor.py`, strictest wins) — so
  paranoia mode / quiet-hours still clamp a delegated `APPROVAL_REQUIRED`.

## 9. Proof obligations (test matrix)

1. Operator-signed directive → roadmap governance edit reaches `APPROVAL_REQUIRED`.
2. O+V-AutoCommitter-signed directive → `BLOCKED`.
3. Hallucinated (uncommitted) directive → `BLOCKED`.
4. Tampered directive (hash mismatch) → `BLOCKED`.
5. Valid token, kernel file → `BLOCKED` (unconditional gate wins).
6. Valid token, security file → `BLOCKED` (unconditional gate wins).
7. Valid token claiming `SAFE_AUTO` → clamped to `APPROVAL_REQUIRED`.
8. Token absent / flag off → byte-identical regression pin.
9. Adversarial: forged signature / wrong signer key → `BLOCKED`.
10. Matrix does not list `work_order` → present token ignored → `BLOCKED`.

## 10. Files (new / touched)

- **NEW** `governance/delegated_provenance.py` — `ProvenanceToken`,
  `ProvenanceVerifier` (git-blame + signature + re-hash, all read-only, fail-closed),
  `DelegatedAuthorityMatrix`, `delegated_provenance_enabled()`.
- `governance/risk_engine.py` — `OperationProfile += provenance`;
  `_self_protection_verdict` token-aware (one branch, no new fn).
- `governance/intake/sensors/work_order_sensor.py` — mint the provenance *claim*
  (git blame + O+V-signature probe) onto the signal evidence at emit time.
- profile-builder seam (orchestrator `_build_profile`) — thread
  `evidence.provenance → profile.provenance`.
- `tests/governance/test_delegated_provenance.py` — the §9 matrix.

## 11. What this deliberately does NOT do

- Does not touch the kernel or security self-mod floors (unconditional-BLOCK stays).
- Does not grant any source `SAFE_AUTO` self-modification.
- Does not add a per-file or per-label bypass.
- Does not trust the sensor's asserted validity — only ground-truth re-derivation.
- Does not remove the human from the loop — delegation lands at `APPROVAL_REQUIRED`.
