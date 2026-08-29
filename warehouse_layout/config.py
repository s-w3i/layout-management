"""Application paths and schema identifiers."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_PROJECT_SCHEMA = "rmf_grid_map_editor/v1"
PROJECT_SCHEMA = "rmf_grid_map_editor/v2"
LEGACY_SLOTTING_SCHEMA = "inventory_slotting_layout/v1"
SLOTTING_SCHEMA = "inventory_slotting_layout/v2"

DEFAULT_BUILDING_INPUT = PROJECT_ROOT / "resources/map/demo.building.yaml"
DEFAULT_BUILDING_OUTPUT = PROJECT_ROOT / "resources/map/v6.building.yaml"
DEFAULT_MAP_DIR = PROJECT_ROOT / "resources/map"
DEFAULT_GRID_INPUT = PROJECT_ROOT / "resources/map/map1.grid.json"
DEFAULT_AFFINITY_INPUT = PROJECT_ROOT / "resources/data/Sample Data.xlsx"
DEFAULT_AFFINITY_CACHE = PROJECT_ROOT / ".cache/rmf_grid_map_editor/affinity"
DEFAULT_AFFINITY_OUTPUT = PROJECT_ROOT / "resources/data/sku_affinity"
DEFAULT_VELOCITY_INPUT = PROJECT_ROOT / "resources/data/sku_velocity_output/sku_velocity_summary.csv"
DEFAULT_CHILLED_INPUT = PROJECT_ROOT / "resources/data/demo_chilled_requirements.csv"
DEFAULT_SKU_ATTRIBUTES_INPUT = (
    PROJECT_ROOT / "resources/data/medicine_sku_attributes.csv"
)
DEFAULT_SLOTTING_OUTPUT = PROJECT_ROOT / "resources/data/basic_slotting_layout.slotting.json"
DEFAULT_TRAFFIC_INPUT = DEFAULT_AFFINITY_INPUT
DEFAULT_TRAFFIC_CACHE = PROJECT_ROOT / ".cache/rmf_grid_map_editor/traffic"
DEFAULT_TRAFFIC_OUTPUT = PROJECT_ROOT / "resources/data/traffic_aware_layout.slotting.json"
DEFAULT_TRAFFIC_REPORT = PROJECT_ROOT / "resources/data/traffic_analysis"
DEFAULT_GLOBAL_TRAFFIC_OUTPUT = (
    PROJECT_ROOT / "resources/data/global_traffic_layout.slotting.json"
)

# Warehouse dimensions are metres and weights are kilograms. A rack contains
# three levels with four slots per level by default, so twelve fully loaded
# 12.5 kg slots exactly match the default 150 kg AMR whole-rack limit.
DEFAULT_SLOT_CAPACITY = {
    "max_item_length": 1.9,
    "max_item_width": 0.5,
    "max_item_height": 1.0,
    "max_item_weight": 12.5,
}

# The former 25 x 19.3 x 19.2 cm and 465 g slot values now describe the ASRS
# machine envelope in warehouse units. AMR uses a whole-rack limit of 150 kg.
DEFAULT_MACHINE_CAPACITY_BY_SYSTEM = {
    "AMR": {
        "max_item_length": None,
        "max_item_width": None,
        "max_item_height": None,
        "max_item_weight": 150.0,
    },
    "Mini-load ASRS": {
        "max_item_length": 0.25,
        "max_item_width": 0.193,
        "max_item_height": 0.192,
        "max_item_weight": 0.465,
    },
    "Pallet ASRS": {
        "max_item_length": 0.25,
        "max_item_width": 0.193,
        "max_item_height": 0.192,
        "max_item_weight": 0.465,
    },
}
