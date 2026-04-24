# Issue #976 audit: score-to-band flattening at routing decision points

## score_to_band() callers

1. `src/theforge/coordinator/preflight_flow.py:313`
   - Classification: **(b) display/log only — leave as-is**
   - Notes: this is the compatibility shim that stores `state.preflight_complexity` for legacy consumers after parsing `complexity_score`. It remains useful as a display/log artifact and fallback input for unconverted sites.

## Band-keyed routing decisions audited

### Converted in this issue

1. `src/theforge/config/role_derivation.py`
   - Site: dev-role tier selection in `derive_roles()`
   - Previous routing: `small/medium/large -> cheap/mid/strong`
   - Classification: **(a) meaningful routing decision — convert to numeric score**
   - Change: added optional `complexity_score` input and now use `score_to_dev_tier()` when present, so score 6 and score 7 no longer route identically.

2. `src/theforge/coordinator/dev_phase.py`
   - Site: DEV timeout override selection via `resolve_timeout()`
   - Previous routing: only `medium` and `large` bands could trigger timeout overrides
   - Classification: **(a) meaningful routing decision — convert to numeric score**
   - Change: timeout resolution now prefers numeric score thresholds (`6-7 -> medium override`, `8-10 -> large override`) so two `medium` stories can receive different DEV timeouts.

3. `src/theforge/coordinator/plan_flow.py`
   - Site: PLAN timeout override selection via `resolve_timeout()`
   - Previous routing: only `medium` and `large` bands could trigger timeout overrides
   - Classification: **(a) meaningful routing decision — convert to numeric score**
   - Change: same numeric timeout policy as DEV, allowing same-band stories to diverge in PLAN timeout.

4. `src/theforge/coordinator/preflight.py`
   - Site: `_apply_complexity_adaptation()` dev model routing
   - Classification: **(a) meaningful routing decision — already numeric**
   - Notes: this site was already converted before this issue and uses `complexity_score` via `score_to_dev_tier()`.

### Leave as display/log only

1. `src/theforge/coordinator/dev_phase.py`
   - Site: `"Dev timeout: ... ({state.preflight_complexity} complexity)"`
   - Classification: **(b) display/log only — leave as-is**

2. `src/theforge/coordinator/plan_flow.py`
   - Site: `"Plan timeout: ... ({state.preflight_complexity} complexity)"`
   - Classification: **(b) display/log only — leave as-is**

3. `src/theforge/coordinator/audit.py`
   - Site: audit payload includes both `complexity` and `complexity_score`
   - Classification: **(b) display/log only — leave as-is**

4. `src/theforge/coordinator/review_phase.py`
   - Site: review audit payload includes `complexity`
   - Classification: **(b) display/log only — leave as-is**

5. `src/theforge/coordinator/validate_phase.py`
   - Site: validate audit payload includes `complexity`
   - Classification: **(b) display/log only — leave as-is**

6. `src/theforge/coordinator/preflight_flow.py`
   - Site: state update payload and preflight logs include `complexity`
   - Classification: **(b) display/log only — leave as-is**

### Out of scope / follow-up required

1. `src/theforge/coordinator/preflight_flow.py:342-351`
   - Site: contract-change upgrade `small -> medium` plus score bump to `5`
   - Classification: **(c) out of scope — file follow-up**
   - Rationale: this is policy coupling between contract-change detection and complexity escalation. It should likely become score-native, but changing it safely needs a separate policy decision.

2. `src/theforge/coordinator/preflight_flow.py:410-416`
   - Site: fallback contract-change upgrade in degraded preflight path
   - Classification: **(c) out of scope — file follow-up**
   - Rationale: same policy concern as above.

3. `src/theforge/config/role_derivation.py`
   - Site: plan-role and review-pool routing still keyed by normalized complexity band
   - Classification: **(c) out of scope — file follow-up**
   - Rationale: these are meaningful routing decisions, but widening them to score-native behavior changes more of the routing matrix and should be handled as a dedicated follow-up.

## Follow-up issues filed from this audit

- `#977` Convert contract-change complexity escalation in preflight flow from band bumping to score-native policy.
- `#978` Extend score-native routing beyond dev tier/timeouts to plan and review routing matrices in role derivation.
