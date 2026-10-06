"""Finalize one completed paired run with evaluator-produced gate outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from judge_candidate import judge_candidate  # noqa: E402

ARMS = ("direct_source", "morph_mediated")
INVARIANT_STATUSES = {"pass", "fail", "unknown", "not_applicable"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def finalize_pair(
    pair_path: Path,
    gates_path: Path,
    reference_path: Path,
    *,
    allow_development_fixtures: bool = False,
) -> list[dict[str, Any]]:
    pair = _read_json(pair_path)
    gates = _read_json(gates_path)
    if not isinstance(pair, dict) or not isinstance(pair.get("runs"), dict):
        raise ValueError("pair artifact must contain a runs object")
    if not isinstance(gates, dict):
        raise ValueError("gate results must be an object keyed by arm")
    if set(pair["runs"]) != set(ARMS) or set(gates) != set(ARMS):
        raise ValueError("pair artifact and gate results must each contain both paired arms")
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
    for arm in ARMS:
        run = pair["runs"][arm]
        gate = gates[arm]
        if not isinstance(run, dict) or not isinstance(gate, dict):
            raise ValueError(f"{arm} run and gate result must be objects")
        for key in ("tests_passed", "simulation_passed"):
            if not isinstance(gate.get(key), bool):
                raise ValueError(f"{arm}.{key} must be a Boolean independently measured outcome")
        invariants = gate.get("invariants_status")
        if invariants not in INVARIANT_STATUSES:
            raise ValueError(f"{arm}.invariants_status must be one of {sorted(INVARIANT_STATUSES)}")

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
        workspace = Path(run["workspace"]).resolve()
        candidate = (workspace / task_entry["baseline_definition"]).resolve()
        if workspace not in candidate.parents or not candidate.is_file():
            raise ValueError(f"{arm} candidate definition is missing or outside its workspace")
        judged = judge_candidate(
            task_id, candidate, reference_path,
            allow_development_fixture=allow_development_fixtures,
        )
        if not judged["baseline_passed"]:
            raise ValueError(f"{arm} baseline fails the evaluator cases; resolve the evaluation setup before scoring")
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
            "judge": {
                "implementation_complete": run.get("implementation_complete"),
                "tests_passed": gate["tests_passed"],
                "simulation_passed": gate["simulation_passed"],
                "task_cases_passed": judged["task_cases_passed"],
                "invariants_status": invariants,
            },
            "human_interventions": interventions,
            "elapsed_seconds": elapsed,
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pair", type=Path, help="Completed pair.json from run_paired_task.py")
    parser.add_argument("--gates", type=Path, required=True, help="Evaluator JSON with test, simulation, and invariant outcomes")
    parser.add_argument("--reference", type=Path, required=True, help="Private evaluator reference JSON")
    parser.add_argument("--output", type=Path, required=True, help="Output JSONL path outside agent-visible workspaces")
    parser.add_argument("--allow-development-fixtures", action="store_true", help="Finalize local development fixtures; not benchmark evidence")
    args = parser.parse_args()
    try:
        records = finalize_pair(
            args.pair, args.gates, args.reference,
            allow_development_fixtures=args.allow_development_fixtures,
        )
        output_path = args.output.resolve()
        pair_root = args.pair.resolve().parent
        if output_path == pair_root or pair_root in output_path.parents:
            raise ValueError("scorer output must be outside the pair directory and agent workspaces")
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent,
            prefix=f".{output_path.name}.", delete=False,
        ) as output:
            for record in records:
                output.write(json.dumps(record, sort_keys=True) + "\n")
            temporary_path = Path(output.name)
        os.replace(temporary_path, output_path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({"records_written": len(records), "output": str(output_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
