import json
from pathlib import Path

from morph.cli import main

DEFINITION = """
name: crosspoint
version: 0.2.0
policies:
  - name: allow_route
    when:
      - field: route.status
        equals: ready
    result:
      status: allow
      action: route_source
""".strip()


def _write_definition(tmp_path: Path) -> Path:
    source = tmp_path / "route.yaml"
    source.write_text(DEFINITION, encoding="utf-8")
    return source


def test_cli_compile_yaml(tmp_path: Path, capsys) -> None:
    source = _write_definition(tmp_path)

    exit_code = main(["compile", str(source), "--target", "node"])

    assert exit_code == 0
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["target"] == "node"
    assert compiled["plan"][0]["when"] == '(route.status == "ready")'


def test_cli_compile_reports_missing_file(tmp_path: Path, capsys) -> None:
    exit_code = main(["compile", str(tmp_path / "nope.yaml")])

    assert exit_code == 1
    assert capsys.readouterr().out.startswith("error:")


def test_cli_compile_reports_invalid_policy(tmp_path: Path, capsys) -> None:
    source = tmp_path / "bad.yaml"
    source.write_text("policies:\n  - name: p\n    when:\n      - field: x\n    result: {status: allow, action: ok}\n", encoding="utf-8")

    exit_code = main(["compile", str(source)])

    assert exit_code == 1
    assert "must declare at least one operator" in capsys.readouterr().out


def test_cli_run_evaluates_context_from_file_and_overrides(tmp_path: Path, capsys) -> None:
    source = _write_definition(tmp_path)
    context = tmp_path / "ctx.json"
    context.write_text(json.dumps({"route": {"status": "busy"}}), encoding="utf-8")

    assert main(["run", str(source), "--context", str(context)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "deny"

    assert main(["run", str(source), "--context", str(context), "--set", "route.status=ready"]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["status"] == "allow"
    assert decision["policy"] == "allow_route"


def test_cli_init_template_validates_and_runs(tmp_path: Path, capsys) -> None:
    target = tmp_path / "demo.yaml"

    assert main(["init", str(target)]) == 0
    capsys.readouterr()
    assert main(["validate", str(target)]) == 0
    assert capsys.readouterr().out.startswith("ok: my_system 0.1.0: 2 policies, 1 entities")

    assert main(["run", str(target), "--set", "route.status=ready"]) == 0
    capsys.readouterr()
    assert main(["run", str(target), "--set", "route.status=ready", "--set", "route.locked=true"]) == 2
    assert json.loads(capsys.readouterr().out)["policy"] == "deny_locked_route"


def test_cli_validate_reports_schema_typo(tmp_path: Path, capsys) -> None:
    source = tmp_path / "typo.yaml"
    source.write_text(
        "entities:\n  - name: route\n    fields: {status: string}\n"
        "policies:\n  - name: p\n    when: route.staus == 'ready'\n    result: {status: allow, action: ok}\n",
        encoding="utf-8",
    )

    assert main(["validate", str(source)]) == 1
    assert "unknown field 'route.staus'" in capsys.readouterr().out


def test_cli_run_explain(tmp_path: Path, capsys) -> None:
    source = _write_definition(tmp_path)

    assert main(["run", str(source), "--set", "route.status=busy", "--explain"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["policies"][0]["condition_holds"] is False


def test_cli_run_parses_override_scalars(tmp_path: Path, capsys) -> None:
    source = tmp_path / "num.yaml"
    source.write_text(
        "policies:\n  - name: p\n    when:\n      - field: latency_ms\n        lt: 120\n    result: {status: allow, action: ok}\n",
        encoding="utf-8",
    )

    assert main(["run", str(source), "--set", "latency_ms=42"]) == 0
    capsys.readouterr()
    assert main(["run", str(source), "--set", "latency_ms=500"]) == 2
