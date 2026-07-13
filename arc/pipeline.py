# Phase 1 합성 회차의 재개 가능한 수직 루프를 실행한다.
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractError, ModelClient, PROSE_MAX_CHARACTERS, PROSE_MIN_CHARACTERS, apply_conflict_selectors, apply_memory_update, parse_object, validate_draft_prose, validate_fixture, validate_memory, validate_plan, validate_prose, validate_review, validate_worker
from .storage import StorageError, read_json, sha256_bytes, sha256_file, verify_artifacts, write_json, write_text

PLANNING_ROLES = ["event", "protagonist_action", "relationship", "continuity", "readability_weight", "reader_payoff"]
REVIEW_ROLES = ["causality", "protagonist_agency", "character_consistency", "continuity", "readability", "narrative_weight", "payoff_and_hook"]
MEMORY_ROLES = ["confirmed_facts", "relationships", "conflicts_and_promises", "important_excerpts"]
MEMORY_FIELDS = ("series_compass", "world_rules", "characters", "confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts", "rolling_plan", "required_next_episode_continuity")


class PipelineError(RuntimeError):
    """A Phase 1 run could not safely advance."""


class WaveCheckpoint:
    """Atomically persists validated desk successes for a dynamic wave."""
    def __init__(self, path: Path, stage: str, wave_input: dict, desks: list[str]):
        self.path, self.stage, self.input_hash, self.desks, self.lock = path, stage, sha256_bytes(json.dumps(wave_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()), {f"{stage}:{role}": (role, order) for order, role in enumerate(desks, start=1)}, threading.Lock()
        self.completed: dict = {}
        if path.exists():
            data = read_json(path)
            if data.get("routing_schema_version") != 2 or data.get("routing_mode") != "dynamic_key_pool" or data.get("stage") != stage or data.get("wave_input_hash") != self.input_hash or data.get("expected_desks") != list(self.desks) or not isinstance(data.get("completed_desks"), dict):
                raise PipelineError("invalid wave checkpoint")
            for desk, item in data["completed_desks"].items():
                if desk not in self.desks or not isinstance(item, dict):
                    raise PipelineError("invalid wave checkpoint desk")
                role, logical_order = self.desks[desk]
                result = item.get("result")
                if item.get("role") != role or item.get("logical_order") != logical_order or not isinstance(result, dict):
                    raise PipelineError("invalid wave checkpoint desk")
                if item.get("result_sha256") != sha256_bytes(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()):
                    raise PipelineError("invalid wave checkpoint result hash")
                try:
                    validate_worker(result, f"{stage}-{role}", role)
                except ContractError as error:
                    raise PipelineError("invalid wave checkpoint contract") from error
            self.completed = data["completed_desks"]

    def save(self, role: str, result: dict) -> None:
        desk = f"{self.stage}:{role}"
        if desk not in self.desks:
            raise PipelineError("unknown checkpoint desk")
        validate_worker(result, f"{self.stage}-{role}", role)
        with self.lock:
            digest = sha256_bytes(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
            if desk in self.completed and self.completed[desk]["result_sha256"] != digest:
                raise PipelineError("checkpoint desk result changed")
            self.completed[desk] = {"role": role, "logical_order": self.desks[desk][1], "result": result, "result_sha256": digest}
            write_json(self.path, {"routing_schema_version": 2, "routing_mode": "dynamic_key_pool", "stage": self.stage, "wave_input_hash": self.input_hash, "expected_desks": list(self.desks), "completed_desks": self.completed})

    def result(self, role: str) -> dict | None:
        item = self.completed.get(f"{self.stage}:{role}")
        return item["result"] if item else None


class MockPipeline:
    def __init__(self, client: ModelClient, mode: str = "mock"):
        self.client, self.mode = client, mode

    def run(self, fixture_path: Path, run_dir: Path, scenario: str | None) -> dict:
        if self.mode == "mock" and scenario not in {"pass", "revise", "hold"}:
            raise PipelineError("unknown scenario")
        raw = fixture_path.read_bytes()
        source = json.loads(raw.decode("utf-8"))
        validate_fixture(source)
        source_hash = sha256_bytes(raw)
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if self.mode == "live" and manifest.get("routing_schema_version") != 2:
                raise PipelineError("LEGACY_ROUTING_SCHEMA")
            if manifest["source_hash"] != source_hash or manifest["scenario"] != scenario or manifest.get("mode", "mock") != self.mode:
                raise PipelineError("source or scenario changed; refusing reuse")
            verify_artifacts(run_dir, manifest, self._operational_files() if self.mode == "live" else None)
            if "PLAN_MERGED" in manifest["completed_stages"] and ("episode_plan.json" not in manifest["artifact_hashes"] or not (run_dir / "episode_plan.json").exists()):
                raise PipelineError("PLAN_MERGED without episode plan")
            if self.mode == "live" and (run_dir / "live_calls.json").exists():
                self.client.restore_telemetry(read_json(run_dir / "live_calls.json"))
                self._reconcile_invalid_memory_merge(source, run_dir, manifest)
            self._prepare_revision_state(run_dir, manifest)
            if manifest["status"] in {"COMPLETE", "HOLD"}:
                return {"no_op": True, "manifest": manifest}
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest = {"schema_version": 1, "mode": self.mode, "fixture_id": source["fixture_id"], "episode_id": source["current_episode"]["episode_id"], "scenario": scenario, "status": "RUNNING", "completed_stages": [], "source_hash": source_hash, "artifact_hashes": {}, "writer_call_count": 0, "revision_count": 0, "review_verdict": None, "last_error": None, **self._initial_revision_state()}
            if self.mode == "live":
                manifest.update({"provider": "gemini_developer_api", "model": self.client.config.model, "sdk_version": self.client.sdk_version, "key_pool_size": 11, "max_live": self.client.config.max_live, "live_call_count": 0, "routing_schema_version": 2, "routing_mode": "dynamic_key_pool"})
            write_json(manifest_path, manifest)
        try:
            self._advance(source, run_dir, manifest)
        except Exception as error:
            manifest["status"] = "ERROR"
            manifest["last_error"] = self._error_record(error)
            self._save_live_calls(run_dir, manifest)
            write_json(manifest_path, manifest)
            raise
        return {"no_op": False, "manifest": manifest}

    def _advance(self, source: dict, run_dir: Path, manifest: dict) -> None:
        self._prepare_revision_state(run_dir, manifest)
        if "CONTEXT_ASSEMBLED" not in manifest["completed_stages"]:
            context = {"fixture_id": source["fixture_id"], "episode_id": source["current_episode"]["episode_id"], "current_episode": source["current_episode"], "series_compass": source["series_compass"], "world_rules": source["world_rules"], "characters": source["characters"], "confirmed_facts": source["confirmed_facts"], "relationship_state": source["relationship_state"], "open_conflicts": source["open_conflicts"], "promises": source["promises"], "recent_summaries": source["episode_summaries"], "important_excerpts": source["important_excerpts"], "rolling_plan": source["rolling_plan"], "required_next_episode_continuity": source["required_next_episode_continuity"], "source_hash": manifest["source_hash"]}
            self._commit(run_dir, manifest, "context_packet.json", context, "CONTEXT_ASSEMBLED")
        context = read_json(run_dir / "context_packet.json")
        if "PLANNING_WAVE_COMPLETED" not in manifest["completed_stages"]:
            workers = self._wave("planning", PLANNING_ROLES, context, run_dir)
            self._commit(run_dir, manifest, "planning_workers.json", workers, "PLANNING_WAVE_COMPLETED")
            (run_dir / "planning_workers.partial.json").unlink(missing_ok=True)
        else:
            if self.mode == "live" and (run_dir / "planning_workers.partial.json").exists():
                raise PipelineError("stale planning partial after completed wave")
            (run_dir / "planning_workers.partial.json").unlink(missing_ok=True)
        planning = read_json(run_dir / "planning_workers.json")
        if "PLAN_MERGED" not in manifest["completed_stages"]:
            try:
                value = validate_plan(parse_object(self._request("planning_merge", "merge", {"episode_id": manifest["episode_id"], "context": context, "workers": planning})), manifest["episode_id"], {worker["worker_id"] for worker in planning})
            except ContractError as error:
                if self.mode == "live":
                    self.client.record_contract_failure("planning_merge", "merge", contract_code=error.contract_code)
                raise
            self._commit(run_dir, manifest, "episode_plan.json", value, "PLAN_MERGED")
        plan = read_json(run_dir / "episode_plan.json")
        if "DRAFT_COMPLETED" not in manifest["completed_stages"]:
            text, draft_contract = self._draft_prose({"context": context, "plan": plan})
            manifest["writer_call_count"] += 1
            self._commit_draft(run_dir, manifest, text, draft_contract)
        draft = (run_dir / "draft.md").read_text(encoding="utf-8")
        draft_contract = self._draft_contract(run_dir, manifest, draft)
        if "REVIEW_WAVE_COMPLETED" not in manifest["completed_stages"]:
            workers = self._wave("review", REVIEW_ROLES, {"context": context, "plan": plan, "draft": draft, "draft_contract": draft_contract}, run_dir)
            self._commit(run_dir, manifest, "review_workers.json", workers, "REVIEW_WAVE_COMPLETED")
            (run_dir / "review_workers.partial.json").unlink(missing_ok=True)
        else:
            (run_dir / "review_workers.partial.json").unlink(missing_ok=True)
        review_workers = read_json(run_dir / "review_workers.json")
        if "REVIEW_MERGED" not in manifest["completed_stages"]:
            decision = validate_review(parse_object(self._request("review_merge", "merge", {"context": context, "plan": plan, "draft": draft, "draft_contract": draft_contract, "workers": review_workers})))
            if draft_contract["verdict"] == "REVISE_REQUIRED" and decision["verdict"] == "PASS":
                if self.mode == "live":
                    self.client.record_contract_failure("review_merge", "merge", contract_code="PROSE_REPAIRABLE_PASS_INVALID", character_count=draft_contract["character_count"])
                raise ContractError("repairable underlength draft cannot pass review merge", "PROSE_REPAIRABLE_PASS_INVALID")
            manifest["review_verdict"] = decision["verdict"]
            self._commit(run_dir, manifest, "review_decision.json", decision, "REVIEW_MERGED")
        decision = read_json(run_dir / "review_decision.json")
        if decision["verdict"] == "HOLD":
            manifest["status"] = "HOLD"
            manifest["last_error"] = None
            self._save_live_calls(run_dir, manifest)
            self._save_manifest(run_dir, manifest)
            return
        if decision["verdict"] == "REVISE_ONCE" and "REVISION_COMPLETED" not in manifest["completed_stages"]:
            if manifest["revision_attempt_state"] == "RESPONSE_RECEIVED":
                self._reject_consumed_revision(run_dir, manifest, "REVISION_RESPONSE_ALREADY_CONSUMED")
                return
            if manifest["revision_attempt_state"] == "REJECTED":
                manifest["status"] = "HOLD"
                self._save_manifest(run_dir, manifest)
                return
            text = self._revision_prose(run_dir, manifest, {"context": context, "plan": plan, "draft": draft, "draft_contract": draft_contract, "decision": decision})
            if text is None:
                return
            self._commit_artifact(run_dir, manifest, "revised.md", text, text=True)
            manifest["completed_stages"].append("REVISION_COMPLETED")
            manifest["revision_attempt_state"] = "COMPLETED"
            manifest["revision_contract_code"] = None
            manifest["status"] = "RUNNING"
            manifest["last_error"] = None
            self._save_manifest(run_dir, manifest)
        if "FINALIZED" not in manifest["completed_stages"]:
            source_path = run_dir / ("revised.md" if decision["verdict"] == "REVISE_ONCE" else "draft.md")
            self._commit(run_dir, manifest, "final.md", source_path.read_text(encoding="utf-8"), "FINALIZED", text=True)
        final = (run_dir / "final.md").read_text(encoding="utf-8")
        if "MEMORY_WAVE_COMPLETED" not in manifest["completed_stages"]:
            memory_before = build_memory_before(source)
            workers = self._wave("memory", MEMORY_ROLES, {"episode_id": manifest["episode_id"], "final": final, "memory_before": memory_before}, run_dir)
            self._commit(run_dir, manifest, "memory_workers.json", workers, "MEMORY_WAVE_COMPLETED")
            (run_dir / "memory_workers.partial.json").unlink(missing_ok=True)
        else:
            (run_dir / "memory_workers.partial.json").unlink(missing_ok=True)
        memory_workers = read_json(run_dir / "memory_workers.json")
        if "MEMORY_MERGED" not in manifest["completed_stages"]:
            try:
                memory_before = build_memory_before(source)
                provider_update = parse_object(self._request("memory_merge", "merge", {"episode_id": manifest["episode_id"], "memory_before": memory_before, "open_conflicts": source["open_conflicts"], "final": final, "workers": memory_workers}))
                canonical_update = apply_conflict_selectors(provider_update, source["open_conflicts"]) if self.mode == "live" else provider_update
                update = validate_memory(canonical_update, manifest["episode_id"])
                apply_memory_update(source, update)
            except ContractError:
                if self.mode == "live":
                    from .live_model import LiveCallError
                    raise LiveCallError("CONTRACT_ERROR", "memory_merge", "merge", "K11", "memory merge contract failed") from None
                raise
            self._commit(run_dir, manifest, "memory_update.json", update, "MEMORY_MERGED")
        update = read_json(run_dir / "memory_update.json")
        if "MEMORY_APPLIED" not in manifest["completed_stages"]:
            memory_after = apply_memory_update(source, update)
            self._commit(run_dir, manifest, "memory_after.json", memory_after, "MEMORY_APPLIED")
        self._save_live_calls(run_dir, manifest)
        manifest["status"] = "COMPLETE"
        manifest["last_error"] = None
        self._save_manifest(run_dir, manifest)

    def _wave(self, stage: str, roles: list[str], payload: dict, run_dir: Path | None = None) -> list[dict]:
        checkpoint = WaveCheckpoint(run_dir / f"{stage}_workers.partial.json", stage, payload, roles) if self.mode == "live" and run_dir else None
        def one(role: str) -> dict:
            request_payload = build_memory_worker_payload(role, episode_id=payload["episode_id"], final=payload["final"], memory_before=payload["memory_before"]) if stage == "memory" else payload
            value = parse_object(self._request(stage, role, request_payload))
            return validate_worker(value, f"{stage}-{role}", role)
        recovered = {role: checkpoint.result(role) for role in roles if checkpoint and checkpoint.result(role)}
        with ThreadPoolExecutor(max_workers=self._wave_max_workers(len(roles))) as executor:
            futures = {executor.submit(one, role): role for role in roles if role not in recovered}
            results = list(recovered.values())
            first_error = None
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    if checkpoint:
                        checkpoint.save(result["role"], result)
                except Exception as error:
                    first_error = first_error or error
            if first_error:
                raise first_error
        return sorted(results, key=lambda item: roles.index(item["role"]))

    def _wave_max_workers(self, role_count: int) -> int:
        return min(self.client.config.max_live, role_count) if self.mode == "live" else min(11, role_count)

    def _request(self, stage: str, role: str, payload: dict) -> str:
        if self.mode == "live":
            from .prompts import build_prompt
            prompt = build_prompt(stage, role, payload)
        else:
            prompt = json.dumps(payload)
        return self.client.generate(stage=stage, role=role, prompt=prompt)

    def _prose(self, stage: str, payload: dict) -> str:
        raw = self._request(stage, "canonical", payload)
        text = raw if self.mode == "live" else parse_object(raw).get("text")
        if self.mode == "live":
            try:
                return validate_prose(text)
            except ContractError as error:
                self.client.record_contract_failure(stage, "canonical", contract_code=error.contract_code, character_count=getattr(error, "character_count", None))
                raise
        if not isinstance(text, str) or not text.strip() or text.lstrip().startswith(("{", "[")):
            raise ContractError("invalid canonical prose")
        if self.mode == "live" and (not PROSE_MIN_CHARACTERS <= len(text) <= PROSE_MAX_CHARACTERS or any(marker in text for marker in ("[화면]", "[음향]", "[카메라]", "장면 1", "장면 2", "SCENE 1", "CUT TO:", "```"))):
            raise ContractError("live prose contract failed")
        return text

    @staticmethod
    def _initial_revision_state() -> dict:
        return {"revision_attempt_state": "NOT_STARTED", "revision_exhausted": False, "revision_response_sha256": None, "revision_character_count": None, "revision_contract_code": None, "revision_response_received_at": None, "revision_call_id": None, "revision_lease_sequence": None}

    def _prepare_revision_state(self, run_dir: Path, manifest: dict) -> None:
        keys = set(self._initial_revision_state())
        present = keys & set(manifest)
        if not present:
            error = manifest.get("last_error") if isinstance(manifest.get("last_error"), dict) else {}
            if manifest.get("review_verdict") == "REVISE_ONCE" and error.get("stage") == "revision":
                raise PipelineError("REVISION_RECONCILIATION_REQUIRED")
            manifest.update(self._initial_revision_state())
            self._save_manifest(run_dir, manifest)
        elif present != keys:
            raise PipelineError("invalid revision evidence")
        self._validate_revision_state(run_dir, manifest)
        if manifest["revision_attempt_state"] == "RESPONSE_RECEIVED":
            self._reject_consumed_revision(run_dir, manifest, "REVISION_RESPONSE_ALREADY_CONSUMED")

    def _validate_revision_state(self, run_dir: Path, manifest: dict) -> None:
        count = manifest.get("revision_count")
        state = manifest.get("revision_attempt_state")
        exhausted = manifest.get("revision_exhausted")
        if type(count) is not int or count not in {0, 1} or state not in {"NOT_STARTED", "RESPONSE_RECEIVED", "COMPLETED", "REJECTED"}:
            raise PipelineError("invalid revision evidence")
        if (state == "NOT_STARTED") != (count == 0) or exhausted is not (count == 1):
            raise PipelineError("invalid revision evidence")
        evidence = (manifest.get("revision_response_sha256"), manifest.get("revision_character_count"), manifest.get("revision_response_received_at"), manifest.get("revision_call_id"), manifest.get("revision_lease_sequence"))
        if state == "NOT_STARTED":
            if any(item is not None for item in evidence) or manifest.get("revision_contract_code") is not None or "REVISION_COMPLETED" in manifest["completed_stages"] or (run_dir / "revised.md").exists():
                raise PipelineError("invalid revision evidence")
            return
        digest, characters, received_at, call_id, lease = evidence
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest) or type(characters) is not int or characters < 0 or not isinstance(received_at, str) or not isinstance(call_id, str) or type(lease) is not int:
            raise PipelineError("invalid revision evidence")
        completed = "REVISION_COMPLETED" in manifest["completed_stages"]
        revised = run_dir / "revised.md"
        if state == "COMPLETED":
            if not completed or not revised.exists() or manifest.get("revision_contract_code") is not None:
                raise PipelineError("invalid revision evidence")
        elif completed or revised.exists():
            raise PipelineError("invalid revision evidence")
        if state == "REJECTED" and not manifest.get("revision_contract_code"):
            raise PipelineError("invalid revision evidence")
        telemetry_path = run_dir / "live_calls.json"
        if self.mode == "live" and telemetry_path.exists():
            calls = [call for call in read_json(telemetry_path).get("calls", []) if call.get("call_id") == call_id and call.get("stage") == "revision" and call.get("role") == "canonical"]
            if len(calls) != 1 or calls[0].get("status") != "PASS" or calls[0].get("response_sha256") != digest or calls[0].get("output_characters") != characters or calls[0].get("lease_sequence") != lease:
                raise PipelineError("invalid revision telemetry evidence")

    def _revision_prose(self, run_dir: Path, manifest: dict, payload: dict) -> str | None:
        if self.mode != "live":
            text = self._prose("revision", payload)
            manifest.update({"revision_count": 1, "revision_attempt_state": "RESPONSE_RECEIVED", "revision_exhausted": True, "revision_response_sha256": hashlib.sha256(text.encode()).hexdigest(), "revision_character_count": len(text), "revision_contract_code": None, "revision_response_received_at": datetime.now(timezone.utc).isoformat(), "revision_call_id": "MOCK", "revision_lease_sequence": 0})
            self._save_manifest(run_dir, manifest)
            return text
        text = self._request("revision", "canonical", payload)
        if not isinstance(text, str):
            raise ContractError("invalid canonical prose")
        digest = hashlib.sha256(text.encode()).hexdigest()
        telemetry = self.client.telemetry() if hasattr(self.client, "telemetry") else {"calls": []}
        calls = [call for call in telemetry.get("calls", []) if call.get("stage") == "revision" and call.get("role") == "canonical" and call.get("status") == "PASS" and call.get("response_sha256") == digest and call.get("output_characters") == len(text)]
        call = calls[-1] if calls else {}
        manifest.update({"revision_count": 1, "revision_attempt_state": "RESPONSE_RECEIVED", "revision_exhausted": True, "revision_response_sha256": digest, "revision_character_count": len(text), "revision_contract_code": None, "revision_response_received_at": call.get("finished_at") or datetime.now(timezone.utc).isoformat(), "revision_call_id": call.get("call_id") or "UNAVAILABLE", "revision_lease_sequence": call.get("lease_sequence", 0)})
        self._save_live_calls(run_dir, manifest)
        self._save_manifest(run_dir, manifest)
        try:
            return validate_prose(text)
        except ContractError as error:
            self.client.record_contract_failure("revision", "canonical", contract_code=error.contract_code, character_count=getattr(error, "character_count", None))
            self._reject_consumed_revision(run_dir, manifest, error.contract_code or "REVISION_CONTRACT_REJECTED", error)
            return None

    def _reject_consumed_revision(self, run_dir: Path, manifest: dict, code: str, error: Exception | None = None) -> None:
        manifest["revision_attempt_state"] = "REJECTED"
        manifest["revision_exhausted"] = True
        manifest["revision_contract_code"] = code
        manifest["status"] = "HOLD"
        manifest["last_error"] = self._error_record(error) if error else {"error_class": "CONTRACT_ERROR", "stage": "revision", "role": "canonical", "contract_code": code, "call_id": manifest.get("revision_call_id"), "character_count": manifest.get("revision_character_count"), "message": "revision response already consumed"}
        self._save_live_calls(run_dir, manifest)
        self._save_manifest(run_dir, manifest)

    def _draft_prose(self, payload: dict) -> tuple[str, dict]:
        raw = self._request("writer", "canonical", payload)
        text = raw if self.mode == "live" else parse_object(raw).get("text")
        if self.mode != "live":
            if not isinstance(text, str) or not text.strip() or text.lstrip().startswith(("{", "[")):
                raise ContractError("invalid canonical prose")
            return text, self._draft_contract_value(len(text), "PASS", None)
        try:
            text, contract = validate_draft_prose(text)
            return text, self._draft_contract_value(contract["character_count"], contract["verdict"], contract["contract_code"])
        except ContractError as error:
            self.client.record_contract_failure("writer", "canonical", contract_code=error.contract_code, character_count=getattr(error, "character_count", None))
            raise

    def _draft_contract_value(self, character_count: int, verdict: str, contract_code: str | None) -> dict:
        return {"schema_version": 1, "episode_id": None, "verdict": verdict, "contract_code": contract_code, "character_count": character_count, "minimum_final_characters": PROSE_MIN_CHARACTERS, "maximum_final_characters": PROSE_MAX_CHARACTERS, "evidence_ref": "draft.md"}

    def _commit_draft(self, run_dir: Path, manifest: dict, text: str, draft_contract: dict) -> None:
        self._commit_artifact(run_dir, manifest, "draft.md", text, text=True)
        draft_contract["episode_id"] = manifest["episode_id"]
        self._commit_artifact(run_dir, manifest, "draft_contract.json", draft_contract)
        manifest["completed_stages"].append("DRAFT_COMPLETED")
        manifest["status"] = "RUNNING"
        manifest["last_error"] = None
        self._save_manifest(run_dir, manifest)

    def _draft_contract(self, run_dir: Path, manifest: dict, draft: str) -> dict:
        if "draft_contract.json" in manifest["artifact_hashes"] and (run_dir / "draft_contract.json").exists():
            return read_json(run_dir / "draft_contract.json")
        return {"schema_version": 1, "episode_id": manifest["episode_id"], "verdict": "PASS", "contract_code": None, "character_count": len(draft), "minimum_final_characters": PROSE_MIN_CHARACTERS, "maximum_final_characters": PROSE_MAX_CHARACTERS, "evidence_ref": "draft.md"}

    def _save_live_calls(self, run_dir: Path, manifest: dict) -> None:
        if self.mode == "live":
            telemetry = self.client.telemetry()
            manifest["live_call_count"] = len(telemetry["calls"])
            self._commit_artifact(run_dir, manifest, "live_calls.json", telemetry)

    def _reconcile_invalid_memory_merge(self, source: dict, run_dir: Path, manifest: dict) -> None:
        if "MEMORY_MERGED" not in manifest["completed_stages"]:
            return
        try:
            apply_memory_update(source, read_json(run_dir / "memory_update.json"))
            return
        except ContractError:
            rejected = run_dir / "memory_update.rejected.json"
            current = run_dir / "memory_update.json"
            if rejected.exists():
                raise PipelineError("invalid memory update reconciliation already exists")
            os.replace(current, rejected)
            manifest["artifact_hashes"].pop("memory_update.json")
            manifest["artifact_hashes"][rejected.name] = sha256_file(rejected)
            manifest["completed_stages"].remove("MEMORY_MERGED")
            manifest["status"] = "ERROR"
            manifest["last_error"] = {"error_class": "CONTRACT_ERROR", "stage": "memory_merge", "role": "merge", "key_slot": "K11", "http_status": None, "provider_code": None, "message": "preserved invalid memory update"}
            self.client.record_contract_failure("memory_merge", "merge", "K11")
            self._save_live_calls(run_dir, manifest)
            self._save_manifest(run_dir, manifest)

    def _error_record(self, error: Exception) -> dict | str:
        if hasattr(error, "error_class"):
            return {"error_class": error.error_class, "stage": error.stage, "role": error.role, "key_slot": error.slot, "http_status": error.http_status, "provider_code": error.provider_code, "message": "sanitized provider failure"}
        if isinstance(error, ContractError) and error.contract_code:
            telemetry = self.client.telemetry() if hasattr(self.client, "telemetry") else {"contract_failures": []}
            event = next((item for item in reversed(telemetry.get("contract_failures", [])) if item.get("contract_code") == error.contract_code), {})
            stage = event.get("stage") or ("planning_merge" if str(error.contract_code).startswith("PLAN_") else "review_merge" if error.contract_code == "PROSE_REPAIRABLE_PASS_INVALID" else None)
            role = event.get("role") or ("merge" if stage in {"planning_merge", "review_merge"} else None)
            message = "sanitized planning merge contract failure" if stage == "planning_merge" else "sanitized prose contract failure" if stage in {"writer", "revision"} else "sanitized contract failure"
            record = {"error_class": "CONTRACT_ERROR", "stage": stage, "role": role, "contract_code": error.contract_code, "key_slot": event.get("key_slot", "UNKNOWN"), "http_status": None, "provider_code": None, "message": message}
            if "character_count" in event:
                record["character_count"] = event["character_count"]
            if stage in {"writer", "revision"} and event.get("call_id"):
                record["call_id"] = event["call_id"]
            return record
        return str(error)

    def _commit(self, run_dir: Path, manifest: dict, filename: str, value: dict | list | str, stage: str, text: bool = False) -> None:
        self._commit_artifact(run_dir, manifest, filename, value, text)
        manifest["completed_stages"].append(stage)
        manifest["status"] = "RUNNING"
        manifest["last_error"] = None
        self._save_manifest(run_dir, manifest)

    def _commit_artifact(self, run_dir: Path, manifest: dict, filename: str, value: dict | list | str, text: bool = False) -> None:
        digest = write_text(run_dir / filename, value) if text else write_json(run_dir / filename, value)
        manifest["artifact_hashes"][filename] = digest

    def _save_manifest(self, run_dir: Path, manifest: dict) -> None:
        write_json(run_dir / "manifest.json", manifest)

    @staticmethod
    def _operational_files() -> set[str]:
        return {"routing_state.json", "planning_workers.partial.json", "review_workers.partial.json", "memory_workers.partial.json"}


def status(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "manifest.json")
    operational = {"routing_state.json", "planning_workers.partial.json", "review_workers.partial.json", "memory_workers.partial.json"} if manifest.get("mode") == "live" else None
    verify_artifacts(run_dir, manifest, operational)
    if manifest["status"] == "COMPLETE" and "MEMORY_APPLIED" not in manifest["completed_stages"]:
        raise StorageError("COMPLETE without MEMORY_APPLIED")
    result = {"mode": manifest.get("mode", "mock"), "fixture_id": manifest["fixture_id"], "episode_id": manifest["episode_id"], "status": manifest["status"], "completed_stages": manifest["completed_stages"], "review_verdict": manifest["review_verdict"], "writer_call_count": manifest["writer_call_count"], "revision_count": manifest["revision_count"], "final_exists": (run_dir / "final.md").exists(), "memory_merged": "MEMORY_MERGED" in manifest["completed_stages"], "memory_applied": "MEMORY_APPLIED" in manifest["completed_stages"], "last_error": manifest["last_error"]}
    if result["mode"] == "live":
        telemetry = read_json(run_dir / "live_calls.json")
        calls = telemetry["calls"]
        result.update({"model": manifest["model"], "key_pool_size": manifest["key_pool_size"], "configured_max_live": manifest["max_live"], "telemetry_schema_version": telemetry["schema_version"], "live_call_count": len(calls), "successful_live_calls": sum(call["status"] == "PASS" for call in calls), "failed_live_calls": sum(call["status"] == "FAIL" for call in calls), "contract_failure_count": len(telemetry.get("contract_failures", [])), "used_key_slots": sorted({call["key_slot"] for call in calls}), "per_wave_max_active_calls": telemetry["max_active_by_stage"], "final_character_count": len((run_dir / "final.md").read_text(encoding="utf-8")) if (run_dir / "final.md").exists() else 0})
    return result


def build_memory_before(source: dict) -> dict:
    missing = set(MEMORY_FIELDS) - source.keys()
    if missing:
        raise ContractError("memory source fields are missing")
    return json.loads(json.dumps({field: source[field] for field in MEMORY_FIELDS}, ensure_ascii=False))


def build_memory_worker_payload(role: str, *, episode_id: str, final: str, memory_before: dict) -> dict:
    sections = {"confirmed_facts": ("series_compass", "world_rules", "characters", "confirmed_facts", "episode_summaries"), "relationships": ("characters", "relationship_state", "confirmed_facts", "episode_summaries"), "conflicts_and_promises": ("open_conflicts", "promises", "required_next_episode_continuity", "episode_summaries"), "important_excerpts": ("important_excerpts", "characters", "relationship_state")}
    if role not in sections:
        raise ContractError("unknown memory role")
    return {"episode_id": episode_id, "final": final, "memory_before": {name: memory_before[name] for name in sections[role]}}
