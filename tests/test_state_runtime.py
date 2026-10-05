import pytest

from morph import CapabilityValidator, MORPHRuntime, StateMachine


def test_capability_validator_rejects_missing_capability():
    context = {"operator": {"capabilities": ["audio_control"]}}

    errors = CapabilityValidator.validate("route_control", context)

    assert errors == ["missing capability: route_control"]


def test_state_machine_transitions_when_state_and_capability_are_valid():
    machine = StateMachine(
        {
            "name": "studio_state_machine",
            "initial": "idle",
            "transitions": [
                {
                    "from": "idle",
                    "on": "route",
                    "when": [{"field": "source.status", "equals": "live"}],
                    "to": "routed",
                    "result": {"status": "allow", "action": "route_source"},
                }
            ],
        }
    )

    decision = machine.evaluate({"source": {"status": "live"}}, "route")

    assert decision["status"] == "allow"
    assert decision["action"] == "route_source"
    assert decision["to"] == "routed"


def test_runtime_checks_capability_requirements_before_approving_action():
    runtime = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.3.0",
            "capabilities": {
                "route_control": {"requires": ["operator.capabilities"]},
            },
            "policies": [
                {
                    "name": "route_requires_capability",
                    "when": [
                        {"field": "source.status", "equals": "live"},
                        {"field": "operator.capabilities", "contains": "route_control"},
                    ],
                    "result": {"status": "allow", "action": "route_source"},
                }
            ],
        }
    )

    decision = runtime.evaluate({
        "source": {"status": "live"},
        "operator": {"capabilities": ["route_control"]},
    })

    assert decision["status"] == "allow"
    assert decision["action"] == "route_source"


def test_runtime_denies_action_when_capability_missing():
    runtime = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.3.0",
            "capabilities": {
                "route_control": {"requires": ["operator.capabilities"]},
            },
            "policies": [
                {
                    "name": "route_requires_capability",
                    "when": [
                        {"field": "source.status", "equals": "live"},
                        {"field": "operator.capabilities", "contains": "route_control"},
                    ],
                    "result": {"status": "allow", "action": "route_source"},
                }
            ],
        }
    )

    decision = runtime.evaluate({
        "source": {"status": "live"},
        "operator": {"capabilities": ["audio_control"]},
    })

    assert decision["status"] == "deny"
    assert decision["action"] == "raise_alert"
