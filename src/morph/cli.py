from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

import yaml

from .compiler import Compiler
from .effects import EffectExecutor, load_adapters
from .ir import MORPHIR
from .loader import load_system_definition
from .project import ProjectScaffold
from .runtime import MORPHRuntime
from .store import EventStore
from .system import MORPHSystem
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

    inspect_parser = subparsers.add_parser("inspect", aliases=["explain"], help="Return a semantic summary of a MORPH system.")
    inspect_parser.add_argument("source", help="Path to the YAML file to inspect.")
    inspect_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    simulate_parser = subparsers.add_parser("simulate", help="Run a semantic safety simulation over example contexts.")
    simulate_parser.add_argument("source", help="Path to the YAML file to simulate.")
    simulate_parser.add_argument("--context", help="Path to a JSON/YAML file containing a single context object or a list of context objects.")
    simulate_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    diff_parser = subparsers.add_parser("diff", help="Compare two MORPH definitions: invariants preserved, added, or blocked; how what the system allows changes; and which policies now decide different inputs.")
    diff_parser.add_argument("baseline", help="Path to the original MORPH YAML file.")
    diff_parser.add_argument("candidate", help="Path to the proposed updated MORPH YAML file.")
    diff_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    diff_parser.add_argument("--allow-missing-fields", action="store_true",
                             help="Also consider inputs missing fields the baseline declares (by default every input carries them).")

    propose_parser = subparsers.add_parser("propose", help="Generate an AI-facing semantic proposal for a candidate mutation.")
    propose_parser.add_argument("baseline", help="Path to the original MORPH YAML file.")
    propose_parser.add_argument("candidate", help="Path to the proposed updated MORPH YAML file.")
    propose_parser.add_argument("--intent", help="Short human description of the change being proposed.")
    propose_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    impact_parser = subparsers.add_parser("impact", help="Explain what a semantic subject depends on and what it affects.")
    impact_parser.add_argument("source", help="Path to the MORPH YAML file to inspect.")
    impact_parser.add_argument("subject", help="An entity, field path, capability, action, policy, or invariant, bare or as kind:name, for example source.latency_ms or policy:deny_on_air.")
    impact_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    plan_parser = subparsers.add_parser("plan", aliases=["review"], help="Review a proposed semantic mutation and report whether it is safe to accept.")
    plan_parser.add_argument("baseline", help="Path to the original MORPH YAML file.")
    plan_parser.add_argument("candidate", help="Path to the proposed updated MORPH YAML file.")
    plan_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

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
    run_parser.add_argument(
        "--adapters",
        metavar="MODULE:ATTR",
        help="Execute the decision's effect through these adapters (a registry, dict, or factory), e.g. morph.examples.crosspoint:adapters.",
    )
    run_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    system_parser = subparsers.add_parser("system", help="Operate a durable, event-sourced system over a definition.")
    system_parser.add_argument("source", help="Path to the YAML definition.")
    system_parser.add_argument("--store", required=True, help="JSON Lines file holding the event log (created if missing).")
    system_sub = system_parser.add_subparsers(dest="system_command")

    observe_parser = system_sub.add_parser("observe", help="Record fields for an entity instance.")
    observe_parser.add_argument("entity")
    observe_parser.add_argument("id")
    observe_parser.add_argument("fields", nargs="*", metavar="FIELD=VALUE", help="Values are parsed as YAML scalars.")

    act_parser = system_sub.add_parser("act", help="Decide for the bound entity instances and execute the effect.")
    act_parser.add_argument("--adapters", required=True, metavar="MODULE:ATTR")
    act_parser.add_argument("bindings", nargs="*", metavar="ENTITY=ID")
    act_parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="PATH=VALUE", help="Extra context values.")
    act_parser.add_argument("--pretty", action="store_true")

    state_parser = system_sub.add_parser("state", help="Show the projection, one entity, or one instance.")
    state_parser.add_argument("entity", nargs="?")
    state_parser.add_argument("id", nargs="?")

    history_parser = system_sub.add_parser("history", help="Show recorded events.")
    history_parser.add_argument("--stream")
    history_parser.add_argument("--kind", action="append", dest="kinds")

    replay_parser = system_sub.add_parser("replay", help="Replay the event history for one entity or the whole system.")
    replay_parser.add_argument("entity", nargs="?")
    replay_parser.add_argument("id", nargs="?")

    provenance_parser = system_sub.add_parser("provenance", help="Explain the causal lineage behind an entity instance.")
    provenance_parser.add_argument("entity")
    provenance_parser.add_argument("id")

    explain_parser = system_sub.add_parser("explain", help="Summarize the causal story for an entity instance in plain English.")
    explain_parser.add_argument("entity")
    explain_parser.add_argument("id")

    review_parser = system_sub.add_parser("review", help="Produce the semantic review object for an entity instance including causal provenance, policy lineage, and approval metadata.")
    review_parser.add_argument("entity")
    review_parser.add_argument("id")
    review_parser.add_argument("--approver")
    review_parser.add_argument("--approved", action="store_true")

    compare_parser = system_sub.add_parser("compare", help="Compare the actual entity state against the counterfactual path without selected events.")
    compare_parser.add_argument("entity")
    compare_parser.add_argument("id")
    compare_parser.add_argument("--omit-kind", action="append", dest="omit_kinds", default=[], help="Event kind to omit from the alternate replay, e.g. transitioned.")

    counterfactual_parser = system_sub.add_parser("counterfactual", help="Rebuild the state without selected kinds of events to explore alternate outcomes.")
    counterfactual_parser.add_argument("entity")
    counterfactual_parser.add_argument("id")
    counterfactual_parser.add_argument("--omit-kind", action="append", dest="omit_kinds", default=[], help="Event kind to omit from the replay, e.g. transitioned.")

    return parser


def _parse_assignments(items: Sequence[str], label: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise ValueError(f"Invalid {label} '{item}'. Expected KEY=VALUE.")
        values[key] = yaml.safe_load(raw) if raw != "" else ""
    return values


def _cmd_system(args: argparse.Namespace) -> int:
    try:
        definition = load_system_definition(Path(args.source))
        store = EventStore(args.store)
        command = args.system_command

        if command == "observe":
            system = MORPHSystem(definition, store=store)
            event = system.observe(args.entity, args.id, _parse_assignments(args.fields, "field"))
            print(json.dumps({"event": event.to_dict(), "state": system.state(args.entity, args.id)}))
            return 0

        if command == "act":
            system = MORPHSystem(definition, load_adapters(args.adapters), store)
            bindings = {key: str(value) for key, value in _parse_assignments(args.bindings, "binding").items()}
            context = system.context(bindings, extra=_load_context(None, args.overrides))
            result = system.act(context)
            print(json.dumps(result.to_dict(), indent=2 if args.pretty else None))
            return 1 if result.status == "failed" else (0 if result.decision.get("status") == "allow" else 2)

        if command == "state":
            system = MORPHSystem(definition, store=store)
            if args.entity and args.id:
                print(json.dumps(system.state(args.entity, args.id)))
            elif args.entity:
                print(json.dumps(system.snapshot()["entities"].get(args.entity, {})))
            else:
                print(json.dumps(system.snapshot()))
            return 0

        if command == "history":
            system = MORPHSystem(definition, store=store)
            print(json.dumps(system.history(stream=args.stream, kinds=args.kinds)))
            return 0

        if command == "replay":
            system = MORPHSystem(definition, store=store)
            if args.entity is None:
                print(json.dumps(system.replay()))
            elif args.id is None:
                print(json.dumps(system.replay(args.entity)))
            else:
                print(json.dumps(system.replay(args.entity, args.id)))
            return 0

        if command == "provenance":
            system = MORPHSystem(definition, store=store)
            print(json.dumps(system.provenance(args.entity, args.id)))
            return 0

        if command == "explain":
            system = MORPHSystem(definition, store=store)
            print(json.dumps(system.explain(args.entity, args.id)))
            return 0

        if command == "review":
            system = MORPHSystem(definition, store=store)
            print(json.dumps(system.review(args.entity, args.id, approver=getattr(args, "approver", None), approved=getattr(args, "approved", False))))
            return 0

        if command == "compare":
            system = MORPHSystem(definition, store=store)
            omitted = set(args.omit_kinds) if args.omit_kinds else None
            print(json.dumps(system.explain_diff(args.entity, args.id, omit_kinds=omitted)))
            return 0

        if command == "counterfactual":
            system = MORPHSystem(definition, store=store)
            omitted = set(args.omit_kinds) if args.omit_kinds else None
            report = system.counterfactual(args.entity, args.id, omit_kinds=omitted)
            print(json.dumps(report))
            return 0
    except (OSError, ValueError, TypeError, ImportError, RuntimeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1

    print("usage: morph system <definition> --store <file> {observe,act,state,history,replay,provenance,explain,review,compare,counterfactual} ...")
    return 1


def _build_runtime(definition: MORPHIR) -> MORPHRuntime:
    return MORPHRuntime(
        name=definition.name,
        version=definition.version,
        policies=definition.policies,
        capabilities=definition.capabilities,
        entities=definition.entities,
        actions=definition.actions,
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
    if runtime.capability_specs:
        summary += f", {len(runtime.capability_specs)} capabilities, {len(runtime.action_specs)} actions"
    if workflow_steps:
        summary += f", {workflow_steps} workflow steps"
    if runtime.schema.empty:
        summary += " (no entity schema: field paths are unchecked)"
    print(summary)
    return 0


def _cmd_inspect(source: str, pretty: bool) -> int:
    try:
        definition = load_system_definition(Path(source))
        print(json.dumps(definition.inspect(), indent=2 if pretty else None))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


def _cmd_simulate(source: str, context_path: str | None, pretty: bool) -> int:
    try:
        definition = load_system_definition(Path(source))
        if context_path is None:
            raise ValueError("--context is required for simulation")
        payload = yaml.safe_load(Path(context_path).read_text(encoding="utf-8"))
        if payload is None:
            scenarios = []
        elif isinstance(payload, list):
            scenarios = payload
        elif isinstance(payload, dict):
            scenarios = [payload]
        else:
            raise ValueError("Simulation contexts must be a dict or list of dicts")
        result = definition.simulate(scenarios)
        print(json.dumps(result, indent=2 if pretty else None))
        return 0 if not result["failed"] else 1
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


def _cmd_diff(baseline: str, candidate: str, pretty: bool, allow_missing_fields: bool = False) -> int:
    try:
        base_ir = load_system_definition(Path(baseline))
        cand_ir = load_system_definition(Path(candidate))
        diff = base_ir.diff(cand_ir)
        diff["relationship"] = base_ir.classify_equivalence(cand_ir)
        diff["decisions"] = base_ir.decision_changes(cand_ir, complete_inputs=not allow_missing_fields)
        if diff["blocked"]:
            print(json.dumps(diff, indent=2 if pretty else None))
            return 1
        print(json.dumps(diff, indent=2 if pretty else None))
        return 0
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


def _cmd_plan(baseline: str, candidate: str, pretty: bool) -> int:
    try:
        base_ir = load_system_definition(Path(baseline))
        cand_ir = load_system_definition(Path(candidate))
        review = base_ir.plan_change(cand_ir)
        print(json.dumps(review, indent=2 if pretty else None))
        return 1 if review["status"] == "blocked" else 0
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


def _cmd_propose(baseline: str, candidate: str, intent: str | None, pretty: bool) -> int:
    try:
        base_ir = load_system_definition(Path(baseline))
        cand_ir = load_system_definition(Path(candidate))
        proposal = base_ir.propose(cand_ir, intent=intent)
        print(json.dumps(proposal, indent=2 if pretty else None))
        return 1 if proposal["status"] in {"blocked", "needs_review"} else 0
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


def _cmd_impact(source: str, subject: str, pretty: bool) -> int:
    try:
        definition = load_system_definition(Path(source))
        report = definition.impact(subject)
        print(json.dumps(report, indent=2 if pretty else None))
        return 0
    except KeyError as exc:
        print(f"error: {exc.args[0]}")
        return 1
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1


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


def _cmd_run(
    source: str,
    context_path: str | None,
    overrides: Sequence[str],
    explain: bool,
    adapters: str | None,
    pretty: bool,
) -> int:
    try:
        definition = load_system_definition(Path(source))
        context = _load_context(context_path, overrides)
        runtime = _build_runtime(definition)
        if adapters:
            executor = EffectExecutor(runtime, load_adapters(adapters))
            decision = runtime.explain(context) if explain else runtime.evaluate(context)
            result = executor.execute(decision, context)
            output: dict[str, Any] = result.to_dict()
            exit_code = 1 if result.status == "failed" else (0 if decision.get("status") == "allow" else 2)
        else:
            output = runtime.explain(context) if explain else runtime.evaluate(context)
            exit_code = 0 if output.get("status") == "allow" else 2
    except (OSError, ValueError, TypeError, ImportError, yaml.YAMLError) as exc:
        print(f"error: {exc}")
        return 1

    print(json.dumps(output, indent=2 if pretty else None))
    return exit_code


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

    if args.command in {"inspect", "explain"}:
        return _cmd_inspect(args.source, args.pretty)

    if args.command == "simulate":
        return _cmd_simulate(args.source, args.context, args.pretty)

    if args.command == "diff":
        return _cmd_diff(args.baseline, args.candidate, args.pretty, args.allow_missing_fields)

    if args.command == "propose":
        return _cmd_propose(args.baseline, args.candidate, args.intent, args.pretty)

    if args.command in {"plan", "review"}:
        return _cmd_plan(args.baseline, args.candidate, args.pretty)

    if args.command == "impact":
        return _cmd_impact(args.source, args.subject, args.pretty)

    if args.command == "compile":
        return _cmd_compile(args.source, args.target, args.pretty)

    if args.command == "run":
        return _cmd_run(args.source, args.context, args.overrides, args.explain, args.adapters, args.pretty)

    if args.command == "system":
        return _cmd_system(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
