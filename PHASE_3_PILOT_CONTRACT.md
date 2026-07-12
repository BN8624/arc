# Phase 3 Pilot Contract

This document is subordinate to `ARC_CANON.md`. It defines the Phase 3A mock architecture and acceptance contract for one disposable five-episode synthetic work.

## Architecture and Artifacts

`PilotPipeline` owns one client/runtime boundary and runs exactly five episodes sequentially through the existing single-episode `MockPipeline`. It does not copy `_advance()` or any single-episode stage implementation. Planning, review, and memory waves retain their existing episode-local parallelism.

The pilot root contains `pilot_manifest.json`, persistent `episode_sources`, five episode directories, four transitions, `pilot_evidence_packet.json`, `pilot_review_workers.json`, and `pilot_acceptance.json`. Root artifacts and episode artifacts are hash-verified. Unknown root files are rejected.

## Episode Source Chain and Transition

Episode 1 uses the fixture initial source. Episode N+1 uses Episode N `memory_after.json` plus exactly one transition. The following values are copied from memory without transition mutation: series compass, world rules, characters, confirmed facts, relationship state, open conflicts, promises, episode summaries, and important excerpts.

Only `current_episode`, `rolling_plan`, and `required_next_episode_continuity` may change in a transition. A transition strictly partitions the previous required continuity into satisfied and deferred values. The next source carries deferred continuity followed by new memory-update continuity, with stable ordering and duplicate removal. Transition evidence references only canonical artifacts inside the pilot output.

Each transition stores its canonical input hash and next-source hash. On restart, a valid transition-only state writes only the missing source, while a valid transition-and-source state records only the missing manifest completion. Existing valid transition or source artifacts are never rebuilt; mismatches fail closed.

## Resume, No-op, and HOLD

Completed episodes and transitions are never rerun. A current episode delegates resume to the existing single-episode pipeline. COMPLETE and HOLD pilot reruns are no-ops. Episode HOLD prevents later source, transition, and acceptance creation. Pilot acceptance HOLD preserves all episodes and never automatically revises or advances to Phase 4.

The seven acceptance dimensions execute through `client.generate(stage="pilot_review", role=dimension, prompt=canonical_prompt)`. Their validated successes are atomically stored in `pilot_review_workers.partial.json`. Restart reuses completed dimensions and calls only missing ones. Terminal desk errors preserve other successful partial results. Canonical review workers and acceptance artifacts are committed before the partial checkpoint is removed.

## Pilot Acceptance

The acceptance review has seven logical dimensions: readability, character consistency, continuity, rolling-plan adaptation, memory correctness, narrative weight, and episode-to-episode interest. All seven must PASS for pilot PASS. A HOLD dimension requires at least one unique critical finding, and PASS may not contain a critical finding. The review does not automatically enter Phase 4.

## Mock Proof

`pilot-mock-run` supports `pass`, `episode_hold`, and `pilot_hold`. The pass proof completes five episodes, four transitions, a valid memory chain, rolling-plan adaptation, and acceptance PASS. The HOLD proofs preserve completed artifacts and no-op on repeat execution. These commands make no live provider call.

## Deferred Phase 3B Scope

Phase 3B may connect the pilot boundary to one shared live runtime, routing state, and full pilot telemetry. It must not introduce fixed key assignment, a new model, automatic extra revision, a database, UI, Phase 4 implementation, or ARC's first real work without a separate approved issue.
