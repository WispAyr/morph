from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProjectTemplate:
    name: str
    description: str
    builder: Callable[[str, Path], None]


def package_name_for(project_name: str) -> str:
    """Turn a directory name into a valid Python package name."""
    name = re.sub(r"\W+", "_", project_name).strip("_").lower()
    if not name:
        name = "service"
    if name[0].isdigit():
        name = f"_{name}"
    return name


class ProjectScaffold:
    """Create a starter MORPH project from a template registry."""

    _REGISTRY: dict[str, ProjectTemplate] = {}

    @classmethod
    def register_template(cls, template: ProjectTemplate) -> None:
        cls._REGISTRY[template.name] = template

    @classmethod
    def list_templates(cls) -> list[str]:
        return sorted(cls._REGISTRY)

    @classmethod
    def generate(cls, target: str | Path, template_name: str = "service") -> Path:
        project_dir = Path(target)
        template = cls._REGISTRY.get(template_name)
        if template is None:
            raise ValueError(f"Unknown template '{template_name}'. Available templates: {cls.list_templates()}")

        if project_dir.exists():
            raise FileExistsError(f"Project path '{project_dir}' already exists.")

        project_dir.mkdir(parents=True, exist_ok=False)
        template.builder(project_dir.name, project_dir)
        return project_dir


def _service_template_builder(project_name: str, project_dir: Path) -> None:
    package = package_name_for(project_name)
    src_dir = project_dir / "src"
    package_dir = src_dir / package
    package_dir.mkdir(parents=True)

    (project_dir / "morph.yaml").write_text(
        f"""name: {package}
version: 0.1.0

entities:
  - name: system
    fields:
      status: string
      load: double

policies:
  - name: shed_load
    when: has(system.load) && system.load > 0.9
    result:
      status: deny
      action: raise_alert
  - name: default_control
    when: system.status == "ready"
    result:
      status: allow
      action: handle_request

workflow:
  steps:
    - name: route_request
      when: system.status == "ready"
      then:
        action: handle_request
""",
        encoding="utf-8",
    )

    (project_dir / "pyproject.toml").write_text(
        f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{package}"
version = "0.1.0"
description = "MORPH service generated from the 'service' template"
requires-python = ">=3.11"
dependencies = [
  "morph",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0.0",
]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
""",
        encoding="utf-8",
    )

    (project_dir / "README.md").write_text(
        f"""# {project_name}

MORPH project scaffold generated with the `service` template.

## Quick start

```bash
python -m pip install -e .[dev]
pytest -q
morph validate morph.yaml
morph run morph.yaml --set system.status=ready
morph run morph.yaml --set system.status=ready --set system.load=0.95 --explain
morph compile morph.yaml --target node
```
""",
        encoding="utf-8",
    )

    (project_dir / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.venv/\n*.pyc\n*.egg-info/\n", encoding="utf-8")

    (package_dir / "__init__.py").write_text(
        '"""Generated MORPH service package."""\n\nfrom .runtime import ServiceRuntime\n\n__all__ = ["ServiceRuntime"]\n',
        encoding="utf-8",
    )

    (package_dir / "runtime.py").write_text(
        '''from __future__ import annotations

from pathlib import Path

from morph import MORPHRuntime, load_system_definition

DEFINITION_PATH = Path(__file__).resolve().parents[2] / "morph.yaml"


class ServiceRuntime(MORPHRuntime):
    """A typed runtime adapter for this generated MORPH service."""

    @classmethod
    def load(cls, path: Path = DEFINITION_PATH) -> "ServiceRuntime":
        definition = load_system_definition(path)
        return cls(
            name=definition.name,
            version=definition.version,
            policies=definition.policies,
            capabilities=definition.capabilities,
            entities=definition.entities,
        )
''',
        encoding="utf-8",
    )

    (project_dir / "tests").mkdir()
    (project_dir / "tests" / "test_runtime.py").write_text(
        f'''from {package} import ServiceRuntime


def test_generated_definition_allows_ready_system() -> None:
    runtime = ServiceRuntime.load()

    assert runtime.evaluate({{"system": {{"status": "ready"}}}})["status"] == "allow"
    assert runtime.evaluate({{"system": {{"status": "down"}}}})["status"] == "deny"
    assert runtime.evaluate({{"system": {{"status": "ready", "load": 0.95}}}})["policy"] == "shed_load"
''',
        encoding="utf-8",
    )


ProjectScaffold.register_template(
    ProjectTemplate(
        name="service",
        description="Creates a reusable MORPH service scaffold with YAML, runtime, and tests.",
        builder=_service_template_builder,
    )
)
