from morph import Compiler, PythonTarget


def test_python_target_compiles_ir_to_executable_plan():
    ir = {
        "name": "studio_control",
        "version": "0.5.0",
        "capabilities": {
            "route_control": {"requires": ["operator.capabilities"]},
        },
        "policies": [
            {
                "name": "route_allowed",
                "requires": ["route_control"],
                "when": [
                    {"field": "source.status", "equals": "live"},
                    {"field": "destination.status", "equals": "ready"},
                    {"field": "operator.capabilities", "contains": "route_control"},
                ],
                "result": {"status": "allow", "action": "route_source"},
            }
        ],
    }

    compiled = Compiler.compile(ir)

    assert compiled["target"] == "python"
    assert compiled["plan"][0]["action"] == "route_source"

    target = PythonTarget(compiled)
    result = target.execute({
        "source": {"status": "live"},
        "destination": {"status": "ready"},
        "operator": {"capabilities": ["route_control"]},
    })

    assert result["status"] == "allow"
    assert result["action"] == "route_source"
