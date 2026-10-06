"""Judge a candidate MORPH definition against evaluator-only task cases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from morph import MORPHRuntime, MORPHSystem, load_system_definition  # noqa: E402
from morph.store import EventStore, stream_for  # noqa: E402


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_text(commit: str, definition_path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{definition_path}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _policy_context(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "id": "cam1",
            "status": "live",
            "latency_ms": case["source_latency_ms"],
        },
        "destination": {
            "id": "wall",
            "status": "ready",
            "latency_ms": case["destination_latency_ms"],
            "state": case["destination_state"],
        },
        "route": {
            "locked": case["route_locked"],
            **({"locked_by": case["locked_by"]} if "locked_by" in case else {}),
        },
        "operator": {"id": "ewan", "capabilities": ["route_control"]},
    }


def _policy_result(definition: Any, case: dict[str, Any]) -> str:
    runtime = MORPHRuntime(
        name=definition.name,
        version=definition.version,
        policies=definition.policies,
        capabilities=definition.capabilities,
        entities=definition.entities,
        actions=definition.actions,
    )
    return runtime.evaluate(_policy_context(case))["status"]


def _transition_result(definition: Any, case: dict[str, Any]) -> str:
    store = EventStore()
    entity_id = "wall"
    starting_state = case["starting_state"]
    store.append(
        stream_for("destination", entity_id),
        "transitioned",
        {"entity": "destination", "id": entity_id, "to": starting_state},
    )
    system = MORPHSystem(definition, store=store)
    event_kind = case["event"]
    fields = case.get("observed_fields") or {}
    if event_kind == "observed":
        system.observe("destination", entity_id, fields)
    else:
        event = {
            "kind": event_kind,
            "entity": "destination",
            "id": entity_id,
            "fields": fields,
        }
        context = {"destination": system.state("destination", entity_id)}
        system._transition(event_kind, context, event)
    return system.state("destination", entity_id).get("state", "")


def _case_result(definition: Any, case: dict[str, Any]) -> str:
    if "baseline_status" in case:
        return _policy_result(definition, case)
    if "starting_state" in case:
        return _transition_result(definition, case)
    raise ValueError(f"unsupported reference case: {case}")


def judge_candidate(
    task_id: str,
    candidate_path: Path,
    reference_path: Path,
    *,
    allow_development_fixture: bool = False,
) -> dict[str, Any]:
    corpus = _load_json(REPO_ROOT / "benchmarks/agent-study/corpus.json")
    reference = _load_json(reference_path)
    if not isinstance(corpus, dict) or not isinstance(corpus.get("tasks"), list):
        raise ValueError("corpus.tasks must be a list")
    if not isinstance(reference, dict) or not isinstance(reference.get("tasks"), dict):
        raise ValueError("reference.tasks must be an object keyed by task_id")
    tasks = {task["task_id"]: task for task in corpus["tasks"]}
    if task_id not in tasks:
        raise ValueError(f"unknown task '{task_id}'")
    task = tasks[task_id]
    if task.get("evaluation_status") != "frozen" and not allow_development_fixture:
        raise ValueError(f"task '{task_id}' is a development fixture; it cannot count as benchmark evidence")
    reference_task = reference.get("tasks", {}).get(task_id)
    if not isinstance(reference_task, dict):
        raise ValueError(f"reference has no task '{task_id}'")
    cases = reference_task.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"reference task '{task_id}' must declare non-empty cases")

    baseline = load_system_definition(_baseline_text(task["baseline_commit"], task["baseline_definition"]))
    candidate = load_system_definition(candidate_path)
    results = []
    for index, case in enumerate(cases):
        baseline_expected = case.get("baseline_status", case.get("baseline_state"))
        candidate_expected = case.get("candidate_status", case.get("candidate_state"))
        baseline_actual = _case_result(baseline, case)
        candidate_actual = _case_result(candidate, case)
        baseline_passed = baseline_actual == baseline_expected
        candidate_passed = candidate_actual == candidate_expected
        results.append({
            "case": index,
            "baseline_expected": baseline_expected,
            "baseline_actual": baseline_actual,
            "baseline_passed": baseline_passed,
            "candidate_expected": candidate_expected,
            "candidate_actual": candidate_actual,
            "candidate_passed": candidate_passed,
        })

    return {
        "task_id": task_id,
        "baseline_commit": task["baseline_commit"],
        "candidate": str(candidate_path),
        "case_count": len(results),
        "baseline_passed": all(result["baseline_passed"] for result in results),
        "task_cases_passed": all(result["candidate_passed"] for result in results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="Task identifier from the evaluator corpus")
    parser.add_argument("candidate", type=Path, help="Candidate MORPH YAML definition")
    parser.add_argument("--reference", type=Path, required=True, help="Evaluator-only reference JSON")
    parser.add_argument("--allow-development-fixture", action="store_true", help="Permit a known leaked fixture for local validation only")
    args = parser.parse_args()
    try:
        result = judge_candidate(
            args.task_id, args.candidate, args.reference,
            allow_development_fixture=args.allow_development_fixture,
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0 if result["baseline_passed"] and result["task_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
