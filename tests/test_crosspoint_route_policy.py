import pytest

from morph import MORPHRuntime


def test_allows_route_when_source_and_destination_are_ready():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.1.0",
            "policies": [
                {
                    "name": "allowed_route_when_source_and_destination_are_ready",
                    "when": [
                        {"field": "source.live", "equals": True},
                        {"field": "destination.available", "equals": True},
                    ],
                    "result": {"status": "allow", "action": "route_source"},
                },
                {
                    "name": "deny_unavailable_destination",
                    "when": [{"field": "destination.available", "equals": False}],
                    "result": {"status": "deny", "action": "raise_alert"},
                },
                {
                    "name": "deny_unavailable_source",
                    "when": [{"field": "source.live", "equals": False}],
                    "result": {"status": "deny", "action": "raise_alert"},
                },
            ],
        }
    )

    decision = system.evaluate({
        "source": {"live": True},
        "destination": {"available": True},
        "operator": {"override": False},
    })

    assert decision["status"] == "allow"
    assert decision["action"] == "route_source"


def test_block_route_when_destination_is_unavailable():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.1.0",
            "policies": [
                {
                    "name": "deny_unavailable_destination",
                    "when": [{"field": "destination.available", "equals": False}],
                    "result": {"status": "deny", "action": "raise_alert"},
                }
            ],
        }
    )

    decision = system.evaluate({
        "source": {"live": True},
        "destination": {"available": False},
        "operator": {"override": False},
    })

    assert decision["status"] == "deny"
    assert decision["action"] == "raise_alert"


def test_block_route_when_source_is_offline():
    system = MORPHRuntime.from_dict(
        {
            "name": "crosspoint",
            "version": "0.1.0",
            "policies": [
                {
                    "name": "deny_unavailable_source",
                    "when": [{"field": "source.live", "equals": False}],
                    "result": {"status": "deny", "action": "raise_alert"},
                }
            ],
        }
    )

    decision = system.evaluate({
        "source": {"live": False},
        "destination": {"available": True},
        "operator": {"override": False},
    })

    assert decision["status"] == "deny"
    assert decision["action"] == "raise_alert"


def test_requires_policies_to_be_defined():
    with pytest.raises(ValueError):
        MORPHRuntime.from_dict({})
