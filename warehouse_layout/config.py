"""Application paths and schema identifiers."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_PROJECT_SCHEMA = "rmf_grid_map_editor/v1"
PROJECT_SCHEMA = "rmf_grid_map_editor/v2"
LEGACY_SLOTTING_SCHEMA = "inventory_slotting_layout/v1"
SLOTTING_SCHEMA = "inventory_slotting_layout/v2"

DEFAULT_BUILDING_INPUT = PROJECT_ROOT / "resources/map/demo.building.yaml"
DEFAULT_BUILDING_OUTPUT = PROJECT_ROOT / "resources/map/v6.building.yaml"
DEFAULT_GRID_INPUT = PROJECT_ROOT / "resources/map/demo.grid.json"
DEFAULT_AFFINITY_INPUT = PROJECT_ROOT / "resources/data/Sample Data.xlsx"
DEFAULT_AFFINITY_CACHE = PROJECT_ROOT / ".cache/rmf_grid_map_editor/affinity"
DEFAULT_AFFINITY_OUTPUT = PROJECT_ROOT / "resources/data/sku_affinity"
DEFAULT_VELOCITY_INPUT = PROJECT_ROOT / "resources/data/sku_velocity_output/sku_velocity_summary.csv"
DEFAULT_CHILLED_INPUT = PROJECT_ROOT / "resources/data/demo_chilled_requirements.csv"
DEFAULT_SLOTTING_OUTPUT = PROJECT_ROOT / "resources/data/basic_slotting_layout.slotting.json"
DEFAULT_TRAFFIC_INPUT = DEFAULT_AFFINITY_INPUT
DEFAULT_TRAFFIC_CACHE = PROJECT_ROOT / ".cache/rmf_grid_map_editor/traffic"
DEFAULT_TRAFFIC_OUTPUT = PROJECT_ROOT / "resources/data/traffic_aware_layout.slotting.json"
DEFAULT_TRAFFIC_REPORT = PROJECT_ROOT / "resources/data/traffic_analysis"
DEFAULT_GLOBAL_TRAFFIC_OUTPUT = (
    PROJECT_ROOT / "resources/data/global_traffic_layout.slotting.json"
)
