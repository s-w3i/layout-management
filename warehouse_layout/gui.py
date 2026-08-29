"""Tkinter user interface for map editing, slotting, and inventory demos."""

from __future__ import annotations

import copy
import json
import math
import queue
import threading
from datetime import date, datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import yaml
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from .affinity import AffinityCancelledError, AffinityService
from .attribute_editor import HierarchyAttributeEditor
from .attributes import (
    DERIVED_OVERSIZE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import (
    DEFAULT_AFFINITY_INPUT,
    DEFAULT_AFFINITY_OUTPUT,
    DEFAULT_GRID_INPUT,
    DEFAULT_BUILDING_OUTPUT,
    DEFAULT_SKU_ATTRIBUTES_INPUT,
    DEFAULT_SLOTTING_OUTPUT,
    DEFAULT_TRAFFIC_INPUT,
    DEFAULT_TRAFFIC_OUTPUT,
    DEFAULT_TRAFFIC_REPORT,
    DEFAULT_VELOCITY_INPUT,
    DEFAULT_MACHINE_CAPACITY_BY_SYSTEM,
)
from .ctbsa import CtbsaParameters
from .domain import GridPosition, GridProject, GridSpec, Marker, StorageLayout
from .inventory import InventoryService
from .global_traffic_gui import GlobalTrafficOptimizerTab
from .rmf import RmfMapService
from .slotting import SlottingLayoutRepository, SlottingService
from .storage_planning import combined_occupied_dynamic_address
from .stock_determination import (
    calculate_rack_requirements,
    determine_stock_requirements,
    write_attribute_combination_csv,
    write_stock_requirements_csv,
)
from .traffic import (
    InsufficientStorageError,
    TrafficAwareSlottingService,
    TrafficCancelledError,
)
from .zone_settings_editor import ZoneStorageSettingsEditor


class GridMapEditorApp:
    """Coordinate the desktop UI and application services."""

    @staticmethod
    def sku_storage_flags(row):
        """Return compact, operator-facing storage markers for an SKU row."""
        flags = []
        requirements = row.get("sku_requirements") or {}
        if requirements.get("chilled") is True:
            flags.append("CHILLED")

        missing_data_type = str(
            row.get("physical_missing_data_type") or ""
        ).upper()
        if missing_data_type == "UNKNOWN_WEIGHT":
            flags.append("UNKNOWN WEIGHT")
        elif missing_data_type == "UNKNOWN_SIZE":
            flags.append("UNKNOWN SIZE")
        elif missing_data_type == "NON_VOLUMETRIC_DATA":
            flags.append("NO SIZE/WEIGHT DATA")

        # A missing-data marker already explains why the SKU is handled as a
        # physical exception.  Do not also expose the conservative internal
        # OVERSIZE / OVERWEIGHT classification to operators.
        if not missing_data_type:
            physical_class = str(
                row.get("physical_storage_class") or ""
            ).upper()
            if physical_class == "OVERSIZE":
                flags.append("OVERSIZE")
            elif physical_class == "OVERWEIGHT":
                flags.append("OVERWEIGHT")
            elif physical_class == "OVERSIZE_AND_OVERWEIGHT":
                flags.extend(("OVERSIZE", "OVERWEIGHT"))
            elif physical_class == "UNVERIFIED_OVERSIZE":
                flags.append("UNVERIFIED OVERSIZE")

        return " · ".join(flags) or "STANDARD AMBIENT"

    @staticmethod
    def rack_storage_flag_counts(rows):
        """Count temperature-controlled and physical-exception SKUs in a rack."""
        chilled = sum(
            (row.get("sku_requirements") or {}).get("chilled") is True
            for row in rows
        )
        physical_exceptions = sum(
            str(row.get("physical_storage_class") or "").upper()
            in {
                "OVERSIZE",
                "OVERWEIGHT",
                "OVERSIZE_AND_OVERWEIGHT",
                "UNVERIFIED_OVERSIZE",
            }
            for row in rows
        )
        return chilled, physical_exceptions

    @staticmethod
    def occupied_dynamic_address(row):
        """Show all occupied positions while retaining canonical address data."""
        return (
            row.get("occupied_dynamic_address")
            or combined_occupied_dynamic_address(row)
            or row.get("dynamic_address", "")
        )

    def __init__(self, root, initial_project: GridProject | None = None):
        self.root = root
        self.root.title("RMF Grid Map Editor")
        self.root.geometry("1220x820")
        self.rmf_maps = RmfMapService()
        self.attributes = StorageAttributeService()
        self.affinity = AffinityService()
        self.slotting = SlottingService(self.rmf_maps, self.attributes)
        self.layouts = SlottingLayoutRepository()
        self.inventory = InventoryService(self.slotting, self.attributes)
        self.traffic = TrafficAwareSlottingService(self.attributes, self.slotting)
        self.project = initial_project or GridProject()
        self.attributes.set_standard_storage_defaults(
            self.project.warehouse_storage_defaults
        )
        self.selected: GridPosition | None = None
        self.bulk_anchor: GridPosition | None = None
        self.bulk_drag_position: GridPosition | None = None
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.drag_undo_started = False
        self.tool = tk.StringVar(value="select")
        self.rack_prefix = tk.StringVar(value="RACK")
        self.grid_zone_id = tk.StringVar(value="Z01")
        self.grid_zone_auto = tk.BooleanVar(value=True)
        self.grid_zone_summary = tk.StringVar(
            value="Assign every rack to a warehouse zone."
        )
        self.grid_warehouse_capacity_values = {
            key: tk.StringVar(value=str(self.project.warehouse_storage_defaults[key]))
            for key in PHYSICAL_ATTRIBUTE_KEYS
        }
        self.grid_machine_capacity_values = {
            key: tk.StringVar(
                value=(
                    "" if self.project.machine_carrying_capacity[key] is None
                    else str(self.project.machine_carrying_capacity[key])
                )
            )
            for key in PHYSICAL_ATTRIBUTE_KEYS
        }
        self.grid_machine_capacity_label = tk.StringVar(
            value="AMR whole-rack max weight (kg)"
        )
        self.map_name = tk.StringVar(value=self.project.grid.map_name)
        self.level_name = tk.StringVar(value=self.project.grid.level_name)
        self.width = tk.StringVar(value=str(self.project.grid.width_m))
        self.length = tk.StringVar(value=str(self.project.grid.length_m))
        self.spacing = tk.StringVar(value=str(self.project.grid.spacing_m))
        self.spacing_y = tk.StringVar(value=str(self.project.grid.spacing_y_m))
        self.selected_coordinate = tk.StringVar(value="No grid point selected")
        self.selected_x = tk.StringVar()
        self.selected_y = tk.StringVar()
        self.role = tk.StringVar(value="none")
        self.endpoint_id = tk.StringVar()
        storage_layout = self.project.storage_layout
        self.grid_storage_system = tk.StringVar(
            value=storage_layout.system_type if storage_layout else "AMR"
        )
        self.grid_storage_levels = tk.StringVar(
            value=str(storage_layout.levels_per_rack if storage_layout else 3)
        )
        self.grid_storage_slots = tk.StringVar(
            value=str(storage_layout.slots_per_level if storage_layout else 4)
        )
        self.grid_buffer_summary = tk.StringVar(
            value=(
                f"{len(storage_layout.buffers)} empty {storage_layout.buffer_level} buffer(s)"
                if storage_layout else "Storage buffers have not been assigned."
            )
        )
        self.grid_sku_attributes_path = tk.StringVar(
            value=self.project.sku_attribute_source
        )
        self.grid_sku_attribute_summary = tk.StringVar(
            value=self.format_sku_attribute_summary(
                self.project.sku_attribute_summary
            )
        )
        self.summary = tk.StringVar()
        self.status = tk.StringVar(value="Bottom-left grid point is (0, 0)")
        self.canvas_viewports = {}
        self._build_ui()
        self.sync_grid_sku_overlay_attribute_list()
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        self.root.bind_all("<Control-Shift-Z>", self.redo)
        self.redraw()

    def enable_canvas_viewport(self, canvas):
        """Enable consistent pan and zoom controls for a Tk layout canvas."""
        self.canvas_viewports.setdefault(
            canvas,
            {"scale": 1.0, "offset_x": 0.0, "offset_y": 0.0, "pan": None},
        )
        canvas.bind(
            "<ButtonPress-3>",
            lambda event, target=canvas: self.canvas_pan_start(target, event),
            add="+",
        )
        canvas.bind(
            "<B3-Motion>",
            lambda event, target=canvas: self.canvas_pan_drag(target, event),
            add="+",
        )
        canvas.bind(
            "<ButtonRelease-3>",
            lambda event, target=canvas: self.canvas_pan_end(target, event),
            add="+",
        )
        canvas.bind(
            "<MouseWheel>",
            lambda event, target=canvas: self.canvas_wheel_zoom(target, event),
            add="+",
        )
        canvas.bind(
            "<Button-4>",
            lambda event, target=canvas: self.canvas_wheel_zoom(target, event),
            add="+",
        )
        canvas.bind(
            "<Button-5>",
            lambda event, target=canvas: self.canvas_wheel_zoom(target, event),
            add="+",
        )

    def canvas_pan_start(self, canvas, event):
        self.canvas_viewports[canvas]["pan"] = (event.x, event.y)
        canvas.configure(cursor="fleur")
        return "break"

    def canvas_pan_drag(self, canvas, event):
        state = self.canvas_viewports[canvas]
        if state["pan"] is None:
            return "break"
        start_x, start_y = state["pan"]
        delta_x, delta_y = event.x - start_x, event.y - start_y
        canvas.move("all", delta_x, delta_y)
        state["offset_x"] += delta_x
        state["offset_y"] += delta_y
        state["pan"] = (event.x, event.y)
        self.update_canvas_scrollregion(canvas)
        if canvas is getattr(self, "canvas", None):
            self.draw_grid_demand_overlay()
        return "break"

    def canvas_pan_end(self, canvas, _event=None):
        self.canvas_viewports[canvas]["pan"] = None
        canvas.configure(cursor="")
        return "break"

    def canvas_wheel_zoom(self, canvas, event):
        state = self.canvas_viewports[canvas]
        zoom_in = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
        factor = 1.12 if zoom_in else 1 / 1.12
        target_scale = min(8.0, max(0.25, state["scale"] * factor))
        factor = target_scale / state["scale"]
        if abs(factor - 1.0) < 1e-12:
            return "break"
        # Tk mouse events are relative to the visible widget.  Canvas item
        # coordinates can have a different origin once the scroll region has
        # shifted during pan/zoom, so always translate the zoom anchor first.
        anchor_x = float(canvas.canvasx(event.x))
        anchor_y = float(canvas.canvasy(event.y))
        canvas.scale("all", anchor_x, anchor_y, factor, factor)
        state["offset_x"] = anchor_x + factor * (
            state["offset_x"] - anchor_x
        )
        state["offset_y"] = anchor_y + factor * (
            state["offset_y"] - anchor_y
        )
        state["scale"] = target_scale
        self.update_canvas_scrollregion(canvas)
        if canvas is getattr(self, "canvas", None):
            self.draw_grid_demand_overlay()
        return "break"

    def apply_canvas_viewport(self, canvas):
        state = self.canvas_viewports.get(canvas)
        if state is None:
            return
        canvas.scale("all", 0, 0, state["scale"], state["scale"])
        canvas.move("all", state["offset_x"], state["offset_y"])
        self.update_canvas_scrollregion(canvas)

    def canvas_viewport_point(self, canvas, x, y):
        state = self.canvas_viewports.get(canvas)
        canvas_x, canvas_y = float(x), float(y)
        if state is not None:
            canvas_x = canvas_x * state["scale"] + state["offset_x"]
            canvas_y = canvas_y * state["scale"] + state["offset_y"]
        # Return widget-relative coordinates, matching event.x/event.y.
        return (
            canvas_x - float(canvas.canvasx(0)),
            canvas_y - float(canvas.canvasy(0)),
        )

    def canvas_viewport_inverse_point(self, canvas, x, y):
        # Convert widget-relative event coordinates into the canvas coordinate
        # system before undoing our own scale/translation transform.
        canvas_x = float(canvas.canvasx(x))
        canvas_y = float(canvas.canvasy(y))
        state = self.canvas_viewports.get(canvas)
        if state is None:
            return canvas_x, canvas_y
        return (
            (canvas_x - state["offset_x"]) / state["scale"],
            (canvas_y - state["offset_y"]) / state["scale"],
        )

    @staticmethod
    def update_canvas_scrollregion(canvas):
        bounds = canvas.bbox("all")
        if bounds:
            canvas.configure(scrollregion=bounds)

    @staticmethod
    def bind_mousewheel_tree(widget, callback):
        """Route wheel events from a frame and all of its current children."""
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, callback, add="+")
        for child in widget.winfo_children():
            GridMapEditorApp.bind_mousewheel_tree(child, callback)

    def scroll_grid_sidebar(self, event):
        """Scroll the Grid Map Editor controls without zooming the map."""
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            direction = -1
        elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            direction = 1
        else:
            return "break"
        self.grid_sidebar_canvas.yview_scroll(direction * 3, "units")
        return "break"

    def add_grid_sidebar_section(self, parent, key, title, header_row, content_rows):
        """Turn a group of sidebar grid rows into a collapsible section."""
        content_rows = set(content_rows)
        widgets = [
            child for child in parent.winfo_children()
            if child.grid_info()
            and int(child.grid_info()["row"]) in content_rows
        ]
        label = tk.StringVar(value=f"▾  {title}")
        button = ttk.Button(
            parent,
            textvariable=label,
            command=lambda section=key: self.toggle_grid_sidebar_section(section),
        )
        button.grid(
            row=header_row, column=0, columnspan=2, sticky="ew", pady=(3, 2)
        )
        if not hasattr(self, "grid_sidebar_sections"):
            self.grid_sidebar_sections = {}
        self.grid_sidebar_sections[key] = {
            "title": title,
            "label": label,
            "widgets": widgets,
            "expanded": True,
        }

    def toggle_grid_sidebar_section(self, key):
        section = self.grid_sidebar_sections[key]
        section["expanded"] = not section["expanded"]
        for widget in section["widgets"]:
            if section["expanded"]:
                widget.grid()
            else:
                widget.grid_remove()
        arrow = "▾" if section["expanded"] else "▸"
        section["label"].set(f"{arrow}  {section['title']}")
        self.root.update_idletasks()
        self.grid_sidebar_canvas.configure(
            scrollregion=self.grid_sidebar_canvas.bbox("all")
        )

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self.root)
        self.notebook = notebook
        notebook.grid(row=0, column=0, sticky="nsew")
        map_tab = ttk.Frame(notebook)
        affinity_tab = ttk.Frame(notebook)
        stock_tab = ttk.Frame(notebook)
        slotting_tab = ttk.Frame(notebook)
        slotting_layout_tab = ttk.Frame(notebook)
        traffic_tab = ttk.Frame(notebook)
        global_traffic_tab = ttk.Frame(notebook)
        operations_tab = ttk.Frame(notebook)
        notebook.add(map_tab, text="Grid Map Editor")
        notebook.add(affinity_tab, text="SKU Affinity")
        notebook.add(stock_tab, text="Stock Requirements")
        notebook.add(slotting_tab, text="Inventory Slotting")
        notebook.add(slotting_layout_tab, text="Interactive Slotting Layout")
        notebook.add(traffic_tab, text="Traffic-Aware Slotting")
        notebook.add(global_traffic_tab, text="Global Traffic Optimizer")
        notebook.add(operations_tab, text="Inventory Operations Demo")
        map_tab.columnconfigure(0, weight=1)
        map_tab.rowconfigure(0, weight=1)
        self.grid_map_panes = ttk.Panedwindow(
            map_tab, orient="horizontal"
        )
        self.grid_map_panes.grid(row=0, column=0, sticky="nsew")
        sidebar = ttk.Frame(self.grid_map_panes, width=290)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(0, weight=1)
        self.grid_sidebar_canvas = tk.Canvas(
            sidebar,
            width=290,
            background=self.root.cget("background"),
            highlightthickness=0,
            borderwidth=0,
        )
        self.grid_sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        self.grid_sidebar_scrollbar = ttk.Scrollbar(
            sidebar,
            orient="vertical",
            command=self.grid_sidebar_canvas.yview,
        )
        self.grid_sidebar_scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_sidebar_canvas.configure(
            yscrollcommand=self.grid_sidebar_scrollbar.set
        )
        left = ttk.Frame(self.grid_sidebar_canvas, padding=12)
        self.grid_sidebar_window = self.grid_sidebar_canvas.create_window(
            0, 0, anchor="nw", window=left
        )
        left.bind(
            "<Configure>",
            lambda _event: self.grid_sidebar_canvas.configure(
                scrollregion=self.grid_sidebar_canvas.bbox("all")
            ),
        )
        self.grid_sidebar_canvas.bind(
            "<Configure>",
            lambda event: self.grid_sidebar_canvas.itemconfigure(
                self.grid_sidebar_window, width=event.width
            ),
        )
        canvas_frame = ttk.Frame(
            self.grid_map_panes, padding=(0, 12, 12, 12)
        )
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)
        self.grid_map_panes.add(sidebar, weight=0)
        self.grid_map_panes.add(canvas_frame, weight=1)

        fields = [
            ("Map name", self.map_name), ("Level name", self.level_name),
            ("Total width (m)", self.width), ("Total length (m)", self.length),
        ]
        for row, (label, variable) in enumerate(fields, start=1):
            ttk.Label(left, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(left, textvariable=variable, width=19).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Label(left, text="Grid distance X / Y (m)").grid(row=5, column=0, sticky="w", pady=3)
        spacing_frame = ttk.Frame(left)
        spacing_frame.grid(row=5, column=1, sticky="ew", pady=3)
        ttk.Entry(spacing_frame, textvariable=self.spacing, width=8).pack(side="left")
        ttk.Label(spacing_frame, text="/").pack(side="left", padx=3)
        ttk.Entry(spacing_frame, textvariable=self.spacing_y, width=8).pack(side="left")
        ttk.Button(left, text="Generate / reset grid", command=self.generate_grid).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(left, textvariable=self.summary, foreground="#4d646d").grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 14))

        ttk.Separator(left).grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)
        tools = [
            ("Select / edit", "select"),
            ("Fill racks (drag rectangle)", "rack_rectangle"),
            ("Assign rack zone (drag rectangle)", "zone_rectangle"),
            ("Place workstation drop-off", "workstation"),
            ("Clear markers (drag)", "clear"),
        ]
        for row, (label, value) in enumerate(tools, start=10):
            ttk.Radiobutton(left, text=label, variable=self.tool, value=value).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        delete_tools = ttk.Frame(left)
        delete_tools.grid(row=15, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(
            delete_tools, text="Delete points", variable=self.tool,
            value="delete_grid",
        ).pack(side="left")
        ttk.Radiobutton(
            delete_tools, text="Delete lanes", variable=self.tool,
            value="delete_lane",
        ).pack(side="left", padx=(8, 0))

        ttk.Label(left, text="Rack ID prefix").grid(row=16, column=0, sticky="w", pady=(7, 3))
        ttk.Entry(left, textvariable=self.rack_prefix, width=19).grid(row=16, column=1, sticky="ew", pady=(7, 3))

        ttk.Separator(left).grid(row=17, column=0, columnspan=2, sticky="ew", pady=7)
        ttk.Label(left, textvariable=self.selected_coordinate).grid(row=19, column=0, columnspan=2, sticky="w", pady=(3, 6))
        ttk.Label(left, text="Position X / Y (m)").grid(row=20, column=0, sticky="w", pady=3)
        coordinate_frame = ttk.Frame(left)
        coordinate_frame.grid(row=20, column=1, sticky="ew", pady=3)
        ttk.Entry(coordinate_frame, textvariable=self.selected_x, width=8).pack(side="left")
        ttk.Label(coordinate_frame, text="/").pack(side="left", padx=3)
        ttk.Entry(coordinate_frame, textvariable=self.selected_y, width=8).pack(side="left")
        ttk.Label(left, text="Role").grid(row=21, column=0, sticky="w", pady=3)
        role_box = ttk.Combobox(left, textvariable=self.role, state="readonly", values=("none", "rack", "workstation"), width=16)
        role_box.grid(row=21, column=1, sticky="ew", pady=3)
        ttk.Label(left, text="Endpoint ID").grid(row=22, column=0, sticky="w", pady=3)
        ttk.Entry(left, textvariable=self.endpoint_id, width=19).grid(row=22, column=1, sticky="ew", pady=3)
        point_buttons = ttk.Frame(left)
        point_buttons.grid(row=23, column=0, columnspan=2, sticky="ew", pady=(6, 9))
        ttk.Button(
            point_buttons, text="Apply point edit", command=self.apply_edit
        ).pack(side="left", expand=True, fill="x")
        ttk.Button(
            point_buttons, text="Delete selected", command=self.delete_selected_grid_point
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ttk.Separator(left).grid(row=24, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Label(left, text="Layout type").grid(row=26, column=0, sticky="w", pady=3)
        storage_system_box = ttk.Combobox(
            left, textvariable=self.grid_storage_system, state="readonly",
            values=("AMR", "Mini-load ASRS", "Pallet ASRS"), width=16,
        )
        storage_system_box.grid(row=26, column=1, sticky="ew", pady=3)
        storage_system_box.bind(
            "<<ComboboxSelected>>", self.grid_storage_system_changed
        )
        buffer_capacity = ttk.Frame(left)
        buffer_capacity.grid(row=27, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(buffer_capacity, text="Levels").pack(side="left")
        ttk.Spinbox(buffer_capacity, from_=1, to=100, textvariable=self.grid_storage_levels, width=4).pack(side="left", padx=(4, 8))
        ttk.Label(buffer_capacity, text="Slots/level").pack(side="left")
        ttk.Spinbox(buffer_capacity, from_=1, to=100, textvariable=self.grid_storage_slots, width=4).pack(side="left", padx=(4, 0))
        ttk.Label(left, textvariable=self.grid_machine_capacity_label).grid(
            row=28, column=0, sticky="w", pady=3
        )
        machine_capacity = ttk.Frame(left)
        machine_capacity.grid(row=28, column=1, sticky="ew", pady=3)
        self.grid_machine_capacity_entries = {}
        for key, label in (
            ("max_item_length", "L"), ("max_item_width", "W"),
            ("max_item_height", "H"), ("max_item_weight", "Wt"),
        ):
            ttk.Label(machine_capacity, text=label).pack(side="left", padx=(3, 1))
            entry = ttk.Entry(
                machine_capacity,
                textvariable=self.grid_machine_capacity_values[key], width=5,
            )
            entry.pack(side="left")
            self.grid_machine_capacity_entries[key] = entry
        ttk.Button(left, text="Assign empty storage buffers", command=self.assign_grid_buffers).grid(row=29, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        ttk.Label(left, textvariable=self.grid_buffer_summary, foreground="#315b66", wraplength=230).grid(row=30, column=0, columnspan=2, sticky="w", pady=(1, 4))

        ttk.Separator(left).grid(row=31, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Label(left, text="Zone ID").grid(row=33, column=0, sticky="w", pady=2)
        ttk.Entry(left, textvariable=self.grid_zone_id, width=19).grid(row=33, column=1, sticky="ew", pady=2)
        ttk.Checkbutton(
            left, text="Advance zone ID automatically",
            variable=self.grid_zone_auto,
        ).grid(row=34, column=0, columnspan=2, sticky="w")
        ttk.Label(left, text="Default slot L / W (m)").grid(
            row=35, column=0, sticky="w", pady=(4, 2)
        )
        warehouse_dimensions_1 = ttk.Frame(left)
        warehouse_dimensions_1.grid(row=35, column=1, sticky="ew", pady=(4, 2))
        ttk.Entry(
            warehouse_dimensions_1,
            textvariable=self.grid_warehouse_capacity_values["max_item_length"],
            width=8,
        ).pack(side="left")
        ttk.Label(warehouse_dimensions_1, text="/").pack(side="left", padx=3)
        ttk.Entry(
            warehouse_dimensions_1,
            textvariable=self.grid_warehouse_capacity_values["max_item_width"],
            width=8,
        ).pack(side="left")
        ttk.Label(left, text="Default slot H (m) / kg").grid(
            row=36, column=0, sticky="w", pady=2
        )
        warehouse_dimensions_2 = ttk.Frame(left)
        warehouse_dimensions_2.grid(row=36, column=1, sticky="ew", pady=2)
        ttk.Entry(
            warehouse_dimensions_2,
            textvariable=self.grid_warehouse_capacity_values["max_item_height"],
            width=8,
        ).pack(side="left")
        ttk.Label(warehouse_dimensions_2, text="/").pack(side="left", padx=3)
        ttk.Entry(
            warehouse_dimensions_2,
            textvariable=self.grid_warehouse_capacity_values["max_item_weight"],
            width=8,
        ).pack(side="left")
        ttk.Button(
            left,
            text="Apply storage defaults to zones",
            command=self.apply_grid_warehouse_storage_defaults,
        ).grid(row=37, column=0, columnspan=2, sticky="ew", pady=(3, 2))
        zone_buttons = ttk.Frame(left)
        zone_buttons.grid(row=38, column=0, columnspan=2, sticky="ew", pady=(3, 2))
        ttk.Button(
            zone_buttons, text="Zone settings…",
            command=self.open_grid_zone_storage_settings,
        ).pack(side="left", expand=True, fill="x")
        ttk.Button(
            zone_buttons, text="Advanced attributes…",
            command=self.open_grid_attribute_editor,
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))
        ttk.Button(
            left, text="Clear rack zones", command=self.clear_grid_zones,
        ).grid(row=39, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(
            left, textvariable=self.grid_zone_summary, foreground="#315b66",
            wraplength=230,
        ).grid(row=40, column=0, columnspan=2, sticky="w", pady=(1, 4))

        ttk.Button(left, text="Save grid project JSON…", command=self.save_project_dialog).grid(row=47, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(left, text="Load grid project JSON…", command=self.load_project_dialog).grid(row=48, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(left, text="Export RMF building YAML…", command=self.export_yaml_dialog).grid(row=49, column=0, columnspan=2, sticky="ew", pady=(5, 2))

        self.add_grid_sidebar_section(left, "grid", "WAREHOUSE GRID", 0, range(1, 8))
        self.add_grid_sidebar_section(left, "tools", "CLICK TOOLS", 9, range(10, 17))
        self.add_grid_sidebar_section(left, "point", "SELECTED GRID POINT", 18, range(19, 24))
        self.add_grid_sidebar_section(left, "buffers", "STORAGE BUFFERS", 25, range(26, 31))
        self.add_grid_sidebar_section(left, "settings", "WAREHOUSE SETTINGS", 32, range(33, 41))
        self.add_grid_sidebar_section(left, "files", "PROJECT FILES", 46, range(47, 50))

        self.bind_mousewheel_tree(
            self.grid_sidebar_canvas, self.scroll_grid_sidebar
        )
        self.update_machine_capacity_ui()

        self.canvas = tk.Canvas(canvas_frame, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self.canvas_click)
        self.canvas.bind("<B1-Motion>", self.canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
        self.canvas.bind("<Delete>", self.delete_selected_grid_point)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.enable_canvas_viewport(self.canvas)
        ttk.Label(canvas_frame, textvariable=self.status).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.grid_dot_legend = ttk.Frame(canvas_frame)
        self.grid_dot_legend.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.grid_dot_legend_labels = []
        for column, (colour, label) in enumerate((
            ("#f3a712", "Unassigned rack"),
            ("#d1495b", "Ambient rack"),
            ("#277da1", "Chilled rack"),
            ("#6a4c93", "Workstation"),
        )):
            legend_label = ttk.Label(
                self.grid_dot_legend,
                text=f"● {label}",
                foreground=colour,
            )
            legend_label.grid(
                row=0, column=column, sticky="w", padx=(0, 14)
            )
            self.grid_dot_legend_labels.append(legend_label)
        ttk.Label(
            self.grid_dot_legend,
            text="Colored outline + badge = assigned zone",
            foreground="#4d646d",
        ).grid(row=0, column=4, sticky="w")
        self._build_affinity_tab(affinity_tab)
        self._build_stock_tab(stock_tab)
        self._build_slotting_tab(slotting_tab)
        self._build_interactive_slotting_tab(slotting_layout_tab)
        self._build_traffic_tab(traffic_tab)
        self.global_traffic_ui = GlobalTrafficOptimizerTab(
            global_traffic_tab, self
        )
        self._build_operations_tab(operations_tab)

    def _build_stock_tab(self, parent):
        self.stock_input_path = tk.StringVar(value=str(DEFAULT_AFFINITY_INPUT))
        self.stock_minimum_days = tk.StringVar(value="2")
        self.stock_buffer_days = tk.StringVar(value="1")
        self.stock_slot_length = tk.StringVar(
            value=str(self.project.warehouse_storage_defaults["max_item_length"])
        )
        self.stock_slot_width = tk.StringVar(
            value=str(self.project.warehouse_storage_defaults["max_item_width"])
        )
        self.stock_slot_height = tk.StringVar(
            value=str(self.project.warehouse_storage_defaults["max_item_height"])
        )
        stock_layout = self.project.storage_layout
        default_slot_load_weight = self.project.warehouse_storage_defaults[
            "max_item_weight"
        ]
        if (
            stock_layout is not None
            and stock_layout.system_type == "AMR"
            and self.project.machine_carrying_capacity["max_item_weight"]
        ):
            default_slot_load_weight = (
                self.project.machine_carrying_capacity["max_item_weight"]
                / (
                    stock_layout.levels_per_rack
                    * stock_layout.slots_per_level
                )
            )
        self.stock_slot_weight = tk.StringVar(
            value=f"{default_slot_load_weight:g}"
        )
        self.stock_storage_system = tk.StringVar(value=self.grid_storage_system.get())
        self.stock_machine_capacity_values = {
            key: tk.StringVar(
                value=(
                    "" if self.project.machine_carrying_capacity[key] is None
                    else str(self.project.machine_carrying_capacity[key])
                )
            )
            for key in PHYSICAL_ATTRIBUTE_KEYS
        }
        self.stock_machine_capacity_label = tk.StringVar()
        self.stock_rack_levels = tk.StringVar(
            value=str(stock_layout.levels_per_rack if stock_layout else 3)
        )
        self.stock_slots_per_level = tk.StringVar(
            value=str(stock_layout.slots_per_level if stock_layout else 4)
        )
        if not self.grid_sku_attributes_path.get().strip():
            self.grid_sku_attributes_path.set(str(DEFAULT_SKU_ATTRIBUTES_INPUT))
        self.stock_status = tk.StringVar(
            value="Choose an order-history workbook and calculate the requirements."
        )
        self.stock_rows = []
        self.stock_combination_rows = []
        self.stock_worker = None
        self.stock_messages = queue.Queue()

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        controls = ttk.LabelFrame(
            parent, text="Stock coverage settings", padding=12
        )
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Order-history Excel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Entry(controls, textvariable=self.stock_input_path).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            controls,
            text="Browse…",
            command=lambda: self.browse_slot_input(
                self.stock_input_path,
                [("Excel workbook", "*.xlsx"), ("All files", "*")],
            ),
        ).grid(row=0, column=2, padx=(8, 0), pady=4)

        day_controls = ttk.Frame(controls)
        day_controls.grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(5, 4)
        )
        ttk.Label(day_controls, text="Minimum stock coverage").pack(side="left")
        ttk.Spinbox(
            day_controls,
            from_=1,
            to=365,
            increment=1,
            textvariable=self.stock_minimum_days,
            width=6,
        ).pack(side="left", padx=(6, 3))
        ttk.Label(day_controls, text="days").pack(side="left")
        ttk.Label(day_controls, text="Buffer stock coverage").pack(
            side="left", padx=(20, 0)
        )
        ttk.Spinbox(
            day_controls,
            from_=0,
            to=365,
            increment=1,
            textvariable=self.stock_buffer_days,
            width=6,
        ).pack(side="left", padx=(6, 3))
        ttk.Label(day_controls, text="days").pack(side="left")
        self.stock_calculate_button = ttk.Button(
            day_controls,
            text="Calculate",
            command=self.calculate_stock_requirements,
        )
        self.stock_calculate_button.pack(side="left", padx=(20, 0))
        self.stock_export_button = ttk.Button(
            day_controls,
            text="Save CSV…",
            command=self.export_stock_requirements,
            state="disabled",
        )
        self.stock_export_button.pack(side="left", padx=(6, 0))
        ttk.Label(
            controls,
            text=(
                "Formula: ceil(average daily demand × (minimum + buffer days)). "
                "The workbook's inclusive calendar span supplies the daily average."
            ),
            foreground="#4d646d",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 1))
        ttk.Label(
            controls, textvariable=self.stock_status, foreground="#315b66"
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))

        attributes = ttk.LabelFrame(
            parent, text="SKU attributes and rack capacity", padding=10
        )
        attributes.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        attributes.columnconfigure(1, weight=1)
        ttk.Label(attributes, text="SKU attributes CSV").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        ttk.Entry(
            attributes, textvariable=self.grid_sku_attributes_path
        ).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(
            attributes,
            text="Load CSV…",
            command=self.load_grid_sku_attributes_dialog,
        ).grid(row=0, column=2, padx=(8, 0), pady=3)

        capacity = ttk.Frame(attributes)
        capacity.grid(row=1, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(capacity, text="Slot L / W / H (m)").pack(side="left")
        for variable in (
            self.stock_slot_length,
            self.stock_slot_width,
            self.stock_slot_height,
        ):
            ttk.Entry(capacity, textvariable=variable, width=7).pack(
                side="left", padx=(5, 0)
            )
        ttk.Label(capacity, text="Slot max kg").pack(side="left", padx=(12, 0))
        ttk.Entry(
            capacity, textvariable=self.stock_slot_weight, width=7
        ).pack(side="left", padx=(5, 0))
        ttk.Label(capacity, text="Rack levels").pack(side="left", padx=(18, 0))
        ttk.Spinbox(
            capacity, from_=1, to=100, textvariable=self.stock_rack_levels,
            width=5,
        ).pack(side="left", padx=(5, 0))
        ttk.Label(capacity, text="Slots / level").pack(side="left", padx=(18, 0))
        ttk.Spinbox(
            capacity, from_=1, to=100, textvariable=self.stock_slots_per_level,
            width=5,
        ).pack(side="left", padx=(5, 0))

        machine = ttk.Frame(attributes)
        machine.grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(machine, text="Warehouse type").pack(side="left")
        stock_system_box = ttk.Combobox(
            machine,
            textvariable=self.stock_storage_system,
            state="readonly",
            values=("AMR", "Mini-load ASRS", "Pallet ASRS"),
            width=16,
        )
        stock_system_box.pack(side="left", padx=(5, 14))
        ttk.Label(machine, textvariable=self.stock_machine_capacity_label).pack(side="left")
        self.stock_machine_capacity_entries = {}
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            entry = ttk.Entry(
                machine,
                textvariable=self.stock_machine_capacity_values[key],
                width=7,
            )
            entry.pack(side="left", padx=(5, 0))
            self.stock_machine_capacity_entries[key] = entry
        stock_system_box.bind(
            "<<ComboboxSelected>>",
            self.stock_storage_system_changed,
        )
        self.update_stock_machine_capacity_ui()

        grouping = ttk.Frame(attributes)
        grouping.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        grouping.columnconfigure(0, weight=1)
        ttk.Label(
            grouping,
            text="Rack grouping attributes (Ctrl-click to select)",
        ).grid(row=0, column=0, sticky="w")
        self.grid_sku_overlay_attribute_list = tk.Listbox(
            grouping, selectmode="extended", exportselection=False, height=3
        )
        self.grid_sku_overlay_attribute_list.grid(
            row=1, column=0, sticky="ew", pady=(2, 2)
        )
        ttk.Button(
            grouping,
            text="Apply grouping to stock results and grid overlay",
            command=self.apply_grid_sku_overlay_attributes,
        ).grid(row=1, column=1, sticky="ns", padx=(7, 0), pady=2)
        ttk.Label(
            grouping,
            textvariable=self.grid_sku_attribute_summary,
            foreground="#315b66",
            wraplength=520,
            justify="left",
        ).grid(row=0, column=2, rowspan=2, sticky="w", padx=(14, 0))
        self.sync_grid_sku_overlay_attribute_list()

        result_tabs = ttk.Notebook(parent)
        result_tabs.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        results = ttk.Frame(result_tabs, padding=8)
        combinations = ttk.Frame(result_tabs, padding=8)
        result_tabs.add(results, text="Per-SKU requirements")
        result_tabs.add(combinations, text="Racks by attribute combination")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        columns = (
            "sku", "daily", "minimum_days", "minimum", "buffer_days",
            "buffer", "total", "units_slot", "slots_unit",
            "required_slots", "required_racks", "status",
        )
        self.stock_tree = ttk.Treeview(
            results, columns=columns, show="headings", height=20
        )
        headings = {
            "sku": "SKU",
            "daily": "Avg daily demand (EA)",
            "minimum_days": "Minimum days",
            "minimum": "Minimum stock (EA)",
            "buffer_days": "Buffer days",
            "buffer": "Buffer stock (EA)",
            "total": "Total required (EA)",
            "units_slot": "EA / slot",
            "slots_unit": "Slots / EA",
            "required_slots": "Required slots",
            "required_racks": "Required racks",
            "status": "Rack status",
        }
        widths = {
            "sku": 190, "daily": 145, "minimum_days": 105,
            "minimum": 140, "buffer_days": 95, "buffer": 135,
            "total": 140,
            "units_slot": 90, "slots_unit": 90,
            "required_slots": 105, "required_racks": 105, "status": 165,
        }
        for column in columns:
            self.stock_tree.heading(column, text=headings[column])
            self.stock_tree.column(
                column,
                width=widths[column],
                anchor="w" if column == "sku" else "center",
            )
        vertical = ttk.Scrollbar(
            results, orient="vertical", command=self.stock_tree.yview
        )
        horizontal = ttk.Scrollbar(
            results, orient="horizontal", command=self.stock_tree.xview
        )
        self.stock_tree.configure(
            yscrollcommand=vertical.set, xscrollcommand=horizontal.set
        )
        self.stock_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        combinations.columnconfigure(0, weight=1)
        combinations.rowconfigure(0, weight=1)
        combination_columns = (
            "combination", "skus", "total_ea", "slots", "racks", "issues",
        )
        self.stock_combination_tree = ttk.Treeview(
            combinations, columns=combination_columns, show="headings", height=12
        )
        combination_headings = {
            "combination": "Attribute combination",
            "skus": "SKUs",
            "total_ea": "Total required (EA)",
            "slots": "Required slots",
            "racks": "Required racks",
            "issues": "Unresolved SKUs",
        }
        combination_widths = {
            "combination": 330, "skus": 70, "total_ea": 135,
            "slots": 100, "racks": 100, "issues": 110,
        }
        for column in combination_columns:
            self.stock_combination_tree.heading(
                column, text=combination_headings[column]
            )
            self.stock_combination_tree.column(
                column,
                width=combination_widths[column],
                anchor="w" if column == "combination" else "center",
            )
        combination_y = ttk.Scrollbar(
            combinations,
            orient="vertical",
            command=self.stock_combination_tree.yview,
        )
        self.stock_combination_tree.configure(yscrollcommand=combination_y.set)
        self.stock_combination_tree.grid(row=0, column=0, sticky="nsew")
        combination_y.grid(row=0, column=1, sticky="ns")

    def update_stock_machine_capacity_ui(self):
        if not hasattr(self, "stock_machine_capacity_entries"):
            return
        is_amr = self.stock_storage_system.get() == "AMR"
        self.stock_machine_capacity_label.set(
            "AMR whole-rack max weight (kg)"
            if is_amr else "ASRS max L/W/H (m) / kg"
        )
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            state = "disabled" if is_amr and key != "max_item_weight" else "normal"
            self.stock_machine_capacity_entries[key].configure(state=state)

    def stock_storage_system_changed(self, _event=None):
        defaults = DEFAULT_MACHINE_CAPACITY_BY_SYSTEM[
            self.stock_storage_system.get()
        ]
        for key, variable in self.stock_machine_capacity_values.items():
            value = defaults[key]
            variable.set("" if value is None else str(value))
        if self.stock_storage_system.get() == "AMR":
            try:
                slot_count = (
                    int(self.stock_rack_levels.get())
                    * int(self.stock_slots_per_level.get())
                )
                self.stock_slot_weight.set(
                    f'{defaults["max_item_weight"] / slot_count:g}'
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        self.update_stock_machine_capacity_ui()

    def calculate_stock_requirements(self):
        try:
            minimum_days = int(self.stock_minimum_days.get())
            buffer_days = int(self.stock_buffer_days.get())
            source = Path(self.stock_input_path.get().strip()).expanduser()
            attribute_source = Path(
                self.grid_sku_attributes_path.get().strip()
            ).expanduser()
            slot_dimensions = (
                float(self.stock_slot_length.get()),
                float(self.stock_slot_width.get()),
                float(self.stock_slot_height.get()),
            )
            slot_max_weight = float(self.stock_slot_weight.get())
            levels = int(self.stock_rack_levels.get())
            slots = int(self.stock_slots_per_level.get())
            machine_capacity = self.parse_machine_capacity(
                self.stock_machine_capacity_values,
                self.stock_storage_system.get(),
            )
            if minimum_days < 1 or buffer_days < 0:
                raise ValueError(
                    "Minimum days must be at least 1 and buffer days cannot be negative."
                )
            if levels < 1 or slots < 1:
                raise ValueError("Rack levels and slots per level must be at least 1.")
            if any(value <= 0 for value in slot_dimensions) or slot_max_weight <= 0:
                raise ValueError(
                    "Slot length, width, height, and maximum weight must be greater than zero."
                )
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Stock requirements", str(exc))
            return

        self.project.machine_carrying_capacity = machine_capacity
        if (
            self.project.storage_layout is not None
            and self.project.storage_layout.system_type
            == self.stock_storage_system.get()
        ):
            self.project.storage_layout.machine_carrying_capacity = dict(
                machine_capacity
            )
        handling_unit = {
            "AMR": "AMR shelf",
            "Mini-load ASRS": "Tote",
            "Pallet ASRS": "Pallet",
        }[self.stock_storage_system.get()]
        self.attributes.set_machine_carrying_capacity(
            machine_capacity, handling_unit
        )

        resolved_attributes = str(attribute_source.resolve())
        if (
            self.project.sku_attribute_source != resolved_attributes
            or not self.project.sku_attribute_summary
        ) and not self.load_grid_sku_attributes(attribute_source):
            return
        combination_attributes = tuple(self.project.sku_overlay_attributes)
        attribute_catalog = copy.deepcopy(self.project.attribute_catalog)

        self.stock_rows = []
        self.stock_combination_rows = []
        self.stock_tree.delete(*self.stock_tree.get_children())
        self.stock_combination_tree.delete(
            *self.stock_combination_tree.get_children()
        )
        self.stock_calculate_button.configure(state="disabled")
        self.stock_export_button.configure(state="disabled")
        self.stock_status.set("Reading order history and calculating stock…")

        def worker():
            try:
                rows = determine_stock_requirements(
                    source, minimum_days, buffer_days
                )
                requirements = self.slotting.load_sku_attribute_requirements(
                    attribute_source,
                    {str(row["sku"]) for row in rows},
                    attribute_catalog,
                    include_derived_grouping=True,
                )
                rows, combinations = calculate_rack_requirements(
                    rows,
                    requirements,
                    slot_dimensions,
                    levels,
                    slots,
                    combination_attributes,
                    slot_max_weight=slot_max_weight,
                    rack_max_weight=(
                        machine_capacity["max_item_weight"]
                        if self.stock_storage_system.get() == "AMR" else None
                    ),
                )
                self.stock_messages.put(("done", (rows, combinations)))
            except Exception as exc:
                self.stock_messages.put(("error", exc))

        self.stock_worker = threading.Thread(target=worker, daemon=True)
        self.stock_worker.start()
        self.root.after(80, self.poll_stock_requirements)

    def poll_stock_requirements(self):
        try:
            kind, payload = self.stock_messages.get_nowait()
        except queue.Empty:
            if self.stock_worker and self.stock_worker.is_alive():
                self.root.after(80, self.poll_stock_requirements)
            return

        self.stock_calculate_button.configure(state="normal")
        if kind == "error":
            self.stock_status.set("Stock calculation failed.")
            messagebox.showerror("Stock requirements", str(payload))
            return

        rows, combinations = payload
        self.stock_rows = rows
        self.stock_combination_rows = combinations
        for row in rows:
            self.stock_tree.insert("", "end", values=(
                row["sku"],
                f'{row["average_daily_demand_ea"]:g}',
                row["minimum_stock_days"],
                row["minimum_stock_ea"],
                row["buffer_stock_days"],
                row["minimum_buffer_stock_ea"],
                row["total_required_ea"],
                row["units_per_slot"] or "—",
                row["slots_per_unit"] or "—",
                row["required_slots"] if row["required_slots"] != "" else "—",
                row["required_racks"] if row["required_racks"] != "" else "—",
                row["rack_calculation_status"],
            ))
        for row in combinations:
            self.stock_combination_tree.insert("", "end", values=(
                row["attribute_combination"],
                row["sku_count"],
                row["total_required_ea"],
                row["required_slots"],
                f'{row["required_racks"]}{"+" if row["unresolved_skus"] else ""}',
                row["unresolved_skus"],
            ))
        summary = copy.deepcopy(self.project.sku_attribute_summary)
        summary["attribute_combinations"] = copy.deepcopy(combinations)
        self.project.sku_attribute_summary = summary
        self.grid_sku_attribute_summary.set(
            self.format_sku_attribute_summary(summary)
        )
        self.redraw()
        first = rows[0]
        unresolved = sum(
            not str(row["rack_calculation_status"]).startswith("CALCULATED")
            for row in rows
        )
        self.stock_export_button.configure(state="normal")
        self.stock_status.set(
            f'{len(rows):,} SKUs · {first["observation_start"]} to '
            f'{first["observation_end"]} ({first["observation_days"]:,} days) · '
            f'{unresolved:,} unresolved rack calculations'
        )

    def export_stock_requirements(self):
        if not self.stock_rows:
            return
        path = filedialog.asksaveasfilename(
            title="Save stock requirements",
            initialdir=str(Path(self.stock_input_path.get()).expanduser().parent),
            initialfile="minimum_stock_requirements.csv",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"), ("All files", "*")),
        )
        if not path:
            return
        try:
            output = write_stock_requirements_csv(self.stock_rows, Path(path))
            combination_output = Path(path).with_name(
                f"{Path(path).stem}_attribute_combinations.csv"
            )
            write_attribute_combination_csv(
                self.stock_combination_rows, combination_output
            )
        except OSError as exc:
            messagebox.showerror("Save stock requirements", str(exc))
            return
        self.stock_status.set(
            f"Saved {len(self.stock_rows):,} SKUs to {output} and grouped racks "
            f"to {combination_output.name}"
        )

    def _build_affinity_tab(self, parent):
        self.affinity_input_path = tk.StringVar(value=str(DEFAULT_AFFINITY_INPUT))
        self.affinity_start_date = tk.StringVar()
        self.affinity_end_date = tk.StringVar()
        self.affinity_sku_search = tk.StringVar()
        self.affinity_top_skus = tk.StringVar(value="30")
        self.affinity_top_stores = tk.StringVar(value="20")
        self.affinity_min_shared = tk.StringVar(value="3")
        self.affinity_status = tk.StringVar(
            value="Choose an order-history workbook and click Analyze."
        )
        self.affinity_kpis = tk.StringVar(
            value=(
                "Line orders —  ·  SKUs —  ·  Stores —  ·  Store-days —  ·  "
                "SKU–store pairs —"
            )
        )
        self.affinity_selected_summary = tk.StringVar(
            value="Select or search for a SKU to inspect its relationships."
        )
        self.affinity_progress = tk.DoubleVar(value=0.0)
        self.affinity_dataset = None
        self.affinity_analysis = None
        self.affinity_selected_sku = None
        self.affinity_heatmap_skus = []
        self.affinity_heatmap_stores = []
        self.affinity_worker = None
        self.affinity_export_worker = None
        self.affinity_cancel_event = threading.Event()
        self.affinity_messages = queue.Queue()
        self.affinity_export_messages = queue.Queue()

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        source = ttk.LabelFrame(parent, text="Order history", padding=10)
        source.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        source.columnconfigure(1, weight=1)
        ttk.Label(source, text="Workbook").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(source, textvariable=self.affinity_input_path).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(source, text="Browse…", command=self.browse_affinity_input).grid(
            row=0, column=2, padx=(8, 0)
        )
        self.affinity_analyze_button = ttk.Button(
            source, text="Analyze", command=self.start_affinity_load
        )
        self.affinity_analyze_button.grid(row=0, column=3, padx=(8, 0))
        self.affinity_cancel_button = ttk.Button(
            source, text="Cancel", command=self.cancel_affinity_load, state="disabled"
        )
        self.affinity_cancel_button.grid(row=0, column=4, padx=(6, 0))
        self.affinity_export_button = ttk.Button(
            source, text="Export JSON + CSV…", command=self.export_affinity, state="disabled"
        )
        self.affinity_export_button.grid(row=0, column=5, padx=(12, 0))
        ttk.Progressbar(
            source, variable=self.affinity_progress, maximum=100, mode="determinate"
        ).grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 3))
        ttk.Label(source, textvariable=self.affinity_status, foreground="#315b66").grid(
            row=2, column=0, columnspan=6, sticky="w"
        )

        filters = ttk.LabelFrame(parent, text="Analysis filters", padding=10)
        filters.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        fields = (
            ("Start date", self.affinity_start_date, 12),
            ("End date", self.affinity_end_date, 12),
            ("Find SKU", self.affinity_sku_search, 14),
            ("Top SKUs", self.affinity_top_skus, 6),
            ("Top stores", self.affinity_top_stores, 6),
            ("Min. shared store-days", self.affinity_min_shared, 6),
        )
        column = 0
        for label, variable, width in fields:
            ttk.Label(filters, text=label).grid(row=0, column=column, sticky="w", padx=(0, 4))
            entry = (
                ttk.Spinbox(filters, from_=1, to=100, textvariable=variable, width=width)
                if label in {"Top SKUs", "Top stores", "Min. shared store-days"}
                else ttk.Entry(filters, textvariable=variable, width=width)
            )
            entry.grid(row=0, column=column + 1, sticky="w", padx=(0, 10))
            if label == "Find SKU":
                entry.bind("<Return>", lambda _event: self.apply_affinity_filters())
            column += 2
        self.affinity_filter_button = ttk.Button(
            filters, text="Apply filters", command=self.apply_affinity_filters,
            state="disabled",
        )
        self.affinity_filter_button.grid(row=0, column=column, padx=(2, 5))
        self.affinity_reset_button = ttk.Button(
            filters, text="Reset", command=self.reset_affinity_filters, state="disabled"
        )
        self.affinity_reset_button.grid(row=0, column=column + 1)
        ttk.Label(
            filters,
            text="Dates are inclusive (YYYY-MM-DD). Each valid row counts as one order event.",
            foreground="#5f6d73",
        ).grid(row=1, column=0, columnspan=column + 2, sticky="w", pady=(7, 0))

        ttk.Label(
            parent, textvariable=self.affinity_kpis,
            font=("TkDefaultFont", 10, "bold"), foreground="#234a57",
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(2, 5))

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
        visual = ttk.LabelFrame(body, text="Affinity explorer", padding=8)
        details = ttk.LabelFrame(body, text="Selected SKU details", padding=8)
        body.add(visual, weight=3)
        body.add(details, weight=2)
        visual.columnconfigure(0, weight=1)
        visual.rowconfigure(0, weight=1)
        details.columnconfigure(0, weight=1)
        details.rowconfigure(2, weight=1)

        views = ttk.Notebook(visual)
        views.grid(row=0, column=0, sticky="nsew")
        heatmap_frame = ttk.Frame(views)
        graph_frame = ttk.Frame(views)
        views.add(heatmap_frame, text="Store–SKU Heatmap")
        views.add(graph_frame, text="SKU Relationship Map")
        heatmap_frame.columnconfigure(0, weight=1)
        heatmap_frame.rowconfigure(0, weight=1)
        graph_frame.columnconfigure(0, weight=1)
        graph_frame.rowconfigure(0, weight=1)

        self.affinity_heatmap_figure = Figure(figsize=(7.2, 5.2), dpi=100)
        self.affinity_heatmap_canvas = FigureCanvasTkAgg(
            self.affinity_heatmap_figure, master=heatmap_frame
        )
        self.affinity_heatmap_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self.affinity_heatmap_canvas.mpl_connect(
            "button_press_event", self.affinity_heatmap_click
        )

        self.affinity_graph_canvas = tk.Canvas(
            graph_frame, background="#071421", highlightthickness=0
        )
        self.affinity_graph_canvas.grid(row=0, column=0, sticky="nsew")
        self.affinity_graph_canvas.bind(
            "<Configure>", lambda _event: self.draw_affinity_graph()
        )
        self.enable_canvas_viewport(self.affinity_graph_canvas)
        self.affinity_graph_canvas.tag_bind(
            "sku_node", "<Button-1>", self.affinity_graph_click
        )
        ttk.Label(
            graph_frame,
            text="Click a related SKU to recenter. Node size = order frequency; edge width = affinity.",
            foreground="#5f6d73",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))

        ttk.Label(
            details, textvariable=self.affinity_selected_summary,
            justify="left", wraplength=480,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Separator(details).grid(row=1, column=0, sticky="ew", pady=(0, 6))
        detail_tabs = ttk.Notebook(details)
        detail_tabs.grid(row=2, column=0, sticky="nsew")
        related_frame = ttk.Frame(detail_tabs)
        stores_frame = ttk.Frame(detail_tabs)
        detail_tabs.add(related_frame, text="Related SKUs")
        detail_tabs.add(stores_frame, text="Store Frequency")
        self.affinity_related_tree = self._build_affinity_tree(
            related_frame,
            ("sku", "affinity", "shared", "selected", "related", "stores"),
            {
                "sku": ("Related SKU", 95), "affinity": ("Affinity", 75),
                "shared": ("Shared store-days", 105), "selected": ("Selected orders", 95),
                "related": ("Related orders", 95), "stores": ("Strongest stores", 260),
            },
        )
        self.affinity_related_tree.bind(
            "<Double-1>", self.affinity_related_double_click
        )
        self.affinity_store_tree = self._build_affinity_tree(
            stores_frame,
            ("store", "orders", "share"),
            {
                "store": ("Store ID", 110), "orders": ("Line orders", 100),
                "share": ("SKU share", 90),
            },
        )
        self.draw_affinity_heatmap()
        self.draw_affinity_graph()

    def _build_affinity_tree(self, parent, columns, specifications):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=columns, show="headings")
        tree._affinity_sort_reverse = {}
        for column in columns:
            heading, width = specifications[column]
            tree.heading(
                column, text=heading,
                command=lambda c=column, t=tree: self.sort_affinity_tree(t, c),
            )
            tree.column(
                column, width=width,
                anchor="w" if column in {"sku", "store", "stores"} else "center",
            )
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return tree

    @staticmethod
    def sort_affinity_tree(tree, column):
        reverse = tree._affinity_sort_reverse.get(column, False)

        def value(item):
            raw = str(tree.set(item, column)).replace(",", "").rstrip("%")
            try:
                return (0, float(raw))
            except ValueError:
                return (1, raw.casefold())

        rows = sorted(tree.get_children(""), key=value, reverse=reverse)
        for position, item in enumerate(rows):
            tree.move(item, "", position)
        tree._affinity_sort_reverse[column] = not reverse

    def browse_affinity_input(self):
        path = filedialog.askopenfilename(
            title="Select order-history workbook",
            filetypes=(("Excel workbooks", "*.xlsx"), ("All files", "*")),
        )
        if path:
            self.affinity_input_path.set(path)

    def start_affinity_load(self):
        if self.affinity_worker and self.affinity_worker.is_alive():
            return
        path = Path(self.affinity_input_path.get().strip()).expanduser()
        if not path.exists():
            messagebox.showerror("Affinity analysis", f"Workbook not found:\n{path}")
            return
        self.affinity_cancel_event.clear()
        while not self.affinity_messages.empty():
            try:
                self.affinity_messages.get_nowait()
            except queue.Empty:
                break
        self.affinity_progress.set(0)
        self.affinity_status.set("Opening workbook…")
        self.affinity_dataset = None
        self.affinity_analysis = None
        self.affinity_selected_sku = None
        self.affinity_kpis.set(
            "Line orders —  ·  SKUs —  ·  Stores —  ·  Store-days —  ·  "
            "SKU–store pairs —"
        )
        self.draw_affinity_heatmap()
        self.draw_affinity_graph()
        self.populate_affinity_details()
        self.affinity_analyze_button.configure(state="disabled")
        self.affinity_cancel_button.configure(state="normal")
        self.affinity_export_button.configure(state="disabled")
        self.affinity_filter_button.configure(state="disabled")
        self.affinity_reset_button.configure(state="disabled")

        def progress(done, total, message):
            self.affinity_messages.put(("progress", done, total, message))

        def worker():
            try:
                dataset = self.affinity.load_orders(
                    path,
                    progress=progress,
                    cancelled=self.affinity_cancel_event.is_set,
                )
                self.affinity_messages.put(("loaded", dataset))
            except AffinityCancelledError as exc:
                self.affinity_messages.put(("cancelled", str(exc)))
            except Exception as exc:
                self.affinity_messages.put(("error", exc))

        self.affinity_worker = threading.Thread(target=worker, daemon=True)
        self.affinity_worker.start()
        self.root.after(80, self.poll_affinity_load)

    def poll_affinity_load(self):
        finished = False
        while True:
            try:
                message = self.affinity_messages.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "progress":
                _kind, done, total, status = message
                self.affinity_progress.set(min(100.0, float(done) * 100.0 / max(1, total)))
                self.affinity_status.set(status)
            elif kind == "loaded":
                self.complete_affinity_load(message[1])
                finished = True
            elif kind == "cancelled":
                self.affinity_status.set("Analysis cancelled; no partial cache was saved.")
                finished = True
            elif kind == "error":
                self.affinity_status.set("Unable to analyze the selected workbook.")
                messagebox.showerror("Affinity analysis", str(message[1]))
                finished = True
        if not finished and self.affinity_worker and self.affinity_worker.is_alive():
            self.root.after(80, self.poll_affinity_load)
            return
        if finished or not (self.affinity_worker and self.affinity_worker.is_alive()):
            self.affinity_analyze_button.configure(state="normal")
            self.affinity_cancel_button.configure(state="disabled")
            if self.affinity_analysis is not None:
                self.affinity_export_button.configure(state="normal")
                self.affinity_filter_button.configure(state="normal")
                self.affinity_reset_button.configure(state="normal")

    def cancel_affinity_load(self):
        self.affinity_cancel_event.set()
        self.affinity_status.set("Cancelling after the current workbook row…")
        self.affinity_cancel_button.configure(state="disabled")

    def complete_affinity_load(self, dataset):
        self.affinity_dataset = dataset
        self.affinity_start_date.set(dataset.min_date.isoformat())
        self.affinity_end_date.set(dataset.max_date.isoformat())
        self.affinity_sku_search.set("")
        source = "local cache" if dataset.cache_used else "workbook"
        self.affinity_status.set(
            f"Loaded {dataset.valid_rows:,} line orders from {dataset.worksheet} "
            f"using {source}."
        )
        self.affinity_progress.set(100)
        self.apply_affinity_filters(show_errors=False)

    def _affinity_filter_values(self):
        try:
            start = date.fromisoformat(self.affinity_start_date.get().strip())
            end = date.fromisoformat(self.affinity_end_date.get().strip())
        except ValueError as exc:
            raise ValueError("Start and end dates must use YYYY-MM-DD") from exc
        try:
            top_skus = int(self.affinity_top_skus.get())
            top_stores = int(self.affinity_top_stores.get())
            min_shared = int(self.affinity_min_shared.get())
        except ValueError as exc:
            raise ValueError(
                "Top counts and minimum shared store-days must be whole numbers"
            ) from exc
        if not 1 <= top_skus <= 100 or not 1 <= top_stores <= 100:
            raise ValueError("Top SKU and store counts must be between 1 and 100")
        if not 1 <= min_shared <= 10000:
            raise ValueError("Minimum shared store-days must be at least 1")
        return start, end, top_skus, top_stores, min_shared

    def apply_affinity_filters(self, show_errors=True):
        if self.affinity_dataset is None:
            if show_errors:
                messagebox.showinfo("Affinity analysis", "Analyze an order workbook first.")
            return
        try:
            start, end, _top_skus, _top_stores, _min_shared = self._affinity_filter_values()
            analysis = self.affinity.analyze(self.affinity_dataset, start, end)
        except ValueError as exc:
            self.affinity_status.set(str(exc))
            self.affinity_analysis = None
            self.affinity_selected_sku = None
            self.affinity_kpis.set(
                "Line orders 0  ·  SKUs 0  ·  Stores 0  ·  Store-days 0  ·  "
                "SKU–store pairs 0"
            )
            self.affinity_export_button.configure(state="disabled")
            self.draw_affinity_heatmap()
            self.draw_affinity_graph()
            self.populate_affinity_details()
            if show_errors:
                messagebox.showerror("Affinity filters", str(exc))
            return
        self.affinity_analysis = analysis
        selected = analysis.resolve_sku(self.affinity_sku_search.get())
        if self.affinity_sku_search.get().strip() and selected is None:
            self.affinity_status.set(
                f"No active SKU matches: {self.affinity_sku_search.get().strip()}"
            )
            self.affinity_selected_sku = None
        else:
            self.affinity_selected_sku = selected
            self.affinity_status.set(
                f"Showing {analysis.event_count:,} line orders from "
                f"{analysis.start_date.isoformat()} through {analysis.end_date.isoformat()}."
            )
        self.affinity_kpis.set(
            f"Line orders {analysis.event_count:,}  ·  "
            f"SKUs {np.count_nonzero(analysis.sku_totals):,}  ·  "
            f"Stores {np.count_nonzero(analysis.store_totals):,}  ·  "
            f"Store-days {analysis.store_day_count:,}  ·  "
            f"SKU–store pairs {analysis.observed_pairs:,}  ·  "
            f"Skipped source rows {analysis.dataset.skipped_rows:,}"
        )
        self.draw_affinity_heatmap()
        self.draw_affinity_graph()
        self.populate_affinity_details()
        self.affinity_export_button.configure(state="normal")
        self.affinity_filter_button.configure(state="normal")
        self.affinity_reset_button.configure(state="normal")

    def reset_affinity_filters(self):
        if self.affinity_dataset is None:
            return
        self.affinity_start_date.set(self.affinity_dataset.min_date.isoformat())
        self.affinity_end_date.set(self.affinity_dataset.max_date.isoformat())
        self.affinity_sku_search.set("")
        self.affinity_top_skus.set("30")
        self.affinity_top_stores.set("20")
        self.affinity_min_shared.set("3")
        self.apply_affinity_filters()

    def draw_affinity_heatmap(self):
        figure = self.affinity_heatmap_figure
        figure.clear()
        axis = figure.add_subplot(111)
        analysis = self.affinity_analysis
        if analysis is None:
            axis.set_axis_off()
            axis.text(
                0.5, 0.5, "Analyze an order workbook to view SKU–store frequency.",
                ha="center", va="center", color="#5f6d73", transform=axis.transAxes,
            )
            self.affinity_heatmap_skus = []
            self.affinity_heatmap_stores = []
            self.affinity_heatmap_canvas.draw_idle()
            return
        try:
            _start, _end, top_sku_count, top_store_count, _shared = self._affinity_filter_values()
        except ValueError:
            top_sku_count, top_store_count = 30, 20
        sku_indices = analysis.top_skus(top_sku_count)
        if self.affinity_selected_sku is not None and self.affinity_selected_sku not in sku_indices:
            sku_indices = [self.affinity_selected_sku] + sku_indices[: max(0, top_sku_count - 1)]
        totals = analysis.frequency[sku_indices].sum(axis=0)
        store_indices = [int(index) for index in np.argsort(-totals, kind="stable") if totals[index] > 0]
        store_indices = store_indices[:top_store_count]
        self.affinity_heatmap_skus = sku_indices
        self.affinity_heatmap_stores = store_indices
        if not sku_indices or not store_indices:
            axis.set_axis_off()
            axis.text(0.5, 0.5, "No frequency data for this selection.", ha="center", va="center")
            self.affinity_heatmap_canvas.draw_idle()
            return
        values = np.log1p(analysis.frequency[np.ix_(sku_indices, store_indices)])
        image = axis.imshow(values, aspect="auto", cmap="viridis", interpolation="nearest")
        axis.set_xticks(range(len(store_indices)))
        axis.set_xticklabels(
            [analysis.dataset.stores[index] for index in store_indices],
            rotation=55, ha="right", fontsize=7,
        )
        axis.set_yticks(range(len(sku_indices)))
        axis.set_yticklabels(
            [analysis.dataset.skus[index] for index in sku_indices], fontsize=7
        )
        axis.set_xlabel("Store ID")
        axis.set_ylabel("SKU")
        axis.set_title("Line-order frequency (log colour scale)")
        figure.colorbar(image, ax=axis, fraction=0.03, pad=0.02, label="log(1 + orders)")
        figure.tight_layout()
        self.affinity_heatmap_canvas.draw_idle()

    def affinity_heatmap_click(self, event):
        if event.xdata is None or event.ydata is None:
            return
        row = int(round(event.ydata))
        column = int(round(event.xdata))
        if not (0 <= row < len(self.affinity_heatmap_skus)):
            return
        if not (0 <= column < len(self.affinity_heatmap_stores)):
            return
        sku_index = self.affinity_heatmap_skus[row]
        store_index = self.affinity_heatmap_stores[column]
        self.select_affinity_sku(sku_index)
        count = int(self.affinity_analysis.frequency[sku_index, store_index])
        self.affinity_status.set(
            f"{self.affinity_analysis.dataset.stores[store_index]} ordered SKU "
            f"{self.affinity_analysis.dataset.skus[sku_index]} on {count:,} line(s) "
            "in the selected date range."
        )

    def select_affinity_sku(self, sku_index):
        if self.affinity_analysis is None:
            return
        if int(self.affinity_analysis.sku_totals[sku_index]) <= 0:
            return
        self.affinity_selected_sku = int(sku_index)
        self.affinity_sku_search.set(self.affinity_analysis.dataset.skus[sku_index])
        self.draw_affinity_graph()
        self.populate_affinity_details()

    def draw_affinity_graph(self):
        canvas = self.affinity_graph_canvas
        canvas.delete("all")
        width = max(500, canvas.winfo_width())
        height = max(380, canvas.winfo_height())
        analysis = self.affinity_analysis
        selected = self.affinity_selected_sku
        if analysis is None or selected is None:
            canvas.create_text(
                width / 2, height / 2,
                text="Select or search for a SKU to view its relationship map.",
                fill="#93a7b5", font=("TkDefaultFont", 11),
            )
            self.apply_canvas_viewport(canvas)
            return
        try:
            min_shared = int(self.affinity_min_shared.get())
        except ValueError:
            min_shared = 3
        related = analysis.related_skus(selected, min_shared, 12)
        center_x, center_y = width / 2, height / 2
        if not related:
            canvas.create_text(
                center_x, center_y + 75,
                text=f"No related SKU meets the {min_shared}-shared-store-day threshold.",
                fill="#93a7b5", font=("TkDefaultFont", 10),
            )
        ring = max(125, min(width, height) * 0.35)
        positions = {}
        for position, row in enumerate(related):
            angle = -math.pi / 2 + 2 * math.pi * position / max(1, len(related))
            positions[row["sku_index"]] = (
                center_x + ring * math.cos(angle), center_y + ring * math.sin(angle)
            )
            edge_width = 1.0 + 5.0 * row["affinity"]
            canvas.create_line(
                center_x, center_y, *positions[row["sku_index"]],
                fill="#1cc8d8", width=edge_width,
            )
            mid_x = (center_x + positions[row["sku_index"]][0]) / 2
            mid_y = (center_y + positions[row["sku_index"]][1]) / 2
            canvas.create_text(
                mid_x, mid_y, text=f"{row['affinity_percent']:.0f}%",
                fill="#91dae0", font=("TkDefaultFont", 8),
            )
        maximum = max(
            [int(analysis.sku_totals[selected])]
            + [int(analysis.sku_totals[row["sku_index"]]) for row in related]
        )

        def node(index, x, y, selected_node=False):
            total = int(analysis.sku_totals[index])
            radius = (25 if selected_node else 16) + 12 * math.sqrt(total / max(1, maximum))
            tags = ("sku_node", f"sku:{index}")
            canvas.create_oval(
                x - radius, y - radius, x + radius, y + radius,
                fill="#00d7e7" if selected_node else "#365f8c",
                outline="#d7fbff", width=2, tags=tags,
            )
            canvas.create_text(
                x, y - 3, text=analysis.dataset.skus[index],
                fill="#05131f" if selected_node else "white",
                font=("TkDefaultFont", 9, "bold"), tags=tags,
            )
            canvas.create_text(
                x, y + 12, text=f"{total:,}",
                fill="#16414c" if selected_node else "#c8deef",
                font=("TkDefaultFont", 7), tags=tags,
            )

        for row in related:
            node(row["sku_index"], *positions[row["sku_index"]])
        node(selected, center_x, center_y, selected_node=True)
        self.apply_canvas_viewport(canvas)

    def affinity_graph_click(self, _event):
        current = self.affinity_graph_canvas.find_withtag("current")
        if not current:
            return
        for tag in self.affinity_graph_canvas.gettags(current[0]):
            if tag.startswith("sku:"):
                self.select_affinity_sku(int(tag.split(":", 1)[1]))
                return

    def populate_affinity_details(self):
        for tree in (self.affinity_related_tree, self.affinity_store_tree):
            for item in tree.get_children():
                tree.delete(item)
        analysis = self.affinity_analysis
        selected = self.affinity_selected_sku
        if analysis is None or selected is None:
            self.affinity_selected_summary.set(
                "Select or search for a SKU to inspect its relationships."
            )
            return
        try:
            min_shared = int(self.affinity_min_shared.get())
        except ValueError:
            min_shared = 3
        related = analysis.related_skus(selected, min_shared, 100)
        stores = analysis.store_rows(selected)
        self.affinity_selected_summary.set(
            f"SKU {analysis.dataset.skus[selected]}\n"
            f"{int(analysis.sku_totals[selected]):,} line orders from {len(stores):,} stores · "
            f"{int(analysis.sku_store_day_totals[selected]):,} store-days · "
            f"{len(related):,} qualifying related SKUs"
        )
        for row in related:
            self.affinity_related_tree.insert("", "end", values=(
                row["sku"], f"{row['affinity_percent']:.2f}%",
                f"{row['shared_store_days']:,}", f"{row['selected_orders']:,}",
                f"{row['related_orders']:,}", "; ".join(row["top_stores"]),
            ))
        for row in stores:
            self.affinity_store_tree.insert("", "end", values=(
                row["store"], f"{row['orders']:,}", f"{row['share_percent']:.2f}%",
            ))

    def affinity_related_double_click(self, _event=None):
        selection = self.affinity_related_tree.selection()
        if not selection or self.affinity_analysis is None:
            return
        sku = self.affinity_related_tree.item(selection[0], "values")[0]
        index = self.affinity_analysis.resolve_sku(str(sku))
        if index is not None:
            self.select_affinity_sku(index)

    def export_affinity(self):
        if self.affinity_analysis is None:
            return
        if self.affinity_export_worker and self.affinity_export_worker.is_alive():
            return
        initial = (
            f"sku_affinity_{self.affinity_analysis.start_date.isoformat()}_"
            f"{self.affinity_analysis.end_date.isoformat()}.affinity.json"
        )
        path = filedialog.asksaveasfilename(
            title="Export affinity analysis",
            initialdir=str(DEFAULT_AFFINITY_OUTPUT.parent),
            initialfile=initial,
            defaultextension=".affinity.json",
            filetypes=(("Affinity JSON", "*.affinity.json"), ("JSON", "*.json")),
        )
        if not path:
            return
        min_shared = int(self.affinity_min_shared.get())
        analysis = self.affinity_analysis
        self.affinity_status.set("Exporting affinity JSON and CSV files…")
        self.affinity_export_button.configure(state="disabled")
        self.affinity_analyze_button.configure(state="disabled")
        self.affinity_filter_button.configure(state="disabled")
        self.affinity_reset_button.configure(state="disabled")

        def worker():
            try:
                paths = self.affinity.export(
                    analysis, Path(path), min_shared_store_days=min_shared
                )
                self.affinity_export_messages.put(("done", paths))
            except Exception as exc:
                self.affinity_export_messages.put(("error", exc))

        self.affinity_export_worker = threading.Thread(target=worker, daemon=True)
        self.affinity_export_worker.start()
        self.root.after(80, self.poll_affinity_export)

    def poll_affinity_export(self):
        try:
            kind, payload = self.affinity_export_messages.get_nowait()
        except queue.Empty:
            if self.affinity_export_worker and self.affinity_export_worker.is_alive():
                self.root.after(80, self.poll_affinity_export)
                return
            return
        self.affinity_analyze_button.configure(state="normal")
        self.affinity_filter_button.configure(state="normal")
        self.affinity_reset_button.configure(state="normal")
        self.affinity_export_button.configure(state="normal")
        if kind == "error":
            self.affinity_status.set("Affinity export failed.")
            messagebox.showerror("Affinity export", str(payload))
            return
        json_path, store_path, pair_path = payload
        self.affinity_status.set(f"Exported affinity analysis to {json_path.parent}")
        messagebox.showinfo(
            "Affinity export",
            "Created:\n"
            f"{json_path.name}\n{store_path.name}\n{pair_path.name}",
        )

    def _build_slotting_tab(self, parent):
        self.slot_building_path = tk.StringVar(value=str(DEFAULT_GRID_INPUT))
        self.slot_velocity_path = tk.StringVar(value=str(DEFAULT_VELOCITY_INPUT))
        self.slot_chilled_path = tk.StringVar(value=str(DEFAULT_SKU_ATTRIBUTES_INPUT))
        self.slot_output_path = tk.StringVar(value=str(DEFAULT_SLOTTING_OUTPUT))
        self.slot_strategy = tk.StringVar(value="basic")
        self.slot_affinity_path = tk.StringVar(value=str(DEFAULT_AFFINITY_INPUT))
        self.slot_affinity_weight = tk.StringVar(value="50")
        self.slot_affinity_max_service = tk.StringVar()
        self.slot_affinity_min_shared = tk.StringVar()
        self.slot_affinity_min_score = tk.StringVar()
        self.slot_affinity_parameter_status = tk.StringVar(
            value="Automatic values will be calculated after generation."
        )
        self.slot_affinity_recommendation = None
        self.slot_handling_unit = tk.StringVar(value="AMR shelf")
        self.slot_zone = tk.StringVar(value="Z01")
        self.slot_levels = tk.StringVar(value="3")
        self.slot_slots = tk.StringVar(value="4")
        self.slot_summary = tk.StringVar(value="Choose the inputs and generate a slotting layout.")
        self.slot_progress_value = tk.DoubleVar(value=0)
        self.slot_progress_text = tk.StringVar(value="Ready")
        self.slot_zone_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect its zone.")
        self.slot_rack_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect it.")
        self.slot_rack_zone_name = tk.StringVar()
        self.slot_rack_zone_edit_status = tk.StringVar(
            value="Select a rack to rename its zone."
        )
        self.slot_building = None
        self.slot_grid_project = None
        self.slot_rows = []
        self.slot_racks = []
        self.slot_selected_rack = None
        self.slot_loaded_path = None
        self.slot_zone_assignments = {}
        self.slot_attribute_catalog = {}
        self.slot_location_attributes = {}
        self.slot_hierarchy_paths = []
        self.slot_storage_initialized = False
        self.slot_zone_storage_types = {}
        self.slot_viewer_status = tk.StringVar(
            value="Generate a layout or load a saved layout to view it."
        )

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        form = ttk.LabelFrame(parent, text="Slotting inputs", padding=12)
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Grid project JSON").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_building_path).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_building_path, [("Grid project", "*.grid.json"), ("JSON", "*.json"), ("All files", "*")])).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Button(form, text="Load project", command=self.load_slot_building).grid(row=0, column=3, padx=(6, 0), pady=4)
        ttk.Label(form, text="ABC SKU velocity CSV").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_velocity_path).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_velocity_path, [("CSV", "*.csv"), ("All files", "*")])).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(form, text="SKU attributes CSV (optional)").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_chilled_path).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_chilled_path, [("CSV", "*.csv"), ("All files", "*")])).grid(row=2, column=2, padx=(8, 0), pady=4)

        ttk.Label(form, text="Strategy").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        strategy_box = ttk.Combobox(
            form,
            textvariable=self.slot_strategy,
            state="readonly",
            values=("basic", "abc_affinity"),
            width=18,
        )
        strategy_box.grid(row=3, column=1, sticky="w", pady=4)
        strategy_box.bind("<<ComboboxSelected>>", self.slot_strategy_changed)

        affinity_panel = ttk.LabelFrame(
            form, text="ABC + affinity settings", padding=(8, 5)
        )
        affinity_panel.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(3, 7))
        affinity_panel.columnconfigure(1, weight=1)
        ttk.Label(affinity_panel, text="Order-history Excel").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=2
        )
        affinity_path_entry = ttk.Entry(
            affinity_panel, textvariable=self.slot_affinity_path
        )
        affinity_path_entry.grid(row=0, column=1, sticky="ew", pady=2)
        affinity_browse = ttk.Button(
            affinity_panel,
            text="Browse…",
            command=lambda: self.browse_slot_input(
                self.slot_affinity_path,
                [("Excel workbook", "*.xlsx"), ("All files", "*")],
            ),
        )
        affinity_browse.grid(row=0, column=2, padx=(8, 0), pady=2)
        ttk.Label(affinity_panel, text="Affinity weight (%)").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=2
        )
        affinity_weight_entry = ttk.Spinbox(
            affinity_panel,
            from_=0,
            to=100,
            increment=1,
            textvariable=self.slot_affinity_weight,
            width=7,
        )
        affinity_weight_entry.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(
            affinity_panel,
            text="0 = pure ABC · 100 = pure affinity · between = weighted blend",
            foreground="#4d646d",
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(75, 0), pady=2)

        tuning = ttk.Frame(affinity_panel)
        tuning.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 1))
        ttk.Label(tuning, text="Auto-suggested after generation:").pack(side="left")
        ttk.Label(tuning, text="Max service increase %").pack(side="left", padx=(10, 4))
        max_service_entry = ttk.Entry(
            tuning, textvariable=self.slot_affinity_max_service, width=8
        )
        max_service_entry.pack(side="left")
        ttk.Label(tuning, text="Min shared store-days").pack(side="left", padx=(10, 4))
        min_shared_entry = ttk.Entry(
            tuning, textvariable=self.slot_affinity_min_shared, width=8
        )
        min_shared_entry.pack(side="left")
        ttk.Label(tuning, text="Min affinity %").pack(side="left", padx=(10, 4))
        min_score_entry = ttk.Entry(
            tuning, textvariable=self.slot_affinity_min_score, width=8
        )
        min_score_entry.pack(side="left")
        affinity_actions = ttk.Frame(affinity_panel)
        affinity_actions.grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Label(
            affinity_actions,
            textvariable=self.slot_affinity_parameter_status,
            foreground="#315b66",
        ).pack(side="left")
        self.slot_affinity_auto_button = ttk.Button(
            affinity_actions,
            text="Recalculate automatic suggestion",
            command=lambda: self.run_slotting(use_adjusted=False),
        )
        self.slot_affinity_auto_button.pack(side="left", padx=(12, 0))
        self.slot_affinity_adjusted_button = ttk.Button(
            affinity_actions,
            text="Regenerate with edited values",
            command=lambda: self.run_slotting(use_adjusted=True),
        )
        self.slot_affinity_adjusted_button.pack(side="left", padx=(6, 0))
        self.slot_affinity_source_widgets = [
            affinity_path_entry,
            affinity_browse,
            affinity_weight_entry,
            self.slot_affinity_auto_button,
        ]
        self.slot_affinity_tuning_widgets = [
            max_service_entry,
            min_shared_entry,
            min_score_entry,
            self.slot_affinity_adjusted_button,
        ]

        ttk.Label(
            form,
            text="Warehouse zones, buffers, capacities, and advanced attributes are loaded from the grid project.",
            foreground="#4d646d",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 6))

        ttk.Label(form, text="Output layout JSON").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_output_path).grid(row=6, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=self.browse_slot_output).grid(row=6, column=2, padx=(8, 0), pady=4)
        slot_actions = ttk.Frame(form)
        slot_actions.grid(row=7, column=1, columnspan=3, sticky="w", pady=(10, 4))
        self.slot_generate_button = ttk.Button(
            slot_actions,
            text="Generate slotting layout",
            command=self.run_slotting,
            style="Accent.TButton",
        )
        self.slot_generate_button.pack(side="left")
        self.slot_progress = ttk.Progressbar(
            slot_actions,
            variable=self.slot_progress_value,
            maximum=100,
            length=260,
            mode="determinate",
        )
        self.slot_progress.pack(side="left", padx=(12, 6))
        ttk.Label(slot_actions, textvariable=self.slot_progress_text).pack(side="left")
        ttk.Label(form, textvariable=self.slot_summary, foreground="#315b66").grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))

        unassigned_frame = ttk.LabelFrame(
            parent, text="SKUs not slotted", padding=8
        )
        unassigned_frame.grid(
            row=1, column=0, sticky="nsew", padx=12, pady=(0, 12)
        )
        unassigned_frame.columnconfigure(0, weight=1)
        unassigned_frame.rowconfigure(1, weight=1)
        self.slot_unassigned_summary = tk.StringVar(
            value="No slotting result has been generated."
        )
        ttk.Label(
            unassigned_frame,
            textvariable=self.slot_unassigned_summary,
            foreground="#4d646d",
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        unassigned_columns = (
            "sku", "chilled", "size", "weight", "data_status", "reason"
        )
        self.slot_unassigned_tree = ttk.Treeview(
            unassigned_frame,
            columns=unassigned_columns,
            show="headings",
            height=8,
        )
        headings = {
            "sku": "SKU ID",
            "chilled": "Chilled",
            "size": "Size (L × W × H)",
            "weight": "Weight",
            "data_status": "Physical data",
            "reason": "Why not slotted",
        }
        widths = {
            "sku": 130, "chilled": 75, "size": 180, "weight": 100,
            "data_status": 150, "reason": 280,
        }
        for column in unassigned_columns:
            self.slot_unassigned_tree.heading(column, text=headings[column])
            self.slot_unassigned_tree.column(
                column,
                width=widths[column],
                anchor="w" if column in {"sku", "reason"} else "center",
            )
        self.slot_unassigned_tree.grid(row=1, column=0, sticky="nsew")
        unassigned_scroll = ttk.Scrollbar(
            unassigned_frame,
            orient="vertical",
            command=self.slot_unassigned_tree.yview,
        )
        unassigned_scroll.grid(row=1, column=1, sticky="ns")
        self.slot_unassigned_tree.configure(yscrollcommand=unassigned_scroll.set)
        self.slot_strategy_changed()

    def _build_interactive_slotting_tab(self, parent):
        self.slot_history_path = tk.StringVar(value=self.slot_affinity_path.get())
        self.slot_movement_status = tk.StringVar(
            value="Load a layout and import order history to rank unit movements."
        )
        self.slot_movement_by_unit = {}
        self.slot_movement_summary = {}
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.LabelFrame(parent, text="Generated layout viewer", padding=12)
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        controls.columnconfigure(1, weight=1)
        ttk.Button(
            controls,
            text="Load saved layout…",
            command=self.load_interactive_slotting_layout,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            controls,
            textvariable=self.slot_viewer_status,
            foreground="#315b66",
        ).grid(row=0, column=1, columnspan=3, sticky="w", padx=(12, 0))
        ttk.Label(controls, text="Order-history Excel").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(controls, textvariable=self.slot_history_path).grid(
            row=1, column=1, sticky="ew", padx=(12, 6), pady=(8, 0)
        )
        ttk.Button(
            controls,
            text="Browse…",
            command=lambda: self.browse_slot_input(
                self.slot_history_path,
                [("Excel workbook", "*.xlsx"), ("All files", "*")],
            ),
        ).grid(row=1, column=2, pady=(8, 0))
        ttk.Button(
            controls,
            text="Calculate movement ranks",
            command=self.calculate_slot_movement_ranks,
        ).grid(row=1, column=3, padx=(6, 0), pady=(8, 0))
        ttk.Label(
            controls,
            textvariable=self.slot_movement_status,
            foreground="#315b66",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        result = ttk.LabelFrame(parent, text="Interactive slotting layout", padding=8)
        result.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        result.columnconfigure(0, weight=1); result.rowconfigure(0, weight=1)
        paned = ttk.Panedwindow(result, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")
        layout_view = ttk.Frame(paned)
        rack_view = ttk.Frame(paned)
        paned.add(layout_view, weight=3); paned.add(rack_view, weight=2)
        layout_view.columnconfigure(0, weight=1); layout_view.rowconfigure(0, weight=1)
        rack_view.columnconfigure(0, weight=1); rack_view.columnconfigure(1, weight=1)
        rack_view.rowconfigure(1, weight=1)

        self.slot_canvas = tk.Canvas(layout_view, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
        self.slot_canvas.grid(row=0, column=0, sticky="nsew")
        self.slot_canvas.bind("<Configure>", lambda _event: self.draw_slotting_layout())
        self.enable_canvas_viewport(self.slot_canvas)
        self.slot_canvas.tag_bind("rack", "<Button-1>", self.slot_rack_click)
        ttk.Label(
            layout_view,
            text="Click a rack to inspect it or rename its zone",
            foreground="#4d646d",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.slot_dot_legend = ttk.Frame(layout_view)
        self.slot_dot_legend.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.slot_dot_legend_labels = []
        for column, (colour, label) in enumerate((
            ("#d1495b", "A movement unit"),
            ("#f3a712", "B movement unit"),
            ("#4c9f70", "C movement unit"),
            ("#7b8b92", "Unranked unit"),
        )):
            legend_label = ttk.Label(
                self.slot_dot_legend,
                text=f"● {label}",
                foreground=colour,
            )
            legend_label.grid(row=0, column=column, padx=(0, 12), sticky="w")
            self.slot_dot_legend_labels.append(legend_label)
        workstation_legend = ttk.Label(
            self.slot_dot_legend,
            text="◆ Workstation",
            foreground="#277da1",
        )
        workstation_legend.grid(row=0, column=4, padx=(0, 12), sticky="w")
        self.slot_dot_legend_labels.append(workstation_legend)
        ttk.Label(
            self.slot_dot_legend,
            text="AMR: shelf dot · ASRS: slot dots · outline = zone",
            foreground="#4d646d",
        ).grid(row=0, column=5, sticky="w")

        zone_detail_frame = ttk.LabelFrame(rack_view, text="Zone details", padding=8)
        zone_detail_frame.grid(row=0, column=0, sticky="nsew", padx=(8, 4))
        zone_detail_frame.columnconfigure(0, weight=1)
        ttk.Label(
            zone_detail_frame,
            textvariable=self.slot_zone_detail,
            justify="left",
            wraplength=210,
        ).grid(row=0, column=0, sticky="ew")
        rack_detail_frame = ttk.LabelFrame(rack_view, text="Rack details", padding=8)
        rack_detail_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 8))
        rack_detail_frame.columnconfigure(0, weight=1)
        ttk.Label(
            rack_detail_frame,
            textvariable=self.slot_rack_detail,
            justify="left",
            wraplength=210,
        ).grid(row=0, column=0, sticky="ew")
        zone_name_editor = ttk.Frame(rack_detail_frame)
        zone_name_editor.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        zone_name_editor.columnconfigure(1, weight=1)
        ttk.Label(zone_name_editor, text="Zone name").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        ttk.Entry(
            zone_name_editor, textvariable=self.slot_rack_zone_name
        ).grid(row=0, column=1, sticky="ew")
        ttk.Button(
            zone_name_editor,
            text="Apply & save",
            command=self.rename_selected_slot_zone,
        ).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(
            zone_name_editor,
            textvariable=self.slot_rack_zone_edit_status,
            foreground="#4d646d",
            wraplength=210,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        columns = ("abc_rank", "affinity_rank", "sku", "rack_quantity", "class", "flags", "static", "dynamic", "unit_type", "unit_id", "status")
        tree_frame = ttk.Frame(rack_view)
        tree_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8, pady=(8, 0))
        tree_frame.columnconfigure(0, weight=1); tree_frame.rowconfigure(0, weight=1)
        self.slot_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        headings = {"abc_rank":"ABC rank", "affinity_rank":"Affinity order", "sku":"SKU", "rack_quantity":"Qty in rack (EA)", "class":"ABC", "flags":"Storage flags", "static":"Current static address", "dynamic":"Occupied dynamic address", "unit_type":"Unit type", "unit_id":"Handling unit ID", "status":"Status"}
        widths = {"abc_rank":65, "affinity_rank":85, "sku":95, "rack_quantity":95, "class":50, "flags":180, "static":150, "dynamic":230, "unit_type":90, "unit_id":120, "status":90}
        for column in columns:
            self.slot_tree.heading(column, text=headings[column]); self.slot_tree.column(column, width=widths[column], anchor="center" if column not in {"flags","static","dynamic"} else "w")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.slot_tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.slot_tree.xview)
        self.slot_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.slot_tree.grid(row=0, column=0, sticky="nsew"); yscroll.grid(row=0, column=1, sticky="ns"); xscroll.grid(row=1, column=0, sticky="ew")
        ttk.Button(rack_view, text="Show all assignments", command=lambda: self.show_slotting_rows(self.slot_rows)).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(7, 0))

    def _build_traffic_tab(self, parent):
        self.traffic_grid_project_path = tk.StringVar(value=str(DEFAULT_GRID_INPUT))
        self.traffic_velocity_path = tk.StringVar(value=str(DEFAULT_VELOCITY_INPUT))
        self.traffic_chilled_path = tk.StringVar(value=str(DEFAULT_SKU_ATTRIBUTES_INPUT))
        self.traffic_order_path = tk.StringVar(value=str(DEFAULT_TRAFFIC_INPUT))
        self.traffic_last_workflow = None
        self.traffic_handling_unit = tk.StringVar(value="AMR shelf")
        self.traffic_levels = tk.StringVar(value="3")
        self.traffic_slots = tk.StringVar(value="4")
        self.traffic_network_mode = tk.StringVar(value="Use embedded RMF map")
        self.traffic_network_path = tk.StringVar()
        self.traffic_start_date = tk.StringVar()
        self.traffic_end_date = tk.StringVar()
        self.traffic_output_path = tk.StringVar(value=str(DEFAULT_TRAFFIC_OUTPUT))
        self.traffic_ctbsa_population = tk.StringVar(value="100")
        self.traffic_ctbsa_generations = tk.StringVar(value="50000")
        self.traffic_ctbsa_solution = tk.StringVar(value="3")
        self.traffic_ctbsa_seed = tk.StringVar(value="0")
        self.traffic_zone_workload_enabled = tk.BooleanVar(value=False)
        self.traffic_zone_overlay = tk.StringVar(value="Off")
        self.traffic_parameter_status = tk.StringVar(
            value="Paper defaults: NSGA-II P=100, G=50,000, Pc=0.9, Pm=0.1."
        )
        self.traffic_status = tk.StringVar(
            value=(
                "Direct C&TBSA: warehouse grid + physical SKU data + order history."
            )
        )
        self.traffic_kpis = tk.StringVar(
            value="Groups —  · Unit visits —  · Mapped —  · Peak —  · P95 —  · Travel —  · Relocated —"
        )
        self.traffic_assignment_summary = tk.StringVar(
            value="Assigned SKUs — / —  · Optimized by C&TBSA —  · Fixed exceptions —  · Unassigned —"
        )
        self.traffic_exception_summary = tk.StringVar(
            value="Physical hard rules · OVERSIZE — · OVERWEIGHT — · INCOMPLETE DATA —"
        )
        self.traffic_view_mode = tk.StringVar(value="Feasibility seed")
        self.traffic_messages = queue.Queue()
        self.traffic_cancel_event = threading.Event()
        self.traffic_worker = None
        self.traffic_baseline_payload = None
        self.traffic_dataset = None
        self.traffic_network = None
        self.traffic_demand = None
        self.traffic_analysis = None
        self.traffic_result = None
        self.traffic_output_payload = None
        self.traffic_selected_unit = None
        self.traffic_pipeline_result = None
        self.traffic_building = None
        self.traffic_grid_project = None
        self.traffic_storage_layout = None
        self.traffic_loaded_grid_project_path = None
        self.traffic_racks = []
        self.traffic_zone_assignments = {}
        self.traffic_location_attributes = {}
        self.traffic_attribute_catalog = {}
        self.traffic_area_mode = tk.BooleanVar(value=False)

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        form = ttk.Frame(parent)
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        form.columnconfigure(0, weight=1, uniform="traffic_settings")
        form.columnconfigure(1, weight=1, uniform="traffic_settings")

        self.traffic_initial_settings_frame = ttk.LabelFrame(
            form,
            text="Warehouse and SKU constraints · Direct C&TBSA inputs",
            padding=10,
        )
        self.traffic_initial_settings_frame.grid(
            row=0, column=0, sticky="nsew", padx=(0, 5)
        )
        initial = self.traffic_initial_settings_frame
        initial.columnconfigure(1, weight=1)

        ttk.Label(initial, text="Grid project JSON").grid(
            row=0, column=0, sticky="w", pady=3
        )
        ttk.Entry(
            initial, textvariable=self.traffic_grid_project_path
        ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            initial, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_grid_project_path,
                [("Grid project JSON", "*.grid.json"), ("JSON", "*.json")],
            ),
        ).grid(row=0, column=2, pady=3)

        ttk.Label(initial, text="SKU demand + physical CSV").grid(
            row=1, column=0, sticky="w", pady=3
        )
        ttk.Entry(
            initial, textvariable=self.traffic_velocity_path
        ).grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            initial, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_velocity_path,
                [("CSV", "*.csv"), ("All files", "*")],
            ),
        ).grid(row=1, column=2, pady=3)

        ttk.Label(initial, text="SKU attributes CSV (optional)").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Entry(
            initial, textvariable=self.traffic_chilled_path
        ).grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            initial, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_chilled_path,
                [("CSV", "*.csv"), ("All files", "*")],
            ),
        ).grid(row=2, column=2, pady=3)


        ttk.Label(initial, text="Derived storage setup").grid(
            row=3, column=0, sticky="w", pady=3
        )
        setup = ttk.Frame(initial)
        setup.grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3
        )
        ttk.Label(setup, text="Unit").pack(side="left")
        ttk.Combobox(
            setup, textvariable=self.traffic_handling_unit, state="disabled",
            values=("AMR shelf", "Tote", "Pallet"), width=11,
        ).pack(side="left", padx=(5, 10))
        ttk.Label(setup, text="Levels").pack(side="left")
        ttk.Spinbox(
            setup, from_=1, to=100, textvariable=self.traffic_levels,
            width=4, state="disabled",
        ).pack(side="left", padx=(4, 8))
        ttk.Label(setup, text="Slots/level").pack(side="left")
        ttk.Spinbox(
            setup, from_=1, to=100, textvariable=self.traffic_slots,
            width=4, state="disabled",
        ).pack(side="left", padx=(4, 0))

        ttk.Label(
            initial,
            text=(
                "ABC and separate affinity slotting are not prerequisites. "
                "Exception inventory is placed only to establish feasibility."
            ),
            foreground="#4d646d",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.traffic_optimization_settings_frame = ttk.LabelFrame(
            form,
            text="Paper C&TBSA and static validation",
            padding=10,
        )
        self.traffic_optimization_settings_frame.grid(
            row=0, column=1, sticky="nsew", padx=(5, 0)
        )
        traffic_settings = self.traffic_optimization_settings_frame
        traffic_settings.columnconfigure(1, weight=1)

        ttk.Label(traffic_settings, text="Order-history Excel").grid(
            row=0, column=0, sticky="w", pady=3
        )
        ttk.Entry(
            traffic_settings, textvariable=self.traffic_order_path
        ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            traffic_settings, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_order_path,
                [("Excel workbook", "*.xlsx"), ("All files", "*")],
            ),
        ).grid(row=0, column=2, pady=3)

        ttk.Label(traffic_settings, text="Movement network").grid(
            row=1, column=0, sticky="w", pady=3
        )
        network_box = ttk.Combobox(
            traffic_settings,
            textvariable=self.traffic_network_mode,
            state="readonly",
            values=(
                "Use embedded RMF map",
                "Use network / grid project JSON",
            ),
            width=25,
        )
        network_box.grid(
            row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=3
        )
        network_box.bind(
            "<<ComboboxSelected>>", self.traffic_network_mode_changed
        )
        ttk.Label(traffic_settings, text="Network JSON").grid(
            row=2, column=0, sticky="w", pady=3
        )
        self.traffic_network_entry = ttk.Entry(
            traffic_settings, textvariable=self.traffic_network_path
        )
        self.traffic_network_entry.grid(
            row=2, column=1, sticky="ew", padx=(8, 4), pady=3
        )
        self.traffic_network_browse = ttk.Button(
            traffic_settings, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_network_path,
                [
                    ("Movement or grid project JSON", "*.json"),
                    ("All files", "*"),
                ],
            ),
        )
        self.traffic_network_browse.grid(row=2, column=2, pady=3)

        dates = ttk.Frame(traffic_settings)
        dates.grid(
            row=3, column=0, columnspan=3, sticky="w", pady=3
        )
        ttk.Label(dates, text="Inclusive dates").pack(side="left")
        ttk.Entry(dates, textvariable=self.traffic_start_date, width=11).pack(side="left", padx=(8, 3))
        ttk.Label(dates, text="to").pack(side="left")
        ttk.Entry(dates, textvariable=self.traffic_end_date, width=11).pack(side="left", padx=(3, 0))
        ttk.Label(
            dates, text="(YYYY-MM-DD; blank = full range)",
            foreground="#4d646d",
        ).pack(side="left", padx=(7, 0))

        parameters = ttk.Frame(traffic_settings)
        parameters.grid(
            row=4, column=0, columnspan=3, sticky="w", pady=3
        )
        ttk.Label(parameters, text="NSGA-II").pack(side="left")
        ttk.Label(parameters, text="Population").pack(side="left", padx=(8, 3))
        ttk.Entry(parameters, textvariable=self.traffic_ctbsa_population, width=6).pack(side="left")
        ttk.Label(parameters, text="Generations").pack(side="left", padx=(8, 3))
        ttk.Entry(parameters, textvariable=self.traffic_ctbsa_generations, width=8).pack(side="left")
        ttk.Label(parameters, text="C&TBSA solution").pack(side="left", padx=(8, 3))
        ttk.Spinbox(parameters, from_=1, to=5, textvariable=self.traffic_ctbsa_solution, width=3).pack(side="left")
        ttk.Label(parameters, text="Seed").pack(side="left", padx=(8, 3))
        ttk.Entry(parameters, textvariable=self.traffic_ctbsa_seed, width=5).pack(side="left")
        ttk.Checkbutton(
            parameters,
            text="Balance workload across zones",
            variable=self.traffic_zone_workload_enabled,
        ).pack(side="left", padx=(10, 0))

        ttk.Label(traffic_settings, text="Optimized layout output").grid(
            row=5, column=0, sticky="w", pady=3
        )
        ttk.Entry(
            traffic_settings, textvariable=self.traffic_output_path
        ).grid(row=5, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            traffic_settings,
            text="Browse…",
            command=lambda: self._browse_traffic_output(),
        ).grid(row=5, column=2, pady=3)

        ttk.Label(
            traffic_settings,
            text=(
                "Hard rules — Paper: each SKU appears in exactly one cluster; "
                "cluster SKU count ≤ shelf locations. Warehouse: AMR shelves, "
                "temperature and physical capacity enforced; exception racks fixed."
            ),
            foreground="#4d646d",
            wraplength=560,
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

        actions = ttk.Frame(form)
        actions.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0)
        )
        self.traffic_full_button = ttk.Button(
            actions,
            text="Run Direct C&TBSA",
            command=self.start_full_traffic_pipeline,
        )
        self.traffic_cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_traffic_work, state="disabled")
        self.traffic_save_button = ttk.Button(actions, text="Save layout", command=self.save_traffic_layout, state="disabled")
        self.traffic_export_button = ttk.Button(actions, text="Export report…", command=self.export_traffic_report, state="disabled")
        for widget in (
            self.traffic_full_button,
            self.traffic_cancel_button,
            self.traffic_save_button, self.traffic_export_button,
        ):
            widget.pack(side="left", padx=(0, 6))
        self.traffic_progress_value = tk.DoubleVar(value=0)
        self.traffic_progress = ttk.Progressbar(
            actions, variable=self.traffic_progress_value, maximum=100, length=180
        )
        self.traffic_progress.pack(side="left", padx=(8, 6))
        ttk.Label(actions, textvariable=self.traffic_parameter_status, foreground="#315b66").pack(side="left", padx=(5, 0))
        self.traffic_network_mode_changed()

        summary = ttk.Frame(parent, padding=(12, 2))
        summary.grid(row=1, column=0, sticky="ew")
        ttk.Label(
            summary,
            textvariable=self.traffic_assignment_summary,
            font=("TkDefaultFont", 10, "bold"),
            foreground="#174f5f",
        ).pack(anchor="w")
        ttk.Label(
            summary,
            textvariable=self.traffic_exception_summary,
            foreground="#8a4b08",
        ).pack(anchor="w")
        ttk.Label(summary, textvariable=self.traffic_kpis, font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        ttk.Label(summary, textvariable=self.traffic_status, foreground="#4d646d").pack(anchor="w", pady=(2, 0))

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew", padx=12, pady=(5, 12))
        map_frame = ttk.LabelFrame(body, text="Expected movement-resource traffic", padding=7)
        details = ttk.LabelFrame(body, text="Traffic analysis details", padding=7)
        body.add(map_frame, weight=3)
        body.add(details, weight=2)
        map_frame.columnconfigure(0, weight=1)
        map_frame.rowconfigure(2, weight=1)
        view_actions = ttk.Frame(map_frame)
        view_actions.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(view_actions, text="View").pack(side="left")
        view_box = ttk.Combobox(
            view_actions, textvariable=self.traffic_view_mode,
            state="readonly",
            values=("Feasibility seed", "C&TBSA result"), width=17,
        )
        view_box.pack(side="left", padx=(6, 0))
        view_box.bind(
            "<<ComboboxSelected>>",
            lambda _event: (self.populate_traffic_results(), self.draw_traffic_map()),
        )
        ttk.Label(view_actions, text="Zone overlay").pack(side="left", padx=(10, 3))
        zone_overlay = ttk.Combobox(
            view_actions,
            textvariable=self.traffic_zone_overlay,
            state="readonly",
            values=("Off", "Normalized demand", "Normalized traffic"),
            width=18,
        )
        zone_overlay.pack(side="left")
        zone_overlay.bind(
            "<<ComboboxSelected>>", lambda _event: self.draw_traffic_map()
        )
        ttk.Label(
            view_actions,
            text="Lane colour = route load · rack colour = handling-unit visits · purple = reassigned · ◇ endpoint",
            foreground="#4d646d",
        ).pack(side="left", padx=(10, 0))
        area_actions = ttk.Frame(map_frame)
        area_actions.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(
            area_actions, text="Load warehouse project",
            command=self.load_traffic_area_map,
        ).pack(side="left")
        ttk.Label(
            area_actions,
            text="Zones, chilled settings, capacities, and advanced attributes come from the grid project.",
            foreground="#4d646d",
        ).pack(side="left", padx=(10, 0))
        self.traffic_canvas = tk.Canvas(
            map_frame, background="white", highlightthickness=1,
            highlightbackground="#9aa8ae",
        )
        self.traffic_canvas.grid(row=2, column=0, sticky="nsew")
        self.traffic_canvas.bind("<Configure>", lambda _event: self.draw_traffic_map())
        self.enable_canvas_viewport(self.traffic_canvas)
        self.traffic_canvas.tag_bind("traffic_link", "<Button-1>", self.traffic_resource_click)

        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(details)
        tabs.grid(row=0, column=0, sticky="nsew")
        resource_tab, zone_tab, relocation_tab, rejected_tab, parameter_tab = (
            ttk.Frame(tabs), ttk.Frame(tabs), ttk.Frame(tabs), ttk.Frame(tabs),
            ttk.Frame(tabs),
        )
        tabs.add(resource_tab, text="Congested Resources")
        tabs.add(zone_tab, text="Zone Workload")
        tabs.add(relocation_tab, text="Relocations")
        tabs.add(rejected_tab, text="Fixed / Rejected")
        tabs.add(parameter_tab, text="Parameters")
        self.traffic_resource_tree = self._traffic_tree(
            resource_tab,
            (("resource", "Resource", 135), ("before", "Seed", 75),
             ("after", "C&TBSA", 75), ("change", "Change", 75),
             ("capacity", "Capacity", 75)),
        )
        self.traffic_zone_tree = self._traffic_tree(
            zone_tab,
            (("zone", "Zone", 80), ("capacity", "Usable slots", 90),
             ("demand", "Demand", 85), ("demand_norm", "Demand / cap", 95),
             ("traffic", "Traffic", 85), ("traffic_norm", "Traffic / cap", 95),
             ("racks", "Occupied racks", 95), ("skus", "SKUs", 65),
             ("quantity", "Quantity EA", 90)),
        )
        self.traffic_relocation_tree = self._traffic_tree(
            relocation_tab,
            (("unit", "Shelf", 105), ("sku", "SKU", 90),
             ("from", "From", 145), ("to", "To", 145)),
        )
        self.traffic_relocation_tree.bind("<<TreeviewSelect>>", self.traffic_relocation_select)
        self.traffic_rejected_tree = self._traffic_tree(
            rejected_tab,
            (("sku", "SKU", 85), ("class", "Physical class", 145),
             ("shelf", "Shelf", 105), ("location", "Fixed location", 155),
             ("reason", "Why C&TBSA cannot move it", 330)),
        )
        self.traffic_parameter_tree = self._traffic_tree(
            parameter_tab, (("parameter", "Parameter", 210), ("value", "Selected value", 190)),
        )

    @staticmethod
    def _traffic_tree(parent, specifications):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        columns = tuple(value[0] for value in specifications)
        tree = ttk.Treeview(parent, columns=columns, show="headings")
        for column, heading, width in specifications:
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w")
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return tree

    def traffic_network_mode_changed(self, _event=None):
        enabled = self.traffic_network_mode.get() != "Use embedded RMF map"
        state = "normal" if enabled else "disabled"
        self.traffic_network_entry.configure(state=state)
        self.traffic_network_browse.configure(state=state)


    def load_traffic_area_map(self):
        try:
            path = Path(
                self.traffic_grid_project_path.get()
            ).expanduser().resolve()
            project = self.rmf_maps.load_project(path)
            self.attributes.set_standard_storage_defaults(
                project.warehouse_storage_defaults
            )
            if project.storage_layout is None or not project.storage_layout.buffers:
                raise ValueError(
                    "grid project has no storage buffers; assign and save buffers "
                    "in Grid Map Editor first"
                )
            building = project.to_building_dict()
            _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
                building
            )
            roots = {
                str(item["grid_waypoint"]): str(item["buffer_id"]).split(
                    "/L", 1
                )[0]
                for item in project.storage_layout.buffers
            }
            for rack in racks:
                rack["static_bay_id"] = roots[rack["waypoint"]]
            network = self.traffic.network_from_rmf(building)
            zones = dict(project.zone_assignments)
            missing = sorted(
                rack["waypoint"] for rack in racks
                if rack["waypoint"] not in zones
            )
            if missing:
                raise ValueError(
                    f"grid project has {len(missing)} rack(s) without a warehouse "
                    "zone; assign all zones in Grid Map Editor first"
                )
            default_zone = next(iter(zones.values()), "Z01")
            self.slotting.apply_zone_local_aisles(
                building, racks, zones, default_zone
            )
            catalog = project.attribute_catalog
            paths = self.attributes.hierarchy_paths(
                racks,
                project.storage_layout.levels_per_rack,
                project.storage_layout.slots_per_level,
            )
            locations = self.attributes.validate_location_attributes(
                project.location_attributes, catalog, paths
            )
        except (
            OSError, ValueError, TypeError, KeyError, json.JSONDecodeError
        ) as exc:
            messagebox.showerror("Traffic storage-area map", str(exc))
            return False
        self.traffic_building = building
        self.traffic_grid_project = project
        self.traffic_storage_layout = project.storage_layout
        self.traffic_loaded_grid_project_path = path
        self.traffic_racks = racks
        self.traffic_network = network
        self.traffic_zone_assignments = zones
        self.traffic_location_attributes = locations
        self.traffic_attribute_catalog = self.attributes.normalize_catalog(catalog)
        if project.sku_attribute_source:
            self.traffic_chilled_path.set(project.sku_attribute_source)
        self.traffic_handling_unit.set(project.storage_layout.handling_unit_type)
        self.traffic_levels.set(str(project.storage_layout.levels_per_rack))
        self.traffic_slots.set(str(project.storage_layout.slots_per_level))
        self.traffic_area_mode.set(True)
        self.traffic_analysis = None
        self.traffic_result = None
        self.traffic_pipeline_result = None
        self.traffic_output_payload = None
        self.traffic_status.set(
            f"Loaded {len(project.storage_layout.buffers)} empty "
            f"{project.storage_layout.buffer_level} buffers and "
            f"{len(set(zones.values()))} configured warehouse zone(s)."
        )
        self.draw_traffic_map()
        return True

    def _browse_traffic_output(self):
        path = filedialog.asksaveasfilename(
            title="Save traffic-aware slotting layout",
            initialdir=str(Path(self.traffic_output_path.get()).expanduser().parent),
            initialfile=Path(self.traffic_output_path.get()).name,
            defaultextension=".slotting.json",
            filetypes=(("Slotting layout", "*.slotting.json"), ("JSON", "*.json")),
        )
        if path:
            self.traffic_output_path.set(path)

    def start_full_traffic_pipeline(self):
        self._start_traffic_work()

    def _start_traffic_work(self):
        if self.traffic_worker and self.traffic_worker.is_alive():
            return
        try:
            order_path = Path(self.traffic_order_path.get()).expanduser().resolve()
            grid_project_path = Path(
                self.traffic_grid_project_path.get()
            ).expanduser().resolve()
            velocity_path = Path(
                self.traffic_velocity_path.get()
            ).expanduser().resolve()
            chilled_path = (
                Path(self.traffic_chilled_path.get()).expanduser().resolve()
                if self.traffic_chilled_path.get().strip() else None
            )
            network_path = (
                Path(self.traffic_network_path.get()).expanduser().resolve()
                if self.traffic_network_mode.get() != "Use embedded RMF map"
                else None
            )
            start = (
                date.fromisoformat(self.traffic_start_date.get())
                if self.traffic_start_date.get().strip() else None
            )
            end = (
                date.fromisoformat(self.traffic_end_date.get())
                if self.traffic_end_date.get().strip() else None
            )
            ctbsa_parameters = CtbsaParameters(
                population_size=int(self.traffic_ctbsa_population.get()),
                generations=int(self.traffic_ctbsa_generations.get()),
                random_seed=int(self.traffic_ctbsa_seed.get()),
                selected_solution=int(self.traffic_ctbsa_solution.get()),
            )
            ctbsa_parameters.validate()
            grid_project = self.rmf_maps.load_project(grid_project_path)
            if (
                grid_project.storage_layout is None
                or not grid_project.storage_layout.buffers
            ):
                raise ValueError(
                    "grid project has no storage buffers; assign and save "
                    "buffers in Grid Map Editor first"
                )
            building = grid_project.to_building_dict()
            storage_layout = grid_project.storage_layout
            levels = int(storage_layout.levels_per_rack)
            slots = int(storage_layout.slots_per_level)
            handling_unit = storage_layout.handling_unit_type
            if self.traffic_loaded_grid_project_path != grid_project_path:
                if not self.load_traffic_area_map():
                    return
            inline_zones = copy.deepcopy(self.traffic_zone_assignments)
            inline_locations = copy.deepcopy(self.traffic_location_attributes)
            inline_catalog = copy.deepcopy(self.traffic_attribute_catalog)
            self.traffic_levels.set(str(levels))
            self.traffic_slots.set(str(slots))
            self.traffic_handling_unit.set(handling_unit)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Traffic-aware slotting", str(exc))
            return

        self.traffic_baseline_payload = None
        self.traffic_analysis = None
        self.traffic_result = None
        self.traffic_output_payload = None
        self.traffic_pipeline_result = None
        self.traffic_area_mode.set(False)
        self.traffic_cancel_event.clear()
        self.traffic_progress_value.set(0)
        self.traffic_assignment_summary.set(
            "Assigned SKUs — / —  · Reading SKU input…"
        )
        self.traffic_exception_summary.set(
            "Physical hard rules · calculating OVERSIZE, OVERWEIGHT, and incomplete-data inventory…"
        )
        self.traffic_status.set(
            "Starting direct paper C&TBSA from warehouse and order data…"
        )
        self._set_traffic_busy(True)

        def report(current, total, message):
            self.traffic_messages.put(("progress", current, total, message))

        def worker():
            try:
                dataset = self.affinity.load_orders(
                    order_path,
                    progress=lambda current, total, message: report(
                        current, total, message
                    ),
                    cancelled=self.traffic_cancel_event.is_set,
                )
                network = (
                    self.traffic.load_network(network_path)
                    if network_path is not None
                    else self.traffic.network_from_rmf(building)
                )
                sku_rows = self.slotting.load_velocity(
                    velocity_path, inline_catalog, chilled_path
                )
                self.traffic_messages.put(("sku_count", len(sku_rows)))
                order_analysis = self.affinity.analyze(dataset, start, end)
                pipeline = self.traffic.run_full_pipeline(
                    building, sku_rows, order_analysis, network,
                    initial_strategy="physical_feasibility",
                    affinity_weight=0.0,
                    levels_per_rack=levels,
                    slots_per_level=slots,
                    handling_unit_type=handling_unit,
                    zone_assignments=inline_zones,
                    attribute_catalog=inline_catalog,
                    location_attributes=inline_locations,
                    storage_layout=storage_layout,
                    start_date=start,
                    end_date=end,
                    ctbsa_parameters=ctbsa_parameters,
                    zone_workload_enabled=bool(
                        self.traffic_zone_workload_enabled.get()
                    ),
                    source_grid_project=str(grid_project_path),
                    source_velocity=str(velocity_path),
                    source_chilled=str(chilled_path or ""),
                    source_orders=str(order_path),
                    workflow_mode="direct_ctbsa",
                    progress=report,
                    cancelled=self.traffic_cancel_event.is_set,
                    assignment_progress=lambda summary: self.traffic_messages.put(
                        ("assignment_summary", summary)
                    ),
                )
                self.traffic_messages.put((
                    "done", pipeline.pretraffic_payload, dataset, network,
                    pipeline.baseline_demand, pipeline.pretraffic_analysis,
                    pipeline.optimization, pipeline.output_payload, pipeline,
                ))
            except (TrafficCancelledError, AffinityCancelledError) as exc:
                self.traffic_messages.put(("cancelled", str(exc)))
            except InsufficientStorageError as exc:
                self.traffic_messages.put(("capacity", exc))
            except Exception as exc:
                self.traffic_messages.put(("error", exc))

        self.traffic_worker = threading.Thread(target=worker, daemon=True)
        self.traffic_worker.start()
        self.root.after(80, self.poll_traffic_work)
    def _set_traffic_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.traffic_full_button.configure(state=state)
        self.traffic_cancel_button.configure(state="normal" if busy else "disabled")
        self.traffic_save_button.configure(
            state="disabled" if busy or self.traffic_output_payload is None else "normal"
        )
        self.traffic_export_button.configure(
            state="disabled" if busy or self.traffic_result is None else "normal"
        )

    def cancel_traffic_work(self):
        self.traffic_cancel_event.set()
        self.traffic_status.set("Cancelling traffic analysis…")

    def poll_traffic_work(self):
        handled = False
        while True:
            try:
                message = self.traffic_messages.get_nowait()
            except queue.Empty:
                break
            handled = True
            kind = message[0]
            if kind == "progress":
                _kind, current, total, status = message
                self.traffic_progress_value.set(100.0 * current / max(1, total))
                self.traffic_status.set(status)
            elif kind == "sku_count":
                total = int(message[1])
                self.traffic_assignment_summary.set(
                    f"Loaded SKUs {total:,}  · Building physical-feasibility assignments…"
                )
            elif kind == "assignment_summary":
                summary = message[1]
                assigned = int(summary.get("assigned_count", 0))
                total = int(summary.get("sku_count", assigned))
                unassigned = int(summary.get("unassigned_count", total - assigned))
                self.traffic_assignment_summary.set(
                    f"Assigned SKUs {assigned:,} / {total:,}  · "
                    f"Unassigned {unassigned:,}  · C&TBSA optimization in progress…"
                )
            elif kind == "done":
                self.complete_traffic_work(*message[1:])
            elif kind == "cancelled":
                self._set_traffic_busy(False)
                self.traffic_progress_value.set(0)
                self.traffic_status.set(message[1])
            elif kind == "capacity":
                self._set_traffic_busy(False)
                self.traffic_progress_value.set(0)
                error = message[1]
                summary = error.summary
                if not self.traffic_building:
                    self.load_traffic_area_map()
                counts = summary.get("unassigned_status_counts", {})
                needs_chilled_zone = bool(
                    counts.get("UNASSIGNED_NO_CHILLED_LOCATION", 0)
                )
                self.traffic_area_mode.set(needs_chilled_zone)
                self.draw_traffic_map()
                detail = "\n".join(
                    f"• {status}: {count}"
                    for status, count in sorted(counts.items()) if count
                )
                self.traffic_status.set(
                    "Generation stopped: update the warehouse grid project configuration."
                )
                messagebox.showwarning(
                    "Not enough compatible storage",
                    "All SKUs must be assigned, so no layout was accepted.\n\n"
                    f"Unassigned SKUs: {summary.get('unassigned_count', 0)}\n"
                    f"Configured slots: {summary.get('capacity', 0)}\n"
                    "Occupied slots required so far: "
                    f"{summary.get('occupied_slot_count', 0)}\n"
                    f"{detail}\n\n"
                    + (
                        "Configure sufficient chilled racks in Grid Map Editor, "
                        if needs_chilled_zone else "Increase project buffer capacity, "
                    )
                    + "save the grid project, reload it here, and generate again. "
                    "Standard and exception capacity follows the shared non-mixed rules.",
                )
            elif kind == "error":
                self._set_traffic_busy(False)
                self.traffic_progress_value.set(0)
                self.traffic_status.set("Traffic-aware slotting failed.")
                messagebox.showerror("Traffic-aware slotting", str(message[1]))
        if self.traffic_worker and self.traffic_worker.is_alive():
            self.root.after(80, self.poll_traffic_work)
        elif not handled:
            self._set_traffic_busy(False)

    def complete_traffic_work(
        self, payload, dataset, network, demand, analysis, result, output_payload,
        pipeline,
    ):
        self.traffic_baseline_payload = payload
        self.traffic_dataset = dataset
        self.traffic_network = network
        self.traffic_demand = demand
        self.traffic_analysis = analysis
        self.traffic_result = result
        self.traffic_output_payload = output_payload
        self.traffic_pipeline_result = pipeline
        self.traffic_last_workflow = pipeline.workflow_mode
        self.traffic_building = payload.get("building")
        self.traffic_selected_unit = None
        self.traffic_start_date.set(demand.start_date)
        self.traffic_end_date.set(demand.end_date)
        if result is not None:
            params = result.parameters
            self.traffic_ctbsa_population.set(str(params["population_size"]))
            self.traffic_ctbsa_generations.set(str(params["generations"]))
            self.traffic_ctbsa_solution.set(str(params["selected_solution"]))
            self.traffic_ctbsa_seed.set(str(params["random_seed"]))
            self.traffic_parameter_status.set(
                "Paper C&TBSA completed; zone balancing "
                + ("enabled." if params.get("zone_workload_enabled") else "disabled.")
            )
            self.traffic_view_mode.set("C&TBSA result")
        else:
            self.traffic_view_mode.set("Feasibility seed")
            self.traffic_parameter_status.set(
                "Traffic analysis completed without an optimization result."
            )
        self.traffic_progress_value.set(100)
        self._set_traffic_busy(False)
        self.populate_traffic_results()
        self.draw_traffic_map()

    def populate_traffic_results(self):
        for tree in (
            self.traffic_resource_tree, self.traffic_zone_tree,
            self.traffic_relocation_tree,
            self.traffic_rejected_tree, self.traffic_parameter_tree,
        ):
            tree.delete(*tree.get_children())
        before = self.traffic_result.before if self.traffic_result else self.traffic_analysis
        after = self.traffic_result.after if self.traffic_result else before
        if before is None:
            return
        relocated = len(self.traffic_result.relocations) if self.traffic_result else 0
        grouping = (
            self.traffic_pipeline_result.grouping_metrics
            if self.traffic_pipeline_result else {}
        )
        baseline_visits = int(grouping.get(
            "baseline_handling_unit_visits",
            before.demand.handling_unit_visits,
        ))
        strategy = str(grouping.get("initial_strategy", "basic"))
        strategy_label = (
            "Physical feasibility"
            if strategy == "physical_feasibility"
            else "ABC + Affinity" if strategy == "abc_affinity" else "ABC"
        )
        generation_summary = (
            self.traffic_baseline_payload.get("summary", {})
            if self.traffic_baseline_payload else {}
        )
        total_skus = int(generation_summary.get(
            "sku_count", len(self.traffic_baseline_payload.get("assignments", []))
            if self.traffic_baseline_payload else 0,
        ))
        assigned_skus = int(generation_summary.get(
            "assigned_count", total_skus - int(generation_summary.get("unassigned_count", 0)),
        ))
        unassigned_skus = int(generation_summary.get(
            "unassigned_count", max(0, total_skus - assigned_skus),
        ))
        optimized_skus = int(
            self.traffic_result.parameters.get("optimized_sku_count", 0)
            if self.traffic_result else 0
        )
        fixed_assigned_skus = max(0, assigned_skus - optimized_skus)
        optimized_loads = int(
            self.traffic_result.parameters.get("optimized_load_count", 0)
            if self.traffic_result else 0
        )
        compact_racks = int(
            self.traffic_result.parameters.get("compact_rack_count", 0)
            if self.traffic_result else generation_summary.get(
                "compact_rack_count", 0
            )
        )
        selected_racks = int(
            self.traffic_result.parameters.get(
                "selected_rack_count", compact_racks
            ) if self.traffic_result else compact_racks
        )
        self.traffic_assignment_summary.set(
            f"Assigned SKUs {assigned_skus:,} / {total_skus:,}  · "
            f"Optimized by C&TBSA {optimized_skus:,}  · "
            f"load targets {optimized_loads:,}  · "
            f"Fixed exceptions {fixed_assigned_skus:,}  · "
            f"Unassigned {unassigned_skus:,}  · "
            f"occupied racks {compact_racks:,} → {selected_racks:,}"
        )
        baseline_rows = (
            self.traffic_baseline_payload.get("assignments", [])
            if self.traffic_baseline_payload else []
        )
        verified_rows = [
            row for row in baseline_rows
            if not row.get("physical_missing_data_type")
        ]
        physical_classes = [
            str(row.get("physical_storage_class") or "").upper()
            for row in verified_rows
        ]
        oversize_count = sum(
            value in {"OVERSIZE", "OVERSIZE_AND_OVERWEIGHT"}
            for value in physical_classes
        )
        overweight_count = sum(
            value in {"OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"}
            for value in physical_classes
        )
        incomplete_count = sum(
            bool(row.get("physical_missing_data_type")) for row in baseline_rows
        )
        self.traffic_exception_summary.set(
            f"Physical hard rules · OVERSIZE {oversize_count:,} · "
            f"OVERWEIGHT {overweight_count:,} · INCOMPLETE DATA {incomplete_count:,} · "
            "fixed outside C&TBSA movement"
        )
        zone_before = before.zone_analysis.get("metrics", {})
        zone_after = after.zone_analysis.get("metrics", {})
        self.traffic_kpis.set(
            f"Groups {before.demand.fulfillment_groups:,}  · "
            f"Input unit visits {baseline_visits:,}  · "
            f"Mapped {len(before.mapped_units):,}  · "
            f"Raw peak {before.metrics['raw_peak_load']:.1f} → {after.metrics['raw_peak_load']:.1f}  · "
            f"Raw P95 {before.metrics['raw_p95_load']:.1f} → {after.metrics['raw_p95_load']:.1f}  · "
            f"Travel {before.metrics['expected_travel']:.1f} → {after.metrics['expected_travel']:.1f}  · "
            f"Zone demand peak {zone_before.get('peak_normalized_zone_demand', 0):.3f} → "
            f"{zone_after.get('peak_normalized_zone_demand', 0):.3f} "
            f"(P95 {zone_before.get('p95_normalized_zone_demand', 0):.3f} → "
            f"{zone_after.get('p95_normalized_zone_demand', 0):.3f})  · "
            f"Zone traffic peak {zone_before.get('peak_normalized_zone_traffic', 0):.3f} → "
            f"{zone_after.get('peak_normalized_zone_traffic', 0):.3f} "
            f"(P95 {zone_before.get('p95_normalized_zone_traffic', 0):.3f} → "
            f"{zone_after.get('p95_normalized_zone_traffic', 0):.3f})  · "
            f"Relocated {relocated:,}  · "
            f"Excluded unassigned {int(grouping.get('excluded_unassigned_sku_count', 0)):,}"
        )
        self.traffic_status.set(
            f"{len(before.unmapped_units):,} unmapped and {len(before.unreachable_units):,} unreachable units · "
            f"{len(before.demand.unmatched_skus):,} workbook SKUs absent from the layout · "
            f"hard validation {grouping.get('hard_validation_status', 'not run')} · "
            f"unverified physical SKUs {int(grouping.get('unverified_physical_sku_count', 0)):,} · "
            f"unassigned SKUs retained unchanged "
            f"{int(grouping.get('excluded_unassigned_sku_count', 0)):,}."
        )
        before_resources = {row["resource_id"]: row for row in before.resources}
        after_resources = {row["resource_id"]: row for row in after.resources}
        ranked = sorted(
            set(before_resources) | set(after_resources),
            key=lambda resource: -max(
                before_resources.get(resource, {}).get("normalized_load", 0),
                after_resources.get(resource, {}).get("normalized_load", 0),
            ),
        )
        for resource in ranked:
            first, second = before_resources.get(resource, {}), after_resources.get(resource, {})
            first_value = float(first.get("normalized_load", 0))
            second_value = float(second.get("normalized_load", 0))
            self.traffic_resource_tree.insert("", "end", values=(
                resource, f"{first_value:.3f}", f"{second_value:.3f}",
                f"{second_value - first_value:+.3f}", first.get("capacity") or "relative",
            ), tags=(f"resource:{resource}",))
        displayed_zone_analysis = (
            after.zone_analysis
            if self.traffic_result and self.traffic_view_mode.get() == "C&TBSA result"
            else before.zone_analysis
        )
        for row in displayed_zone_analysis.get("zones", []):
            self.traffic_zone_tree.insert("", "end", values=(
                row["zone_id"], f"{int(row['usable_slots']):,}",
                f"{row['expected_visits']:.1f}",
                f"{row['normalized_demand_workload']:.3f}",
                f"{row['attributed_resource_flow']:.1f}",
                f"{row['normalized_traffic_workload']:.3f}",
                f"{int(row['occupied_rack_count']):,}",
                f"{int(row['sku_count']):,}", f"{row['quantity_ea']:g}",
            ))
        if self.traffic_result:
            for row in self.traffic_result.relocations:
                self.traffic_relocation_tree.insert("", "end", values=(
                    row["handling_unit_id"], row.get("sku", ""),
                    row["from"], row["to"],
                ))
            rejected = list(self.traffic_result.rejected_units)
            rejected.extend(
                {"handling_unit_id": unit, "shelf_id": unit,
                 "reason": "no movement-network location mapping"}
                for unit in before.unmapped_units
            )
            rejected.extend(
                {"handling_unit_id": unit, "shelf_id": unit,
                 "reason": "no route to a service endpoint"}
                for unit in before.unreachable_units
            )
            for row in rejected:
                self.traffic_rejected_tree.insert("", "end", values=(
                    row.get("sku", ""),
                    row.get("physical_storage_class", "NETWORK"),
                    row.get("shelf_id", ""),
                    row.get("location", ""),
                    row["reason"],
                ))
            labels = {
                "population_size": "NSGA-II population",
                "generations": "NSGA-II generations",
                "crossover_probability": "PMX crossover probability",
                "mutation_probability": "2-opt mutation probability",
                "selected_solution": "Selected C&TBSA solution",
                "zone_workload_enabled": "Zone workload objective",
                "zone_workload_objective_order": "Zone objective order",
                "zone_workload_normalization": "Zone normalization",
                "zone_traffic_attribution": "Zone traffic attribution",
                "zone_optimization_status": "Zone optimization status",
                "optimized_sku_count": "Optimized SKUs",
                "fixed_sku_count": "Fixed physical-exception SKUs",
                "hard_rule_profile": "Hard-rule profile",
                "hard_rules": "Enforced hard rules",
                "hard_rule_validation": "Final hard-rule audit",
            }
            if grouping:
                for label, value in (
                    ("Traffic workflow", self.traffic_pipeline_result.workflow_mode),
                    ("Input preparation", strategy_label),
                    ("Baseline handling-unit visits", baseline_visits),
                    ("Fulfillment groups", before.demand.fulfillment_groups),
                ):
                    self.traffic_parameter_tree.insert("", "end", values=(label, value))
            for key, value in self.traffic_result.parameters.items():
                if key in {
                    "clusters", "regenerated_summary", "zone_analysis_before",
                    "zone_analysis_after",
                }:
                    continue
                if key == "hard_rules":
                    value = " · ".join(str(item) for item in value)
                elif key == "hard_rule_validation":
                    value = (
                        f"{value.get('hard_validation_status', 'UNKNOWN')} · "
                        f"assigned {int(value.get('assigned_sku_count', 0)):,} · "
                        f"excluded unassigned "
                        f"{int(value.get('excluded_unassigned_sku_count', 0)):,}"
                    )
                self.traffic_parameter_tree.insert("", "end", values=(labels.get(key, key), value))

    def _traffic_geometry(self):
        nodes = list(self.traffic_network.nodes.values())
        xs, ys = [node.x for node in nodes], [node.y for node in nodes]
        width, height, padding = max(400, self.traffic_canvas.winfo_width()), max(300, self.traffic_canvas.winfo_height()), 28
        scale = min(
            (width - 2 * padding) / max(max(xs) - min(xs), 1e-9),
            (height - 2 * padding) / max(max(ys) - min(ys), 1e-9),
        )
        return min(xs), min(ys), width, height, padding, scale

    def _traffic_point(self, node, geometry):
        min_x, min_y, _width, height, padding, scale = geometry
        return padding + (node.x - min_x) * scale, height - padding - (node.y - min_y) * scale

    @staticmethod
    def _traffic_heat_colour(ratio):
        """Readable blue-to-yellow-to-red traffic heat scale."""
        value = min(1.0, max(0.0, float(ratio)))
        stops = (
            (0.0, (50, 110, 180)),
            (0.45, (35, 180, 170)),
            (0.72, (246, 190, 60)),
            (1.0, (210, 55, 55)),
        )
        for index in range(1, len(stops)):
            left_value, left_colour = stops[index - 1]
            right_value, right_colour = stops[index]
            if value <= right_value:
                blend = (value - left_value) / (right_value - left_value)
                colour = tuple(
                    round(left_colour[channel] + blend * (
                        right_colour[channel] - left_colour[channel]
                    ))
                    for channel in range(3)
                )
                return "#" + "".join(f"{component:02x}" for component in colour)
        return "#d23737"

    def _traffic_racks_for_view(self):
        """Return one readable rack marker for every storage node."""
        rows = (
            self.traffic_result.assignments
            if self.traffic_result and self.traffic_view_mode.get() == "C&TBSA result"
            else (self.traffic_baseline_payload or {}).get("assignments", [])
        )
        racks = {}
        for row in rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            node_id = self.traffic._resolve_node(row, self.traffic_network)
            if node_id is None:
                continue
            bay = str(row.get("static_bay_id") or row.get("rack_id") or node_id)
            record = racks.setdefault(node_id, {
                "node_id": node_id,
                "bay": bay,
                "label": str(row.get("rack_id") or bay),
                "zone_id": str(
                    (self.traffic_zone_assignments or {}).get(
                        str(row.get("rack_id") or bay), "Z01"
                    )
                ),
                "units": set(),
                "occupied": 0,
            })
            record["units"].add(str(row.get("handling_unit_id", "")))
            record["occupied"] += 1
        keys_by_node = {}
        for key, node_id in self.traffic_network.storage_nodes.items():
            keys_by_node.setdefault(node_id, []).append(str(key))
        for node_id in sorted(set(self.traffic_network.storage_nodes.values())):
            if node_id in racks:
                continue
            labels = [
                value for value in keys_by_node.get(node_id, [])
                if not value.isdigit() and not value.startswith("vertex:")
            ]
            label = min(labels, key=lambda value: (len(value), value)) if labels else node_id
            racks[node_id] = {
                "node_id": node_id, "bay": label, "label": label,
                "zone_id": str(
                    (self.traffic_zone_assignments or {}).get(label, "Z01")
                ),
                "units": set(), "occupied": 0,
            }
        unit_visits = self.traffic_demand.unit_visits if self.traffic_demand else {}
        for record in racks.values():
            record["visits"] = sum(
                int(unit_visits.get(unit, 0)) for unit in record["units"]
            )
        return racks

    def _draw_traffic_area_setup(self):
        """Draw a readable rack-area editor before traffic analysis exists."""
        geometry = self._traffic_geometry()
        visible_segments = set()
        for link in self.traffic_network.links:
            segment = tuple(sorted((link.start, link.end)))
            if segment in visible_segments:
                continue
            visible_segments.add(segment)
            start = self.traffic_network.nodes[link.start]
            end = self.traffic_network.nodes[link.end]
            x1, y1 = self._traffic_point(start, geometry)
            x2, y2 = self._traffic_point(end, geometry)
            self.traffic_canvas.create_line(
                x1, y1, x2, y2, fill="#d9e1e5", width=3,
                tags=("traffic_area_lane",),
            )
        colour_by_temperature = {False: "#dce6ea", True: "#71c6e8"}
        for rack in self.traffic_racks:
            node = self.traffic_network.nodes.get(f"v:{rack['vertex_index']}")
            if node is None:
                continue
            x, y = self._traffic_point(node, geometry)
            zone = self.traffic_zone_assignments.get(rack["waypoint"], "Z01")
            effective = self.traffic_location_attributes.get(zone, {})
            chilled = effective.get("chilled") is True
            self.traffic_canvas.create_rectangle(
                x - 8, y - 8, x + 8, y + 8,
                fill=colour_by_temperature[chilled],
                outline="#314d59", width=2,
                tags=("traffic_area_rack", f"traffic_area:{zone}"),
            )
            self.traffic_canvas.create_text(
                x, y - 14, text=f"{rack['waypoint']} · {zone}",
                fill="#314d59", font=("TkDefaultFont", 7, "bold"),
            )
        self.apply_canvas_viewport(self.traffic_canvas)
        legend = "Temperature zones: grey ambient · blue chilled"
        self.traffic_canvas.create_text(
            12, 12, text=legend, anchor="nw", fill="#314d59",
            font=("TkDefaultFont", 8, "bold"),
        )

    def draw_traffic_map(self, highlight_unit=None):
        if not hasattr(self, "traffic_canvas"):
            return
        if highlight_unit is not None:
            self.traffic_selected_unit = highlight_unit
        highlight_unit = self.traffic_selected_unit
        self.traffic_canvas.delete("all")
        if (
            self.traffic_area_mode.get()
            and self.traffic_network is not None
            and self.traffic_building is not None
            and self.traffic_analysis is None
        ):
            self._draw_traffic_area_setup()
            return
        if self.traffic_network is None or self.traffic_analysis is None:
            self.traffic_canvas.create_text(
                max(200, self.traffic_canvas.winfo_width() / 2),
                max(150, self.traffic_canvas.winfo_height() / 2),
                text="Load the map to label storage areas, then run the pipeline.",
                fill="#93a7b5", font=("TkDefaultFont", 11),
            )
            self.apply_canvas_viewport(self.traffic_canvas)
            return
        analysis = (
            self.traffic_result.after
            if self.traffic_result and self.traffic_view_mode.get() == "C&TBSA result"
            else self.traffic_result.before if self.traffic_result else self.traffic_analysis
        )
        resource_rows = {row["resource_id"]: row for row in analysis.resources}
        positive_loads = [
            row["normalized_load"] for row in analysis.resources
            if row["normalized_load"] > 0
        ]
        # Clip the colour range at P95 so one extreme bottleneck does not make
        # every other lane look empty.
        maximum = float(np.percentile(positive_loads, 95)) if positive_loads else 1.0
        geometry = self._traffic_geometry()
        # A bidirectional RMF lane is represented by two graph links. Draw the
        # physical segment only once; direction remains part of routing, not UI.
        visible_links = {}
        for link in self.traffic_network.links:
            key = (tuple(sorted((link.start, link.end))), link.resource_id)
            visible_links.setdefault(key, link)
        for link in visible_links.values():
            start, end = self.traffic_network.nodes[link.start], self.traffic_network.nodes[link.end]
            x1, y1 = self._traffic_point(start, geometry)
            x2, y2 = self._traffic_point(end, geometry)
            value = resource_rows.get(link.resource_id, {}).get("normalized_load", 0.0)
            ratio = min(1.0, value / maximum)
            colour = self._traffic_heat_colour(ratio)
            self.traffic_canvas.create_line(
                x1, y1, x2, y2, fill="#d9e1e5", width=7,
                tags=("traffic_lane_background",),
            )
            self.traffic_canvas.create_line(
                x1, y1, x2, y2, fill=colour,
                width=2.0 + 3.5 * math.sqrt(ratio),
                tags=("traffic_link", f"resource:{link.resource_id}"),
            )

        relocations = self.traffic_result.relocations if self.traffic_result else []
        reassigned_bays = {
            str(value)
            for row in relocations
            for value in (
                row.get("from_rack") or row.get("from"),
                row.get("to_rack") or row.get("to"),
            )
            if value
        }
        selected_rows = [
            row for row in relocations
            if highlight_unit and row.get("handling_unit_id") == highlight_unit
        ]
        selected_from = {
            str(row.get("from_rack") or row.get("from"))
            for row in selected_rows
        }
        selected_to = {
            str(row.get("to_rack") or row.get("to"))
            for row in selected_rows
        }
        racks = self._traffic_racks_for_view()
        positive_rack_visits = [
            rack["visits"] for rack in racks.values() if rack["visits"] > 0
        ]
        rack_heat_maximum = (
            float(np.percentile(positive_rack_visits, 95))
            if positive_rack_visits else 1.0
        )
        zone_overlay = self.traffic_zone_overlay.get()
        zone_metric = (
            "normalized_demand_workload"
            if zone_overlay == "Normalized demand"
            else "normalized_traffic_workload"
        )
        zone_rows = {
            row["zone_id"]: row
            for row in analysis.zone_analysis.get("zones", [])
        }
        positive_zone_values = [
            float(row.get(zone_metric, 0.0)) for row in zone_rows.values()
            if float(row.get(zone_metric, 0.0)) > 0
        ]
        zone_heat_maximum = (
            float(np.percentile(positive_zone_values, 95))
            if positive_zone_values else 1.0
        )
        show_all_labels = len(racks) <= 60
        for node_id, rack in sorted(racks.items(), key=lambda item: item[1]["label"]):
            node = self.traffic_network.nodes[node_id]
            x, y = self._traffic_point(node, geometry)
            bay = rack["bay"]
            selected_source = bay in selected_from
            selected_destination = bay in selected_to
            reassigned = bay in reassigned_bays
            visit_ratio = min(1.0, rack["visits"] / rack_heat_maximum)
            if zone_overlay != "Off":
                zone_value = float(
                    zone_rows.get(rack.get("zone_id", "Z01"), {}).get(
                        zone_metric, 0.0
                    )
                )
                fill = (
                    self._traffic_heat_colour(
                        min(1.0, zone_value / zone_heat_maximum)
                    ) if zone_value > 0 else "#f2f5f6"
                )
            else:
                fill = (
                    self._traffic_heat_colour(visit_ratio)
                    if rack["visits"] > 0 else "#f2f5f6"
                )
            outline = "#e07a1f" if selected_source else "#258b55" if selected_destination else "#7b2cbf" if reassigned else "#344f5c"
            width = 4 if selected_source or selected_destination else 3 if reassigned else 1
            size = 8 if selected_source or selected_destination else 6
            tags = ("traffic_rack", f"traffic_rack:{bay}") + (("reassigned_rack",) if reassigned else ())
            self.traffic_canvas.create_rectangle(
                x - size, y - size, x + size, y + size,
                fill=fill, outline=outline, width=width, tags=tags,
            )
            if show_all_labels or reassigned or selected_source or selected_destination:
                suffix = " FROM" if selected_source else " TO" if selected_destination else ""
                self.traffic_canvas.create_text(
                    x, y - size - 5,
                    text=f"{rack['label']}{suffix} · {rack['visits']:,}",
                    fill="#6a1b83" if reassigned else "#344f5c",
                    font=("TkDefaultFont", 7, "bold" if reassigned else "normal"),
                    tags=("traffic_rack_label",),
                )

        endpoint_nodes = {endpoint.node_id for endpoint in self.traffic_network.endpoints}
        endpoint_labels = {
            endpoint.node_id: endpoint.endpoint_id or endpoint.node_id
            for endpoint in self.traffic_network.endpoints
        }
        for node_id in endpoint_nodes:
            node = self.traffic_network.nodes[node_id]
            x, y = self._traffic_point(node, geometry)
            self.traffic_canvas.create_polygon(
                x, y - 8, x + 8, y, x, y + 8, x - 8, y,
                fill="#224f88", outline="white", width=2,
            )
            self.traffic_canvas.create_text(
                x, y - 13, text=endpoint_labels[node_id], fill="#224f88",
                font=("TkDefaultFont", 8, "bold"),
            )
        legend_mode = (
            "rack visits" if zone_overlay == "Off"
            else f"zone {zone_overlay.lower()} (P95 colour scale)"
        )
        self.traffic_canvas.create_text(
            12, 12, text=f"Rack colour: {legend_mode}", anchor="nw",
            fill="#314d59", font=("TkDefaultFont", 8, "bold"),
            tags=("traffic_legend",),
        )
        self.apply_canvas_viewport(self.traffic_canvas)

    def traffic_resource_click(self, _event=None):
        current = self.traffic_canvas.find_withtag("current")
        if not current:
            return
        resource = next(
            (tag.split(":", 1)[1] for tag in self.traffic_canvas.gettags(current[0]) if tag.startswith("resource:")),
            None,
        )
        if resource:
            analysis = (
                self.traffic_result.after
                if self.traffic_result
                and self.traffic_view_mode.get() == "C&TBSA result"
                else self.traffic_analysis
            )
            row = next((row for row in analysis.resources if row["resource_id"] == resource), None)
            if row:
                contributors = ", ".join(
                    f"{value['handling_unit_id']} ({value['flow']:.1f})"
                    for value in row["contributors"][:5]
                ) or "none"
                self.traffic_status.set(
                    f"Resource {resource}: load {row['load']:.2f}, normalized {row['normalized_load']:.3f}; top units: {contributors}"
                )

    def traffic_relocation_select(self, _event=None):
        selection = self.traffic_relocation_tree.selection()
        if not selection:
            return
        unit = str(self.traffic_relocation_tree.item(selection[0], "values")[0])
        self.draw_traffic_map(unit)

    def save_traffic_layout(self):
        if self.traffic_output_payload is None:
            return
        try:
            path = Path(self.traffic_output_path.get()).expanduser()
            self.layouts.save_payload(self.traffic_output_payload, path)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Save traffic-aware layout", str(exc))
            return
        self.traffic_status.set(f"Saved traffic-aware slotting layout to {path}")

    def export_traffic_report(self):
        if self.traffic_result is None or self.traffic_network is None:
            return
        path = filedialog.asksaveasfilename(
            title="Export traffic analysis",
            initialdir=str(DEFAULT_TRAFFIC_REPORT.parent),
            initialfile="traffic_analysis.traffic.json",
            defaultextension=".traffic.json",
            filetypes=(("Traffic analysis", "*.traffic.json"), ("JSON", "*.json")),
        )
        if not path:
            return
        try:
            paths = self.traffic.export(self.traffic_result, Path(path), self.traffic_network)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Export traffic report", str(exc))
            return
        self.traffic_status.set(f"Exported traffic analysis to {paths[0].parent}")
        messagebox.showinfo(
            "Traffic analysis export",
            "Created:\n" + "\n".join(path.name for path in paths),
        )

    def _build_operations_tab(self,parent):
        self.ops_layout_path=tk.StringVar(value=str(DEFAULT_SLOTTING_OUTPUT))
        self.ops_history_path=tk.StringVar(value=str(DEFAULT_AFFINITY_INPUT))
        self.ops_search=tk.StringVar(); self.ops_source_sku=tk.StringVar(); self.ops_target_sku=tk.StringVar()
        self.ops_source_label=tk.StringVar(value="Source SKU");self.ops_target_label=tk.StringVar(value="Target SKU")
        self.ops_swap_mode=tk.StringVar(value="SKU slot")
        self.ops_status=tk.StringVar(value="Load a generated slotting layout to begin.")
        self.ops_movement_status=tk.StringVar(value="Load a layout and import order history to rank unit movements.")
        self.ops_details=tk.StringVar(value="Search for a SKU to show its current addresses and map position.")
        self.ops_payload=None; self.ops_building=None; self.ops_rows=[]; self.ops_racks=[]; self.ops_highlight_rack=None
        self.ops_search_racks=set();self.ops_search_skus=set()
        self.ops_shelf_selection=[];self.ops_sku_selection=[];self.ops_inventory_rows={}
        self.ops_movement_by_unit={};self.ops_movement_summary={};self.ops_movement_analysis=None
        parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=1)
        top=ttk.LabelFrame(parent,text="Slotting layout",padding=10); top.grid(row=0,column=0,sticky="ew",padx=12,pady=12); top.columnconfigure(1,weight=1)
        ttk.Label(top,text="Layout JSON").grid(row=0,column=0,sticky="w",padx=(0,8))
        ttk.Entry(top,textvariable=self.ops_layout_path).grid(row=0,column=1,sticky="ew")
        ttk.Button(top,text="Browse…",command=self.browse_ops_layout).grid(row=0,column=2,padx=(8,0))
        ttk.Button(top,text="Load layout",command=self.load_ops_layout).grid(row=0,column=3,padx=(6,0))
        ttk.Button(top,text="Save changes as…",command=self.save_ops_layout).grid(row=0,column=4,padx=(6,0))
        ttk.Label(top,text="Order-history Excel").grid(row=1,column=0,sticky="w",padx=(0,8),pady=(7,0))
        ttk.Entry(top,textvariable=self.ops_history_path).grid(row=1,column=1,sticky="ew",pady=(7,0))
        ttk.Button(
            top,text="Browse…",
            command=lambda:self.browse_slot_input(
                self.ops_history_path,
                [("Excel workbook","*.xlsx"),("All files","*")],
            ),
        ).grid(row=1,column=2,padx=(8,0),pady=(7,0))
        ttk.Button(top,text="Calculate movement ranks",command=self.calculate_ops_movement_ranks).grid(row=1,column=3,columnspan=2,sticky="w",padx=(6,0),pady=(7,0))
        ttk.Label(top,textvariable=self.ops_status,foreground="#315b66").grid(row=2,column=0,columnspan=5,sticky="w",pady=(7,0))
        ttk.Label(top,textvariable=self.ops_movement_status,foreground="#315b66").grid(row=3,column=0,columnspan=5,sticky="w",pady=(4,0))

        paned=ttk.Panedwindow(parent,orient="horizontal"); paned.grid(row=1,column=0,sticky="nsew",padx=12,pady=(0,12))
        map_frame=ttk.LabelFrame(paned,text="Current inventory layout",padding=8)
        control=ttk.Frame(paned,padding=8); paned.add(map_frame,weight=3); paned.add(control,weight=2)
        map_frame.columnconfigure(0,weight=1); map_frame.rowconfigure(0,weight=1)
        control.columnconfigure(0,weight=1); control.rowconfigure(2,weight=2); control.rowconfigure(5,weight=1)
        self.ops_canvas=tk.Canvas(map_frame,background="white",highlightthickness=1,highlightbackground="#9aa8ae")
        self.ops_canvas.grid(row=0,column=0,sticky="nsew"); self.ops_canvas.bind("<Configure>",lambda _event:self.draw_ops_layout())
        self.enable_canvas_viewport(self.ops_canvas)
        self.ops_canvas.tag_bind("ops_rack","<Button-1>",self.ops_rack_click)
        ttk.Label(map_frame,text="Read-only assignment view · highlighted ring = searched SKU or selected swap position · click a rack to list inventory",foreground="#4d646d").grid(row=1,column=0,sticky="w",pady=(5,0))
        self.ops_dot_legend=ttk.Frame(map_frame);self.ops_dot_legend.grid(row=2,column=0,sticky="w",pady=(4,0))
        self.ops_dot_legend_labels=[]
        for column,(colour,label) in enumerate((("#d1495b","A movement unit"),("#f3a712","B movement unit"),("#4c9f70","C movement unit"),("#7b8b92","Unranked unit"))):
            widget=ttk.Label(self.ops_dot_legend,text=f"● {label}",foreground=colour)
            widget.grid(row=0,column=column,padx=(0,12),sticky="w");self.ops_dot_legend_labels.append(widget)
        workstation_legend=ttk.Label(self.ops_dot_legend,text="◆ Workstation",foreground="#277da1")
        workstation_legend.grid(row=0,column=4,padx=(0,12),sticky="w");self.ops_dot_legend_labels.append(workstation_legend)
        ttk.Label(self.ops_dot_legend,text="AMR: shelf dot · ASRS: slot dots · outline + badge = zone",foreground="#4d646d").grid(row=0,column=5,sticky="w")

        search=ttk.LabelFrame(control,text="1. Find SKU",padding=10); search.grid(row=0,column=0,sticky="ew",pady=(0,8)); search.columnconfigure(0,weight=1)
        ttk.Entry(search,textvariable=self.ops_search).grid(row=0,column=0,sticky="ew")
        ttk.Button(search,text="Search",command=self.search_ops_sku).grid(row=0,column=1,padx=(6,0))
        ttk.Label(control,textvariable=self.ops_details,justify="left",wraplength=470).grid(row=1,column=0,sticky="ew",pady=(0,10))

        inventory=ttk.LabelFrame(control,text="SELECTED RACK INVENTORY",padding=6); inventory.grid(row=2,column=0,sticky="nsew",pady=(0,8)); inventory.columnconfigure(0,weight=1); inventory.rowconfigure(0,weight=1)
        columns=("sku","quantity","class","flags","static","dynamic","unit")
        self.ops_inventory_tree=ttk.Treeview(inventory,columns=columns,show="headings",height=8)
        headings={"sku":"SKU","quantity":"Qty in rack (EA)","class":"ABC","flags":"Storage flags","static":"Static address","dynamic":"Occupied dynamic address","unit":"Shelf / unit"}
        widths={"sku":100,"quantity":100,"class":45,"flags":180,"static":190,"dynamic":220,"unit":110}
        for column in columns:
            self.ops_inventory_tree.heading(column,text=headings[column]);self.ops_inventory_tree.column(column,width=widths[column],anchor="center" if column in {"class","unit"} else "w")
        inventory_y=ttk.Scrollbar(inventory,orient="vertical",command=self.ops_inventory_tree.yview)
        inventory_x=ttk.Scrollbar(inventory,orient="horizontal",command=self.ops_inventory_tree.xview)
        self.ops_inventory_tree.configure(yscrollcommand=inventory_y.set,xscrollcommand=inventory_x.set)
        self.ops_inventory_tree.grid(row=0,column=0,sticky="nsew");inventory_y.grid(row=0,column=1,sticky="ns");inventory_x.grid(row=1,column=0,sticky="ew")
        self.ops_inventory_tree.bind("<<TreeviewSelect>>",self.ops_inventory_select)

        swap=ttk.LabelFrame(control,text="2. Mock position swap",padding=10); swap.grid(row=3,column=0,sticky="ew",pady=(0,8)); swap.columnconfigure(1,weight=1)
        ttk.Label(swap,textvariable=self.ops_source_label).grid(row=0,column=0,sticky="w",padx=(0,8),pady=3); ttk.Entry(swap,textvariable=self.ops_source_sku).grid(row=0,column=1,sticky="ew",pady=3)
        ttk.Label(swap,textvariable=self.ops_target_label).grid(row=1,column=0,sticky="w",padx=(0,8),pady=3); ttk.Entry(swap,textvariable=self.ops_target_sku).grid(row=1,column=1,sticky="ew",pady=3)
        ttk.Label(swap,text="Swap type").grid(row=2,column=0,sticky="w",padx=(0,8),pady=3)
        self.ops_swap_box=ttk.Combobox(swap,textvariable=self.ops_swap_mode,state="readonly",values=("SKU slot","Whole shelf"),width=18)
        self.ops_swap_box.grid(row=2,column=1,sticky="w",pady=3);self.ops_swap_box.bind("<<ComboboxSelected>>",self.ops_swap_mode_changed)
        ttk.Button(swap,text="Execute mock swap",command=self.execute_ops_swap).grid(row=3,column=1,sticky="w",pady=(8,2))
        ttk.Label(swap,text="SKU slot: click two SKU rows. Whole shelf: click two occupied rack points. Review the fields, then execute.",foreground="#4d646d",wraplength=390).grid(row=4,column=0,columnspan=2,sticky="w",pady=(6,0))

        ttk.Label(control,text="OPERATION LOG",font=("TkDefaultFont",10,"bold")).grid(row=4,column=0,sticky="w",pady=(6,4))
        self.ops_log=tk.Listbox(control,height=8); self.ops_log.grid(row=5,column=0,sticky="nsew")

    def browse_ops_layout(self):
        path=filedialog.askopenfilename(filetypes=[("Slotting layout","*.slotting.json"),("JSON","*.json"),("All files","*")],initialdir=str(Path(self.ops_layout_path.get()).expanduser().parent))
        if path:self.ops_layout_path.set(path)

    def load_ops_layout(self):
        try:
            payload=self.layouts.load(Path(self.ops_layout_path.get()).expanduser())
            _,racks,workstations,unreachable=self.slotting.rack_distances(payload["building"])
            zones=dict(payload.get("zone_assignments",{}))
            self.slotting.apply_zone_local_aisles(
                payload["building"],racks,zones,next(iter(zones.values()),"Z01")
            )
        except (OSError,ValueError,TypeError,json.JSONDecodeError,yaml.YAMLError) as exc:
            messagebox.showerror("Layout load failed",str(exc));return
        self.ops_payload=payload;self.ops_building=payload["building"];self.ops_rows=payload["assignments"];self.ops_racks=racks;self.ops_highlight_rack=None;self.ops_search_racks=set();self.ops_search_skus=set();self.ops_shelf_selection=[];self.ops_sku_selection=[]
        self.ops_source_sku.set("");self.ops_target_sku.set("")
        source_history=(payload.get("sources",{}).get("movement_order_workbook") or payload.get("sources",{}).get("traffic_order_workbook") or payload.get("sources",{}).get("affinity_order_workbook") or "")
        if source_history:self.ops_history_path.set(source_history)
        self.ops_movement_by_unit={};self.ops_movement_summary={};self.ops_movement_analysis=None
        self.ops_movement_status.set("Order history selected. Calculate movement ranks for this layout." if self.ops_history_path.get().strip() else "Import order-history Excel to calculate movement ranks.")
        self.show_ops_rack_inventory(None)
        self.ops_log.delete(0,"end")
        for event in payload.get("operation_log",[]):self.ops_log.insert("end",event.get("message",str(event)))
        self.ops_status.set(f"Loaded {len(self.ops_rows):,} SKU assignments · {len(racks)} racks · {workstations} workstations · {unreachable} unreachable racks")
        self.ops_details.set("Search for a SKU to show its current addresses and map position.");self.draw_ops_layout()

    def calculate_ops_movement_ranks(self):
        if not self.ops_rows:
            messagebox.showerror("Movement ranking","Load a slotting layout first.");return
        try:
            history_path=Path(self.ops_history_path.get().strip()).expanduser().resolve()
            self.ops_movement_status.set("Loading historical store orders…");self.root.update_idletasks()
            analysis=self.affinity.analyze(self.affinity.load_orders(history_path))
            movement=self.slotting.handling_unit_visit_metrics(
                analysis,self.ops_rows,self.ops_payload.get("handling_unit_type","AMR shelf")
            )
            if not movement["units"]:raise ValueError("no assigned layout SKUs match the order-history workbook")
        except (OSError,ValueError,TypeError,KeyError) as exc:
            self.ops_movement_status.set("Movement ranking failed.");messagebox.showerror("Movement ranking",str(exc));return
        self.ops_movement_analysis=analysis
        self.ops_payload.setdefault("sources",{})["movement_order_workbook"]=str(history_path)
        self.set_ops_movement_metrics(movement)

    def set_ops_movement_metrics(self,movement):
        self.ops_movement_summary=movement
        self.ops_movement_by_unit={row["handling_unit_id"]:row for row in movement["units"]}
        counts={label:sum(row["movement_class"]==label for row in movement["units"]) for label in ("A","B","C")}
        self.ops_movement_status.set(
            f"{movement['fulfillment_group_count']:,} Store ID + Date tasks · "
            f"{movement['total_handling_unit_visits']:,} {movement['handling_unit_type']} visits · "
            f"ranked at {movement['ranking_level']} level · A {counts['A']} / B {counts['B']} / C {counts['C']}"
        )
        self.draw_ops_layout()

    def save_ops_layout(self):
        if not self.ops_payload:
            messagebox.showinfo("Load layout","Load a slotting layout first.");return
        path=filedialog.asksaveasfilename(defaultextension=".slotting.json",filetypes=[("Slotting layout","*.slotting.json"),("JSON","*.json")],initialdir=str(Path(self.ops_layout_path.get()).expanduser().parent),initialfile=Path(self.ops_layout_path.get()).name)
        if not path:return
        self.ops_payload["assignments"]=self.ops_rows;self.ops_payload["modified_at"]=datetime.now(timezone.utc).isoformat()
        self.layouts.save_payload(self.ops_payload,Path(path));self.ops_layout_path.set(path);self.ops_status.set(f"Saved modified layout: {path}")

    def ops_geometry(self,vertices):
        xs=[float(v[0]) for v in vertices];ys=[float(v[1]) for v in vertices];min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
        width=max(300,self.ops_canvas.winfo_width());height=max(300,self.ops_canvas.winfo_height());padding=55
        scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y));return min_x,max_x,min_y,max_y,width,height,padding,scale

    def ops_screen_point(self,x,y,geometry):
        min_x,max_x,min_y,max_y,width,height,padding,scale=geometry;sx=padding+(float(x)-min_x)*scale
        sy=padding+(float(y)-min_y)*scale if self.ops_building.get("coordinate_system")=="reference_image" else height-padding-(float(y)-min_y)*scale
        return sx,sy

    def draw_ops_layout(self):
        if not hasattr(self,"ops_canvas"):return
        self.ops_canvas.delete("all")
        if not self.ops_building:return
        _,level=next(iter(self.ops_building["levels"].items()));vertices=level.get("vertices",[])
        if not vertices:return
        geometry=self.ops_geometry(vertices)
        for lane in level.get("lanes",[]):
            if len(lane)<2:continue
            a,b=vertices[lane[0]],vertices[lane[1]];x1,y1=self.ops_screen_point(a[0],a[1],geometry);x2,y2=self.ops_screen_point(b[0],b[1],geometry);self.ops_canvas.create_line(x1,y1,x2,y2,fill="#d9e0e3")
        zone_palette=("#6c8cd5","#31a6a0","#d47b4c","#8ca63c","#c75d8b","#81756e","#3d8fbe","#9b70c7")
        zone_ids=sorted({str(rack.get("zone_id","")).strip() for rack in self.ops_racks if str(rack.get("zone_id","")).strip()})
        zone_colours={zone:zone_palette[index%len(zone_palette)] for index,zone in enumerate(zone_ids)}
        for zone in zone_ids:
            positions=[self.ops_screen_point(rack["x"],rack["y"],geometry) for rack in self.ops_racks if str(rack.get("zone_id","")).strip()==zone]
            if not positions:continue
            left=min(x for x,_y in positions)-10;top=min(y for _x,y in positions)-10
            right=max(x for x,_y in positions)+10;bottom=max(y for _x,y in positions)+10
            self.ops_canvas.create_rectangle(left,top,right,bottom,outline=zone_colours[zone],width=3,tags=("ops_zone_boundary",))
            badge_width=max(42,len(zone)*8+14);badge_top=top-26
            self.ops_canvas.create_rectangle(left,badge_top,left+badge_width,badge_top+22,fill=zone_colours[zone],outline=zone_colours[zone],tags=("ops_zone_label_badge",))
            self.ops_canvas.create_text(left+badge_width/2,badge_top+11,text=zone,fill="white",font=("TkDefaultFont",9,"bold"),tags=("ops_zone_label",))
        grouped={}
        for row in self.ops_rows:
            if row.get("assignment_status")=="ASSIGNED":grouped.setdefault(row.get("rack_id",""),[]).append(row)
        handling_unit_type=(self.ops_payload or {}).get("handling_unit_type","AMR shelf")
        capacity=(self.ops_payload or {}).get("rack_capacity",{})
        level_count=max(1,int(capacity.get("levels",1)));slot_count=max(1,int(capacity.get("slots_per_level",1)))
        class_colours={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
        shelf_racks={selection["rack_id"] for selection in self.ops_shelf_selection}
        for rack in self.ops_racks:
            rack_id=rack["rack_id"];x,y=self.ops_screen_point(rack["x"],rack["y"],geometry);rows=grouped.get(rack_id,[])
            shelf_selected=rack_id in shelf_racks;selected=rack_id==self.ops_highlight_rack
            search_match=rack_id in self.ops_search_racks
            outline="#e07a1f" if shelf_selected else ("#065f69" if selected else ("#087f8c" if search_match else "white"))
            if handling_unit_type=="AMR shelf":
                unit_id=str(rows[0].get("handling_unit_id","")) if rows else ""
                movement=self.ops_movement_by_unit.get(unit_id,{})
                fill=class_colours.get(movement.get("movement_class",""),"#7b8b92")
                radius=11 if shelf_selected or selected else (9 if search_match else 4)
                self.ops_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline=outline,width=4 if shelf_selected else (4 if selected else (3 if search_match else 1)),tags=("ops_rack",f"opsrack:{rack_id}",f"opsunit:{unit_id}"))
            else:
                outer_radius=13 if selected else (11 if search_match else 7)
                rack_outline="#065f69" if selected else ("#087f8c" if search_match else "#aab4b8")
                self.ops_canvas.create_oval(x-outer_radius,y-outer_radius,x+outer_radius,y+outer_radius,fill="",outline=rack_outline,width=4 if selected else (3 if search_match else 1),tags=("ops_rack",f"opsrack:{rack_id}"))
                locations={}
                for row in rows:
                    occupied=row.get("occupied_handling_units") or [{"handling_unit_id":row.get("handling_unit_id",""),"storage_level":row.get("storage_level",1),"storage_slot":row.get("storage_slot",1)}]
                    for location in occupied:
                        unit_id=str(location.get("handling_unit_id","")).strip()
                        if unit_id:locations.setdefault(unit_id,location)
                dx=min(6.0,30.0/max(1,slot_count-1));dy=min(6.0,30.0/max(1,level_count-1))
                for unit_id,location in locations.items():
                    slot=int(location.get("storage_slot") or 1);storage_level=int(location.get("storage_level") or 1)
                    unit_x=x+(slot-(slot_count+1)/2)*dx;unit_y=y+(storage_level-(level_count+1)/2)*dy
                    movement=self.ops_movement_by_unit.get(unit_id,{})
                    fill=class_colours.get(movement.get("movement_class",""),"#7b8b92")
                    self.ops_canvas.create_oval(unit_x-3,unit_y-3,unit_x+3,unit_y+3,fill=fill,outline="white",width=1,tags=("ops_rack",f"opsrack:{rack_id}",f"opsunit:{unit_id}"))
            if selected or search_match:
                self.ops_canvas.create_text(x,y-18,text=rack_id,fill="#065f69",font=("TkDefaultFont",9,"bold"))
        for vertex in vertices:
            params=vertex[4] if len(vertex)>4 and isinstance(vertex[4],dict) else {}
            if "dropoff_ingestor" not in params:continue
            endpoint=str(self.slotting.typed_value(params["dropoff_ingestor"],vertex[3]));x,y=self.ops_screen_point(vertex[0],vertex[1],geometry);radius=6
            self.ops_canvas.create_polygon(x,y-radius,x+radius,y,x,y+radius,x-radius,y,fill="#277da1",outline="white",tags=("ops_workstation",))
            self.ops_canvas.create_text(x,y-11,text=endpoint,fill="#1d5d78",font=("TkDefaultFont",8,"bold"),tags=("ops_workstation",))
        self.ops_canvas.tag_raise("ops_zone_label_badge");self.ops_canvas.tag_raise("ops_zone_label")
        self.apply_canvas_viewport(self.ops_canvas)

    def show_ops_rack_inventory(self,rack_id):
        if not hasattr(self,"ops_inventory_tree"):return
        self.ops_inventory_rows={}
        self.ops_inventory_tree.delete(*self.ops_inventory_tree.get_children())
        if not rack_id:return
        rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
        rows.sort(key=lambda row:(int(row.get("storage_level") or 0),int(row.get("storage_slot") or 0),str(row.get("sku",""))))
        rack_quantities=self.rack_sku_quantity_totals(rows)
        for row in rows:
            item=self.ops_inventory_tree.insert("","end",values=(row.get("sku",""),rack_quantities.get((str(rack_id),str(row.get("sku",""))),""),row.get("velocity_class",""),self.sku_storage_flags(row),row.get("static_address",""),self.occupied_dynamic_address(row),row.get("handling_unit_id","")))
            self.ops_inventory_rows[item]=row

    def ops_inventory_select(self,_event=None):
        selected=self.ops_inventory_tree.selection()
        if not selected:return
        row=self.ops_inventory_rows.get(selected[0])
        if row and self.ops_swap_mode.get()=="SKU slot":self.select_ops_sku_for_swap(row)

    def select_ops_sku_for_swap(self,row):
        sku=str(row.get("sku",""))
        if not sku:return
        if len(self.ops_sku_selection)>=2:self.ops_sku_selection=[]
        if sku in self.ops_sku_selection:return
        self.ops_sku_selection.append(sku)
        self.ops_source_sku.set(self.ops_sku_selection[0])
        self.ops_target_sku.set(self.ops_sku_selection[1] if len(self.ops_sku_selection)>1 else "")
        if len(self.ops_sku_selection)==1:self.ops_status.set(f"Selected source SKU {sku} · select the target SKU.")
        else:self.ops_status.set(f"Selected SKU swap: {self.ops_sku_selection[0]} ↔ {self.ops_sku_selection[1]} · click Execute mock swap.")

    def show_ops_assignment(self,row):
        self.ops_search_racks=set();self.ops_search_skus=set()
        self.ops_highlight_rack=row.get("rack_id","")
        self.show_ops_rack_inventory(self.ops_highlight_rack)
        local = self.ops_payload.get("location_attributes", {}) if self.ops_payload else {}
        effective, _sources = self.attributes.effective_attributes(
            row.get("storage_location_address") or row.get("static_address", ""), local
        )
        self.ops_details.set(
            f"SKU: {row.get('sku','')} · ABC class {row.get('velocity_class','')} · quantity {row.get('total_quantity_ea','')} EA\n"
            f"Storage flags: {self.sku_storage_flags(row)}\n"
            f"Physical class: {row.get('physical_storage_class','NOT_EVALUATED')} · "
            f"data {row.get('physical_data_status','NOT_EVALUATED')} "
            f"{row.get('physical_missing_data_type', '')}\n"
            f"Current static address: {row.get('static_address','')}\n"
            f"Occupied dynamic address: {self.occupied_dynamic_address(row)}\n"
            f"Handling unit: {row.get('handling_unit_id','')} ({row.get('handling_unit_type','')})\nRMF grid position: {row.get('rmf_grid_address','')}\n"
            f"SKU requirements: {self.attributes.format_values(row.get('sku_requirements'))}\n"
            f"Effective location attributes: {self.attributes.format_values(effective)}\n"
            f"Compatibility: {row.get('compatibility_status','NOT_EVALUATED')}\n"
            f"Warnings / mismatch: {'; '.join(row.get('compatibility_issues', [])) or 'none'}"
        );self.draw_ops_layout()

    def search_ops_sku(self):
        if not self.ops_rows:
            messagebox.showinfo("Load layout","Load a slotting layout first.");return
        try:matches=self.inventory.find_skus(self.ops_rows,self.ops_search.get())
        except ValueError as exc:messagebox.showerror("SKU search",str(exc));return
        matches=[row for row in matches if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")]
        if not matches:
            messagebox.showerror("SKU search",f"SKU has no assigned rack: {self.ops_search.get().strip()}");return
        self.ops_search_racks={str(row.get("rack_id")) for row in matches}
        self.ops_search_skus={str(row.get("sku","")).strip().lower() for row in matches}
        self.ops_highlight_rack=None
        self.show_ops_rack_inventory(None)
        sku_names=sorted({str(row.get("sku","")) for row in matches})
        sku_label=", ".join(sku_names)
        self.ops_details.set(
            f"SKU: {sku_label}\n"
            f"Found {len(matches):,} assigned load(s) across {len(self.ops_search_racks):,} rack(s).\n"
            "All matching racks are highlighted. Click one to display its full rack inventory and matching SKU details."
        )
        self.ops_status.set(f"Located {sku_label} across {len(self.ops_search_racks):,} rack(s) · select a highlighted rack")
        self.draw_ops_layout()

    def ops_rack_click(self,event):
        item=self.ops_canvas.find_withtag("current")
        if not item:return
        tags=self.ops_canvas.gettags(item[0]);found=[tag.split(":",1)[1] for tag in tags if tag.startswith("opsrack:")]
        if not found:return
        rack_id=found[0];rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
        self.ops_highlight_rack=rack_id
        search_rows=[row for row in rows if str(row.get("sku","")).strip().lower() in self.ops_search_skus]
        self.show_ops_rack_inventory(rack_id)
        if search_rows:
            quantities=[]
            for row in search_rows:
                try:quantities.append(float(row.get("quantity_ea")))
                except (TypeError,ValueError):pass
            quantity_label=f"{sum(quantities):g} EA" if quantities else "not available"
            sku_names=sorted({str(row.get("sku","")) for row in search_rows})
            addresses=", ".join(str(row.get("static_address","")) for row in search_rows)
            self.ops_details.set(
                f"Selected rack: {rack_id}\n"
                f"SKU: {', '.join(sku_names)} · {len(search_rows):,} load(s) · quantity on this rack {quantity_label}\n"
                f"Addresses: {addresses}\n"
                "Select a row in the rack inventory table for its swap operation."
            )
            self.ops_status.set(
                f"Selected rack {rack_id} · showing all {len(rows):,} rack load(s) · "
                f"{len(search_rows):,} match the searched SKU"
            )
        elif self.ops_search_racks:
            self.ops_details.set(f"Selected rack: {rack_id}\nThis rack does not contain the searched SKU; showing all rack inventory.")
        if self.ops_swap_mode.get()=="Whole shelf":
            self.select_ops_shelf_for_swap(rack_id,rows)
        else:self.draw_ops_layout()

    def ops_swap_mode_changed(self,_event=None):
        self.ops_shelf_selection=[];self.ops_sku_selection=[];self.ops_source_sku.set("");self.ops_target_sku.set("");self.draw_ops_layout()
        if self.ops_swap_mode.get()=="Whole shelf":
            if self.ops_payload and self.ops_payload.get("handling_unit_type") != "AMR shelf":
                messagebox.showinfo("AMR shelf only","Whole-shelf swap is only available for AMR shelf layouts. Tote and pallet units are addressed at slot level.")
                self.ops_swap_mode.set("SKU slot");self.ops_source_label.set("Source SKU");self.ops_target_label.set("Target SKU");self.ops_status.set("SKU slot mode: click two SKU rows, then click Execute mock swap.");return
            self.ops_source_label.set("Source shelf");self.ops_target_label.set("Target shelf");self.ops_status.set("Whole shelf mode: click two occupied rack points, then click Execute mock swap.")
        else:
            self.ops_source_label.set("Source SKU");self.ops_target_label.set("Target SKU");self.ops_status.set("SKU slot mode: click two SKU rows, then click Execute mock swap.")

    def select_ops_shelf_for_swap(self,rack_id,rows):
        units=sorted({str(row.get("handling_unit_id","")) for row in rows if row.get("handling_unit_id")})
        if not units:
            messagebox.showinfo("Empty rack","This rack has no shelf to swap.");self.draw_ops_layout();return
        if len(units)>1:
            messagebox.showerror("Shelf selection",f"Rack {rack_id} contains multiple handling units; select a SKU from the required shelf instead.");self.draw_ops_layout();return
        unit=units[0]
        if len(self.ops_shelf_selection)>=2:self.ops_shelf_selection=[]
        if any(selection["unit_id"]==unit for selection in self.ops_shelf_selection):
            messagebox.showinfo("Select another shelf","Click a different shelf for the swap.");self.draw_ops_layout();return
        self.ops_shelf_selection.append({"rack_id":rack_id,"unit_id":unit})
        self.ops_source_sku.set(self.ops_shelf_selection[0]["unit_id"])
        self.ops_target_sku.set(self.ops_shelf_selection[1]["unit_id"] if len(self.ops_shelf_selection)>1 else "")
        if len(self.ops_shelf_selection)==1:self.ops_status.set(f"Selected source shelf {unit} at {rack_id} · click the target shelf.")
        else:self.ops_status.set(f"Selected shelf swap: {self.ops_shelf_selection[0]['unit_id']} ↔ {unit} · click Execute mock swap.")
        self.draw_ops_layout()

    def perform_ops_swap(self,source,target,mode):
        timestamp=datetime.now(timezone.utc).isoformat()
        catalog=self.ops_payload.get("attribute_catalog",[]) if self.ops_payload else []
        local=self.ops_payload.get("location_attributes",{}) if self.ops_payload else {}
        try:
            if mode=="SKU slot":
                first,second=self.inventory.swap_sku_slots(self.ops_rows,source,target,catalog,local);message=f"Swapped SKU slots: {first['sku']} ↔ {second['sku']}"
                row=self.inventory.find_sku(self.ops_rows,source)
            else:
                first_unit,second_unit,first_count,second_count=self.inventory.swap_whole_shelf_units(self.ops_rows,source,target,catalog,local);message=f"Swapped shelves: {first_unit} ({first_count} SKUs) ↔ {second_unit} ({second_count} SKUs)"
                row=next(row for row in self.ops_rows if row.get("handling_unit_id")==first_unit)
        except ValueError as exc:messagebox.showerror("Swap failed",str(exc));return False
        self.ops_payload.setdefault("summary", {})["zone_storage_types"] = (
            self.slotting.derive_zone_storage_types(self.ops_rows)
        )
        event={"timestamp":timestamp,"type":mode,"source":source,"target":target,"message":message};self.ops_payload.setdefault("operation_log",[]).append(event);self.ops_log.insert("end",f"{timestamp[:19]}  {message}");self.ops_log.see("end")
        self.ops_shelf_selection=[];self.ops_sku_selection=[];self.show_ops_assignment(row);self.ops_status.set(message+" · save changes to persist the demo result")
        if self.ops_movement_analysis is not None:
            movement=self.slotting.handling_unit_visit_metrics(
                self.ops_movement_analysis,self.ops_rows,
                self.ops_payload.get("handling_unit_type","AMR shelf"),
            )
            self.set_ops_movement_metrics(movement)
        return True

    def execute_ops_swap(self):
        if not self.ops_payload:
            messagebox.showinfo("Load layout","Load a slotting layout first.");return
        source,target=self.ops_source_sku.get().strip(),self.ops_target_sku.get().strip();self.perform_ops_swap(source,target,self.ops_swap_mode.get())

    def browse_slot_input(self, variable, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes, initialdir=str(Path(variable.get()).expanduser().parent))
        if path: variable.set(path)

    def browse_slot_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".slotting.json", filetypes=[("Slotting layout", "*.slotting.json"), ("JSON", "*.json")], initialdir=str(Path(self.slot_output_path.get()).expanduser().parent), initialfile=Path(self.slot_output_path.get()).name)
        if path: self.slot_output_path.set(path)

    def prepare_slot_attribute_hierarchy(self, confirm_orphans=True):
        if not self.slot_building:
            raise ValueError("load the grid project before editing attributes")
        unassigned = [
            rack["waypoint"] for rack in self.slot_racks
            if rack["waypoint"] not in self.slot_zone_assignments
        ]
        if unassigned:
            raise ValueError(
                f"assign a zone to all racks first; {len(unassigned)} remain unassigned"
            )
        levels = int(self.slot_levels.get())
        slots = int(self.slot_slots.get())
        self.slotting.apply_zone_local_aisles(
            self.slot_building, self.slot_racks, self.slot_zone_assignments,
            self.slot_zone.get() or "Z01",
        )
        paths = self.attributes.hierarchy_paths(self.slot_racks, levels, slots)
        orphaned = sorted(set(self.slot_location_attributes) - set(paths))
        if orphaned:
            raise ValueError(
                f"grid project contains {len(orphaned)} attribute path(s) outside "
                "its saved warehouse hierarchy; correct it in Grid Map Editor"
            )
        self.slot_location_attributes = self.attributes.validate_location_attributes(
            self.slot_location_attributes, self.slot_attribute_catalog, paths
        )
        self.slot_hierarchy_paths = paths
        return paths

    def load_slotting_configuration(self, path=None):
        if path is None:
            path = filedialog.askopenfilename(
                filetypes=[("Slotting layout", "*.slotting.json"), ("JSON", "*.json")],
                initialdir=str(Path(self.slot_output_path.get()).expanduser().parent),
            )
        if not path:
            return False
        try:
            layout_path = Path(path).expanduser().resolve()
            payload = self.layouts.load(layout_path)
            building = payload["building"]
            _, racks, workstations, unreachable = self.slotting.rack_distances(building)
            stored_layout = (
                StorageLayout.from_dict(payload["storage_layout"])
                if payload.get("storage_layout") else None
            )
            self.attributes.set_machine_carrying_capacity(
                (
                    stored_layout.machine_carrying_capacity
                    if stored_layout is not None else None
                ),
                payload.get("handling_unit_type", ""),
            )
            if stored_layout is not None:
                roots = {
                    str(item["grid_waypoint"]): str(item["buffer_id"]).split("/L", 1)[0]
                    for item in stored_layout.buffers
                }
                for rack in racks:
                    if rack["waypoint"] in roots:
                        rack["static_bay_id"] = roots[rack["waypoint"]]
            capacity = payload.get("rack_capacity", {})
            levels = int(capacity.get("levels", 1))
            slots = int(capacity.get("slots_per_level", 6))
            zones = dict(payload.get("zone_assignments", {}))
            default_zone = next(iter(zones.values()), "Z01")
            self.slotting.apply_zone_local_aisles(building, racks, zones, default_zone)
            paths = self.attributes.hierarchy_paths(racks, levels, slots)
            catalog = self.attributes.normalize_catalog(payload.get("attribute_catalog"))
            local = self.attributes.validate_location_attributes(
                payload.get("location_attributes"), catalog, paths
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            messagebox.showerror("Layout configuration load failed", str(exc)); return False
        source_grid_project = payload.get("sources", {}).get("grid_project_json", "")
        source_building = payload.get("sources", {}).get("building_yaml", "")
        source_velocity = payload.get("sources", {}).get("sku_velocity_csv", "")
        source_chilled = (
            payload.get("sources", {}).get("sku_attributes_csv")
            or payload.get("sources", {}).get("chilled_requirements_csv", "")
        )
        source_affinity = payload.get("sources", {}).get(
            "affinity_order_workbook", ""
        )
        source_map = source_grid_project or source_building
        if source_map:
            self.slot_building_path.set(source_map)
        if source_velocity:
            self.slot_velocity_path.set(source_velocity)
        self.slot_chilled_path.set(source_chilled)
        if source_affinity:
            self.slot_affinity_path.set(source_affinity)
            self.slot_history_path.set(source_affinity)
        self.slot_building = building
        self.slot_grid_project = None
        if source_grid_project:
            try:
                self.slot_grid_project = self.rmf_maps.load_project(
                    Path(source_grid_project).expanduser()
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
        self.slot_racks = racks
        self.slot_loaded_path = Path(self.slot_building_path.get()).expanduser().resolve()
        self.slot_zone_assignments = zones
        self.slot_levels.set(str(levels)); self.slot_slots.set(str(slots))
        self.slot_strategy.set(payload.get("strategy", "basic"))
        affinity_configuration = payload.get("affinity_configuration") or payload.get(
            "summary", {}
        ).get("affinity_tuning", {})
        if affinity_configuration:
            self.slot_affinity_weight.set(
                f"{float(affinity_configuration.get('affinity_weight', 0.5)) * 100:g}"
            )
            self.set_slot_affinity_tuning(affinity_configuration)
        else:
            self.slot_affinity_recommendation = None
            self.slot_affinity_max_service.set("")
            self.slot_affinity_min_shared.set("")
            self.slot_affinity_min_score.set("")
            self.slot_strategy_changed()
        self.slot_handling_unit.set(payload.get("handling_unit_type", "AMR shelf"))
        self.slot_attribute_catalog = catalog
        self.slot_location_attributes = local
        self.slot_hierarchy_paths = paths
        self.slot_storage_initialized = any(
            "/" not in path
            and all(key in values for key in PHYSICAL_ATTRIBUTE_KEYS)
            for path, values in local.items()
        )
        self.slot_rows = payload.get("assignments", [])
        self.slot_movement_by_unit = {}
        self.slot_movement_summary = {}
        self.slot_movement_status.set(
            "Order history selected. Calculate movement ranks for this layout."
            if self.slot_history_path.get().strip()
            else "Import order-history Excel to calculate movement ranks."
        )
        self.slot_zone_storage_types = payload.get("summary", {}).get(
            "zone_storage_types", {}
        )
        self.slot_output_path.set(str(layout_path))
        self.slot_selected_rack = None
        self.slot_rack_zone_name.set("")
        self.slot_rack_zone_edit_status.set("Select a rack to rename its zone.")
        self.show_slotting_rows(self.slot_rows)
        self.show_unassigned_slotting_rows(self.slot_rows)
        self.draw_slotting_layout()
        self.update_slot_progress(100, "Loaded saved layout")
        self.slot_summary.set(
            f"Restored {len(racks)} racks, {len(set(zones.values()))} zones, "
            f"{len(local)} attributed nodes and {len(self.slot_rows):,} assignments · "
            f"{workstations} workstations · {unreachable} unreachable"
        )
        self.slot_rack_detail.set(
            "Previous layout configuration restored. Edit attributes or regenerate."
        )
        self.slot_zone_detail.set(
            "Previous layout configuration restored. Click a rack to inspect its zone."
        )
        self.slot_viewer_status.set(f"Viewing saved layout: {layout_path}")
        return True

    def load_interactive_slotting_layout(self):
        """Load a saved layout into the read-only interactive result viewer."""
        self.load_slotting_configuration()

    def calculate_slot_movement_ranks(self):
        if not self.slot_rows:
            messagebox.showerror(
                "Movement ranking", "Load or generate a slotting layout first."
            )
            return
        try:
            history_path = Path(
                self.slot_history_path.get().strip()
            ).expanduser().resolve()
            self.slot_movement_status.set("Loading historical store orders…")
            self.root.update_idletasks()
            dataset = self.affinity.load_orders(history_path)
            analysis = self.affinity.analyze(dataset)
            movement = self.slotting.handling_unit_visit_metrics(
                analysis, self.slot_rows, self.slot_handling_unit.get()
            )
            if not movement["units"]:
                raise ValueError(
                    "no assigned layout SKUs match the order-history workbook"
                )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.slot_movement_status.set("Movement ranking failed.")
            messagebox.showerror("Movement ranking", str(exc))
            return
        self.slot_movement_summary = movement
        self.slot_movement_by_unit = {
            row["handling_unit_id"]: row for row in movement["units"]
        }
        class_counts = {
            label: sum(
                row["movement_class"] == label for row in movement["units"]
            )
            for label in ("A", "B", "C")
        }
        self.slot_movement_status.set(
            f"{movement['fulfillment_group_count']:,} Store ID + Date tasks · "
            f"{movement['total_handling_unit_visits']:,} "
            f"{movement['handling_unit_type']} visits · ranked at "
            f"{movement['ranking_level']} level · "
            f"A {class_counts['A']} / B {class_counts['B']} / C {class_counts['C']}"
        )
        self.draw_slotting_layout()

    def load_slot_building(self):
        try:
            path=Path(self.slot_building_path.get()).expanduser().resolve()
            project=self.rmf_maps.load_project(path)
            self.attributes.set_standard_storage_defaults(
                project.warehouse_storage_defaults
            )
            if project.storage_layout is None or not project.storage_layout.buffers:
                raise ValueError(
                    "grid project has no storage buffers; assign and save buffers "
                    "in Grid Map Editor first"
                )
            building=project.to_building_dict()
            _,racks,workstations,unreachable=self.slotting.rack_distances(building)
            roots = {
                str(item["grid_waypoint"]): str(item["buffer_id"]).split("/L", 1)[0]
                for item in project.storage_layout.buffers
            }
            for rack in racks:
                rack["static_bay_id"] = roots[rack["waypoint"]]
            zones = dict(project.zone_assignments)
            missing = sorted(
                rack["waypoint"] for rack in racks
                if rack["waypoint"] not in zones
            )
            if missing:
                raise ValueError(
                    f"grid project has {len(missing)} rack(s) without a warehouse "
                    "zone; assign all zones in Grid Map Editor first"
                )
            default_zone = next(iter(zones.values()), "Z01")
            self.slotting.apply_zone_local_aisles(
                building, racks, zones, default_zone
            )
            catalog = project.attribute_catalog
            paths = self.attributes.hierarchy_paths(
                racks,
                project.storage_layout.levels_per_rack,
                project.storage_layout.slots_per_level,
            )
            local = self.attributes.validate_location_attributes(
                project.location_attributes, catalog, paths
            )
        except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
            messagebox.showerror("Grid project load failed",str(exc)); return
        self.slot_building=building; self.slot_grid_project=project; self.slot_racks=racks; self.slot_loaded_path=path
        self.slot_handling_unit.set(project.storage_layout.handling_unit_type)
        self.slot_levels.set(str(project.storage_layout.levels_per_rack))
        self.slot_slots.set(str(project.storage_layout.slots_per_level))
        self.slot_zone_assignments=zones; self.slot_rows=[]; self.slot_selected_rack=None
        self.slot_rack_zone_name.set("")
        self.slot_rack_zone_edit_status.set("Select a rack to rename its zone.")
        self.slot_movement_by_unit = {}
        self.slot_movement_summary = {}
        self.slot_movement_status.set(
            "Project loaded. Generate a layout before calculating movement ranks."
        )
        self.slot_attribute_catalog=self.attributes.normalize_catalog(catalog); self.slot_location_attributes=copy.deepcopy(local); self.slot_hierarchy_paths=paths
        if project.sku_attribute_source:
            self.slot_chilled_path.set(project.sku_attribute_source)
        self.slot_storage_initialized=True
        self.slot_zone_storage_types={}
        self.slot_zone.set(default_zone)
        self.show_slotting_rows([])
        self.show_unassigned_slotting_rows([])
        self.slot_unassigned_summary.set(
            "Project loaded. Generate slotting to review rejected SKUs."
        )
        self.update_slot_progress(0, "Ready")
        self.draw_slotting_layout()
        self.slot_summary.set(
            f"Loaded {len(racks)} racks, {len(project.storage_layout.buffers)} empty "
            f"{project.storage_layout.buffer_level} buffers and {workstations} "
            f"workstations · {len(set(zones.values()))} warehouse zones · "
            f"{unreachable} unreachable"
        )
        self.slot_zone_detail.set(
            "Warehouse zones and attributes loaded from the grid project."
        )
        self.slot_rack_detail.set("Rack details will appear after slotting is generated.")
        self.slot_viewer_status.set(
            "Project configuration loaded. Generate a layout to view results."
        )

    def slot_strategy_changed(self, _event=None):
        affinity_enabled = self.slot_strategy.get() == "abc_affinity"
        for widget in self.slot_affinity_source_widgets:
            widget.configure(state="normal" if affinity_enabled else "disabled")
        tuning_enabled = affinity_enabled and self.slot_affinity_recommendation is not None
        for widget in self.slot_affinity_tuning_widgets:
            widget.configure(state="normal" if tuning_enabled else "disabled")
        if not affinity_enabled:
            self.slot_affinity_parameter_status.set(
                "Basic keeps the current ABC-only allocation; affinity settings are not used."
            )
        elif self.slot_affinity_recommendation is None:
            self.slot_affinity_parameter_status.set(
                "Automatic values will be calculated from the selected workbook and map."
            )

    def set_slot_affinity_tuning(self, tuning):
        self.slot_affinity_recommendation = dict(tuning)
        self.slot_affinity_max_service.set(
            f"{float(tuning['maximum_service_distance_increase']) * 100:.4g}"
        )
        self.slot_affinity_min_shared.set(
            str(int(tuning["minimum_shared_store_days"]))
        )
        self.slot_affinity_min_score.set(
            f"{float(tuning['minimum_affinity_score']) * 100:.4g}"
        )
        status = str(tuning.get("parameter_status", "AUTO_SUGGESTED"))
        label = "Automatically suggested" if status == "AUTO_SUGGESTED" else "User adjusted"
        self.slot_affinity_parameter_status.set(
            f"{label} for this workbook and warehouse map. Edit values and regenerate if needed."
        )
        self.slot_strategy_changed()

    def update_slot_progress(self, value, message):
        self.slot_progress_value.set(max(0, min(100, float(value))))
        self.slot_progress_text.set(message)
        self.root.update_idletasks()

    @staticmethod
    def slot_requirement_value(requirements, key, unknown="Unknown"):
        value = (requirements or {}).get(key)
        if value is None or str(value).strip() == "":
            return unknown
        return str(value)

    def show_unassigned_slotting_rows(self, rows):
        self.slot_unassigned_tree.delete(
            *self.slot_unassigned_tree.get_children()
        )
        unassigned = [
            row for row in rows
            if row.get("assignment_status") != "ASSIGNED"
        ]
        reason_labels = {
            "UNASSIGNED_NO_CAPACITY": "All compatible slots are occupied",
            "UNASSIGNED_NO_CHILLED_LOCATION": "No chilled storage location",
            "UNASSIGNED_NO_AMBIENT_LOCATION": "No ambient storage location",
            "UNASSIGNED_NO_COMPATIBLE_LOCATION": "No compatible location attributes",
            "UNASSIGNED_NO_OVERSIZE_LOCATION": "No oversize-capable location",
        }
        for row in unassigned:
            requirements = row.get("sku_requirements") or {}
            chilled = requirements.get("chilled")
            chilled_label = (
                "Yes" if chilled is True else "No" if chilled is False else "Unknown"
            )
            size = " × ".join(
                self.slot_requirement_value(requirements, key)
                for key in (
                    "max_item_length", "max_item_width", "max_item_height"
                )
            )
            issues = row.get("compatibility_issues") or []
            if isinstance(issues, str):
                issues = [issues]
            reason = "; ".join(str(issue) for issue in issues if str(issue).strip())
            if not reason:
                reason = reason_labels.get(
                    row.get("assignment_status"),
                    str(row.get("assignment_status", "Unassigned")),
                )
            self.slot_unassigned_tree.insert(
                "", "end",
                values=(
                    row.get("sku", ""),
                    chilled_label,
                    size,
                    self.slot_requirement_value(
                        requirements, "max_item_weight"
                    ),
                    row.get("physical_data_status", "Unknown"),
                    reason,
                ),
            )
        self.slot_unassigned_summary.set(
            f"{len(unassigned):,} inventory load(s) could not be slotted."
            if unassigned else "All SKUs were slotted successfully."
        )

    def run_slotting(self, use_adjusted=False):
        self.slot_generate_button.configure(state="disabled")
        self.update_slot_progress(2, "Validating inputs…")
        try:
            strategy = self.slot_strategy.get()
            if strategy not in {"basic", "abc_affinity"}:
                raise ValueError(f"unsupported slotting strategy: {strategy}")
            if use_adjusted and strategy != "abc_affinity":
                raise ValueError("adjusted affinity values require ABC + affinity strategy")
            current_path=Path(self.slot_building_path.get()).expanduser().resolve()
            if self.slot_building is None or self.slot_loaded_path!=current_path:
                raise ValueError("load the selected grid project JSON before generating")
            if self.slot_grid_project is None or self.slot_grid_project.storage_layout is None:
                raise ValueError("loaded grid project has no storage buffers")
            self.attributes.set_standard_storage_defaults(
                self.slot_grid_project.warehouse_storage_defaults
            )
            self.prepare_slot_attribute_hierarchy()
            self.update_slot_progress(12, "Loading SKU data…")
            building=self.slot_building
            skus=self.slotting.load_velocity(
                Path(self.slot_velocity_path.get()).expanduser(),
                self.slot_attribute_catalog,
                (
                    Path(self.slot_chilled_path.get()).expanduser()
                    if self.slot_chilled_path.get().strip()
                    else None
                ),
            )
            if self.stock_rows:
                skus = self.slotting.apply_stock_requirements(
                    skus, self.stock_rows
                )
            self.update_slot_progress(25, f"Loaded {len(skus):,} SKUs")
            levels=int(self.slot_levels.get()); slots=int(self.slot_slots.get())
            source_affinity = ""
            if strategy == "abc_affinity":
                affinity_weight = float(self.slot_affinity_weight.get()) / 100.0
                if not 0.0 <= affinity_weight <= 1.0:
                    raise ValueError("affinity weight must be between 0% and 100%")
                affinity_path = Path(
                    self.slot_affinity_path.get()
                ).expanduser().resolve()
                source_affinity = str(affinity_path)

                def report_affinity_progress(current, total, message):
                    fraction = current / max(1, total)
                    self.update_slot_progress(
                        25 + 30 * fraction, message
                    )

                affinity_dataset = self.affinity.load_orders(
                    affinity_path, progress=report_affinity_progress
                )
                affinity_analysis = self.affinity.analyze(affinity_dataset)
                tuning_parameters = None
                if use_adjusted:
                    tuning_parameters = {
                        "maximum_service_distance_increase": (
                            float(self.slot_affinity_max_service.get()) / 100.0
                        ),
                        "minimum_shared_store_days": int(
                            self.slot_affinity_min_shared.get()
                        ),
                        "minimum_affinity_score": (
                            float(self.slot_affinity_min_score.get()) / 100.0
                        ),
                    }
                self.slot_summary.set(
                    "Evaluating ABC-preserving affinity layouts and map-derived parameters…"
                )
                self.update_slot_progress(60, "Generating affinity layout…")
                rows, summary = self.slotting.generate_abc_affinity(
                    building,
                    skus,
                    affinity_analysis,
                    affinity_weight,
                    levels,
                    slots,
                    self.slot_handling_unit.get(),
                    self.slot_zone.get(),
                    self.slot_zone_assignments,
                    self.slot_attribute_catalog,
                    self.slot_location_attributes,
                    tuning_parameters,
                    storage_layout=self.slot_grid_project.storage_layout,
                )
            else:
                self.update_slot_progress(45, "Generating basic ABC layout…")
                rows, summary = self.slotting.generate_basic(
                    building, skus, levels, slots, self.slot_handling_unit.get(),
                    self.slot_zone.get(), self.slot_zone_assignments,
                    self.slot_attribute_catalog, self.slot_location_attributes,
                    storage_layout=self.slot_grid_project.storage_layout,
                )
            self.slot_zone_assignments = dict(
                summary.get("zone_assignments", self.slot_zone_assignments)
            )
            for rack in self.slot_racks:
                rack["zone_id"] = self.slot_zone_assignments.get(
                    rack.get("waypoint"), rack.get("zone_id", "")
                )
            self.update_slot_progress(85, "Saving slotting layout…")
            self.layouts.save(
                rows, building, summary, Path(self.slot_output_path.get()).expanduser(),
                strategy=self.slot_strategy.get(),
                handling_unit_type=self.slot_handling_unit.get(),
                levels_per_rack=levels, slots_per_level=slots,
                zone_assignments=self.slot_zone_assignments,
                attribute_catalog=self.slot_attribute_catalog,
                location_attributes=self.slot_location_attributes,
                source_grid_project=str(current_path),
                source_velocity=str(Path(self.slot_velocity_path.get()).expanduser().resolve()),
                source_chilled=(
                    str(Path(self.slot_chilled_path.get()).expanduser().resolve())
                    if self.slot_chilled_path.get().strip()
                    else ""
                ),
                source_affinity=source_affinity,
                affinity_configuration=summary.get("affinity_tuning", {}),
                storage_layout=self.slot_grid_project.storage_layout,
            )
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            self.slot_generate_button.configure(state="normal")
            self.update_slot_progress(0, "Generation failed")
            messagebox.showerror("Slotting generation failed", str(exc)); return
        self.slot_rows = rows
        self.slot_movement_by_unit = {}
        self.slot_movement_summary = {}
        if source_affinity:
            self.slot_history_path.set(source_affinity)
        self.slot_movement_status.set(
            "Layout generated. Calculate movement ranks from the selected history."
            if self.slot_history_path.get().strip()
            else "Import order-history Excel to calculate movement ranks."
        )
        self.slot_zone_storage_types = summary["zone_storage_types"]
        self.slotting.apply_zone_local_aisles(building,self.slot_racks,self.slot_zone_assignments,self.slot_zone.get())
        self.slot_selected_rack = None
        self.slot_rack_zone_name.set("")
        self.slot_rack_zone_edit_status.set("Select a rack to rename its zone.")
        self.show_slotting_rows(rows)
        self.show_unassigned_slotting_rows(rows)
        self.draw_slotting_layout()
        self.slot_viewer_status.set(
            f"Viewing generated layout: {self.slot_output_path.get()}"
        )
        self.slot_zone_detail.set("Click a coloured rack point on the map to inspect its zone.")
        self.slot_rack_detail.set("Click a coloured rack point on the map to inspect its assignments.")
        affinity_summary = ""
        if strategy == "abc_affinity":
            tuning = summary["affinity_tuning"]
            self.set_slot_affinity_tuning(tuning)
            comparison = summary["baseline_comparison"]
            affinity_summary = (
                f"affinity parameters: service +{tuning['maximum_service_distance_increase'] * 100:.2f}%, "
                f"shared store-days ≥{tuning['minimum_shared_store_days']}, "
                f"score ≥{tuning['minimum_affinity_score'] * 100:.2f}% "
                f"({tuning['parameter_status'].lower().replace('_', ' ')})"
                f" · affinity distance change "
                f"{comparison['weighted_pair_distance_change_fraction'] * 100:+.1f}%"
                f" · service distance change "
                f"{comparison['weighted_service_distance_change_fraction'] * 100:+.1f}%"
                f" · related-pair same bay "
                f"{summary['affinity_metrics']['affinity_pair_same_bay_fraction'] * 100:.1f}%"
                f" · mixed ABC bays "
                f"{summary['affinity_metrics']['mixed_abc_rack_count']}"
            )
            rack_touches = summary.get("order_rack_touch_metrics", {})
            baseline_touches = comparison.get(
                "basic_order_rack_touch_metrics", {}
            )
            if rack_touches and baseline_touches:
                affinity_summary += (
                    f" · racks/store-day "
                    f"{baseline_touches['average_racks_per_group']:.2f}→"
                    f"{rack_touches['average_racks_per_group']:.2f}"
                    f" · total rack touches "
                    f"{baseline_touches['total_rack_touches']:,}→"
                    f"{rack_touches['total_rack_touches']:,} "
                    f"({comparison['total_rack_touch_change_fraction'] * 100:+.2f}%)"
                )
        quantity_summary = (
            f"Assigned {summary['assigned_load_count']:,}/"
            f"{summary['inventory_load_count']:,} inventory loads from "
            f"{summary['sku_count']:,} SKUs · fully assigned SKUs "
            f"{summary['fully_assigned_sku_count']:,} · partially assigned "
            f"{summary['partially_assigned_sku_count']:,} · unassigned loads "
            f"{summary['unassigned_load_count']:,}"
            if summary.get("quantity_enabled_sku_count") else
            f"Assigned {summary['assigned_count']:,}/{summary['sku_count']:,} SKUs · "
            f"unassigned {summary['unassigned_count']:,}"
        )
        self.slot_summary.set(
            quantity_summary + " · "
            f"temperature-zone shortage "
            f"{summary['unassigned_status_counts'].get('UNASSIGNED_NO_CHILLED_LOCATION', 0) + summary['unassigned_status_counts'].get('UNASSIGNED_NO_AMBIENT_LOCATION', 0):,} · "
            f"all slots occupied {summary['unassigned_no_capacity_count']:,} · "
            f"map-defined oversize zones {summary['auto_planned_oversize_segment_count']:,} · "
            f"occupied racks {summary.get('final_occupied_rack_count', 0):,}/"
            f"{summary['rack_count']:,} ({summary['unreachable_rack_count']} unreachable) · "
            f"{summary['workstation_count']} workstations · {summary['zone_count']} zones · capacity {summary['capacity']:,} · "
            f"buffers occupied {summary['occupied_buffer_count']:,}/{summary['buffer_count']:,} "
            f"({summary['buffer_occupancy_rate'] * 100:.1f}%) · "
            f"unverified physical data {summary['unverified_oversize_count']:,} "
            f"({summary['assigned_unverified_count']:,} assigned with warning) · "
            f"auto slot overrides {summary['auto_overridden_slot_count']:,} · "
            f"map zone types "
            + ", ".join(
                f"{zone}={storage_type}"
                for zone, storage_type in summary["zone_storage_types"].items()
            )
            + (f" · {affinity_summary}" if affinity_summary else "")
            + " · "
            f"saved to {self.slot_output_path.get()}"
        )
        self.slot_generate_button.configure(state="normal")
        self.update_slot_progress(100, "Complete")

    @staticmethod
    def rack_sku_quantity_totals(rows):
        """Return stored EA per logical SKU and rack for quantity-aware views."""
        totals = {}
        for row in rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            raw = row.get("quantity_ea")
            if raw in (None, ""):
                continue
            try:
                quantity = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(quantity):
                continue
            key = (str(row.get("rack_id", "")), str(row.get("sku", "")))
            totals[key] = totals.get(key, 0.0) + quantity
        return {key: f"{value:g}" for key, value in totals.items()}

    def show_slotting_rows(self, rows):
        self.slot_tree.delete(*self.slot_tree.get_children())
        rack_quantities = self.rack_sku_quantity_totals(rows)
        for row in rows[:1000]:
            self.slot_tree.insert("", "end", values=(
                row.get("abc_frequency_rank", row.get("sku_rank", "")),
                row.get("affinity_placement_rank", ""),
                row["sku"],
                rack_quantities.get(
                    (str(row.get("rack_id", "")), str(row.get("sku", ""))),
                    "",
                ),
                row["velocity_class"], self.sku_storage_flags(row),
                row["static_address"], self.occupied_dynamic_address(row),
                row["handling_unit_type"], row["handling_unit_id"],
                row["assignment_status"],
            ))

    def slotting_geometry(self, vertices, canvas=None):
        canvas = canvas or self.slot_canvas
        xs=[float(v[0]) for v in vertices]; ys=[float(v[1]) for v in vertices]
        min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
        width=max(300,canvas.winfo_width()); height=max(300,canvas.winfo_height()); padding=55
        scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y))
        return min_x,max_x,min_y,max_y,width,height,padding,scale

    def slotting_screen_point(self, x, y, geometry):
        min_x,max_x,min_y,max_y,width,height,padding,scale=geometry
        screen_x=padding+(float(x)-min_x)*scale
        if self.slot_building.get("coordinate_system")=="reference_image": screen_y=padding+(float(y)-min_y)*scale
        else: screen_y=height-padding-(float(y)-min_y)*scale
        return screen_x,screen_y

    def draw_slotting_layout(self):
        if hasattr(self, "slot_canvas"):
            self._draw_slotting_canvas(self.slot_canvas)

    def _draw_slotting_canvas(self, canvas):
        canvas.delete("all")
        if not self.slot_building:
            return
        _,level=next(iter(self.slot_building["levels"].items())); vertices=level.get("vertices",[])
        if not vertices:
            return
        geometry=self.slotting_geometry(vertices, canvas)
        for lane in level.get("lanes",[]):
            if len(lane)<2 or lane[0]>=len(vertices) or lane[1]>=len(vertices): continue
            a,b=vertices[lane[0]],vertices[lane[1]]; x1,y1=self.slotting_screen_point(a[0],a[1],geometry); x2,y2=self.slotting_screen_point(b[0],b[1],geometry)
            canvas.create_line(x1,y1,x2,y2,fill="#d9e0e3",width=1)
        zone_palette = (
            "#6c8cd5", "#31a6a0", "#d47b4c", "#8ca63c",
            "#c75d8b", "#81756e", "#3d8fbe", "#9b70c7",
        )
        assigned_zone_by_rack = {
            str(row.get("rack_id", "")): str(
                row.get("generated_attribute_zone_id")
                or row.get("planned_zone_id")
                or row.get("zone_id", "")
            )
            for row in self.slot_rows
            if row.get("assignment_status") == "ASSIGNED"
        }
        displayed_zone_by_rack = {
            str(rack.get("rack_id", "")): assigned_zone_by_rack.get(
                str(rack.get("rack_id", "")), str(rack.get("zone_id", ""))
            )
            for rack in self.slot_racks
        }
        zone_ids = sorted({
            zone.strip() for zone in displayed_zone_by_rack.values()
            if zone.strip()
        })
        zone_colors = {
            zone: zone_palette[index % len(zone_palette)]
            for index, zone in enumerate(zone_ids)
        }
        rack_zone_at = {
            (float(rack["x"]), float(rack["y"])): displayed_zone_by_rack.get(
                str(rack.get("rack_id", "")), ""
            )
            for rack in self.slot_racks
        }
        x_values = sorted({point[0] for point in rack_zone_at})
        y_values = sorted({point[1] for point in rack_zone_at})

        def cell_limits(value, values):
            index = values.index(value)
            previous = values[index - 1] if index else None
            following = values[index + 1] if index + 1 < len(values) else None
            fallback = min(
                (right - left for left, right in zip(values, values[1:])),
                default=1.0,
            ) / 2
            lower = (previous + value) / 2 if previous is not None else value - fallback
            upper = (value + following) / 2 if following is not None else value + fallback
            return lower, upper, previous, following

        # Draw the perimeter of each rack-cell union instead of one bounding
        # rectangle. L-shaped or sparse zones therefore cannot cover a rack
        # belonging to another zone.
        for (x, y), zone in rack_zone_at.items():
            if not zone:
                continue
            left, right, previous_x, following_x = cell_limits(x, x_values)
            bottom, top, previous_y, following_y = cell_limits(y, y_values)
            sides = (
                (previous_x is None or rack_zone_at.get((previous_x, y)) != zone,
                 (left, bottom), (left, top)),
                (following_x is None or rack_zone_at.get((following_x, y)) != zone,
                 (right, bottom), (right, top)),
                (previous_y is None or rack_zone_at.get((x, previous_y)) != zone,
                 (left, bottom), (right, bottom)),
                (following_y is None or rack_zone_at.get((x, following_y)) != zone,
                 (left, top), (right, top)),
            )
            for visible, start, end in sides:
                if not visible:
                    continue
                x1, y1 = self.slotting_screen_point(*start, geometry)
                x2, y2 = self.slotting_screen_point(*end, geometry)
                rack_x, rack_y = self.slotting_screen_point(x, y, geometry)
                midpoint_x = (x1 + x2) / 2
                midpoint_y = (y1 + y2) / 2
                inward_x = rack_x - midpoint_x
                inward_y = rack_y - midpoint_y
                inward_length = math.hypot(inward_x, inward_y) or 1.0
                boundary_gap = 3.0
                offset_x = boundary_gap * inward_x / inward_length
                offset_y = boundary_gap * inward_y / inward_length
                canvas.create_line(
                    x1 + offset_x, y1 + offset_y,
                    x2 + offset_x, y2 + offset_y,
                    fill=zone_colors[zone],
                    width=3,
                    tags=("slot_zone_boundary",),
                )
        assignments={}
        for row in self.slot_rows:
            if row["assignment_status"]=="ASSIGNED": assignments.setdefault(row["rack_id"],[]).append(row)
        class_colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
        for rack in self.slot_racks:
            rack_id = rack["rack_id"]
            x, y = self.slotting_screen_point(rack["x"], rack["y"], geometry)
            rack_rows = assignments.get(rack_id, [])
            selected = rack_id == self.slot_selected_rack
            if self.slot_handling_unit.get() == "AMR shelf":
                unit_id = (
                    str(rack_rows[0].get("handling_unit_id", ""))
                    if rack_rows else ""
                )
                movement = self.slot_movement_by_unit.get(unit_id, {})
                fill = class_colors.get(
                    movement.get("movement_class", ""), "#7b8b92"
                )
                radius = 7 if selected else 4
                canvas.create_oval(
                    x-radius, y-radius, x+radius, y+radius,
                    fill=fill,
                    outline="#087f8c" if selected else "white",
                    width=3 if selected else 1,
                    tags=("rack", f"rack:{rack_id}", f"unit:{unit_id}"),
                )
                continue

            # ASRS retrieves individual totes/pallets, so display movement rank
            # at storage-slot level instead of assigning one colour to the rack.
            locations = {}
            for row in rack_rows:
                occupied = row.get("occupied_handling_units") or [{
                    "handling_unit_id": row.get("handling_unit_id", ""),
                    "storage_level": row.get("storage_level", 1),
                    "storage_slot": row.get("storage_slot", 1),
                }]
                for location in occupied:
                    unit_id = str(location.get("handling_unit_id", "")).strip()
                    if unit_id:
                        locations.setdefault(unit_id, location)
            outline_radius = 8 if selected else 6
            canvas.create_oval(
                x-outline_radius, y-outline_radius,
                x+outline_radius, y+outline_radius,
                fill="", outline="#087f8c" if selected else "#aab4b8",
                width=3 if selected else 1,
                tags=("rack", f"rack:{rack_id}"),
            )
            level_count = max(1, int(self.slot_levels.get()))
            slot_count = max(1, int(self.slot_slots.get()))
            dx = min(6.0, 30.0 / max(1, slot_count - 1))
            dy = min(6.0, 30.0 / max(1, level_count - 1))
            for unit_id, location in locations.items():
                slot = int(location.get("storage_slot") or 1)
                level_number = int(location.get("storage_level") or 1)
                unit_x = x + (slot - (slot_count + 1) / 2) * dx
                unit_y = y + (level_number - (level_count + 1) / 2) * dy
                movement = self.slot_movement_by_unit.get(unit_id, {})
                fill = class_colors.get(
                    movement.get("movement_class", ""), "#7b8b92"
                )
                canvas.create_oval(
                    unit_x-3, unit_y-3, unit_x+3, unit_y+3,
                    fill=fill, outline="white", width=1,
                    tags=("rack", f"rack:{rack_id}", f"unit:{unit_id}"),
                )
        for index,vertex in enumerate(vertices):
            params=vertex[4] if len(vertex)>4 and isinstance(vertex[4],dict) else {}
            if "dropoff_ingestor" not in params: continue
            endpoint=str(self.slotting.typed_value(params["dropoff_ingestor"],vertex[3])); x,y=self.slotting_screen_point(vertex[0],vertex[1],geometry); r=6
            canvas.create_polygon(x,y-r,x+r,y,x,y+r,x-r,y,fill="#277da1",outline="white")
            canvas.create_text(x,y-11,text=endpoint,fill="#1d5d78",font=("TkDefaultFont",8,"bold"))
        self.apply_canvas_viewport(canvas)

    @staticmethod
    def _renamed_zone_value(value, old_zone, new_zone):
        if not isinstance(value, str):
            return value
        if value == old_zone:
            return new_zone
        for delimiter in ("/", "__", "_"):
            prefix = old_zone + delimiter
            if value.startswith(prefix):
                return new_zone + value[len(old_zone):]
        return value

    @classmethod
    def _rename_zone_in_layout_payload(cls, payload, old_zone, new_zone):
        rename = lambda value: cls._renamed_zone_value(
            value, old_zone, new_zone
        )
        payload["zone_assignments"] = {
            waypoint: new_zone if zone == old_zone else zone
            for waypoint, zone in payload.get("zone_assignments", {}).items()
        }
        payload["location_attributes"] = {
            rename(path): values
            for path, values in payload.get("location_attributes", {}).items()
        }
        scalar_fields = (
            "zone_id", "planned_zone_id",
            "generated_attribute_zone_id", "static_address",
            "storage_location_address",
        )
        list_fields = (
            "occupied_static_addresses",
            "occupied_storage_location_addresses",
        )
        for row in payload.get("assignments", []):
            for field in scalar_fields:
                row[field] = rename(row.get(field, ""))
            for field in list_fields:
                row[field] = [rename(value) for value in row.get(field, [])]
            for unit in row.get("occupied_handling_units", []):
                for field in ("static_address", "storage_location_address"):
                    if field in unit:
                        unit[field] = rename(unit[field])
        summary = payload.setdefault("summary", {})
        summary["zone_storage_types"] = {
            rename(zone): storage_type
            for zone, storage_type
            in summary.get("zone_storage_types", {}).items()
        }
        generated = {}
        for zone, definition in summary.get(
            "generated_attribute_zones", {}
        ).items():
            for step in definition.get("hierarchy_path", []):
                step["zone_id"] = rename(step.get("zone_id", ""))
            generated[rename(zone)] = definition
        summary["generated_attribute_zones"] = generated
        return payload

    def rename_selected_slot_zone(self):
        rack = next((
            item for item in self.slot_racks
            if item.get("rack_id") == self.slot_selected_rack
        ), None)
        if rack is None:
            messagebox.showerror("Rename zone", "Select a rack first.")
            return False
        old_zone = str(rack.get("zone_id", "")).strip()
        new_zone = self.slot_rack_zone_name.get().strip()
        if not new_zone:
            messagebox.showerror("Rename zone", "Zone name cannot be blank.")
            return False
        if "/" in new_zone:
            messagebox.showerror(
                "Rename zone", "Zone name cannot contain '/'."
            )
            return False
        if new_zone == old_zone:
            self.slot_rack_zone_edit_status.set("Zone name is unchanged.")
            return True
        existing = set(self.slot_zone_assignments.values())
        if new_zone in existing:
            messagebox.showerror(
                "Rename zone",
                f"Zone '{new_zone}' already exists; choose a unique name.",
            )
            return False
        path = Path(self.slot_output_path.get()).expanduser().resolve()
        try:
            payload = self.layouts.load(path)
            self._rename_zone_in_layout_payload(
                payload, old_zone, new_zone
            )
            self.layouts.save_payload(payload, path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror("Rename zone", str(exc))
            return False
        self.slot_zone_assignments = dict(payload["zone_assignments"])
        self.slot_rows = payload["assignments"]
        self.slot_location_attributes = payload.get("location_attributes", {})
        self.slot_zone_storage_types = payload.get("summary", {}).get(
            "zone_storage_types", {}
        )
        for item in self.slot_racks:
            item["zone_id"] = self.slot_zone_assignments.get(
                item.get("waypoint"), item.get("zone_id", "")
            )
        if self.slot_zone.get() == old_zone:
            self.slot_zone.set(new_zone)
        self.slot_rack_zone_name.set(new_zone)
        self.slot_viewer_status.set(f"Saved zone rename to: {path}")
        self.draw_slotting_layout()
        self.slot_rack_click_by_id(self.slot_selected_rack)
        self.slot_rack_zone_edit_status.set(
            f"Renamed {old_zone} to {new_zone} and saved the layout."
        )
        return True

    def slot_rack_click_by_id(self, rack_id):
        self._show_slot_rack_details(rack_id)

    def rack_attribute_detail_text(self, zone_path, bay_path):
        zone_effective, _zone_sources = self.attributes.effective_attributes(
            zone_path, self.slot_location_attributes
        )
        bay_effective, bay_sources = self.attributes.effective_attributes(
            bay_path, self.slot_location_attributes
        )
        rack_overrides = {
            key: value for key, value in bay_effective.items()
            if bay_sources.get(key) != zone_path
        }
        return (
            f"Zone: {zone_path}\n"
            f"Attributes: {self.attributes.format_values(zone_effective)}",
            f"Overrides: {self.attributes.format_values(rack_overrides)}",
        )

    def planned_zone_detail_text(self, zone_path, rows):
        assigned_rows = [row for row in rows if row.get("assignment_status") == "ASSIGNED"]
        planned_zone = (
            assigned_rows[0].get("planned_zone_id", "") if assigned_rows else ""
        ) or zone_path
        generated_zone = (
            assigned_rows[0].get("generated_attribute_zone_id", "")
            if assigned_rows else ""
        ) or planned_zone
        display_zone = (
            generated_zone
            if generated_zone != zone_path
            else zone_path
        )
        planned_type = (
            assigned_rows[0].get("planned_storage_type", "") if assigned_rows else ""
        ) or self.slot_zone_storage_types.get(planned_zone, "UNUSED")
        attributes_by_key = {}
        for row in assigned_rows:
            for key, value in (row.get("effective_location_attributes") or {}).items():
                attributes_by_key.setdefault(key, value)
        if not attributes_by_key:
            attributes_by_key = self.attributes.effective_attributes(
                zone_path, self.slot_location_attributes
            )[0]
        return (
            f"Zone: {display_zone}\n"
            f"Generated storage type: {planned_type or 'UNUSED'}\n"
            f"Attributes: {self.attributes.format_values(attributes_by_key)}"
        )

    def slot_rack_click(self, event):
        item=self.slot_canvas.find_withtag("current")
        if not item: return
        tags=self.slot_canvas.gettags(item[0]); rack_tags=[tag for tag in tags if tag.startswith("rack:")]
        if not rack_tags: return
        rack_id=rack_tags[0].split(":",1)[1]
        self._show_slot_rack_details(rack_id)

    def _show_slot_rack_details(self, rack_id):
        self.slot_selected_rack=rack_id
        rack=next((item for item in self.slot_racks if item["rack_id"]==rack_id),None)
        rows=[row for row in self.slot_rows if row["rack_id"]==rack_id and row["assignment_status"]=="ASSIGNED"]
        self.show_slotting_rows(rows); self.draw_slotting_layout()
        if not rack: return
        classes={label:sum(row["velocity_class"]==label for row in rows) for label in ("A","B","C")}
        distinct_skus={str(row.get("sku","")) for row in rows if str(row.get("sku","")).strip()}
        rack_quantities=self.rack_sku_quantity_totals(rows)
        rack_total_quantity=sum(float(value) for value in rack_quantities.values())
        unit_ids=sorted({row["handling_unit_id"] for row in rows})
        zone_path = rack.get("zone_id", "UNASSIGNED")
        self.slot_rack_zone_name.set(zone_path)
        self.slot_rack_zone_edit_status.set(
            "Renaming here changes the entire zone."
        )
        bay_path = f"{rack.get('zone_id','UNASSIGNED')}/{rack['aisle_id']}/{rack['static_bay_id']}"
        _zone_detail, rack_override_detail = self.rack_attribute_detail_text(
            zone_path, bay_path
        )
        chilled_count, exception_count = self.rack_storage_flag_counts(rows)
        rack_class = rows[0].get("rack_velocity_class", "") if rows else ""
        rack_frequency_rank = rows[0].get("rack_frequency_rank", "") if rows else ""
        rack_pick_frequency = rows[0].get("rack_pick_frequency", "") if rows else ""
        movement_rows = [
            self.slot_movement_by_unit[unit_id]
            for unit_id in unit_ids
            if unit_id in self.slot_movement_by_unit
        ]
        if self.slot_handling_unit.get() != "AMR shelf":
            all_unit_ids = {
                str(unit.get("handling_unit_id", ""))
                for row in rows
                for unit in (
                    row.get("occupied_handling_units") or [{
                        "handling_unit_id": row.get("handling_unit_id", "")
                    }]
                )
            }
            unit_ids = sorted(unit_id for unit_id in all_unit_ids if unit_id)
            movement_rows = [
                self.slot_movement_by_unit[unit_id]
                for unit_id in unit_ids
                if unit_id in self.slot_movement_by_unit
            ]
        movement_classes = {
            label: sum(row.get("movement_class") == label for row in movement_rows)
            for label in ("A", "B", "C")
        }
        if self.slot_handling_unit.get() == "AMR shelf":
            movement_detail = (
                f"Shelf movement: class {movement_rows[0]['movement_class']} · "
                f"rank {movement_rows[0]['visit_rank']} · "
                f"{movement_rows[0]['visit_count']:,} Store ID + Date visits · "
                f"visit rate {movement_rows[0]['visit_rate'] * 100:.1f}%\n"
                if movement_rows else
                "Shelf movement: unranked; import history and calculate ranks\n"
            )
        else:
            movement_detail = (
                f"Slot movement: {len(movement_rows)}/{len(unit_ids)} ranked · "
                f"A {movement_classes['A']} / B {movement_classes['B']} / "
                f"C {movement_classes['C']} · "
                f"{sum(row['visit_count'] for row in movement_rows):,} total visits\n"
                if movement_rows else
                "Slot movement: unranked; import history and calculate ranks\n"
            )
        self.slot_zone_detail.set(self.planned_zone_detail_text(zone_path, rows))
        self.slot_rack_detail.set(
            f"Static grid rack: {rack_id}\nPickup dispenser: {rack['pickup_dispenser_id']} · vertex {rack['vertex_index']}\n"
            f"Current static buffer address: {rows[0]['static_address'] if rows else rack.get('zone_id','UNASSIGNED')+'/'+rack['aisle_id']+'/'+rack['static_bay_id']}\n"
            f"{movement_detail}"
            f"Rack pick-frequency ABC: {rack_class or 'unassigned'} · frequency rank {rack_frequency_rank or 'n/a'} · picks {rack_pick_frequency or 0}\n"
            f"Assigned SKUs: {len(distinct_skus)} · inventory loads {len(rows)} · stored quantity {rack_total_quantity:g} EA · A {classes['A']} / B {classes['B']} / C {classes['C']}\n"
            f"Storage flags: CHILLED={chilled_count} · OVERSIZE / WEIGHT EXCEPTION={exception_count}\n"
            f"Physical classes: "
            + (", ".join(
                f"{key}={sum(row.get('physical_storage_class') == key for row in rows)}"
                for key in ("STANDARD", "OVERSIZE", "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT", "UNVERIFIED_OVERSIZE")
                if any(row.get('physical_storage_class') == key for row in rows)
            ) or "not evaluated") + "\n"
            f"Handling unit(s): {', '.join(unit_ids) if unit_ids else 'none'}\n"
            f"{rack_override_detail}"
        )

    def spec_from_inputs(self) -> GridSpec:
        spec = GridSpec(
            float(self.width.get()), float(self.length.get()),
            float(self.spacing.get()), self.map_name.get().strip(),
            self.level_name.get().strip(), float(self.spacing_y.get()),
        )
        spec.validate()
        return spec

    @staticmethod
    def format_sku_attribute_summary(summary: dict | None) -> str:
        if not summary:
            return "Load a SKU attributes CSV to discover required zone attributes."
        details = []
        for key, item in (summary.get("attributes") or {}).items():
            values = item.get("values") or []
            if item.get("value_type") == "number":
                value_text = (
                    f"{item.get('minimum'):g}–{item.get('maximum'):g}"
                    if item.get("minimum") is not None
                    and item.get("maximum") is not None else "numeric"
                )
            else:
                value_text = "/".join(values) if values else "configured value"
            details.append(f"{key}: {value_text}")
        selected = summary.get("combination_attributes") or []
        selected_text = ", ".join(selected) if selected else "none"
        return (
            f"{int(summary.get('sku_count', 0)):,} SKUs loaded\n"
            f"Overlay grouping: {selected_text}\n"
            "Attributes discovered:\n" + " · ".join(details)
        )

    def load_grid_sku_attributes_dialog(self):
        path = filedialog.askopenfilename(
            initialdir=str(DEFAULT_SKU_ATTRIBUTES_INPUT.parent),
            initialfile=DEFAULT_SKU_ATTRIBUTES_INPUT.name,
            filetypes=[("SKU attributes CSV", "*.csv"), ("All files", "*")],
        )
        if not path:
            return
        self.load_grid_sku_attributes(Path(path))

    def load_grid_sku_attributes(self, path: Path) -> bool:
        self.attributes.set_standard_storage_defaults(
            self.project.warehouse_storage_defaults
        )
        try:
            catalog, discovered_summary = self.slotting.inspect_sku_attribute_csv(
                path, self.project.attribute_catalog
            )
            available = list(
                discovered_summary.get("available_combination_attributes") or []
            )
            selected = (
                [
                    key for key in self.project.sku_overlay_attributes
                    if key in available
                ]
                if self.project.sku_attribute_source else available
            )
            catalog, summary = self.slotting.inspect_sku_attribute_csv(
                path, catalog, selected
            )
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("SKU attributes", str(exc))
            return False
        self.push_undo()
        self.project.attribute_catalog = self.attributes.serialize_catalog(catalog)
        self.project.sku_attribute_source = str(path.resolve())
        self.project.sku_attribute_summary = copy.deepcopy(summary)
        self.project.sku_overlay_attributes = list(
            summary.get("combination_attributes") or []
        )
        self.grid_sku_attributes_path.set(self.project.sku_attribute_source)
        self.grid_sku_attribute_summary.set(
            self.format_sku_attribute_summary(summary)
        )
        self.sync_grid_sku_overlay_attribute_list()
        active_physical = set(PHYSICAL_ATTRIBUTE_KEYS) & set(catalog)
        for zone in set(self.project.zone_assignments.values()):
            values = self.project.location_attributes.setdefault(zone, {})
            for key in active_physical:
                values.setdefault(key, self.project.warehouse_storage_defaults[key])
        self.status.set(
            f"Loaded attributes for {summary['sku_count']:,} SKUs. "
            "Configure the listed values in warehouse zone settings."
        )
        if hasattr(self, "stock_status"):
            self.stock_status.set(
                f"Loaded attributes for {summary['sku_count']:,} SKUs. "
                "Click Calculate to include rack requirements."
            )
        self.redraw()
        return True

    def apply_grid_sku_overlay_attributes(self) -> bool:
        source = Path(self.project.sku_attribute_source)
        if not self.project.sku_attribute_source or not source.is_file():
            messagebox.showerror(
                "SKU attributes",
                "Load a SKU attributes CSV before selecting overlay attributes.",
            )
            return False
        selected = [
            str(self.grid_sku_overlay_attribute_list.get(index))
            for index in self.grid_sku_overlay_attribute_list.curselection()
        ]
        try:
            catalog, summary = self.slotting.inspect_sku_attribute_csv(
                source, self.project.attribute_catalog, selected
            )
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("SKU attributes", str(exc))
            return False
        self.push_undo()
        self.project.attribute_catalog = self.attributes.serialize_catalog(catalog)
        self.project.sku_overlay_attributes = list(
            summary.get("combination_attributes") or []
        )
        self.project.sku_attribute_summary = copy.deepcopy(summary)
        self.grid_sku_attribute_summary.set(
            self.format_sku_attribute_summary(summary)
        )
        self.redraw()
        selected_text = ", ".join(
            self.project.sku_overlay_attributes
        ) or "no Boolean attributes"
        self.status.set(
            f"Rack-demand overlay now groups SKUs by {selected_text}."
        )
        if getattr(self, "stock_rows", None):
            self.refresh_stock_rack_grouping()
        return True

    def refresh_stock_rack_grouping(self):
        """Regroup existing stock results without rescanning order history."""
        try:
            source = Path(self.project.sku_attribute_source)
            requirements = self.slotting.load_sku_attribute_requirements(
                source,
                {str(row["sku"]) for row in self.stock_rows},
                self.project.attribute_catalog,
                include_derived_grouping=True,
            )
            rows, combinations = calculate_rack_requirements(
                self.stock_rows,
                requirements,
                (
                    float(self.stock_slot_length.get()),
                    float(self.stock_slot_width.get()),
                    float(self.stock_slot_height.get()),
                ),
                int(self.stock_rack_levels.get()),
                int(self.stock_slots_per_level.get()),
                self.project.sku_overlay_attributes,
                slot_max_weight=float(self.stock_slot_weight.get()),
                rack_max_weight=(
                    float(self.stock_machine_capacity_values[
                        "max_item_weight"
                    ].get())
                    if self.stock_storage_system.get() == "AMR" else None
                ),
            )
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Stock rack grouping", str(exc))
            return False
        self.stock_rows = rows
        self.stock_combination_rows = combinations
        self.stock_combination_tree.delete(
            *self.stock_combination_tree.get_children()
        )
        for row in combinations:
            self.stock_combination_tree.insert("", "end", values=(
                row["attribute_combination"],
                row["sku_count"],
                row["total_required_ea"],
                row["required_slots"],
                f'{row["required_racks"]}{"+" if row["unresolved_skus"] else ""}',
                row["unresolved_skus"],
            ))
        summary = copy.deepcopy(self.project.sku_attribute_summary)
        summary["attribute_combinations"] = copy.deepcopy(combinations)
        self.project.sku_attribute_summary = summary
        self.grid_sku_attribute_summary.set(
            self.format_sku_attribute_summary(summary)
        )
        self.redraw()
        self.stock_status.set("Rack results regrouped without rescanning order history.")
        return True

    def generate_grid(self):
        try:
            spec = self.spec_from_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid grid", str(exc)); return
        if self.project.markers and not messagebox.askyesno("Reset grid", "Generating a new grid removes all rack and workstation markers. Continue?"):
            return
        self.push_undo()
        warehouse_defaults = dict(self.project.warehouse_storage_defaults)
        machine_capacity = dict(self.project.machine_carrying_capacity)
        attribute_catalog = copy.deepcopy(self.project.attribute_catalog)
        sku_attribute_source = self.project.sku_attribute_source
        sku_attribute_summary = copy.deepcopy(self.project.sku_attribute_summary)
        sku_overlay_attributes = list(self.project.sku_overlay_attributes)
        self.project = GridProject(
            spec,
            warehouse_storage_defaults=warehouse_defaults,
            machine_carrying_capacity=machine_capacity,
        )
        self.project.attribute_catalog = attribute_catalog
        self.project.sku_attribute_source = sku_attribute_source
        self.project.sku_attribute_summary = sku_attribute_summary
        self.project.sku_overlay_attributes = sku_overlay_attributes
        self.attributes.set_standard_storage_defaults(warehouse_defaults)
        self.sync_grid_storage_controls()
        self.sync_grid_sku_attribute_controls()
        self.selected = None
        self.bulk_anchor = None
        self.bulk_drag_position = None
        self.update_selected_editor()
        self.redraw()
        self.status.set("Grid generated. All neighbouring points are connected bidirectionally.")

    def sync_grid_storage_controls(self):
        layout = self.project.storage_layout
        if layout is None:
            self.grid_buffer_summary.set("Storage buffers have not been assigned.")
            return
        self.grid_storage_system.set(layout.system_type)
        self.grid_storage_levels.set(str(layout.levels_per_rack))
        self.grid_storage_slots.set(str(layout.slots_per_level))
        if hasattr(self, "stock_rack_levels"):
            self.stock_rack_levels.set(str(layout.levels_per_rack))
            self.stock_slots_per_level.set(str(layout.slots_per_level))
            self.stock_storage_system.set(layout.system_type)
            self.update_stock_machine_capacity_ui()
        self.grid_buffer_summary.set(
            f"{len(layout.buffers)} empty {layout.buffer_level} buffer(s) · "
            f"dynamic unit {layout.handling_unit_type}"
        )
        self.update_machine_capacity_ui()

    def update_machine_capacity_ui(self):
        """Show the carrying dimensions used by the selected machine type."""
        if not hasattr(self, "grid_machine_capacity_entries"):
            return
        is_amr = self.grid_storage_system.get() == "AMR"
        self.grid_machine_capacity_label.set(
            "AMR whole-rack max weight (kg)"
            if is_amr else "ASRS max L/W/H (m) / kg"
        )
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            state = "disabled" if is_amr and key != "max_item_weight" else "normal"
            self.grid_machine_capacity_entries[key].configure(state=state)

    def grid_storage_system_changed(self, _event=None):
        defaults = DEFAULT_MACHINE_CAPACITY_BY_SYSTEM[
            self.grid_storage_system.get()
        ]
        for key, variable in self.grid_machine_capacity_values.items():
            value = defaults[key]
            variable.set("" if value is None else str(value))
        self.update_machine_capacity_ui()

    @staticmethod
    def parse_machine_capacity(values, system_type):
        """Parse optional carrying limits; AMR ignores volumetric limits."""
        result = {}
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            text = values[key].get().strip()
            if system_type == "AMR" and key != "max_item_weight":
                result[key] = None
                continue
            if not text:
                result[key] = None
                continue
            value = float(text)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Machine carrying limits must be greater than zero.")
            result[key] = value
        return result

    def sync_grid_warehouse_storage_controls(self):
        self.attributes.set_standard_storage_defaults(
            self.project.warehouse_storage_defaults
        )
        for key, variable in self.grid_warehouse_capacity_values.items():
            variable.set(str(self.project.warehouse_storage_defaults[key]))
        if hasattr(self, "stock_slot_weight"):
            slot_weight = self.project.warehouse_storage_defaults[
                "max_item_weight"
            ]
            layout = self.project.storage_layout
            if (
                layout is not None
                and layout.system_type == "AMR"
                and self.project.machine_carrying_capacity["max_item_weight"]
            ):
                slot_weight = (
                    self.project.machine_carrying_capacity["max_item_weight"]
                    / (layout.levels_per_rack * layout.slots_per_level)
                )
            self.stock_slot_weight.set(f"{slot_weight:g}")
        for key, variable in self.grid_machine_capacity_values.items():
            value = self.project.machine_carrying_capacity[key]
            variable.set("" if value is None else str(value))
        if hasattr(self, "stock_machine_capacity_values"):
            for key, variable in self.stock_machine_capacity_values.items():
                value = self.project.machine_carrying_capacity[key]
                variable.set("" if value is None else str(value))
            if self.project.storage_layout is not None:
                self.stock_storage_system.set(
                    self.project.storage_layout.system_type
                )
            self.update_stock_machine_capacity_ui()
        self.update_machine_capacity_ui()

    def sync_grid_sku_attribute_controls(self):
        self.grid_sku_attributes_path.set(self.project.sku_attribute_source)
        self.grid_sku_attribute_summary.set(
            self.format_sku_attribute_summary(
                self.project.sku_attribute_summary
            )
        )
        self.sync_grid_sku_overlay_attribute_list()

    def sync_grid_sku_overlay_attribute_list(self):
        if not hasattr(self, "grid_sku_overlay_attribute_list"):
            return
        widget = self.grid_sku_overlay_attribute_list
        widget.delete(0, "end")
        summary = self.project.sku_attribute_summary or {}
        available = list(
            summary.get("available_combination_attributes") or []
        )
        summary_attributes = summary.get("attributes") or {}
        if (
            all(key in summary_attributes for key in PHYSICAL_ATTRIBUTE_KEYS)
            and DERIVED_OVERSIZE_KEY not in available
        ):
            # Older saved projects predate the derived selector. Expose it
            # immediately; applying the selection refreshes the full summary.
            available.append(DERIVED_OVERSIZE_KEY)
        selected = set(self.project.sku_overlay_attributes)
        for index, key in enumerate(available):
            widget.insert("end", key)
            if key in selected:
                widget.selection_set(index)

    def invalidate_grid_buffers(self):
        if self.project.storage_layout is not None:
            self.project.storage_layout = None
            self.grid_buffer_summary.set(
                "Rack markers changed; assign storage buffers again."
            )

    def assign_grid_buffers(self):
        try:
            levels = int(self.grid_storage_levels.get())
            slots = int(self.grid_storage_slots.get())
            if not any(marker.role == "rack" for marker in self.project.markers.values()):
                raise ValueError("place at least one rack pickup before assigning buffers")
            machine_capacity = self.parse_machine_capacity(
                self.grid_machine_capacity_values,
                self.grid_storage_system.get(),
            )
            self.push_undo()
            self.project.machine_carrying_capacity = machine_capacity
            self.project.assign_storage_buffers(
                self.grid_storage_system.get(), levels, slots
            )
            self.project.validate()
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Storage buffers", str(exc)); return
        self.sync_grid_storage_controls()
        self.redraw()
        self.status.set(
            f"Assigned {len(self.project.storage_layout.buffers)} empty "
            f"{self.project.storage_layout.buffer_level} storage buffer(s)."
        )

    def calculate_grid_geometry(self):
        width = max(200, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        padding = 45
        coordinates = [
            self.project.coordinates(column, row)
            for column, row in self.project.iter_positions()
        ]
        # Always include the predefined warehouse extent, while allowing an
        # edited point to expand the visible area beyond it in any direction.
        coordinates.extend([
            (0.0, 0.0),
            (self.project.grid.width_m, self.project.grid.length_m),
        ])
        min_x = min(point[0] for point in coordinates)
        max_x = max(point[0] for point in coordinates)
        min_y = min(point[1] for point in coordinates)
        max_y = max(point[1] for point in coordinates)
        span_x = max(max_x - min_x, 1e-9)
        span_y = max(max_y - min_y, 1e-9)
        scale = min((width - 2 * padding) / span_x, (height - 2 * padding) / span_y)
        return padding, scale, height, min_x, min_y

    def geometry(self):
        geometry = getattr(self, "_grid_geometry", None)
        if geometry is None:
            geometry = self.calculate_grid_geometry()
            self._grid_geometry = geometry
        return geometry

    def physical_screen_point(self, x, y):
        padding, scale, height, min_x, min_y = self.geometry()
        return (
            padding + (x - min_x) * scale,
            height - padding - (y - min_y) * scale,
        )

    def screen_point(self, column, row):
        return self.physical_screen_point(
            *self.project.coordinates(column, row)
        )

    def redraw(self):
        if not hasattr(self, "canvas"): return
        self._grid_geometry = self.calculate_grid_geometry()
        self.canvas.delete("all")
        spec = self.project.grid
        boundary_start = self.physical_screen_point(0, 0)
        boundary_end = self.physical_screen_point(spec.width_m, spec.length_m)
        self.canvas.create_rectangle(
            boundary_start[0], boundary_end[1], boundary_end[0], boundary_start[1],
            outline="#aab7bc", dash=(5, 4), width=2,
            tags=("warehouse_boundary",),
        )
        for start, end in self.project.iter_lane_positions():
            x1, y1 = self.screen_point(*start)
            x2, y2 = self.screen_point(*end)
            self.canvas.create_line(
                x1, y1, x2, y2, fill="#d7dfe2", tags=("grid_lane",)
            )
        radius = max(
            2,
            min(5, self.geometry()[1] * min(spec.spacing_m, spec.spacing_y_m) * 0.10),
        )
        buffer_positions = {
            (int(item["column"]), int(item["row"]))
            for item in (
                self.project.storage_layout.buffers
                if self.project.storage_layout else []
            )
        }
        zone_palette = (
            "#6c8cd5", "#31a6a0", "#d47b4c", "#8ca63c",
            "#c75d8b", "#81756e", "#3d8fbe", "#9b70c7",
        )
        zone_ids = sorted(set(self.project.zone_assignments.values()))
        zone_colors = {
            zone: zone_palette[index % len(zone_palette)]
            for index, zone in enumerate(zone_ids)
        }
        zone_positions = {zone: [] for zone in zone_ids}
        for (column, row), marker in self.project.markers.items():
            if marker.role != "rack":
                continue
            zone = self.project.zone_assignments.get(
                self.project.vertex_name(column, row), ""
            )
            if zone:
                zone_positions.setdefault(zone, []).append((column, row))
        boundary_padding = max(
            10,
            min(spec.spacing_m, spec.spacing_y_m) * self.geometry()[1] * 0.25,
        )
        for zone, positions in zone_positions.items():
            if not positions:
                continue
            points = [self.screen_point(column, row) for column, row in positions]
            left = min(x for x, _y in points) - boundary_padding
            top = min(y for _x, y in points) - boundary_padding
            right = max(x for x, _y in points) + boundary_padding
            bottom = max(y for _x, y in points) + boundary_padding
            badge_width = max(42, len(zone) * 8 + 14)
            badge_height = 22
            self.canvas.create_rectangle(
                left, top, right, bottom,
                outline=zone_colors[zone],
                width=3,
                tags=("grid_zone_boundary",),
            )
            badge_left = left
            badge_top = top - badge_height - 4
            self.canvas.create_rectangle(
                badge_left,
                badge_top,
                badge_left + badge_width,
                badge_top + badge_height,
                fill=zone_colors[zone],
                outline=zone_colors[zone],
                tags=("grid_zone_label_badge",),
            )
            self.canvas.create_text(
                badge_left + badge_width / 2,
                badge_top + badge_height / 2,
                text=zone,
                anchor="center",
                fill="white",
                font=("TkDefaultFont", 9, "bold"),
                tags=("grid_zone_label",),
            )
        for column, row in self.project.iter_positions():
            x, y = self.screen_point(column, row)
            marker = self.project.markers.get((column, row))
            waypoint = self.project.vertex_name(column, row)
            zone = self.project.zone_assignments.get(waypoint, "")
            if marker and marker.role == "rack":
                if not zone:
                    fill = "#f3a712"
                else:
                    effective, _sources = self.attributes.effective_attributes(
                        zone, self.project.location_attributes
                    )
                    fill = "#277da1" if effective.get("chilled") is True else "#d1495b"
            elif marker and marker.role == "workstation":
                fill = "#6a4c93"
            else:
                fill = "#50656e"
            r = radius + 3 if self.selected == (column, row) else radius
            outline = (
                "#087f8c" if self.selected == (column, row)
                else "#7b2cbf" if (column, row) in buffer_positions
                else fill
            )
            tags = ("grid_point", "rack_point") if marker and marker.role == "rack" else ("grid_point",)
            self.canvas.create_oval(
                x-r, y-r, x+r, y+r, fill=fill, outline=outline,
                width=3 if self.selected == (column, row) else 1,
                tags=tags,
            )
            if marker and marker.role == "workstation":
                self.canvas.create_text(
                    x, y-12, text=marker.endpoint_id, fill=fill,
                    font=("TkDefaultFont", 8, "bold"),
                )
        self.canvas.tag_raise("grid_zone_label_badge")
        self.canvas.tag_raise("grid_zone_label")
        if self.bulk_anchor is not None:
            x, y = self.screen_point(*self.bulk_anchor)
            if (
                self.tool.get() in {"rack_rectangle", "zone_rectangle"}
                and self.bulk_drag_position is not None
            ):
                drag_x, drag_y = self.screen_point(*self.bulk_drag_position)
                self.canvas.create_rectangle(
                    x, y, drag_x, drag_y,
                    outline="#7b2cbf", width=3, dash=(6, 3),
                )
            else:
                self.canvas.create_oval(
                    x-9, y-9, x+9, y+9, outline="#7b2cbf", width=3
                )
        x0, y0 = self.physical_screen_point(0, 0)
        self.canvas.create_text(x0, y0+20, text="(0, 0)", anchor="n", fill="#087f8c", font=("TkDefaultFont", 9, "bold"))
        self.apply_canvas_viewport(self.canvas)
        self.draw_grid_demand_overlay()
        self.summary.set(
            f"{spec.columns} columns × {spec.rows} rows\n"
            f"{self.project.vertex_count:,} vertices · "
            f"{self.project.edge_count:,} edges"
        )
        self.update_grid_zone_summary()

    def draw_grid_demand_overlay(self):
        """Draw fixed, schema-driven SKU rack demand in the map viewport."""
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("grid_demand_overlay")
        summary = self.project.sku_attribute_summary or {}
        combinations = summary.get("attribute_combinations") or []
        if not combinations:
            return
        layout = self.project.storage_layout
        try:
            levels = int(
                self.stock_rack_levels.get()
                if hasattr(self, "stock_rack_levels")
                else layout.levels_per_rack
                if layout else self.grid_storage_levels.get()
            )
            slots = int(
                self.stock_slots_per_level.get()
                if hasattr(self, "stock_slots_per_level")
                else layout.slots_per_level
                if layout else self.grid_storage_slots.get()
            )
        except (TypeError, ValueError):
            levels, slots = 1, 1
        rack_capacity = max(1, levels * slots)
        display_rows = []
        total_racks = 0
        for combination in combinations:
            sku_count = int(combination.get("sku_count", 0))
            if "required_racks" in combination:
                racks = combination.get("required_racks")
            else:
                racks = math.ceil(sku_count / rack_capacity)
            if racks is not None:
                total_racks += int(racks)
            attributes = combination.get("attributes") or {}
            profile = " · ".join(
                f"{key}={'T' if value is True else 'F' if value is False else '?'}"
                for key, value in attributes.items()
            ) or "no Boolean flags"
            total_ea = combination.get("total_required_ea")
            demand_text = (
                f" · {int(total_ea):,} EA"
                if total_ea is not None else ""
            )
            unresolved = int(combination.get("unresolved_skus", 0))
            rack_text = (
                f"{int(racks):,}{'+' if unresolved else ''} racks"
                if racks is not None else "unresolved"
            )
            display_rows.append((
                f"{combination.get('storage_type', 'STANDARD')} · {profile} · "
                f"{sku_count:,} SKUs{demand_text} → {rack_text}",
                (
                    self.grid_demand_combination_is_covered(combination, int(racks))
                    if racks is not None and not unresolved else False
                ),
            ))
        x = float(self.canvas.canvasx(max(12, self.canvas.winfo_width() - 12)))
        y = float(self.canvas.canvasy(12))
        cursor_y = y + 9
        text_ids = []

        def add_line(text, fill="#243238", tags=(), bold=False):
            nonlocal cursor_y
            text_id = self.canvas.create_text(
                x - 10, cursor_y,
                text=text,
                anchor="ne",
                width=430,
                justify="left",
                fill=fill,
                font=("TkDefaultFont", 9, "bold" if bold else "normal"),
                tags=("grid_demand_overlay", *tags),
            )
            text_ids.append(text_id)
            bounds = self.canvas.bbox(text_id)
            cursor_y = (bounds[3] + 2) if bounds else cursor_y + 18

        add_line(
            f"Calculated rack demand · {rack_capacity} storage slots/rack",
            bold=True,
        )
        for text, covered in display_rows:
            add_line(
                text,
                fill="#173f73" if covered else "#243238",
                tags=("grid_demand_covered",) if covered else (),
            )
        add_line(f"Total calculated: {total_racks:,} racks", bold=True)
        bounds = self.canvas.bbox("grid_demand_overlay")
        if bounds:
            rectangle_id = self.canvas.create_rectangle(
                bounds[0] - 9, bounds[1] - 7,
                x, bounds[3] + 7,
                fill="#f8fbfc",
                outline="#50656e",
                width=1,
                tags=("grid_demand_overlay",),
            )
            self.canvas.tag_lower(rectangle_id, text_ids[0])
        for text_id in text_ids:
            self.canvas.tag_raise(text_id)

    def grid_demand_combination_is_covered(
        self, combination: dict, required_racks: int
    ) -> bool:
        """Return whether configured zones provide enough exact-profile racks."""
        if required_racks <= 0:
            return True
        rack_counts: dict[str, int] = {}
        for (column, row), marker in self.project.markers.items():
            if marker.role != "rack":
                continue
            zone = self.project.zone_assignments.get(
                self.project.vertex_name(column, row), ""
            )
            if zone:
                rack_counts[zone] = rack_counts.get(zone, 0) + 1
        required_attributes = combination.get("attributes") or {}
        required_storage_type = str(
            combination.get("storage_type", "STANDARD")
        ).upper()
        matching_racks = 0
        for zone, rack_count in rack_counts.items():
            effective, _sources = self.attributes.effective_attributes(
                zone, self.project.location_attributes
            )
            if any(
                effective.get(key, False) is not value
                for key, value in required_attributes.items()
            ):
                continue
            oversize_planned = effective.get("oversize_capable") is True
            zone_storage_type = "OVERSIZE" if oversize_planned else "STANDARD"
            if zone_storage_type == required_storage_type:
                matching_racks += rack_count
        return matching_racks >= required_racks

    def nearest_position(self, event) -> GridPosition | None:
        event_x, event_y = self.canvas_viewport_inverse_point(
            self.canvas, event.x, event.y
        )
        positions = list(self.project.iter_positions())
        if not positions:
            return None
        position = min(
            positions,
            key=lambda item: math.hypot(
                event_x - self.screen_point(*item)[0],
                event_y - self.screen_point(*item)[1],
            ),
        )
        x, y = self.screen_point(*position)
        scale = self.geometry()[1]
        if math.hypot(event_x-x, event_y-y) <= max(
            12,
            min(self.project.grid.spacing_m, self.project.grid.spacing_y_m)
            * scale * .35,
        ):
            return position
        return None

    @staticmethod
    def point_segment_distance(px, py, x1, y1, x2, y2):
        delta_x, delta_y = x2 - x1, y2 - y1
        length_squared = delta_x * delta_x + delta_y * delta_y
        if length_squared == 0:
            return math.hypot(px - x1, py - y1)
        ratio = max(0.0, min(
            1.0,
            ((px - x1) * delta_x + (py - y1) * delta_y) / length_squared,
        ))
        return math.hypot(
            px - (x1 + ratio * delta_x),
            py - (y1 + ratio * delta_y),
        )

    def nearest_lane(self, event):
        event_x, event_y = self.canvas_viewport_inverse_point(
            self.canvas, event.x, event.y
        )
        lanes = list(self.project.iter_lane_positions())
        if not lanes:
            return None
        lane, distance = min(
            (
                (
                    lane,
                    self.point_segment_distance(
                        event_x, event_y,
                        *self.screen_point(*lane[0]),
                        *self.screen_point(*lane[1]),
                    ),
                )
                for lane in lanes
            ),
            key=lambda item: item[1],
        )
        return lane if distance <= 9 else None

    def canvas_click(self, event):
        current = self.canvas.find_withtag("current")
        if current and "grid_demand_overlay" in self.canvas.gettags(current[0]):
            self.grid_overlay_pointer_down = True
            return "break"
        self.grid_overlay_pointer_down = False
        self.canvas.focus_set()
        action = self.tool.get()
        if action == "delete_lane":
            lane = self.nearest_lane(event)
            if lane is None:
                return
            self.push_undo()
            self.drag_undo_started = True
            self.delete_grid_lane(lane)
            self.redraw()
            return
        position = self.nearest_position(event)
        if position is None: return
        if action == "clear":
            self.push_undo(); self.drag_undo_started = True
            self.invalidate_grid_buffers()
            self.project.markers.pop(position, None)
            self.prune_grid_warehouse_configuration()
        elif action == "rack_rectangle":
            self.bulk_anchor = position
            self.bulk_drag_position = position
            self.status.set(
                "Drag a rectangle over the required rack points, then release."
            )
        elif action == "zone_rectangle":
            zone = self.grid_zone_id.get().strip()
            if not zone:
                messagebox.showerror("Zone ID", "Enter a zone ID first.")
                return
            self.bulk_anchor = position
            self.bulk_drag_position = position
            self.status.set(
                f"Drag the rectangle over racks for zone {zone}, then release."
            )
        elif action == "workstation":
            self.push_undo()
            self.invalidate_grid_buffers()
            self.project.markers[position] = Marker("workstation", f"WS_{position[0]}_{position[1]}")
            self.prune_grid_warehouse_configuration()
        elif action == "delete_grid":
            self.push_undo()
            self.drag_undo_started = True
            self.delete_grid_position(position)
        self.selected = None if action == "delete_grid" else position
        self.update_selected_editor()
        self.redraw()

    def canvas_drag(self, event):
        if getattr(self, "grid_overlay_pointer_down", False):
            return "break"
        action = self.tool.get()
        if action == "delete_lane":
            lane = self.nearest_lane(event)
            if lane is None:
                return
            if not self.drag_undo_started:
                self.push_undo(); self.drag_undo_started = True
            self.delete_grid_lane(lane)
            self.redraw()
            return
        if action not in {"clear", "delete_grid", "rack_rectangle", "zone_rectangle"}: return
        position = self.nearest_position(event)
        if position is None: return
        if action in {"rack_rectangle", "zone_rectangle"}:
            if self.bulk_anchor is None:
                return
            self.bulk_drag_position = position
            self.selected = position
            self.update_selected_editor()
            self.redraw()
            return
        if action == "delete_grid":
            if not self.drag_undo_started:
                self.push_undo(); self.drag_undo_started = True
            self.delete_grid_position(position)
            self.selected = None
            self.update_selected_editor()
            self.redraw()
            return
        if position == self.selected: return
        if not self.drag_undo_started:
            self.push_undo(); self.drag_undo_started = True
        self.invalidate_grid_buffers()
        self.project.markers.pop(position, None)
        self.prune_grid_warehouse_configuration()
        self.selected = position
        self.update_selected_editor()
        self.redraw()

    def canvas_release(self, event):
        if getattr(self, "grid_overlay_pointer_down", False):
            self.grid_overlay_pointer_down = False
            return "break"
        action = self.tool.get()
        if action in {"rack_rectangle", "zone_rectangle"} and self.bulk_anchor is not None:
            position = self.nearest_position(event) or self.bulk_drag_position
            if position is not None:
                if action == "rack_rectangle":
                    self.fill_grid_rack_rectangle(self.bulk_anchor, position)
                else:
                    self.assign_grid_zone_rectangle(self.bulk_anchor, position)
            self.bulk_anchor = None
            self.bulk_drag_position = None
            self.redraw()
        self.drag_undo_started = False

    def delete_grid_position(self, position: GridPosition) -> bool:
        """Delete one vertex; lane generation bridges to the next survivors."""
        if position in self.project.deleted_positions:
            return False
        self.invalidate_grid_buffers()
        self.project.markers.pop(position, None)
        self.project.coordinate_overrides.pop(position, None)
        self.project.deleted_lanes = {
            lane for lane in self.project.deleted_lanes if position not in lane
        }
        self.project.deleted_positions.add(position)
        self.prune_grid_warehouse_configuration()
        self.status.set(
            f"Deleted {self.project.vertex_name(*position)}; lanes reconnected "
            "to the next available grid points."
        )
        return True

    def delete_grid_lane(self, lane) -> bool:
        lane = self.project.normalized_lane(*lane)
        if lane in self.project.deleted_lanes:
            return False
        self.project.deleted_lanes.add(lane)
        start, end = lane
        self.status.set(
            f"Deleted lane {self.project.vertex_name(*start)} ↔ "
            f"{self.project.vertex_name(*end)}."
        )
        return True

    def delete_selected_grid_point(self, _event=None):
        if self.selected is None:
            self.status.set("Select a grid point before deleting it.")
            return "break"
        self.push_undo()
        position = self.selected
        self.delete_grid_position(position)
        self.selected = None
        self.update_selected_editor()
        self.redraw()
        return "break"

    def fill_grid_rack_rectangle(self, start, end):
        """Fill every grid point inside a completed drag rectangle with racks."""
        self.push_undo()
        c1, r1 = start
        c2, r2 = end
        count = 0
        for row in range(min(r1, r2), max(r1, r2) + 1):
            for column in range(min(c1, c2), max(c1, c2) + 1):
                if (column, row) in self.project.deleted_positions:
                    continue
                self.place_rack((column, row), redraw=False)
                count += 1
        self.status.set(f"Placed {count} rack pickup point(s).")

    def assign_grid_zone_rectangle(self, start, end):
        """Assign every rack inside a completed drag rectangle to one zone."""
        zone = self.grid_zone_id.get().strip()
        c1, r1 = start
        c2, r2 = end
        selected = [
            self.project.vertex_name(column, row)
            for (column, row), marker in self.project.markers.items()
            if marker.role == "rack"
            and min(c1, c2) <= column <= max(c1, c2)
            and min(r1, r2) <= row <= max(r1, r2)
        ]
        if selected:
            self.push_undo()
            self.project.zone_assignments.update(
                {waypoint: zone for waypoint in selected}
            )
            self.ensure_grid_zone_defaults()
            if self.grid_zone_auto.get():
                self.grid_zone_id.set(self.slotting.next_zone_id(zone))
        self.update_grid_zone_summary()
        self.status.set(
            f"Assigned {len(selected)} rack(s) to {zone}. "
            f"Next zone: {self.grid_zone_id.get()}."
        )

    def place_rack(self, position: GridPosition, redraw=True):
        prefix = self.rack_prefix.get().strip() or "RACK"
        self.invalidate_grid_buffers()
        self.project.markers[position] = Marker("rack", f"{prefix}_{position[0]}_{position[1]}")
        if redraw: self.redraw()

    def prune_grid_warehouse_configuration(self):
        """Remove hierarchy state made invalid by rack-topology edits."""
        rack_waypoints = {
            self.project.vertex_name(column, row)
            for (column, row), marker in self.project.markers.items()
            if marker.role == "rack"
        }
        self.project.zone_assignments = {
            waypoint: zone
            for waypoint, zone in self.project.zone_assignments.items()
            if waypoint in rack_waypoints
        }
        self.project.location_attributes = {
            path: values
            for path, values in self.project.location_attributes.items()
            if "/" not in path
        }
        self.update_grid_zone_summary()

    def update_grid_zone_summary(self):
        rack_count = sum(
            marker.role == "rack" for marker in self.project.markers.values()
        )
        counts = {}
        for zone in self.project.zone_assignments.values():
            counts[zone] = counts.get(zone, 0) + 1
        assigned = sum(counts.values())
        detail = " · ".join(
            f"{zone}: {count}" for zone, count in sorted(counts.items())
        )
        self.grid_zone_summary.set(
            f"{assigned}/{rack_count} racks assigned"
            + (f" · {detail}" if detail else "")
        )

    def ensure_grid_zone_defaults(self):
        catalog = self.attributes.normalize_catalog(self.project.attribute_catalog)
        for zone in sorted(set(self.project.zone_assignments.values())):
            values = self.project.location_attributes.setdefault(zone, {})
            for key in PHYSICAL_ATTRIBUTE_KEYS:
                if key in catalog:
                    values.setdefault(
                        key, self.project.warehouse_storage_defaults[key]
                    )

    def apply_grid_warehouse_storage_defaults(self):
        try:
            values = {
                key: float(variable.get().strip())
                for key, variable in self.grid_warehouse_capacity_values.items()
            }
            self.attributes.set_standard_storage_defaults(values)
        except ValueError as exc:
            messagebox.showerror("Warehouse storage defaults", str(exc))
            return False
        normalized = dict(self.attributes.standard_storage_defaults)
        self.push_undo()
        self.project.warehouse_storage_defaults = normalized
        catalog = self.attributes.normalize_catalog(self.project.attribute_catalog)
        for zone in set(self.project.zone_assignments.values()):
            zone_values = self.project.location_attributes.setdefault(zone, {})
            for key in PHYSICAL_ATTRIBUTE_KEYS:
                if key in catalog:
                    zone_values[key] = normalized[key]
        source = Path(self.project.sku_attribute_source)
        if self.project.sku_attribute_source and source.is_file():
            try:
                _catalog, summary = self.slotting.inspect_sku_attribute_csv(
                    source, catalog, self.project.sku_overlay_attributes
                )
                self.project.sku_attribute_summary = copy.deepcopy(summary)
                self.grid_sku_attribute_summary.set(
                    self.format_sku_attribute_summary(summary)
                )
            except (OSError, ValueError, TypeError):
                self.project.sku_attribute_summary.pop(
                    "attribute_combinations", None
                )
        else:
            self.project.sku_attribute_summary.pop("attribute_combinations", None)
        self.sync_grid_warehouse_storage_controls()
        self.redraw()
        self.status.set(
            "Warehouse storage defaults saved and carried forward to all zones."
        )
        return True

    def prepare_grid_attribute_hierarchy(self, confirm_orphans=True):
        layout = self.project.storage_layout
        if layout is None or not layout.buffers:
            raise ValueError("assign storage buffers before editing warehouse attributes")
        building = self.project.to_building_dict()
        _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
            building
        )
        racks = [
            rack for rack in racks
            if rack["waypoint"] in self.project.zone_assignments
        ]
        if not racks:
            raise ValueError("assign at least one rack to a zone before editing attributes")
        roots = {
            str(item["grid_waypoint"]): str(item["buffer_id"]).split("/L", 1)[0]
            for item in layout.buffers
        }
        for rack in racks:
            rack["static_bay_id"] = roots[rack["waypoint"]]
        self.slotting.apply_zone_local_aisles(
            building, racks, self.project.zone_assignments, "Z01"
        )
        self.ensure_grid_zone_defaults()
        paths = self.attributes.hierarchy_paths(
            racks, layout.levels_per_rack, layout.slots_per_level
        )
        orphaned = sorted(set(self.project.location_attributes) - set(paths))
        if orphaned:
            if confirm_orphans and not messagebox.askyesno(
                "Hierarchy changed",
                f"{len(orphaned)} saved attribute path(s) no longer exist. "
                "Discard those local values?",
            ):
                raise ValueError("attribute update cancelled")
            self.project.location_attributes = {
                path: values
                for path, values in self.project.location_attributes.items()
                if path in paths
            }
        catalog = self.attributes.normalize_catalog(self.project.attribute_catalog)
        self.project.location_attributes = self.attributes.validate_location_attributes(
            self.project.location_attributes, catalog, paths
        )
        return paths

    def open_grid_zone_storage_settings(self):
        zones = sorted({
            str(zone).strip()
            for zone in self.project.zone_assignments.values()
            if str(zone).strip()
        })
        if not zones:
            messagebox.showerror(
                "Zone storage settings",
                "Assign at least one rack to a zone before editing zone settings.",
            )
            return
        self.ensure_grid_zone_defaults()
        editor = ZoneStorageSettingsEditor(
            self.root,
            self.attributes,
            zones,
            self.project.location_attributes,
            self.apply_grid_zone_storage_settings,
            attribute_catalog=self.project.attribute_catalog,
            warehouse_storage_defaults=self.project.warehouse_storage_defaults,
        )
        editor.grab_set()

    def apply_grid_zone_storage_settings(self, location_attributes):
        self.push_undo()
        self.project.location_attributes = copy.deepcopy(location_attributes)
        self.update_grid_zone_summary()
        self.redraw()
        self.status.set("Warehouse zone storage settings saved in the grid project.")

    def open_grid_attribute_editor(self):
        try:
            paths = self.prepare_grid_attribute_hierarchy()
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Hierarchy attributes", str(exc))
            return
        editor = HierarchyAttributeEditor(
            self.root,
            self.attributes,
            self.project.attribute_catalog,
            self.project.location_attributes,
            paths,
            self.apply_grid_attributes,
        )
        editor.grab_set()

    def apply_grid_attributes(self, catalog, location_attributes):
        self.push_undo()
        self.project.attribute_catalog = self.attributes.serialize_catalog(catalog)
        self.project.location_attributes = copy.deepcopy(location_attributes)
        self.redraw()
        self.status.set(
            f"Saved {len(self.project.attribute_catalog)} warehouse attribute "
            f"definitions and {len(self.project.location_attributes)} local nodes."
        )

    def clear_grid_zones(self):
        if not self.project.zone_assignments and not self.project.location_attributes:
            return
        self.push_undo()
        self.project.zone_assignments = {}
        self.project.location_attributes = {}
        self.grid_zone_id.set("Z01")
        self.update_grid_zone_summary()
        self.redraw()
        self.status.set("Cleared rack zones and their local warehouse attributes.")

    def snapshot(self) -> dict:
        return self.project.to_project_dict()

    def restore_snapshot(self, snapshot: dict):
        self.project = GridProject.from_project_dict(snapshot)
        self.selected = None
        self.bulk_anchor = None
        self.bulk_drag_position = None
        self.map_name.set(self.project.grid.map_name)
        self.level_name.set(self.project.grid.level_name)
        self.width.set(str(self.project.grid.width_m))
        self.length.set(str(self.project.grid.length_m))
        self.spacing.set(str(self.project.grid.spacing_m))
        self.spacing_y.set(str(self.project.grid.spacing_y_m))
        self.sync_grid_storage_controls()
        self.sync_grid_warehouse_storage_controls()
        self.sync_grid_sku_attribute_controls()
        self.update_grid_zone_summary()
        self.update_selected_editor()
        self.redraw()

    def push_undo(self):
        self.undo_stack.append(self.snapshot())
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self, _event=None):
        if not self.undo_stack:
            self.status.set("Nothing to undo.")
            return "break"
        self.redo_stack.append(self.snapshot())
        self.restore_snapshot(self.undo_stack.pop())
        self.status.set(f"Undo complete · {len(self.undo_stack)} earlier action(s)")
        return "break"

    def redo(self, _event=None):
        if not self.redo_stack:
            self.status.set("Nothing to redo.")
            return "break"
        self.undo_stack.append(self.snapshot())
        self.restore_snapshot(self.redo_stack.pop())
        self.status.set(f"Redo complete · {len(self.redo_stack)} later action(s)")
        return "break"

    def update_selected_editor(self):
        if self.selected is None:
            self.selected_coordinate.set("No grid point selected")
            self.selected_x.set(""); self.selected_y.set("")
            self.role.set("none"); self.endpoint_id.set(""); return
        column, row = self.selected
        x, y = self.project.coordinates(column, row)
        self.selected_coordinate.set(
            f"Column {column}, row {row}  →  "
            f"({x:g}, {y:g}) m"
        )
        self.selected_x.set(f"{x:g}")
        self.selected_y.set(f"{y:g}")
        marker = self.project.markers.get(self.selected)
        self.role.set(marker.role if marker else "none")
        self.endpoint_id.set(marker.endpoint_id if marker else "")

    def apply_edit(self):
        if self.selected is None:
            messagebox.showinfo("Select a point", "Select a grid point first."); return
        role = self.role.get()
        before = self.snapshot()
        try:
            x = float(self.selected_x.get())
            y = float(self.selected_y.get())
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("X and Y coordinates must be finite numbers")
        except ValueError as exc:
            messagebox.showerror("Invalid position", str(exc)); return
        self.invalidate_grid_buffers()
        column, row = self.selected
        default_coordinates = (
            self.project.grid.x_coordinate(column),
            self.project.grid.y_coordinate(row),
        )
        if (x, y) == default_coordinates:
            self.project.coordinate_overrides.pop(self.selected, None)
        else:
            self.project.coordinate_overrides[self.selected] = (x, y)
        if role == "none": self.project.markers.pop(self.selected, None)
        else:
            endpoint = self.endpoint_id.get().strip()
            if not endpoint:
                self.restore_snapshot(before)
                messagebox.showerror("Endpoint ID", "Endpoint ID cannot be blank."); return
            self.project.markers[self.selected] = Marker(role, endpoint)
        self.prune_grid_warehouse_configuration()
        try: self.project.validate()
        except ValueError as exc:
            self.restore_snapshot(before); messagebox.showerror("Invalid edit", str(exc)); return
        self.undo_stack.append(before)
        if len(self.undo_stack) > 100: self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.update_selected_editor()
        self.redraw(); self.status.set(f"Point edit applied at ({x:g}, {y:g}) m.")

    def save_project_dialog(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Grid project", "*.json")], initialfile=f"{self.project.grid.map_name}.grid.json")
        if path:
            try: self.project.validate(); self.rmf_maps.save_project(self.project, Path(path)); self.status.set(f"Project saved: {path}")
            except (OSError, ValueError) as exc: messagebox.showerror("Save failed", str(exc))

    def load_project_dialog(self):
        path = filedialog.askopenfilename(filetypes=[("Grid project", "*.json"), ("All files", "*")])
        if not path: return
        try:
            loaded_project = self.rmf_maps.load_project(Path(path))
            self.push_undo(); self.project = loaded_project
            self.selected = None; self.map_name.set(self.project.grid.map_name); self.level_name.set(self.project.grid.level_name)
            self.width.set(str(self.project.grid.width_m)); self.length.set(str(self.project.grid.length_m)); self.spacing.set(str(self.project.grid.spacing_m))
            self.spacing_y.set(str(self.project.grid.spacing_y_m))
            self.sync_grid_storage_controls()
            self.sync_grid_warehouse_storage_controls()
            self.sync_grid_sku_attribute_controls()
            self.update_grid_zone_summary()
            self.update_selected_editor(); self.redraw(); self.status.set(f"Project loaded: {path}")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc: messagebox.showerror("Load failed", str(exc))

    def export_yaml_dialog(self):
        path = filedialog.asksaveasfilename(defaultextension=".building.yaml", filetypes=[("RMF building map", "*.building.yaml"), ("YAML", "*.yaml")], initialdir=str(DEFAULT_BUILDING_OUTPUT.parent), initialfile=DEFAULT_BUILDING_OUTPUT.name)
        if path:
            try:
                self.project.validate(); self.rmf_maps.export_building(self.project, Path(path))
                self.status.set(f"RMF map exported: {path}")
                messagebox.showinfo("Export complete", f"Generated {self.project.vertex_count:,} vertices and {self.project.edge_count:,} bidirectional edges.\n\n{path}")
            except (OSError, ValueError) as exc: messagebox.showerror("Export failed", str(exc))


def run_gui(initial_project: GridProject | None = None) -> None:
    root = tk.Tk()
    GridMapEditorApp(root, initial_project)
    root.mainloop()
