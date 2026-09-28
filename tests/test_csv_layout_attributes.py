import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from warehouse_layout.attributes import StorageAttributeService, PHYSICAL_ATTRIBUTE_KEYS
from warehouse_layout.domain import GridProject
from warehouse_layout.gui import GridMapEditorApp
from warehouse_layout.slotting import SlottingService


SOURCE = Path(__file__).resolve().parents[1] / "resources/data/medicine_sku_attributes.csv"


class CsvLayoutAttributesTests(unittest.TestCase):
    def test_empty_project_and_roundtrip(self):
        project = GridProject()
        self.assertEqual([item["key"] for item in project.resolve_attribute_catalog()], ["oversize_capable"])
        saved = project.to_project_dict()
        self.assertNotIn("attribute_catalog", saved)
        self.assertEqual(GridProject.from_project_dict(saved).active_sku_attributes, [])
        self.assertEqual(project.location_attributes, {})

    def test_import_and_replacement_preserve_exclusions_without_defaults(self):
        class Value:
            def set(self, value):
                pass
        project = GridProject()
        editor = SimpleNamespace(project=project, attributes=StorageAttributeService(), slotting=SlottingService(),
                                 push_undo=lambda: None, grid_sku_attributes_path=Value(), grid_sku_attribute_summary=Value(),
                                 status=Value(), sync_grid_sku_overlay_attribute_list=lambda: None,
                                 format_sku_attribute_summary=lambda summary: "", redraw=lambda: None)
        self.assertTrue(GridMapEditorApp.load_grid_sku_attributes(editor, SOURCE))
        self.assertEqual(set(project.active_sku_attributes), set(PHYSICAL_ATTRIBUTE_KEYS) | {"chilled", "tablet", "flammable"})
        self.assertEqual(project.location_attributes, {})
        project.active_sku_attributes.remove("tablet")
        self.assertTrue(GridMapEditorApp.load_grid_sku_attributes(editor, SOURCE))
        self.assertNotIn("tablet", project.active_sku_attributes)
        loaded = GridProject.from_project_dict(project.to_project_dict())
        self.assertEqual(loaded.active_sku_attributes, project.active_sku_attributes)

    def test_legacy_selection_migration(self):
        _, summary = SlottingService().inspect_sku_attribute_csv(SOURCE)
        saved = GridProject().to_project_dict()
        saved.pop("active_sku_attributes")
        saved.update(sku_attribute_summary=summary, sku_overlay_attributes=["tablet"],
                     location_attributes={"Z01": {"tablet": True, "max_item_weight": 12.5}})
        project = GridProject.from_project_dict(saved)
        self.assertEqual(set(project.active_sku_attributes), set(PHYSICAL_ATTRIBUTE_KEYS) | {"tablet"})
        self.assertEqual(project.location_attributes, saved["location_attributes"])
        self.assertNotIn("attribute_catalog", project.to_project_dict())

    def test_disabled_checks_preserve_physical_classification(self):
        service = StorageAttributeService()
        catalog = service.starter_catalog()
        catalog["chilled"] = replace(catalog["chilled"], enabled=False)
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            catalog[key] = replace(catalog[key], enabled=False)
        requirements = {"chilled": True, **{key: 0.1 for key in PHYSICAL_ATTRIBUTE_KEYS}}
        effective = {"chilled": False, **{key: 0.01 for key in PHYSICAL_ATTRIBUTE_KEYS}}
        self.assertTrue(service.evaluate_location(requirements, effective, catalog)[0])
        requirements["max_item_weight"] = 100
        self.assertEqual(service.physical_profile(requirements)["storage_class"], "OVERWEIGHT")

    def test_allocation_ignores_disabled_flags_and_capacities(self):
        service = SlottingService()
        catalog = service.attributes.starter_catalog()
        building = {"levels": {"L1": {
            "vertices": [[0, 0, 0, "WS", {"dropoff_ingestor": [1, "WS"]}],
                         [1, 0, 0, "R1", {"pickup_dispenser": [1, "R1"]}]],
            "lanes": [[0, 1, {"bidirectional": [4, True]}]],
        }}}
        requirements = {"chilled": True, **{key: 0.1 for key in PHYSICAL_ATTRIBUTE_KEYS}}
        local = {"Z1": {"chilled": False, **{key: 0.01 for key in PHYSICAL_ATTRIBUTE_KEYS}}}
        sku = {"sku": "1", "velocity_class": "A", "pick_frequency": 1, "sku_requirements": requirements}
        _, rejected = service.generate_basic(building, [sku], levels_per_rack=1, slots_per_level=1,
                                             zone_assignments={"R1": "Z1"}, attribute_catalog=catalog,
                                             location_attributes=local)
        self.assertEqual(rejected["unassigned_count"], 1)
        disabled = {key: replace(item, enabled=key == "oversize_capable") for key, item in catalog.items()}
        _, accepted = service.generate_basic(building, [sku], levels_per_rack=1, slots_per_level=1,
                                             zone_assignments={"R1": "Z1"}, attribute_catalog=disabled,
                                             location_attributes=local)
        self.assertEqual(accepted["unassigned_count"], 0)
        self.assertFalse(local["Z1"]["chilled"])
        self.assertEqual(local["Z1"]["max_item_weight"], 0.01)
        oversize_sku = dict(sku, sku_requirements={**requirements, "max_item_weight": 100})
        _, oversize_rejected = service.generate_basic(
            building, [oversize_sku], levels_per_rack=1, slots_per_level=1,
            zone_assignments={"R1": "Z1"}, attribute_catalog=disabled, location_attributes=local)
        self.assertEqual(oversize_rejected["unassigned_count"], 1)


if __name__ == "__main__":
    unittest.main()
