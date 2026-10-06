"""Finalize one completed paired run with evaluator-produced gate outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from judge_candidate import _baseline_text, judge_candidate  # noqa: E402
from morph import MORPHRuntime, load_system_definition  # noqa: E402

ARMS = ("direct_source", "morph_mediated")
RELATIONSHIPS = {"equivalent", "narrower", "broader", "conflicting", "unknown"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_workspace(pair_path: Path, arm: str, workspace_path: str) -> Path:
    pair_root = pair_path.resolve().parent
    workspace = Path(workspace_path).resolve()
    expected = (pair_root / arm / "source").resolve()
    if workspace != expected or pair_root not in workspace.parents:
        raise ValueError(f"{arm} workspace must be the runner-created source directory inside this pair")
    return workspace


def _simulation_passed(model: Any, scenarios: list[dict[str, Any]]) -> bool:
    result = model.simulate([scenario["context"] for scenario in scenarios])
    outcomes = result["passed"] + result["failed"]
    decisions_match = all(
        outcome["decision"].get("status") == scenarios[outcome["index"]]["expected_status"]
        for outcome in outcomes
    ) and len(outcomes) == len(scenarios)
    return decisions_match and not result["failed"]


def _run_evaluator_gates(
    task_id: str,
    task_entry: dict[str, Any],
    workspace: Path,
    evaluator_config: dict[str, Any],
    reference_path: Path,
    *,
    allow_development_fixtures: bool,
) -> dict[str, Any]:
    """Run trusted hidden tests and MORPH checks from evaluator-owned configuration."""
    tasks = evaluator_config.get("tasks")
    config = tasks.get(task_id) if isinstance(tasks, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"evaluator config has no task '{task_id}'")
    tests = config.get("tests")
    scenarios = config.get("simulation_scenarios")
    if not isinstance(tests, list) or not tests:
        raise ValueError(f"evaluator task '{task_id}' must declare hidden test files")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(f"evaluator task '{task_id}' must declare simulation scenarios")
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("context"), dict)
        and item.get("expected_status") in {"allow", "deny"}
        for item in scenarios
    ):
        raise ValueError(f"evaluator task '{task_id}' scenarios need context objects and expected_status allow/deny")
    approved = config.get("approved_invariant_changes", [])
    if not isinstance(approved, list) or not all(isinstance(name, str) for name in approved):
        raise ValueError(f"evaluator task '{task_id}' approved_invariant_changes must be a list of names")
    evaluator_root = evaluator_config["_root"].resolve()
    test_files = []
    for item in tests:
        if not isinstance(item, str) or not item:
            raise ValueError(f"evaluator task '{task_id}' test paths must be strings")
        path = (evaluator_root / item).resolve()
        if evaluator_root not in path.parents or not path.is_file():
            raise ValueError(f"evaluator test path is missing or outside private evaluator storage: {item}")
        test_files.append(path)

    candidate_path = workspace / task_entry["baseline_definition"]
    structural_passed = False
    candidate = None
    baseline = None
    invariant_status = "unknown"
    simulation_passed = False
    task_cases_passed = False
    semantic_relationship = "unknown"
    try:
        candidate = load_system_definition(candidate_path).validate()
        baseline = load_system_definition(_baseline_text(task_entry["baseline_commit"], task_entry["baseline_definition"])).validate()
        for model in (baseline, candidate):
            MORPHRuntime(
                name=model.name, version=model.version, policies=model.policies,
                capabilities=model.capabilities, entities=model.entities, actions=model.actions,
            )
        structural_passed = True
        classification = baseline.classify_equivalence(candidate).lower()
        if classification == "identical":
            classification = "equivalent"
        if classification not in RELATIONSHIPS:
            classification = "unknown"
        semantic_relationship = classification

        invariant_diff = baseline.diff(candidate)
        blocked = set(invariant_diff["blocked"])
        invariant_names = {item.get("name") for item in baseline.invariants if isinstance(item, dict)}
        if not invariant_names:
            invariant_status = "not_applicable"
        else:
            invariant_status = "pass" if blocked.issubset(set(approved)) else "fail"
        simulation_passed = _simulation_passed(candidate, scenarios)
        judged = judge_candidate(
            task_id, candidate_path, reference_path,
            allow_development_fixture=allow_development_fixtures,
        )
        task_cases_passed = judged["baseline_passed"] and judged["task_cases_passed"]
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError):
        pass

    tests_passed = False
    test_elapsed = 0.0
    test_timeout = config.get("test_timeout_seconds", 900)
    if not isinstance(test_timeout, int) or test_timeout <= 0:
        raise ValueError(f"evaluator task '{task_id}' test_timeout_seconds must be a positive integer")
    if structural_passed and (workspace / "src").is_dir():
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL")
            if key in os.environ
        }
        env["PYTHONPATH"] = str(workspace / "src")
        env["MORPH_CANDIDATE_WORKSPACE"] = str(workspace)
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *map(str, test_files)],
                cwd=workspace, env=env, capture_output=True, text=True,
                timeout=test_timeout, check=False,
            )
            tests_passed = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            tests_passed = False
        test_elapsed = round(time.monotonic() - started, 3)
    return {
        "structural_passed": structural_passed,
        "tests_passed": tests_passed,
        "simulation_passed": simulation_passed,
        "task_cases_passed": task_cases_passed,
        "invariants_status": invariant_status,
        "semantic_relationship": semantic_relationship,
        "test_elapsed_seconds": test_elapsed,
    }


def finalize_pair(
    pair_path: Path,
    evaluator_config_path: Path,
    reference_path: Path,
    *,
    allow_development_fixtures: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the agent run records and the independent evaluation records for one pair.

    The scorer rejects run records that carry evaluator-owned fields, so gate outcomes are
    kept in their own records keyed by pair and arm.
    """
    pair_root = pair_path.resolve().parent
    for private_path in (evaluator_config_path.resolve(), reference_path.resolve()):
        if private_path == REPO_ROOT or REPO_ROOT in private_path.parents:
            raise ValueError("evaluator configuration and reference must be outside the source repository")
        if private_path == pair_root or pair_root in private_path.parents:
            raise ValueError("evaluator configuration and reference must be outside agent workspaces")
    pair = _read_json(pair_path)
    evaluator_config = _read_json(evaluator_config_path)
    if not isinstance(pair, dict) or not isinstance(pair.get("runs"), dict):
        raise ValueError("pair artifact must contain a runs object")
    if not isinstance(evaluator_config, dict):
        raise ValueError("evaluator config must be an object")
    if evaluator_config.get("schema_version") != 1:
        raise ValueError("evaluator config schema_version must be 1")
    evaluator_config["_root"] = evaluator_config_path.resolve().parent
    if set(pair["runs"]) != set(ARMS):
        raise ValueError("pair artifact must contain both paired arms")
    task_id = pair.get("task_id")
    pair_id = pair.get("pair_id")
    if not isinstance(task_id, str) or not isinstance(pair_id, str):
        raise ValueError("pair artifact must declare task_id and pair_id")

    corpus = _read_json(REPO_ROOT / "benchmarks/agent-study/corpus.json")
    if not isinstance(corpus, dict) or not isinstance(corpus.get("tasks"), list):
        raise ValueError("corpus.tasks must be a list")
    task_entry = next((item for item in corpus["tasks"] if item.get("task_id") == task_id), None)
    if task_entry is None:
        raise ValueError(f"unknown task '{task_id}'")
    if pair.get("evaluation_status") != task_entry.get("evaluation_status"):
        raise ValueError("pair evaluation_status does not match the current corpus")
    if pair.get("prompt_sha256") != hashlib.sha256((REPO_ROOT / task_entry["prompt"]).read_bytes()).hexdigest():
        raise ValueError("pair prompt hash does not match the current corpus prompt")
    if pair.get("arm_order") not in (list(ARMS), list(reversed(ARMS))):
        raise ValueError("pair arm_order must contain both arms exactly once")
    records = []
    evaluations = []
    for arm in ARMS:
        run = pair["runs"][arm]
        if not isinstance(run, dict):
            raise ValueError(f"{arm} run must be an object")

        frozen = run.get("pre_implementation")
        analysis = frozen.get("analysis") if isinstance(frozen, dict) else None
        if not isinstance(analysis, dict):
            raise ValueError(f"{arm} has no frozen analysis artifact")
        canonical_artifact = json.dumps(frozen, indent=2, sort_keys=True) + "\n"
        artifact_hash = hashlib.sha256(canonical_artifact.encode("utf-8")).hexdigest()
        if artifact_hash != run.get("pre_implementation_sha256"):
            raise ValueError(f"{arm} frozen pre-implementation artifact hash does not match")
        if run.get("baseline_commit") != task_entry["baseline_commit"]:
            raise ValueError(f"{arm} baseline commit does not match the corpus")
        if task_entry.get("evaluation_status") != "frozen" and not allow_development_fixtures:
            raise ValueError(f"task '{task_id}' is a development fixture")
        workspace = _validate_workspace(pair_path, arm, run.get("workspace", ""))
        candidate = (workspace / task_entry["baseline_definition"]).resolve()
        if workspace not in candidate.parents or not candidate.is_file():
            raise ValueError(f"{arm} candidate definition is missing or outside its workspace")
        evaluation = _run_evaluator_gates(
            task_id, task_entry, workspace, evaluator_config, reference_path,
            allow_development_fixtures=allow_development_fixtures,
        )
        if not isinstance(run.get("implementation_complete"), bool):
            raise ValueError(f"{arm}.implementation_complete must be a Boolean")
        agent = run.get("agent")
        if not isinstance(agent, dict) or any(not isinstance(agent.get(key), str) or not agent[key] for key in ("provider", "model", "version")):
            raise ValueError(f"{arm}.agent must declare provider, model, and version")
        interventions = run.get("human_interventions", 0)
        elapsed = run.get("elapsed_seconds")
        if not isinstance(interventions, int) or isinstance(interventions, bool) or interventions < 0:
            raise ValueError(f"{arm}.human_interventions must be a non-negative integer")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
            raise ValueError(f"{arm}.elapsed_seconds must be a non-negative number")
        records.append({
            "task_id": task_id,
            "pair_id": pair_id,
            "arm": arm,
            "agent": agent,
            "pre_implementation_understanding": frozen["understanding"],
            "semantic_proposal": frozen["semantic_proposal"],
            "morph_analysis": frozen["morph_analysis"],
            "analysis": analysis,
            "patch": run.get("patch", ""),
            "human_interventions": interventions,
            "elapsed_seconds": elapsed,
        })
        evaluations.append({
            "task_id": task_id,
            "pair_id": pair_id,
            "arm": arm,
            "implementation_complete": run.get("implementation_complete"),
            **evaluation,
        })
    return records, evaluations


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as output:
        for record in records:
            output.write(json.dumps(record, sort_keys=True) + "\n")
        temporary_path = Path(output.name)
    os.replace(temporary_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pair", type=Path, help="Completed pair.json from run_paired_task.py")
    parser.add_argument("--evaluator-config", type=Path, required=True, help="Private evaluator manifest with hidden tests and scenarios")
    parser.add_argument("--reference", type=Path, required=True, help="Private evaluator reference JSON")
    parser.add_argument("--output", type=Path, required=True, help="Run records JSONL path outside agent-visible workspaces")
    parser.add_argument("--evaluations-output", type=Path, required=True, help="Independent evaluation JSONL path outside agent-visible workspaces")
    parser.add_argument("--allow-development-fixtures", action="store_true", help="Finalize local development fixtures; not benchmark evidence")
    args = parser.parse_args()
    try:
        output_path = args.output.resolve()
        evaluations_path = args.evaluations_output.resolve()
        evaluator_config_path = args.evaluator_config.resolve()
        reference_path = args.reference.resolve()
        if output_path == evaluations_path:
            raise ValueError("run records and evaluations must be written to different files")
        pair_root = args.pair.resolve().parent
        private_roots = {evaluator_config_path.parent, reference_path.parent}
        for path in (output_path, evaluations_path):
            if path == pair_root or pair_root in path.parents:
                raise ValueError("scorer output must be outside the pair directory and agent workspaces")
            if path == REPO_ROOT or REPO_ROOT in path.parents:
                raise ValueError("scorer output must be outside the source repository")
            if any(path == root or root in path.parents for root in private_roots):
                raise ValueError("scorer output must be outside private evaluator storage")
            if path in {evaluator_config_path, reference_path}:
                raise ValueError("scorer output cannot overwrite evaluator inputs")
        records, evaluations = finalize_pair(
            args.pair, evaluator_config_path, reference_path,
            allow_development_fixtures=args.allow_development_fixtures,
        )
        _write_jsonl(output_path, records)
        _write_jsonl(evaluations_path, evaluations)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "records_written": len(records),
        "output": str(output_path),
        "evaluations_written": len(evaluations),
        "evaluations_output": str(evaluations_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
