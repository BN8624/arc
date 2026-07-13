# 기존 단일 회차 pipeline을 순차 다섯 회차 pilot으로 조정한다.
from __future__ import annotations

import json
from pathlib import Path

from .contracts import ContractError, parse_object, validate_worker
from .pipeline import MockPipeline, WaveCheckpoint, status
from .pilot_contracts import PILOT_REVIEW_ROLES, STABLE_MEMORY_FIELDS, canonical_bytes, validate_pilot_acceptance, validate_pilot_fixture, validate_transition
from .storage import StorageError, read_json, sha256_bytes, sha256_file, write_json


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
            if self.mode == "live" and inspect_pilot_checkpoint(run_dir, manifest)["checkpoint_integrity"] == "RECONCILABLE":
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
                    manifest["completed_transitions"].append(transition_id)
                    self._save_checkpoint(run_dir, manifest)
                else:
                    self._verify_completed_transition(run_dir, manifest, transition_id, episode_id, ids[index + 1], source, index)
        if "pilot_acceptance.json" not in manifest["artifact_hashes"]:
            evidence = self._write_evidence_packet(run_dir, manifest)
            workers_path = run_dir / "pilot_review_workers.json"
            if workers_path.exists():
                if manifest["artifact_hashes"].get("pilot_review_workers.json") != sha256_file(workers_path):
                    raise PilotError("pilot review worker hash mismatch")
                workers = read_json(workers_path)
                if not isinstance(workers, list) or [worker.get("role") for worker in workers] != PILOT_REVIEW_ROLES:
                    raise PilotError("invalid canonical pilot review workers")
                for worker in workers:
                    self._review_worker_contract(worker, worker["role"])
            else:
                workers = self._review_workers(run_dir, manifest, evidence)
                self._save_checkpoint(run_dir, manifest)
                self._write_artifact(run_dir, manifest, "pilot_review_workers.json", workers)
            acceptance = self._acceptance(evidence, workers)
            self._write_artifact(run_dir, manifest, "pilot_acceptance.json", acceptance)
            manifest["acceptance_verdict"] = acceptance["verdict"]
        else:
            acceptance = read_json(run_dir / "pilot_acceptance.json")
            validate_pilot_acceptance(acceptance, self._evidence_refs(manifest))
            manifest["acceptance_verdict"] = acceptance["verdict"]
        manifest["status"] = "COMPLETE" if manifest["acceptance_verdict"] == "PASS" else "HOLD"
        manifest["active_episode_id"] = None
        self._save_checkpoint(run_dir, manifest)
        (run_dir / "pilot_review_workers.partial.json").unlink(missing_ok=True)

    def _transition_input_hash(self, run_dir: Path, manifest: dict, episode_id: str, next_id: str, source: dict, index: int) -> str:
        root = run_dir / "episodes" / episode_id
        value = {"pilot_id": manifest["pilot_id"], "completed_episode_id": episode_id, "next_episode_id": next_id, "source_hash": sha256_bytes(canonical_bytes(source)), "episode_plan_hash": sha256_file(root / "episode_plan.json"), "final_hash": sha256_file(root / "final.md"), "memory_update_hash": sha256_file(root / "memory_update.json"), "memory_after_hash": sha256_file(root / "memory_after.json"), "rolling_plan": source["rolling_plan"], "required_continuity": source["required_next_episode_continuity"], "remaining_episode_count": len(manifest["episode_ids"]) - index - 1}
        return sha256_bytes(canonical_bytes(value))

    def _reconcile_transition(self, run_dir: Path, manifest: dict, transition_id: str, episode_id: str, next_id: str, source: dict, index: int) -> None:
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        source_path = run_dir / "episode_sources" / f"{next_id}.json"
        input_hash = self._transition_input_hash(run_dir, manifest, episode_id, next_id, source, index)
        if transition_path.exists():
            transition = read_json(transition_path)
            validate_transition(transition, source, next_id, str(run_dir))
            if transition["transition_input_hash"] != input_hash:
                raise PilotError("transition input hash mismatch")
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
            transition, next_source = self._transition(run_dir, manifest, episode_id, next_id, source, input_hash)
            self._write_artifact(run_dir, manifest, f"transitions/{transition_id}.json", transition)
            self._write_artifact(run_dir, manifest, f"episode_sources/{next_id}.json", next_source)

    def _transition(self, run_dir: Path, manifest: dict, episode_id: str, next_id: str, source: dict, input_hash: str) -> tuple[dict, dict]:
        episode_dir = run_dir / "episodes" / episode_id
        memory_after = read_json(episode_dir / "memory_after.json")
        update = read_json(episode_dir / "memory_update.json")
        rolling_plan = dict(memory_after["rolling_plan"])
        rolling_plan["near_horizon"] = list(rolling_plan.get("near_horizon", [])) + [f"synthetic transition toward {next_id}"]
        transition = {"schema_version": 1, "completed_episode_id": episode_id, "next_episode_id": next_id, "transition_input_hash": input_hash, "next_source_hash": "pending", "next_episode": {"episode_id": next_id, "importance": "ordinary", "required_role": f"synthetic pilot role for {next_id}"}, "rolling_plan_after": rolling_plan, "continuity_satisfied": [], "continuity_deferred": list(source["required_next_episode_continuity"]), "adaptation_summary": f"Synthetic plan adapts after {episode_id} toward {next_id}.", "evidence_refs": [f"episodes/{episode_id}/final.md", f"episodes/{episode_id}/memory_update.json", f"episodes/{episode_id}/memory_after.json", f"episodes/{episode_id}/episode_plan.json"]}
        next_source = self._next_source_from_transition(run_dir, episode_id, transition)
        transition["next_source_hash"] = _json_file_hash(next_source)
        validate_transition(transition, source, next_id, str(run_dir))
        return transition, next_source

    def _verify_completed_transition(self, run_dir: Path, manifest: dict, transition_id: str, episode_id: str, next_id: str, source: dict, index: int) -> None:
        transition_path = run_dir / "transitions" / f"{transition_id}.json"
        source_path = run_dir / "episode_sources" / f"{next_id}.json"
        if not transition_path.exists() or not source_path.exists():
            raise PilotError("completed transition artifact is missing")
        transition = read_json(transition_path)
        validate_transition(transition, source, next_id, str(run_dir))
        if transition["transition_input_hash"] != self._transition_input_hash(run_dir, manifest, episode_id, next_id, source, index) or transition["next_source_hash"] != sha256_file(source_path):
            raise PilotError("completed transition hash mismatch")

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

    def _write_evidence_packet(self, run_dir: Path, manifest: dict) -> list[str]:
        evidence = {"pilot_id": manifest["pilot_id"], "episode_ids": manifest["episode_ids"], "episodes": [], "transitions": [], "rolling_plan_hashes": []}
        refs = []
        for episode_id in manifest["episode_ids"]:
            root = run_dir / "episodes" / episode_id
            source = read_json(run_dir / "episode_sources" / f"{episode_id}.json")
            evidence["episodes"].append({"episode_id": episode_id, "plan": read_json(root / "episode_plan.json"), "final": (root / "final.md").read_text(encoding="utf-8"), "review_verdict": read_json(root / "review_decision.json")["verdict"], "writer_call_count": read_json(root / "manifest.json")["writer_call_count"], "revision_count": read_json(root / "manifest.json")["revision_count"], "memory_before": {field: source[field] for field in STABLE_MEMORY_FIELDS}, "memory_after": {field: read_json(root / "memory_after.json")[field] for field in STABLE_MEMORY_FIELDS}})
            evidence["rolling_plan_hashes"].append(sha256_bytes(canonical_bytes(source["rolling_plan"])))
            refs.extend([f"episodes/{episode_id}/final.md", f"episodes/{episode_id}/memory_after.json"])
        for episode_id, next_id in zip(manifest["episode_ids"], manifest["episode_ids"][1:]):
            name = f"transitions/{episode_id}_to_{next_id}.json"
            evidence["transitions"].append(read_json(run_dir / name))
            refs.append(name)
        self._write_artifact(run_dir, manifest, "pilot_evidence_packet.json", evidence)
        return refs

    def _evidence_refs(self, manifest: dict) -> list[str]:
        refs = []
        for episode_id in manifest["episode_ids"]:
            refs.extend([f"episodes/{episode_id}/final.md", f"episodes/{episode_id}/memory_after.json"])
        for episode_id, next_id in zip(manifest["episode_ids"], manifest["episode_ids"][1:]):
            refs.append(f"transitions/{episode_id}_to_{next_id}.json")
        return refs

    def _review_workers(self, run_dir: Path, manifest: dict, evidence_refs: list[str]) -> list[dict]:
        evidence_hash = manifest["artifact_hashes"]["pilot_evidence_packet.json"]
        checkpoint = WaveCheckpoint(run_dir / "pilot_review_workers.partial.json", "pilot_review", {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": self.scenario, "episode_ids": manifest["episode_ids"], "evidence_packet_hash": evidence_hash}, PILOT_REVIEW_ROLES)
        workers = [checkpoint.result(role) for role in PILOT_REVIEW_ROLES if checkpoint.result(role)]
        first_error = None
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(PILOT_REVIEW_ROLES)) as executor:
            futures = {executor.submit(self._review_worker, run_dir, manifest, role, evidence_hash): role for role in PILOT_REVIEW_ROLES if not checkpoint.result(role)}
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

    def _review_worker(self, run_dir: Path, manifest: dict, role: str, evidence_hash: str) -> dict:
        payload = {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": self.scenario, "episode_ids": manifest["episode_ids"], "evidence_packet_hash": evidence_hash, "dimension": role, "allowed_evidence_refs": ["pilot_evidence_packet.json"], "contract": "proposal.dimension_result is PASS or HOLD; HOLD requires proposal.critical_finding"}
        if self.mode == "live":
            payload.update({"pilot_evidence_packet": read_json(run_dir / "pilot_evidence_packet.json"), "dimension_question": f"Evaluate pilot dimension: {role}.", "strict_output_schema": {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": "string", "primary_risk": "string", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "PASS|HOLD", "critical_finding": "string|null"}}})
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        client = self._acceptance_client() if self.mode == "live" else self.client
        try:
            value = parse_object(client.generate(stage="pilot_review", role=role, prompt=prompt))
            return self._review_worker_contract(value, role)
        except ContractError:
            if self.mode == "live":
                client.record_contract_failure("pilot_review", role, "UNKNOWN")
            raise

    def _review_worker_contract(self, value: dict, role: str) -> dict:
        worker = validate_worker(value, f"pilot_review-{role}", role)
        proposal = worker["proposal"]
        if set(proposal) != {"dimension_result", "critical_finding"} or proposal["dimension_result"] not in {"PASS", "HOLD"} or worker["evidence_refs"] != ["pilot_evidence_packet.json"] or (proposal["dimension_result"] == "PASS" and proposal["critical_finding"] is not None) or (proposal["dimension_result"] == "HOLD" and (not isinstance(proposal["critical_finding"], str) or not proposal["critical_finding"])):
            raise PilotError("invalid pilot review worker")
        return worker

    def _acceptance(self, evidence_refs: list[str], workers: list[dict]) -> dict:
        dimensions = {worker["role"]: worker["proposal"]["dimension_result"] for worker in workers}
        findings = [worker["proposal"]["critical_finding"] for worker in workers if worker["proposal"]["critical_finding"]]
        value = {"verdict": "PASS" if all(item == "PASS" for item in dimensions.values()) else "HOLD", "dimension_results": dimensions, "critical_findings": findings, "strengths_to_preserve": ["synthetic continuity evidence"], "evidence_refs": evidence_refs}
        return validate_pilot_acceptance(value, evidence_refs)

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


def verify_pilot_artifacts(run_dir: Path, manifest: dict) -> None:
    for name, digest in manifest["artifact_hashes"].items():
        if manifest.get("mode") == "live" and name == "pilot_live_calls.json":
            continue
        path = run_dir / name
        if not path.exists() or sha256_file(path) != digest:
            raise StorageError(f"pilot artifact hash mismatch: {name}")
    immutable = set(manifest["artifact_hashes"]) - ({"pilot_live_calls.json"} if manifest.get("mode") == "live" else set())
    expected = {"pilot_manifest.json", *immutable, "pilot_review_workers.partial.json"}
    operational = {"routing_state.json", "pilot_live_calls.json"} if manifest.get("mode") == "live" else set()
    actual = {path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()}
    if actual - expected - operational - {f"episodes/{episode_id}/{name}" for episode_id in manifest["episode_ids"] for name in _episode_files(run_dir / "episodes" / episode_id)}:
        raise StorageError("unknown pilot artifact")
    if manifest.get("mode") == "live" and (run_dir / "pilot_live_calls.json").exists():
        _verify_live_telemetry_projections(run_dir, manifest)
    for episode_id in manifest["completed_episodes"]:
        status(run_dir / "episodes" / episode_id)


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
        _verify_live_telemetry_projection_for_episodes(run_dir, derived["completed_episodes"] + ([derived["active_episode_id"]] if derived["active_episode_id"] else []))
    except Exception as error:
        return {"checkpoint_integrity": "CORRUPT", "reconciliation_required": False, "reason_codes": [str(error)], "current": current, "derived": {}}
    if manifest.get("mode") == "live" and "pilot_live_calls.json" in manifest.get("artifact_hashes", {}):
        actual = sha256_file(run_dir / "pilot_live_calls.json")
        if manifest["artifact_hashes"]["pilot_live_calls.json"] != actual:
            reason_codes.append("LEGACY_TELEMETRY_HASH_STALE")
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
    return {"no_op": False, "changed_files": ["pilot_manifest.json"], **after}


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
        validate_transition(transition, source, next_id, str(run_dir))
        value = {"pilot_id": manifest["pilot_id"], "completed_episode_id": episode_id, "next_episode_id": next_id, "source_hash": sha256_bytes(canonical_bytes(source)), "episode_plan_hash": sha256_file(run_dir / "episodes" / episode_id / "episode_plan.json"), "final_hash": sha256_file(run_dir / "episodes" / episode_id / "final.md"), "memory_update_hash": sha256_file(run_dir / "episodes" / episode_id / "memory_update.json"), "memory_after_hash": sha256_file(run_dir / "episodes" / episode_id / "memory_after.json"), "rolling_plan": source["rolling_plan"], "required_continuity": source["required_next_episode_continuity"], "remaining_episode_count": len(ids) - index - 1}
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


def _verify_live_telemetry_projections(run_dir: Path, manifest: dict) -> None:
    root = read_json(run_dir / "pilot_live_calls.json")
    call_ids = [call.get("call_id") for call in root.get("calls", [])]
    lease_sequences = [call.get("lease_sequence") for call in root.get("calls", [])]
    if len(call_ids) != len(set(call_ids)):
        raise StorageError("duplicate pilot live call id")
    if len(lease_sequences) != len(set(lease_sequences)):
        raise StorageError("duplicate pilot live lease sequence")
    if lease_sequences and (run_dir / "routing_state.json").exists():
        routing = read_json(run_dir / "routing_state.json")
        if routing.get("next_lease_sequence", 0) <= max(lease_sequences):
            raise StorageError("routing lease sequence behind telemetry")
    verify_live_telemetry_checkpoint(root, manifest.get("live_telemetry_checkpoint"))
    root_by_scope = {}
    for call in root.get("calls", []):
        root_by_scope.setdefault(call.get("scope_id"), []).append(call)
    checked = list(manifest["completed_episodes"])
    active_episode_id = manifest.get("active_episode_id")
    if active_episode_id and (run_dir / "episodes" / active_episode_id / "live_calls.json").exists():
        checked.append(active_episode_id)
    for episode_id in checked:
        _verify_live_telemetry_projection_for_episodes(run_dir, [episode_id], root_by_scope)


def _verify_live_telemetry_projection_for_episodes(run_dir: Path, episode_ids: list[str], root_by_scope: dict | None = None) -> None:
    if root_by_scope is None:
        root_by_scope = {}
        for call in read_json(run_dir / "pilot_live_calls.json").get("calls", []):
            root_by_scope.setdefault(call.get("scope_id"), []).append(call)
    for episode_id in episode_ids:
        live_path = run_dir / "episodes" / episode_id / "live_calls.json"
        if not live_path.exists():
            raise StorageError("episode live telemetry projection mismatch")
        episode_calls = read_json(live_path).get("calls", [])
        scope_id = f"episode:{episode_id}"
        if episode_calls != root_by_scope.get(scope_id, []):
            raise StorageError("episode live telemetry projection mismatch")


def pilot_status(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "pilot_manifest.json")
    inspection = inspect_pilot_checkpoint(run_dir, manifest)
    if inspection["checkpoint_integrity"] == "CORRUPT":
        raise StorageError("; ".join(inspection["reason_codes"]))
    episodes = {episode_id: status(run_dir / "episodes" / episode_id)["status"] for episode_id in manifest["completed_episodes"]}
    finals = all((run_dir / "episodes" / episode_id / "final.md").exists() for episode_id in manifest["completed_episodes"])
    sources = [episode_id for episode_id in manifest["episode_ids"] if (run_dir / "episode_sources" / f"{episode_id}.json").exists()]
    result = {"mode": manifest.get("mode", "mock"), "pilot_id": manifest["pilot_id"], "status": manifest["status"], "episode_count": len(manifest["episode_ids"]), "completed_episode_count": len(manifest["completed_episodes"]), "completed_transition_count": len(manifest["completed_transitions"]), "active_episode_id": manifest["active_episode_id"], "episode_statuses": episodes, "writer_call_count": sum(item["writer_call_count"] for item in manifest["episode_records"]), "revision_count": sum(item["revision_count"] for item in manifest["episode_records"]), "acceptance_verdict": manifest["acceptance_verdict"], "finals_exist": finals, "memory_chain_valid": _memory_chain_valid(run_dir, manifest), "rolling_plan_adapted": len({sha256_bytes(canonical_bytes(read_json(run_dir / "episode_sources" / f"{episode_id}.json")["rolling_plan"])) for episode_id in sources}) > 1, "checkpoint_integrity": inspection["checkpoint_integrity"], "reconciliation_required": inspection["reconciliation_required"], "reason_codes": inspection["reason_codes"], "current": inspection["current"], "derived": inspection["derived"]}
    if result["mode"] == "live":
        telemetry = read_json(run_dir / "pilot_live_calls.json")
        checkpoint = manifest.get("live_telemetry_checkpoint")
        calls = telemetry.get("calls", [])
        call_ids = [call.get("call_id") for call in calls]
        lease_sequences = [call.get("lease_sequence") for call in calls]
        acceptance_calls = [call for call in calls if call.get("scope_id") == "pilot:acceptance"]
        result.update({"model": manifest["model"], "key_pool_size": manifest["key_pool_size"], "configured_max_live": manifest["max_live"], "telemetry_schema_version": telemetry["schema_version"], "pilot_live_call_count": len(calls), "live_telemetry_checkpoint": checkpoint, "successful_live_calls": sum(call["status"] == "PASS" for call in calls), "failed_live_calls": sum(call["status"] == "FAIL" for call in calls), "transient_failure_count": sum(call["status"] == "FAIL" and call.get("error_class") in {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR"} for call in calls), "contract_failure_count": len(telemetry.get("contract_failures", [])), "used_key_slots": sorted({call["key_slot"] for call in calls}), "rotation_count": sum(1 for call in calls if call["status"] == "FAIL" and call.get("error_class")), "episode_call_counts": {episode_id: len(read_json(run_dir / "episodes" / episode_id / "live_calls.json")["calls"]) for episode_id in manifest["completed_episodes"]}, "acceptance_call_count": len(acceptance_calls), "acceptance_pass_calls": sum(call["status"] == "PASS" for call in acceptance_calls), "call_ids_unique": len(call_ids) == len(set(call_ids)), "lease_sequences_unique": len(lease_sequences) == len(set(lease_sequences))})
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
