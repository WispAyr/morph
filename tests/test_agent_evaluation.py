import copy
import json
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
        "human_interventions": 0,
        "elapsed_seconds": 30,
    }


def _evaluation(arm, *, pair_id="pilot-1", tests_passed=True):
    return {
        "pair_id": pair_id,
        "arm": arm,
        "implementation_complete": True,
        "tests_passed": tests_passed,
        "simulation_passed": True,
        "invariants_status": "not_applicable",
    }


def test_score_runs_compares_a_paired_task_by_arm():
    expected = ["policy:allow_live_route", "capability:route_control", "entity:source.latency_ms"]
    runs = [
        _run("direct_source", affected=expected[:2], tests_passed=False),
        _run("morph_mediated", affected=expected),
    ]

    result = score_runs(runs, {"tasks": {"pilot": {"affected_subjects": expected, "relationship": "broader"}}}, [_evaluation("direct_source", tests_passed=False), _evaluation("morph_mediated")])

    assert result["by_arm"]["morph_mediated"]["impact_f1_rate"] == 1.0
    assert result["by_arm"]["direct_source"]["impact_recall_rate"] == pytest.approx(2 / 3)
    assert result["by_arm"]["morph_mediated"]["tests_passed_rate"] == 1.0
    assert result["by_arm"]["direct_source"]["tests_passed_rate"] == 0.0
    assert result["mean_paired_deltas_morph_minus_direct"]["tests_passed"] == 1.0


def test_score_runs_rejects_incomplete_pairs():
    run = _run("morph_mediated", affected=[])
    with pytest.raises(ValueError, match="one run for each arm"):
        score_runs([run], {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}}, [_evaluation("morph_mediated")])


def test_score_runs_rejects_mismatched_models_in_a_pair():
    direct = _run("direct_source", affected=[])
    morph = _run("morph_mediated", affected=[])
    morph["agent"]["model"] = "different-agent"
    with pytest.raises(ValueError, match="same provider and model version"):
        score_runs([direct, morph], {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}}, [_evaluation("direct_source"), _evaluation("morph_mediated")])


def test_score_runs_flags_unknown_relationship_overclaim():
    direct = _run("direct_source", affected=[], relationship="broader")
    morph = _run("morph_mediated", affected=[], relationship="unknown")
    reference = {"tasks": {"pilot": {"affected_subjects": [], "relationship": "unknown"}}}

    result = score_runs([direct, morph], reference, [_evaluation("direct_source"), _evaluation("morph_mediated")])

    assert result["by_arm"]["direct_source"]["unknown_overclaims"] == 1
    assert result["by_arm"]["morph_mediated"]["unknown_overclaims"] == 0
    assert result["by_arm"]["morph_mediated"]["relationship_correct_rate"] == 1.0


def test_crosspoint_pilot_reference_matches_semantics_and_decisions():
    root = Path(__file__).parents[1]
    reference = json.loads((root / "benchmarks/agent-study/reference.json").read_text(encoding="utf-8"))
    task = reference["tasks"]["crosspoint-source-latency-150"]
    baseline = load_system_definition(root / "src/morph/examples/crosspoint.yaml")
    candidate_data = copy.deepcopy(baseline.to_dict())
    candidate_policy = next(policy for policy in candidate_data["policies"] if policy["name"] == "allow_live_route")
    candidate_policy["when"] = candidate_policy["when"].replace("source.latency_ms < 120", "source.latency_ms < 150")
    candidate = MORPHIR.from_dict(candidate_data)
    baseline_policy = next(policy for policy in baseline.policies if policy["name"] == "allow_live_route")

    analysis = SemanticReasoner().analyze(
        baseline_policy["when"],
        candidate_policy["when"],
        types=MORPHIR._shared_semantic_types(baseline, candidate),
    )
    assert analysis["relationship"] == task["relationship"]
    assert analysis["confidence"] == "proven"

    runtimes = [
        MORPHRuntime(name=model.name, version=model.version, policies=model.policies, capabilities=model.capabilities, entities=model.entities, actions=model.actions)
        for model in (baseline, candidate)
    ]
    for case in task["cases"]:
        context = {
            "source": {"id": "cam1", "status": "live", "latency_ms": case["source_latency_ms"]},
            "destination": {
                "id": "wall",
                "status": "ready",
                "latency_ms": case["destination_latency_ms"],
                "source": "cam0",
                "state": case["destination_state"],
            },
            "route": {"locked": case["route_locked"], "locked_by": case.get("locked_by", "")},
            "operator": {"id": "ewan", "capabilities": ["route_control"]},
        }
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