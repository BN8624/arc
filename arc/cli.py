# Phase 1 mock 수직 루프 검증 명령을 제공한다.
from __future__ import annotations

import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from .mock_model import MockModelClient
from .pipeline import MockPipeline, status
from .pilot import PROJECTION_STALE_REASON, PilotPipeline, pilot_status, reconcile_pilot_checkpoint
from .storage import write_json
from .usage import UsageLedger, backup_usage_db, repair_preflight_collision, usage_db_path
from .prose_probe import PROBE_TELEMETRY, prose_live_probe_status, run_prose_live_probe


def classify_preflight(results: list[dict]) -> dict:
    transient_errors = {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR"}
    disabled_errors = {"AUTH_ERROR", "PERMISSION_ERROR"}
    global_errors = {"INVALID_REQUEST", "MODEL_NOT_FOUND"}
    categories = {"PASS": [], "TRANSIENT": [], "DISABLED": [], "GLOBAL_BLOCKER": [], "UNKNOWN": []}
    for result in results:
        if result["status"] == "PASS":
            category = "PASS"
        elif result["error_class"] in transient_errors:
            category = "TRANSIENT"
        elif result["error_class"] in disabled_errors:
            category = "DISABLED"
        elif result["error_class"] in global_errors:
            category = "GLOBAL_BLOCKER"
        else:
            category = "UNKNOWN"
        result["category"] = category
        categories[category].append(result)
    allowed = bool(categories["PASS"]) and not categories["GLOBAL_BLOCKER"] and not categories["UNKNOWN"]
    degraded = allowed and (len(categories["PASS"]) != len(results) or bool(categories["DISABLED"]))
    return {"categories": categories, "live_run_allowed": allowed, "degraded_admission": degraded, "admission_reason": "at_least_one_pass_and_no_global_blocker" if allowed else "missing_pass_or_blocking_preflight_result", "status": "DEGRADED_PASS" if degraded else "PASS" if allowed else "FAIL"}


def _load_live_client(preflight: Path | None = None, run_dir: Path | None = None, telemetry_path: Path | None = None):
    load_dotenv(override=False)
    from .live_model import AtomicTelemetryStore, GemmaPoolClient, LiveConfig, RoutingStateStore
    config = LiveConfig.from_environment()
    state_store = RoutingStateStore(run_dir / "routing_state.json", list(config.keys)) if run_dir else None
    telemetry_sink = AtomicTelemetryStore(telemetry_path).save if telemetry_path else None
    if preflight is None:
        return GemmaPoolClient(config, state_store=state_store, telemetry_sink=telemetry_sink)
    document = json.loads(preflight.read_text(encoding="utf-8"))
    if document.get("status") not in {"PASS", "DEGRADED_PASS"} or not document.get("live_run_allowed"):
        raise ValueError("preflight does not allow a live run")
    return GemmaPoolClient(config, state_store=state_store, telemetry_sink=telemetry_sink)


def _preflight(output: Path) -> dict:
    client = _load_live_client()
    try:
        slots = [f"K{i:02d}" for i in range(1, 12)]
        def check(slot: str) -> dict:
            raw = client.probe_key(key_slot=slot, prompt=f'Return only {{"ok":true,"slot":"{slot}"}}.')
            value = json.loads(raw)
            if value != {"ok": True, "slot": slot}:
                raise ValueError("invalid preflight response")
            call = next(item for item in client.calls if item["stage"] == "preflight" and item["role"] == slot)
            return {"slot": slot, "status": "PASS", "latency_ms": call["latency_ms"], "error_class": None, "http_status": None}
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=client.config.max_live) as executor:
            futures = {executor.submit(check, slot): slot for slot in slots}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as error:
                    results.append({"slot": futures[future], "status": "FAIL", "latency_ms": 0, "error_class": getattr(error, "error_class", "CONTRACT_ERROR"), "http_status": getattr(error, "http_status", None)})
        results.sort(key=lambda item: item["slot"])
        admission = classify_preflight(results)
        categories = admission.pop("categories")
        document = {"schema_version": 4, "model": client.config.model, "sdk_version": client.sdk_version, "usage_run_id": getattr(getattr(client, "usage_gate", None), "usage_run_id", None), "configured_max_live": client.config.max_live, "max_live": client.config.max_live, "launch_interval_seconds": client.config.launch_interval, "max_active_calls": client.max_active_by_stage.get("preflight", 0), "slots": results, "pass_slots": len(categories["PASS"]), "transient_slots": len(categories["TRANSIENT"]), "disabled_slots": len(categories["DISABLED"]), "global_blocker_slots": len(categories["GLOBAL_BLOCKER"]), "unknown_slots": len(categories["UNKNOWN"]), "total_slots": len(results), **admission}
        write_json(output / "preflight.json", document)
        return document
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="arc")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("mock-run")
    run.add_argument("fixture", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--scenario", choices=["pass", "revise", "hold"], required=True)
    state = commands.add_parser("mock-status")
    state.add_argument("output", type=Path)
    preflight = commands.add_parser("live-preflight")
    preflight.add_argument("--output", type=Path, required=True)
    live = commands.add_parser("live-run")
    live.add_argument("fixture", type=Path)
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--preflight", type=Path, required=True)
    live_state = commands.add_parser("live-status")
    live_state.add_argument("output", type=Path)
    pilot_run = commands.add_parser("pilot-mock-run")
    pilot_run.add_argument("fixture", type=Path)
    pilot_run.add_argument("--output", type=Path, required=True)
    pilot_run.add_argument("--scenario", choices=["pass", "episode_hold", "pilot_hold"], required=True)
    pilot_state = commands.add_parser("pilot-status")
    pilot_state.add_argument("output", type=Path)
    pilot_live = commands.add_parser("pilot-live-run")
    pilot_live.add_argument("fixture", type=Path)
    pilot_live.add_argument("--output", type=Path, required=True)
    pilot_live.add_argument("--preflight", type=Path, required=True)
    pilot_live_state = commands.add_parser("pilot-live-status")
    pilot_live_state.add_argument("output", type=Path)
    pilot_live_reconcile = commands.add_parser("pilot-live-reconcile")
    pilot_live_reconcile.add_argument("fixture", type=Path)
    pilot_live_reconcile.add_argument("--output", type=Path, required=True)
    prose_probe = commands.add_parser("prose-live-probe")
    prose_probe.add_argument("--source-episode", type=Path, required=True)
    prose_probe.add_argument("--output", type=Path, required=True)
    prose_probe.add_argument("--preflight", type=Path, required=True)
    prose_probe_state = commands.add_parser("prose-live-probe-status")
    prose_probe_state.add_argument("output", type=Path)
    usage = commands.add_parser("usage")
    usage_commands = usage.add_subparsers(dest="usage_command", required=True)
    usage_status = usage_commands.add_parser("status")
    usage_status.add_argument("--date")
    usage_status.add_argument("--json", action="store_true")
    usage_import = usage_commands.add_parser("import-pilot")
    usage_import.add_argument("--output", type=Path, required=True)
    usage_check = usage_commands.add_parser("db-check")
    usage_backup = usage_commands.add_parser("backup")
    usage_backup.add_argument("--output", type=Path)
    usage_repair = usage_commands.add_parser("repair-preflight-collision")
    usage_repair.add_argument("--apply", action="store_true")
    usage_repair.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.command == "mock-run":
        result = MockPipeline(MockModelClient(args.scenario)).run(args.fixture, args.output, args.scenario)
        print(json.dumps({"no_op": result["no_op"], **status(args.output)}, ensure_ascii=False))
    elif args.command == "mock-status":
        print(json.dumps(status(args.output), ensure_ascii=False))
    elif args.command == "live-preflight":
        print(json.dumps(_preflight(args.output), ensure_ascii=False))
    elif args.command == "live-run":
        client = _load_live_client(args.preflight, args.output)
        try:
            result = MockPipeline(client, mode="live").run(args.fixture, args.output, None)
            print(json.dumps({"no_op": result["no_op"], **status(args.output)}, ensure_ascii=False))
        finally:
            client.close()
    elif args.command == "pilot-mock-run":
        client = MockModelClient("pass")
        result = PilotPipeline(client, args.scenario).run(args.fixture, args.output)
        print(json.dumps({"no_op": result["no_op"], **pilot_status(args.output)}, ensure_ascii=False))
    elif args.command == "pilot-status":
        print(json.dumps(pilot_status(args.output), ensure_ascii=False))
    elif args.command == "pilot-live-run":
        current = pilot_status(args.output) if (args.output / "pilot_manifest.json").exists() else None
        if current and current.get("checkpoint_integrity") == "RECONCILABLE" and set(current.get("reason_codes", [])) - {PROJECTION_STALE_REASON}:
            raise RuntimeError("pilot checkpoint reconciliation required")
        client = _load_live_client(args.preflight, args.output, args.output / "pilot_live_calls.json")
        try:
            result = PilotPipeline(client, scenario=None, mode="live").run(args.fixture, args.output)
            print(json.dumps({"no_op": result["no_op"], **pilot_status(args.output)}, ensure_ascii=False))
        finally:
            client.close()
    elif args.command == "pilot-live-status":
        print(json.dumps(pilot_status(args.output), ensure_ascii=False))
    elif args.command == "pilot-live-reconcile":
        print(json.dumps(reconcile_pilot_checkpoint(args.fixture, args.output), ensure_ascii=False))
    elif args.command == "prose-live-probe":
        client = _load_live_client(args.preflight, args.output, args.output / PROBE_TELEMETRY)
        try:
            print(json.dumps(run_prose_live_probe(args.source_episode, args.output, args.preflight, client), ensure_ascii=False))
        finally:
            client.close()
    elif args.command == "prose-live-probe-status":
        print(json.dumps(prose_live_probe_status(args.output), ensure_ascii=False))
    elif args.command == "usage":
        ledger = UsageLedger(usage_db_path())
        if args.usage_command == "status":
            document = ledger.status(args.date)
            if args.json:
                print(json.dumps(document, ensure_ascii=False))
            else:
                totals = document["totals"]
                print(f"Pacific date: {document['pacific_date']}")
                print(f"DB: {ledger.path}")
                print(f"provider_requests={totals['provider_requests']} generation_requests={totals['generation_requests']} count_token_requests={totals['count_token_requests']} blocked={totals['blocked_count']}")
                print(f"input_tokens={totals['actual_input_tokens']} candidate_tokens={totals['candidate_tokens']} reasoning_tokens={totals['reasoning_tokens']} output_tokens={totals['combined_output_tokens']} provider_total_tokens={totals['provider_total_tokens']}")
                for row in document["keys"]:
                    print(f"{row['key_slot_id']} provider_requests={row['provider_requests']} generation_requests={row['generation_requests']} count_token_requests={row['count_token_requests']} blocked={row['blocked_count']} output_tokens={row['combined_output_tokens']}")
        elif args.usage_command == "import-pilot":
            print(json.dumps(ledger.import_pilot(args.output), ensure_ascii=False))
        elif args.usage_command == "db-check":
            print(json.dumps({"path": str(ledger.path), "schema_version": ledger.schema_version(), "status": "OK"}, ensure_ascii=False))
        elif args.usage_command == "backup":
            print(json.dumps(backup_usage_db(ledger.path, args.output), ensure_ascii=False))
        elif args.usage_command == "repair-preflight-collision":
            print(json.dumps(repair_preflight_collision(ledger, apply=args.apply, backup_path=args.backup), ensure_ascii=False))
    else:
        print(json.dumps(status(args.output), ensure_ascii=False))
