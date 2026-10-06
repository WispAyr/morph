"""Validate and score paired direct-source and MORPH-mediated agent run records."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from statistics import fmean
from typing import Any

ARMS = {"direct_source", "morph_mediated"}
RELATIONSHIPS = {"equivalent", "narrower", "broader", "conflicting", "unknown"}
INVARIANT_STATUSES = {"pass", "fail", "unknown", "not_applicable"}
METRICS = (
    "impact_precision",
    "impact_recall",
    "impact_f1",
    "relationship_correct",
    "implementation_complete",
    "tests_passed",
    "simulation_passed",
    "task_cases_passed",
)


def _validate_run(run: Any, index: int) -> None:
    label = f"runs[{index}]"
    if not isinstance(run, dict):
        raise ValueError(f"{label} must be an object")
    for key in ("task_id", "pair_id", "arm"):
        if not isinstance(run.get(key), str) or not run[key]:
            raise ValueError(f"{label}.{key} must be a non-empty string")
    if run["arm"] not in ARMS:
        raise ValueError(f"{label}.arm must be one of {sorted(ARMS)}")
    agent = run.get("agent")
    if not isinstance(agent, dict) or any(not isinstance(agent.get(key), str) or not agent[key] for key in ("provider", "model", "version")):
        raise ValueError(f"{label}.agent must declare non-empty provider, model, and version strings")

    analysis = run.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError(f"{label}.analysis must be an object")
    affected = analysis.get("affected_subjects")
    if not isinstance(affected, list) or not all(isinstance(item, str) for item in affected):
        raise ValueError(f"{label}.analysis.affected_subjects must be a list of strings")
    if analysis.get("relationship") not in RELATIONSHIPS:
        raise ValueError(f"{label}.analysis.relationship must be one of {sorted(RELATIONSHIPS)}")

    judge = run.get("judge")
    if not isinstance(judge, dict):
        raise ValueError(f"{label}.judge must be an object")
    for key in ("implementation_complete", "tests_passed", "simulation_passed", "task_cases_passed"):
        if not isinstance(judge.get(key), bool):
            raise ValueError(f"{label}.judge.{key} must be a Boolean")
    if judge.get("invariants_status") not in INVARIANT_STATUSES:
        raise ValueError(f"{label}.judge.invariants_status must be one of {sorted(INVARIANT_STATUSES)}")

    if not isinstance(run.get("human_interventions"), int) or run["human_interventions"] < 0:
        raise ValueError(f"{label}.human_interventions must be a non-negative integer")
    elapsed = run.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        raise ValueError(f"{label}.elapsed_seconds must be a non-negative number")


def _impact_scores(predicted: set[str], expected: set[str]) -> tuple[float, float, float]:
    true_positives = len(predicted & expected)
    precision = true_positives / len(predicted) if predicted else float(not expected)
    recall = true_positives / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def score_runs(runs: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reference, dict):
        raise ValueError("reference must be a JSON object")
    tasks = reference.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("reference.tasks must be a non-empty object keyed by task_id")

    evaluated: list[dict[str, Any]] = []
    pair_arms: dict[str, dict[str, dict[str, Any]]] = {}
    for index, run in enumerate(runs):
        _validate_run(run, index)
        task_id = run["task_id"]
        task_reference = tasks.get(task_id)
        if not isinstance(task_reference, dict):
            raise ValueError(f"runs[{index}] references unknown task '{task_id}'")
        arm_runs = pair_arms.setdefault(run["pair_id"], {})
        if run["arm"] in arm_runs:
            raise ValueError(f"pair '{run['pair_id']}' has multiple '{run['arm']}' runs")
        arm_runs[run["arm"]] = run

        expected_subjects = set(task_reference.get("affected_subjects") or [])
        predicted_subjects = set(run["analysis"]["affected_subjects"])
        precision, recall, f1 = _impact_scores(predicted_subjects, expected_subjects)
        expected_relationship = task_reference.get("relationship")
        if expected_relationship not in RELATIONSHIPS:
            raise ValueError(f"reference task '{task_id}' has an invalid relationship")
        analysis = run["analysis"]
        judge = run["judge"]
        result = {
            "task_id": task_id,
            "pair_id": run["pair_id"],
            "arm": run["arm"],
            "impact_precision": precision,
            "impact_recall": recall,
            "impact_f1": f1,
            "relationship_correct": analysis["relationship"] == expected_relationship,
            "unknown_overclaim": expected_relationship == "unknown" and analysis["relationship"] != "unknown",
            "implementation_complete": judge["implementation_complete"],
            "tests_passed": judge["tests_passed"],
            "simulation_passed": judge["simulation_passed"],
            "task_cases_passed": judge["task_cases_passed"],
            "invariants_status": judge["invariants_status"],
            "human_interventions": run["human_interventions"],
            "elapsed_seconds": run["elapsed_seconds"],
        }
        evaluated.append(result)

    for pair_id, arm_runs in pair_arms.items():
        if set(arm_runs) != ARMS:
            raise ValueError(f"pair '{pair_id}' must contain one run for each arm")
        if arm_runs["direct_source"]["task_id"] != arm_runs["morph_mediated"]["task_id"]:
            raise ValueError(f"pair '{pair_id}' contains different tasks")
        direct_agent = arm_runs["direct_source"]["agent"]
        morph_agent = arm_runs["morph_mediated"]["agent"]
        identity = ("provider", "model", "version")
        if any(direct_agent[key] != morph_agent[key] for key in identity):
            raise ValueError(f"pair '{pair_id}' must use the same provider and model version in both arms")

    aggregates: dict[str, Any] = {}
    for arm in sorted(ARMS):
        arm_results = [result for result in evaluated if result["arm"] == arm]
        aggregates[arm] = {"runs": len(arm_results)}
        for metric in METRICS:
            aggregates[arm][metric + "_rate"] = fmean(float(result[metric]) for result in arm_results) if arm_results else 0.0
        applicable_invariants = [result for result in arm_results if result["invariants_status"] != "not_applicable"]
        aggregates[arm]["invariant_pass_rate"] = (
            fmean(float(result["invariants_status"] == "pass") for result in applicable_invariants)
            if applicable_invariants
            else None
        )
        aggregates[arm]["invariants_unknown"] = sum(result["invariants_status"] == "unknown" for result in arm_results)
        aggregates[arm]["unknown_overclaims"] = sum(result["unknown_overclaim"] for result in arm_results)
        aggregates[arm]["mean_human_interventions"] = fmean(result["human_interventions"] for result in arm_results) if arm_results else 0.0
        aggregates[arm]["mean_elapsed_seconds"] = fmean(result["elapsed_seconds"] for result in arm_results) if arm_results else 0.0

    paired_deltas = {
        metric: fmean(
            next(result for result in evaluated if result["pair_id"] == pair_id and result["arm"] == "morph_mediated")[metric]
            - next(result for result in evaluated if result["pair_id"] == pair_id and result["arm"] == "direct_source")[metric]
            for pair_id in pair_arms
        )
        if pair_arms
        else 0.0
        for metric in METRICS
    }
    return {"runs": evaluated, "by_arm": aggregates, "mean_paired_deltas_morph_minus_direct": paired_deltas}


def _validate_corpus(corpus: Any, reference: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
        raise ValueError("corpus.schema_version must be 1")
    if not isinstance(reference, dict):
        raise ValueError("reference must be a JSON object")
    entries = corpus.get("tasks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("corpus.tasks must be a non-empty list")

    indexed: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(entries):
        label = f"corpus.tasks[{index}]"
        if not isinstance(task, dict):
            raise ValueError(f"{label} must be an object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{label}.task_id must be a non-empty string")
        if task_id in indexed:
            raise ValueError(f"corpus contains duplicate task_id '{task_id}'")
        for key in ("category", "difficulty", "prompt", "baseline_definition"):
            if not isinstance(task.get(key), str) or not task[key]:
                raise ValueError(f"{label}.{key} must be a non-empty string")
        if task.get("evaluation_status") not in {"development_fixture", "frozen"}:
            raise ValueError(f"{label}.evaluation_status must be 'development_fixture' or 'frozen'")
        commit = task.get("baseline_commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{label}.baseline_commit must be a full lowercase Git commit SHA")
        boundary = task.get("implementation_boundary")
        if not isinstance(boundary, list) or not boundary or not all(isinstance(item, str) and item for item in boundary):
            raise ValueError(f"{label}.implementation_boundary must be a non-empty list of paths")
        indexed[task_id] = task

    reference_tasks = reference.get("tasks")
    if not isinstance(reference_tasks, dict):
        raise ValueError("reference.tasks must be an object keyed by task_id")
    if set(indexed) != set(reference_tasks):
        missing = sorted(set(indexed) - set(reference_tasks))
        extra = sorted(set(reference_tasks) - set(indexed))
        raise ValueError(f"corpus/reference task ids differ (missing reference: {missing}; missing corpus: {extra})")
    for task_id, task in indexed.items():
        label = f"corpus task '{task_id}'"
        reference_task = reference_tasks[task_id]
        if not isinstance(reference_task, dict):
            raise ValueError(f"reference task '{task_id}' must be an object")
        if reference_task.get("category") != task["category"]:
            raise ValueError(f"{label} category does not match the reference")
    return indexed


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    runs = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return runs


def _read_corpus(path: Path, reference: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        corpus = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}") from exc
    tasks = _validate_corpus(corpus, reference)
    repo_root = path.resolve().parents[2]
    for task_id, task in tasks.items():
        paths = [task["prompt"], task["baseline_definition"], *task["implementation_boundary"]]
        for relative in paths:
            candidate = (repo_root / relative).resolve()
            if repo_root not in candidate.parents or not candidate.is_file():
                raise ValueError(f"corpus task '{task_id}' references missing or unsafe path '{relative}'")
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=Path, help="JSON Lines file containing paired agent run records")
    parser.add_argument("--reference", type=Path, required=True, help="JSON reference labels keyed by task_id")
    parser.add_argument("--corpus", type=Path, required=True, help="Versioned task corpus index to validate against")
    parser.add_argument("--allow-development-fixtures", action="store_true", help="Allow explicitly marked fixtures; results are not benchmark evidence")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    try:
        reference = json.loads(args.reference.read_text(encoding="utf-8"))
        corpus_tasks = _read_corpus(args.corpus, reference)
        fixture_tasks = sorted(
            task_id for task_id, task in corpus_tasks.items()
            if task["evaluation_status"] == "development_fixture"
        )
        if fixture_tasks and not args.allow_development_fixtures:
            raise ValueError(
                f"corpus contains development fixtures {fixture_tasks}; pass --allow-development-fixtures for local validation only"
            )
        runs = _read_jsonl(args.runs)
        result = score_runs(runs, reference)
        covered = {run.get("task_id") for run in runs if isinstance(run, dict)}
        if covered != set(corpus_tasks):
            missing = sorted(set(corpus_tasks) - covered)
            extra = sorted(covered - set(corpus_tasks))
            raise ValueError(f"run task coverage does not match corpus (missing: {missing}; extra: {extra})")
        result["corpus"] = {
            "tasks": len(corpus_tasks),
            "task_ids": sorted(corpus_tasks),
            "development_fixtures": fixture_tasks,
        }
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
