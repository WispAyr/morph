"""Regression tests for deny-by-default behaviour of the runtime and its consumers."""

import pytest

from morph import (
    Compiler,
    ExecutionPlanner,
    MORPHRuntime,
    NodeTarget,
    PythonTarget,
    SQLTarget,
    StateMachine,
    WorkflowEngine,
)
from morph.validators import CapabilityValidator, PolicyValidator


def _allow_policy(name="allow", when=None, **extra):
    return {"name": name, "when": when or [], "result": {"status": "allow", "action": "ok"}, **extra}


def test_range_condition_checks_every_operator():
    runtime = MORPHRuntime.from_dict({"policies": [_allow_policy(when=[{"field": "v", "gte": 10, "lt": 120}])]})

    assert runtime.evaluate({"v": 50})["status"] == "allow"
    assert runtime.evaluate({"v": 5})["status"] == "deny"
    assert runtime.evaluate({"v": 500})["status"] == "deny"


def test_missing_or_null_field_denies_instead_of_raising():
    runtime = MORPHRuntime.from_dict(
        {"policies": [_allow_policy(when=[{"field": "destination.status", "equals": "ready"}])]}
    )

    assert runtime.evaluate({})["status"] == "deny"
    assert runtime.evaluate({"destination": {"status": None}})["status"] == "deny"
    assert runtime.evaluate({"destination": "ready"})["status"] == "deny"


def test_incomparable_types_deny_instead_of_raising():
    runtime = MORPHRuntime.from_dict({"policies": [_allow_policy(when=[{"field": "latency", "lt": 120}])]})

    assert runtime.evaluate({"latency": "fast"})["status"] == "deny"
    assert runtime.evaluate({"latency": 42})["status"] == "allow"


def test_contains_on_non_container_denies():
    runtime = MORPHRuntime.from_dict({"policies": [_allow_policy(when=[{"field": "caps", "contains": "x"}])]})

    assert runtime.evaluate({"caps": 7})["status"] == "deny"
    assert runtime.evaluate({"caps": ["x"]})["status"] == "allow"


def test_policy_result_cannot_spoof_audit_fields():
    runtime = MORPHRuntime.from_dict(
        {
            "name": "sys",
            "version": "1.0",
            "policies": [
                {
                    "name": "real",
                    "when": [],
                    "result": {"status": "allow", "action": "ok", "policy": "spoofed", "trace": ["spoofed"], "name": "x"},
                }
            ],
        }
    )

    decision = runtime.evaluate({})

    assert decision["policy"] == "real"
    assert decision["trace"] == ["real"]
    assert decision["name"] == "sys"
    assert decision["status"] == "allow"


def test_validator_rejects_condition_without_operator_or_field():
    assert PolicyValidator.validate(_allow_policy(when=[{"field": "x"}])) == [
        "policy.when[0] must declare at least one operator: ['equals', 'lt', 'lte', 'gt', 'gte', 'contains']"
    ]
    assert "policy.when[0].field is required and must be a non-empty string" in PolicyValidator.validate(
        _allow_policy(when=[{"field": "", "equals": 1}])
    )


def test_every_entry_point_validates_policies():
    bad = {"policies": [{"name": "p", "when": [{"field": "x", "bogus": 1}], "result": {"status": "allow", "action": "ok"}}]}

    with pytest.raises(ValueError):
        MORPHRuntime(name="n", version="v", policies=bad["policies"])
    with pytest.raises(ValueError):
        ExecutionPlanner.from_dict(bad)
    with pytest.raises(ValueError):
        WorkflowEngine.from_dict(bad)
    with pytest.raises(ValueError):
        Compiler.compile(bad)
    with pytest.raises(ValueError):
        StateMachine({"transitions": [{"from": "a", "on": "go", "when": [{"field": "x"}]}]})


def test_capability_definition_is_consulted():
    context = {"operator": {"capabilities": ["route_control"]}, "session": {"elevated": False}}

    assert CapabilityValidator.validate("route_control", context) == []
    assert CapabilityValidator.validate(
        "route_control", context, {"requires": ["session.elevated"]}
    ) == ["missing capability: route_control"]
    assert CapabilityValidator.validate(
        "route_control", {**context, "session": {"elevated": True}}, {"requires": ["session.elevated"]}
    ) == []


def test_runtime_uses_declared_capability_paths():
    runtime = MORPHRuntime.from_dict(
        {
            "capabilities": {"route_control": {"requires": ["session.grants"]}},
            "policies": [_allow_policy(requires=["route_control"])],
        }
    )

    # operator.capabilities alone is not enough once the capability declares its own path.
    assert runtime.evaluate({"operator": {"capabilities": ["route_control"]}})["status"] == "deny"
    assert runtime.evaluate({"session": {"grants": ["route_control"]}})["status"] == "allow"


IR = {
    "name": "studio_control",
    "version": "0.6.0",
    "policies": [
        {
            "name": "route_allowed",
            "requires": ["route_control"],
            "when": [
                {"field": "source.status", "equals": "live"},
                {"field": "destination.status", "equals": "ready"},
            ],
            "result": {"status": "allow", "action": "route_source"},
        }
    ],
}
GOOD = {"source": {"status": "live"}, "destination": {"status": "ready"}, "operator": {"capabilities": ["route_control"]}}
BAD_STATE = {"source": {"status": "offline"}, "destination": {"status": "faulted"}, "operator": {"capabilities": ["route_control"]}}
NO_CAP = {"source": {"status": "live"}, "destination": {"status": "ready"}, "operator": {"capabilities": []}}


@pytest.mark.parametrize("target_name,target_cls", [("python", PythonTarget), ("node", NodeTarget), ("sql", SQLTarget)])
def test_compiled_targets_evaluate_conditions_and_capabilities(target_name, target_cls):
    target = target_cls(Compiler.compile(IR, target=target_name))

    assert target.execute(GOOD)["status"] == "allow"
    assert target.execute(BAD_STATE)["status"] == "deny"
    assert target.execute(NO_CAP)["status"] == "deny"
    assert target.execute({})["status"] == "deny"


def test_compiler_rejects_unknown_target_before_doing_work():
    with pytest.raises(ValueError, match="Unknown target"):
        Compiler.compile(IR, target="cobol")


def test_sql_compiler_escapes_values_and_handles_empty_policies():
    hostile = {"policies": [_allow_policy(when=[{"field": "source.status", "equals": "x' OR '1'='1"}])]}
    compiled = Compiler.compile(hostile, target="sql")

    assert compiled["sql"] == "SELECT 'allow' AS status, 'ok' AS action WHERE source_status = 'x'' OR ''1''=''1';"
    assert compiled["statements"][0]["params"] == ["x' OR '1'='1"]

    empty = Compiler.compile({"name": "empty", "policies": []}, target="sql")
    assert empty["sql"] == ""
    assert empty["plan"] == []


def test_sql_compiler_renders_numeric_and_boolean_comparisons():
    compiled = Compiler.compile(
        {"policies": [_allow_policy(when=[{"field": "latency_ms", "lt": 120}, {"field": "route.locked", "equals": False}])]},
        target="sql",
    )

    assert compiled["sql"] == "SELECT 'allow' AS status, 'ok' AS action WHERE latency_ms < 120 AND route_locked = FALSE;"


def test_sql_compiler_refuses_contains():
    with pytest.raises(ValueError, match="cannot express"):
        Compiler.compile({"policies": [_allow_policy(when=[{"field": "caps", "contains": "x"}])]}, target="sql")


def test_state_machine_transition_without_result_is_not_domain_specific():
    machine = StateMachine({"transitions": [{"from": "idle", "on": "go", "to": "done"}]})

    decision = machine.evaluate({}, "go")

    assert decision == {"status": "allow", "action": "transition", "to": "done", "from": "idle", "event": "go"}


def test_workflow_reports_skipped_steps():
    engine = WorkflowEngine.from_dict(
        {
            "workflow": {
                "steps": [
                    {"name": "a", "when": [{"field": "x", "equals": 1}], "then": {"action": "do_a"}},
                    {"name": "b", "when": [{"field": "x", "equals": 2}], "then": {"action": "do_b"}},
                ]
            }
        }
    )

    result = engine.execute({"x": 1})

    assert result["plan"] == ["do_a"]
    assert result["steps_skipped"] == ["b"]
    assert engine.execute({"x": 3})["status"] == "deny"
