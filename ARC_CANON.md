# ARC CANON

## 1. Identity

ARC is an AI system for creating readable, long-running serialized novels.

ARC is a generator, not a work title, genre, world, or narrative structure.
Each novel is an independent project with its own creative specification.

## 2. Primary Objective

Create novels that can continue for tens or hundreds of episodes while preserving:

- readability
- character consistency
- narrative continuity
- reader interest
- controlled pacing
- recoverable generation state

Development proceeds through increasing validation scope:

1. one-episode vertical loop
2. five-episode pilot
3. twenty-episode continuity validation
4. first volume
5. long-running series

No phase advances automatically without evidence from the previous phase.

## 3. Model Contract

Primary model:

- model: `gemma-4-31b-it`
- context window: 256k tokens
- maximum output: 32k tokens including reasoning
- RPM per key: 15
- TPM: unlimited
- RPD per key: 1500
- active key pool: 11 keys

Model name, key values, timeout, and output limits must be runtime configuration.
Secrets must never be logged, stored in artifacts, or committed.

## 4. Core Architecture

ARC uses a serial canonical-writing path with parallel support waves.

Canonical prose must be produced by one writer call.

Parallel workers may perform:

- pre-writing analysis
- alternative generation for selected decisions
- continuity checks
- readability checks
- post-draft review
- memory extraction

Parallel workers must not independently write fragments that are concatenated into canonical prose.

Workflow:

1. assemble writing context
2. run parallel planning wave
3. merge planning results
4. create episode plan
5. generate one canonical draft
6. run parallel review wave
7. merge review results
8. revise at most once
9. finalize episode
10. run parallel memory extraction
11. merge memory updates

## 5. Parallel Execution Policy

Maximum concurrent live calls:

```text
11
```

The system must not fill all slots without need.

Workers are fixed logical desks identified by stage and role. API keys are fungible physical execution resources leased from the active key pool; no desk permanently owns a key. On HTTP 408, HTTP 429, HTTP 5xx, timeout, network, or transport failure, only the affected key enters cooldown and another available key continues the same desk. Such transient key failures do not terminate an episode run.

Successful planning, review, and memory desks are saved in persistent partial checkpoints. After process interruption, ARC validates the checkpoint and resumes only unfinished desks.

Live admission requires at least one directly probed PASS key and no global configuration blocker. Transient or credential failures make admission degraded; they do not impose a fixed minimum healthy-key threshold.

Recommended worker count:

- ordinary episode: 4–7
- important episode: 8–11
- major transition: up to 11

Each specialist worker must return:

- verdict
- one primary finding
- one primary risk
- evidence references

Review workers must not produce unlimited issue lists.

The merged revision order may contain at most three required changes.

## 6. Rolling Planning Contract

Planning detail decreases with narrative distance.

### Series horizon

Store only:

- central reader promise
- protagonist's long-term drive
- major direction
- possible ending direction

### Current volume or major section

Store:

- destination
- major transitions
- unresolved strategic conflicts

### Near horizon

Maintain a rolling plan for approximately 5–10 episodes.

### Immediate horizon

Maintain detailed plans for the next 1–3 episodes.

### Current episode

Specify:

- immediate objective
- obstacle
- protagonist action
- meaningful change
- episode ending

Confirmed past events are binding.
Near-future plans are revisable.
Distant plans are hypotheses.

## 7. Episode Quality Contract

Each episode should normally contain:

- one primary event
- one clear protagonist objective
- one meaningful consequence or reward
- limited relationship change
- limited new terminology
- limited long-term revelation
- a reason to continue reading

The episode must not require every stored setting, mystery, or plan element to appear.

Review must explicitly detect:

- excessive exposition
- narrative heaviness
- inactive protagonist
- repeated information
- continuity conflict
- weak episode payoff
- artificial cliffhanger
- unnecessary setup for distant events

Literary density is not a quality metric by itself.

## 8. Canonical Writing Rule

Canonical prose is created as one coherent episode output.

For long or difficult episodes, ARC may create internal scene plans or scene alternatives.
The final episode must still be written or coherently rewritten by one canonical writer call.

The writer receives only material needed for the current episode.

The writer must not receive raw parallel-worker discussions.

## 9. Review and Revision Rule

Post-draft verdicts:

- `PASS`
- `REVISE_ONCE`
- `HOLD`

### PASS

The current draft may become canonical.

### REVISE_ONCE

A single bounded revision may resolve the identified problems.

Requirements:

- maximum three required changes
- preserve working strengths
- no unrelated expansion
- no new central conflict
- no second automatic revision

### HOLD

The episode cannot safely become canonical through one bounded revision.

HOLD stops the episode loop.

## 10. Long-Term Memory Contract

ARC must not depend on full-history context.

Persistent memory is separated into:

- series compass
- world rules
- character records
- confirmed fact ledger
- relationship state
- open conflicts and promises
- episode summaries
- important source excerpts
- rolling future plan

Recent episode prose may be included directly.
Older prose must be retrieved only when relevant.

Facts and summaries must remain separate.

### Confirmed facts

Objective continuity records.

### Episode summaries

Compressed narrative and emotional flow.

Summaries must not overwrite confirmed facts.

## 11. Context Assembly Contract

The 256k context window is a working surface, not permanent storage.

Recommended operating input:

```text
120k–180k tokens
```

Normal hard operating ceiling:

```text
approximately 200k tokens
```

Context assembly must prioritize:

1. current episode requirements
2. recent canonical prose
3. directly relevant confirmed facts
4. active relationships and conflicts
5. near-horizon plans
6. stable series guidance

Irrelevant history must be excluded.

## 12. Memory Update Contract

After an episode becomes canonical, parallel extraction may identify:

- confirmed facts
- character and relationship changes
- resolved conflicts
- open conflicts
- promises and obligations
- important dialogue or prose excerpts
- required next-episode continuity

A merge stage validates and applies the update.

Memory changes must be attributable to canonical prose.
Planning documents alone cannot create confirmed facts.

## 13. State and Resume Contract

Every generation stage must be resumable.

Completed valid stages must not be repeated automatically.

A failed stage must not corrupt earlier artifacts.

Canonical prose and memory updates must use atomic writes.

Re-running a completed stage with unchanged inputs must be a no-op.

Changed canonical inputs must be detected before overwriting existing results.

## 14. Initial Validation Phases

### Phase 0 — Canon

Deliver:

- minimal README
- this canonical contract

No generation engine.

### Phase 1 — Mock Vertical Loop

Validate with synthetic content:

- parallel planning
- planning merge
- canonical draft
- parallel review
- review merge
- optional single revision
- finalization
- parallel memory extraction
- memory merge
- resume
- no-op
- failure preservation

No live model calls.

### Phase 2 — Live Single-Episode Validation

Connect the 11-key Gemma pool.

Validate:

- actual parallel calls
- deterministic result ordering
- dynamic key leasing
- timeout and error classification
- malformed-output handling
- context assembly
- one canonical episode
- memory update
- artifact safety

### Phase 3 — Five-Episode Pilot

Generate five sequential episodes for one test work.

Validate:

- readability
- character consistency
- continuity
- rolling-plan adaptation
- memory correctness
- narrative weight
- episode-to-episode interest

### Phase 4 — Twenty-Episode Validation

Proceed only after the five-episode pilot is accepted.

Validate long-horizon drift and memory integrity.

## 15. Direct Implementation Constraints

- Do not generate a complete long novel in one call.
- Do not freeze detailed multi-volume plans before evidence exists.
- Do not concatenate independently written prose fragments as canonical prose.
- Do not allow unlimited review findings.
- Do not allow repeated automatic revision.
- Do not treat future plans as confirmed facts.
- Do not store secrets.
- Do not advance validation scope without an explicit phase result.
- Do not create architecture that is not required by the current phase.

## 16. Current Project State

Current phase:

```text
PHASE_3_FIVE_EPISODE_PILOT
```

Previous phase result:

```text
PHASE_2_COMPLETE
```

Phase 2 evidence:

```text
PHASE_2_COMPLETION_REVIEW.md
implementation baseline: a094dd9
```

Next deliverable:

```text
Implement and validate five sequential episodes for one disposable synthetic test work while preserving shared memory, continuity, and rolling-plan adaptation.
```

Next implementation after approval:

```text
PHASE_4_TWENTY_EPISODE_VALIDATION
```
