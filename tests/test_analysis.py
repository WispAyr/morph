import json

from morph import analyze
from morph.cli import main
from morph.examples.crosspoint import DEFINITION_PATH

ENTITIES = [
    {"name": "source", "fields": {"id": "string", "status": "string", "latency_ms": "int"}},
    {"name": "route", "fields": {"locked": "bool"}},
    {"name": "operator", "fields": {"id": "string", "capabilities": "list"}},
]


def _policy(name, when, status="allow", action="go", **extra):
    return {"name": name, "when": when, "result": {"status": status, "action": action}, **extra}


def _codes(report):
    return sorted(finding.code for finding in report.findings)


def test_clean_definition_has_no_errors_or_warnings():
    report = analyze(
        {
            "entities": ENTITIES,
            "policies": [
                _policy("deny_locked", "has(route.locked) && route.locked", status="deny", action="alert"),
                _policy("allow_live", 'source.status == "live" && source.latency_ms < 120'),
            ],
        },
        samples=300,
    )

    assert report.errors == []
    assert report.warnings == []
    assert report.coverage["deny_locked"]["fired"] > 0
    assert report.coverage["allow_live"]["fired"] > 0


def test_shadowed_policy_is_reported():
    report = analyze(
        {
            "entities": ENTITIES,
            "policies": [
                _policy("broad", 'source.status == "live"'),
                _policy("narrow", 'source.status == "live" && source.latency_ms < 120'),
            ],
        },
        samples=300,
    )

    [finding] = report.by_code("policy-shadowed")
    assert "'narrow' never decided" in finding.message
    assert "'broad' always came first" in finding.message
    assert finding.severity == "warning"


def test_deny_after_allow_is_reported_as_ordering_hazard():
    report = analyze(
        {
            "entities": ENTITIES,
            "policies": [
                _policy("allow_live", 'source.status == "live"'),
                _policy("deny_slow", 'source.status == "live" && source.latency_ms > 120', status="deny", action="alert"),
            ],
        },
        samples=300,
    )

    assert [finding.code for finding in report.warnings] == ["policy-shadowed", "deny-after-allow"]
    assert "move it first" in report.by_code("deny-after-allow")[0].message


def test_unreached_policy_is_a_note_not_a_warning():
    report = analyze(
        {"entities": ENTITIES, "policies": [_policy("p", 'source.status == "live" && source.status == "offline"')]},
        samples=200,
    )

    [finding] = report.by_code("policy-unreached")
    assert finding.severity == "info"
    assert report.warnings == []


def test_catch_all_not_last_and_constant_false():
    report = analyze({"policies": [_policy("all", "true"), _policy("never", "false"), _policy("after", "x == 1")]}, samples=50)

    assert "policy-catch-all-not-last" in _codes(report)
    assert "policy-never-fires" in _codes(report)
    assert "2 policies after it" in report.by_code("policy-catch-all-not-last")[0].message


def test_sampling_works_without_a_schema():
    report = analyze({"policies": [_policy("a", 'x.status == "live"'), _policy("b", "x.status == 'live' && y > 5")]}, samples=300)

    assert report.coverage["a"]["fired"] > 0
    assert report.by_code("policy-shadowed")[0].location == "policies.b"


def test_requires_participates_in_reachability():
    report = analyze(
        {
            "entities": ENTITIES,
            "capabilities": {"route_control": {"requires": ["operator.capabilities"]}},
            "policies": [
                _policy("gated", 'source.status == "live"', requires=["route_control"]),
                _policy("fallback", 'source.status == "live"', status="deny", action="alert"),
            ],
        },
        samples=300,
    )

    assert report.coverage["gated"]["fired"] > 0
    assert report.coverage["fallback"]["fired"] > 0  # reached when the capability is absent
    assert report.by_code("policy-shadowed") == []


def test_state_machine_checks():
    report = analyze(
        {
            "entities": [
                {
                    "name": "d",
                    "fields": {"id": "string"},
                    "states": ["idle", "routed", "orphan", "sink"],
                    "transitions": [
                        {"on": "go.succeeded", "from": "idle", "to": "routed"},
                        {"on": "go.succeeded", "from": "idle", "to": "sink"},
                        {"on": "go.exploded", "from": "idle", "to": "sink"},
                        {"on": "missing.succeeded", "from": "idle", "to": "sink"},
                    ],
                }
            ],
            "capabilities": {"c": {"requires": [], "inputs": {}, "outputs": {}}},
            "actions": {"go": {"capability": "c", "inputs": {}}},
            "policies": [_policy("p", "true")],
        },
        samples=0,
    )

    assert [finding.code for finding in report.errors] == ["transition-unknown-action"]
    codes = _codes(report)
    assert "state-unreachable" in codes and "'orphan'" in report.by_code("state-unreachable")[0].message
    assert "transition-shadowed" in codes and report.by_code("transition-shadowed")[0].location == "entities.d.transitions[1]"
    assert "transition-unknown-event" in codes
    assert any("'sink'" in finding.message for finding in report.by_code("state-terminal"))


def test_wiring_checks():
    report = analyze(
        {
            "entities": [{"name": "operator", "fields": {"id": "string", "capabilities": "list", "unused": "int"}}],
            "capabilities": {
                "used": {"requires": ["operator.capabilities"], "inputs": {}, "outputs": {}},
                "orphan": {"requires": [], "inputs": {}, "outputs": {}},
            },
            "actions": {
                "go": {"capability": "used", "inputs": {}},
                "idle": {"capability": "used", "inputs": {}},
            },
            "policies": [_policy("p", "has(operator.id)", action="go")],
        },
        samples=0,
    )

    assert report.by_code("capability-unused")[0].location == "capabilities.orphan"
    assert report.by_code("action-unused")[0].location == "actions.idle"
    assert "does not require it" in report.by_code("policy-missing-requires")[0].message
    assert report.by_code("field-unread")[0].message.startswith("field 'operator.unused'")
    assert not any("operator.capabilities" in finding.message for finding in report.by_code("field-unread"))


def test_crosspoint_example_is_clean():
    report = analyze(__import__("morph").load_system_definition(DEFINITION_PATH), samples=400)

    assert report.errors == []
    assert report.warnings == []
    assert all(counts["fired"] > 0 for counts in report.coverage.values()), report.coverage


def test_analysis_is_deterministic_for_a_seed():
    definition = {"entities": ENTITIES, "policies": [_policy("a", 'source.status == "live"'), _policy("b", "source.latency_ms < 10")]}

    assert analyze(definition, samples=100, seed=7).coverage == analyze(definition, samples=100, seed=7).coverage


def test_cli_check(tmp_path, capsys):
    assert main(["check", str(DEFINITION_PATH), "--samples", "200"]) == 0
    assert "0 errors, 0 warnings" in capsys.readouterr().out

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "policies:\n"
        "  - {name: all, when: 'true', result: {status: allow, action: go}}\n"
        "  - {name: never, when: 'x == 1', result: {status: deny, action: alert}}\n",
        encoding="utf-8",
    )
    assert main(["check", str(bad), "--samples", "50"]) == 0
    out = capsys.readouterr().out
    assert "policy-catch-all-not-last" in out and "warnings" in out
    assert main(["check", str(bad), "--samples", "50", "--strict"]) == 1
    capsys.readouterr()

    assert main(["check", str(bad), "--samples", "50", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["warnings"] >= 1 and report["coverage"]["all"]["fired"] > 0

    assert main(["check", str(tmp_path / "missing.yaml")]) == 1
