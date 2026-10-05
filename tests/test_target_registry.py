from morph import Compiler


class CustomTarget:
    def __init__(self, compiled):
        self.compiled = compiled

    def execute(self, context):
        return {
            "status": "allow",
            "action": "custom_route",
            "name": self.compiled["name"],
            "version": self.compiled["version"],
        }


def test_compiler_target_registry_supports_custom_target() -> None:
    Compiler.register_target("custom", CustomTarget)

    compiled = Compiler.compile(
        {
            "name": "demo",
            "version": "0.2.0",
            "policies": [
                {
                    "name": "allow_route",
                    "when": [{"field": "route.status", "equals": "ready"}],
                    "result": {"status": "allow", "action": "custom_route"},
                }
            ],
        },
        target="custom",
    )

    assert compiled["target"] == "custom"
    assert compiled["plan"][0]["action"] == "custom_route"
    assert "custom" in Compiler.list_targets()
