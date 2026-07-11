# ARC 초기화와 상태 조회 명령을 제공한다.

import argparse
import json
from pathlib import Path

from .artifacts import episode_directory, missing_artifacts
from .project import initialise_project
from .states import EpisodeState, TRANSITIONS


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create the project skeleton without overwriting files")
    init_parser.add_argument("path", nargs="?", default=default_project_root())
    init_parser.set_defaults(func=command_init)
    status_parser = subparsers.add_parser("status", help="show episode state, missing artifacts, and next work")
    status_parser.add_argument("path", nargs="?", default=default_project_root())
    status_parser.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
