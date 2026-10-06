"""Independently evaluate one completed arm of a paired run.

The evaluator trusts only the source repository, the corpus, and the private reference.
From the agent's workspace it reads the files inside the task's implementation boundary and
nothing else: every gate runs on a fresh tree exported from the pinned baseline commit with
those files copied over it. The agent's own Git history, test edits outside the boundary,
and claims about its work are never inputs to a gate.

Gates:

- ``within_boundary``: no file outside the implementation boundary differs from the baseline.
- ``definition_valid``: the candidate MORPH definition loads and validates.
- ``tests_passed``: every baseline test that passes on the baseline also passes on the
  candidate (test files inside the boundary are excluded, because the agent may change
  them), and any evaluator acceptance tests listed in the reference pass.
- ``task_cases_passed``: the evaluator-only definition cases (``judge_candidate``).
- ``simulation_passed``: the candidate's invariants hold over the reference scenarios.
- ``invariants_status``: whether the baseline's invariants are preserved.
- ``implementation_complete``: every applicable gate above passed.

Running candidate tests executes agent-written code. Run the evaluator inside a container
or VM that can reach only the pair directory and the evaluator's own files.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from judge_candidate import _policy_context, judge_candidate  # noqa: E402
from morph import MORPHIR, load_system_definition  # noqa: E402
from morph.reasoner import SemanticReasoner  # noqa: E402
from run_paired_task import EXCLUDED_AGENT_FILES  # noqa: E402

EVALUATOR_VERSION = 1
WORKSPACE_DIRECTORY = "source"
IGNORED_WORKSPACE_PARTS = {".git", "__pycache__", ".pytest_cache"}
TEST_TIMEOUT_SECONDS = 600


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contained_workspace(pair_dir: Path, arm: str, recorded: Any) -> Path:
    """Return the arm's workspace, refusing any path the runner did not create for it.

    The runner always places an arm's implementation workspace at ``<pair>/<arm>/source``.
    A recorded workspace anywhere else, or one reached through a symbolic link, is rejected
    so an adapter cannot point the evaluator at files outside the pair.
    """
    if not isinstance(recorded, str) or not recorded:
        raise ValueError(f"{arm} has no recorded workspace")
    pair_dir = pair_dir.resolve()
    expected = pair_dir / arm / WORKSPACE_DIRECTORY
    for path in (pair_dir / arm, expected):
        if path.is_symlink():
            raise ValueError(f"{arm} workspace path {path} is a symbolic link")
    if not expected.is_dir():
        raise ValueError(f"{arm} workspace {expected} does not exist")
    if Path(recorded).resolve() != expected.resolve() or expected.resolve() != expected:
        raise ValueError(f"{arm} workspace must be {expected}, not {recorded}")
    return expected


def _export_baseline(commit: str, destination: Path) -> dict[str, bytes]:
    """Extract the pinned commit into destination and return its files by relative path.

    Evaluator-only files are left out, exactly as the runner leaves them out of the agent's
    snapshot, so the baseline the agent saw and the baseline evaluated here are the same.
    """
    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit], cwd=REPO_ROOT,
        check=True, capture_output=True,
    ).stdout
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe path in baseline archive: {member.name}")
            if relative.as_posix() in EXCLUDED_AGENT_FILES:
                continue
            target = destination / Path(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"unable to read baseline archive member: {member.name}")
                data = source.read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                files[relative.as_posix()] = data
    return files


def _workspace_files(workspace: Path) -> dict[str, Path]:
    """Regular files in the workspace by relative path. Links and special files are refused."""
    files: dict[str, Path] = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if IGNORED_WORKSPACE_PARTS & set(relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"workspace contains a symbolic link: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"workspace contains a special file: {relative.as_posix()}")
        files[relative.as_posix()] = path
    return files


def _boundary_changes(baseline: dict[str, bytes], workspace: dict[str, Path], boundary: set[str]) -> tuple[list[str], list[str]]:
    changed = sorted(
        path for path in set(baseline) | set(workspace)
        if path not in baseline or path not in workspace or workspace[path].read_bytes() != baseline[path]
    )
    return changed, [path for path in changed if path not in boundary]


def _python_environment(tree: Path) -> dict[str, str]:
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if name in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(tree / "src"), str(tree)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _confirm_import_root(tree: Path) -> None:
    """Fail closed if the tree's own package is not the one its tests would import."""
    located = subprocess.run(
        [sys.executable, "-c", "import morph, sys; sys.stdout.write(morph.__file__)"],
        cwd=tree, env=_python_environment(tree), check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    if (tree / "src") not in Path(located).resolve().parents:
        raise ValueError(f"evaluation tree imports morph from {located}, not from {tree}")


def _run_tests(tree: Path, paths: list[str], report: Path) -> dict[str, str]:
    """Run pytest on paths inside tree and return each test's outcome by node id."""
    if not paths:
        return {}
    _confirm_import_root(tree)
    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *paths],
            cwd=tree, env=_python_environment(tree), check=False, capture_output=True, timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"<timeout>": "failed"}
    if not report.is_file():
        return {"<no report>": "failed"}
    outcomes: dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        node = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[node] = "failed"
        elif case.find("skipped") is not None:
            outcomes[node] = "skipped"
        else:
            outcomes[node] = "passed"
    return outcomes


def _regression_test_files(tree: Path, boundary: set[str]) -> list[str]:
    tests = tree / "tests"
    if not tests.is_dir():
        return []
    return sorted(
        path.relative_to(tree).as_posix() for path in tests.rglob("test_*.py")
        if path.relative_to(tree).as_posix() not in boundary
    )


def _invariants_status(baseline: MORPHIR, candidate: MORPHIR, allowed: set[str]) -> tuple[str, dict[str, str]]:
    """pass when every baseline invariant is kept or proven equivalent, fail when one is removed
    or proven different, unknown when a rewrite cannot be proven either way."""
    current = {item.get("name"): item.get("when") for item in baseline.invariants if isinstance(item, dict) and item.get("name")}
    if not current:
        return "not_applicable", {}
    proposed = {item.get("name"): item.get("when") for item in candidate.invariants if isinstance(item, dict) and item.get("name")}
    types = MORPHIR._shared_semantic_types(baseline, candidate)
    detail: dict[str, str] = {}
    for name, when in current.items():
        if name in allowed:
            detail[name] = "allowed_change"
        elif name not in proposed:
            detail[name] = "removed"
        elif proposed[name] == when:
            detail[name] = "unchanged"
        else:
            analysis = SemanticReasoner().analyze(when, proposed[name], types=types)
            if analysis["confidence"] != "proven":
                detail[name] = "unknown"
            else:
                detail[name] = "preserved" if analysis["relationship"] == "equivalent" else "changed"
    if any(value in {"removed", "changed"} for value in detail.values()):
        return "fail", detail
    if "unknown" in detail.values():
        return "unknown", detail
    return "pass", detail


def _scenarios(reference_task: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = reference_task.get("scenarios")
    if scenarios is not None:
        if not isinstance(scenarios, list) or not all(isinstance(item, dict) for item in scenarios):
            raise ValueError("reference scenarios must be a list of context objects")
        return scenarios
    return [_policy_context(case) for case in reference_task.get("cases", []) if isinstance(case, dict) and "baseline_status" in case]


def _acceptance_tests(reference_path: Path, reference_task: dict[str, Any], tree: Path) -> list[str]:
    """Copy the reference's evaluator-only acceptance tests into the tree and return their paths."""
    listed = reference_task.get("acceptance_tests") or []
    if not isinstance(listed, list) or not all(isinstance(item, str) and item for item in listed):
        raise ValueError("reference acceptance_tests must be a list of paths")
    root = reference_path.resolve().parent
    target_dir = tree / "tests" / "_evaluator_acceptance"
    copied = []
    for index, relative in enumerate(listed):
        source = (root / relative).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError(f"acceptance test '{relative}' is missing or outside the reference directory")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"test_acceptance_{index}_{source.name.removeprefix('test_')}"
        shutil.copyfile(source, target)
        copied.append(target.relative_to(tree).as_posix())
    return copied


def evaluate_arm(
    task: dict[str, Any],
    pair_dir: Path,
    arm: str,
    run: dict[str, Any],
    reference_path: Path,
    *,
    allow_development_fixtures: bool = False,
) -> dict[str, Any]:
    """Run every gate for one arm and return its evaluation record."""
    workspace = contained_workspace(pair_dir, arm, run.get("workspace"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    reference_task = reference.get("tasks", {}).get(task["task_id"]) if isinstance(reference, dict) else None
    if not isinstance(reference_task, dict):
        raise ValueError(f"reference has no task '{task['task_id']}'")
    boundary = set(task["implementation_boundary"])
    definition_path = task["baseline_definition"]
    if definition_path not in boundary:
        raise ValueError("the task's baseline definition must be inside its implementation boundary")

    with tempfile.TemporaryDirectory(prefix="morph-eval-") as scratch:
        scratch_path = Path(scratch)
        baseline_tree = scratch_path / "baseline"
        candidate_tree = scratch_path / "candidate"
        baseline_files = _export_baseline(task["baseline_commit"], baseline_tree)
        _export_baseline(task["baseline_commit"], candidate_tree)

        workspace_files = _workspace_files(workspace)
        changed, outside = _boundary_changes(baseline_files, workspace_files, boundary)
        evaluated_files: dict[str, str] = {}
        for relative in sorted(boundary):
            target = candidate_tree / relative
            if relative in workspace_files:
                data = workspace_files[relative].read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                evaluated_files[relative] = _sha256(data)
            elif target.exists():
                target.unlink()
                evaluated_files[relative] = "deleted"

        candidate_definition = candidate_tree / definition_path
        try:
            candidate = load_system_definition(candidate_definition)
            definition_error = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            candidate, definition_error = None, str(exc)
        baseline = load_system_definition(baseline_tree / definition_path)

        regression_files = _regression_test_files(baseline_tree, boundary)
        baseline_outcomes = _run_tests(baseline_tree, regression_files, scratch_path / "baseline.xml")
        candidate_outcomes = _run_tests(candidate_tree, regression_files, scratch_path / "candidate.xml")
        regressions = sorted(
            node for node, outcome in baseline_outcomes.items()
            if outcome == "passed" and candidate_outcomes.get(node) != "passed"
        )
        acceptance_paths = _acceptance_tests(reference_path, reference_task, candidate_tree)
        acceptance_outcomes = _run_tests(candidate_tree, acceptance_paths, scratch_path / "acceptance.xml")
        acceptance_failures = sorted(node for node, outcome in acceptance_outcomes.items() if outcome != "passed")
        if acceptance_paths and not acceptance_outcomes:
            acceptance_failures = ["<no acceptance tests ran>"]
        agent_test_files = sorted(path for path in boundary if path.startswith("tests/") and (candidate_tree / path).is_file())
        agent_outcomes = _run_tests(candidate_tree, agent_test_files, scratch_path / "agent.xml")

        if candidate is None:
            task_cases_passed = False
            simulation_passed: bool | None = False
            simulation_failures: list[Any] = ["definition does not load"]
            invariants_status, invariant_detail = "fail", {}
            semantic_relationship = "unknown"
        else:
            judged = judge_candidate(
                task["task_id"], candidate_definition, reference_path,
                allow_development_fixture=allow_development_fixtures,
            )
            if not judged["baseline_passed"]:
                raise ValueError(f"{arm} baseline fails the evaluator cases; resolve the evaluation setup before scoring")
            task_cases_passed = judged["task_cases_passed"]
            scenarios = _scenarios(reference_task)
            if scenarios and candidate.invariants:
                simulated = candidate.simulate(scenarios)
                simulation_failures = [{"index": item["index"], "invariants": item["invariants"]} for item in simulated["failed"]]
                simulation_passed = not simulation_failures
            else:
                simulation_passed, simulation_failures = None, []
            allowed = set(reference_task.get("allowed_invariant_changes") or [])
            invariants_status, invariant_detail = _invariants_status(baseline, candidate, allowed)
            semantic_relationship = baseline.classify_equivalence(candidate).lower()

    tests_passed = not regressions and not acceptance_failures and bool(baseline_outcomes or acceptance_paths)
    gates = [not outside, candidate is not None, tests_passed, task_cases_passed, simulation_passed is not False, invariants_status != "fail"]
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "task_id": task["task_id"],
        "arm": arm,
        "baseline_commit": task["baseline_commit"],
        "evaluated_files": evaluated_files,
        "changed_files": changed,
        "outside_boundary": outside,
        "within_boundary": not outside,
        "definition_valid": candidate is not None,
        "definition_error": definition_error,
        "tests_passed": tests_passed,
        "regressions": regressions,
        "regression_tests_run": len(baseline_outcomes),
        "acceptance_failures": acceptance_failures,
        "acceptance_tests_run": len(acceptance_outcomes),
        "agent_tests_passed": all(outcome == "passed" for outcome in agent_outcomes.values()) if agent_outcomes else None,
        "task_cases_passed": task_cases_passed,
        "simulation_passed": simulation_passed,
        "simulation_failures": simulation_failures,
        "invariants_status": invariants_status,
        "invariants": invariant_detail,
        "semantic_relationship": semantic_relationship,
        "relationship_matches_reference": semantic_relationship == reference_task.get("relationship"),
        "implementation_complete": all(gates),
        "agent_claimed_complete": run.get("implementation_complete") if isinstance(run.get("implementation_complete"), bool) else None,
    }
