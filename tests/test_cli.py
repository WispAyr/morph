from pathlib import Path

from morph.cli import main


def test_cli_compile_yaml(tmp_path: Path) -> None:
    source = tmp_path / "route.yaml"
    source.write_text(
        """
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
""".strip(),
        encoding="utf-8",
    )

    exit_code = main(["compile", str(source), "--target", "node"])

    assert exit_code == 0
