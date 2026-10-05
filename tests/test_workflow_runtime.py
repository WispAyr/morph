from textwrap import dedent

from morph import WorkflowEngine, load_system_definition


def test_workflow_engine_executes_steps_in_order():
    yaml_text = dedent(
        """
        name: workflow_demo
        version: 0.1.0
        entities:
          - name: source
            state:
              status: live
        workflow:
          steps:
            - name: validate_source
              when:
                - field: source.status
                  equals: live
              then:
                action: validate
            - name: route_source
              when:
                - field: source.status
                  equals: live
              then:
                action: route_source
        policies:
          - name: validate_source
            when:
              - field: source.status
                equals: live
            result:
              status: allow
              action: validate
          - name: route_source
            when:
              - field: source.status
                equals: live
            result:
              status: allow
              action: route_source
        """
    ).strip()

    engine = WorkflowEngine.from_yaml(yaml_text)
    result = engine.execute({"source": {"status": "live"}})

    assert result["status"] == "allow"
    assert result["plan"] == ["validate", "route_source"]
    assert result["steps_executed"] == 2
