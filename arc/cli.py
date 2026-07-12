# Phase 1 mock 수직 루프 검증 명령을 제공한다.
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .mock_model import MockModelClient
from .pipeline import MockPipeline, status


def main() -> None:
    parser = argparse.ArgumentParser(prog="arc")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("mock-run")
    run.add_argument("fixture", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--scenario", choices=["pass", "revise", "hold"], required=True)
    state = commands.add_parser("mock-status")
    state.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "mock-run":
        result = MockPipeline(MockModelClient(args.scenario)).run(args.fixture, args.output, args.scenario)
        print(json.dumps({"no_op": result["no_op"], **status(args.output)}, ensure_ascii=False))
    else:
        print(json.dumps(status(args.output), ensure_ascii=False))
