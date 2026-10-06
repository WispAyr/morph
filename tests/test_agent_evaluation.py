import copy
import json
import os
from pathlib import Path

import pytest

from benchmarks.score_agent_runs import score_runs
from morph import MORPHIR, MORPHRuntime, MORPHSystem, SemanticReasoner, load_system_definition


def _run(arm, *, affected, relationship="broader", tests_passed=True):
    return {
        "task_id": "pilot",
        "pair_id": "pilot-1",
        "arm": arm,
        "agent": {"provider": "test", "model": "same-agent", "version": "1"},
        "analysis": {"affected_subjects": affected, "relationship": relationship},
        "judge": {
            "implementation_complete": True,
            "tests_passed": tests_passed,
            "simulation_passed": True,
            "task_cases_passed": True,
            "invariants_status": "not_applicable",
        },
        "human_interventions": 0,
        "elapsed_seconds": 30,
    }


def test_score_runs_compares_a_paired_task_by_arm():
    expected = ["policy:allow_live_route", "capability:route_control", "entity:source.latency_ms"]
    runs = [
        _run("direct_source", affected=expected[:2], tests_passed=False),
        _run("morph_mediated", affected=expected),
    ]

    result = score_runs(runs, {"tasks": {"pilot": {"affected_subjects": expected, "relationship": "broader"}}})

    assert result["by_arm"]["morph_mediated"]["impact_f1_rate"] == 1.0
    assert result["by_arm"]["direct_source"]["impact_recall_rate"] == pytest.approx(2 / 3)
    assert result["by_arm"]["morph_mediated"]["tests_passed_rate"] == 1.0
    assert result["by_arm"]["direct_source"]["tests_passed_rate"] == 0.0
    assert result["mean_paired_deltas_morph_minus_direct"]["tests_passed"] == 1.0


def test_score_runs_rejects_incomplete_pairs():
    run = _run("morph_mediated", affected=[])
    with pytest.raises(ValueError, match="one run for each arm"):
        score_runs([run], {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}})


def test_score_runs_rejects_mismatched_models_in_a_pair():
    direct = _run("direct_source", affected=[])
    morph = _run("morph_mediated", affected=[])
    morph["agent"]["model"] = "different-agent"
    with pytest.raises(ValueError, match="same provider and model version"):
        score_runs([direct, morph], {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}})


def test_score_runs_flags_unknown_relationship_overclaim():
    direct = _run("direct_source", affected=[], relationship="broader")
    morph = _run("morph_mediated", affected=[], relationship="unknown")
    reference = {"tasks": {"pilot": {"affected_subjects": [], "relationship": "unknown"}}}

    result = score_runs([direct, morph], reference)

    assert result["by_arm"]["direct_source"]["unknown_overclaims"] == 1
    assert result["by_arm"]["morph_mediated"]["unknown_overclaims"] == 0
    assert result["by_arm"]["morph_mediated"]["relationship_correct_rate"] == 1.0


ROOT = Path(__file__).parents[1]
# The evaluator reference is deliberately kept outside the repository. Point this variable
# at the private copy to also check the pilot against its labelled cases.
REFERENCE_ENV = "MORPH_EVALUATOR_REFERENCE"


def _latency_pilot_models():
    baseline = load_system_definition(ROOT / "src/morph/examples/crosspoint.yaml")
    candidate_data = copy.deepcopy(baseline.to_dict())
    candidate_policy = next(policy for policy in candidate_data["policies"] if policy["name"] == "allow_live_route")
    candidate_policy["when"] = candidate_policy["when"].replace("source.latency_ms < 120", "source.latency_ms < 150")
    return baseline, MORPHIR.from_dict(candidate_data)


def _runtime(model):
    return MORPHRuntime(
        name=model.name,
        version=model.version,
        policies=model.policies,
        capabilities=model.capabilities,
        entities=model.entities,
        actions=model.actions,
    )


def _crosspoint_context(source_latency_ms, *, destination_latency_ms=60, destination_state="idle", route_locked=False, locked_by=""):
    return {
        "source": {"id": "cam1", "status": "live", "latency_ms": source_latency_ms},
        "destination": {
            "id": "wall",
            "status": "ready",
            "latency_ms": destination_latency_ms,
            "source": "cam0",
            "state": destination_state,
        },
        "route": {"locked": route_locked, "locked_by": locked_by},
        "operator": {"id": "ewan", "capabilities": ["route_control"]},
    }


def test_crosspoint_latency_pilot_is_a_proven_broadening():
    baseline, candidate = _latency_pilot_models()
    baseline_when, candidate_when = (
        next(p["when"] for p in model.policies if p["name"] == "allow_live_route") for model in (baseline, candidate)
    )

    analysis = SemanticReasoner().analyze(
        baseline_when,
        candidate_when,
        types=MORPHIR._shared_semantic_types(baseline, candidate),
    )
    assert analysis["relationship"] == "broader"
    assert analysis["confidence"] == "proven"

    baseline_runtime, candidate_runtime = _runtime(baseline), _runtime(candidate)
    # Inside the old bound both allow, in the new band only the candidate allows, and the
    # guard policies still deny in both.
    assert [r.evaluate(_crosspoint_context(100))["status"] for r in (baseline_runtime, candidate_runtime)] == ["allow", "allow"]
    assert [r.evaluate(_crosspoint_context(130))["status"] for r in (baseline_runtime, candidate_runtime)] == ["deny", "allow"]
    assert [r.evaluate(_crosspoint_context(150))["status"] for r in (baseline_runtime, candidate_runtime)] == ["deny", "deny"]
    faulted = _crosspoint_context(100, destination_state="faulted")
    assert [r.evaluate(faulted)["policy"] for r in (baseline_runtime, candidate_runtime)] == ["deny_faulted_destination"] * 2
    locked = _crosspoint_context(100, route_locked=True, locked_by="someone_else")
    assert [r.evaluate(locked)["policy"] for r in (baseline_runtime, candidate_runtime)] == ["deny_locked_route"] * 2

    system = MORPHSystem(
        candidate,
        {
            "route_control": lambda inputs: {"route_id": "route-1", "previous_source": "cam0"},
            "notify": lambda inputs: {},
        },
    )
    execution = system.act(_crosspoint_context(149))
    assert execution.status == "executed"
    assert system.state("destination", "wall")["state"] == "routed"


def test_crosspoint_pilot_reference_matches_semantics_and_decisions():
    reference_path = os.environ.get(REFERENCE_ENV)
    if not reference_path or not Path(reference_path).is_file():
        pytest.skip(f"set {REFERENCE_ENV} to the private evaluator reference to check labelled cases")
    reference = json.loads(Path(reference_path).read_text(encoding="utf-8"))
    task = reference["tasks"]["crosspoint-source-latency-150"]
    baseline, candidate = _latency_pilot_models()
    baseline_policy = next(policy for policy in baseline.policies if policy["name"] == "allow_live_route")
    candidate_policy = next(policy for policy in candidate.policies if policy["name"] == "allow_live_route")

    analysis = SemanticReasoner().analyze(
        baseline_policy["when"],
        candidate_policy["when"],
        types=MORPHIR._shared_semantic_types(baseline, candidate),
    )
    assert analysis["relationship"] == task["relationship"]
    assert analysis["confidence"] == "proven"

    runtimes = [_runtime(model) for model in (baseline, candidate)]
    for case in task["cases"]:
        context = _crosspoint_context(
            case["source_latency_ms"],
            destination_latency_ms=case["destination_latency_ms"],
            destination_state=case["destination_state"],
            route_locked=case["route_locked"],
            locked_by=case.get("locked_by", ""),
        )
        expected = (case["baseline_status"], case["candidate_status"])
        actual = tuple(runtime.evaluate(context)["status"] for runtime in runtimes)
        assert actual == expected

    system = MORPHSystem(
        candidate,
        {
            "route_control": lambda inputs: {"route_id": "route-1", "previous_source": "cam0"},
            "notify": lambda inputs: {},
        },
    )
    execution = system.act(
        {
            "source": {"id": "cam1", "status": "live", "latency_ms": 149},
            "destination": {"id": "wall", "status": "ready", "latency_ms": 60, "source": "cam0", "state": "idle"},
            "route": {"locked": False, "locked_by": ""},
            "operator": {"id": "ewan", "capabilities": ["route_control"]},
        }
    )
    assert execution.status == "executed"
    assert system.state("destination", "wall")["state"] == "routed"