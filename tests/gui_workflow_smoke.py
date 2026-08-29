"""End-to-end Tkinter workflow smoke test; run under xvfb-run."""

from __future__ import annotations

import copy
import csv
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
import tkinter as tk
from types import SimpleNamespace

from openpyxl import Workbook

import warehouse_layout.gui as gui
from warehouse_layout.global_traffic_search import (
    GlobalTrafficSearchScenario,
)


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
    assert len(app.notebook.tabs()) == 8
    assert app.notebook.tab(app.notebook.tabs()[1], "text") == "SKU Affinity"
    assert app.notebook.tab(app.notebook.tabs()[2], "text") == "Stock Requirements"
    assert app.notebook.tab(app.notebook.tabs()[4], "text") == "Interactive Slotting Layout"
    assert app.notebook.tab(app.notebook.tabs()[5], "text") == "Traffic-Aware Slotting"
    assert app.notebook.tab(app.notebook.tabs()[6], "text") == "Global Traffic Optimizer"
    assert app.grid_sidebar_canvas.cget("yscrollcommand")
    assert app.grid_sidebar_scrollbar.cget("command")
    assert app.grid_sidebar_canvas.bind("<MouseWheel>")
    assert len(app.grid_map_panes.panes()) == 2
    assert [label.cget("text") for label in app.grid_dot_legend_labels] == [
        "● Unassigned rack",
        "● Ambient rack",
        "● Chilled rack",
        "● Workstation",
    ]
    assert [label.cget("text") for label in app.slot_dot_legend_labels] == [
        "● A movement unit", "● B movement unit", "● C movement unit",
        "● Unranked unit",
        "◆ Workstation",
    ]
    assert [label.cget("text") for label in app.ops_dot_legend_labels] == [
        "● A movement unit", "● B movement unit", "● C movement unit",
        "● Unranked unit", "◆ Workstation",
    ]
    assert app.traffic_full_button.cget("text") == (
        "Run Direct C&TBSA"
    )
    assert app.traffic_initial_settings_frame.cget("text") == (
        "Warehouse and SKU constraints · Direct C&TBSA inputs"
    )
    assert app.traffic_optimization_settings_frame.cget("text") == (
        "Paper C&TBSA and static validation"
    )
    assert set(app.grid_sidebar_sections) == {
        "grid", "tools", "point", "buffers", "settings", "files",
    }
    app.toggle_grid_sidebar_section("files")
    assert not app.grid_sidebar_sections["files"]["expanded"]
    assert all(
        not widget.winfo_manager()
        for widget in app.grid_sidebar_sections["files"]["widgets"]
    )
    app.toggle_grid_sidebar_section("files")
    assert app.grid_sidebar_sections["files"]["expanded"]
    layout_canvases = (
        app.canvas,
        app.affinity_graph_canvas,
        app.slot_canvas,
        app.traffic_canvas,
        app.global_traffic_ui.canvas,
        app.ops_canvas,
    )
    assert all(canvas.bind("<ButtonPress-3>") for canvas in layout_canvases)
    assert all(canvas.bind("<B3-Motion>") for canvas in layout_canvases)
    assert all(canvas.bind("<MouseWheel>") for canvas in layout_canvases)

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)

        # Grid editor: generate, mark, edit, undo/redo, persist, and export.
        app.width.set("4")
        app.length.set("3")
        app.spacing.set("1")
        app.spacing_y.set("1.5")
        app.generate_grid()
        root.update_idletasks()
        assert (app.project.grid.columns, app.project.grid.rows) == (4, 2)
        assert app.project.attribute_catalog == []
        workflow_skus = [f"SKU_TEST_{index:02d}" for index in range(30)]
        sku_attributes_path = temp / "sku-attributes.csv"
        sku_attributes_path.write_text(
            "sku,max_item_length,max_item_width,max_item_height,max_item_weight,"
            "chilled,tablet,oversize_capable\n"
            + "".join(
                f"{sku},1,1,1,1,"
                f"{'true' if index == 3 else 'false'},"
                f"{'true' if index % 2 else 'false'},false\n"
                for index, sku in enumerate(workflow_skus)
            ),
            encoding="utf-8",
        )
        assert app.load_grid_sku_attributes(sku_attributes_path)
        root.update_idletasks()
        overlay_items = app.canvas.find_withtag("grid_demand_overlay")
        assert overlay_items
        overlay_text = "\n".join(
            app.canvas.itemcget(item, "text")
            for item in overlay_items
            if app.canvas.type(item) == "text"
        )
        assert "Calculated rack demand" in overlay_text
        assert "chilled=T" in overlay_text
        assert "racks" in overlay_text
        app.grid_sku_overlay_attribute_list.selection_clear(0, "end")
        tablet_index = list(
            app.grid_sku_overlay_attribute_list.get(0, "end")
        ).index("tablet")
        app.grid_sku_overlay_attribute_list.selection_set(tablet_index)
        assert app.apply_grid_sku_overlay_attributes()
        root.update_idletasks()
        overlay_text = "\n".join(
            app.canvas.itemcget(item, "text")
            for item in app.canvas.find_withtag("grid_demand_overlay")
            if app.canvas.type(item) == "text"
        )
        assert "tablet=T" in overlay_text
        assert "chilled=" not in overlay_text
        app.canvas_wheel_zoom(
            app.canvas, SimpleNamespace(x=120, y=100, delta=120, num=None)
        )
        app.canvas_pan_start(app.canvas, SimpleNamespace(x=40, y=40))
        app.canvas_pan_drag(app.canvas, SimpleNamespace(x=65, y=55))
        app.canvas_pan_end(app.canvas)
        root.update_idletasks()
        overlay_bounds = app.canvas.bbox("grid_demand_overlay")
        assert overlay_bounds is not None
        expected_right = app.canvas.canvasx(app.canvas.winfo_width() - 12)
        assert abs(overlay_bounds[2] - expected_right) <= 2
        assert abs(overlay_bounds[1] - app.canvas.canvasy(12)) <= 2

        def grid_event(position):
            x, y = app.screen_point(*position)
            x, y = app.canvas_viewport_point(app.canvas, x, y)
            return SimpleNamespace(x=x, y=y)

        # Click mapping must remain correct even when Tk's visible canvas
        # origin no longer matches canvas coordinate (0, 0).
        app.canvas.configure(scrollregion=(-500, -400, 900, 700))
        app.canvas.xview_moveto(0.45)
        app.canvas.yview_moveto(0.35)
        assert app.nearest_position(grid_event((1, 1))) == (1, 1)

        app.tool.set("rack_rectangle")
        app.canvas_click(grid_event((1, 1)))
        app.canvas_release(grid_event((1, 1)))
        app.canvas_click(grid_event((2, 0)))
        app.canvas_drag(grid_event((3, 1)))
        app.canvas_release(grid_event((3, 1)))
        app.tool.set("workstation")
        app.canvas_click(grid_event((4, 2)))
        app.selected = (4, 2)
        app.role.set("workstation")
        app.endpoint_id.set("WS_TEST")
        app.apply_edit()
        assert app.project.markers[(4, 2)].endpoint_id == "WS_TEST"
        assert {
            app.canvas.itemcget(item, "fill")
            for item in app.canvas.find_withtag("rack_point")
        } == {"#f3a712"}
        marker_count = len(app.project.markers)
        app.undo()
        app.redo()
        assert len(app.project.markers) == marker_count
        app.grid_storage_system.set("AMR")
        app.grid_storage_levels.set("1")
        app.grid_storage_slots.set("12")
        app.assign_grid_buffers()
        assert app.project.storage_layout is not None
        assert len(app.project.storage_layout.buffers) == 5
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

        # Grid Map Editor owns warehouse zones and all hierarchy attributes.
        app.tool.set("zone_rectangle")
        app.grid_zone_id.set("Z01")
        app.canvas_click(grid_event((0, 0)))
        app.canvas_drag(grid_event((4, 2)))
        app.canvas_release(grid_event((4, 2)))
        assert len(app.project.zone_assignments) == 5
        assert len(app.canvas.find_withtag("grid_zone_boundary")) == 1
        assert [
            app.canvas.itemcget(item, "text")
            for item in app.canvas.find_withtag("grid_zone_label")
        ] == ["Z01"]
        boundary = app.canvas.coords(
            app.canvas.find_withtag("grid_zone_boundary")[0]
        )
        badge = app.canvas.coords(
            app.canvas.find_withtag("grid_zone_label_badge")[0]
        )
        assert badge[0] == boundary[0]
        assert badge[3] < boundary[1]
        assert {
            app.canvas.itemcget(item, "fill")
            for item in app.canvas.find_withtag("rack_point")
        } == {"#d1495b"}
        assert not any(
            app.canvas.type(item) == "text"
            and app.canvas.itemcget(item, "text").startswith("RACK")
            for item in app.canvas.find_all()
        )
        warehouse_defaults = {
            "max_item_length": 30,
            "max_item_width": 20,
            "max_item_height": 18,
            "max_item_weight": 500,
        }
        for key, value in warehouse_defaults.items():
            app.grid_warehouse_capacity_values[key].set(str(value))
        assert app.apply_grid_warehouse_storage_defaults()
        assert app.project.warehouse_storage_defaults == warehouse_defaults
        assert all(
            app.project.location_attributes["Z01"][key] == value
            for key, value in warehouse_defaults.items()
        )
        temporarily_unassigned = next(iter(app.project.zone_assignments))
        saved_zone = app.project.zone_assignments.pop(temporarily_unassigned)
        partial_paths = app.prepare_grid_attribute_hierarchy(
            confirm_orphans=False
        )
        assert "Z01" in partial_paths
        app.project.zone_assignments[temporarily_unassigned] = saved_zone
        app.project.location_attributes["Z01"].update({
            "chilled": False,
            "oversize_capable": False,
            "max_item_length": 25.0,
            "max_item_width": 19.3,
            "max_item_height": 19.2,
            "max_item_weight": 465.0,
        })
        app.project.location_attributes["Z01"]["chilled"] = True
        app.redraw()
        assert {
            app.canvas.itemcget(item, "fill")
            for item in app.canvas.find_withtag("rack_point")
        } == {"#277da1"}
        app.project.location_attributes["Z01"]["chilled"] = False
        app.redraw()
        covered_demand = [
            app.canvas.itemcget(item, "text")
            for item in app.canvas.find_withtag("grid_demand_covered")
        ]
        assert any(
            "STANDARD" in text and "chilled=F" in text
            for text in covered_demand
        )
        assert all(
            app.canvas.itemcget(item, "fill") == "#173f73"
            for item in app.canvas.find_withtag("grid_demand_covered")
        )
        hierarchy_paths = app.prepare_grid_attribute_hierarchy()
        zone_paths = [path for path in hierarchy_paths if "/" not in path]
        assert all(
            key in app.project.location_attributes[zone_paths[0]]
            for key in (
                "chilled", "oversize_capable", "max_item_length", "max_item_width",
                "max_item_height", "max_item_weight",
            )
        )
        assert not app.attributes.is_oversize_location(
            app.project.location_attributes[zone_paths[0]]
        )
        assert {
            key: app.project.location_attributes[zone_paths[0]][key]
            for key in (
                "max_item_length", "max_item_width", "max_item_height",
                "max_item_weight",
            )
        } == {
            "max_item_length": 25.0,
            "max_item_width": 19.3,
            "max_item_height": 19.2,
            "max_item_weight": 465.0,
        }
        zone_state = {}
        zone_editor = gui.ZoneStorageSettingsEditor(
            root, app.attributes, zone_paths, app.project.location_attributes,
            lambda local: zone_state.update({"local": local}),
            attribute_catalog=app.project.attribute_catalog,
        )
        root.update_idletasks()
        zone_editor.tree.selection_set(zone_paths[0])
        zone_editor._selected()
        zone_editor.chilled.set(True)
        zone_editor.capacity_values["max_item_weight"].set("")
        zone_editor.update_selected()
        zone_editor.commit()
        app.apply_grid_zone_storage_settings(zone_state["local"])
        assert app.project.location_attributes[zone_paths[0]]["max_item_weight"] is None
        assert {
            app.canvas.itemcget(item, "fill")
            for item in app.canvas.find_withtag("rack_point")
        } == {"#277da1"}
        saved_attribute_state = {}

        def capture_attributes(catalog, local_values):
            saved_attribute_state["catalog"] = catalog
            saved_attribute_state["local"] = local_values

        editor = gui.HierarchyAttributeEditor(
            root,
            app.attributes,
            app.project.attribute_catalog,
            app.project.location_attributes,
            hierarchy_paths,
            capture_attributes,
        )
        root.update_idletasks()
        editor.clear_definition_form()
        editor.attribute_key.set("fish_area")
        editor.attribute_label.set("Fish area")
        editor.attribute_type.set("boolean")
        editor.attribute_match.set("exact")
        editor.attribute_hierarchy.set("4")
        editor.save_definition()
        assert "fish_area" in editor.catalog
        assert editor.catalog["fish_area"].hierarchy_level == 4

        zone_path = next(path for path in hierarchy_paths if "/" not in path)
        special_slot = next(
            path for path in hierarchy_paths if path.endswith("/L01/S01")
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
        app.apply_grid_attributes(
            saved_attribute_state["catalog"], saved_attribute_state["local"]
        )
        assert app.project.location_attributes[zone_path]["chilled"] is False
        assert app.project.location_attributes[special_slot]["chilled"] is True
        assert all(
            app.project.location_attributes[path]["max_item_weight"] == 100
            for path in bulk_slots
        )
        app.rmf_maps.save_project(app.project, project_path)

        # Inventory Slotting consumes the saved warehouse configuration and
        # keeps only SKU/strategy/output controls.
        app.slot_building_path.set(str(project_path))
        app.load_slot_building()
        root.update_idletasks()
        assert app.slot_racks
        assert app.slot_zone_assignments == app.project.zone_assignments
        assert app.slot_location_attributes[special_slot]["chilled"] is True
        assert not hasattr(app, "slot_zone_canvas")
        assert not app.slot_canvas.bind("<B1-Motion>")

        layout_path = temp / "workflow-AMR-shelf.slotting.json"
        workflow_velocity_path = temp / "workflow-velocity.csv"
        workflow_velocity_path.write_text(
            "sku,pick_frequency,velocity_class\n"
            + "".join(
                f"{sku},{100-index},A\n"
                for index, sku in enumerate(workflow_skus)
            ),
            encoding="utf-8",
        )
        app.slot_velocity_path.set(str(workflow_velocity_path))
        app.slot_output_path.set(str(layout_path))
        app.run_slotting()
        root.update_idletasks()
        assert layout_path.exists(), errors
        assert app.slot_progress_value.get() == 100
        assert app.slot_progress_text.get() == "Complete"
        assert "Viewing generated layout:" in app.slot_viewer_status.get()
        assert app.slot_canvas.find_withtag("rack")
        assert app.slot_canvas.find_withtag("slot_zone_boundary")
        assert not app.slot_canvas.find_withtag("slot_zone_label")
        assert not app.slot_canvas.find_withtag("slot_zone_label_badge")
        assert all(
            row["dynamic_address_level"] == "shelf_slot"
            for row in assigned_rows(app.slot_rows)
        )
        assert all(
            row["static_address"].count("/") == 2
            for row in assigned_rows(app.slot_rows)
        )
        saved_payload = app.layouts.load(layout_path)
        assert saved_payload["schema"] == "inventory_slotting_layout/v2"
        assert saved_payload["location_attributes"][special_slot]["chilled"] is True
        app.show_unassigned_slotting_rows([{
            "sku": "REJECTED_CHILLED",
            "assignment_status": "UNASSIGNED_NO_CHILLED_LOCATION",
            "sku_requirements": {
                "chilled": True,
                "max_item_length": 40,
                "max_item_width": 30,
                "max_item_height": 20,
                "max_item_weight": 50,
            },
            "physical_data_status": "VERIFIED",
            "compatibility_issues": [],
        }])
        rejected_values = app.slot_unassigned_tree.item(
            app.slot_unassigned_tree.get_children()[0], "values"
        )
        assert rejected_values == (
            "REJECTED_CHILLED", "Yes", "40 × 30 × 20", "50",
            "VERIFIED", "No chilled storage location",
        )

        # ABC + affinity: calculate workbook/map-specific values, expose them,
        # then accept an edited regeneration and persist both input and tuning.
        with Path(app.slot_velocity_path.get()).open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            affinity_skus = [row["sku"] for row in csv.DictReader(stream)][:3]
        affinity_velocity_path = temp / "slot-affinity-velocity.csv"
        with affinity_velocity_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "sku", "pick_frequency", "velocity_class", "req_chilled",
                    "req_max_item_length", "req_max_item_width",
                    "req_max_item_height", "req_max_item_weight",
                ),
            )
            writer.writeheader()
            for index, sku in enumerate(affinity_skus):
                writer.writerow({
                    "sku": sku,
                    "pick_frequency": 100 - index,
                    "velocity_class": "A",
                    "req_chilled": False,
                    "req_max_item_length": 1,
                    "req_max_item_width": 1,
                    "req_max_item_height": 1,
                    "req_max_item_weight": 1,
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

        # Traffic-aware tab: run C&TBSA directly from warehouse, SKU, and orders.
        app.traffic_grid_project_path.set(str(project_path))
        app.traffic_velocity_path.set(str(affinity_velocity_path))
        app.traffic_order_path.set(str(slot_affinity_path))
        app.traffic_handling_unit.set("AMR shelf")
        app.traffic_levels.set("1")
        app.traffic_slots.set("12")
        app.traffic_ctbsa_population.set("10")
        app.traffic_ctbsa_generations.set("5")
        assert app.traffic_zone_workload_enabled.get() is False
        app.traffic_zone_workload_enabled.set(True)
        app.traffic_output_path.set(str(temp / "workflow-traffic.slotting.json"))
        assert app.load_traffic_area_map()
        root.update_idletasks()
        assert app.traffic_building is not None
        assert app.traffic_storage_layout is not None
        assert app.traffic_storage_layout.to_dict() == app.project.storage_layout.to_dict()
        assert app.traffic_zone_assignments == app.project.zone_assignments
        assert app.traffic_location_attributes[special_slot]["chilled"] is True
        app.traffic_chilled_path.set("")
        assert app.traffic_canvas.find_withtag("traffic_area_rack")
        app.traffic_area_mode.set(False)
        app.draw_traffic_map()
        app.start_full_traffic_pipeline()
        deadline = time.monotonic() + 10
        while app.traffic_worker and app.traffic_worker.is_alive():
            root.update()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        app.poll_traffic_work()
        root.update_idletasks()
        assert app.traffic_result is not None, (app.traffic_status.get(), errors)
        assert app.traffic_pipeline_result.workflow_mode == "direct_ctbsa"
        assert app.traffic_pipeline_result.initial_strategy == "physical_feasibility"
        assert app.traffic_pipeline_result.pretraffic_payload["sources"][
            "grid_project_json"
        ] == str(project_path.resolve())
        assert app.traffic_ctbsa_population.get()
        assert app.traffic_ctbsa_generations.get()
        assert app.traffic_demand.fulfillment_groups > 0
        assert "Assigned SKUs" in app.traffic_assignment_summary.get()
        assert "/" in app.traffic_assignment_summary.get()
        assert "Optimized by C&TBSA" in app.traffic_assignment_summary.get()
        assert "Groups" in app.traffic_kpis.get()
        assert app.traffic_resource_tree.get_children()
        assert app.traffic_zone_tree.get_children()
        assert "Zone demand peak" in app.traffic_kpis.get()
        assert app.traffic_result.parameters["zone_workload_enabled"] is True
        app.traffic_zone_overlay.set("Normalized demand")
        app.draw_traffic_map()
        traffic_links = app.traffic_canvas.find_withtag("traffic_link")
        assert traffic_links
        assert all(
            app.traffic_canvas.itemcget(item, "arrow") in {"", "none"}
            for item in traffic_links
        )
        assert app.traffic_canvas.find_withtag("traffic_rack")
        assert app.traffic_canvas.find_withtag("traffic_legend")
        assert app.traffic_pipeline_result.grouping_metrics[
            "hard_validation_status"
        ] == "PASSED"
        # Reassigned source/destination racks remain highlighted in both views.
        app.traffic_view_mode.set("Feasibility seed")
        before_bays = {
            row["bay"] for row in app._traffic_racks_for_view().values()
        }
        app.traffic_view_mode.set("C&TBSA result")
        rack_bays = list(dict.fromkeys(
            row["bay"] for row in app._traffic_racks_for_view().values()
            if row["bay"] in before_bays
        ))
        assert len(rack_bays) >= 2
        original_relocations = app.traffic_result.relocations
        app.traffic_result.relocations = [{
            "sku": "GUI_TEST_SKU",
            "handling_unit_id": "GUI_TEST_UNIT",
            "from": rack_bays[0],
            "to": rack_bays[1],
        }]
        for traffic_view in ("Feasibility seed", "C&TBSA result"):
            app.traffic_view_mode.set(traffic_view)
            app.draw_traffic_map("GUI_TEST_UNIT")
            assert len(app.traffic_canvas.find_withtag("reassigned_rack")) >= 2
        app.traffic_result.relocations = original_relocations
        app.traffic_selected_unit = None
        app.save_traffic_layout()
        traffic_layout_path = Path(app.traffic_output_path.get())
        assert traffic_layout_path.exists()
        saved_traffic_layout = app.layouts.load(traffic_layout_path)
        assert saved_traffic_layout["traffic_configuration"]
        assert saved_traffic_layout["traffic_configuration"][
            "workflow_mode"
        ] == "direct_ctbsa"
        assert saved_traffic_layout["traffic_configuration"][
            "initial_strategy"
        ] == "physical_feasibility"
        assert saved_traffic_layout["traffic_configuration"][
            "zone_workload_enabled"
        ] is True
        assert saved_traffic_layout["traffic_analysis"]["zone_analysis"]["after"]
        assert saved_traffic_layout["sources"]["grid_project_json"] == str(
            project_path.resolve()
        )
        assert saved_traffic_layout["storage_layout"] == (
            app.project.storage_layout.to_dict()
        )
        assert len(saved_traffic_layout["buffers"]) == len(
            app.project.storage_layout.buffers
        )
        traffic_export = temp / "workflow-traffic.traffic.json"
        gui.filedialog.asksaveasfilename = lambda **_kwargs: str(traffic_export)
        app.export_traffic_report()
        assert traffic_export.exists()
        assert traffic_export.with_name("workflow-traffic_traffic_resources.csv").exists()

        # Independent global optimizer: reuse a saved layout, solve a complete
        # no-empty-buffer permutation, and preserve proof metadata.
        global_ui = app.global_traffic_ui
        global_ui.layout_path.set(str(affinity_layout_path))
        global_ui.order_path.set(str(slot_affinity_path))
        global_ui.time_limit.set("10")
        global_ui.gap_percent.set("0")
        global_ui.max_travel.set("100")
        global_output = temp / "workflow-global.slotting.json"
        global_ui.output_path.set(str(global_output))
        global_ui.start("existing_layout")
        deadline = time.monotonic() + 15
        while global_ui.worker and global_ui.worker.is_alive():
            root.update()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        global_ui.poll()
        root.update_idletasks()
        assert global_ui.result is not None, (global_ui.status.get(), errors)
        assert global_ui.result.solver["status"] in {"OPTIMAL", "FEASIBLE"}
        assert global_ui.result.output_payload[
            "global_traffic_configuration"
        ]["workflow_mode"] == "existing_layout"
        assert global_ui.solver_tree.get_children()
        assert global_ui.canvas.find_withtag("global_link")
        assert global_ui.canvas.find_withtag("global_rack")
        global_rack_colours = {
            global_ui.canvas.itemcget(item, "fill")
            for item in global_ui.canvas.find_withtag("global_rack_heat")
        }
        assert global_rack_colours != {"#eff4f5"}
        global_rack_visits = {
            next(
                tag for tag in global_ui.canvas.gettags(item)
                if tag.startswith("rack_visits:")
            )
            for item in global_ui.canvas.find_withtag("global_rack_heat")
        }
        if len(global_rack_visits) >= 2:
            assert len(global_rack_colours) >= 2
        assert all(
            any(
                tag.startswith("rack_visits:")
                for tag in global_ui.canvas.gettags(item)
            )
            for item in global_ui.canvas.find_withtag("global_rack_heat")
        )
        global_ui.show_rack_heat.set(False)
        global_ui.draw_map()
        assert {
            global_ui.canvas.itemcget(item, "fill")
            for item in global_ui.canvas.find_withtag("global_rack_heat")
        } == {"#eff4f5"}
        global_ui.show_rack_heat.set(True)
        global_ui.draw_map()
        global_ui.save()
        assert global_output.exists()
        saved_global = app.layouts.load(global_output)
        assert saved_global["strategy"] == "global_congestion_balanced"
        assert saved_global["global_traffic_configuration"]["backend"] == (
            "OR-Tools CP-SAT"
        )
        auto_output = temp / "workflow-global-auto.slotting.json"
        global_ui.output_path.set(str(auto_output))
        global_ui.search_screen_seconds.set("1")
        global_ui.search_final_seconds.set("1")
        global_ui.search_finalists.set("1")
        global_ui.search.default_scenarios = lambda: (
            GlobalTrafficSearchScenario("SMOKE", 1.0, 1.0),
        )
        global_ui.start("auto_search")
        deadline = time.monotonic() + 20
        while global_ui.worker and global_ui.worker.is_alive():
            root.update()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        global_ui.poll()
        root.update_idletasks()
        assert global_ui.search_result is not None, (
            global_ui.status.get(), errors
        )
        assert global_ui.search_tree.get_children()
        selected_trial = next(
            trial for trial in global_ui.search_result.trials
            if trial.accepted
            and trial is not global_ui.search_result.best_trial
        )
        selected_item = next(
            item for item, trial in global_ui.search_trials_by_item.items()
            if trial is selected_trial
        )
        global_ui.search_tree.selection_set(selected_item)
        global_ui.select_search_trial()
        assert global_ui.result is selected_trial.result
        global_ui.save()
        assert auto_output.exists()
        auto_report = auto_output.with_name(
            f"{auto_output.stem}_search"
        )
        assert (auto_report / "parameter_comparison.csv").exists()
        assert (auto_report / "search_summary.json").exists()
        auto_layout = app.layouts.load(auto_output)
        assert auto_layout["global_traffic_configuration"][
            "parameter_search"
        ]["selected_scenario"] == "SMOKE"
        assert auto_layout["global_traffic_configuration"][
            "parameter_search"
        ]["selected_phase"] == selected_trial.phase
        global_ui.grid_path.set(str(project_path))
        global_ui.velocity_path.set(str(affinity_velocity_path))
        global_ui.initial_strategy.set("ABC")
        global_ui.start("full_pipeline")
        deadline = time.monotonic() + 15
        while global_ui.worker and global_ui.worker.is_alive():
            root.update()
            time.sleep(0.01)
            assert time.monotonic() < deadline
        global_ui.poll()
        root.update_idletasks()
        assert global_ui.result is not None, (global_ui.status.get(), errors)
        assert global_ui.result.workflow_mode == "full_pipeline"
        assert global_ui.result.initial_strategy == "basic"
        assert global_ui.result.output_payload["sources"][
            "grid_project_json"
        ] == str(project_path.resolve())

        app.slot_location_attributes = {}
        original_open_dialog = gui.filedialog.askopenfilename
        gui.filedialog.askopenfilename = lambda **_kwargs: str(layout_path)
        try:
            app.load_interactive_slotting_layout()
        finally:
            gui.filedialog.askopenfilename = original_open_dialog
        assert "Viewing saved layout:" in app.slot_viewer_status.get()
        assert app.slot_location_attributes[special_slot]["chilled"] is True
        app.slot_history_path.set(str(slot_affinity_path))
        app.calculate_slot_movement_ranks()
        root.update_idletasks()
        assert app.slot_movement_summary["grouping"] == "Store ID + Date"
        assert app.slot_movement_summary["ranking_level"] == "shelf"
        assert app.slot_movement_by_unit
        assert "ranked at shelf level" in app.slot_movement_status.get()
        app.draw_slotting_layout()
        first_assignment = assigned_rows(app.slot_rows)[0]
        selected_bay_path = first_assignment["storage_location_address"].rsplit("/L", 1)[0]
        app.slot_location_attributes[selected_bay_path] = {"max_item_weight": 123}
        rack_item = next(
            item
            for item in app.slot_canvas.find_withtag("rack")
            if f"rack:{first_assignment['rack_id']}" in app.slot_canvas.gettags(item)
        )
        app.slot_canvas.addtag_withtag("current", rack_item)
        app.slot_rack_click(SimpleNamespace())
        zone_detail = app.slot_zone_detail.get()
        rack_detail = app.slot_rack_detail.get()
        expected_zone = (
            first_assignment["planned_zone_id"]
            if "_chill_" in first_assignment["planned_zone_id"]
            else first_assignment["zone_id"]
        )
        assert f"Zone: {expected_zone}" in zone_detail
        assert "Parent zone:" not in zone_detail
        assert (
            f"Generated storage type: {first_assignment['planned_storage_type']}"
            in zone_detail
        )
        assert "Attributes:" in zone_detail
        assert "Static grid rack" not in zone_detail
        assert "Static grid rack" in rack_detail
        assert "Shelf movement: class" in rack_detail
        assert "Store ID + Date visits" in rack_detail
        assert "Rack pick-frequency ABC:" in rack_detail
        assert "Overrides:" in rack_detail
        assert "Effective bay attributes" not in rack_detail
        old_zone = app.slot_rack_zone_name.get()
        app.slot_rack_zone_name.set("MEDICINE_ZONE")
        assert app.rename_selected_slot_zone(), errors
        renamed_payload = app.layouts.load(layout_path)
        assert old_zone not in renamed_payload["zone_assignments"].values()
        assert "MEDICINE_ZONE" in renamed_payload["zone_assignments"].values()
        assert all(
            row.get("zone_id") != old_zone
            for row in renamed_payload["assignments"]
        )
        assert "Renamed" in app.slot_rack_zone_edit_status.get()
        override_line = next(
            line for line in rack_detail.splitlines()
            if line.startswith("Overrides:")
        )
        assert "max_item_weight=123" in override_line
        assert "chilled=False" not in override_line
        assert app.slot_tree.get_children()
        slot_item = app.slot_tree.get_children()[0]
        slot_row = next(
            row
            for row in assigned_rows(app.slot_rows)
            if row["sku"] == app.slot_tree.item(slot_item, "values")[2]
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
        assert app.sku_storage_flags({
            "physical_missing_data_type": "UNKNOWN_WEIGHT",
            "physical_storage_class": "OVERWEIGHT",
        }) == "UNKNOWN WEIGHT"
        assert app.sku_storage_flags({
            "physical_missing_data_type": "UNKNOWN_SIZE",
            "physical_storage_class": "OVERSIZE",
        }) == "UNKNOWN SIZE"
        assert app.sku_storage_flags({
            "physical_missing_data_type": "NON_VOLUMETRIC_DATA",
            "physical_storage_class": "OVERSIZE_AND_OVERWEIGHT",
        }) == "NO SIZE/WEIGHT DATA"

        # Operations tab: load/search, select and execute both swap modes, save.
        app.ops_layout_path.set(str(layout_path))
        app.load_ops_layout()
        root.update_idletasks()
        app.ops_history_path.set(str(slot_affinity_path))
        app.calculate_ops_movement_ranks()
        root.update_idletasks()
        assert app.ops_movement_summary["grouping"] == "Store ID + Date"
        assert app.ops_movement_summary["ranking_level"] == "shelf"
        assert app.ops_movement_by_unit
        assert "ranked at shelf level" in app.ops_movement_status.get()
        assert len(app.ops_canvas.find_withtag("ops_zone_boundary")) == 1
        assert [
            app.ops_canvas.itemcget(item, "text")
            for item in app.ops_canvas.find_withtag("ops_zone_label")
        ] == ["Z01"]
        operation_rows = assigned_rows(app.ops_rows)
        assert operation_rows
        special_row = next(
            row for row in operation_rows
            if row["storage_location_address"] == special_slot
        )
        ambient_row = next(
            row for row in operation_rows
            if row["storage_location_address"] != special_slot
            and app.attributes.effective_attributes(
                row["storage_location_address"], app.ops_payload["location_attributes"]
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
        details_before_rack_click = app.ops_details.get()
        app.ops_rack_click(SimpleNamespace())
        assert app.ops_details.get() == details_before_rack_click
        assert app.ops_inventory_tree.get_children()
        assert app.ops_inventory_tree.heading("quantity", "text") == "Qty in rack (EA)"
        visible_rows = list(app.ops_inventory_rows.values())
        visible_quantities = app.rack_sku_quantity_totals(visible_rows)
        for item, row in app.ops_inventory_rows.items():
            values = app.ops_inventory_tree.item(item, "values")
            assert app.sku_storage_flags(row) in values
            assert values[1] == visible_quantities.get(
                (str(row.get("rack_id", "")), str(row.get("sku", ""))), ""
            )
        app.ops_search.set(str(operation_rows[0]["sku"]))
        app.search_ops_sku()
        expected_search_racks = {
            row["rack_id"] for row in operation_rows
            if row["sku"] == operation_rows[0]["sku"]
            and row["assignment_status"] == "ASSIGNED"
        }
        assert app.ops_highlight_rack is None
        assert app.ops_search_racks == expected_search_racks

        app.ops_swap_mode.set("SKU slot")
        app.ops_swap_mode_changed()
        normal_rows = [
            row for row in operation_rows
            if row["rack_id"] != special_row["rack_id"]
            and row["sku_requirements"].get("chilled") is False
        ]
        source = target = None
        for candidate_source in normal_rows:
            for candidate_target in normal_rows:
                if candidate_source["rack_id"] == candidate_target["rack_id"]:
                    continue
                try:
                    app.inventory._validate_target(
                        candidate_source,
                        candidate_target["storage_location_address"],
                        app.ops_payload["attribute_catalog"],
                        app.ops_payload["location_attributes"],
                    )
                    app.inventory._validate_target(
                        candidate_target,
                        candidate_source["storage_location_address"],
                        app.ops_payload["attribute_catalog"],
                        app.ops_payload["location_attributes"],
                    )
                except ValueError:
                    continue
                source, target = candidate_source, candidate_target
                break
            if source is not None:
                break
        assert source is not None and target is not None
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
        assert app.ops_movement_by_unit
        assert "ranked at shelf level" in app.ops_movement_status.get()

        app.ops_swap_mode.set("Whole shelf")
        app.ops_swap_mode_changed()
        operation_rows = assigned_rows(app.ops_rows)
        rows_by_unit = {}
        for row in operation_rows:
            rows_by_unit.setdefault(row["handling_unit_id"], []).append(row)
        eligible_units = [
            unit_rows for unit_rows in rows_by_unit.values()
            if all(row["rack_id"] != special_row["rack_id"] for row in unit_rows)
        ]
        first = second = None
        for first_rows in eligible_units:
            for second_rows in eligible_units:
                if first_rows is second_rows:
                    continue
                try:
                    app.inventory.swap_whole_shelf_units(
                        copy.deepcopy(operation_rows),
                        first_rows[0]["handling_unit_id"],
                        second_rows[0]["handling_unit_id"],
                        app.ops_payload["attribute_catalog"],
                        copy.deepcopy(app.ops_payload["location_attributes"]),
                    )
                except ValueError:
                    continue
                first, second = first_rows[0], second_rows[0]
                break
            if first is not None:
                break
        assert first is not None and second is not None
        first_rack_rows = [row for row in operation_rows if row["rack_id"] == first["rack_id"]]
        second_rack_rows = [row for row in operation_rows if row["rack_id"] == second["rack_id"]]
        app.select_ops_shelf_for_swap(first["rack_id"], first_rack_rows)
        app.select_ops_shelf_for_swap(second["rack_id"], second_rack_rows)
        assert len(app.ops_shelf_selection) == 2
        app.execute_ops_swap()

        saved_path = temp / "saved-operations.slotting.json"
        gui.filedialog.asksaveasfilename = lambda **_kwargs: str(saved_path)
        app.save_ops_layout()
        saved_operations = app.layouts.load(saved_path)
        assert saved_operations["operation_log"]
        assert saved_operations["sources"]["movement_order_workbook"] == str(
            slot_affinity_path.resolve()
        )

    root.destroy()
    if errors:
        raise AssertionError("; ".join(errors))
    print("GUI workflow smoke test passed")


if __name__ == "__main__":
    main()
