"""End-to-end Tkinter workflow smoke test; run under xvfb-run."""

from __future__ import annotations

import csv
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
import tkinter as tk
from types import SimpleNamespace

from openpyxl import Workbook

import warehouse_layout.gui as gui


def assigned_rows(rows):
    return [row for row in rows if row.get("assignment_status") == "ASSIGNED"]


def main() -> None:
    errors = []

    def fail_dialog(title, message, **_kwargs):
        errors.append(f"{title}: {message}")

    gui.messagebox.showerror = fail_dialog
    gui.messagebox.showinfo = lambda *_args, **_kwargs: None
    gui.messagebox.askyesno = lambda *_args, **_kwargs: True

    root = tk.Tk()
    app = gui.GridMapEditorApp(root)
    root.update_idletasks()
    assert len(app.notebook.tabs()) == 4
    assert app.notebook.tab(app.notebook.tabs()[1], "text") == "SKU Affinity"

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)

        # Grid editor: generate, mark, edit, undo/redo, persist, and export.
        app.width.set("4")
        app.length.set("3")
        app.spacing.set("1")
        app.generate_grid()
        root.update_idletasks()

        def grid_event(position):
            x, y = app.screen_point(*position)
            return SimpleNamespace(x=x, y=y)

        app.tool.set("rack")
        app.canvas_click(grid_event((1, 1)))
        app.tool.set("rack_rectangle")
        app.canvas_click(grid_event((2, 0)))
        app.canvas_click(grid_event((3, 1)))
        app.tool.set("workstation")
        app.canvas_click(grid_event((4, 2)))
        app.selected = (4, 2)
        app.role.set("workstation")
        app.endpoint_id.set("WS_TEST")
        app.apply_edit()
        assert app.project.markers[(4, 2)].endpoint_id == "WS_TEST"
        marker_count = len(app.project.markers)
        app.undo()
        app.redo()
        assert len(app.project.markers) == marker_count
        project_path = temp / "workflow.grid.json"
        yaml_path = temp / "workflow.building.yaml"
        app.rmf_maps.save_project(app.project, project_path)
        app.rmf_maps.export_building(app.project, yaml_path)
        assert app.rmf_maps.load_project(project_path).to_project_dict() == app.project.to_project_dict()
        assert app.rmf_maps.load_building(yaml_path)["levels"]

        # Affinity tab: asynchronously load line orders, filter, inspect, and export.
        affinity_path = temp / "affinity-orders.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Picking"
        sheet.append(["Date", "Store ID", "Item or SKU", "Quantity (in EA)"])
        sheet.append(["2026-01-01", "STORE_1", "SKU_A", 1])
        sheet.append(["2026-01-01", "STORE_1", "SKU_B", 500])
        sheet.append(["2026-01-02", "STORE_2", "SKU_A", 2])
        sheet.append(["2026-01-02", "STORE_2", "SKU_B", 2])
        sheet.append(["2026-01-02", "STORE_3", "SKU_C", 1])
        workbook.save(affinity_path)
        app.affinity.cache_dir = temp / "affinity-cache"
        app.affinity_input_path.set(str(affinity_path))
        app.start_affinity_load()
        deadline = time.monotonic() + 10
        while app.affinity_worker and app.affinity_worker.is_alive():
            root.update()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        app.poll_affinity_load()
        root.update_idletasks()
        assert app.affinity_dataset.valid_rows == 5
        assert app.affinity_analysis.event_count == 5
        assert "SKUs 3" in app.affinity_kpis.get()
        assert app.affinity_heatmap_skus
        app.affinity_min_shared.set("1")
        app.affinity_sku_search.set("SKU_A")
        app.apply_affinity_filters()
        assert app.affinity_selected_sku is not None
        assert app.affinity_related_tree.get_children()
        related_item = app.affinity_related_tree.get_children()[0]
        app.affinity_related_tree.selection_set(related_item)
        app.affinity_related_double_click()
        assert app.affinity_sku_search.get() == "SKU_B"
        app.affinity_start_date.set("2026-01-02")
        app.affinity_end_date.set("2026-01-02")
        app.apply_affinity_filters()
        assert app.affinity_analysis.event_count == 3
        affinity_export = temp / "gui-affinity.affinity.json"
        original_save_dialog = gui.filedialog.asksaveasfilename
        gui.filedialog.asksaveasfilename = lambda **_kwargs: str(affinity_export)
        try:
            app.export_affinity()
            deadline = time.monotonic() + 10
            while app.affinity_export_worker and app.affinity_export_worker.is_alive():
                root.update()
                time.sleep(0.01)
                assert time.monotonic() < deadline
            app.poll_affinity_export()
        finally:
            gui.filedialog.asksaveasfilename = original_save_dialog
        assert affinity_export.exists()
        assert (temp / "gui-affinity_sku_store.csv").exists()
        assert (temp / "gui-affinity_sku_pairs.csv").exists()

        # Slotting tab: load/draw map, zone every rack, generate, and inspect.
        app.load_slot_building()
        root.update_idletasks()
        assert app.slot_racks
        zone_start = SimpleNamespace(x=0, y=0)
        zone_end = SimpleNamespace(
            x=max(300, app.slot_canvas.winfo_width()),
            y=max(300, app.slot_canvas.winfo_height()),
        )
        app.slot_canvas_press(zone_start)
        app.slot_canvas_drag(zone_end)
        app.slot_canvas_release(zone_end)
        assert len(app.slot_zone_assignments) == len(app.slot_racks)
        app.slot_levels.set("1")
        app.slot_slots.set("12")
        hierarchy_paths = app.prepare_slot_attribute_hierarchy()
        zone_paths = [path for path in hierarchy_paths if "/" not in path]
        assert all(
            key in app.slot_location_attributes[zone_paths[0]]
            for key in (
                "chilled", "max_item_length", "max_item_width",
                "max_item_height", "max_item_weight",
            )
        )
        assert not app.attributes.is_oversize_location(
            app.slot_location_attributes[zone_paths[0]]
        )
        assert {
            key: app.slot_location_attributes[zone_paths[0]][key]
            for key in (
                "max_item_length", "max_item_width", "max_item_height",
                "max_item_weight",
            )
        } == {
            "max_item_length": 15,
            "max_item_width": 16,
            "max_item_height": 13,
            "max_item_weight": 250,
        }
        zone_state = {}
        zone_editor = gui.ZoneStorageSettingsEditor(
            root, app.attributes, zone_paths, app.slot_location_attributes,
            lambda local: zone_state.update({"local": local}),
        )
        root.update_idletasks()
        zone_editor.tree.selection_set(zone_paths[0])
        zone_editor._selected()
        zone_editor.chilled.set(False)
        zone_editor.update_selected()
        zone_editor.commit()
        app.apply_zone_storage_settings(zone_state["local"])
        saved_attribute_state = {}

        def capture_attributes(catalog, local_values):
            saved_attribute_state["catalog"] = catalog
            saved_attribute_state["local"] = local_values

        editor = gui.HierarchyAttributeEditor(
            root,
            app.attributes,
            app.slot_attribute_catalog,
            app.slot_location_attributes,
            hierarchy_paths,
            capture_attributes,
        )
        root.update_idletasks()
        editor.clear_definition_form()
        editor.attribute_key.set("storage_class")
        editor.attribute_label.set("Storage class")
        editor.attribute_type.set("choice")
        editor.attribute_match.set("exact")
        editor.attribute_choices.set("standard, secure")
        editor.save_definition()
        assert "storage_class" in editor.catalog

        zone_path = next(path for path in hierarchy_paths if "/" not in path)
        special_rack = next(
            rack for rack in app.slot_racks if rack["distance_m"] < float("inf")
        )
        special_slot = (
            f"{special_rack['zone_id']}/{special_rack['aisle_id']}/"
            f"{special_rack['static_bay_id']}/L01/S01"
        )
        editor.hierarchy_tree.selection_set(zone_path)
        editor.assignment_attribute.set("chilled")
        editor.assignment_value.set("false")
        editor.apply_local_value()
        editor.hierarchy_tree.selection_set(special_slot)
        editor.assignment_value.set("true")
        editor.apply_local_value()
        bulk_slots = [path for path in hierarchy_paths if path.endswith(("/S01", "/S02"))][:2]
        editor.hierarchy_tree.selection_set(bulk_slots)
        editor.assignment_attribute.set("max_item_weight")
        editor.assignment_value.set("100")
        editor.apply_local_value()
        editor.commit()
        app.apply_slot_attributes(
            saved_attribute_state["catalog"], saved_attribute_state["local"]
        )
        assert app.slot_location_attributes[zone_path]["chilled"] is False
        assert app.slot_location_attributes[special_slot]["chilled"] is True
        assert all(
            app.slot_location_attributes[path]["max_item_weight"] == 100
            for path in bulk_slots
        )

        generated_layouts = {}
        for handling_unit in ("Tote", "Pallet", "AMR shelf"):
            layout_path = temp / f"workflow-{handling_unit.replace(' ', '-')}.slotting.json"
            app.slot_handling_unit.set(handling_unit)
            app.slot_output_path.set(str(layout_path))
            app.run_slotting()
            root.update_idletasks()
            assert layout_path.exists()
            expected_layer = "bay" if handling_unit == "AMR shelf" else "slot"
            assert all(
                row["dynamic_address_level"] == expected_layer
                for row in assigned_rows(app.slot_rows)
            )
            generated_layouts[handling_unit] = layout_path
        layout_path = generated_layouts["AMR shelf"]
        saved_payload = app.layouts.load(layout_path)
        assert saved_payload["schema"] == "inventory_slotting_layout/v2"
        assert saved_payload["location_attributes"][special_slot]["chilled"] is True

        # ABC + affinity: calculate workbook/map-specific values, expose them,
        # then accept an edited regeneration and persist both input and tuning.
        with Path(app.slot_velocity_path.get()).open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            affinity_skus = [row["sku"] for row in csv.DictReader(stream)][:3]
        affinity_velocity_path = temp / "slot-affinity-velocity.csv"
        with affinity_velocity_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=("sku", "pick_frequency", "velocity_class")
            )
            writer.writeheader()
            for index, sku in enumerate(affinity_skus):
                writer.writerow({
                    "sku": sku,
                    "pick_frequency": 100 - index,
                    "velocity_class": "A",
                })
        slot_affinity_path = temp / "slot-affinity-orders.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Date", "Store ID", "Item or SKU"])
        picked_date = date(2026, 2, 1)
        for store, pair, count in (
            ("STORE_AB", affinity_skus[:2], 5),
            ("STORE_AC", (affinity_skus[0], affinity_skus[2]), 2),
            ("STORE_BC", affinity_skus[1:], 1),
        ):
            for _ in range(count):
                for sku in pair:
                    sheet.append([picked_date, store, sku])
                picked_date += timedelta(days=1)
        workbook.save(slot_affinity_path)
        affinity_layout_path = temp / "workflow-affinity.slotting.json"
        app.slot_strategy.set("abc_affinity")
        app.slot_strategy_changed()
        app.slot_velocity_path.set(str(affinity_velocity_path))
        app.slot_chilled_path.set("")
        app.slot_affinity_path.set(str(slot_affinity_path))
        app.slot_affinity_weight.set("60")
        app.slot_output_path.set(str(affinity_layout_path))
        app.run_slotting()
        root.update_idletasks()
        assert affinity_layout_path.exists()
        assert app.slot_affinity_recommendation["parameter_status"] == "AUTO_SUGGESTED"
        assert app.slot_affinity_min_shared.get()
        automatic_payload = app.layouts.load(affinity_layout_path)
        assert automatic_payload["sources"]["affinity_order_workbook"] == str(
            slot_affinity_path.resolve()
        )
        assert automatic_payload["affinity_configuration"]["parameter_status"] == "AUTO_SUGGESTED"
        app.slot_affinity_max_service.set("0")
        app.slot_affinity_min_shared.set("1")
        app.slot_affinity_min_score.set("0")
        app.run_slotting(use_adjusted=True)
        adjusted_payload = app.layouts.load(affinity_layout_path)
        assert adjusted_payload["affinity_configuration"]["parameter_status"] == "USER_ADJUSTED"
        assert adjusted_payload["affinity_configuration"]["minimum_shared_store_days"] == 1

        app.slot_location_attributes = {}
        app.load_slotting_configuration(layout_path)
        assert app.slot_location_attributes[special_slot]["chilled"] is True
        app.draw_slotting_layout()
        first_assignment = assigned_rows(app.slot_rows)[0]
        rack_item = next(
            item
            for item in app.slot_canvas.find_withtag("rack")
            if f"rack:{first_assignment['rack_id']}" in app.slot_canvas.gettags(item)
        )
        app.slot_canvas.addtag_withtag("current", rack_item)
        app.slot_rack_click(SimpleNamespace())
        assert app.slot_tree.get_children()
        slot_item = app.slot_tree.get_children()[0]
        slot_row = next(
            row
            for row in assigned_rows(app.slot_rows)
            if row["sku"] == app.slot_tree.item(slot_item, "values")[1]
        )
        assert app.sku_storage_flags(slot_row) in app.slot_tree.item(
            slot_item, "values"
        )
        assert app.sku_storage_flags(
            {
                "sku_requirements": {"chilled": True},
                "physical_storage_class": "OVERSIZE_AND_OVERWEIGHT",
            }
        ) == "CHILLED · OVERSIZE · OVERWEIGHT"
        assert app.sku_storage_flags(
            {"physical_storage_class": "UNVERIFIED_OVERSIZE"}
        ) == "UNVERIFIED OVERSIZE"

        # Operations tab: load/search, select and execute both swap modes, save.
        app.ops_layout_path.set(str(layout_path))
        app.load_ops_layout()
        root.update_idletasks()
        operation_rows = assigned_rows(app.ops_rows)
        assert operation_rows
        special_row = next(
            row for row in operation_rows if row["static_address"] == special_slot
        )
        ambient_row = next(
            row for row in operation_rows
            if row["static_address"] != special_slot
            and app.attributes.effective_attributes(
                row["static_address"], app.ops_payload["location_attributes"]
            )[0].get("chilled") is False
        )
        special_requirements = dict(special_row["sku_requirements"])
        ambient_requirements = dict(ambient_row["sku_requirements"])
        special_row["sku_requirements"] = {"chilled": True}
        ambient_row["sku_requirements"] = {"chilled": False}
        special_before = special_row["static_address"]
        ambient_before = ambient_row["static_address"]
        error_count = len(errors)
        assert not app.perform_ops_swap(
            str(special_row["sku"]), str(ambient_row["sku"]), "SKU slot"
        )
        assert special_row["static_address"] == special_before
        assert ambient_row["static_address"] == ambient_before
        assert len(errors) == error_count + 1
        errors.pop()
        special_row["sku_requirements"] = special_requirements
        ambient_row["sku_requirements"] = ambient_requirements
        ops_rack_item = next(
            item
            for item in app.ops_canvas.find_withtag("ops_rack")
            if f"opsrack:{operation_rows[0]['rack_id']}" in app.ops_canvas.gettags(item)
        )
        app.ops_canvas.addtag_withtag("current", ops_rack_item)
        app.ops_rack_click(SimpleNamespace())
        assert app.ops_inventory_tree.get_children()
        for item, row in app.ops_inventory_rows.items():
            assert app.sku_storage_flags(row) in app.ops_inventory_tree.item(
                item, "values"
            )
        app.ops_search.set(str(operation_rows[0]["sku"]))
        app.search_ops_sku()
        assert app.ops_highlight_rack == operation_rows[0]["rack_id"]

        app.ops_swap_mode.set("SKU slot")
        app.ops_swap_mode_changed()
        normal_rows = [
            row for row in operation_rows
            if row["rack_id"] != special_row["rack_id"]
            and row["sku_requirements"].get("chilled") is False
        ]
        source = normal_rows[0]
        target = next(row for row in normal_rows if row["rack_id"] != source["rack_id"])
        app.show_ops_rack_inventory(source["rack_id"])
        source_item = next(
            item for item, row in app.ops_inventory_rows.items() if row["sku"] == source["sku"]
        )
        app.ops_inventory_tree.selection_set(source_item)
        app.ops_inventory_select()
        app.show_ops_rack_inventory(target["rack_id"])
        target_item = next(
            item for item, row in app.ops_inventory_rows.items() if row["sku"] == target["sku"]
        )
        app.ops_inventory_tree.selection_set(target_item)
        app.ops_inventory_select()
        source_address = source["static_address"]
        target_address = target["static_address"]
        app.execute_ops_swap()
        assert source["static_address"] == target_address
        assert target["static_address"] == source_address

        app.ops_swap_mode.set("Whole shelf")
        app.ops_swap_mode_changed()
        operation_rows = assigned_rows(app.ops_rows)
        first = next(
            row for row in operation_rows if row["rack_id"] != special_row["rack_id"]
        )
        second = next(
            row for row in operation_rows
            if row["handling_unit_id"] != first["handling_unit_id"]
            and row["rack_id"] != special_row["rack_id"]
        )
        first_rack_rows = [row for row in operation_rows if row["rack_id"] == first["rack_id"]]
        second_rack_rows = [row for row in operation_rows if row["rack_id"] == second["rack_id"]]
        app.select_ops_shelf_for_swap(first["rack_id"], first_rack_rows)
        app.select_ops_shelf_for_swap(second["rack_id"], second_rack_rows)
        assert len(app.ops_shelf_selection) == 2
        app.execute_ops_swap()

        saved_path = temp / "saved-operations.slotting.json"
        gui.filedialog.asksaveasfilename = lambda **_kwargs: str(saved_path)
        app.save_ops_layout()
        assert app.layouts.load(saved_path)["operation_log"]

    root.destroy()
    if errors:
        raise AssertionError("; ".join(errors))
    print("GUI workflow smoke test passed")


if __name__ == "__main__":
    main()
