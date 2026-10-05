import pytest

from morph import ExpressionError, MORPHRuntime, compile_when
from morph.expressions import cel_literal, clause_to_cel, when_to_cel


def _policy(when, name="p"):
    return {"name": name, "when": when, "result": {"status": "allow", "action": "ok"}}


def test_cel_string_condition():
    runtime = MORPHRuntime.from_dict(
        {"policies": [_policy('source.status == "live" && destination.latency_ms < 120')]}
    )

    assert runtime.evaluate({"source": {"status": "live"}, "destination": {"latency_ms": 42}})["status"] == "allow"
    assert runtime.evaluate({"source": {"status": "live"}, "destination": {"latency_ms": 500}})["status"] == "deny"


def test_or_not_and_membership():
    runtime = MORPHRuntime.from_dict(
        {"policies": [_policy('(source.status == "live" || source.status == "preview") && !route.locked && "route_control" in operator.capabilities')]}
    )
    base = {"route": {"locked": False}, "operator": {"capabilities": ["route_control"]}}

    assert runtime.evaluate({**base, "source": {"status": "preview"}})["status"] == "allow"
    assert runtime.evaluate({**base, "source": {"status": "offline"}})["status"] == "deny"
    assert runtime.evaluate({**base, "source": {"status": "live"}, "route": {"locked": True}})["status"] == "deny"


def test_collection_macros():
    runtime = MORPHRuntime.from_dict({"policies": [_policy('destinations.all(d, d.status == "ready") && size(destinations) > 0')]})

    assert runtime.evaluate({"destinations": [{"status": "ready"}, {"status": "ready"}]})["status"] == "allow"
    assert runtime.evaluate({"destinations": [{"status": "ready"}, {"status": "faulted"}]})["status"] == "deny"
    assert runtime.evaluate({"destinations": []})["status"] == "deny"


def test_has_makes_optional_fields_explicit():
    runtime = MORPHRuntime.from_dict({"policies": [_policy("!has(route.locked) || !route.locked")]})

    assert runtime.evaluate({"route": {}})["status"] == "allow"
    assert runtime.evaluate({"route": {"locked": False}})["status"] == "allow"
    assert runtime.evaluate({"route": {"locked": True}})["status"] == "deny"


def test_list_of_cel_strings_is_anded():
    runtime = MORPHRuntime.from_dict({"policies": [_policy(['a == 1', 'b == 2 || b == 3'])]})

    assert runtime.evaluate({"a": 1, "b": 3})["status"] == "allow"
    assert runtime.evaluate({"a": 1, "b": 4})["status"] == "deny"


def test_mixed_clauses_and_strings():
    runtime = MORPHRuntime.from_dict({"policies": [_policy([{"field": "a", "equals": 1}, "b > 10"])]})

    assert runtime.evaluate({"a": 1, "b": 11})["status"] == "allow"
    assert runtime.evaluate({"a": 2, "b": 11})["status"] == "deny"


def test_missing_field_type_mismatch_and_non_bool_never_match():
    assert MORPHRuntime.matches("x.y == 1", {}) is False
    assert MORPHRuntime.matches("x < 120", {"x": "fast"}) is False
    assert MORPHRuntime.matches("1 + 1", {}) is False
    assert MORPHRuntime.matches("x == 1", "not a context") is False


def test_syntax_error_is_rejected_at_load():
    with pytest.raises(ValueError, match="Invalid expression"):
        MORPHRuntime.from_dict({"policies": [_policy('source.status ==')]})


def test_legacy_clause_translation():
    assert clause_to_cel({"field": "v", "gte": 10, "lt": 120}) == "v < 120 && v >= 10"
    assert clause_to_cel({"field": "route.locked", "equals": False}) == "route.locked == false"
    assert clause_to_cel({"field": "caps", "contains": "x"}) == '(type(caps) == string ? caps.contains("x") : "x" in caps)'
    assert when_to_cel([]) == "true"
    assert when_to_cel(None) == "true"
    with pytest.raises(ExpressionError):
        clause_to_cel({"field": "v"})
    with pytest.raises(ExpressionError):
        when_to_cel(42)


def test_literals_are_escaped():
    assert cel_literal('x" || true || "') == '"x\\" || true || \\""'
    assert cel_literal(None) == "null"
    assert cel_literal([1, "a", True]) == '[1, "a", true]'
    assert cel_literal({"k": 1.5}) == '{"k": 1.5}'
    assert MORPHRuntime.matches([{"field": "s", "equals": 'x" || true || "'}], {"s": "other"}) is False


def test_predicate_reports_context_paths():
    predicate = compile_when('source.status == "live" && destinations.all(d, d.status == "ready") && has(route.lock.owner) && type(x) == string && size(y) > 0')

    assert predicate.paths == {"source.status", "destinations", "route.lock.owner", "x", "y"}


def test_predicates_are_cached():
    assert compile_when('(a == 1)') is compile_when([{"field": "a", "equals": 1}])
    assert compile_when('a == 1') is compile_when('a == 1')
