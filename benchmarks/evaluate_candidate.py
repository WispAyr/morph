"""Independently evaluate one completed arm of a paired run.

The evaluator trusts only the source repository, the corpus, the private reference, and the
private evaluator manifest (hidden tests, simulation scenarios, approved invariant changes).
From the agent's workspace it reads the files inside the task's implementation boundary and
nothing else: every gate runs on a fresh tree exported from the pinned baseline commit with
those files copied over it. The agent's own Git history, test edits outside the boundary,
and claims about its work are never inputs to a gate.

Gates:

- ``within_boundary``: no file outside the implementation boundary differs from the baseline.
- ``structural_passed``: the candidate MORPH definition loads, validates, and builds a runtime.
- ``tests_passed``: every baseline test that passes on the baseline also passes on the
  candidate (test files inside the boundary are excluded, because the agent may change
  them), and every hidden test in the manifest passes.
- ``task_cases_passed``: the evaluator-only definition cases (``judge_candidate``).
- ``simulation_passed``: every manifest scenario reaches its expected decision and the
  candidate's invariants hold in it.
- ``invariants_status``: whether the baseline's invariants are preserved.
- ``semantic_relationship``: how the candidate relates to the baseline, from the classifier.
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

from judge_candidate import judge_candidate  # noqa: E402
from morph import MORPHIR, MORPHRuntime, load_system_definition  # noqa: E402
from morph.reasoner import SemanticReasoner  # noqa: E402
from run_paired_task import EXCLUDED_AGENT_FILES  # noqa: E402

EVALUATOR_VERSION = 1
RELATIONSHIPS = {"equivalent", "narrower", "broader", "overlapping", "conflicting", "unknown"}
WORKSPACE_DIRECTORY = "source"
IGNORED_WORKSPACE_PARTS = {".git", "__pycache__", ".pytest_cache"}
DEFAULT_TEST_TIMEOUT_SECONDS = 900


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
    if Path(recorded).resolve() != expected.resolve() or expected.resolve() != expected:
        raise ValueError(f"{arm} workspace must be the runner-created source directory inside this pair ({expected}), not {recorded}")
    if not expected.is_dir():
        raise ValueError(f"{arm} workspace {expected} does not exist")
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
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT", "WINDIR") if name in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(tree / "src"), str(tree)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Hidden tests locate the candidate through this; it is the evaluation tree, never the agent's workspace.
    env["MORPH_CANDIDATE_WORKSPACE"] = str(tree)
    return env


def _confirm_import_root(tree: Path) -> None:
    """Fail closed if the tree's own package is not the one its tests would import."""
    located = subprocess.run(
        [sys.executable, "-c", "import morph, sys; sys.stdout.write(morph.__file__)"],
        cwd=tree, env=_python_environment(tree), check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    if (tree / "src") not in Path(located).resolve().parents:
        raise ValueError(f"evaluation tree imports morph from {located}, not from {tree}")


def _run_tests(tree: Path, paths: list[str], report: Path, timeout: int = DEFAULT_TEST_TIMEOUT_SECONDS) -> dict[str, str]:
    """Run pytest on paths inside tree and return each test's outcome by node id."""
    if not paths:
        return {}
    _confirm_import_root(tree)
    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *paths],
            cwd=tree, env=_python_environment(tree), check=False, capture_output=True, timeout=timeout,
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


def evaluator_task(manifest: Any, task_id: str) -> dict[str, Any]:
    """Validate and return one task's entry from the private evaluator manifest."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("evaluator config schema_version must be 1")
    tasks = manifest.get("tasks")
    config = tasks.get(task_id) if isinstance(tasks, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"evaluator config has no task '{task_id}'")
    tests = config.get("tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(item, str) and item for item in tests):
        raise ValueError(f"evaluator task '{task_id}' must declare hidden test files")
    scenarios = config.get("simulation_scenarios")
    if not isinstance(scenarios, list) or not scenarios or not all(
        isinstance(item, dict) and isinstance(item.get("context"), dict) and item.get("expected_status") in {"allow", "deny"}
        for item in scenarios
    ):
        raise ValueError(f"evaluator task '{task_id}' scenarios need context objects and expected_status allow/deny")
    approved = config.get("approved_invariant_changes", [])
    if not isinstance(approved, list) or not all(isinstance(name, str) for name in approved):
        raise ValueError(f"evaluator task '{task_id}' approved_invariant_changes must be a list of names")
    timeout = config.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"evaluator task '{task_id}' test_timeout_seconds must be a positive integer")
    return config


def simulation_passed(model: MORPHIR, scenarios: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    """Every scenario must reach its expected decision with all of the model's invariants holding."""
    result = model.simulate([scenario["context"] for scenario in scenarios])
    failures = []
    for outcome in sorted(result["passed"] + result["failed"], key=lambda item: item["index"]):
        expected = scenarios[outcome["index"]]["expected_status"]
        actual = outcome["decision"].get("status")
        if actual != expected or outcome["invariants"]:
            failures.append({"index": outcome["index"], "expected_status": expected, "status": actual, "invariants": outcome["invariants"]})
    complete = len(result["passed"]) + len(result["failed"]) == len(scenarios)
    return complete and not failures, failures


def _hidden_tests(evaluator_root: Path, config: dict[str, Any], tree: Path) -> list[str]:
    """Copy the manifest's hidden tests into the evaluation tree and return their paths."""
    root = evaluator_root.resolve()
    target_dir = tree / "tests" / "_evaluator_hidden"
    copied = []
    for index, relative in enumerate(config["tests"]):
        source = (root / relative).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError(f"evaluator test path is missing or outside private evaluator storage: {relative}")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"test_hidden_{index}_{source.name.removeprefix('test_')}"
        shutil.copyfile(source, target)
        copied.append(target.relative_to(tree).as_posix())
    return copied


def _structural_model(path: Path) -> MORPHIR:
    """Load, validate, and build a runtime for a definition, raising if any step fails."""
    model = load_system_definition(path).validate()
    MORPHRuntime(
        name=model.name, version=model.version, policies=model.policies,
        capabilities=model.capabilities, entities=model.entities, actions=model.actions,
    )
    return model


def evaluate_arm(
    task: dict[str, Any],
    pair_dir: Path,
    arm: str,
    run: dict[str, Any],
    reference_path: Path,
    evaluator_config: dict[str, Any],
    evaluator_root: Path,
    *,
    allow_development_fixtures: bool = False,
) -> dict[str, Any]:
    """Run every gate for one arm and return its evaluation record.

    ``evaluator_config`` is this task's validated manifest entry (see ``evaluator_task``) and
    ``evaluator_root`` the private directory its hidden test paths are relative to.
    """
    workspace = contained_workspace(pair_dir, arm, run.get("workspace"))
    timeout = evaluator_config.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS)
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
            candidate = _structural_model(candidate_definition)
            definition_error = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            candidate, definition_error = None, str(exc)
        baseline = _structural_model(baseline_tree / definition_path)

        regression_files = _regression_test_files(baseline_tree, boundary)
        baseline_outcomes = _run_tests(baseline_tree, regression_files, scratch_path / "baseline.xml", timeout)
        candidate_outcomes = _run_tests(candidate_tree, regression_files, scratch_path / "candidate.xml", timeout)
        regressions = sorted(
            node for node, outcome in baseline_outcomes.items()
            if outcome == "passed" and candidate_outcomes.get(node) != "passed"
        )
        hidden_paths = _hidden_tests(evaluator_root, evaluator_config, candidate_tree)
        hidden_outcomes = _run_tests(candidate_tree, hidden_paths, scratch_path / "hidden.xml", timeout)
        hidden_failures = sorted(node for node, outcome in hidden_outcomes.items() if outcome != "passed")
        if not hidden_outcomes:
            hidden_failures = ["<no hidden tests ran>"]
        agent_test_files = sorted(
            path for path in boundary
            if path.startswith("tests/") and PurePosixPath(path).name.startswith("test_") and path.endswith(".py")
            and (candidate_tree / path).is_file()
        )
        agent_outcomes = _run_tests(candidate_tree, agent_test_files, scratch_path / "agent.xml", timeout)

        if candidate is None:
            task_cases_passed = False
            simulated = False
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
            simulated, simulation_failures = simulation_passed(candidate, evaluator_config["simulation_scenarios"])
            allowed = set(evaluator_config.get("approved_invariant_changes") or [])
            invariants_status, invariant_detail = _invariants_status(baseline, candidate, allowed)
            classification = baseline.classify_equivalence(candidate).lower()
            classification = "equivalent" if classification == "identical" else classification
            semantic_relationship = classification if classification in RELATIONSHIPS else "unknown"

    tests_passed = not regressions and not hidden_failures and bool(baseline_outcomes)
    gates = [not outside, candidate is not None, tests_passed, task_cases_passed, simulated, invariants_status != "fail"]
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "task_id": task["task_id"],
        "arm": arm,
        "baseline_commit": task["baseline_commit"],
        "evaluated_files": evaluated_files,
        "changed_files": changed,
        "outside_boundary": outside,
        "within_boundary": not outside,
        "structural_passed": candidate is not None,
        "definition_error": definition_error,
        "tests_passed": tests_passed,
        "regressions": regressions,
        "regression_tests_run": len(baseline_outcomes),
        "hidden_test_failures": hidden_failures,
        "hidden_tests_run": len(hidden_outcomes),
        "agent_tests_passed": all(outcome == "passed" for outcome in agent_outcomes.values()) if agent_outcomes else None,
        "task_cases_passed": task_cases_passed,
        "simulation_passed": simulated,
        "simulation_failures": simulation_failures,
        "invariants_status": invariants_status,
        "invariants": invariant_detail,
        "semantic_relationship": semantic_relationship,
        "relationship_matches_reference": semantic_relationship == reference_task.get("relationship"),
        "implementation_complete": all(gates),
        "agent_claimed_complete": run.get("implementation_complete") if isinstance(run.get("implementation_complete"), bool) else None,
    }
