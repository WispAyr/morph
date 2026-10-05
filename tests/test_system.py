import json

import pytest

from morph import EventStore, MORPHRuntime, MORPHSystem, Schema
from morph.cli import main
from morph.examples.crosspoint import DEFINITION_PATH, FakeRouter, system


def _warm(sys_):
    sys_.observe("source", "cam1", status="live", latency_ms=40)
    sys_.observe("destination", "wall", status="ready", latency_ms=60)
    sys_.observe("operator", "ewan", capabilities=["route_control"])


# --- schema: state machines -------------------------------------------------------------


def test_stateful_entity_gets_a_state_field_and_initial():
    schema = Schema.from_ir([{"name": "d", "fields": {"id": "string"}, "states": ["idle", "routed"], "transitions": [{"on": "x", "to": "routed"}]}])

    entity = schema.entities["d"]
    assert entity.fields["state"] == "string"
    assert entity.initial == "idle"
    assert entity.transitions[0].sources is None
    assert entity.to_dict()["transitions"] == [{"on": "x", "from": "*", "to": "routed"}]


@pytest.mark.parametrize(
    "declaration,message",
    [
        ({"states": ["a"], "initial": "b"}, "initial 'b' is not one of its states"),
        ({"transitions": [{"on": "x", "to": "a"}]}, "transitions or initial without states"),
        ({"states": ["a"], "transitions": [{"to": "a"}]}, "transitions\\[0\\].on is required"),
        ({"states": ["a"], "transitions": [{"on": "x", "to": "zzz"}]}, "to 'zzz' is not one of"),
        ({"states": ["a"], "transitions": [{"on": "x", "from": ["q"], "to": "a"}]}, "from names unknown states \\['q'\\]"),
        ({"states": ["a"], "transitions": [{"on": "x", "to": "a", "when": "d.nope == 1"}]}, "unknown field 'd.nope'"),
        ({"states": ["a"], "transitions": [{"on": "x", "to": "a", "when": "d.id =="}]}, "Invalid expression"),
        ({"states": ["a"], "transitions": [{"on": "x", "to": "a", "bogus": 1}]}, "unsupported keys: \\['bogus'\\]"),
        ({"states": ["a"], "fields": {"id": "string", "state": "int"}}, "reserved for the machine state"),
    ],
)
def test_state_machine_declarations_are_validated(declaration, message):
    with pytest.raises(ValueError, match=message):
        Schema.from_ir([{"name": "d", "fields": {"id": "string"}, **declaration}])


def test_transition_conditions_may_read_the_event():
    schema = Schema.from_ir([{"name": "d", "fields": {"id": "string"}, "states": ["a", "b"], "transitions": [{"on": "observed", "to": "b", "when": "event.fields.x == 1"}]}])

    assert schema.entities["d"].transitions[0].when.source == "event.fields.x == 1"


def test_policies_can_read_machine_state():
    runtime = MORPHRuntime.from_dict(
        {
            "entities": [{"name": "d", "fields": {"id": "string"}, "states": ["idle", "busy"]}],
            "policies": [{"name": "p", "when": 'd.state == "idle"', "result": {"status": "allow", "action": "go"}}],
        }
    )

    assert runtime.evaluate({"d": {"id": "x", "state": "idle"}})["status"] == "allow"
    assert runtime.evaluate({"d": {"id": "x", "state": "busy"}})["status"] == "deny"
    assert runtime.evaluate({"d": {"id": "x", "state": "bogus"}})["reason"] == "invalid_context"


# --- event store -----------------------------------------------------------------------


def test_event_store_persists_and_reloads(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("a/1", "observed", {"x": 1})
    store.append("system", "decided", {"y": [1, 2]})

    reloaded = EventStore(path)

    assert [event.to_dict() for event in reloaded] == [event.to_dict() for event in store]
    assert reloaded.events(stream="a/1")[0].data == {"x": 1}
    assert reloaded.events(kinds=["decided"])[0].seq == 2
    assert reloaded.events(after=1)[0].kind == "decided"
    assert reloaded.streams() == ["a/1", "system"]
    assert len(path.read_text().strip().splitlines()) == 2


def test_event_store_rejects_corrupt_log(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "at": "t", "stream": "s", "kind": "k", "data": {}}\n{"seq": 5, "at": "t", "stream": "s", "kind": "k", "data": {}}\n')

    with pytest.raises(ValueError, match="expected seq 2, found 5"):
        EventStore(path)


def test_event_data_is_made_serialisable():
    store = EventStore()
    event = store.append("s", "k", {"path": __import__("pathlib").Path("/x")})

    assert event.data == {"path": "/x"}


# --- the system -------------------------------------------------------------------------


def test_observe_validates_entity_and_types():
    sys_ = system()

    with pytest.raises(ValueError, match="unknown entity 'camera'"):
        sys_.observe("camera", "1", status="live")
    with pytest.raises(ValueError, match="latency_ms must be int"):
        sys_.observe("source", "cam1", latency_ms="fast")
    with pytest.raises(ValueError, match="owned by the state machine"):
        sys_.observe("destination", "wall", state="routed")

    sys_.observe("source", "cam1", status="live", latency_ms=40)
    assert sys_.state("source", "cam1") == {"id": "cam1", "status": "live", "latency_ms": 40}
    assert sys_.state("destination", "wall") == {"id": "wall"}


def test_context_is_built_from_the_projection():
    sys_ = system()
    _warm(sys_)

    context = sys_.context(source="cam1", destination="wall", operator="ewan", extra={"route": {"locked": False}})

    assert context["source"] == {"id": "cam1", "status": "live", "latency_ms": 40}
    assert context["destination"]["state"] == "idle"
    assert context["route"] == {"locked": False}


def test_act_routes_records_events_and_advances_state():
    router = FakeRouter()
    sys_ = system(router=router)
    _warm(sys_)

    result = sys_.act(source="cam1", destination="wall", operator="ewan")

    assert result.status == "executed"
    assert router.routes == {"wall": "cam1"}
    assert sys_.state("destination", "wall")["state"] == "routed"
    kinds = [event.kind for event in sys_.store]
    assert kinds == ["observed", "observed", "observed", "decided", "effect", "transitioned"]
    transitioned = sys_.history(kinds=["transitioned"])[0]["data"]
    decision = sys_.history(kinds=["decided"])[0]
    effect = sys_.history(kinds=["effect"])[0]
    assert transitioned == {
        "entity": "destination",
        "id": "wall",
        "from": "idle",
        "to": "routed",
        "on": "route_source.succeeded",
        "caused_by": {"decision_seq": decision["seq"], "effect_ids": [effect["data"]["id"]]},
    }


def test_observation_driven_transitions_and_state_based_policy():
    sys_ = system()
    _warm(sys_)

    sys_.observe("destination", "wall", status="faulted")
    assert sys_.state("destination", "wall")["state"] == "faulted"

    result = sys_.act(source="cam1", destination="wall", operator="ewan")
    assert result.decision["policy"] == "deny_faulted_destination"
    assert result.capability == "notify"

    sys_.observe("destination", "wall", status="ready")
    assert sys_.state("destination", "wall")["state"] == "idle"
    assert sys_.act(source="cam1", destination="wall", operator="ewan").status == "executed"
    assert sys_.state("destination", "wall")["state"] == "routed"


def test_replay_rebuilds_projection_and_idempotency_without_rerunning_effects(tmp_path):
    path = tmp_path / "crosspoint.jsonl"
    first_router = FakeRouter()
    first = system(EventStore(path), router=first_router)
    _warm(first)
    assert first.act(source="cam1", destination="wall", operator="ewan").status == "executed"
    events_before = len(first.store)

    second_router = FakeRouter()
    second = system(EventStore(path), router=second_router)

    assert second.snapshot()["entities"] == first.snapshot()["entities"]
    assert second.state("destination", "wall")["state"] == "routed"
    assert second.executor.completed_keys() == first.executor.completed_keys()

    repeat = second.act(source="cam1", destination="wall", operator="ewan")
    assert repeat.status == "skipped"
    assert second_router.calls == []
    assert len(second.store) == events_before + 2  # decided + effect(skipped); no transition


def test_failed_effect_emits_failed_outcome_and_no_transition():
    router = FakeRouter()
    router.lock("wall", "someone_else")
    sys_ = system(router=router)
    _warm(sys_)

    result = sys_.act(source="cam1", destination="wall", operator="ewan")

    assert result.status == "failed"
    assert sys_.state("destination", "wall")["state"] == "idle"
    assert sys_.history(kinds=["effect"])[0]["data"]["error"]["code"] == "destination_locked"


def test_system_without_adapters_can_decide_but_not_act():
    sys_ = MORPHSystem({"policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "x"}}]})

    assert sys_.decide({})["status"] == "allow"
    assert sys_.history(kinds=["decided"])[0]["data"]["decision"]["policy"] == "p"
    with pytest.raises(RuntimeError, match="without adapters"):
        sys_.act({})


def test_transition_id_expression_targets_another_instance():
    definition = {
        "entities": [
            {"name": "job", "fields": {"id": "string", "worker": "string"}},
            {"name": "worker", "fields": {"id": "string"}, "states": ["free", "busy"], "transitions": [{"on": "assign.succeeded", "to": "busy", "id": "job.worker"}]},
        ],
        "capabilities": {"assigner": {"requires": [], "inputs": {}, "outputs": {}}},
        "actions": {"assign": {"capability": "assigner", "inputs": {}}},
        "policies": [{"name": "p", "when": "true", "result": {"status": "allow", "action": "assign"}}],
    }
    sys_ = MORPHSystem(definition, {"assigner": lambda inputs: {}})
    sys_.observe("job", "j1", worker="w7")

    sys_.act(job="j1")

    assert sys_.state("worker", "w7")["state"] == "busy"


def test_system_can_replay_causal_history_for_an_entity():
    sys_ = system()
    _warm(sys_)
    sys_.act(source="cam1", destination="wall", operator="ewan")

    provenance = sys_.provenance("destination", "wall")
    assert provenance["entity"] == "destination"
    assert provenance["id"] == "wall"
    assert provenance["current_state"]["state"] == "routed"
    assert provenance["events"][0]["kind"] == "observed"
    assert any(event["kind"] == "transitioned" for event in provenance["events"])

    replay = sys_.replay("destination", "wall")
    assert [event["kind"] for event in replay] == [event["kind"] for event in provenance["events"]]


def test_system_can_reconstruct_counterfactual_state_without_a_transition():
    sys_ = system()
    _warm(sys_)
    sys_.act(source="cam1", destination="wall", operator="ewan")

    counterfactual = sys_.counterfactual("destination", "wall", omit_kinds={"transitioned"})

    assert counterfactual["entity"] == "destination"
    assert counterfactual["id"] == "wall"
    assert counterfactual["current_state"]["state"] == "idle"
    assert counterfactual["omitted"] == ["transitioned"]


def test_system_can_explain_causal_history_in_plain_english():
    sys_ = system()
    _warm(sys_)
    sys_.act(source="cam1", destination="wall", operator="ewan")

    explanation = sys_.explain("destination", "wall")

    assert explanation["entity"] == "destination"
    assert explanation["id"] == "wall"
    assert "transitioned" in explanation["summary"].lower()
    assert "routed" in explanation["current_state"]["state"]
    assert explanation["narrative"].startswith("destination")
    causal = explanation["causal"]
    decision = causal["decision"]["data"]["decision"]
    effect = causal["effects"][0]["data"]
    assert decision["policy"]
    assert effect["action"] == decision["action"]
    assert effect["status"] == "succeeded"
    assert causal["transition"]["data"]["caused_by"]["decision_seq"] == causal["decision"]["seq"]
    assert decision["policy"] in explanation["narrative"]


def test_system_can_explain_causal_difference_between_actual_and_counterfactual_state():
    sys_ = system()
    _warm(sys_)
    sys_.act(source="cam1", destination="wall", operator="ewan")

    diff = sys_.explain_diff("destination", "wall", omit_kinds={"transitioned"})

    assert diff["entity"] == "destination"
    assert diff["id"] == "wall"
    assert diff["before"]["state"] == "idle"
    assert diff["after"]["state"] == "routed"
    assert "transitioned" in diff["narrative"]
    assert "routed" in diff["narrative"]


def test_system_can_build_a_semantic_contract_review_object():
    sys_ = system()
    _warm(sys_)
    sys_.act(source="cam1", destination="wall", operator="ewan")

    review = sys_.review("destination", "wall", approver="ewan", approved=True)

    assert review["entity"] == "destination"
    assert review["id"] == "wall"
    assert review["approval"]["approved"] is True
    assert review["approval"]["by"] == "ewan"
    assert review["policy_lineage"]
    assert review["provenance"]["current_state"]["state"] == "routed"


# --- CLI ---------------------------------------------------------------------------------


def test_cli_system_commands(tmp_path, capsys):
    store = str(tmp_path / "events.jsonl")
    base = ["system", str(DEFINITION_PATH), "--store", store]

    assert main(base + ["observe", "source", "cam1", "status=live", "latency_ms=40"]) == 0
    assert main(base + ["observe", "destination", "wall", "status=ready", "latency_ms=60"]) == 0
    assert main(base + ["observe", "operator", "ewan", "capabilities=[route_control]"]) == 0
    capsys.readouterr()

    assert main(base + ["act", "--adapters", "morph.examples.crosspoint:adapters", "source=cam1", "destination=wall", "operator=ewan"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "executed"

    assert main(base + ["state", "destination", "wall"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "routed"

    assert main(base + ["history", "--kind", "transitioned"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["data"]["to"] == "routed"

    assert main(base + ["explain", "destination", "wall"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["entity"] == "destination"
    assert "transitioned" in explanation["narrative"].lower()
    assert "routed" in explanation["narrative"]
    assert explanation["causal"]["decision"]["data"]["decision"]["policy"] in explanation["narrative"]
    assert explanation["causal"]["effects"][0]["data"]["status"] == "succeeded"

    assert main(base + ["observe", "source", "cam1", "latency_ms=fast"]) == 1
    assert "latency_ms must be int" in capsys.readouterr().out
