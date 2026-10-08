import copy
import json
import os
from pathlib import Path

import pytest

from benchmarks.score_agent_runs import score_runs
from benchmarks.finalize_paired_run import _simulation_passed, _validate_workspace
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


def _evaluation(arm, *, pair_id="pilot-1", tests_passed=True, semantic_relationship="broader"):
    return {
        "pair_id": pair_id,
        "arm": arm,
        "implementation_complete": True,
        "within_boundary": True,
        "structural_passed": True,
        "tests_passed": tests_passed,
        "semantic_relationship": semantic_relationship,
        "simulation_passed": True,
        "task_cases_passed": True,
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
    assert result["by_arm"]["morph_mediated"]["structural_passed_rate"] == 1.0
    assert result["by_arm"]["morph_mediated"]["semantic_relationship_correct_rate"] == 1.0
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

def test_score_runs_rejects_agent_supplied_judge_fields():
    run = _run("direct_source", affected=[])
    run["judge"] = {"implementation_complete": True}
    with pytest.raises(ValueError, match="evaluator-owned fields"):
        score_runs([run], {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}}, [])


def test_classify_equivalence_does_not_call_different_policies_identical_without_invariants():
    entities = [{"name": "source", "fields": {"status": "string"}}]
    baseline = MORPHIR.from_dict({
        "name": "x",
        "entities": entities,
        "policies": [{"name": "allow_route", "when": "source.status == 'live'"}],
    })
    candidate = MORPHIR.from_dict({
        "name": "x",
        "entities": entities,
        "policies": [{"name": "allow_route", "when": "source.status == 'faulted'"}],
    })
    assert baseline.structurally_equal(candidate) is False
    assert baseline.classify_equivalence(candidate) == "CONFLICTING"


def test_classify_equivalence_distinguishes_structural_and_semantic_equality():
    baseline = MORPHIR.from_dict({
        "name": "x",
        "policies": [{"name": "allow_route", "when": "source.latency_ms < 120"}],
    })
    candidate = MORPHIR.from_dict({
        "name": "x",
        "policies": [{"name": "allow_route", "when": "source.latency_ms <= 119"}],
    })
    assert baseline.structurally_equal(candidate) is False
    assert baseline.classify_equivalence(candidate) in {"EQUIVALENT", "UNKNOWN"}


def _classification_model(policies, invariants=None):
    return MORPHIR.from_dict({
        "name": "x",
        "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
        "invariants": invariants or [],
        "policies": policies,
    })


def _policy(name, when, action="ok"):
    return {"name": name, "when": when, "result": {"status": "allow", "action": action}}


def test_removing_one_policy_and_widening_another_is_overlapping():
    baseline = _classification_model([_policy("a", "source.latency_ms < 120"), _policy("b", "source.status == 'live'")])
    candidate = _classification_model([_policy("a", "source.latency_ms < 200")])
    # Slow live sources are no longer allowed and offline sources under 200 ms now are: each side allows something new.
    assert baseline.classify_equivalence(candidate) == "OVERLAPPING"
    assert baseline.propose(candidate)["proposal"] is not None


def test_a_candidate_may_add_actions_and_capabilities_and_is_compared_by_its_decisions():
    capability = {"description": "Route.", "requires": [], "inputs": {}, "outputs": {}, "failures": []}
    data = _classification_model([_policy("a", "source.latency_ms < 120", action="route")]).to_dict()
    data["capabilities"] = {"route": capability}
    data["actions"] = {"route": {"capability": "route", "inputs": {}}}
    baseline = MORPHIR.from_dict(data)
    grown = copy.deepcopy(data)
    grown["capabilities"]["notify"] = {**capability, "description": "Tell someone."}
    grown["actions"]["notify"] = {"capability": "notify", "inputs": {}}
    grown["policies"].append(_policy("b", "source.status == 'live'", action="notify"))
    assert baseline.classify_equivalence(grown) == "BROADER"

    changed = copy.deepcopy(grown)
    changed["capabilities"]["route"] = {**capability, "description": "changed"}
    assert baseline.classify_equivalence(changed) == "UNKNOWN", "changing an existing capability is not additive"


def test_reordering_policies_is_equivalent_only_when_no_decision_changes():
    first = _policy("a", "source.latency_ms < 120")
    second = _policy("b", "source.status == 'live'")
    assert _classification_model([first, second]).classify_equivalence(_classification_model([second, first])) == "EQUIVALENT"

    deny = {"name": "deny_slow", "when": "source.latency_ms >= 100", "result": {"status": "deny", "action": "reject"}}
    allow = _policy("allow_live", "source.status == 'live'")
    # Moving the allow above the overlapping deny lets slow live sources through.
    assert _classification_model([deny, allow]).classify_equivalence(_classification_model([allow, deny])) == "BROADER"


def test_a_narrower_deny_classifies_as_broader():
    deny = {"name": "deny_slow", "when": "source.latency_ms >= 100", "result": {"status": "deny", "action": "reject"}}
    allow = _policy("allow_live", "source.status == 'live'")
    relaxed = {**deny, "when": "source.latency_ms >= 150"}
    assert _classification_model([deny, allow]).classify_equivalence(_classification_model([relaxed, allow])) == "BROADER"
    assert _classification_model([relaxed, allow]).classify_equivalence(_classification_model([deny, allow])) == "NARROWER"


def test_classify_equivalence_is_unknown_when_a_policy_result_changes():
    baseline = _classification_model([_policy("a", "source.latency_ms < 120")])
    candidate = _classification_model([_policy("a", "source.latency_ms <= 119", action="other")])
    assert baseline.classify_equivalence(candidate) == "UNKNOWN"


def test_policy_changes_decide_the_classification_and_invariants_only_when_policies_are_unchanged():
    invariant = [{"name": "fast", "when": "source.latency_ms < 120"}]
    baseline = _classification_model([_policy("a", "source.latency_ms < 120")], invariant)
    broader_policy = _classification_model([_policy("a", "source.latency_ms < 200")], [{"name": "fast", "when": "source.latency_ms <= 119"}])
    tighter_spec = _classification_model([_policy("a", "source.latency_ms < 200")], [{"name": "fast", "when": "source.latency_ms < 60"}])
    invariant_only = _classification_model([_policy("a", "source.latency_ms < 120")], [{"name": "fast", "when": "source.latency_ms < 60"}])
    assert baseline.classify_equivalence(broader_policy) == "BROADER"
    # The invariants are the specification; what the system allows changed the same way.
    assert baseline.classify_equivalence(tighter_spec) == "BROADER"
    assert baseline.classify_equivalence(invariant_only) == "NARROWER"


def test_a_candidate_that_only_adds_fields_is_compared_over_its_inputs():
    baseline = _classification_model([_policy("a", "source.latency_ms < 120")])
    data = copy.deepcopy(baseline.to_dict())
    data["entities"][0]["fields"]["muted"] = "bool"
    data["policies"].insert(0, {"name": "deny_muted", "when": "has(source.muted) && source.muted", "result": {"status": "deny", "action": "reject"}})
    assert baseline.classify_equivalence(data) == "NARROWER"

    retyped = copy.deepcopy(baseline.to_dict())
    retyped["entities"][0]["fields"]["latency_ms"] = "double"
    assert baseline.classify_equivalence(retyped) == "UNKNOWN"


def test_score_runs_rejects_evaluations_without_a_run():
    runs = [_run("direct_source", affected=[]), _run("morph_mediated", affected=[])]
    evaluations = [_evaluation("direct_source"), _evaluation("morph_mediated"), _evaluation("morph_mediated", pair_id="other")]
    with pytest.raises(ValueError, match="do not match any run"):
        score_runs(runs, {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}}, evaluations)


def test_finalizer_rejects_workspace_outside_pair_directory(tmp_path):
    pair = tmp_path / "pair-1" / "pair.json"
    pair.parent.mkdir(parents=True)
    external_workspace = tmp_path / "unrelated" / "source"
    external_workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="inside this pair"):
        _validate_workspace(pair, "direct_source", str(external_workspace))


def test_finalizer_accepts_only_runner_workspace_for_arm(tmp_path):
    pair = tmp_path / "pair-1" / "pair.json"
    pair.parent.mkdir(parents=True)
    expected = pair.parent / "morph_mediated" / "source"
    expected.mkdir(parents=True)

    assert _validate_workspace(pair, "morph_mediated", str(expected)) == expected.resolve()


def test_evaluator_simulation_checks_expected_decisions_and_invariants():
    _, candidate = _latency_pilot_models()
    allowed = _crosspoint_context(100)
    denied = _crosspoint_context(130)
    scenarios = [
        {"context": allowed, "expected_status": "allow"},
        {"context": denied, "expected_status": "deny"},
    ]

    assert _simulation_passed(candidate, scenarios) is False
    scenarios[1]["expected_status"] = "allow"
    assert _simulation_passed(candidate, scenarios) is True


def test_score_runs_rejects_an_evaluation_for_a_different_task():
    runs = [_run("direct_source", affected=[]), _run("morph_mediated", affected=[])]
    evaluations = [_evaluation("direct_source"), {**_evaluation("morph_mediated"), "task_id": "other"}]
    with pytest.raises(ValueError, match="different task"):
        score_runs(runs, {"tasks": {"pilot": {"affected_subjects": [], "relationship": "broader"}}}, evaluations)


def test_scorer_cli_scores_a_single_pilot_task_as_partial(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).parents[1]
    corpus = root / "benchmarks/agent-study/corpus.json"
    tasks = json.loads(corpus.read_text())["tasks"]
    pilot = "crosspointd-screen-accepts-raw-sources"
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"tasks": {
        task["task_id"]: {"category": task["category"], "relationship": "broader", "affected_subjects": []} for task in tasks
    }}))
    runs = [{**_run(arm, affected=[]), "task_id": pilot} for arm in ("direct_source", "morph_mediated")]
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(run) + "\n" for run in runs))
    (tmp_path / "evaluations.jsonl").write_text("".join(json.dumps(_evaluation(arm)) + "\n" for arm in ("direct_source", "morph_mediated")))
    command = [sys.executable, str(root / "benchmarks/score_agent_runs.py"), str(tmp_path / "runs.jsonl"),
               "--evaluations", str(tmp_path / "evaluations.jsonl"), "--reference", str(reference), "--corpus", str(corpus)]

    whole = subprocess.run(command + ["--allow-development-fixtures"], capture_output=True, text=True)
    pilot_only = subprocess.run(command + ["--task", pilot], capture_output=True, text=True)

    assert whole.returncode != 0 and "coverage does not match corpus" in whole.stderr
    assert pilot_only.returncode == 0, pilot_only.stderr
    result = json.loads(pilot_only.stdout)
    assert result["corpus"] == {"tasks": 1, "task_ids": [pilot], "development_fixtures": [], "partial": True}


def test_reasoner_models_list_fields_exactly():
    types = {"source.kind": "string", "destination.accepts": "list"}
    reasoner = SemanticReasoner()

    assert reasoner.analyze("source.kind in destination.accepts", "size(destination.accepts) > 0", types=types)["relationship"] == "broader"
    assert reasoner.analyze("size(destination.accepts) == 0", "source.kind in destination.accepts", types=types)["relationship"] == "conflicting"
    overlap = reasoner.analyze("source.kind in destination.accepts", 'source.kind == "camera"', types=types)
    assert overlap["relationship"] == "overlapping" and overlap["confidence"] == "proven"


def test_the_screen_accepts_change_classifies_as_broader():
    # The example now carries the change (crosspoint #59, 8 Oct 2026): rebuild the model from BEFORE it and check that
    # going to the current model is classified as broader.
    path = Path(__file__).parents[1] / "src/morph/examples/crosspointd.yaml"
    current = load_system_definition(path).to_dict()
    policy_clause, invariant_clause = " && !(source.kind in destination.accepts)", " || source.kind in destination.accepts"
    before_data = copy.deepcopy(current)
    screen = next(policy for policy in before_data["policies"] if policy["name"] == "deny_screen_needs_layout")
    invariant = next(item for item in before_data["invariants"] if item["name"] == "screens_take_only_layouts")
    assert policy_clause in screen["when"] and invariant_clause in invariant["when"], "the example no longer carries the change"
    screen["when"] = screen["when"].replace(policy_clause, "")
    invariant["when"] = invariant["when"].replace(invariant_clause, "")
    before = MORPHIR.from_dict(before_data)

    assert before.classify_equivalence(copy.deepcopy(current)) == "BROADER"
    # Changing only the policy is broader too; the evaluator's simulation is what catches the stale invariant.
    policy_only = copy.deepcopy(before_data)
    next(policy for policy in policy_only["policies"] if policy["name"] == "deny_screen_needs_layout")["when"] += policy_clause
    assert before.classify_equivalence(policy_only) == "BROADER"
