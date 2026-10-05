from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .compiler import Compiler
from .loader import load_system_definition


DEFAULT_TEMPLATE = """name: my_system
version: 0.1.0
policies:
  - name: allow_route
    when:
      - field: route.status
        equals: ready
    result:
      status: allow
      action: route_source
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="morph",
        description="MORPH CLI for loading system definitions and compiling them to execution targets.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create a starter MORPH YAML definition.")
    init_parser.add_argument("path", nargs="?", default="morph.yaml", help="Destination YAML file.")

    compile_parser = subparsers.add_parser("compile", help="Compile a MORPH YAML definition to a target backend.")
    compile_parser.add_argument("source", help="Path to the YAML file to compile.")
    compile_parser.add_argument("--target", default="python", choices=["python", "node", "sql"], help="Target backend to compile for.")
    compile_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def _cmd_init(path: str) -> int:
    destination = Path(path)
    if destination.exists() and destination.is_file():
        print(f"MORPH definition already exists at {destination}")
        return 1

    destination.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    print(f"Created MORPH definition at {destination}")
    return 0


def _cmd_compile(source: str, target: str, pretty: bool) -> int:
    definition = load_system_definition(Path(source))
    compiled = Compiler.compile(definition.to_dict(), target=target)
    payload = json.dumps(compiled, indent=2 if pretty else None)
    print(payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "init":
        return _cmd_init(args.path)

    if args.command == "compile":
        return _cmd_compile(args.source, args.target, args.pretty)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
