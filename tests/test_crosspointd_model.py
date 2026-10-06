"""The crosspointd MORPH model must decide exactly as crosspointd does.

tests/fixtures/crosspointd_manual_decisions.jsonl.gz is recorded from crosspointd's own Core by
tools/crosspointd_oracle.mjs. Set CROSSPOINT_DIR to a Crosspoint checkout to also re-record it
and fail when crosspointd's behaviour has drifted from the fixture.
"""

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from morph import MORPHIR, MORPHRuntime, load_system_definition

ROOT = Path(__file__).parents[1]
MODEL = ROOT / "src/morph/examples/crosspointd.yaml"
FIXTURE = ROOT / "tests/fixtures/crosspointd_manual_decisions.jsonl.gz"


def _runtime():
    definition = load_system_definition(MODEL)
    return MORPHRuntime(
        name=definition.name, version=definition.version, policies=definition.policies,
        capabilities=definition.capabilities, entities=definition.entities, actions=definition.actions,
    )


def _records():
    lines = gzip.decompress(FIXTURE.read_bytes()).decode("utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]


def _outcome(decision):
    if decision["status"] == "allow":
        return {"status": "allow", "action": decision["action"]}
    return {"status": "deny", "reason": decision.get("reason")}


def test_the_model_agrees_with_every_recorded_crosspointd_decision():
    header, records = _records()
    runtime = _runtime()
    assert len(records) > 2000 and len(header["crosspoint_commit"]) == 40

    disagreements = []
    for record in records:
        context = {
            "request": record["request"],
            "destination": {"id": "dest", **record["destination"]},
            "source": {"id": "src", **record["source"]},
        }
        decision = runtime.evaluate(context)
        if _outcome(decision) != record["outcome"]:
            disagreements.append((record, decision.get("policy"), _outcome(decision)))

    assert not disagreements, f"{len(disagreements)} disagreements, first: {disagreements[0]}"


def test_the_fixture_covers_every_crosspointd_outcome():
    _, records = _records()
    outcomes = {record["outcome"].get("action") or record["outcome"]["reason"] for record in records}
    assert outcomes == {
        "take", "forced_take", "release", "forced_release", "propose_take", "propose_release", "forward_take", "forward_release",
        "screen_needs_layout", "not_accepted", "pull_not_granted", "on_air", "in_flight",
    }


def _contexts(records):
    return [
        {"request": record["request"], "destination": {"id": "dest", **record["destination"]}, "source": {"id": "src", **record["source"]}}
        for record in records
    ]


def test_every_recorded_scenario_satisfies_the_safety_invariants():
    _, records = _records()
    model = MORPHIR.from_dict(load_system_definition(MODEL).to_dict())

    assert len(model.invariants) == 6
    assert model.simulate(_contexts(records))["failed"] == []


@pytest.mark.parametrize("policy, invariant", [
    ("deny_on_air", "on_air_changed_only_with_force"),
    ("deny_in_flight", "no_command_while_one_is_in_flight"),
    ("forward_take", "peer_destinations_are_forwarded"),
    ("propose_take", "operator_only_gets_proposals"),
    ("deny_screen_needs_layout", "screens_take_only_layouts"),
])
def test_dropping_a_safety_policy_breaks_its_invariant(policy, invariant):
    _, records = _records()
    data = load_system_definition(MODEL).to_dict()
    data["policies"] = [item for item in data["policies"] if item["name"] != policy]

    failed = MORPHIR.from_dict(data).simulate(_contexts(records))["failed"]

    assert failed and any(invariant in item["invariants"] for item in failed)


def test_impact_of_the_operator_only_flag_reaches_the_on_air_check_and_proposals():
    impact = MORPHIR.from_dict(load_system_definition(MODEL).to_dict()).impact("destination.operator_only")

    assert {"deny_on_air", "propose_take", "propose_release"} <= set(impact["policies"])
    assert "propose_to_operator" in impact["capabilities"]


@pytest.mark.skipif(not os.environ.get("CROSSPOINT_DIR") or not shutil.which("node"), reason="set CROSSPOINT_DIR and install Node to re-record")
def test_the_fixture_matches_the_current_crosspointd():
    recorded = subprocess.run(
        ["node", str(ROOT / "tools/crosspointd_oracle.mjs"), os.environ["CROSSPOINT_DIR"]],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    _, fixture = _records()
    current = [json.loads(line) for line in recorded[1:]]
    changed = [(old, new) for old, new in zip(fixture, current) if old != new]
    assert len(current) == len(fixture) and not changed, (
        f"crosspointd now decides {len(changed)} scenarios differently; re-record the fixture and update the model. "
        f"First: {changed[:1]}"
    )
