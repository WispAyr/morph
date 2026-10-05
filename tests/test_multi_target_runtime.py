from morph import Compiler, NodeTarget, SQLTarget


def test_compiler_supports_multiple_targets():
    ir = {
        "name": "studio_control",
        "version": "0.6.0",
        "policies": [
            {
                "name": "route_allowed",
                "when": [
                    {"field": "source.status", "equals": "live"},
                    {"field": "destination.status", "equals": "ready"},
                ],
                "result": {"status": "allow", "action": "route_source"},
            }
        ],
    }

    compiled = Compiler.compile(ir, target="node")
    assert compiled["target"] == "node"
    assert compiled["plan"][0]["action"] == "route_source"

    sql_compiled = Compiler.compile(ir, target="sql")
    assert sql_compiled["target"] == "sql"
    assert "route_source" in sql_compiled["sql"]


def test_node_target_executes_compiled_plan():
    compiled = {
        "target": "node",
        "name": "studio_control",
        "version": "0.6.0",
        "plan": [{"name": "route_allowed", "action": "route_source", "status": "allow"}],
    }

    target = NodeTarget(compiled)
    result = target.execute({"source": {"status": "live"}, "destination": {"status": "ready"}})

    assert result["status"] == "allow"
    assert result["action"] == "route_source"


def test_sql_target_compiles_to_sql_statement():
    ir = {
        "name": "studio_control",
        "version": "0.6.0",
        "policies": [
            {
                "name": "route_allowed",
                "when": [
                    {"field": "source.status", "equals": "live"},
                    {"field": "destination.status", "equals": "ready"},
                ],
                "result": {"status": "allow", "action": "route_source"},
            }
        ],
    }

    compiled = Compiler.compile(ir, target="sql")

    assert compiled["sql"] == (
        "SELECT 'allow' AS status, 'route_source' AS action "
        "WHERE source_status = 'live' AND destination_status = 'ready';"
    )
    statement = compiled["statements"][0]
    assert statement["parameterized_sql"] == (
        "SELECT 'allow' AS status, 'route_source' AS action WHERE source_status = ? AND destination_status = ?;"
    )
    assert statement["params"] == ["live", "ready"]

    target = SQLTarget(compiled)
    result = target.execute({"source": {"status": "live"}, "destination": {"status": "ready"}})

    assert result["status"] == "allow"
    assert result["action"] == "route_source"
