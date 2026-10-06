import pytest

from morph import Compiler, ExecutionPlanner, MORPHRuntime, PythonTarget, Schema, StateMachine, WorkflowEngine

ENTITIES = [
    {"name": "source", "fields": {"status": "string", "latency_ms": "int"}},
    {"name": "destination", "state": {"status": "ready", "latency_ms": 70, "tags": ["a"], "meta": {"k": 1}}},
]


def _policy(when, name="p"):
    return {"name": name, "when": when, "result": {"status": "allow", "action": "ok"}}


def test_schema_infers_types_from_example_state():
    schema = Schema.from_ir(ENTITIES)

    assert schema.entities["destination"].fields == {"status": "string", "latency_ms": "int", "tags": "list", "meta": "map"}
    assert schema.entities["source"].fields == {"status": "string", "latency_ms": "int"}


def test_schema_rejects_bad_declarations():
    with pytest.raises(ValueError, match="unknown type 'text'"):
        Schema.from_ir([{"name": "a", "fields": {"x": "text"}}])
    with pytest.raises(ValueError, match="declared more than once"):
        Schema.from_ir([{"name": "a"}, {"name": "a"}])
    with pytest.raises(ValueError, match="name is required"):
        Schema.from_ir([{"fields": {}}])


def test_schema_validates_paths():
    schema = Schema.from_ir(ENTITIES)

    assert schema.validate_paths(["source.status", "destination.meta.k", "destination.tags", "source"]) == []
    assert schema.validate_paths(["source.staus"]) == [
        "unknown field 'source.staus' in 'source.staus' (known: ['latency_ms', 'status'])"
    ]
    assert schema.validate_paths(["router.status"]) == [
        "unknown entity 'router' in 'router.status' (known: ['destination', 'source'])"
    ]
    assert schema.validate_paths(["source.status.code"]) == [
        "field 'source.status' is string and has no member 'code' in 'source.status.code'"
    ]


def test_schema_validates_context_types():
    schema = Schema.from_ir(ENTITIES)

    assert schema.validate_context({"source": {"status": "live", "latency_ms": 42}}) == []
    assert schema.validate_context({"source": {"status": "live"}, "extra": 1}) == []
    assert schema.validate_context({"source": {"latency_ms": "42"}}) == ["context.source.latency_ms must be int, got string"]
    assert schema.validate_context({"source": {"latency_ms": True}}) == ["context.source.latency_ms must be int, got bool"]
    assert schema.validate_context({"source": "live"}) == ["context.source must be an object"]


def test_empty_schema_is_permissive():
    schema = Schema.from_ir(None)

    assert schema.empty
    assert schema.validate_paths(["anything.goes"]) == []
    assert schema.validate_context({"anything": 1}) == []


def test_policy_typo_is_rejected_at_load():
    with pytest.raises(ValueError, match="unknown field 'source.staus'"):
        MORPHRuntime.from_dict({"entities": ENTITIES, "policies": [_policy('source.staus == "live"')]})


def test_unknown_entity_is_rejected_at_load():
    with pytest.raises(ValueError, match="unknown entity 'operator'"):
        MORPHRuntime.from_dict({"entities": ENTITIES, "policies": [_policy('"x" in operator.capabilities')]})


def test_macro_bound_variables_are_not_entities():
    runtime = MORPHRuntime.from_dict(
        {"entities": ENTITIES, "policies": [_policy('destination.tags.all(t, t != "banned")')]}
    )

    assert runtime.evaluate({"destination": {"tags": ["a", "b"]}})["status"] == "allow"
    assert runtime.evaluate({"destination": {"tags": ["a", "banned"]}})["status"] == "deny"


def test_mistyped_context_denies_with_reason():
    runtime = MORPHRuntime.from_dict({"entities": ENTITIES, "policies": [_policy("source.latency_ms < 120")]})

    decision = runtime.evaluate({"source": {"latency_ms": "42"}})

    assert decision["status"] == "deny"
    assert decision["reason"] == "invalid_context"
    assert decision["errors"] == ["context.source.latency_ms must be int, got string"]


def test_schema_is_enforced_by_every_entry_point():
    bad = {"entities": ENTITIES, "policies": [_policy('source.staus == "live"')]}

    with pytest.raises(ValueError, match="source.staus"):
        ExecutionPlanner.from_dict(bad)
    with pytest.raises(ValueError, match="source.staus"):
        WorkflowEngine.from_dict(bad)
    with pytest.raises(ValueError, match="source.staus"):
        Compiler.compile(bad)
    with pytest.raises(ValueError, match="source.staus"):
        WorkflowEngine.from_dict(
            {"entities": ENTITIES, "workflow": {"steps": [{"name": "s", "when": 'source.staus == "live"', "then": {"action": "a"}}]}}
        )
    with pytest.raises(ValueError, match="source.staus"):
        StateMachine({"entities": ENTITIES, "transitions": [{"from": "a", "on": "go", "when": 'source.staus == "live"'}]})


def test_compiled_plan_carries_schema_and_cel():
    ir = {"entities": ENTITIES, "policies": [_policy([{"field": "source.status", "equals": "live"}])]}

    compiled = Compiler.compile(ir)

    assert compiled["plan"][0]["when"] == '(source.status == "live")'
    assert compiled["entities"][0]["name"] == "source"
    target = PythonTarget(compiled)
    assert target.execute({"source": {"status": "live"}})["status"] == "allow"
    assert target.execute({"source": {"status": 1}})["status"] == "deny"


def test_explain_reports_every_policy():
    runtime = MORPHRuntime.from_dict(
        {
            "entities": ENTITIES,
            "policies": [
                {"name": "deny_slow", "when": "source.latency_ms > 100", "result": {"status": "deny", "action": "raise_alert"}},
                _policy('source.status == "live"', name="allow_live"),
            ],
        }
    )

    report = runtime.explain({"source": {"status": "live", "latency_ms": 42}})

    assert report["policy"] == "allow_live"
    assert report["policies"] == [
        {"policy": "deny_slow", "expression": "source.latency_ms > 100", "capabilities_granted": True, "condition_holds": False},
        {"policy": "allow_live", "expression": 'source.status == "live"', "capabilities_granted": True, "condition_holds": True},
    ]


def test_morph_ir_supports_canonical_semantic_fields_and_validation():
    ir = MORPHRuntime.from_dict(
        {
            "name": "studio",
            "version": "0.7.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
            "policies": [{"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}}],
        }
    )

    assert ir.name == "studio"
    assert ir.evaluate({"source": {"status": "live", "latency_ms": 42}})["status"] == "allow"

    from morph import MORPHIR

    semantic = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
        }
    )

    assert semantic.canonicalize()["kind"] == "morph.ir.v1"
    assert semantic.canonicalize()["intent"]["summary"] == "Only live sources with low latency can route."
    semantic.validate()

    with pytest.raises(ValueError, match="unknown field 'source.staus'"):
        MORPHIR.from_dict(
            {
                "name": "studio",
                "version": "0.7.0",
                "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
                "invariants": [{"name": "bad", "when": 'source.staus == "live"'}],
            }
        ).validate()


def test_morph_ir_preserves_invariants_across_semantic_changes():
    from morph import MORPHIR

    baseline = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
        }
    )

    safe = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.1",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [
                {"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'},
                {"name": "source_is_known", "when": 'has(source.status)'}
            ],
        }
    )

    baseline.validate_change(safe)

    diff = baseline.diff(safe)
    assert diff["preserved"] == ["live_route"]
    assert diff["added"] == ["source_is_known"]
    assert diff["blocked"] == []

    unsafe = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.1",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "ready" && source.latency_ms < 120'}],
        }
    )

    with pytest.raises(ValueError, match="preserve invariant 'live_route'"):
        baseline.validate_change(unsafe)

    blocked = baseline.diff(unsafe)
    assert blocked["blocked"] == ["live_route"]


def test_morph_ir_can_inspect_and_simulate_system_semantics():
    from morph import MORPHIR

    system = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.8.0",
            "entities": [
                {"name": "source", "fields": {"status": "string", "latency_ms": "int"}},
                {"name": "operator", "fields": {"capabilities": "list"}},
            ],
            "capabilities": {"route_control": {"requires": ["operator.capabilities"], "inputs": {"source": "string"}, "failures": ["blocked"]}},
            "actions": {"route_source": {"capability": "route_control", "inputs": {"source": "source.status"}}},
            "policies": [{"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "route_source"}}],
            "invariants": [{"name": "safe_routing", "when": 'source.status == "live" && source.latency_ms < 120'}],
        }
    )

    inspection = system.inspect()
    assert inspection["summary"]["policy_count"] == 1
    assert inspection["entities"][0]["name"] == "source"
    assert inspection["invariants"][0]["name"] == "safe_routing"

    results = system.simulate([
        {"source": {"status": "live", "latency_ms": 42}},
        {"source": {"status": "offline", "latency_ms": 42}},
    ])
    assert results["passed"][0]["decision"]["status"] == "allow"
    assert results["failed"][0]["index"] == 1


def test_morph_ir_can_plan_semantic_changes():
    from morph import MORPHIR

    baseline = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
            "policies": [{"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}}],
        }
    )

    blocked = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.1",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [{"name": "live_route", "when": 'source.status == "ready" && source.latency_ms < 120'}],
            "policies": [{"name": "allow_ready", "when": 'source.status == "ready" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}}],
        }
    )

    plan = baseline.plan_change(blocked)
    assert plan["status"] == "blocked"
    assert "live_route" in plan["diff"]["blocked"]

    safe = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.7.1",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "intent": {"summary": "Only live sources with low latency can route."},
            "invariants": [
                {"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'},
                {"name": "source_is_known", "when": 'has(source.status)'}
            ],
            "policies": [
                {"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}},
                {"name": "check_known", "when": 'has(source.status)', "result": {"status": "allow", "action": "ok"}},
            ],
        }
    )

    safe_plan = baseline.plan_change(safe)
    assert safe_plan["status"] == "safe"
    assert safe_plan["diff"]["added"] == ["source_is_known"]
    assert any(change["kind"] == "invariant" and change["action"] == "add" and change["name"] == "source_is_known" for change in safe_plan["changes"])

    blocked_plan = baseline.plan_change(blocked)
    assert blocked_plan["status"] == "blocked"
    assert any(change["kind"] == "invariant" and change["action"] == "rewrite" for change in blocked_plan["changes"])


def test_morph_ir_can_map_semantics_and_compute_impact():
    from morph import MORPHIR

    system = MORPHIR.from_dict(
        {
            "name": "parking",
            "version": "0.9.0",
            "entities": [{"name": "ParkingSession", "fields": {"arrival_time": "int", "status": "string"}}],
            "policies": [{
                "name": "grace_period",
                "when": 'ParkingSession.status == "active" && ParkingSession.arrival_time < 30',
                "depends_on": ["ParkingSession.arrival_time", "ParkingSession.status"],
                "affects": ["enforcement decision", "ParkingSession.status"],
                "result": {"status": "allow", "action": "evaluate"},
            }],
            "capabilities": {"enforce": {"requires": ["ParkingSession.status"], "affects": ["ParkingSession.status"]}},
            "actions": {"evaluate": {"capability": "enforce", "inputs": {"session": "ParkingSession"}}},
            "invariants": [{"name": "permit_exempt", "when": 'ParkingSession.status == "active"'}],
        }
    )

    semantic_map = system.semantic_map()
    assert semantic_map["entities"]["ParkingSession"]["policies"] == ["grace_period"]
    assert semantic_map["policies"]["grace_period"]["depends_on"] == ["ParkingSession.arrival_time", "ParkingSession.status"]

    impact = system.impact("ParkingSession.status")
    assert impact["subjects"] == ["ParkingSession.status"]
    assert "grace_period" in impact["policies"]
    assert "enforce" in impact["capabilities"]

    equivalent = system.equivalent_to(system)
    assert equivalent is True


def test_impact_uses_exact_paths_and_follows_affected_policies_to_their_effects():
    from morph import load_system_definition
    from morph.examples.crosspoint import DEFINITION_PATH

    crosspoint = load_system_definition(DEFINITION_PATH)

    latency = crosspoint.impact("source.latency_ms")
    assert latency["policies"] == ["allow_live_route"]
    assert latency["actions"] == ["route_source"]
    assert latency["capabilities"] == ["route_control"]

    # A prefix of a field name is not the field.
    near_miss = crosspoint.impact("route.lock")
    assert near_miss["policies"] == [] and near_miss["actions"] == [] and near_miss["capabilities"] == []

    # A whole entity matches every field read beneath it.
    assert crosspoint.impact("route")["policies"] == ["deny_locked_route"]

    # Capabilities are subjects too.
    assert crosspoint.impact("route_control")["policies"] == ["allow_live_route"]

    # The destination state machine reads its own state, so it is an affected entity.
    assert "destination" in crosspoint.impact("destination.state")["entities"]


def test_semantic_map_links_entities_from_compiled_paths_only():
    from morph import load_system_definition
    from morph.examples.crosspoint import DEFINITION_PATH

    semantic_map = load_system_definition(DEFINITION_PATH).semantic_map()

    assert set(semantic_map["entities"]) == {"source", "destination", "route", "operator"}
    assert semantic_map["entities"]["source"]["policies"] == ["allow_live_route"]
    assert semantic_map["entities"]["source"]["actions"] == ["route_source", "raise_alert"]
    assert semantic_map["entities"]["operator"]["capabilities"] == ["route_control"]
    assert semantic_map["actions"]["route_source"]["reads"] == ["destination.id", "operator.id", "source.id"]


def test_capability_affects_annotation_is_accepted_by_the_runtime():
    from morph import MORPHRuntime

    runtime = MORPHRuntime.from_dict(
        {
            "entities": [{"name": "session", "fields": {"status": "string"}}],
            "capabilities": {"enforce": {"requires": [], "affects": ["session.status"], "inputs": {"status": "string"}}},
            "actions": {"evaluate": {"capability": "enforce", "inputs": {"status": "session.status"}}},
            "policies": [{"name": "p", "when": 'session.status == "active"', "result": {"status": "allow", "action": "evaluate"}}],
        }
    )
    assert runtime.capability_specs["enforce"].affects == ["session.status"]

    with pytest.raises(ValueError, match="affects must be a list"):
        MORPHRuntime.from_dict(
            {
                "capabilities": {"enforce": {"affects": "session.status"}},
                "policies": [{"name": "p", "when": "true", "result": {"status": "deny", "action": "x"}}],
            }
        )


def test_morph_ir_can_classify_semantic_equivalence_and_emit_a_proposal():
    from morph import MORPHIR

    baseline = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.9.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
            "policies": [{"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}}],
        }
    )

    equivalent = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.9.0",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "invariants": [{"name": "live_route", "when": 'source.latency_ms < 120 && source.status == "live"'}],
            "policies": [{"name": "allow_live", "when": 'source.latency_ms < 120 && source.status == "live"', "result": {"status": "allow", "action": "ok"}}],
        }
    )
    broader = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.9.1",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "invariants": [{"name": "live_route", "when": 'source.status == "live" || source.status == "ready"'}],
        }
    )
    conflicting = MORPHIR.from_dict(
        {
            "name": "studio",
            "version": "0.9.2",
            "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
            "invariants": [{"name": "live_route", "when": 'source.status == "faulted"'}],
        }
    )

    assert baseline.classify_equivalence(equivalent) == "EQUIVALENT"
    assert baseline.classify_equivalence(broader) == "BROADER"
    assert baseline.classify_equivalence(conflicting) == "CONFLICTING"
    assert baseline.diff(equivalent) == {"preserved": ["live_route"], "added": [], "blocked": []}
    assert baseline.plan_change(equivalent)["status"] == "safe"

    proposal = baseline.propose({
        "name": "studio",
        "version": "0.9.1",
        "entities": [{"name": "source", "fields": {"status": "string", "latency_ms": "int"}}],
        "invariants": [{"name": "live_route", "when": 'source.status == "live" && source.latency_ms < 120'}],
        "policies": [{"name": "allow_live", "when": 'source.status == "live" && source.latency_ms < 120', "result": {"status": "allow", "action": "ok"}}],
    })

    assert proposal["status"] == "safe_to_review"
    assert proposal["equivalence"] == "IDENTICAL"
    assert proposal["changes"]


def test_semantic_reasoner_proves_typed_relationships_and_preserves_unknown():
    from morph import MORPHIR, SemanticReasoner

    reasoner = SemanticReasoner()
    types = {"a": "bool", "b": "bool", "x": "int", "floating": "double", "source.status": "string"}

    commutative = reasoner.analyze("a && b", "b && a", types=types)
    integer_boundary = reasoner.analyze("x > 10", "x >= 11", types=types)
    floating_boundary = reasoner.analyze("floating > 10", "floating >= 11", types=types)
    membership = reasoner.analyze('source.status == "live"', 'source.status in ["live"]', types=types)
    conflict = reasoner.analyze('source.status == "live"', 'source.status == "ready"', types=types)
    narrower = reasoner.analyze("x > 10", "x > 20", types=types)
    optional_field = reasoner.analyze("a", "a && (b || !b)", types=types)
    unknown = reasoner.analyze("external.check(source.status)", 'source.status == "live"', types=types)

    assert commutative["relationship"] == "equivalent"
    assert commutative["confidence"] == "proven"
    assert integer_boundary["relationship"] == "equivalent"
    assert integer_boundary["confidence"] == "proven"
    assert floating_boundary["relationship"] == "narrower"
    assert membership["relationship"] == "equivalent"
    assert conflict["relationship"] == "conflicting"
    assert conflict["confidence"] == "proven"
    assert narrower["relationship"] == "narrower"
    assert narrower["confidence"] == "proven"
    assert optional_field["relationship"] == "narrower"
    assert optional_field["confidence"] == "proven"
    assert unknown["relationship"] == "unknown"
    assert unknown["confidence"] == "unknown"
    assert unknown["reason"]

    integer_baseline = MORPHIR.from_dict({
        "entities": [{"name": "session", "fields": {"minutes": "int"}}],
        "invariants": [{"name": "grace", "when": "session.minutes > 10"}],
    })
    integer_candidate = MORPHIR.from_dict({
        "entities": [{"name": "session", "fields": {"minutes": "int"}}],
        "invariants": [{"name": "grace", "when": "session.minutes >= 11"}],
    })
    assert integer_baseline.diff(integer_candidate) == {"preserved": ["grace"], "added": [], "blocked": []}
