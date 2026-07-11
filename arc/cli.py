# ARC 초기화와 상태 조회 명령을 제공한다.

import argparse
import json
from pathlib import Path

from .artifacts import episode_directory, missing_artifacts
from .project import initialise_project
from .states import ApprovalGate, EpisodeState, TRANSITIONS
from .validation import ValidationError
from .workflow import advance, approve, create_episode, run_until_blocked, status as episode_status


def default_project_root() -> Path:
    return Path("projects") / "kingdom_archive"


def command_init(args: argparse.Namespace) -> int:
    project_root = Path(args.path)
    created = initialise_project(project_root)
    if created:
        print(f"initialized {project_root}")
        for path in created:
            print(f"created {path}")
    else:
        print(f"already initialized {project_root}; no files changed")
    return 0


def command_status(args: argparse.Namespace) -> int:
    project_root = Path(args.path)
    episodes_root = project_root / "episodes"
    if not (project_root / "project.json").exists():
        print(f"project not initialized: {project_root}")
        return 1
    episode_files = sorted(episodes_root.glob("*/episode.json"))
    if not episode_files:
        print("current state: no episodes")
        print("missing artifacts: none")
        print("next allowed work: create a PITCHED episode manifest after G1 approval")
        return 0
    for manifest_path in episode_files:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = EpisodeState(data["state"])
        missing = missing_artifacts(episode_directory(project_root, data["episode_id"]), state)
        next_states = ", ".join(item.value for item in TRANSITIONS[state]) or "none"
        print(f"{data['episode_id']}: {state.value}")
        print(f"  missing artifacts: {', '.join(missing) if missing else 'none'}")
        print(f"  next allowed work: {next_states}")
    return 0


def command_episode_create(args: argparse.Namespace) -> int:
    create_episode(Path(args.path), args.episode_id, args.scenario)
    print(f"created {args.episode_id} from fixture scenario {args.scenario}")
    return 0


def command_episode_advance(args: argparse.Namespace) -> int:
    state = advance(Path(args.path), args.episode_id)
    print(f"{args.episode_id}: {state.value}")
    return 0


def command_episode_run(args: argparse.Namespace) -> int:
    state, reason = run_until_blocked(Path(args.path), args.episode_id)
    print(f"{args.episode_id}: {state.value}")
    if reason:
        print(f"blocked: {reason}")
    return 0


def command_episode_status(args: argparse.Namespace) -> int:
    state, missing, reason = episode_status(Path(args.path), args.episode_id)
    print(f"{args.episode_id}: {state.value}")
    print(f"missing artifacts: {', '.join(missing) if missing else 'none'}")
    print(f"blocked: {reason or 'none'}")
    return 0


def command_approve(args: argparse.Namespace) -> int:
    if len(args.items) == 1:
        episode_id, gate_value = None, args.items[0]
    elif len(args.items) == 2:
        episode_id, gate_value = args.items
    else:
        raise ValidationError("usage: arc approve [EPISODE_ID] GATE")
    gate = ApprovalGate(gate_value)
    changed = approve(Path(args.path), episode_id, gate)
    print(f"{gate.value}: {'recorded' if changed else 'already recorded'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create the project skeleton without overwriting files")
    init_parser.add_argument("path", nargs="?", default=default_project_root())
    init_parser.set_defaults(func=command_init)
    status_parser = subparsers.add_parser("status", help="show episode state, missing artifacts, and next work")
    status_parser.add_argument("path", nargs="?", default=default_project_root())
    status_parser.set_defaults(func=command_status)
    approve_parser = subparsers.add_parser("approve", help="record a user approval")
    approve_parser.add_argument("items", nargs="+")
    approve_parser.add_argument("--path", default=default_project_root())
    approve_parser.set_defaults(func=command_approve)
    episode_parser = subparsers.add_parser("episode", help="run the E001 fixture workflow")
    episode_subparsers = episode_parser.add_subparsers(dest="episode_command", required=True)
    create_parser = episode_subparsers.add_parser("create", help="create an episode from fixtures")
    create_parser.add_argument("episode_id")
    create_parser.add_argument("--scenario", default="pass")
    create_parser.add_argument("--path", default=default_project_root())
    create_parser.set_defaults(func=command_episode_create)
    advance_parser = episode_subparsers.add_parser("advance", help="advance one workflow step")
    advance_parser.add_argument("episode_id")
    advance_parser.add_argument("--path", default=default_project_root())
    advance_parser.set_defaults(func=command_episode_advance)
    run_parser = episode_subparsers.add_parser("run", help="advance until an approval or block")
    run_parser.add_argument("episode_id")
    run_parser.add_argument("--path", default=default_project_root())
    run_parser.set_defaults(func=command_episode_run)
    episode_status_parser = episode_subparsers.add_parser("status", help="show episode state and block reason")
    episode_status_parser.add_argument("episode_id")
    episode_status_parser.add_argument("--path", default=default_project_root())
    episode_status_parser.set_defaults(func=command_episode_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (ValidationError, ValueError) as error:
        print(f"error: {error}")
        return 1
