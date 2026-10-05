from pathlib import Path

from morph.cli import main


def test_cli_new_creates_project_template(tmp_path: Path) -> None:
    project_dir = tmp_path / "demo_service"

    exit_code = main(["new", str(project_dir), "--template", "service"])

    assert exit_code == 0
    assert (project_dir / "morph.yaml").exists()
    assert (project_dir / "README.md").exists()
    assert (project_dir / "src").is_dir()
