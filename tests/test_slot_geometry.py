import json
import tempfile
import unittest
from pathlib import Path

from warehouse_layout.domain import GridProject, GridSpec, Marker, build_slot_geometry
from warehouse_layout.slotting_repository import SlottingLayoutRepository
from warehouse_layout.gui import GridMapEditorApp


class SlotGeometryTests(unittest.TestCase):
    def project(self, system="AMR"):
        project = GridProject(grid=GridSpec(width_m=4, length_m=3, spacing_m=2, spacing_y_m=1.5))
        project.markers[(1, 1)] = Marker("rack", "RACK_01")
        project.coordinate_overrides[(1, 1)] = (10.0, 5.0)
        project.assign_storage_buffers(system, 3, 4, rack_height_m=3.1)
        return project

    def test_centres_and_dimensions_use_outer_clearance(self):
        slots = build_slot_geometry("G1_1", 10, 5, 2, 1.5, 3.1, 3, 4)
        first, last = slots[0], slots[-1]
        self.assertAlmostEqual(first["width"], 0.475)
        self.assertAlmostEqual(first["length"], 1.4)
        self.assertAlmostEqual(first["height"], 1.0)
        self.assertAlmostEqual(first["center_x"], 9.2875)
        self.assertAlmostEqual(first["center_y"], 5.0)
        self.assertAlmostEqual(first["center_z"], 0.55)
        self.assertAlmostEqual(last["center_x"], 10.7125)
        self.assertAlmostEqual(last["center_z"], 2.55)
        self.assertAlmostEqual(first["center_x"] - first["width"] / 2, 9.05)
        self.assertAlmostEqual(last["center_x"] + last["width"] / 2, 10.95)

    def test_geometry_rejects_dimensions_that_cannot_hold_clearance(self):
        with self.assertRaisesRegex(ValueError, "grid spacing"):
            build_slot_geometry("G0_0", 0, 0, 0.1, 1, 1, 1, 1)
        with self.assertRaisesRegex(ValueError, "rack height"):
            build_slot_geometry("G0_0", 0, 0, 1, 1, 0.1, 1, 1)

    def test_renderer_builds_six_faces_around_slot_centre(self):
        slot = build_slot_geometry("G0_0", 2, 3, 1, 2, 1.1, 1, 1)[0]
        faces = GridMapEditorApp.slot_cuboid_faces(slot)
        self.assertEqual(len(faces), 6)
        self.assertTrue(all(len(face) == 4 for face in faces))
        coordinates = [point for face in faces for point in face]
        self.assertAlmostEqual((min(p[0] for p in coordinates) + max(p[0] for p in coordinates)) / 2, slot["center_x"])
        self.assertAlmostEqual((min(p[2] for p in coordinates) + max(p[2] for p in coordinates)) / 2, slot["center_z"])

    def test_renderer_cache_reuses_and_invalidates_geometry(self):
        project = self.project()
        editor = GridMapEditorApp.__new__(GridMapEditorApp)
        editor.grid_3d_cache = None
        first = editor.get_grid_3d_cache(project.storage_layout)
        second = editor.get_grid_3d_cache(project.storage_layout)
        self.assertIs(first, second)
        project.storage_layout.slots[0]["center_z"] += 0.01
        third = editor.get_grid_3d_cache(project.storage_layout)
        self.assertIsNot(first, third)

    def test_renderer_schedules_one_redraw_for_burst_of_motion(self):
        class Root:
            def __init__(self):
                self.calls = []
            def after(self, delay, callback):
                self.calls.append((delay, callback))
                return len(self.calls)
            def after_cancel(self, callback_id):
                self.calls = [call for index, call in enumerate(self.calls, 1)
                              if index != callback_id]

        editor = GridMapEditorApp.__new__(GridMapEditorApp)
        editor.root = Root()
        editor.grid_3d_quality = "idle"
        editor.grid_3d_redraw_after = None
        editor.grid_3d_idle_after = None
        editor.schedule_grid_3d_redraw(interactive=True)
        editor.schedule_grid_3d_redraw(interactive=True)
        self.assertEqual(len(editor.root.calls), 1)
        self.assertEqual(editor.root.calls[0][0], 16)
        self.assertEqual(editor.grid_3d_quality, "interactive")

    def test_amr_and_asrs_keep_existing_buffer_models(self):
        amr = self.project("AMR").storage_layout
        asrs = self.project("Mini-load ASRS").storage_layout
        self.assertEqual(len(amr.buffers), 1)
        self.assertEqual(len(asrs.buffers), 12)
        self.assertEqual(len(amr.slots), 12)
        self.assertEqual(len(asrs.slots), 12)

    def test_legacy_map_derives_height_and_slot_geometry(self):
        project = self.project()
        payload = project.to_project_dict()
        payload["storage_layout"].pop("rack_height_m")
        payload["storage_layout"].pop("clearance_m")
        payload["storage_layout"].pop("slots")
        restored = GridProject.from_project_dict(payload)
        self.assertAlmostEqual(restored.storage_layout.rack_height_m, 3.1)
        self.assertEqual(len(restored.storage_layout.slots), 12)
        self.assertAlmostEqual(restored.storage_layout.slots[0]["center_x"], 9.2875)

    def test_saved_assignments_include_slot_centres_without_changing_rack_id(self):
        project = self.project()
        rows = [{
            "sku": "A", "rack_id": "G1_1", "storage_level": 2,
            "storage_slot": 3, "assignment_status": "ASSIGNED",
            "occupied_handling_units": [{
                "handling_unit_id": "SHELF_001", "rack_id": "G1_1",
                "storage_level": 2, "storage_slot": 3,
            }],
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            SlottingLayoutRepository().save(
                rows, {}, {}, path, strategy="basic", handling_unit_type="AMR shelf",
                levels_per_rack=3, slots_per_level=4, zone_assignments={},
                storage_layout=project.storage_layout,
            )
            saved = json.loads(path.read_text())
        row = saved["assignments"][0]
        self.assertEqual(row["rack_id"], "G1_1")
        self.assertAlmostEqual(row["center_x"], 10.2375)
        self.assertAlmostEqual(row["occupied_handling_units"][0]["center_z"], 1.55)


if __name__ == "__main__":
    unittest.main()
