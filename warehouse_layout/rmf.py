"""Persistence service for editable projects and Open-RMF building YAML."""

from __future__ import annotations

import json
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

from .domain import GridProject


class FlowStyleDumper(yaml.SafeDumper):
    """Keep RMF typed parameter lists compact in exported YAML."""

    def ignore_aliases(self, data):
        return True


def _represent_sequence(dumper, data):
    flow = len(data) <= 4 and not any(isinstance(item, (dict, list)) for item in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


FlowStyleDumper.add_representer(list, _represent_sequence)


class RmfMapService:
    """Load, validate, save, and export warehouse map documents."""

    def load_building(self, path: Path) -> dict:
        with path.open("r", encoding="utf-8") as stream:
            building = yaml.safe_load(stream)
        if not isinstance(building, dict) or not building.get("levels"):
            raise ValueError("building YAML has no RMF levels")
        return building

    def export_building(self, project: GridProject, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            yaml.dump(
                project.to_building_dict(),
                stream,
                Dumper=FlowStyleDumper,
                sort_keys=False,
                width=160,
            )

    def save_project(self, project: GridProject, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(project.to_project_dict(), indent=2) + "\n", encoding="utf-8")

    def load_project(self, path: Path) -> GridProject:
        data = json.loads(path.read_text(encoding="utf-8"))
        return GridProject.from_project_dict(data)
