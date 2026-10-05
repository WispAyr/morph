from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import yaml

from .compiler import Compiler
from .ir import MORPHIR
from .loader import load_system_definition
from .project import ProjectScaffold
from .runtime import MORPHRuntime
from .workflow import WorkflowEngine


DEFAULT_TEMPLATE = """name: my_system
version: 0.1.0

# Entities declare the shape of the context. Policies may only read declared fields.
entities:
  - name: route
    fields:
      status: string
      locked: bool

# Policies are evaluated in order; the first whose condition holds decides.
# Conditions are CEL expressions: https://cel.dev
policies:
  - name: deny_locked_route
    when: has(route.locked) && route.locked
    result:
      status: deny
      action: raise_alert
  - name: allow_route
    when: route.status == "ready"
    result:
      status: allow
      action: route_source
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="morph",
        description="MORPH CLI for validating, evaluating, and compiling system definitions.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create a starter MORPH YAML definition.")
    init_parser.add_argument("path", nargs="?", default="morph.yaml", help="Destination YAML file.")

    new_parser = subparsers.add_parser("new", help="Create a MORPH project from a registered template.")
    new_parser.add_argument("path", help="Destination directory for the new MORPH project.")
    new_parser.add_argument("--template", default="service", choices=ProjectScaffold.list_templates(), help="Project template to apply.")

    validate_parser = subparsers.add_parser("validate", help="Check a MORPH YAML definition without evaluating it.")
    validate_parser.add_argument("source", help="Path to the YAML file to validate.")

    compile_parser = subparsers.add_parser("compile", help="Compile a MORPH YAML definition to a target backend.")
    compile_parser.add_argument("source", help="Path to the YAML file to compile.")
    compile_parser.add_argument("--target", default="python", choices=Compiler.list_targets(), help="Target backend to compile for.")
    compile_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    run_parser = subparsers.add_parser("run", help="Evaluate a MORPH YAML definition against a context.")
    run_parser.add_argument("source", help="Path to the YAML file to evaluate.")
    run_parser.add_argument("--context", help="Path to a JSON or YAML file holding the execution context.")
    run_parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="PATH=VALUE",
        help="Set a dotted context path, e.g. --set source.status=live. Values are parsed as YAML scalars.",
    )
    run_parser.add_argument("--explain", action="store_true", help="Report how every policy fared, not just the decision.")
    run_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def _build_runtime(definition: MORPHIR) -> MORPHRuntime:
    return MORPHRuntime(
        name=definition.name,
        version=definition.version,
        policies=definition.policies,
        capabilities=definition.capabilities,
        entities=definition.entities,
    )


def _cmd_init(path: str) -> int:
    destination = Path(path)
    if destination.exists() and destination.is_file():
        print(f"MORPH definition already exists at {destination}")
        return 1

    destination.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
    print(f"Created MORPH definition at {destination}")
    return 0


def _cmd_validate(source: str) -> int:
    try:
        definition = load_system_definition(Path(source))
        runtime = _build_runtime(definition)
        workflow_steps = 0
        if definition.workflow:
            workflow_steps = len(WorkflowEngine(definition)._steps())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1

    summary = f"ok: {definition.name} {definition.version}: {len(runtime.policies)} policies, {len(runtime.schema.entities)} entities"
    if workflow_steps:
        summary += f", {workflow_steps} workflow steps"
    if runtime.schema.empty:
        summary += " (no entity schema: field paths are unchecked)"
    print(summary)
    return 0


def _cmd_compile(source: str, target: str, pretty: bool) -> int:
    try:
        definition = load_system_definition(Path(source))
        compiled = Compiler.compile(definition.to_dict(), target=target)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1

    print(json.dumps(compiled, indent=2 if pretty else None))
    return 0


def _load_context(path: str | None, overrides: Sequence[str]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if path:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("Context file must hold an object at the top level.")
        context = loaded

    for override in overrides:
        key, separator, raw_value = override.partition("=")
        if not separator or not key:
            raise ValueError(f"Invalid --set '{override}'. Expected PATH=VALUE.")
        value = yaml.safe_load(raw_value) if raw_value != "" else ""
        cursor = context
        segments = key.split(".")
        for segment in segments[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                existing = {}
                cursor[segment] = existing
            cursor = existing
        cursor[segments[-1]] = value

    return context


def _cmd_run(source: str, context_path: str | None, overrides: Sequence[str], explain: bool, pretty: bool) -> int:
    try:
        definition = load_system_definition(Path(source))
        context = _load_context(context_path, overrides)
        runtime = _build_runtime(definition)
        decision = runtime.explain(context) if explain else runtime.evaluate(context)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1

    print(json.dumps(decision, indent=2 if pretty else None))
    return 0 if decision.get("status") == "allow" else 2


def _cmd_new(path: str, template_name: str) -> int:
    try:
        project_dir = ProjectScaffold.generate(path, template_name)
    except (FileExistsError, ValueError) as exc:
        print(str(exc))
        return 1

    print(f"Created MORPH project at {project_dir} using template '{template_name}'.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "init":
        return _cmd_init(args.path)

    if args.command == "new":
        return _cmd_new(args.path, args.template)

    if args.command == "validate":
        return _cmd_validate(args.source)

    if args.command == "compile":
        return _cmd_compile(args.source, args.target, args.pretty)

    if args.command == "run":
        return _cmd_run(args.source, args.context, args.overrides, args.explain, args.pretty)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
