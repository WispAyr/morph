import pytest

from morph import MORPHRuntime


def test_allows_access_when_permit_and_payment_are_valid():
    system = MORPHRuntime.from_dict(
        {
            "name": "parking_access",
            "version": "0.1.0",
            "grace_period_minutes": 15,
            "policies": [
                {
                    "name": "permit_and_payment_required",
                    "when": [
                        {"field": "vehicle.permit_valid", "equals": True},
                        {"field": "vehicle.payment_valid", "equals": True},
                    ],
                    "result": {"status": "allow", "action": "open_barrier"},
                },
                {
                    "name": "deny_without_valid_payment",
                    "when": [{"field": "vehicle.payment_valid", "equals": False}],
                    "result": {"status": "deny", "action": "create_enforcement_event"},
                },
                {
                    "name": "deny_without_valid_permit",
                    "when": [{"field": "vehicle.permit_valid", "equals": False}],
                    "result": {"status": "deny", "action": "create_enforcement_event"},
                },
            ],
        }
    )

    decision = system.evaluate({
        "vehicle": {"permit_valid": True, "payment_valid": True},
        "site": {"grace_period_minutes": 15},
        "session": {"minutes_since_arrival": 10},
    })

    assert decision["status"] == "allow"
    assert decision["action"] == "open_barrier"


def test_grace_period_allows_late_entry_when_within_window():
    system = MORPHRuntime.from_dict(
        {
            "name": "parking_access",
            "version": "0.1.0",
            "grace_period_minutes": 15,
            "policies": [
                {
                    "name": "grace_period_allowance",
                    "when": [
                        {"field": "vehicle.permit_valid", "equals": True},
                        {"field": "vehicle.payment_valid", "equals": True},
                        {"field": "session.minutes_since_arrival", "lte": 15},
                    ],
                    "result": {"status": "allow", "action": "open_barrier"},
                }
            ],
        }
    )

    decision = system.evaluate({
        "vehicle": {"permit_valid": True, "payment_valid": True},
        "site": {"grace_period_minutes": 15},
        "session": {"minutes_since_arrival": 12},
    })

    assert decision["status"] == "allow"


def test_denies_access_when_permit_is_invalid():
    system = MORPHRuntime.from_dict(
        {
            "name": "parking_access",
            "version": "0.1.0",
            "grace_period_minutes": 15,
            "policies": [
                {
                    "name": "deny_without_permit",
                    "when": [{"field": "vehicle.permit_valid", "equals": False}],
                    "result": {"status": "deny", "action": "create_enforcement_event"},
                }
            ],
        }
    )

    decision = system.evaluate({
        "vehicle": {"permit_valid": False, "payment_valid": True},
        "site": {"grace_period_minutes": 15},
        "session": {"minutes_since_arrival": 0},
    })

    assert decision["status"] == "deny"
    assert decision["action"] == "create_enforcement_event"


def test_requires_policies_to_be_defined():
    with pytest.raises(ValueError):
        MORPHRuntime.from_dict({})
