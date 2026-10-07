"""MORPHIR.decision_changes: which policies decide different inputs, including ones whose text did not change."""

import copy
import json

from morph import MORPHIR
from morph.cli import main


def _model(policies):
    return MORPHIR.from_dict({
        "name": "routing",
        "entities": [{"name": "destination", "fields": {"on_air": "bool", "busy": "bool", "kind": "string"}}],
        "policies": policies,
    })


DENY_ON_AIR = {"name": "deny_on_air", "when": "destination.on_air", "result": {"status": "deny", "action": "reject", "reason": "on_air"}}
DENY_BUSY = {"name": "deny_busy", "when": "destination.busy", "result": {"status": "deny", "action": "reject", "reason": "busy"}}
TAKE = {"name": "take", "when": "true", "result": {"status": "allow", "action": "take"}}


def _by_name(result):
    return {entry["name"]: entry for entry in result["policies"]}


def test_narrowing_an_early_policy_moves_inputs_to_untouched_later_ones():
    baseline = _model([DENY_ON_AIR, DENY_BUSY, TAKE])
    candidate = _model([{**DENY_ON_AIR, "when": 'destination.on_air && destination.kind != "screen"'}, DENY_BUSY, TAKE])
    result = baseline.decision_changes(candidate)
    changes = _by_name(result)

    assert result["confidence"] == "proven"
    assert result["decide_differently"] == ["deny_on_air", "deny_busy", "take"]
    assert changes["deny_on_air"]["change"] == "decides_fewer_inputs"
    # deny_busy and take are textually identical but now decide on-air screens.
    assert changes["deny_busy"]["change"] == "decides_more_inputs"
    assert changes["take"]["change"] == "decides_more_inputs"
    example = changes["take"]["example_now_decided"]
    assert example["input"]["destination"]["on_air"] is True and example["input"]["destination"]["kind"] == "screen"
    assert example["baseline"]["policy"] == "deny_on_air" and example["candidate"] == {"policy": "take", "status": "allow", "action": "take"}


def test_a_policy_added_behind_one_that_covers_it_never_decides():
    baseline = _model([DENY_ON_AIR, TAKE])
    shadowed = {"name": "deny_on_air_screen", "when": 'destination.on_air && destination.kind == "screen"',
                "result": {"status": "deny", "action": "reject", "reason": "screen"}}
    changes = _by_name(baseline.decision_changes(_model([DENY_ON_AIR, shadowed, TAKE])))
    assert changes["deny_on_air_screen"]["change"] == "added_never_decides"
    assert changes["deny_on_air"]["change"] == changes["take"]["change"] == "unchanged"


def test_same_inputs_with_a_new_outcome_and_removed_policies_are_reported():
    baseline = _model([DENY_ON_AIR, DENY_BUSY, TAKE])
    candidate = _model([{**DENY_ON_AIR, "result": {**DENY_ON_AIR["result"], "reason": "live"}}, TAKE])
    changes = _by_name(baseline.decision_changes(candidate))
    assert changes["deny_on_air"]["change"] == "same_inputs_different_outcome"
    assert changes["deny_busy"]["change"] == "removed"
    assert changes["deny_busy"]["example_no_longer_decided"]["candidate"]["policy"] == "take"
    assert changes["take"]["change"] == "decides_more_inputs"


def test_by_default_inputs_carry_every_baseline_field():
    baseline = _model([DENY_ON_AIR, TAKE])
    # True whenever busy is present, so it changes nothing for complete inputs; an input missing busy no longer matches.
    candidate = _model([{**DENY_ON_AIR, "when": "destination.on_air && (destination.busy || !destination.busy)"}, TAKE])
    complete = baseline.decision_changes(candidate)
    assert complete["decide_differently"] == []
    loose = baseline.decision_changes(candidate, complete_inputs=False)
    assert loose["decide_differently"] == ["deny_on_air", "take"]
    assert "busy" not in _by_name(loose)["take"]["example_now_decided"]["input"]["destination"]
    assert "absent" in loose["assumption"]


def test_a_gated_policy_or_a_retyped_field_makes_the_result_unknown():
    baseline = _model([DENY_ON_AIR, TAKE])
    gated = _model([DENY_ON_AIR, {**TAKE, "requires": ["route"]}]).to_dict()
    gated["capabilities"] = {"route": {"description": "Route.", "requires": [], "inputs": {}, "outputs": {}, "failures": []}}
    assert baseline.decision_changes(gated)["confidence"] == "unknown"
    retyped = copy.deepcopy(baseline.to_dict())
    retyped["entities"][0]["fields"]["on_air"] = "string"
    assert baseline.decision_changes(retyped)["confidence"] == "unknown"


def test_cli_diff_reports_the_relationship_and_decision_changes(tmp_path, capsys):
    baseline, candidate = tmp_path / "baseline.json", tmp_path / "candidate.json"
    baseline.write_text(json.dumps(_model([DENY_ON_AIR, DENY_BUSY, TAKE]).to_dict()))
    candidate.write_text(json.dumps(_model([DENY_BUSY, TAKE]).to_dict()))
    assert main(["diff", str(baseline), str(candidate)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["relationship"] == "BROADER"
    assert report["decisions"]["decide_differently"] == ["deny_on_air", "deny_busy", "take"]
