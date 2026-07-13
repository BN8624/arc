# ARC

ARC is an AI system for creating readable, long-running serialized novels.

ARC is a generator. It is not a work title, genre, world, or fixed narrative structure. Each novel created by ARC is an independent project with its own creative specification.

## Canon

`ARC_CANON.md` is the sole canonical contract for:

- project identity
- model capabilities
- generation architecture
- parallel execution policy
- rolling planning
- episode quality
- long-term memory
- context assembly
- validation phases
- implementation constraints

Before planning, implementing, reviewing, or modifying ARC:

1. Read `ARC_CANON.md`.
2. Determine the current phase from that document.
3. Work only within the current phase.
4. Do not infer requirements from previous ARC versions or repository history.
5. Do not introduce architecture that is not required by the current phase.

## Current Phase

`PHASE_3_FIVE_EPISODE_PILOT`

This phase validates five sequential episodes for one disposable synthetic test work. It tests continuity, rolling-plan adaptation, memory correctness, readability, and episode-to-episode interest. It does not create ARC's first real work.

Mock validation commands:

```bash
arc mock-run tests/fixtures/synthetic_work.json --output .tmp/phase1-pass --scenario pass
arc mock-status .tmp/phase1-pass
arc live-preflight --output .tmp/phase2-preflight
arc live-run tests/fixtures/live_synthetic_work.json --output .tmp/phase2-live
arc live-status .tmp/phase2-live
arc pilot-mock-run tests/fixtures/pilot_synthetic_work.json --output .tmp/phase3a-pilot-pass --scenario pass
arc pilot-status .tmp/phase3a-pilot-pass
arc pilot-live-run tests/fixtures/pilot_synthetic_work.json --output .tmp/phase3b-pilot-live --preflight .tmp/phase2-preflight/preflight.json
arc pilot-live-status .tmp/phase3b-pilot-live
```

The single-episode live commands above are historical Phase 2 validation commands. The pilot live commands use fake-test-covered runtime integration and still require a separate real validation issue before Phase 4. `ARC_CANON.md` remains the canonical contract.

## Usage Ledger Safety

Runtime usage events are owned by a unique `usage_run_id` and `usage_attempt_id`; their count-token and generation rows share one `request_group_id` while retaining distinct request kinds. `SUCCEEDED`, `FAILED`, `USAGE_UNKNOWN`, and `BLOCKED` are terminal and cannot be changed by general runtime updates. Runtime transitions use atomic conditional updates that verify the prior state and event ownership.

`provider_dispatched` on a count-token row means the provider endpoint was actually entered; injected counters do not set it. Collision repair is isolated from runtime updates, accepts only one exact pre-v2 companion generation, and requires a verified pre-repair online backup whose schema, row count, and content fingerprint match the target ledger. Repair verification does not perform a real preflight or resume Phase 3 Live; those remain separate operations.

Current deliverables:

- `README.md`
- `ARC_CANON.md`
- `PHASE_3_PILOT_CONTRACT.md`
- mock vertical-loop and five-episode pilot validation
