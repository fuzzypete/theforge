# Epic: Smart Model Escalation — Adaptive Dev Model Selection

## Vision

When a spec repeatedly fails (persistent P1 findings, same errors across
retries), the coordinator should escalate to a more capable (and expensive)
model rather than burning cycles at the same capability level. The
escalation ladder is configurable: e.g., sonnet → codex → opus.

## Stories

### Phase 1: Core escalation
- [x] `smart-model-config.md` — smart_config_models in forge.yaml,
      auto-assign dev/review/synthesis profiles from a model list
- [x] `dev-model-escalation.md` — _escalate_dev_model() promotes dev
      profile when persistent P1 detected
- [x] `persistent-p1-detection.md` — fix _has_persistent_p1() to match
      on description similarity, not same-file guard

### Phase 2: Targeted retry
- [x] `targeted-fix-prompt.md` — build_fix_prompt() for review iterations,
      retry_reason routing in coordinator

### Phase 3: Budget-aware escalation
- [ ] Escalation should consider remaining budget — don't escalate to opus
      if only $0.50 remains (needs spec)
- [ ] Escalation metrics: track how often escalation leads to APPROVE vs.
      further failure, log to audit (needs spec)

## Definition of Done

- Persistent P1s trigger model escalation automatically
- Escalation ladder is configurable per project
- Budget-aware: won't escalate when insufficient budget remains
- Audit log shows which model tier produced the final result
