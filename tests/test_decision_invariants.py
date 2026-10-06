"""Invariants may read the decision the policies reach, under the reserved `decision` root."""

import pytest

from morph import MORPHIR

ENTITIES = [
    {"name": "request", "fields": {"force": "bool"}},
    {"name": "destination", "fields": {"on_air": "bool"}},
]
POLICIES = [
    {"name": "deny_on_air", "when": "destination.on_air && !request.force", "result": {"status": "deny", "action": "reject", "reason": "on_air"}},
    {"name": "take", "when": "true", "result": {"status": "allow", "action": "take"}},
]
ON_AIR_NEEDS_FORCE = {"name": "on_air_needs_force", "when": 'decision.status != "allow" || !destination.on_air || request.force'}


def _model(policies=POLICIES, invariants=(ON_AIR_NEEDS_FORCE,), entities=ENTITIES):
    return MORPHIR.from_dict({"name": "x", "entities": entities, "policies": list(policies), "invariants": list(invariants)}).validate()


def _scenarios():
    return [{"request": {"force": force}, "destination": {"on_air": on_air}} for force in (False, True) for on_air in (False, True)]


def test_simulation_checks_a_decision_invariant_against_the_decision_reached():
    assert _model().simulate(_scenarios())["failed"] == []

    unsafe = _model(policies=POLICIES[1:]).simulate(_scenarios())
    assert [(item["context"], item["invariants"]) for item in unsafe["failed"]] == [
        ({"request": {"force": False}, "destination": {"on_air": True}}, ["on_air_needs_force"]),
    ]


def test_absent_decision_fields_read_as_empty_strings():
    model = _model(invariants=[{"name": "denials_carry_a_reason", "when": 'decision.status == "allow" || decision.reason != ""'}])
    assert model.simulate(_scenarios())["failed"] == []

    no_reason = [{**POLICIES[0], "result": {"status": "deny", "action": "reject"}}, POLICIES[1]]
    failed = _model(policies=no_reason, invariants=model.invariants).simulate(_scenarios())["failed"]
    assert len(failed) == 1


def test_context_invariants_still_read_only_the_context():
    model = _model(invariants=[{"name": "force_is_set", "when": "has(request.force)"}])
    assert model.simulate(_scenarios())["failed"] == []


def test_validation_rejects_unknown_decision_fields_and_a_decision_entity():
    with pytest.raises(ValueError, match="not a decision field"):
        _model(invariants=[{"name": "bad", "when": 'decision.verdict == "allow"'}])
    with pytest.raises(ValueError, match="reserved for the decision"):
        _model(entities=ENTITIES + [{"name": "decision", "fields": {"status": "string"}}])


def test_diff_proves_rewritten_decision_invariants_and_blocks_weakened_ones():
    baseline = _model()
    rewritten = _model(invariants=[{"name": "on_air_needs_force", "when": '!(decision.status == "allow" && destination.on_air) || request.force'}])
    weakened = _model(invariants=[{"name": "on_air_needs_force", "when": 'decision.status != "allow" || request.force || true'}])

    assert baseline.diff(rewritten) == {"preserved": ["on_air_needs_force"], "added": [], "blocked": []}
    assert baseline.diff(weakened)["blocked"] == ["on_air_needs_force"]
