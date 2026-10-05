import subprocess
import sys
from pathlib import Path

from morph.cli import main
from morph.project import package_name_for


def test_cli_new_creates_project_template(tmp_path: Path) -> None:
    project_dir = tmp_path / "demo_service"

    exit_code = main(["new", str(project_dir), "--template", "service"])

    assert exit_code == 0
    assert (project_dir / "morph.yaml").exists()
    assert (project_dir / "README.md").exists()
    assert (project_dir / "pyproject.toml").exists()
    assert (project_dir / "src" / "demo_service" / "runtime.py").exists()


def test_package_name_is_sanitised() -> None:
    assert package_name_for("my-service") == "my_service"
    assert package_name_for("1st Project!") == "_1st_project"
    assert package_name_for("---") == "service"


def test_cli_new_refuses_existing_path(tmp_path: Path, capsys) -> None:
    project_dir = tmp_path / "exists"
    project_dir.mkdir()

    assert main(["new", str(project_dir)]) == 1
    assert "already exists" in capsys.readouterr().out


def test_generated_project_tests_pass(tmp_path: Path) -> None:
    project_dir = tmp_path / "gen-svc"
    assert main(["new", str(project_dir)]) == 0

    morph_src = Path(__file__).resolve().parents[1] / "src"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=project_dir,
        env={"PYTHONPATH": f"{project_dir / 'src'}:{morph_src}", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
