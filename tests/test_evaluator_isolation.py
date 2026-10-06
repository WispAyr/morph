"""The evaluator computes every gate itself and reads only the agent's in-boundary files."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

from evaluate_candidate import contained_workspace, evaluate_arm  # noqa: E402
from finalize_paired_run import finalize_pair  # noqa: E402
from run_paired_task import _snapshot  # noqa: E402
from score_agent_runs import score_runs  # noqa: E402

TASK_ID = "crosspoint-route-success-from-idle"
TASK = next(task for task in json.loads((ROOT / "benchmarks/agent-study/corpus.json").read_text())["tasks"] if task["task_id"] == TASK_ID)
DEFINITION = TASK["baseline_definition"]
BASELINE_TRANSITION = '{on: route_source.succeeded, from: "*", to: routed}'
FIXED_TRANSITION = "{on: route_source.succeeded, from: idle, to: routed}"


def _has_baseline_commit():
    return subprocess.run(["git", "cat-file", "-e", f"{TASK['baseline_commit']}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_baseline_commit(), reason="needs the full Git history for the pinned baseline commit")


@pytest.fixture
def reference(tmp_path):
    cases = [
        {"starting_state": "idle", "event": "route_source.succeeded", "baseline_state": "routed", "candidate_state": "routed"},
        {"starting_state": "faulted", "event": "route_source.succeeded", "baseline_state": "routed", "candidate_state": "faulted"},
        {"starting_state": "faulted", "event": "observed", "observed_fields": {"status": "ready"}, "baseline_state": "idle", "candidate_state": "idle"},
    ]
    path = tmp_path / "evaluator" / "reference.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"tasks": {TASK_ID: {"category": TASK["category"], "relationship": "narrower", "affected_subjects": [], "cases": cases}}}))
    return path


def _arm(pair_dir, arm="morph_mediated", *, edit=None):
    workspace = pair_dir / arm / "source"
    workspace.parent.mkdir(parents=True)
    _snapshot(TASK["baseline_commit"], workspace)
    if edit:
        edit(workspace)
    return {"workspace": str(workspace), "implementation_complete": True}


def _fix_transition(workspace):
    definition = workspace / DEFINITION
    definition.write_text(definition.read_text().replace(BASELINE_TRANSITION, FIXED_TRANSITION))


def test_a_correct_in_boundary_change_passes_every_gate(tmp_path, reference):
    pair_dir = tmp_path / "pair"
    run = _arm(pair_dir, edit=_fix_transition)

    result = evaluate_arm(TASK, pair_dir, "morph_mediated", run, reference, allow_development_fixtures=True)

    assert result["within_boundary"] and result["definition_valid"]
    assert result["changed_files"] == [DEFINITION]
    assert result["tests_passed"] and result["regression_tests_run"] > 100 and not result["regressions"]
    assert result["task_cases_passed"]
    assert result["invariants_status"] == "not_applicable" and result["simulation_passed"] is None
    assert result["implementation_complete"] is True
    assert result["agent_claimed_complete"] is True


def test_an_unchanged_workspace_fails_the_task_cases_even_if_the_agent_claims_completion(tmp_path, reference):
    pair_dir = tmp_path / "pair"
    run = _arm(pair_dir)

    result = evaluate_arm(TASK, pair_dir, "morph_mediated", run, reference, allow_development_fixtures=True)

    assert result["agent_claimed_complete"] is True
    assert result["task_cases_passed"] is False
    assert result["implementation_complete"] is False


def test_a_recorded_workspace_outside_the_pair_is_rejected(tmp_path, reference):
    pair_dir = tmp_path / "pair"
    _arm(pair_dir, edit=_fix_transition)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _snapshot(TASK["baseline_commit"], elsewhere / "source")
    _fix_transition(elsewhere / "source")

    with pytest.raises(ValueError, match="workspace must be"):
        evaluate_arm(TASK, pair_dir, "morph_mediated", {"workspace": str(elsewhere / "source")}, reference, allow_development_fixtures=True)
    with pytest.raises(ValueError, match="workspace must be"):
        contained_workspace(pair_dir, "morph_mediated", str(pair_dir / "direct_source" / "source"))


def test_a_workspace_reached_through_a_symbolic_link_is_rejected(tmp_path):
    pair_dir = tmp_path / "pair"
    target = tmp_path / "elsewhere"
    (target / "source").mkdir(parents=True)
    pair_dir.mkdir()
    os.symlink(target, pair_dir / "morph_mediated")

    with pytest.raises(ValueError, match="symbolic link"):
        contained_workspace(pair_dir, "morph_mediated", str(pair_dir / "morph_mediated" / "source"))


def test_a_symbolic_link_inside_the_workspace_is_rejected(tmp_path, reference):
    pair_dir = tmp_path / "pair"
    outside = tmp_path / "outside.yaml"
    outside.write_text("name: elsewhere\n")

    def link_definition(workspace):
        (workspace / DEFINITION).unlink()
        os.symlink(outside, workspace / DEFINITION)

    run = _arm(pair_dir, edit=link_definition)
    with pytest.raises(ValueError, match="symbolic link"):
        evaluate_arm(TASK, pair_dir, "morph_mediated", run, reference, allow_development_fixtures=True)


def test_changes_outside_the_boundary_are_reported_and_never_evaluated(tmp_path, reference):
    pair_dir = tmp_path / "pair"

    def tamper(workspace):
        _fix_transition(workspace)
        # Break the runtime and gut a regression test; neither may reach the evaluation tree.
        (workspace / "src/morph/runtime.py").write_text("raise ImportError('sabotaged')\n")
        (workspace / "tests/test_effects.py").write_text("def test_nothing():\n    assert True\n")
        (workspace / "tests/test_extra.py").write_text("def test_extra():\n    assert True\n")

    run = _arm(pair_dir, edit=tamper)
    result = evaluate_arm(TASK, pair_dir, "morph_mediated", run, reference, allow_development_fixtures=True)

    assert result["within_boundary"] is False
    assert result["outside_boundary"] == ["src/morph/runtime.py", "tests/test_effects.py", "tests/test_extra.py"]
    assert set(result["evaluated_files"]) <= set(TASK["implementation_boundary"])
    # The gates ran on the baseline runtime and the baseline tests, so they still pass ...
    assert result["tests_passed"] and result["task_cases_passed"]
    # ... but the run is not complete, because it changed files it was not allowed to.
    assert result["implementation_complete"] is False


def test_a_regression_in_baseline_tests_fails_the_tests_gate(tmp_path, reference):
    pair_dir = tmp_path / "pair"

    def break_routing(workspace):
        _fix_transition(workspace)
        definition = workspace / DEFINITION
        definition.write_text(definition.read_text().replace("source.latency_ms < 120 && destination.latency_ms < 120", "false"))

    run = _arm(pair_dir, edit=break_routing)
    result = evaluate_arm(TASK, pair_dir, "morph_mediated", run, reference, allow_development_fixtures=True)

    assert result["within_boundary"] and result["task_cases_passed"]
    assert result["tests_passed"] is False
    assert any("test_effects" in node or "test_crosspoint" in node for node in result["regressions"])
    assert result["implementation_complete"] is False


def test_evaluator_acceptance_tests_run_against_the_candidate(tmp_path, reference):
    acceptance = reference.parent / "test_route_scope.py"
    acceptance.write_text(
        "from pathlib import Path\n"
        "from morph import load_system_definition\n"
        "def test_route_success_is_scoped_to_idle():\n"
        f"    definition = load_system_definition(Path('{DEFINITION}'))\n"
        "    destination = next(e for e in definition.entities if e['name'] == 'destination')\n"
        "    route = next(t for t in destination['transitions'] if t.get('on', t.get(True)) == 'route_source.succeeded')\n"
        "    assert route['from'] == 'idle'\n"
    )
    data = json.loads(reference.read_text())
    data["tasks"][TASK_ID]["acceptance_tests"] = ["test_route_scope.py"]
    reference.write_text(json.dumps(data))

    fixed = evaluate_arm(TASK, tmp_path / "a", "morph_mediated", _arm(tmp_path / "a", edit=_fix_transition), reference, allow_development_fixtures=True)
    unchanged = evaluate_arm(TASK, tmp_path / "b", "morph_mediated", _arm(tmp_path / "b"), reference, allow_development_fixtures=True)

    assert fixed["acceptance_tests_run"] == 1 and fixed["tests_passed"] is True
    assert unchanged["acceptance_failures"] and unchanged["tests_passed"] is False


def test_finalized_records_carry_only_evaluator_gates_and_score(tmp_path, reference):
    import hashlib

    pair_dir = tmp_path / "pair"
    frozen = {"analysis": {"affected_subjects": [], "relationship": "narrower"}, "understanding": {}, "semantic_proposal": {}, "morph_analysis": {}}
    frozen_hash = hashlib.sha256((json.dumps(frozen, indent=2, sort_keys=True) + "\n").encode()).hexdigest()
    runs = {}
    for arm, edit in (("direct_source", None), ("morph_mediated", _fix_transition)):
        run = _arm(pair_dir, arm, edit=edit)
        runs[arm] = {
            **run, "pre_implementation": frozen, "pre_implementation_sha256": frozen_hash,
            "baseline_commit": TASK["baseline_commit"], "agent": {"provider": "p", "model": "m", "version": "1"},
            "elapsed_seconds": 1, "tests_passed": True, "task_cases_passed": True,
        }
    pair = {
        "task_id": TASK_ID, "pair_id": "pair-1", "evaluation_status": TASK["evaluation_status"],
        "prompt_sha256": hashlib.sha256((ROOT / TASK["prompt"]).read_bytes()).hexdigest(),
        "arm_order": ["direct_source", "morph_mediated"], "runs": runs,
    }
    (pair_dir / "pair.json").write_text(json.dumps(pair))

    records, evaluations = finalize_pair(pair_dir / "pair.json", reference, allow_development_fixtures=True)

    assert all("judge" not in record and "tests_passed" not in record for record in records)
    by_arm = {evaluation["arm"]: evaluation for evaluation in evaluations}
    # The direct arm claimed completion and reported passing gates, but changed nothing.
    assert by_arm["direct_source"]["task_cases_passed"] is False
    assert by_arm["morph_mediated"]["implementation_complete"] is True
    scored = score_runs(records, json.loads(reference.read_text()), evaluations)
    assert scored["by_arm"]["morph_mediated"]["task_cases_passed_rate"] == 1.0
    assert scored["by_arm"]["direct_source"]["task_cases_passed_rate"] == 0.0
