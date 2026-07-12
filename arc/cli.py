# Phase 1 mock 수직 루프 검증 명령을 제공한다.
from __future__ import annotations

import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from .mock_model import MockModelClient
from .pipeline import MockPipeline, status
from .storage import write_json


def _load_live_client(preflight: Path | None = None):
    load_dotenv(override=False)
    from .live_model import GemmaPoolClient, LiveConfig, build_health_assignment
    config = LiveConfig.from_environment()
    if preflight is None:
        return GemmaPoolClient(config)
    document = json.loads(preflight.read_text(encoding="utf-8"))
    if document.get("status") not in {"PASS", "DEGRADED_PASS"} or not document.get("live_run_allowed"):
        raise ValueError("preflight does not allow a live run")
    return GemmaPoolClient(config, assignments=build_health_assignment(document["healthy_slots"]))


def _preflight(output: Path) -> dict:
    client = _load_live_client()
    try:
        slots = [f"K{i:02d}" for i in range(1, 12)]
        def check(slot: str) -> dict:
            raw = client.generate(stage="preflight", role=slot, prompt=f'Return only {{"ok":true,"slot":"{slot}"}}.')
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
        healthy = [item["slot"] for item in results if item["status"] == "PASS"]
        transient = [item for item in results if item["status"] == "FAIL" and item["error_class"] in {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR"}]
        fatal = [item for item in results if item["status"] == "FAIL" and item not in transient]
        status = "PASS" if len(healthy) == 11 else "DEGRADED_PASS" if len(healthy) >= 7 and not fatal else "FAIL"
        document = {"schema_version": 3, "model": client.config.model, "sdk_version": client.sdk_version, "configured_max_live": client.config.max_live, "max_live": client.config.max_live, "launch_interval_seconds": client.config.launch_interval, "max_active_calls": client.max_active_by_stage.get("preflight", 0), "slots": results, "healthy_slots": healthy, "transient_unavailable_slots": transient, "fatal_slots": fatal, "minimum_healthy_slots": 7, "live_run_allowed": status in {"PASS", "DEGRADED_PASS"}, "status": status}
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
    args = parser.parse_args()
    if args.command == "mock-run":
        result = MockPipeline(MockModelClient(args.scenario)).run(args.fixture, args.output, args.scenario)
        print(json.dumps({"no_op": result["no_op"], **status(args.output)}, ensure_ascii=False))
    elif args.command == "mock-status":
        print(json.dumps(status(args.output), ensure_ascii=False))
    elif args.command == "live-preflight":
        print(json.dumps(_preflight(args.output), ensure_ascii=False))
    elif args.command == "live-run":
        client = _load_live_client(args.preflight)
        try:
            result = MockPipeline(client, mode="live").run(args.fixture, args.output, None)
            print(json.dumps({"no_op": result["no_op"], **status(args.output)}, ensure_ascii=False))
        finally:
            client.close()
    else:
        print(json.dumps(status(args.output), ensure_ascii=False))
