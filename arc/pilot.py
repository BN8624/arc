# 기존 단일 회차 pipeline을 순차 다섯 회차 pilot으로 조정한다.
from __future__ import annotations

import json
from pathlib import Path

from .contracts import ContractError, parse_object, validate_worker
from .live_model import scope_projection
from .pipeline import MockPipeline, WaveCheckpoint, status
from .pilot_contracts import ACCEPTANCE_EXCERPT_MAX_CHARACTERS, ACCEPTANCE_EXCERPT_MIN_CHARACTERS, ACCEPTANCE_RUBRIC, ACCEPTANCE_RUBRIC_VERSION, PILOT_REVIEW_ROLES, ROLLING_PLAN_HORIZON_LIMITS, STABLE_MEMORY_FIELDS, TRANSITION_ACTIONS, TRANSITION_CONTRACT_VERSION, TRANSITION_EVIDENCE_FILES, TRANSITION_RESPONSE_FIELDS, TRANSITION_SCHEMA_VERSION, acceptance_catalog_plan, aggregate_pilot_acceptance, canonical_bytes, rolling_plan_hash, transition_action_counts, validate_acceptance_worker, validate_grounded_pilot_acceptance, validate_pilot_fixture, validate_transition, validate_transition_response
from .storage import StorageError, read_json, sha256_bytes, sha256_file, write_json

PROJECTION_CURRENT = "CURRENT"
PROJECTION_MISSING = "MISSING"
PROJECTION_STALE_PREFIX = "STALE_PREFIX"
PROJECTION_CONFLICT = "CONFLICT"
PROJECTION_STALE_REASON = "EPISODE_PROJECTION_STALE"
TRANSITION_RECEIPT_FIELDS = {"schema_version", "transition_id", "completed_episode_id", "next_episode_id", "transition_input_hash", "state", "response_sha256", "raw_response", "contract_code"}
TRANSITION_RECEIPT_STATES = {"RESPONSE_RECEIVED", "COMPLETED", "REJECTED"}


class PilotError(RuntimeError):
    """A five-episode pilot could not safely advance."""


class PilotPipeline:
    def __init__(self, client, scenario: str | None = "pass", mode: str = "mock"):
        if mode not in {"mock", "live"}:
            raise PilotError("unknown pilot mode")
        if mode == "mock" and scenario not in {"pass", "episode_hold", "pilot_hold"}:
            raise PilotError("unknown pilot scenario")
        if mode == "live" and scenario is not None:
            raise PilotError("live pilot does not accept mock scenario")
        self.client, self.scenario, self.mode = client, scenario, mode

    def run(self, fixture_path: Path, run_dir: Path) -> dict:
        raw = fixture_path.read_bytes()
        fixture = validate_pilot_fixture(json.loads(raw.decode("utf-8")))
        source_hash = sha256_bytes(raw)
        manifest_path = run_dir / "pilot_manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest["source_hash"] != source_hash or manifest["scenario"] != self.scenario or manifest["mode"] != self.mode:
                raise PilotError("pilot input changed; refusing reuse")
            if self.mode == "live" and (run_dir / "pilot_live_calls.json").exists():
                self.client.restore_telemetry(read_json(run_dir / "pilot_live_calls.json"))
            verify_pilot_artifacts(run_dir, manifest)
            if self.mode == "live":
                reconcile_live_telemetry_projections(run_dir, manifest)
                inspection = inspect_pilot_checkpoint(run_dir, manifest)
                if _reconcile_legacy_writer_attempt(run_dir, manifest, inspection, fixture):
                    return {"no_op": False, "manifest": manifest}
                if _reconcile_legacy_revision_attempt(run_dir, manifest, inspection):
                    return {"no_op": False, "manifest": manifest}
                active_episode = manifest.get("active_episode_id")
                active_manifest = run_dir / "episodes" / str(active_episode) / "manifest.json"
                if active_episode in manifest["episode_ids"] and active_manifest.exists() and "revision_attempt_state" in read_json(active_manifest):
                    index = manifest["episode_ids"].index(active_episode)
                    child = MockPipeline(self._episode_client(active_episode, index), mode="live")
                    child_manifest = read_json(active_manifest)
                    if "writer_attempt_state" in child_manifest:
                        child._validate_writer_state(active_manifest.parent, child_manifest)
                    child._validate_revision_state(active_manifest.parent, child_manifest)
                active_value = read_json(active_manifest) if active_manifest.exists() else {}
                response_received = active_value.get("revision_attempt_state") == "RESPONSE_RECEIVED" or active_value.get("writer_attempt_state") == "RESPONSE_RECEIVED"
                if inspection["checkpoint_integrity"] == "RECONCILABLE" and not response_received:
                    raise PilotError("pilot checkpoint reconciliation required")
            if manifest["status"] in {"COMPLETE", "HOLD"}:
                (run_dir / "pilot_review_workers.partial.json").unlink(missing_ok=True)
                return {"no_op": True, "manifest": manifest}
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest = {"schema_version": 1, "mode": self.mode, "pilot_id": fixture["pilot_id"], "fixture_id": fixture["initial_source"]["fixture_id"], "source_hash": source_hash, "scenario": self.scenario, "status": "RUNNING", "episode_ids": fixture["episode_ids"], "completed_episodes": [], "completed_transitions": [], "active_episode_id": fixture["episode_ids"][0], "episode_records": [], "artifact_hashes": {}, "acceptance_verdict": None, "last_error": None}
            if self.mode == "live":
                manifest.update({"provider": "gemini_developer_api", "model": self.client.config.model, "key_pool_size": len(self.client.config.keys), "max_live": self.client.config.max_live, "routing_schema_version": 2, "routing_mode": "dynamic_key_pool", "pilot_live_call_count": 0})
            self._save_checkpoint(run_dir, manifest)
        try:
            self._advance(fixture, run_dir, manifest)
        except Exception as error:
            manifest["status"] = "ERROR"
            if self.mode == "live":
                active = manifest.get("active_episode_id")
                child = read_json(run_dir / "episodes" / active / "manifest.json") if active and (run_dir / "episodes" / active / "manifest.json").exists() else {}
                child_error = child.get("last_error") if isinstance(child.get("last_error"), dict) else None
                manifest["last_error"] = {"error_class": (child_error or {}).get("error_class", "CONTRACT_ERROR" if isinstance(error, ContractError) else "PIPELINE_ERROR"), "active_episode_id": active, "stage": (child_error or {}).get("stage"), "role": (child_error or {}).get("role"), "contract_code": (child_error or {}).get("contract_code"), "message": "sanitized child episode failure"}
                self._save_checkpoint(run_dir, manifest)
            else:
                manifest["last_error"] = str(error)
                self._save_manifest(run_dir, manifest)
            raise
        return {"no_op": False, "manifest": manifest}

    def _advance(self, fixture: dict, run_dir: Path, manifest: dict) -> None:
        ids = manifest["episode_ids"]
        for index, episode_id in enumerate(ids):
            source_path = run_dir / "episode_sources" / f"{episode_id}.json"
            if not source_path.exists():
                if index:
                    raise PilotError("missing transitioned episode source")
                self._write_artifact(run_dir, manifest, f"episode_sources/{episode_id}.json", fixture["initial_source"])
            source = read_json(source_path)
            if source["current_episode"]["episode_id"] != episode_id:
                raise PilotError("episode source identity mismatch")
            episode_dir = run_dir / "episodes" / episode_id
            if episode_id not in manifest["completed_episodes"]:
                if self.mode == "live":
                    manifest["active_episode_id"] = episode_id
                    self._save_checkpoint(run_dir, manifest)
                    MockPipeline(self._episode_client(episode_id, index), mode="live").run(source_path, episode_dir, None)
                    self._save_checkpoint(run_dir, manifest)
                else:
                    original = getattr(self.client, "scenario", None)
                    if self.scenario == "episode_hold" and index == 2:
                        self.client.scenario = "hold"
                    try:
                        MockPipeline(self.client).run(source_path, episode_dir, getattr(self.client, "scenario", "pass"))
                    finally:
                        if original is not None:
                            self.client.scenario = original
                current = status(episode_dir)
                if current["status"] == "HOLD":
                    manifest["status"], manifest["active_episode_id"] = "HOLD", episode_id
                    self._save_checkpoint(run_dir, manifest)
                    return
                if current["status"] != "COMPLETE":
                    raise PilotError("episode did not complete")
                record = {"episode_id": episode_id, "status": current["status"], "writer_call_count": current["writer_call_count"], "revision_count": current["revision_count"], "final_sha256": sha256_file(episode_dir / "final.md"), "memory_after_sha256": sha256_file(episode_dir / "memory_after.json")}
                manifest["completed_episodes"].append(episode_id)
                manifest["episode_records"].append(record)
                self._save_checkpoint(run_dir, manifest)
            if index < len(ids) - 1:
                transition_id = f"{episode_id}_to_{ids[index + 1]}"
                if transition_id not in manifest["completed_transitions"]:
                    self._reconcile_transition(run_dir, manifest, transition_id, episode_id, ids[index + 1], source, index)
                    if self.mode == "live":
                        write_json(run_dir / "episodes" / episode_id / "live_calls.json", episode_projection_document(self.client.telemetry(), episode_id))
                    manifest["completed_transitions"].append(transition_id)
                    self._save_checkpoint(run_dir, manifest)
                else:
                    self._verify_completed_transition(run_dir, manifest, transition_id, episode_id, ids[index + 1], source, index)
        if "pilot_acceptance.json" not in manifest["artifact_hashes"]:
            catalog = self._write_evidence_packet(run_dir, manifest)
            self._save_checkpoint(run_dir, manifest)
            workers_path = run_dir / "pilot_review_workers.json"
            if workers_path.exists():
                if manifest["artifact_hashes"].get("pilot_review_workers.json") != sha256_file(workers_path):
                    raise PilotError("pilot review worker hash mismatch")
                workers = read_json(workers_path)
                if not isinstance(workers, list) or [worker.get("role") for worker in workers] != PILOT_REVIEW_ROLES:
                    raise PilotError("invalid canonical pilot review workers")
                for worker in workers:
                    self._review_worker_contract(worker, worker["role"], catalog, manifest["episode_ids"])
            else:
                workers = self._review_workers(run_dir, manifest, catalog)
                self._save_checkpoint(run_dir, manifest)
                self._write_artifact(run_dir, manifest, "pilot_review_workers.json", workers)
            acceptance = aggregate_pilot_acceptance(workers)
            self._write_artifact(run_dir, manifest, "pilot_acceptance.json", acceptance)
            manifest["acceptance_verdict"] = acceptance["verdict"]
        else:
            catalog = build_acceptance_evidence_catalog(run_dir, manifest)
            workers_path = run_dir / "pilot_review_workers.json"
            if manifest["artifact_hashes"].get("pilot_review_workers.json") != sha256_file(workers_path):
                raise PilotError("pilot review worker hash mismatch")
            workers = read_json(workers_path)
            if not isinstance(workers, list) or [worker.get("role") for worker in workers] != PILOT_REVIEW_ROLES:
                raise PilotError("invalid canonical pilot review workers")
            for worker in workers:
                self._review_worker_contract(worker, worker["role"], catalog, manifest["episode_ids"])
            acceptance = read_json(run_dir / "pilot_acceptance.json")
            validate_grounded_pilot_acceptance(acceptance, workers)
            manifest["acceptance_verdict"] = acceptance["verdict"]
        manifest["status"] = "COMPLETE" if manifest["acceptance_verdict"] == "PASS" else "HOLD"
        manifest["active_episode_id"] = None
        self._save_checkpoint(run_dir, manifest)
        (run_dir / "pilot_review_workers.partial.json").unlink(missing_ok=True)

    def _transition_input_hash(self, run_dir: Path, manifest: dict, episode_id: str, next_id: str, source: dict, index: int) -> str:
        value = _transition_input_value(run_dir, manifest["pilot_id"], manifest["episode_ids"], episode_id, next_id, source, index)
        return sha256_bytes(canonical_bytes(value))

    def _reconcile_transition(self, run_dir: Path, manifest: dict, transition_id: str, episode_id: str, next_id: str, source: dict, index: int) -> None:
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        source_path = run_dir / "episode_sources" / f"{next_id}.json"
        input_hash = self._transition_input_hash(run_dir, manifest, episode_id, next_id, source, index)
        if transition_path.exists():
            transition = read_json(transition_path)
            validate_transition(transition, source, next_id, run_dir)
            if transition["transition_input_hash"] != input_hash:
                raise PilotError("transition input hash mismatch")
            if self.mode == "live":
                self._reconcile_transition_receipt(run_dir, transition_id, episode_id, next_id, input_hash, transition)
            if source_path.exists():
                if transition["next_source_hash"] != sha256_file(source_path):
                    raise PilotError("transition next source hash mismatch")
            else:
                next_source = self._next_source_from_transition(run_dir, episode_id, transition)
                if _json_file_hash(next_source) != transition["next_source_hash"]:
                    raise PilotError("transition next source payload mismatch")
                self._write_artifact(run_dir, manifest, f"episode_sources/{next_id}.json", next_source)
            manifest["artifact_hashes"][f"transitions/{transition_id}.json"] = sha256_file(transition_path)
            manifest["artifact_hashes"][f"episode_sources/{next_id}.json"] = sha256_file(source_path)
        elif source_path.exists():
            raise PilotError("next episode source exists without transition")
        else:
            transition, next_source = self._transition(run_dir, manifest, transition_id, episode_id, next_id, source, input_hash, index)
            self._write_artifact(run_dir, manifest, f"transitions/{transition_id}.json", transition)
            self._save_checkpoint(run_dir, manifest)
            self._write_artifact(run_dir, manifest, f"episode_sources/{next_id}.json", next_source)
            self._save_checkpoint(run_dir, manifest)
            if self.mode == "live":
                self._reconcile_transition_receipt(run_dir, transition_id, episode_id, next_id, input_hash, transition)

    def _transition_payload(self, run_dir: Path, manifest: dict, episode_id: str, next_id: str, source: dict, index: int) -> dict:
        episode_dir = run_dir / "episodes" / episode_id
        return {
            "stage": "transition",
            "role": "adapter",
            "transition_contract_version": TRANSITION_CONTRACT_VERSION,
            "pilot_id": manifest["pilot_id"],
            "completed_episode_id": episode_id,
            "next_episode_id": next_id,
            "remaining_episode_count": len(manifest["episode_ids"]) - index - 1,
            "rolling_plan": source["rolling_plan"],
            "required_next_episode_continuity": source["required_next_episode_continuity"],
            "episode_plan": read_json(episode_dir / "episode_plan.json"),
            "final": (episode_dir / "final.md").read_text(encoding="utf-8"),
            "memory_update": read_json(episode_dir / "memory_update.json"),
            "memory_after": read_json(episode_dir / "memory_after.json"),
            "allowed_evidence_refs": [f"episodes/{episode_id}/{name}" for name in TRANSITION_EVIDENCE_FILES],
            "plan_limits": dict(ROLLING_PLAN_HORIZON_LIMITS),
            "action_contract": "KEEP: item_before exists exactly once in the source rolling plan and item_after equals item_before. CHANGE: item_before exists exactly once and item_after is a different non-blank string placed at horizon_after. DROP: horizon_after and item_after are null and the item appears nowhere in rolling_plan_after. ADD: horizon_before and item_before are null and item_after is a new item absent from the source plan. Every decision needs a non-blank reason and at least one evidence excerpt copied verbatim from an allowed artifact.",
            "accounting_rules": "Consume every source plan item with exactly one KEEP, CHANGE, or DROP decision, in source plan order (immediate_horizon first, then near_horizon). Applying the decisions in order must rebuild rolling_plan_after exactly. rolling_plan_after.immediate_horizon needs at least one item, items are unique across both horizons, and next_episode.required_role must equal rolling_plan_after.immediate_horizon[0]. Do not generate identities or hashes; return only the fields in strict_output_schema.",
            "strict_output_schema": {"next_episode": {"episode_id": next_id, "importance": "ordinary|major|pivot", "required_role": "string"}, "rolling_plan_after": {"immediate_horizon": ["string"], "near_horizon": ["string"]}, "adaptation_decisions": [{"action": "KEEP|CHANGE|DROP|ADD", "horizon_before": "immediate_horizon|near_horizon|null", "item_before": "string|null", "horizon_after": "immediate_horizon|near_horizon|null", "item_after": "string|null", "reason": "string", "evidence": [{"ref": f"episodes/{episode_id}/final.md", "excerpt": "string"}]}], "continuity_satisfied": ["string"], "continuity_deferred": ["string"], "adaptation_summary": "string", "evidence_refs": ["string"]},
        }

    def _transition(self, run_dir: Path, manifest: dict, transition_id: str, episode_id: str, next_id: str, source: dict, input_hash: str, index: int) -> tuple[dict, dict]:
        payload = self._transition_payload(run_dir, manifest, episode_id, next_id, source, index)
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.mode != "live":
            raw = self.client.generate(stage="transition", role="adapter", prompt=prompt)
            return self._transition_from_response(run_dir, episode_id, next_id, source, input_hash, raw)
        client = self._episode_client(episode_id, index)
        receipt_path = run_dir / "transitions" / f"{transition_id}.response.json"
        receipt = _read_transition_receipt(receipt_path, transition_id, episode_id, next_id, input_hash)
        if receipt is not None and receipt["state"] == "REJECTED":
            raise PilotError(f"TRANSITION_RESPONSE_ALREADY_CONSUMED: {receipt['contract_code']}")
        if receipt is not None and receipt["state"] == "COMPLETED":
            raise PilotError("completed transition receipt without canonical transition")
        if receipt is None:
            if self._consumed_transition_response(episode_id):
                raise PilotError("TRANSITION_RECONCILIATION_REQUIRED")
            raw = client.generate(stage="transition", role="adapter", prompt=prompt)
            receipt = {"schema_version": 1, "transition_id": transition_id, "completed_episode_id": episode_id, "next_episode_id": next_id, "transition_input_hash": input_hash, "state": "RESPONSE_RECEIVED", "response_sha256": sha256_bytes(raw.encode("utf-8")), "raw_response": raw, "contract_code": None}
            write_json(receipt_path, receipt)
            self._save_checkpoint(run_dir, manifest)
        try:
            return self._transition_from_response(run_dir, episode_id, next_id, source, input_hash, receipt["raw_response"])
        except ContractError as error:
            code = error.contract_code or "TRANSITION_RESPONSE_NOT_OBJECT"
            client.record_contract_failure("transition", "adapter", contract_code=code)
            receipt.update({"state": "REJECTED", "contract_code": code})
            write_json(receipt_path, receipt)
            self._save_checkpoint(run_dir, manifest)
            raise

    def _transition_from_response(self, run_dir: Path, episode_id: str, next_id: str, source: dict, input_hash: str, raw: str) -> tuple[dict, dict]:
        response = validate_transition_response(parse_object(raw))
        transition = {"schema_version": TRANSITION_SCHEMA_VERSION, "completed_episode_id": episode_id, "next_episode_id": next_id, "transition_input_hash": input_hash, "next_source_hash": "pending", "rolling_plan_before_hash": rolling_plan_hash(source["rolling_plan"]), **response}
        validate_transition(transition, source, next_id, run_dir)
        next_source = self._next_source_from_transition(run_dir, episode_id, transition)
        transition["next_source_hash"] = _json_file_hash(next_source)
        return transition, next_source

    def _consumed_transition_response(self, episode_id: str) -> bool:
        telemetry = self.client.telemetry() if hasattr(self.client, "telemetry") else {"calls": []}
        return any(call.get("scope_id") == f"episode:{episode_id}" and call.get("stage") == "transition" and call.get("role") == "adapter" and call.get("status") == "PASS" for call in telemetry.get("calls", []))

    def _reconcile_transition_receipt(self, run_dir: Path, transition_id: str, episode_id: str, next_id: str, input_hash: str, transition: dict) -> None:
        receipt_path = run_dir / "transitions" / f"{transition_id}.response.json"
        receipt = _read_transition_receipt(receipt_path, transition_id, episode_id, next_id, input_hash)
        if receipt is None:
            return
        _verify_receipt_matches_transition(receipt, transition)
        if receipt["state"] != "COMPLETED":
            receipt.update({"state": "COMPLETED", "contract_code": None})
            write_json(receipt_path, receipt)

    def _verify_completed_transition(self, run_dir: Path, manifest: dict, transition_id: str, episode_id: str, next_id: str, source: dict, index: int) -> None:
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        source_path = run_dir / "episode_sources" / f"{next_id}.json"
        if not transition_path.exists() or not source_path.exists():
            raise PilotError("completed transition artifact is missing")
        transition = read_json(transition_path)
        validate_transition(transition, source, next_id, run_dir)
        input_hash = self._transition_input_hash(run_dir, manifest, episode_id, next_id, source, index)
        if transition["transition_input_hash"] != input_hash or transition["next_source_hash"] != sha256_file(source_path):
            raise PilotError("completed transition hash mismatch")
        if self.mode == "live":
            self._reconcile_transition_receipt(run_dir, transition_id, episode_id, next_id, input_hash, transition)

    def _next_source_from_transition(self, run_dir: Path, episode_id: str, transition: dict) -> dict:
        episode_dir = run_dir / "episodes" / episode_id
        memory_after = read_json(episode_dir / "memory_after.json")
        update = read_json(episode_dir / "memory_update.json")
        next_source = dict(memory_after)
        next_source["current_episode"] = transition["next_episode"]
        next_source["rolling_plan"] = transition["rolling_plan_after"]
        next_source["required_next_episode_continuity"] = _unique(transition["continuity_deferred"] + update["required_next_episode_continuity"])
        for field in STABLE_MEMORY_FIELDS:
            if canonical_bytes(next_source[field]) != canonical_bytes(memory_after[field]):
                raise PilotError("transition mutated stable memory")
        return next_source

    def _write_evidence_packet(self, run_dir: Path, manifest: dict) -> list[dict]:
        catalog = build_acceptance_evidence_catalog(run_dir, manifest)
        evidence = {"pilot_id": manifest["pilot_id"], "episode_ids": manifest["episode_ids"], "episodes": [], "transitions": [], "rolling_plan_hashes": [], "rolling_plan_adaptation": rolling_plan_adaptation_summary(run_dir, manifest), "acceptance_evidence_catalog": catalog, "acceptance_rubric_version": ACCEPTANCE_RUBRIC_VERSION}
        for episode_id in manifest["episode_ids"]:
            root = run_dir / "episodes" / episode_id
            source = read_json(run_dir / "episode_sources" / f"{episode_id}.json")
            evidence["episodes"].append({"episode_id": episode_id, "plan": read_json(root / "episode_plan.json"), "final": (root / "final.md").read_text(encoding="utf-8"), "review_verdict": read_json(root / "review_decision.json")["verdict"], "writer_call_count": read_json(root / "manifest.json")["writer_call_count"], "revision_count": read_json(root / "manifest.json")["revision_count"], "memory_before": {field: source[field] for field in STABLE_MEMORY_FIELDS}, "memory_after": {field: read_json(root / "memory_after.json")[field] for field in STABLE_MEMORY_FIELDS}})
            evidence["rolling_plan_hashes"].append(sha256_bytes(canonical_bytes(source["rolling_plan"])))
        for episode_id, next_id in zip(manifest["episode_ids"], manifest["episode_ids"][1:]):
            evidence["transitions"].append(read_json(run_dir / f"transitions/{episode_id}_to_{next_id}.json"))
        self._write_artifact(run_dir, manifest, "pilot_evidence_packet.json", evidence)
        return catalog

    def _review_workers(self, run_dir: Path, manifest: dict, catalog: list[dict]) -> list[dict]:
        evidence_hash = manifest["artifact_hashes"]["pilot_evidence_packet.json"]
        checkpoint = WaveCheckpoint(run_dir / "pilot_review_workers.partial.json", "pilot_review", {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": self.scenario, "episode_ids": manifest["episode_ids"], "evidence_packet_hash": evidence_hash, "acceptance_rubric_version": ACCEPTANCE_RUBRIC_VERSION}, PILOT_REVIEW_ROLES)
        workers = []
        for role in PILOT_REVIEW_ROLES:
            completed = checkpoint.result(role)
            if completed:
                workers.append(self._review_worker_contract(completed, role, catalog, manifest["episode_ids"]))
        first_error = None
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(PILOT_REVIEW_ROLES)) as executor:
            futures = {executor.submit(self._review_worker, run_dir, manifest, role, evidence_hash, catalog): role for role in PILOT_REVIEW_ROLES if not checkpoint.result(role)}
            for future in as_completed(futures):
                try:
                    worker = future.result()
                    workers.append(worker)
                    checkpoint.save(worker["role"], worker)
                except Exception as error:
                    first_error = first_error or error
        if first_error:
            raise first_error
        return sorted(workers, key=lambda worker: PILOT_REVIEW_ROLES.index(worker["role"]))

    def _review_worker_payload(self, manifest: dict, role: str, evidence_hash: str, catalog: list[dict]) -> dict:
        dimension = next(item for item in ACCEPTANCE_RUBRIC if item["dimension"] == role)
        return {
            "pilot_id": manifest["pilot_id"],
            "mode": manifest["mode"],
            "scenario": self.scenario,
            "episode_ids": manifest["episode_ids"],
            "evidence_packet_hash": evidence_hash,
            "acceptance_rubric_version": ACCEPTANCE_RUBRIC_VERSION,
            "dimension": role,
            "dimension_title": dimension["title"],
            "dimension_question": dimension["question"],
            "criteria": dimension["criteria"],
            "coverage_rule": dimension["coverage_rule"],
            "evidence_catalog": catalog,
            "evidence_contract": f"Cite only refs from evidence_catalog. Every evidence item needs an excerpt of {ACCEPTANCE_EXCERPT_MIN_CHARACTERS} to {ACCEPTANCE_EXCERPT_MAX_CHARACTERS} characters copied verbatim from that artifact's content, each criterion needs at least one evidence item per required evidence kind without duplicate items, top-level evidence_refs must equal the sorted unique refs cited by criterion_results and strengths, and coverage_refs must be the sorted unique refs you actually reviewed, containing every cited ref and satisfying coverage_rule.",
            "consistency_contract": "dimension_result is PASS only when every criterion result is PASS, critical_finding is null, and at least one grounded strength exists. dimension_result is HOLD when at least one criterion result is HOLD and critical_finding names one HOLD criterion_id with a non-blank finding. Strengths attach only to PASS criteria of this dimension, at most two, each a concrete statement with verbatim evidence.",
            "strict_output_schema": {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": "string", "primary_risk": "string", "evidence_refs": ["string"], "proposal": {"dimension_result": "PASS|HOLD", "criterion_results": [{"criterion_id": "string", "result": "PASS|HOLD", "finding": "string", "evidence": [{"ref": "string", "excerpt": "string"}]}], "critical_finding": {"criterion_id": "string", "finding": "string"}, "strengths": [{"criterion_id": "string", "strength": "string", "evidence": [{"ref": "string", "excerpt": "string"}]}], "coverage_refs": ["string"]}},
        }

    def _review_worker(self, run_dir: Path, manifest: dict, role: str, evidence_hash: str, catalog: list[dict]) -> dict:
        payload = self._review_worker_payload(manifest, role, evidence_hash, catalog)
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        client = self._acceptance_client() if self.mode == "live" else self.client
        try:
            value = parse_object(client.generate(stage="pilot_review", role=role, prompt=prompt))
            return self._review_worker_contract(value, role, catalog, manifest["episode_ids"])
        except ContractError as error:
            if self.mode == "live":
                client.record_contract_failure("pilot_review", role, contract_code=error.contract_code or "PILOT_REVIEW_RESPONSE_NOT_OBJECT")
            raise

    def _review_worker_contract(self, value: dict, role: str, catalog: list[dict], episode_ids: list[str]) -> dict:
        validate_worker(value, f"pilot_review-{role}", role)
        return validate_acceptance_worker(value, role, catalog, episode_ids)

    def _write_artifact(self, run_dir: Path, manifest: dict, name: str, value: dict | list) -> None:
        manifest["artifact_hashes"][name] = write_json(run_dir / name, value)

    def _episode_client(self, episode_id: str, index: int):
        return self.client.scope(scope_id=f"episode:{episode_id}", logical_order_base=index * 100)

    def _acceptance_client(self):
        return self.client.scope(scope_id="pilot:acceptance", logical_order_base=500)

    def _save_pilot_live_calls(self, run_dir: Path, manifest: dict) -> None:
        if self.mode == "live":
            telemetry = self.client.telemetry()
            manifest["pilot_live_call_count"] = len(telemetry["calls"])
            write_json(run_dir / "pilot_live_calls.json", telemetry)
            manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(telemetry)
            manifest["artifact_hashes"].pop("pilot_live_calls.json", None)

    def _save_checkpoint(self, run_dir: Path, manifest: dict) -> None:
        if self.mode == "live":
            self._save_pilot_live_calls(run_dir, manifest)
        self._save_manifest(run_dir, manifest)

    def _save_manifest(self, run_dir: Path, manifest: dict) -> None:
        write_json(run_dir / "pilot_manifest.json", manifest)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _transition_input_value(run_dir: Path, pilot_id: str, episode_ids: list[str], episode_id: str, next_id: str, source: dict, index: int) -> dict:
    root = run_dir / "episodes" / episode_id
    return {"pilot_id": pilot_id, "completed_episode_id": episode_id, "next_episode_id": next_id, "source_hash": sha256_bytes(canonical_bytes(source)), "episode_plan_hash": sha256_file(root / "episode_plan.json"), "final_hash": sha256_file(root / "final.md"), "memory_update_hash": sha256_file(root / "memory_update.json"), "memory_after_hash": sha256_file(root / "memory_after.json"), "rolling_plan": source["rolling_plan"], "required_continuity": source["required_next_episode_continuity"], "remaining_episode_count": len(episode_ids) - index - 1, "transition_schema_version": TRANSITION_SCHEMA_VERSION, "transition_contract_version": TRANSITION_CONTRACT_VERSION}


def _json_file_hash(value: object) -> str:
    return sha256_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _prefix_hash(items: list[dict]) -> str:
    return sha256_bytes(canonical_bytes(items))


def live_telemetry_checkpoint(telemetry: dict) -> dict:
    calls = list(telemetry.get("calls", []))
    failures = list(telemetry.get("contract_failures", []))
    lease_sequences = [call.get("lease_sequence") for call in calls if call.get("lease_sequence") is not None]
    return {
        "schema_version": 1,
        "call_count": len(calls),
        "contract_failure_count": len(failures),
        "calls_prefix_sha256": _prefix_hash(calls),
        "contract_failures_prefix_sha256": _prefix_hash(failures),
        "max_lease_sequence": max(lease_sequences) if lease_sequences else None,
        "last_call_id": calls[-1].get("call_id") if calls else None,
    }


def verify_live_telemetry_checkpoint(telemetry: dict, checkpoint: dict | None) -> None:
    if not checkpoint:
        return
    calls = list(telemetry.get("calls", []))
    failures = list(telemetry.get("contract_failures", []))
    call_count = checkpoint.get("call_count")
    failure_count = checkpoint.get("contract_failure_count")
    if not isinstance(call_count, int) or not isinstance(failure_count, int):
        raise StorageError("invalid pilot live telemetry checkpoint")
    if len(calls) < call_count or len(failures) < failure_count:
        raise StorageError("pilot live telemetry shorter than checkpoint")
    if checkpoint.get("calls_prefix_sha256") != _prefix_hash(calls[:call_count]):
        raise StorageError("pilot live telemetry checkpoint call prefix mismatch")
    if checkpoint.get("contract_failures_prefix_sha256") != _prefix_hash(failures[:failure_count]):
        raise StorageError("pilot live telemetry checkpoint contract failure prefix mismatch")
    if call_count:
        if checkpoint.get("last_call_id") != calls[call_count - 1].get("call_id"):
            raise StorageError("pilot live telemetry checkpoint last call mismatch")
        leases = [call.get("lease_sequence") for call in calls[:call_count] if call.get("lease_sequence") is not None]
        if checkpoint.get("max_lease_sequence") != (max(leases) if leases else None):
            raise StorageError("pilot live telemetry checkpoint lease mismatch")


def _pilot_transition_ids(manifest: dict) -> list[tuple[str, str, str]]:
    ids = manifest["episode_ids"]
    return [(f"{episode_id}_to_{next_id}", episode_id, next_id) for episode_id, next_id in zip(ids, ids[1:])]


def rolling_plan_adaptation_summary(run_dir: Path, manifest: dict) -> dict:
    """Prove adaptation from validated non-KEEP transition actions, never from plan hash diversity."""
    counts = {action: 0 for action in TRANSITION_ACTIONS}
    transition_count = validated = legacy = 0
    for transition_id, _, _ in _pilot_transition_ids(manifest):
        if transition_id not in manifest.get("completed_transitions", []):
            continue
        transition_count += 1
        transition = read_json(run_dir / "transitions" / f"{transition_id}.json")
        if transition.get("schema_version") != TRANSITION_SCHEMA_VERSION:
            legacy += 1
            continue
        validated += 1
        for action, count in transition_action_counts(transition).items():
            counts[action] += count
    non_keep = counts["CHANGE"] + counts["DROP"] + counts["ADD"]
    return {
        "transition_count": transition_count,
        "validated_transition_count": validated,
        "legacy_transition_count": legacy,
        "action_counts": counts,
        "non_keep_action_count": non_keep,
        "adaptation_proven": transition_count > 0 and validated == transition_count and non_keep >= 1,
    }


def build_acceptance_evidence_catalog(run_dir: Path, manifest: dict) -> list[dict]:
    """Build the granular acceptance evidence catalog from actual pilot artifacts."""
    catalog = []
    episode_hashes: dict[str, dict] = {}
    for ref, kind, episode_id in acceptance_catalog_plan(manifest["episode_ids"]):
        path = run_dir / ref
        if not path.exists():
            raise PilotError(f"acceptance evidence artifact missing: {ref}")
        content = path.read_bytes().decode("utf-8")
        digest = sha256_bytes(content.encode("utf-8"))
        if ref.startswith("episodes/"):
            if episode_id not in episode_hashes:
                episode_hashes[episode_id] = read_json(run_dir / "episodes" / episode_id / "manifest.json").get("artifact_hashes", {})
            pinned = episode_hashes[episode_id].get(path.name)
        else:
            pinned = manifest["artifact_hashes"].get(ref)
        if pinned != digest:
            raise PilotError(f"acceptance evidence artifact hash mismatch: {ref}")
        catalog.append({"ref": ref, "kind": kind, "episode_id": episode_id, "sha256": digest, "content": content})
    return catalog


def _read_transition_receipt(receipt_path: Path, transition_id: str, episode_id: str, next_id: str, input_hash: str | None) -> dict | None:
    if not receipt_path.exists():
        return None
    receipt = read_json(receipt_path)
    if not isinstance(receipt, dict) or set(receipt) != TRANSITION_RECEIPT_FIELDS or receipt["schema_version"] != 1 or receipt["transition_id"] != transition_id or receipt["completed_episode_id"] != episode_id or receipt["next_episode_id"] != next_id or receipt["state"] not in TRANSITION_RECEIPT_STATES:
        raise PilotError("invalid transition response receipt")
    if not isinstance(receipt["raw_response"], str) or receipt["response_sha256"] != sha256_bytes(receipt["raw_response"].encode("utf-8")):
        raise PilotError("transition receipt response hash mismatch")
    if input_hash is not None and receipt["transition_input_hash"] != input_hash:
        raise PilotError("transition receipt input hash mismatch")
    return receipt


def _verify_receipt_matches_transition(receipt: dict, transition: dict) -> None:
    if receipt["state"] == "REJECTED":
        raise PilotError("rejected transition receipt conflicts with canonical transition")
    try:
        response = validate_transition_response(parse_object(receipt["raw_response"]))
    except ContractError:
        raise PilotError("transition receipt response does not match canonical transition") from None
    if any(response[field] != transition.get(field) for field in TRANSITION_RESPONSE_FIELDS):
        raise PilotError("transition receipt response does not match canonical transition")


def _verify_transition_receipts(run_dir: Path, manifest: dict) -> None:
    for transition_id, episode_id, next_id in _pilot_transition_ids(manifest):
        receipt = _read_transition_receipt(run_dir / "transitions" / f"{transition_id}.response.json", transition_id, episode_id, next_id, None)
        if receipt is None:
            continue
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        if receipt["state"] == "COMPLETED" and not transition_path.exists():
            raise PilotError("completed transition receipt without canonical transition")
        if transition_path.exists():
            transition = read_json(transition_path)
            if receipt["transition_input_hash"] != transition.get("transition_input_hash"):
                raise PilotError("transition receipt input hash mismatch")
            _verify_receipt_matches_transition(receipt, transition)


def verify_pilot_artifacts(run_dir: Path, manifest: dict) -> None:
    for name, digest in manifest["artifact_hashes"].items():
        if manifest.get("mode") == "live" and name == "pilot_live_calls.json":
            continue
        path = run_dir / name
        if not path.exists() or sha256_file(path) != digest:
            raise StorageError(f"pilot artifact hash mismatch: {name}")
    immutable = set(manifest["artifact_hashes"]) - ({"pilot_live_calls.json"} if manifest.get("mode") == "live" else set())
    expected = {"pilot_manifest.json", *immutable, "pilot_review_workers.partial.json"}
    pending = {f"episode_sources/{episode_id}.json" for episode_id in manifest["episode_ids"]}
    for transition_id, _, _ in _pilot_transition_ids(manifest):
        pending.add(f"transitions/{transition_id}.json")
        if manifest.get("mode") == "live":
            pending.add(f"transitions/{transition_id}.response.json")
    operational = {"routing_state.json", "pilot_live_calls.json"} if manifest.get("mode") == "live" else set()
    actual = {path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()}
    if actual - expected - operational - pending - {f"episodes/{episode_id}/{name}" for episode_id in manifest["episode_ids"] for name in _episode_files(run_dir / "episodes" / episode_id)}:
        raise StorageError("unknown pilot artifact")
    if manifest.get("mode") == "live":
        _verify_transition_receipts(run_dir, manifest)
    if manifest.get("mode") == "live" and (run_dir / "pilot_live_calls.json").exists():
        _verify_live_telemetry_projections(run_dir, manifest)
    for episode_id in manifest["completed_episodes"]:
        status(run_dir / "episodes" / episode_id)


def _reconcile_legacy_revision_attempt(run_dir: Path, manifest: dict, inspection: dict) -> bool:
    episode_id = manifest.get("active_episode_id")
    episode_dir = run_dir / "episodes" / str(episode_id)
    episode_manifest_path = episode_dir / "manifest.json"
    if not episode_manifest_path.exists():
        return False
    episode = read_json(episode_manifest_path)
    if "revision_attempt_state" in episode:
        return False
    error = episode.get("last_error") if isinstance(episode.get("last_error"), dict) else {}
    suspicious = episode.get("review_verdict") == "REVISE_ONCE" and error.get("stage") == "revision"
    if not suspicious:
        return False
    telemetry = read_json(run_dir / "pilot_live_calls.json")
    revision_calls = [call for call in telemetry.get("calls", []) if call.get("scope_id") == "episode:episode_004" and call.get("stage") == "revision" and call.get("role") == "canonical"]
    responses = [call for call in revision_calls if call.get("status") == "PASS"]
    failures = telemetry.get("contract_failures", [])
    matched_failures = [item for item in failures if item.get("scope_id") == "episode:episode_004" and item.get("stage") == "revision" and item.get("role") == "canonical" and item.get("contract_code") == "PROSE_TOO_SHORT" and item.get("character_count") == 3949]
    response = responses[0] if len(responses) == 1 else None
    exact = all((
        manifest.get("mode") == "live",
        episode_id == "episode_004",
        manifest.get("status") == "ERROR",
        episode.get("status") == "ERROR",
        episode.get("revision_count") == 0,
        "REVISION_COMPLETED" not in episode.get("completed_stages", []),
        "revised.md" not in episode.get("artifact_hashes", {}),
        not (episode_dir / "revised.md").exists(),
        error.get("contract_code") == "PROSE_TOO_SHORT",
        error.get("character_count") == 3949,
        inspection.get("checkpoint_integrity") == "VALID",
        inspection.get("reconciliation_required") is False,
        sum(call.get("key_slot") == "K10" and call.get("status") == "FAIL" and call.get("http_status") == 500 for call in revision_calls) == 1,
        sum(call.get("key_slot") == "K11" and call.get("status") == "FAIL" and call.get("http_status") == 500 for call in revision_calls) == 1,
        response is not None and response.get("key_slot") == "K01" and response.get("output_characters") == 3949,
        len(matched_failures) == 1 and response is not None and matched_failures[0].get("call_id") == response.get("call_id"),
        len({call.get("call_id") for call in telemetry.get("calls", [])}) == len(telemetry.get("calls", [])),
        len({call.get("lease_sequence") for call in telemetry.get("calls", [])}) == len(telemetry.get("calls", [])),
    ))
    if not exact:
        raise PilotError("REVISION_RECONCILIATION_BLOCKED")
    episode.update({"revision_count": 1, "revision_attempt_state": "REJECTED", "revision_exhausted": True, "revision_response_sha256": response["response_sha256"], "revision_character_count": 3949, "revision_contract_code": "PROSE_TOO_SHORT", "revision_response_received_at": response["finished_at"], "revision_call_id": response["call_id"], "revision_lease_sequence": response["lease_sequence"], "status": "HOLD", "last_error": {"error_class": "CONTRACT_ERROR", "stage": "revision", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "call_id": response["call_id"], "character_count": 3949, "message": "revision exhausted after rejected response"}})
    write_json(episode_manifest_path, episode)
    manifest["status"] = "HOLD"
    manifest["active_episode_id"] = "episode_004"
    manifest["last_error"] = {"error_class": "CONTRACT_ERROR", "active_episode_id": "episode_004", "stage": "revision", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "message": "revision exhausted after rejected response"}
    write_json(run_dir / "pilot_manifest.json", manifest)
    return True


def _reconcile_legacy_writer_attempt(run_dir: Path, manifest: dict, inspection: dict, fixture: dict) -> bool:
    episode_id = manifest.get("active_episode_id")
    episode_dir = run_dir / "episodes" / str(episode_id)
    episode_manifest_path = episode_dir / "manifest.json"
    if not episode_manifest_path.exists():
        return False
    episode = read_json(episode_manifest_path)
    writer_keys = set(MockPipeline._initial_writer_state())
    present = writer_keys & set(episode)
    error = episode.get("last_error") if isinstance(episode.get("last_error"), dict) else {}
    suspicious = episode.get("writer_call_count") == 0 and error.get("stage") == "writer"
    if not suspicious:
        return False
    if present:
        if present == writer_keys and all(episode.get(key) == value for key, value in MockPipeline._initial_writer_state().items()):
            return False
        raise PilotError("WRITER_RECONCILIATION_BLOCKED")
    telemetry = read_json(run_dir / "pilot_live_calls.json")
    writer_calls = [call for call in telemetry.get("calls", []) if call.get("scope_id") == "episode:episode_001" and call.get("stage") == "writer" and call.get("role") == "canonical"]
    responses = [call for call in writer_calls if call.get("status") == "PASS"]
    failures = [item for item in telemetry.get("contract_failures", []) if item.get("scope_id") == "episode:episode_001" and item.get("stage") == "writer" and item.get("role") == "canonical"]
    response = responses[0] if len(responses) == 1 else None
    failure = failures[0] if len(failures) == 1 else None
    expected_stages = ["CONTEXT_ASSEMBLED", "PLANNING_WAVE_COMPLETED", "PLAN_MERGED"]
    forbidden_episode_files = ("draft.md", "draft_contract.json", "review_workers.json", "review_workers.partial.json", "review_decision.json", "revised.md")
    exact = all((
        manifest.get("mode") == "live",
        manifest.get("status") == "ERROR",
        episode_id == "episode_001",
        manifest.get("completed_episodes") == [],
        manifest.get("completed_transitions") == [],
        manifest.get("acceptance_verdict") is None,
        not (run_dir / "pilot_acceptance.json").exists(),
        not (run_dir / "pilot_review_workers.json").exists(),
        not (run_dir / "episode_sources" / "episode_002.json").exists(),
        not (run_dir / "transitions").exists() or not any((run_dir / "transitions").iterdir()),
        episode.get("status") == "ERROR",
        episode.get("completed_stages") == expected_stages,
        episode.get("writer_call_count") == 0,
        episode.get("review_verdict") is None,
        episode.get("revision_count") == 0,
        episode.get("revision_attempt_state") == "NOT_STARTED",
        not any((episode_dir / name).exists() for name in forbidden_episode_files),
        read_json(run_dir / "episode_sources" / "episode_001.json") == fixture["initial_source"],
        error.get("contract_code") == "PROSE_TOO_SHORT",
        error.get("character_count") == 2858,
        inspection.get("checkpoint_integrity") == "VALID",
        inspection.get("reconciliation_required") is False,
        response is not None and response.get("call_id") == "L008-A001",
        response is not None and response.get("key_slot") == "K04",
        response is not None and response.get("lease_sequence") == 15,
        response is not None and response.get("output_characters") == 2858,
        response is not None and isinstance(response.get("response_sha256"), str) and len(response.get("response_sha256")) == 64,
        response is not None and response.get("http_status") is None,
        response is not None and response.get("error_class") is None,
        failure is not None and failure.get("contract_code") == "PROSE_TOO_SHORT",
        failure is not None and failure.get("character_count") == 2858,
        failure is not None and response is not None and failure.get("call_id") == response.get("call_id"),
        len({call.get("call_id") for call in telemetry.get("calls", [])}) == len(telemetry.get("calls", [])),
        len({call.get("lease_sequence") for call in telemetry.get("calls", [])}) == len(telemetry.get("calls", [])),
        not any(call.get("scope_id") == "pilot:acceptance" for call in telemetry.get("calls", [])),
    ))
    if not exact:
        raise PilotError("WRITER_RECONCILIATION_BLOCKED")
    episode.update({"writer_call_count": 1, "writer_attempt_state": "REJECTED", "writer_exhausted": True, "writer_response_sha256": response["response_sha256"], "writer_character_count": 2858, "writer_contract_code": "PROSE_TOO_SHORT", "writer_response_received_at": response["finished_at"], "writer_call_id": response["call_id"], "writer_lease_sequence": response["lease_sequence"], "status": "HOLD", "last_error": {"error_class": "CONTRACT_ERROR", "stage": "writer", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "call_id": response["call_id"], "character_count": 2858, "message": "writer exhausted after rejected response"}})
    write_json(episode_manifest_path, episode)
    manifest["status"] = "HOLD"
    manifest["active_episode_id"] = "episode_001"
    manifest["last_error"] = {"error_class": "CONTRACT_ERROR", "active_episode_id": "episode_001", "stage": "writer", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "message": "writer exhausted after rejected response"}
    write_json(run_dir / "pilot_manifest.json", manifest)
    return True


def inspect_pilot_checkpoint(run_dir: Path, manifest: dict | None = None) -> dict:
    manifest = manifest or read_json(run_dir / "pilot_manifest.json")
    current = _manifest_progress(manifest)
    reason_codes: list[str] = []
    if manifest.get("mode") != "live":
        try:
            verify_pilot_artifacts(run_dir, manifest)
        except Exception as error:
            return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": [str(error)], "current": current, "derived": {}}
        return {"checkpoint_integrity": "VALID", "reconciliation_required": False, "reason_codes": [], "current": current, "derived": current}
    try:
        verify_pilot_artifacts(run_dir, manifest)
    except Exception as error:
        return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": [str(error)], "current": _manifest_progress(manifest), "derived": {}}
    try:
        derived = _derive_pilot_progress(run_dir, manifest)
    except Exception as error:
        return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": [str(error)], "current": current, "derived": {}}
    if (run_dir / "pilot_live_calls.json").exists():
        projection_states = _live_projection_states(run_dir, manifest, read_json(run_dir / "pilot_live_calls.json"))
        if any(state != PROJECTION_CURRENT for state in projection_states.values()):
            reason_codes.append(PROJECTION_STALE_REASON)
    if manifest.get("mode") == "live" and "pilot_live_calls.json" in manifest.get("artifact_hashes", {}):
        actual = sha256_file(run_dir / "pilot_live_calls.json")
        if manifest["artifact_hashes"]["pilot_live_calls.json"] != actual:
            reason_codes.append("LEGACY_TELEMETRY_HASH_STALE")
    checkpoint = manifest.get("live_telemetry_checkpoint") or {}
    if checkpoint:
        telemetry = read_json(run_dir / "pilot_live_calls.json")
        if len(telemetry.get("calls", [])) > checkpoint.get("call_count", 0) or len(telemetry.get("contract_failures", [])) > checkpoint.get("contract_failure_count", 0):
            reason_codes.append("TELEMETRY_APPEND_AFTER_CHECKPOINT")
    if current["completed_episodes"] != derived["completed_episodes"]:
        if not _is_prefix(current["completed_episodes"], derived["completed_episodes"]):
            return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": ["COMPLETED_EPISODE_PREFIX_CONFLICT"], "current": current, "derived": derived}
        reason_codes.append("COMPLETED_EPISODE_PREFIX_STALE")
    if current["completed_transitions"] != derived["completed_transitions"]:
        if not _is_prefix(current["completed_transitions"], derived["completed_transitions"]):
            return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": ["COMPLETED_TRANSITION_PREFIX_CONFLICT"], "current": current, "derived": derived}
        reason_codes.append("COMPLETED_TRANSITION_PREFIX_STALE")
    active = current["active_episode_id"]
    derived_active = derived["active_episode_id"]
    if active != derived_active:
        ids = manifest["episode_ids"]
        if active is not None and derived_active is not None and ids.index(active) > ids.index(derived_active):
            return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": ["ACTIVE_EPISODE_AHEAD"], "current": current, "derived": derived}
        reason_codes.append("ACTIVE_EPISODE_STALE")
    records = manifest.get("episode_records", [])
    derived_records = derived["episode_records"]
    if records != derived_records:
        if len(records) > len(derived_records) or records != derived_records[: len(records)]:
            return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": ["EPISODE_RECORDS_CONFLICT"], "current": current, "derived": derived}
        reason_codes.append("EPISODE_RECORDS_STALE")
    if _root_artifact_index_stale(manifest, derived):
        reason_codes.append("ROOT_ARTIFACT_INDEX_STALE")
    integrity = "RECONCILABLE" if reason_codes else "VALID"
    return {"checkpoint_integrity": integrity, "reconciliation_required": integrity == "RECONCILABLE", "reason_codes": reason_codes, "current": current, "derived": derived}


def reconcile_pilot_checkpoint(fixture_path: Path, run_dir: Path) -> dict:
    raw = fixture_path.read_bytes()
    fixture = validate_pilot_fixture(json.loads(raw.decode("utf-8")))
    manifest_path = run_dir / "pilot_manifest.json"
    before = manifest_path.read_bytes()
    manifest = json.loads(before.decode("utf-8"))
    if manifest["source_hash"] != sha256_bytes(raw) or manifest["fixture_id"] != fixture["initial_source"]["fixture_id"]:
        raise PilotError("pilot input changed; refusing reconciliation")
    inspection = inspect_pilot_checkpoint(run_dir, manifest)
    if inspection["checkpoint_integrity"] == "VALID":
        return {"no_op": True, **inspection}
    if inspection["checkpoint_integrity"] != "RECONCILABLE":
        raise StorageError("pilot checkpoint corrupt")
    projection_states = reconcile_live_telemetry_projections(run_dir, manifest)
    changed_files = [f"episodes/{episode_id}/live_calls.json" for episode_id, state in sorted(projection_states.items()) if state in {PROJECTION_MISSING, PROJECTION_STALE_PREFIX}]
    inspection = inspect_pilot_checkpoint(run_dir, manifest)
    if inspection["checkpoint_integrity"] == "VALID":
        return {"no_op": False, "changed_files": changed_files, **inspection}
    derived = inspection["derived"]
    telemetry = read_json(run_dir / "pilot_live_calls.json") if (run_dir / "pilot_live_calls.json").exists() else {"calls": [], "contract_failures": []}
    manifest["completed_episodes"] = derived["completed_episodes"]
    manifest["completed_transitions"] = derived["completed_transitions"]
    manifest["active_episode_id"] = derived["active_episode_id"]
    manifest["episode_records"] = derived["episode_records"]
    manifest["pilot_live_call_count"] = len(telemetry.get("calls", []))
    manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(telemetry)
    manifest["artifact_hashes"].pop("pilot_live_calls.json", None)
    manifest["checkpoint_reconciliation"] = {
        "schema_version": 1,
        "reason_codes": inspection["reason_codes"],
        "previous_manifest_sha256": sha256_bytes(before),
        "telemetry_sha256": sha256_file(run_dir / "pilot_live_calls.json"),
        "derived_completed_episode_count": len(derived["completed_episodes"]),
        "derived_completed_transition_count": len(derived["completed_transitions"]),
        "derived_active_episode_id": derived["active_episode_id"],
    }
    write_json(manifest_path, manifest)
    after = inspect_pilot_checkpoint(run_dir, manifest)
    return {"no_op": False, "changed_files": changed_files + ["pilot_manifest.json"], **after}


def _episode_files(path: Path) -> list[str]:
    return [item.name for item in path.iterdir() if item.is_file()] if path.exists() else []


def _manifest_progress(manifest: dict) -> dict:
    return {"status": manifest.get("status"), "active_episode_id": manifest.get("active_episode_id"), "completed_episodes": list(manifest.get("completed_episodes", [])), "completed_transitions": list(manifest.get("completed_transitions", [])), "episode_record_count": len(manifest.get("episode_records", []))}


def _is_prefix(left: list, right: list) -> bool:
    return right[: len(left)] == left


def _derive_pilot_progress(run_dir: Path, manifest: dict) -> dict:
    ids = manifest["episode_ids"]
    completed: list[str] = []
    records: list[dict] = []
    found_incomplete = False
    for episode_id in ids:
        episode_dir = run_dir / "episodes" / episode_id
        if not episode_dir.exists():
            found_incomplete = True
            break
        current = status(episode_dir)
        if current["status"] != "COMPLETE":
            found_incomplete = True
            break
        completed.append(episode_id)
        records.append({"episode_id": episode_id, "status": current["status"], "writer_call_count": current["writer_call_count"], "revision_count": current["revision_count"], "final_sha256": sha256_file(episode_dir / "final.md"), "memory_after_sha256": sha256_file(episode_dir / "memory_after.json")})
    if found_incomplete:
        for episode_id in ids[len(completed) + 1 :]:
            episode_dir = run_dir / "episodes" / episode_id
            if episode_dir.exists() and status(episode_dir)["status"] == "COMPLETE":
                raise StorageError("non-contiguous completed episode prefix")
    transitions: list[str] = []
    for index, episode_id in enumerate(ids[:-1]):
        if episode_id not in completed:
            break
        next_id = ids[index + 1]
        transition_id = f"{episode_id}_to_{next_id}"
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        source_path = run_dir / "episode_sources" / f"{next_id}.json"
        if not transition_path.exists() and not source_path.exists():
            break
        if not transition_path.exists() or not source_path.exists():
            raise StorageError("non-contiguous transition prefix")
        source = read_json(run_dir / "episode_sources" / f"{episode_id}.json")
        transition = read_json(transition_path)
        validate_transition(transition, source, next_id, run_dir)
        value = _transition_input_value(run_dir, manifest["pilot_id"], ids, episode_id, next_id, source, index)
        if transition["transition_input_hash"] != sha256_bytes(canonical_bytes(value)) or transition["next_source_hash"] != sha256_file(source_path):
            raise StorageError("transition semantic contract failure")
        transitions.append(transition_id)
    active = None
    if len(completed) < len(ids):
        for episode_id in ids[len(completed) :]:
            if (run_dir / "episode_sources" / f"{episode_id}.json").exists() or (run_dir / "episodes" / episode_id).exists():
                active = episode_id
                break
    return {"completed_episodes": completed, "completed_transitions": transitions, "active_episode_id": active, "episode_records": records}


def _root_artifact_index_stale(manifest: dict, derived: dict) -> bool:
    expected = {f"episode_sources/{episode_id}.json" for episode_id in derived["completed_episodes"]}
    expected.update(f"transitions/{transition_id}.json" for transition_id in derived["completed_transitions"])
    return bool(expected - set(manifest.get("artifact_hashes", {})))


def episode_projection_document(root_telemetry: dict, episode_id: str) -> dict:
    return scope_projection(root_telemetry, f"episode:{episode_id}")


def classify_episode_projection(root_telemetry: dict, episode_id: str, projection: object) -> str:
    canonical = episode_projection_document(root_telemetry, episode_id)
    if projection is None:
        return PROJECTION_MISSING
    if not isinstance(projection, dict) or not isinstance(projection.get("calls"), list) or not isinstance(projection.get("contract_failures"), list):
        return PROJECTION_CONFLICT
    calls = projection["calls"]
    if calls != canonical["calls"][: len(calls)]:
        return PROJECTION_CONFLICT
    failures = projection["contract_failures"]
    if failures != canonical["contract_failures"][: len(failures)]:
        return PROJECTION_CONFLICT
    return PROJECTION_CURRENT if projection == canonical else PROJECTION_STALE_PREFIX


def _live_projection_states(run_dir: Path, manifest: dict, root_telemetry: dict) -> dict[str, str]:
    states: dict[str, str] = {}
    for episode_id in manifest.get("episode_ids", []):
        episode_dir = run_dir / "episodes" / episode_id
        if not episode_dir.exists():
            continue
        path = episode_dir / "live_calls.json"
        try:
            projection = read_json(path) if path.exists() else None
        except ValueError:
            projection = object()
        states[episode_id] = classify_episode_projection(root_telemetry, episode_id, projection)
    return states


def reconcile_live_telemetry_projections(run_dir: Path, manifest: dict) -> dict[str, str]:
    """Rebuild MISSING/STALE_PREFIX episode projections from canonical root telemetry."""
    if manifest.get("mode") != "live" or not (run_dir / "pilot_live_calls.json").exists():
        return {}
    root = _verify_root_live_telemetry(run_dir, manifest)
    states = _live_projection_states(run_dir, manifest, root)
    for episode_id, state in states.items():
        if state == PROJECTION_CONFLICT:
            raise StorageError("episode live telemetry projection mismatch")
        if state in {PROJECTION_MISSING, PROJECTION_STALE_PREFIX}:
            write_json(run_dir / "episodes" / episode_id / "live_calls.json", episode_projection_document(root, episode_id))
    return states


def _verify_root_live_telemetry(run_dir: Path, manifest: dict) -> dict:
    root = read_json(run_dir / "pilot_live_calls.json")
    calls = root.get("calls", [])
    call_ids = [call.get("call_id") for call in calls]
    lease_sequences = [call.get("lease_sequence") for call in calls]
    if len(call_ids) != len(set(call_ids)):
        raise StorageError("duplicate pilot live call id")
    if len(lease_sequences) != len(set(lease_sequences)):
        raise StorageError("duplicate pilot live lease sequence")
    known_scopes = {f"episode:{episode_id}" for episode_id in manifest.get("episode_ids", [])} | {"pilot:acceptance"}
    if any(call.get("scope_id") not in known_scopes for call in calls):
        raise StorageError("unknown pilot live telemetry scope")
    if any(item.get("scope_id") is not None and item.get("scope_id") not in known_scopes for item in root.get("contract_failures", [])):
        raise StorageError("unknown pilot live telemetry scope")
    if lease_sequences and (run_dir / "routing_state.json").exists():
        routing = read_json(run_dir / "routing_state.json")
        if routing.get("next_lease_sequence", 0) <= max(lease_sequences):
            raise StorageError("routing lease sequence behind telemetry")
    verify_live_telemetry_checkpoint(root, manifest.get("live_telemetry_checkpoint"))
    return root


def _verify_live_telemetry_projections(run_dir: Path, manifest: dict) -> None:
    root = _verify_root_live_telemetry(run_dir, manifest)
    for state in _live_projection_states(run_dir, manifest, root).values():
        if state == PROJECTION_CONFLICT:
            raise StorageError("episode live telemetry projection mismatch")


def pilot_status(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "pilot_manifest.json")
    inspection = inspect_pilot_checkpoint(run_dir, manifest)
    if inspection["checkpoint_integrity"] == "CORRUPT":
        raise StorageError("; ".join(inspection["reason_codes"]))
    episodes = {episode_id: status(run_dir / "episodes" / episode_id)["status"] for episode_id in manifest["completed_episodes"]}
    finals = all((run_dir / "episodes" / episode_id / "final.md").exists() for episode_id in manifest["completed_episodes"])
    adaptation = rolling_plan_adaptation_summary(run_dir, manifest)
    result = {"mode": manifest.get("mode", "mock"), "pilot_id": manifest["pilot_id"], "status": manifest["status"], "episode_count": len(manifest["episode_ids"]), "completed_episode_count": len(manifest["completed_episodes"]), "completed_transition_count": len(manifest["completed_transitions"]), "active_episode_id": manifest["active_episode_id"], "episode_statuses": episodes, "writer_call_count": sum(item["writer_call_count"] for item in manifest["episode_records"]), "revision_count": sum(item["revision_count"] for item in manifest["episode_records"]), "acceptance_verdict": manifest["acceptance_verdict"], "finals_exist": finals, "memory_chain_valid": _memory_chain_valid(run_dir, manifest), "rolling_plan_adapted": adaptation["adaptation_proven"], "rolling_plan_adaptation_action_counts": adaptation["action_counts"], "legacy_transition_count": adaptation["legacy_transition_count"], "checkpoint_integrity": inspection["checkpoint_integrity"], "reconciliation_required": inspection["reconciliation_required"], "reason_codes": inspection["reason_codes"], "current": inspection["current"], "derived": inspection["derived"]}
    if result["mode"] == "live":
        telemetry = read_json(run_dir / "pilot_live_calls.json")
        checkpoint = manifest.get("live_telemetry_checkpoint")
        calls = telemetry.get("calls", [])
        call_ids = [call.get("call_id") for call in calls]
        lease_sequences = [call.get("lease_sequence") for call in calls]
        acceptance_calls = [call for call in calls if call.get("scope_id") == "pilot:acceptance"]
        result.update({"model": manifest["model"], "key_pool_size": manifest["key_pool_size"], "configured_max_live": manifest["max_live"], "telemetry_schema_version": telemetry["schema_version"], "pilot_live_call_count": len(calls), "live_telemetry_checkpoint": checkpoint, "successful_live_calls": sum(call["status"] == "PASS" for call in calls), "failed_live_calls": sum(call["status"] == "FAIL" for call in calls), "transient_failure_count": sum(call["status"] == "FAIL" and call.get("error_class") in {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR"} for call in calls), "contract_failure_count": len(telemetry.get("contract_failures", [])), "used_key_slots": sorted({call["key_slot"] for call in calls}), "rotation_count": sum(1 for call in calls if call["status"] == "FAIL" and call.get("error_class")), "episode_call_counts": {episode_id: sum(call.get("scope_id") == f"episode:{episode_id}" for call in calls) for episode_id in manifest["completed_episodes"]}, "acceptance_call_count": len(acceptance_calls), "acceptance_pass_calls": sum(call["status"] == "PASS" for call in acceptance_calls), "call_ids_unique": len(call_ids) == len(set(call_ids)), "lease_sequences_unique": len(lease_sequences) == len(set(lease_sequences))})
    return result


def _memory_chain_valid(run_dir: Path, manifest: dict) -> bool:
    for current, following in zip(manifest["episode_ids"], manifest["episode_ids"][1:]):
        if current not in manifest["completed_episodes"] or following not in manifest["completed_episodes"]:
            continue
        memory_after = read_json(run_dir / "episodes" / current / "memory_after.json")
        source = read_json(run_dir / "episode_sources" / f"{following}.json")
        if any(canonical_bytes(memory_after[field]) != canonical_bytes(source[field]) for field in STABLE_MEMORY_FIELDS):
            return False
    return True
