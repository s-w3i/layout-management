"""End-to-end Tkinter workflow smoke test; run under xvfb-run."""

from __future__ import annotations

import tempfile
from pathlib import Path
import tkinter as tk
from types import SimpleNamespace

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

        # Operations tab: load/search, select and execute both swap modes, save.
        app.ops_layout_path.set(str(layout_path))
        app.load_ops_layout()
        root.update_idletasks()
        operation_rows = assigned_rows(app.ops_rows)
        assert operation_rows
        ops_rack_item = next(
            item
            for item in app.ops_canvas.find_withtag("ops_rack")
            if f"opsrack:{operation_rows[0]['rack_id']}" in app.ops_canvas.gettags(item)
        )
        app.ops_canvas.addtag_withtag("current", ops_rack_item)
        app.ops_rack_click(SimpleNamespace())
        assert app.ops_inventory_tree.get_children()
        app.ops_search.set(str(operation_rows[0]["sku"]))
        app.search_ops_sku()
        assert app.ops_highlight_rack == operation_rows[0]["rack_id"]

        app.ops_swap_mode.set("SKU slot")
        app.ops_swap_mode_changed()
        source = operation_rows[0]
        target = next(row for row in operation_rows if row["rack_id"] != source["rack_id"])
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
        first = operation_rows[0]
        second = next(
            row for row in operation_rows
            if row["handling_unit_id"] != first["handling_unit_id"]
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
