"""Application paths and schema identifiers."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_SCHEMA = "rmf_grid_map_editor/v1"
LEGACY_SLOTTING_SCHEMA = "inventory_slotting_layout/v1"
SLOTTING_SCHEMA = "inventory_slotting_layout/v2"

DEFAULT_BUILDING_INPUT = PROJECT_ROOT / "resources/map/demo.building.yaml"
DEFAULT_BUILDING_OUTPUT = PROJECT_ROOT / "resources/map/v6.building.yaml"
DEFAULT_AFFINITY_INPUT = PROJECT_ROOT / "resources/data/Sample Data.xlsx"
DEFAULT_AFFINITY_CACHE = PROJECT_ROOT / ".cache/rmf_grid_map_editor/affinity"
DEFAULT_AFFINITY_OUTPUT = PROJECT_ROOT / "resources/data/sku_affinity"
DEFAULT_VELOCITY_INPUT = PROJECT_ROOT / "resources/data/sku_velocity_output/sku_velocity_summary.csv"
DEFAULT_CHILLED_INPUT = PROJECT_ROOT / "resources/data/demo_chilled_requirements.csv"
DEFAULT_SLOTTING_OUTPUT = PROJECT_ROOT / "resources/data/basic_slotting_layout.slotting.json"
