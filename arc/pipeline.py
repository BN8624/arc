# Phase 1 합성 회차의 재개 가능한 수직 루프를 실행한다.
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .contracts import ContractError, ModelClient, parse_object, validate_fixture, validate_memory, validate_plan, validate_review, validate_worker
from .storage import StorageError, read_json, sha256_bytes, sha256_file, verify_artifacts, write_json, write_text

PLANNING_ROLES = ["event", "protagonist_action", "relationship", "continuity", "readability_weight", "reader_payoff"]
REVIEW_ROLES = ["causality", "protagonist_agency", "character_consistency", "continuity", "readability", "narrative_weight", "payoff_and_hook"]
MEMORY_ROLES = ["confirmed_facts", "relationships", "conflicts_and_promises", "important_excerpts"]


class PipelineError(RuntimeError):
    """A Phase 1 run could not safely advance."""


class MockPipeline:
    def __init__(self, client: ModelClient):
        self.client = client

    def run(self, fixture_path: Path, run_dir: Path, scenario: str) -> dict:
        if scenario not in {"pass", "revise", "hold"}:
            raise PipelineError("unknown scenario")
        raw = fixture_path.read_bytes()
        source = json.loads(raw.decode("utf-8"))
        validate_fixture(source)
        source_hash = sha256_bytes(raw)
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest["source_hash"] != source_hash or manifest["scenario"] != scenario:
                raise PipelineError("source or scenario changed; refusing reuse")
            verify_artifacts(run_dir, manifest)
            if manifest["status"] in {"COMPLETE", "HOLD"}:
                return {"no_op": True, "manifest": manifest}
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest = {"schema_version": 1, "fixture_id": source["fixture_id"], "episode_id": source["current_episode"]["episode_id"], "scenario": scenario, "status": "RUNNING", "completed_stages": [], "source_hash": source_hash, "artifact_hashes": {}, "writer_call_count": 0, "revision_count": 0, "review_verdict": None, "last_error": None}
            write_json(manifest_path, manifest)
        try:
            self._advance(source, run_dir, manifest)
        except Exception as error:
            manifest["status"] = "ERROR"
            manifest["last_error"] = str(error)
            write_json(manifest_path, manifest)
            raise
        return {"no_op": False, "manifest": manifest}

    def _advance(self, source: dict, run_dir: Path, manifest: dict) -> None:
        if "CONTEXT_ASSEMBLED" not in manifest["completed_stages"]:
            context = {"fixture_id": source["fixture_id"], "episode_id": source["current_episode"]["episode_id"], "current_episode": source["current_episode"], "series_compass": source["series_compass"], "world_rules": source["world_rules"], "characters": source["characters"], "confirmed_facts": source["confirmed_facts"], "relationship_state": source["relationship_state"], "open_conflicts": source["open_conflicts"], "recent_summaries": source["episode_summaries"], "important_excerpts": source["important_excerpts"], "rolling_plan": source["rolling_plan"], "source_hash": manifest["source_hash"]}
            self._commit(run_dir, manifest, "context_packet.json", context, "CONTEXT_ASSEMBLED")
        context = read_json(run_dir / "context_packet.json")
        if "PLANNING_WAVE_COMPLETED" not in manifest["completed_stages"]:
            workers = self._wave("planning", PLANNING_ROLES, context)
            self._commit(run_dir, manifest, "planning_workers.json", workers, "PLANNING_WAVE_COMPLETED")
        planning = read_json(run_dir / "planning_workers.json")
        if "PLAN_MERGED" not in manifest["completed_stages"]:
            value = validate_plan(parse_object(self.client.generate(stage="planning_merge", role="merge", prompt=json.dumps({"context": context, "workers": planning}))), manifest["episode_id"])
            self._commit(run_dir, manifest, "episode_plan.json", value, "PLAN_MERGED")
        plan = read_json(run_dir / "episode_plan.json")
        if "DRAFT_COMPLETED" not in manifest["completed_stages"]:
            value = parse_object(self.client.generate(stage="writer", role="canonical", prompt=json.dumps({"context": context, "plan": plan})))
            text = value.get("text")
            if not isinstance(text, str) or not text.strip() or text.lstrip().startswith(("{", "[")):
                raise ContractError("invalid canonical draft")
            manifest["writer_call_count"] += 1
            self._commit(run_dir, manifest, "draft.md", text, "DRAFT_COMPLETED", text=True)
        draft = (run_dir / "draft.md").read_text(encoding="utf-8")
        if "REVIEW_WAVE_COMPLETED" not in manifest["completed_stages"]:
            workers = self._wave("review", REVIEW_ROLES, {"context": context, "plan": plan, "draft": draft})
            self._commit(run_dir, manifest, "review_workers.json", workers, "REVIEW_WAVE_COMPLETED")
        review_workers = read_json(run_dir / "review_workers.json")
        if "REVIEW_MERGED" not in manifest["completed_stages"]:
            decision = validate_review(parse_object(self.client.generate(stage="review_merge", role="merge", prompt=json.dumps({"context": context, "plan": plan, "draft": draft, "workers": review_workers}))))
            manifest["review_verdict"] = decision["verdict"]
            self._commit(run_dir, manifest, "review_decision.json", decision, "REVIEW_MERGED")
        decision = read_json(run_dir / "review_decision.json")
        if decision["verdict"] == "HOLD":
            manifest["status"] = "HOLD"
            manifest["last_error"] = None
            self._save_manifest(run_dir, manifest)
            return
        if decision["verdict"] == "REVISE_ONCE" and "REVISION_COMPLETED" not in manifest["completed_stages"]:
            value = parse_object(self.client.generate(stage="revision", role="canonical", prompt=json.dumps({"context": context, "plan": plan, "draft": draft, "decision": decision})))
            text = value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ContractError("invalid revision")
            manifest["revision_count"] = 1
            self._commit(run_dir, manifest, "revised.md", text, "REVISION_COMPLETED", text=True)
        if "FINALIZED" not in manifest["completed_stages"]:
            source_path = run_dir / ("revised.md" if decision["verdict"] == "REVISE_ONCE" else "draft.md")
            self._commit(run_dir, manifest, "final.md", source_path.read_text(encoding="utf-8"), "FINALIZED", text=True)
        final = (run_dir / "final.md").read_text(encoding="utf-8")
        if "MEMORY_WAVE_COMPLETED" not in manifest["completed_stages"]:
            workers = self._wave("memory", MEMORY_ROLES, {"final": final})
            self._commit(run_dir, manifest, "memory_workers.json", workers, "MEMORY_WAVE_COMPLETED")
        memory_workers = read_json(run_dir / "memory_workers.json")
        if "MEMORY_MERGED" not in manifest["completed_stages"]:
            update = validate_memory(parse_object(self.client.generate(stage="memory_merge", role="merge", prompt=json.dumps({"final": final, "workers": memory_workers}))), manifest["episode_id"])
            self._commit(run_dir, manifest, "memory_update.json", update, "MEMORY_MERGED")
            memory_after = {"confirmed_facts": source["confirmed_facts"] + update["confirmed_facts_added"], "relationship_state": source["relationship_state"] + update["relationship_changes"], "open_conflicts": source["open_conflicts"] + update["conflicts_opened"], "episode_summaries": source["episode_summaries"] + [update["episode_summary"]]}
            self._commit_artifact(run_dir, manifest, "memory_after.json", memory_after)
        manifest["status"] = "COMPLETE"
        manifest["last_error"] = None
        self._save_manifest(run_dir, manifest)

    def _wave(self, stage: str, roles: list[str], payload: dict) -> list[dict]:
        def one(role: str) -> dict:
            value = parse_object(self.client.generate(stage=stage, role=role, prompt=json.dumps(payload)))
            return validate_worker(value, f"{stage}-{role}", role)
        with ThreadPoolExecutor(max_workers=min(11, len(roles))) as executor:
            futures = [executor.submit(one, role) for role in roles]
            results = [future.result() for future in as_completed(futures)]
        return sorted(results, key=lambda item: roles.index(item["role"]))

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


def status(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "manifest.json")
    verify_artifacts(run_dir, manifest)
    return {"fixture_id": manifest["fixture_id"], "episode_id": manifest["episode_id"], "status": manifest["status"], "completed_stages": manifest["completed_stages"], "review_verdict": manifest["review_verdict"], "writer_call_count": manifest["writer_call_count"], "revision_count": manifest["revision_count"], "final_exists": (run_dir / "final.md").exists(), "memory_merged": "MEMORY_MERGED" in manifest["completed_stages"], "last_error": manifest["last_error"]}
