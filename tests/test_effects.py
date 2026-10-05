import json

import pytest

from morph import (
    AdapterRegistry,
    Compiler,
    EffectExecutor,
    EffectFailure,
    EffectLog,
    MORPHRuntime,
    PythonTarget,
    load_adapters,
    load_system_definition,
)
from morph.cli import main
from morph.examples.crosspoint import DEFINITION_PATH, FakeNotifier, FakeRouter, build, load_runtime

LIVE = {
    "source": {"id": "cam1", "status": "live", "latency_ms": 40},
    "destination": {"id": "wall", "status": "ready", "latency_ms": 60},
    "operator": {"id": "ewan", "capabilities": ["route_control"]},
}


def _ctx(**overrides):
    context = {key: dict(value) for key, value in LIVE.items()}
    for key, value in overrides.items():
        context.setdefault(key, {}).update(value)
    return context


# --- the Crosspoint example end to end --------------------------------------------------


def test_allowed_route_is_applied_on_the_router():
    executor, router, notifier = build()

    result = executor.run(LIVE)

    assert result.status == "executed"
    assert result.decision["policy"] == "allow_live_route"
    assert result.inputs == {"source": "cam1", "destination": "wall", "operator": "ewan"}
    assert result.outputs == {"route_id": "cam1->wall", "previous_source": ""}
    assert router.routes == {"wall": "cam1"}
    assert notifier.messages == []
    assert [record.status for record in executor.log.records()] == ["succeeded"]


def test_denied_route_notifies_instead_of_routing():
    executor, router, notifier = build()
    context = _ctx(route={"locked": True, "locked_by": "someone_else"})

    result = executor.run(context)

    assert result.decision["status"] == "deny"
    assert result.decision["policy"] == "deny_locked_route"
    assert result.status == "executed"
    assert result.capability == "notify"
    assert notifier.messages == [{"operator": "ewan", "message": "route cam1 -> wall blocked"}]
    assert router.routes == {}


def test_lock_owner_may_still_route():
    executor, router, _ = build()

    result = executor.run(_ctx(route={"locked": True, "locked_by": "ewan"}))

    assert result.status == "executed"
    assert router.routes == {"wall": "cam1"}


def test_repeat_of_same_route_is_idempotent():
    executor, router, _ = build()

    first = executor.run(LIVE)
    second = executor.run(LIVE)

    assert first.status == "executed"
    assert second.status == "skipped"
    assert second.outputs == first.outputs
    assert len(router.calls) == 1
    assert [record.status for record in executor.log.records()] == ["succeeded", "skipped"]


def test_retryable_failure_is_retried_then_succeeds():
    executor, router, _ = build(router=FakeRouter(busy_for=1))

    result = executor.run(LIVE)

    assert result.status == "executed"
    assert result.attempts == 2
    assert [record.status for record in result.records] == ["failed", "succeeded"]
    assert result.records[0].error["code"] == "router_busy"
    assert result.records[0].error["declared"] is True


def test_retries_are_bounded_by_the_capability():
    executor, router, _ = build(router=FakeRouter(busy_for=10))

    result = executor.run(LIVE)

    assert result.status == "failed"
    assert result.attempts == 3  # retries: 2
    assert result.error["code"] == "router_busy"
    assert len(router.calls) == 3


def test_non_retryable_failure_stops_immediately():
    router = FakeRouter()
    router.lock("wall", "someone_else")
    executor, _, _ = build(router=router)

    result = executor.run(LIVE)

    assert result.status == "failed"
    assert result.attempts == 1
    assert result.error["code"] == "destination_locked"
    assert result.error["details"] == {"locked_by": "someone_else"}


def test_ungranted_capability_is_refused_at_execution_even_if_policy_forgot_requires():
    definition = {
        "entities": [{"name": "operator", "fields": {"capabilities": "list"}}],
        "capabilities": {"route_control": {"requires": ["operator.capabilities"], "inputs": {}, "outputs": {}}},
        "actions": {"route_source": {"capability": "route_control", "inputs": {}}},
        "policies": [{"name": "careless_allow", "when": "true", "result": {"status": "allow", "action": "route_source"}}],
    }
    calls = []
    executor = EffectExecutor(MORPHRuntime.from_dict(definition), {"route_control": lambda inputs: calls.append(inputs) or {}})

    result = executor.run({"operator": {"capabilities": []}})

    assert result.decision["status"] == "allow"
    assert result.status == "denied"
    assert result.error["code"] == "capability_not_granted"
    assert calls == []


def test_input_expression_failure_does_not_reach_the_adapter():
    executor, router, _ = build()
    context = _ctx()
    del context["destination"]["id"]

    result = executor.run(context)

    assert result.status == "failed"
    assert result.error["code"] == "invalid_inputs"
    assert "destination" in result.error["message"]
    assert router.calls == []


def test_mistyped_input_is_rejected_before_the_adapter():
    definition = {
        "capabilities": {"c": {"requires": [], "inputs": {"n": "int"}, "outputs": {}}},
        "actions": {"a": {"capability": "c", "inputs": {"n": "value"}}},
        "policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "a"}}],
    }
    calls = []
    executor = EffectExecutor(MORPHRuntime.from_dict(definition), {"c": lambda inputs: calls.append(inputs) or {}})

    result = executor.run({"value": "not an int"})

    assert result.status == "failed"
    assert result.error["message"] == "inputs.n must be int, got string"
    assert calls == []


def test_invalid_outputs_are_a_failure():
    definition = {
        "capabilities": {"c": {"requires": [], "inputs": {}, "outputs": {"id": "string"}}},
        "actions": {"a": {"capability": "c", "inputs": {}}},
        "policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "a"}}],
    }
    executor = EffectExecutor(MORPHRuntime.from_dict(definition), {"c": lambda inputs: {"id": 42}})

    result = executor.run({})

    assert result.status == "failed"
    assert result.error["code"] == "invalid_outputs"


def test_adapter_exceptions_become_failures_not_crashes():
    definition = {
        "capabilities": {"c": {"requires": [], "inputs": {}, "outputs": {}}},
        "actions": {"a": {"capability": "c", "inputs": {}}},
        "policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "a"}}],
    }

    def broken(inputs):
        raise RuntimeError("boom")

    result = EffectExecutor(MORPHRuntime.from_dict(definition), {"c": broken}).run({})

    assert result.status == "failed"
    assert result.error == {"code": "adapter_error", "message": "RuntimeError: boom", "retryable": False}


def test_unbound_deny_action_is_logged_without_effect():
    runtime = MORPHRuntime.from_dict({"policies": [{"name": "p", "when": "false", "result": {"status": "allow", "action": "x"}}]})
    executor = EffectExecutor(runtime, {})

    result = executor.run({})

    assert result.status == "denied"
    assert result.decision["reason"] == "no_matching_policy"
    assert executor.log.records()[0].status == "denied"


def test_strict_executor_requires_an_adapter_per_capability():
    with pytest.raises(ValueError, match="No adapter registered for capabilities: \\['notify'\\]"):
        EffectExecutor(load_runtime(), {"route_control": lambda inputs: {}})

    executor = EffectExecutor(load_runtime(), {"route_control": lambda inputs: {}}, strict=False)
    result = executor.run(_ctx(route={"locked": True, "locked_by": "x"}))
    assert result.status == "failed"
    assert result.error["code"] == "adapter_missing"


def test_effect_log_sink_receives_every_record():
    seen = []
    executor, _, _ = build(log=EffectLog(sink=seen.append))

    executor.run(LIVE)
    executor.run(LIVE)

    assert [record.status for record in seen] == ["succeeded", "skipped"]
    assert json.dumps(executor.log.to_list())  # records are JSON-serialisable


# --- definition validation -------------------------------------------------------------


def _definition(**overrides):
    base = {
        "entities": [{"name": "source", "fields": {"id": "string"}}],
        "capabilities": {"c": {"requires": [], "inputs": {"id": "string"}, "outputs": {}}},
        "actions": {"a": {"capability": "c", "inputs": {"id": "source.id"}}},
        "policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "a"}}],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"capabilities": {"c": {"inputs": {"id": "text"}}}}, "unknown type 'text'"),
        ({"capabilities": {"c": {"bogus": 1}}}, "unsupported keys: \\['bogus'\\]"),
        ({"capabilities": {"c": {"inputs": {"id": "string"}, "idempotency": ["nope"]}}}, "unknown input 'nope'"),
        ({"capabilities": {"c": {"retries": -1}}}, "retries must be a non-negative integer"),
        ({"actions": {"a": {"capability": "missing", "inputs": {}}}}, "unknown capability 'missing'"),
        ({"actions": {"a": {"capability": "c", "inputs": {}}}}, "does not bind inputs \\['id'\\]"),
        ({"actions": {"a": {"capability": "c", "inputs": {"id": "source.id", "extra": "1"}}}}, "binds inputs \\['extra'\\]"),
        ({"actions": {"a": {"capability": "c", "inputs": {"id": "source.idd"}}}}, "unknown field 'source.idd'"),
        ({"actions": {"a": {"capability": "c", "inputs": {"id": 42}}}}, "literal must be string, got int"),
        ({"actions": {"a": {"capability": "c", "inputs": {"id": "source.id =="}}}}, "Invalid expression"),
        ({"policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "unbound"}}]}, "not bound in actions"),
        ({"policies": [{"name": "p", "requires": ["ghost"], "when": "true", "result": {"status": "allow", "action": "a"}}]}, "undeclared capability 'ghost'"),
    ],
)
def test_definition_errors_are_reported_at_load(overrides, message):
    with pytest.raises(ValueError, match=message):
        MORPHRuntime.from_dict(_definition(**overrides))


def test_deny_actions_need_not_be_bound():
    definition = _definition(policies=[{"name": "p", "when": "true", "result": {"status": "deny", "action": "raise_alert"}}])

    runtime = MORPHRuntime.from_dict(definition)

    assert runtime.evaluate({"source": {"id": "x"}})["status"] == "deny"


def test_definitions_without_actions_stay_decision_only():
    runtime = MORPHRuntime.from_dict({"policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "anything"}}]})

    assert runtime.evaluate({})["status"] == "allow"
    assert EffectExecutor(runtime, {}).run({}).status == "unbound"


def test_explain_shows_the_binding():
    report = load_runtime().explain(LIVE)

    assert report["binding"] == {
        "capability": "route_control",
        "inputs": {"source": "source.id", "destination": "destination.id", "operator": "operator.id"},
    }


def test_compiled_plan_carries_capabilities_and_actions():
    compiled = Compiler.compile(load_system_definition(DEFINITION_PATH).to_dict())

    assert compiled["capabilities"]["route_control"]["idempotency"] == ["source", "destination"]
    assert compiled["actions"]["route_source"]["capability"] == "route_control"
    target = PythonTarget(compiled)
    executor = EffectExecutor(target.runtime, build()[0].adapters)
    assert executor.run(LIVE).status == "executed"


def test_adapter_registry_coercion_and_loading():
    registry = load_adapters("morph.examples.crosspoint:adapters")
    assert registry.names() == ["notify", "route_control"]
    assert AdapterRegistry.coerce(registry) is registry
    assert AdapterRegistry.coerce(lambda: {"x": lambda i: {}}).names() == ["x"]

    with pytest.raises(ValueError, match="must look like"):
        load_adapters("nonsense")
    with pytest.raises(ValueError, match="has no attribute"):
        load_adapters("morph.examples.crosspoint:nope")
    with pytest.raises(TypeError):
        AdapterRegistry({"x": "not callable"})


def test_cli_run_with_adapters_executes_effect(tmp_path, capsys):
    context = tmp_path / "ctx.json"
    context.write_text(json.dumps(LIVE), encoding="utf-8")

    code = main(["run", str(DEFINITION_PATH), "--context", str(context), "--adapters", "morph.examples.crosspoint:adapters"])

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "executed"
    assert output["outputs"]["route_id"] == "cam1->wall"
    assert output["effects"][0]["status"] == "succeeded"

    code = main(["run", str(DEFINITION_PATH), "--context", str(context), "--set", "route.locked=true", "--set", "route.locked_by=x", "--adapters", "morph.examples.crosspoint:adapters"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["capability"] == "notify"


def test_cli_validate_counts_capabilities(capsys):
    assert main(["validate", str(DEFINITION_PATH)]) == 0
    assert "2 capabilities, 2 actions" in capsys.readouterr().out
