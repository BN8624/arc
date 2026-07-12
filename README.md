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

`PHASE_2_LIVE_SINGLE_EPISODE_VALIDATION`

This phase validates one disposable synthetic episode with live `gemma-4-31b-it` calls and an 11-key pool. It never creates the first real work.

Mock validation commands:

```bash
arc mock-run tests/fixtures/synthetic_work.json --output .tmp/phase1-pass --scenario pass
arc mock-status .tmp/phase1-pass
arc live-preflight --output .tmp/phase2-preflight
arc live-run tests/fixtures/live_synthetic_work.json --output .tmp/phase2-live
arc live-status .tmp/phase2-live
```

Live commands use real keys, quota, and cost only for the synthetic Phase 2 validation. `ARC_CANON.md` remains the canonical contract.

Current deliverables:

- `README.md`
- `ARC_CANON.md`
- mock vertical-loop validation
