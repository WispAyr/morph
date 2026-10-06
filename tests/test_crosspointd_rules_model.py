"""The crosspointd rules-engine MORPH model must decide exactly as crosspointd's Core.runRules does.

tests/fixtures/crosspointd_rule_decisions.jsonl.gz is recorded from crosspointd by
tools/crosspointd_rules_oracle.mjs. Set CROSSPOINT_DIR to a Crosspoint checkout to also re-record
it and fail when crosspointd's behaviour has drifted from the fixture.
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
MODEL = ROOT / "src/morph/examples/crosspointd_rules.yaml"
FIXTURE = ROOT / "tests/fixtures/crosspointd_rule_decisions.jsonl.gz"


def _records():
    lines = gzip.decompress(FIXTURE.read_bytes()).decode("utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]


def _context(record):
    return {
        "rule": {"id": "r1", **record["rule"]},
        "destination": {"id": "studio/cam5", **record["destination"]},
        "candidate": {"id": "studio/caller1", **record["candidate"]},
        "current_source": record["current_source"],
        "route": record["route"],
        "retry": record["retry"],
    }


def _outcome(decision):
    allowed = decision["status"] == "allow"
    return {"command": decision["action"] if allowed else None, "level": decision.get("level"), "kind": decision["action"] if allowed else decision.get("reason")}


def test_the_model_agrees_with_every_recorded_rules_engine_decision():
    header, records = _records()
    definition = load_system_definition(MODEL)
    runtime = MORPHRuntime(
        name=definition.name, version=definition.version, policies=definition.policies,
        capabilities=definition.capabilities, entities=definition.entities, actions=definition.actions,
    )
    assert len(records) > 6000 and len(header["crosspoint_commit"]) == 40

    disagreements = [(record, _outcome(runtime.evaluate(_context(record)))) for record in records
                     if _outcome(runtime.evaluate(_context(record))) != record["outcome"]]

    assert not disagreements, f"{len(disagreements)} disagreements, first: {disagreements[0]}"


def test_the_fixture_covers_every_rules_engine_outcome():
    _, records = _records()
    assert {record["outcome"]["kind"] for record in records} == {
        "take", "release", "disabled", "peer_destination", "destination_unavailable", "operator_only",
        "release_waiting_on_air", "queued_on_air", "on_air_routed", "on_air_idle", "in_flight",
        "already_routed", "busy", "take_retrying", "release_retrying", "idle_no_source",
    }


def test_every_recorded_tick_satisfies_the_safety_invariants():
    _, records = _records()
    model = MORPHIR.from_dict(load_system_definition(MODEL).to_dict())

    assert len(model.invariants) == 8
    assert model.simulate([_context(record) for record in records])["failed"] == []


@pytest.mark.parametrize("policies, invariant", [
    (["release_waiting_on_air", "queued_on_air", "on_air_routed", "on_air_idle"], "never_touches_an_on_air_destination"),
    (["operator_only"], "never_targets_peer_or_operator_only_destinations"),
    (["busy"], "if_free_never_displaces_a_live_source"),
    (["take_retrying", "release_retrying"], "failed_commands_wait_before_retrying"),
    (["in_flight"], "one_command_at_a_time"),
])
def test_dropping_a_safety_policy_breaks_its_invariant(policies, invariant):
    _, records = _records()
    data = load_system_definition(MODEL).to_dict()
    data["policies"] = [item for item in data["policies"] if item["name"] not in policies]

    failed = MORPHIR.from_dict(data).simulate([_context(record) for record in records])["failed"]

    assert any(invariant in item["invariants"] for item in failed)


@pytest.mark.skipif(not os.environ.get("CROSSPOINT_DIR") or not shutil.which("node"), reason="set CROSSPOINT_DIR and install Node to re-record")
def test_the_fixture_matches_the_current_crosspointd():
    recorded = subprocess.run(
        ["node", str(ROOT / "tools/crosspointd_rules_oracle.mjs"), os.environ["CROSSPOINT_DIR"]],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    _, fixture = _records()
    current = [json.loads(line) for line in recorded[1:]]
    changed = [(old, new) for old, new in zip(fixture, current) if old != new]
    assert len(current) == len(fixture) and not changed, (
        f"crosspointd's rules engine now decides {len(changed)} scenarios differently; re-record the fixture and update the model. "
        f"First: {changed[:1]}"
    )
