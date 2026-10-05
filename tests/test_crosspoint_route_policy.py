import pytest

from morph import MORPHRuntime


def test_allows_route_when_source_destination_and_capabilities_are_valid():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.2.0",
            "capabilities": {
                "route_control": {"requires": ["operator.route_control"]},
            },
            "policies": [
                {
                    "name": "allowed_route_when_ready",
                    "when": [
                        {"field": "source.status", "equals": "live"},
                        {"field": "destination.status", "equals": "ready"},
                        {"field": "route.locked", "equals": False},
                        {"field": "source.latency_ms", "lt": 120},
                        {"field": "destination.latency_ms", "lt": 120},
                        {"field": "operator.capabilities", "contains": "route_control"},
                    ],
                    "result": {"status": "allow", "action": "route_source"},
                },
                {
                    "name": "deny_locked_route",
                    "when": [{"field": "route.locked", "equals": True}],
                    "result": {"status": "deny", "action": "raise_alert"},
                },
            ],
        }
    )

    decision = system.evaluate({
        "source": {"status": "live", "latency_ms": 42},
        "destination": {"status": "ready", "latency_ms": 70},
        "route": {"locked": False},
        "operator": {"capabilities": ["route_control"]},
    })

    assert decision["status"] == "allow"
    assert decision["action"] == "route_source"


def test_denies_route_when_locked():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.2.0",
            "policies": [
                {
                    "name": "deny_locked_route",
                    "when": [{"field": "route.locked", "equals": True}],
                    "result": {"status": "deny", "action": "raise_alert"},
                }
            ],
        }
    )

    decision = system.evaluate({
        "source": {"status": "live", "latency_ms": 30},
        "destination": {"status": "ready", "latency_ms": 45},
        "route": {"locked": True},
        "operator": {"capabilities": ["route_control"]},
    })

    assert decision["status"] == "deny"
    assert decision["action"] == "raise_alert"


def test_requires_capability_for_override_action():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.2.0",
            "policies": [
                {
                    "name": "override_requires_route_control",
                    "when": [
                        {"field": "route.locked", "equals": True},
                        {"field": "operator.capabilities", "contains": "route_control"},
                    ],
                    "result": {"status": "allow", "action": "override_route"},
                },
                {
                    "name": "deny_without_control_capability",
                    "when": [{"field": "route.locked", "equals": True}],
                    "result": {"status": "deny", "action": "raise_alert"},
                },
            ],
        }
    )

    decision = system.evaluate({
        "source": {"status": "live", "latency_ms": 20},
        "destination": {"status": "ready", "latency_ms": 25},
        "route": {"locked": True},
        "operator": {"capabilities": ["route_control"]},
    })

    assert decision["status"] == "allow"
    assert decision["action"] == "override_route"


def test_requires_policies_to_be_defined():
    with pytest.raises(ValueError):
        MORPHRuntime.from_dict({})
