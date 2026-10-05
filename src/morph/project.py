from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProjectTemplate:
    name: str
    description: str
    builder: Callable[[str, Path], None]


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
    src_dir = project_dir / "src"
    package_dir = src_dir / project_name
    package_dir.mkdir(parents=True)

    (project_dir / "morph.yaml").write_text(
        f"""name: {project_name}
version: 0.1.0
policies:
  - name: default_control
    when:
      - field: system.status
        equals: ready
    result:
      status: allow
      action: handle_request
workflow:
  steps:
    - name: route_request
      when:
        - field: system.status
          equals: ready
      then:
        action: handle_request
""".strip() + "\n",
        encoding="utf-8",
    )

    (project_dir / "README.md").write_text(
        f"""# {project_name}

MORPH project scaffold generated with the `service` template.

## Quick start

```bash
python -m pip install -e .[dev]
morph compile morph.yaml --target node
```
""",
        encoding="utf-8",
    )

    (src_dir / "__init__.py").write_text("\n", encoding="utf-8")
    (package_dir / "__init__.py").write_text(
        """\"\"\"Generated MORPH service package.\"\"\"\n\n__all__ = [\"runtime\"]\n""",
        encoding="utf-8",
    )

    (package_dir / "runtime.py").write_text(
        """from __future__ import annotations\n\nfrom morph import MORPHRuntime\n\n\nclass ServiceRuntime(MORPHRuntime):\n    \"\"\"A typed runtime adapter for this generated MORPH service.\"\"\"\n\n    def __init__(self, definition):\n        super().__init__(\n            name=definition.get('name', 'service'),\n            version=definition.get('version', '0.1.0'),\n            policies=definition.get('policies', []),\n            capabilities=definition.get('capabilities', {}),\n        )\n""",
        encoding="utf-8",
    )

    (project_dir / "tests").mkdir()
    (project_dir / "tests" / "test_runtime.py").write_text(
        """from pathlib import Path\n\nfrom morph import load_system_definition\n\n\ndef test_generated_definition_loads() -> None:\n    definition = load_system_definition(Path(__file__).resolve().parents[1] / 'morph.yaml')\n    assert definition.name\n    assert definition.policies\n""",
        encoding="utf-8",
    )


ProjectScaffold.register_template(
    ProjectTemplate(
        name="service",
        description="Creates a reusable MORPH service scaffold with YAML, runtime, and tests.",
        builder=_service_template_builder,
    )
)
