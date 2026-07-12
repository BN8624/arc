# 기존 단일 회차 pipeline을 순차 다섯 회차 pilot으로 조정한다.
from __future__ import annotations

import json
from pathlib import Path

from .contracts import ContractError, validate_worker
from .pipeline import MockPipeline, status
from .pilot_contracts import PILOT_REVIEW_ROLES, STABLE_MEMORY_FIELDS, canonical_bytes, validate_pilot_acceptance, validate_pilot_fixture, validate_transition
from .storage import StorageError, read_json, sha256_bytes, sha256_file, write_json


class PilotError(RuntimeError):
    """A five-episode pilot could not safely advance."""


class PilotPipeline:
    def __init__(self, client, scenario: str):
        if scenario not in {"pass", "episode_hold", "pilot_hold"}:
            raise PilotError("unknown pilot scenario")
        self.client, self.scenario = client, scenario

    def run(self, fixture_path: Path, run_dir: Path) -> dict:
        raw = fixture_path.read_bytes()
        fixture = validate_pilot_fixture(json.loads(raw.decode("utf-8")))
        source_hash = sha256_bytes(raw)
        manifest_path = run_dir / "pilot_manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest["source_hash"] != source_hash or manifest["scenario"] != self.scenario or manifest["mode"] != "mock":
                raise PilotError("pilot input changed; refusing reuse")
            verify_pilot_artifacts(run_dir, manifest)
            if manifest["status"] in {"COMPLETE", "HOLD"}:
                return {"no_op": True, "manifest": manifest}
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest = {"schema_version": 1, "mode": "mock", "pilot_id": fixture["pilot_id"], "fixture_id": fixture["initial_source"]["fixture_id"], "source_hash": source_hash, "scenario": self.scenario, "status": "RUNNING", "episode_ids": fixture["episode_ids"], "completed_episodes": [], "completed_transitions": [], "active_episode_id": fixture["episode_ids"][0], "episode_records": [], "artifact_hashes": {}, "acceptance_verdict": None, "last_error": None}
            self._save_manifest(run_dir, manifest)
        try:
            self._advance(fixture, run_dir, manifest)
        except Exception as error:
            manifest["status"] = "ERROR"
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
                    self._save_manifest(run_dir, manifest)
                    return
                if current["status"] != "COMPLETE":
                    raise PilotError("episode did not complete")
                record = {"episode_id": episode_id, "status": current["status"], "writer_call_count": current["writer_call_count"], "revision_count": current["revision_count"], "final_sha256": sha256_file(episode_dir / "final.md"), "memory_after_sha256": sha256_file(episode_dir / "memory_after.json")}
                manifest["completed_episodes"].append(episode_id)
                manifest["episode_records"].append(record)
                self._save_manifest(run_dir, manifest)
            if index < len(ids) - 1:
                transition_id = f"{episode_id}_to_{ids[index + 1]}"
                if transition_id not in manifest["completed_transitions"]:
                    transition, next_source = self._transition(run_dir, episode_id, ids[index + 1], source)
                    self._write_artifact(run_dir, manifest, f"transitions/{transition_id}.json", transition)
                    self._write_artifact(run_dir, manifest, f"episode_sources/{ids[index + 1]}.json", next_source)
                    manifest["completed_transitions"].append(transition_id)
                    self._save_manifest(run_dir, manifest)
        if "pilot_acceptance.json" not in manifest["artifact_hashes"]:
            evidence = self._write_evidence_packet(run_dir, manifest)
            workers = [self._review_worker(role) for role in PILOT_REVIEW_ROLES]
            acceptance = self._acceptance(evidence, workers)
            self._write_artifact(run_dir, manifest, "pilot_review_workers.json", workers)
            self._write_artifact(run_dir, manifest, "pilot_acceptance.json", acceptance)
            manifest["acceptance_verdict"] = acceptance["verdict"]
        manifest["status"] = "COMPLETE" if manifest["acceptance_verdict"] == "PASS" else "HOLD"
        manifest["active_episode_id"] = None
        self._save_manifest(run_dir, manifest)

    def _transition(self, run_dir: Path, episode_id: str, next_id: str, source: dict) -> tuple[dict, dict]:
        episode_dir = run_dir / "episodes" / episode_id
        memory_after = read_json(episode_dir / "memory_after.json")
        update = read_json(episode_dir / "memory_update.json")
        rolling_plan = dict(memory_after["rolling_plan"])
        rolling_plan["near_horizon"] = list(rolling_plan.get("near_horizon", [])) + [f"synthetic transition toward {next_id}"]
        transition = {"completed_episode_id": episode_id, "next_episode": {"episode_id": next_id, "importance": "ordinary", "required_role": f"synthetic pilot role for {next_id}"}, "rolling_plan_after": rolling_plan, "continuity_satisfied": [], "continuity_deferred": list(source["required_next_episode_continuity"]), "adaptation_summary": f"Synthetic plan adapts after {episode_id} toward {next_id}.", "evidence_refs": [f"episodes/{episode_id}/final.md", f"episodes/{episode_id}/memory_update.json", f"episodes/{episode_id}/memory_after.json", f"episodes/{episode_id}/episode_plan.json"]}
        validate_transition(transition, source, next_id, str(run_dir))
        next_source = dict(memory_after)
        next_source["current_episode"] = transition["next_episode"]
        next_source["rolling_plan"] = transition["rolling_plan_after"]
        next_source["required_next_episode_continuity"] = _unique(transition["continuity_deferred"] + update["required_next_episode_continuity"])
        for field in STABLE_MEMORY_FIELDS:
            if canonical_bytes(next_source[field]) != canonical_bytes(memory_after[field]):
                raise PilotError("transition mutated stable memory")
        return transition, next_source

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

    def _review_worker(self, role: str) -> dict:
        value = {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"role": role}}
        return validate_worker(value, f"pilot_review-{role}", role)

    def _acceptance(self, evidence_refs: list[str], workers: list[dict]) -> dict:
        dimensions = {worker["role"]: "PASS" for worker in workers}
        if self.scenario == "pilot_hold":
            dimensions["continuity"] = "HOLD"
        value = {"verdict": "PASS" if all(item == "PASS" for item in dimensions.values()) else "HOLD", "dimension_results": dimensions, "critical_findings": [] if all(item == "PASS" for item in dimensions.values()) else ["synthetic cross-episode continuity hold"], "strengths_to_preserve": ["synthetic continuity evidence"], "evidence_refs": evidence_refs}
        return validate_pilot_acceptance(value, evidence_refs)

    def _write_artifact(self, run_dir: Path, manifest: dict, name: str, value: dict | list) -> None:
        manifest["artifact_hashes"][name] = write_json(run_dir / name, value)

    def _save_manifest(self, run_dir: Path, manifest: dict) -> None:
        write_json(run_dir / "pilot_manifest.json", manifest)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def verify_pilot_artifacts(run_dir: Path, manifest: dict) -> None:
    for name, digest in manifest["artifact_hashes"].items():
        path = run_dir / name
        if not path.exists() or sha256_file(path) != digest:
            raise StorageError(f"pilot artifact hash mismatch: {name}")
    expected = {"pilot_manifest.json", *manifest["artifact_hashes"]}
    actual = {path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()}
    if actual - expected - {f"episodes/{episode_id}/{name}" for episode_id in manifest["episode_ids"] for name in _episode_files(run_dir / "episodes" / episode_id)}:
        raise StorageError("unknown pilot artifact")
    for episode_id in manifest["completed_episodes"]:
        status(run_dir / "episodes" / episode_id)


def _episode_files(path: Path) -> list[str]:
    return [item.name for item in path.iterdir() if item.is_file()] if path.exists() else []


def pilot_status(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "pilot_manifest.json")
    verify_pilot_artifacts(run_dir, manifest)
    episodes = {episode_id: status(run_dir / "episodes" / episode_id)["status"] for episode_id in manifest["completed_episodes"]}
    finals = all((run_dir / "episodes" / episode_id / "final.md").exists() for episode_id in manifest["completed_episodes"])
    sources = [episode_id for episode_id in manifest["episode_ids"] if (run_dir / "episode_sources" / f"{episode_id}.json").exists()]
    return {"pilot_id": manifest["pilot_id"], "status": manifest["status"], "episode_count": len(manifest["episode_ids"]), "completed_episode_count": len(manifest["completed_episodes"]), "completed_transition_count": len(manifest["completed_transitions"]), "active_episode_id": manifest["active_episode_id"], "episode_statuses": episodes, "writer_call_count": sum(item["writer_call_count"] for item in manifest["episode_records"]), "revision_count": sum(item["revision_count"] for item in manifest["episode_records"]), "acceptance_verdict": manifest["acceptance_verdict"], "finals_exist": finals, "memory_chain_valid": _memory_chain_valid(run_dir, manifest), "rolling_plan_adapted": len({sha256_bytes(canonical_bytes(read_json(run_dir / "episode_sources" / f"{episode_id}.json")["rolling_plan"])) for episode_id in sources}) > 1}


def _memory_chain_valid(run_dir: Path, manifest: dict) -> bool:
    for current, following in zip(manifest["episode_ids"], manifest["episode_ids"][1:]):
        if current not in manifest["completed_episodes"] or following not in manifest["completed_episodes"]:
            continue
        memory_after = read_json(run_dir / "episodes" / current / "memory_after.json")
        source = read_json(run_dir / "episode_sources" / f"{following}.json")
        if any(canonical_bytes(memory_after[field]) != canonical_bytes(source[field]) for field in STABLE_MEMORY_FIELDS):
            return False
    return True
