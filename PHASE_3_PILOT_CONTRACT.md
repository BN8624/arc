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

Live writer drafts between 3,000 and 3,999 characters are repairable drafts, not final prose. They are saved as `draft.md` with `draft_contract.json`, pass through the existing review wave, and may receive at most one full-prose revision. The final prose contract remains 4,000 to 8,000 characters. ARC must not concatenate fragments to repair length. If the single revision fails the final prose contract, the run fails closed.

A writer opportunity is consumed by receipt of provider content, not by transport attempts. Admission failures, HTTP failures, timeouts, and connection failures without content leave `writer_call_count` at zero and the writer state at `NOT_STARTED`. Once a content string is returned, including empty, whitespace-only, malformed, underlength, or overlength content, the episode atomically persists `RESPONSE_RECEIVED`, exhaustion, the response SHA-256, character count, receipt time, call ID, and lease sequence before validation. A valid 4,000-to-8,000-character draft or repairable 3,000-to-3,999-character draft is committed and becomes `COMPLETED`. Any other response becomes `REJECTED` and places both the episode and pilot in terminal HOLD without creating draft, review, revision, transition, later-episode, or acceptance artifacts.

Resume never issues a second automatic writer call after `RESPONSE_RECEIVED`, `COMPLETED`, or `REJECTED`. An interruption after receipt persistence but before draft commit converts `RESPONSE_RECEIVED` to `REJECTED` with `WRITER_RESPONSE_ALREADY_CONSUMED`, because raw provider content is not retained. Writer count/state, artifact/stage, response hash, character count, call identity, lease, and telemetry contradictions fail closed before any provider call. A legacy writer checkpoint is reconciled without provider access only when one unique successful response and one matching contract failure provide exact evidence; missing, ambiguous, duplicate, or mismatched evidence is blocked. Reconciliation changes only the episode and pilot manifests and does not change canonical artifacts, telemetry, routing state, or usage records. A new Phase 3 pilot execution remains a separate task.

A revision is consumed by receipt of provider content, not by transport attempts. Admission failures, HTTP failures, timeouts, and connection failures without a content response leave `revision_count` at zero. Once content is returned, the episode atomically persists `RESPONSE_RECEIVED`, the response SHA-256, character count, receipt time, and call identity before validation; `revision_count` is one even when validation rejects the content. A valid committed revision becomes `COMPLETED`. An invalid revision becomes `REJECTED`, exhausts the revision opportunity, and places both the episode and pilot in terminal HOLD without creating `revised.md` or `REVISION_COMPLETED`.

Resume never issues a second automatic revision after `RESPONSE_RECEIVED`, `COMPLETED`, or `REJECTED`. An interruption after response persistence but before validation converts `RESPONSE_RECEIVED` to a fail-closed rejected HOLD because the raw response is not retained. Count/state, artifact/stage, response hash, character count, call identity, and telemetry contradictions are rejected before any provider call. Legacy revision checkpoints may be reconciled without a provider call only when one unique successful response and its contract failure are linked by exact telemetry evidence; ambiguous or duplicate responses are blocked. This reconciliation does not change canonical artifacts or usage records. A new Phase 3 pilot execution remains a separate verification task.

The seven acceptance dimensions execute through `client.generate(stage="pilot_review", role=dimension, prompt=canonical_prompt)`. Their validated successes are atomically stored in `pilot_review_workers.partial.json`. Restart reuses completed dimensions and calls only missing ones. Terminal desk errors preserve other successful partial results. Canonical review workers and acceptance artifacts are committed before the partial checkpoint is removed.

Malformed, hash-mismatched, unknown, or duplicate acceptance partial dimensions fail closed. COMPLETE and both HOLD no-op reruns make no episode, transition, or pilot-review calls and preserve canonical artifact hashes. A stale partial after a verified terminal pilot state is removed without changing canonical artifacts.

## Pilot Acceptance

The acceptance review has seven logical dimensions: readability, character consistency, continuity, rolling-plan adaptation, memory correctness, narrative weight, and episode-to-episode interest. All seven must PASS for pilot PASS. A HOLD dimension requires at least one unique critical finding, and PASS may not contain a critical finding. The review does not automatically enter Phase 4.

## Mock Proof

`pilot-mock-run` supports `pass`, `episode_hold`, and `pilot_hold`. The pass proof completes five episodes, four transitions, a valid memory chain, rolling-plan adaptation, and acceptance PASS. The HOLD proofs preserve completed artifacts and no-op on repeat execution. These commands make no live provider call.

## Phase 3B Shared Live Runtime

`pilot-live-run` connects the five-episode pilot to one base `GemmaPoolClient`. The five episode scopes and the `pilot:acceptance` scope share the same dynamic key pool, launch pacer, provider client cache, cooldown state, routing state, and close lifecycle. The pilot root owns the only `routing_state.json`.

Live pilot telemetry is stored at the pilot root in `pilot_live_calls.json`. Each episode still writes `live_calls.json`, but that file is a deterministic projection of only that episode scope. Acceptance calls exist only in the pilot root telemetry. Status rejects duplicate call IDs, duplicate lease sequences, projection mismatch, and unknown root operational files.

Live acceptance prompts include the canonical pilot evidence packet, dimension question, PASS/HOLD contract, evidence-reference contract, and strict output schema. The prompt is canonical JSON and deterministic for the same evidence packet and dimension.

`pilot-live-run` requires an accepted existing `live-preflight` artifact and does not create a new pilot-specific preflight. `pilot-live-status` validates root telemetry, episode projections, routing state presence, canonical artifacts, memory chain, and acceptance call counts.

## Prose Length Reliability Validation

The canonical prose hard contract remains 4,000 to 8,000 characters, with 3,000 to 3,999 characters remaining repairable only for a writer draft. Prompt reliability is not improved by merely raising the numeric target. After structural expansion guidance at 5,200 to 6,400 characters still produced bounded probe responses below 4,000 characters, the guidance target was calibrated to 6,000 to 6,800 characters while keeping every hard validator unchanged. A second bounded probe showed that numeric calibration alone was insufficient, so writer and revision prompts require the model to develop the objective, obstacle, protagonist action, counteraction, consequence, changed situation or relationship, aftermath, payoff, and ending hook as coherent scenes across roughly 20 to 24 natural paragraphs, with at least three complete sentences in most paragraphs. Headings and paragraph numbers remain forbidden, as do summary compression, repetitive padding, and unsupported central conflicts.

A repairable-draft revision remains one coherent full replacement. Its prompt includes the persisted current draft character count, `hard_gap = max(0, 4000 - current_character_count)`, and `safe_expansion = max(1200, hard_gap + 1000)`. Safe expansion is guidance for meaningful scene, action, dialogue, reaction, consequence, and aftermath development; it is not a new validator or permission to append a fragment. Writer and revision each retain the limit of one actual provider content response. Transport-only failures do not consume that response, and an invalid or underlength content response is never followed by an automatic prose retry.

`prose-live-probe` is a bounded live validation before a new full pilot. It reads the preserved Episode 2 context, plan, repairable draft contract, draft, and review decision from the designated HOLD pilot, verifies their hashes and rejection state, and invokes only the canonical writer and canonical revision prompts once each. It stores metadata, hashes, character counts, contract results, call identities, telemetry, and usage identity, but never raw prompts, raw responses, provider response objects, request headers, API keys, or secrets. A PASS requires both independent responses to satisfy the unchanged 4,000-to-8,000-character prose contract.

A probe PASS demonstrates only bounded prose-length reliability. It is not Phase 3 pilot acceptance, does not run planning, review, memory, transition, or acceptance stages, and does not authorize a five-episode live pilot. A new full pilot remains a separate task, and Phase 4 remains a separate user decision.

This is a pre-live integration proof. Actual Phase 3 live validation, ARC's first real work, and Phase 4 remain separate work.
