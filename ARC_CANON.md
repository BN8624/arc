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