from textwrap import dedent

from morph import ExecutionPlanner, MORPHIR, load_system_definition


def test_yaml_loader_loads_ir_definition():
    yaml_text = dedent(
        """
        name: studio_control
        version: 0.4.0
        capabilities:
          route_control:
            requires:
              - operator.capabilities
        entities:
          - name: source
            state:
              status: live
          - name: destination
            state:
              status: ready
        policies:
          - name: route_allowed
            requires:
              - route_control
            when:
              - field: source.status
                equals: live
              - field: destination.status
                equals: ready
            result:
              status: allow
              action: route_source
        """
    ).strip()

    ir = load_system_definition(yaml_text)

    assert isinstance(ir, MORPHIR)
    assert ir.name == "studio_control"
    assert ir.version == "0.4.0"
    assert ir.policies[0]["name"] == "route_allowed"


def test_execution_planner_generates_route_plan():
    yaml_text = dedent(
        """
        name: studio_control
        version: 0.4.0
        capabilities:
          route_control:
            requires:
              - operator.capabilities
        policies:
          - name: route_allowed
            requires:
              - route_control
            when:
              - field: source.status
                equals: live
              - field: destination.status
                equals: ready
            result:
              status: allow
              action: route_source
        """
    ).strip()

    planner = ExecutionPlanner.from_yaml(yaml_text)
    plan = planner.plan({
        "source": {"status": "live"},
        "destination": {"status": "ready"},
        "operator": {"capabilities": ["route_control"]},
    })

    assert plan["status"] == "allow"
    assert plan["next_action"] == "route_source"
    assert plan["plan"] == ["route_source"]
