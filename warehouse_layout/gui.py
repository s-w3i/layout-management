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
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    PHYSICAL_DIMENSION_KEYS,
    PHYSICAL_WEIGHT_KEY,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import (
    DEFAULT_AFFINITY_INPUT,
    DEFAULT_AFFINITY_OUTPUT,
    DEFAULT_BUILDING_INPUT,
    DEFAULT_GRID_INPUT,
    DEFAULT_BUILDING_OUTPUT,
    DEFAULT_CHILLED_INPUT,
    DEFAULT_SLOTTING_OUTPUT,
    DEFAULT_TRAFFIC_INPUT,
    DEFAULT_TRAFFIC_OUTPUT,
    DEFAULT_TRAFFIC_REPORT,
    DEFAULT_VELOCITY_INPUT,
)
from .domain import GridPosition, GridProject, GridSpec, Marker, StorageLayout
from .inventory import InventoryService
from .rmf import RmfMapService
from .slotting import SlottingLayoutRepository, SlottingService
from .traffic import (
    InsufficientStorageError,
    TrafficAwareSlottingService,
    TrafficCancelledError,
)
from .zone_settings_editor import ZoneStorageSettingsEditor


class GridMapEditorApp:
    """Coordinate the five-tab desktop UI and application services."""

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
        self.selected: GridPosition | None = None
        self.bulk_anchor: GridPosition | None = None
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.drag_undo_started = False
        self.tool = tk.StringVar(value="select")
        self.rack_prefix = tk.StringVar(value="RACK")
        self.map_name = tk.StringVar(value=self.project.grid.map_name)
        self.level_name = tk.StringVar(value=self.project.grid.level_name)
        self.width = tk.StringVar(value=str(self.project.grid.width_m))
        self.length = tk.StringVar(value=str(self.project.grid.length_m))
        self.spacing = tk.StringVar(value=str(self.project.grid.spacing_m))
        self.spacing_y = tk.StringVar(value=str(self.project.grid.spacing_y_m))
        self.selected_coordinate = tk.StringVar(value="No grid point selected")
        self.role = tk.StringVar(value="none")
        self.endpoint_id = tk.StringVar()
        storage_layout = self.project.storage_layout
        self.grid_storage_system = tk.StringVar(
            value=storage_layout.system_type if storage_layout else "AMR"
        )
        self.grid_storage_levels = tk.StringVar(
            value=str(storage_layout.levels_per_rack if storage_layout else 1)
        )
        self.grid_storage_slots = tk.StringVar(
            value=str(storage_layout.slots_per_level if storage_layout else 6)
        )
        self.grid_buffer_summary = tk.StringVar(
            value=(
                f"{len(storage_layout.buffers)} empty {storage_layout.buffer_level} buffer(s)"
                if storage_layout else "Storage buffers have not been assigned."
            )
        )
        self.summary = tk.StringVar()
        self.status = tk.StringVar(value="Bottom-left grid point is (0, 0)")
        self.canvas_viewports = {}
        self._build_ui()
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
        anchor_x, anchor_y = float(event.x), float(event.y)
        canvas.scale("all", anchor_x, anchor_y, factor, factor)
        state["offset_x"] = anchor_x + factor * (
            state["offset_x"] - anchor_x
        )
        state["offset_y"] = anchor_y + factor * (
            state["offset_y"] - anchor_y
        )
        state["scale"] = target_scale
        self.update_canvas_scrollregion(canvas)
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
        if state is None:
            return float(x), float(y)
        return (
            float(x) * state["scale"] + state["offset_x"],
            float(y) * state["scale"] + state["offset_y"],
        )

    def canvas_viewport_inverse_point(self, canvas, x, y):
        state = self.canvas_viewports.get(canvas)
        if state is None:
            return float(x), float(y)
        return (
            (float(x) - state["offset_x"]) / state["scale"],
            (float(y) - state["offset_y"]) / state["scale"],
        )

    @staticmethod
    def update_canvas_scrollregion(canvas):
        bounds = canvas.bbox("all")
        if bounds:
            canvas.configure(scrollregion=bounds)

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self.root)
        self.notebook = notebook
        notebook.grid(row=0, column=0, sticky="nsew")
        map_tab = ttk.Frame(notebook)
        affinity_tab = ttk.Frame(notebook)
        slotting_tab = ttk.Frame(notebook)
        slotting_layout_tab = ttk.Frame(notebook)
        traffic_tab = ttk.Frame(notebook)
        operations_tab = ttk.Frame(notebook)
        notebook.add(map_tab, text="Grid Map Editor")
        notebook.add(affinity_tab, text="SKU Affinity")
        notebook.add(slotting_tab, text="Inventory Slotting")
        notebook.add(slotting_layout_tab, text="Interactive Slotting Layout")
        notebook.add(traffic_tab, text="Traffic-Aware Slotting")
        notebook.add(operations_tab, text="Inventory Operations Demo")
        map_tab.columnconfigure(1, weight=1)
        map_tab.rowconfigure(0, weight=1)
        left = ttk.Frame(map_tab, padding=12)
        left.grid(row=0, column=0, sticky="ns")
        canvas_frame = ttk.Frame(map_tab, padding=(0, 12, 12, 12))
        canvas_frame.grid(row=0, column=1, sticky="nsew")
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)

        ttk.Label(left, text="WAREHOUSE GRID", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
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
        ttk.Label(left, text="CLICK TOOL", font=("TkDefaultFont", 10, "bold")).grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 5))
        tools = [
            ("Select / edit", "select"),
            ("Paint rack pickups (drag)", "rack"),
            ("Fill rack rectangle (2 clicks)", "rack_rectangle"),
            ("Place workstation drop-off", "workstation"),
            ("Clear markers (drag)", "clear"),
        ]
        for row, (label, value) in enumerate(tools, start=10):
            ttk.Radiobutton(left, text=label, variable=self.tool, value=value).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(left, text="Rack ID prefix").grid(row=15, column=0, sticky="w", pady=(7, 3))
        ttk.Entry(left, textvariable=self.rack_prefix, width=19).grid(row=15, column=1, sticky="ew", pady=(7, 3))

        ttk.Separator(left).grid(row=16, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(left, text="SELECTED GRID POINT", font=("TkDefaultFont", 10, "bold")).grid(row=17, column=0, columnspan=2, sticky="w")
        ttk.Label(left, textvariable=self.selected_coordinate).grid(row=18, column=0, columnspan=2, sticky="w", pady=(3, 6))
        ttk.Label(left, text="Role").grid(row=19, column=0, sticky="w", pady=3)
        role_box = ttk.Combobox(left, textvariable=self.role, state="readonly", values=("none", "rack", "workstation"), width=16)
        role_box.grid(row=19, column=1, sticky="ew", pady=3)
        ttk.Label(left, text="Endpoint ID").grid(row=20, column=0, sticky="w", pady=3)
        ttk.Entry(left, textvariable=self.endpoint_id, width=19).grid(row=20, column=1, sticky="ew", pady=3)
        ttk.Button(left, text="Apply point edit", command=self.apply_edit).grid(row=21, column=0, columnspan=2, sticky="ew", pady=(6, 12))

        ttk.Separator(left).grid(row=22, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(left, text="STORAGE BUFFERS", font=("TkDefaultFont", 10, "bold")).grid(row=23, column=0, columnspan=2, sticky="w", pady=(5, 3))
        ttk.Label(left, text="Layout type").grid(row=24, column=0, sticky="w", pady=3)
        ttk.Combobox(
            left, textvariable=self.grid_storage_system, state="readonly",
            values=("AMR", "Mini-load ASRS", "Pallet ASRS"), width=16,
        ).grid(row=24, column=1, sticky="ew", pady=3)
        buffer_capacity = ttk.Frame(left)
        buffer_capacity.grid(row=25, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(buffer_capacity, text="Levels").pack(side="left")
        ttk.Spinbox(buffer_capacity, from_=1, to=100, textvariable=self.grid_storage_levels, width=4).pack(side="left", padx=(4, 8))
        ttk.Label(buffer_capacity, text="Slots/level").pack(side="left")
        ttk.Spinbox(buffer_capacity, from_=1, to=100, textvariable=self.grid_storage_slots, width=4).pack(side="left", padx=(4, 0))
        ttk.Button(left, text="Assign empty storage buffers", command=self.assign_grid_buffers).grid(row=26, column=0, columnspan=2, sticky="ew", pady=(5, 3))
        ttk.Label(left, textvariable=self.grid_buffer_summary, foreground="#315b66", wraplength=230).grid(row=27, column=0, columnspan=2, sticky="w", pady=(2, 6))

        ttk.Separator(left).grid(row=28, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Button(left, text="Save editable project…", command=self.save_project_dialog).grid(row=29, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="Load editable project…", command=self.load_project_dialog).grid(row=30, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="Export RMF building YAML…", command=self.export_yaml_dialog).grid(row=31, column=0, columnspan=2, sticky="ew", pady=(10, 3))

        self.canvas = tk.Canvas(canvas_frame, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self.canvas_click)
        self.canvas.bind("<B1-Motion>", self.canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.enable_canvas_viewport(self.canvas)
        ttk.Label(canvas_frame, textvariable=self.status).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._build_affinity_tab(affinity_tab)
        self._build_slotting_tab(slotting_tab)
        self._build_interactive_slotting_tab(slotting_layout_tab)
        self._build_traffic_tab(traffic_tab)
        self._build_operations_tab(operations_tab)

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
        self.slot_chilled_path = tk.StringVar(value=str(DEFAULT_CHILLED_INPUT))
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
        self.slot_levels = tk.StringVar(value="1")
        self.slot_slots = tk.StringVar(value="6")
        self.slot_summary = tk.StringVar(value="Choose the inputs and generate a slotting layout.")
        self.slot_zone_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect its zone.")
        self.slot_rack_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect it.")
        self.slot_building = None
        self.slot_grid_project = None
        self.slot_rows = []
        self.slot_racks = []
        self.slot_selected_rack = None
        self.slot_loaded_path = None
        self.slot_zone_assignments = {}
        self.slot_attribute_catalog = self.attributes.starter_catalog()
        self.slot_location_attributes = {}
        self.slot_hierarchy_paths = []
        self.slot_storage_initialized = False
        self.slot_zone_storage_types = {}
        self.slot_zone_mode = tk.BooleanVar(value=True)
        self.slot_zone_auto = tk.BooleanVar(value=True)
        self.slot_zone_drag_start = None
        self.slot_zone_drag_current = None
        self.slot_legend = tk.StringVar(value="Load the grid project JSON, then drag a rectangle to assign rack zones.")
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

        ttk.Label(form, text="Chilled SKU CSV (optional)").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
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
            text="0 = ABC bay purity · 100 = same-bay affinity consolidation",
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

        ttk.Label(form, text="Handling unit").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        self.slot_handling_unit_box = ttk.Combobox(form, textvariable=self.slot_handling_unit, state="disabled", values=("AMR shelf", "Tote", "Pallet"), width=18)
        self.slot_handling_unit_box.grid(row=5, column=1, sticky="w", pady=4)
        capacity = ttk.Frame(form)
        capacity.grid(row=6, column=1, sticky="w", pady=4)
        ttk.Label(form, text="Rack capacity").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(capacity, text="Levels").pack(side="left")
        self.slot_levels_box = ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_levels, width=5, state="disabled")
        self.slot_levels_box.pack(side="left", padx=(5, 14))
        ttk.Label(capacity, text="Slots per level").pack(side="left")
        self.slot_slots_box = ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_slots, width=5, state="disabled")
        self.slot_slots_box.pack(side="left", padx=5)

        ttk.Label(form, text="Output layout JSON").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_output_path).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=self.browse_slot_output).grid(row=7, column=2, padx=(8, 0), pady=4)
        slot_actions = ttk.Frame(form)
        slot_actions.grid(row=8, column=1, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Button(slot_actions, text="Zone storage settings…", command=self.open_zone_storage_settings).pack(side="left")
        ttk.Button(slot_actions, text="Advanced attributes…", command=self.open_attribute_editor).pack(side="left", padx=(6, 0))
        ttk.Button(slot_actions, text="Generate slotting layout", command=self.run_slotting, style="Accent.TButton").pack(side="left", padx=(12, 0))
        ttk.Label(form, textvariable=self.slot_summary, foreground="#315b66").grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.slot_strategy_changed()

        zone_editor = ttk.LabelFrame(
            parent, text="Slotting zone editor", padding=8
        )
        zone_editor.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        zone_editor.columnconfigure(0, weight=1)
        zone_editor.rowconfigure(1, weight=1)
        zone_controls = ttk.Frame(zone_editor)
        zone_controls.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(zone_controls, text="Zone ID").pack(side="left")
        ttk.Entry(
            zone_controls, textvariable=self.slot_zone, width=20
        ).pack(side="left", padx=(8, 12))
        ttk.Checkbutton(
            zone_controls,
            text="Auto next ID",
            variable=self.slot_zone_auto,
        ).pack(side="left")
        ttk.Button(
            zone_controls,
            text="Clear zones",
            command=self.clear_slot_zones,
        ).pack(side="left", padx=(6, 0))
        self.slot_zone_canvas = tk.Canvas(
            zone_editor,
            background="white",
            highlightthickness=1,
            highlightbackground="#9aa8ae",
        )
        self.slot_zone_canvas.grid(row=1, column=0, sticky="nsew")
        self.slot_zone_canvas.bind(
            "<Configure>", lambda _event: self.draw_slotting_layout()
        )
        self.slot_zone_canvas.bind("<ButtonPress-1>", self.slot_canvas_press)
        self.slot_zone_canvas.bind("<B1-Motion>", self.slot_canvas_drag)
        self.slot_zone_canvas.bind("<ButtonRelease-1>", self.slot_canvas_release)
        self.enable_canvas_viewport(self.slot_zone_canvas)
        ttk.Label(
            zone_editor,
            textvariable=self.slot_legend,
            foreground="#4d646d",
        ).grid(row=2, column=0, sticky="w", pady=(5, 0))

    def _build_interactive_slotting_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.LabelFrame(parent, text="Generated layout viewer", padding=12)
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        ttk.Button(
            controls,
            text="Load saved layout…",
            command=self.load_interactive_slotting_layout,
        ).pack(side="left")
        ttk.Label(
            controls,
            textvariable=self.slot_viewer_status,
            foreground="#315b66",
        ).pack(side="left", padx=(12, 0))
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
            text="Read-only generated assignment view · click a rack to inspect it",
            foreground="#4d646d",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))

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
        columns = ("rank", "sku", "class", "flags", "static", "dynamic", "unit_type", "unit_id", "status")
        tree_frame = ttk.Frame(rack_view)
        tree_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=8, pady=(8, 0))
        tree_frame.columnconfigure(0, weight=1); tree_frame.rowconfigure(0, weight=1)
        self.slot_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        headings = {"rank":"Rank", "sku":"SKU", "class":"ABC", "flags":"Storage flags", "static":"Current static address", "dynamic":"Current dynamic address", "unit_type":"Unit type", "unit_id":"Handling unit ID", "status":"Status"}
        widths = {"rank":55, "sku":95, "class":50, "flags":180, "static":150, "dynamic":230, "unit_type":90, "unit_id":120, "status":90}
        for column in columns:
            self.slot_tree.heading(column, text=headings[column]); self.slot_tree.column(column, width=widths[column], anchor="center" if column not in {"flags","static","dynamic"} else "w")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.slot_tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.slot_tree.xview)
        self.slot_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.slot_tree.grid(row=0, column=0, sticky="nsew"); yscroll.grid(row=0, column=1, sticky="ns"); xscroll.grid(row=1, column=0, sticky="ew")
        ttk.Button(rack_view, text="Show all assignments", command=lambda: self.show_slotting_rows(self.slot_rows)).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(7, 0))

    def _build_traffic_tab(self, parent):
        self.traffic_building_path = tk.StringVar(value=str(DEFAULT_BUILDING_INPUT))
        self.traffic_velocity_path = tk.StringVar(value=str(DEFAULT_VELOCITY_INPUT))
        self.traffic_chilled_path = tk.StringVar()
        self.traffic_order_path = tk.StringVar(value=str(DEFAULT_TRAFFIC_INPUT))
        self.traffic_storage_config_path = tk.StringVar()
        self.traffic_affinity_weight = tk.StringVar(value="50")
        self.traffic_handling_unit = tk.StringVar(value="AMR shelf")
        self.traffic_levels = tk.StringVar(value="1")
        self.traffic_slots = tk.StringVar(value="6")
        self.traffic_network_mode = tk.StringVar(value="Use embedded RMF map")
        self.traffic_network_path = tk.StringVar()
        self.traffic_start_date = tk.StringVar()
        self.traffic_end_date = tk.StringVar()
        self.traffic_output_path = tk.StringVar(value=str(DEFAULT_TRAFFIC_OUTPUT))
        self.traffic_max_travel = tk.StringVar()
        self.traffic_hotspot_percentile = tk.StringVar()
        self.traffic_parameter_status = tk.StringVar(
            value="Generate once to calculate warehouse-specific parameters."
        )
        self.traffic_status = tk.StringVar(
            value="Select raw inputs; this tab runs ABC, affinity, validation, visits, and traffic placement."
        )
        self.traffic_kpis = tk.StringVar(
            value="Groups —  · Unit visits —  · Mapped —  · Peak —  · P95 —  · Travel —  · Relocated —"
        )
        self.traffic_view_mode = tk.StringVar(value="Before")
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
        self.traffic_loaded_building_path = None
        self.traffic_racks = []
        self.traffic_zone_assignments = {}
        self.traffic_location_attributes = {}
        self.traffic_attribute_catalog = self.attributes.starter_catalog()
        self.traffic_area_mode = tk.BooleanVar(value=False)
        self.traffic_area_id = tk.StringVar(value="Z01")
        self.traffic_area_type = tk.StringVar(value="Ambient")
        self.traffic_area_drag_start = None
        self.traffic_area_drag_current = None

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        form = ttk.LabelFrame(parent, text="Traffic-aware slotting inputs", padding=10)
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(4, weight=1)

        ttk.Label(form, text="Building YAML").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_building_path).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_building_path,
                [("RMF building YAML", "*.building.yaml"), ("YAML", "*.yaml")],
            ),
        ).grid(row=0, column=2, padx=(2, 12), pady=3)
        ttk.Label(form, text="Order-history Excel").grid(row=0, column=3, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_order_path).grid(row=0, column=4, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_order_path,
                [("Excel workbook", "*.xlsx"), ("All files", "*")],
            ),
        ).grid(row=0, column=5, pady=3)

        ttk.Label(form, text="ABC SKU velocity CSV").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_velocity_path).grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_velocity_path, [("CSV", "*.csv"), ("All files", "*")],
            ),
        ).grid(row=1, column=2, padx=(2, 12), pady=3)
        ttk.Label(form, text="Chilled SKU CSV (optional)").grid(row=1, column=3, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_chilled_path).grid(row=1, column=4, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_chilled_path, [("CSV", "*.csv"), ("All files", "*")],
            ),
        ).grid(row=1, column=5, pady=3)

        ttk.Label(form, text="Storage rules (optional; assignments ignored)").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_storage_config_path).grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…",
            command=lambda: self.browse_slot_input(
                self.traffic_storage_config_path,
                [("Slotting configuration", "*.slotting.json"), ("JSON", "*.json")],
            ),
        ).grid(row=2, column=2, padx=(2, 12), pady=3)
        ttk.Label(form, text="Movement network").grid(row=2, column=3, sticky="w", pady=3)
        network_box = ttk.Combobox(
            form, textvariable=self.traffic_network_mode, state="readonly",
            values=("Use embedded RMF map", "Use generic network JSON"), width=25,
        )
        network_box.grid(row=2, column=4, sticky="w", padx=(8, 4), pady=3)
        network_box.bind("<<ComboboxSelected>>", self.traffic_network_mode_changed)
        self.traffic_network_entry = ttk.Entry(form, textvariable=self.traffic_network_path)
        self.traffic_network_entry.grid(row=3, column=3, columnspan=2, sticky="ew", padx=(0, 4), pady=3)
        self.traffic_network_browse = ttk.Button(
            form, text="Browse network…",
            command=lambda: self.browse_slot_input(
                self.traffic_network_path,
                [("Movement network JSON", "*.json"), ("All files", "*")],
            ),
        )
        self.traffic_network_browse.grid(row=3, column=5, pady=3)

        setup = ttk.Frame(form)
        setup.grid(row=3, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(setup, text="Affinity weight %").pack(side="left")
        ttk.Spinbox(setup, from_=0, to=100, textvariable=self.traffic_affinity_weight, width=6).pack(side="left", padx=(5, 10))
        ttk.Label(setup, text="Unit").pack(side="left")
        ttk.Combobox(
            setup, textvariable=self.traffic_handling_unit, state="readonly",
            values=("AMR shelf", "Tote", "Pallet"), width=11,
        ).pack(side="left", padx=(5, 10))
        ttk.Label(setup, text="Levels").pack(side="left")
        ttk.Spinbox(setup, from_=1, to=100, textvariable=self.traffic_levels, width=4).pack(side="left", padx=(4, 8))
        ttk.Label(setup, text="Slots/level").pack(side="left")
        ttk.Spinbox(setup, from_=1, to=100, textvariable=self.traffic_slots, width=4).pack(side="left", padx=(4, 0))
        dates = ttk.Frame(form)
        dates.grid(row=4, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(dates, text="Inclusive dates").pack(side="left")
        ttk.Entry(dates, textvariable=self.traffic_start_date, width=11).pack(side="left", padx=(8, 3))
        ttk.Label(dates, text="to").pack(side="left")
        ttk.Entry(dates, textvariable=self.traffic_end_date, width=11).pack(side="left", padx=(3, 0))
        ttk.Label(dates, text="(YYYY-MM-DD; blank = full range)", foreground="#4d646d").pack(side="left", padx=(7, 0))
        parameters = ttk.Frame(form)
        parameters.grid(row=4, column=3, columnspan=3, sticky="w", pady=3)
        ttk.Label(parameters, text="Auto parameters:").pack(side="left")
        ttk.Label(parameters, text="Max travel increase %").pack(side="left", padx=(8, 3))
        self.traffic_max_travel_entry = ttk.Entry(parameters, textvariable=self.traffic_max_travel, width=7)
        self.traffic_max_travel_entry.pack(side="left")
        ttk.Label(parameters, text="Hotspot percentile").pack(side="left", padx=(8, 3))
        self.traffic_hotspot_entry = ttk.Entry(parameters, textvariable=self.traffic_hotspot_percentile, width=7)
        self.traffic_hotspot_entry.pack(side="left")

        ttk.Label(form, text="Optimized layout output").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self.traffic_output_path).grid(row=5, column=1, columnspan=4, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(
            form, text="Browse…", command=lambda: self._browse_traffic_output()
        ).grid(row=5, column=5, pady=3)
        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, columnspan=6, sticky="ew", pady=(7, 0))
        self.traffic_analyze_button = ttk.Button(actions, text="Run stages 1–5", command=self.start_traffic_analysis)
        self.traffic_generate_button = ttk.Button(actions, text="Run full pipeline", command=self.start_traffic_generation)
        self.traffic_regenerate_button = ttk.Button(actions, text="Regenerate with edited values", command=lambda: self.start_traffic_generation(True))
        self.traffic_cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_traffic_work, state="disabled")
        self.traffic_save_button = ttk.Button(actions, text="Save layout", command=self.save_traffic_layout, state="disabled")
        self.traffic_export_button = ttk.Button(actions, text="Export report…", command=self.export_traffic_report, state="disabled")
        for widget in (
            self.traffic_analyze_button, self.traffic_generate_button,
            self.traffic_regenerate_button, self.traffic_cancel_button,
            self.traffic_save_button, self.traffic_export_button,
        ):
            widget.pack(side="left", padx=(0, 6))
        ttk.Progressbar(actions, variable=tk.DoubleVar(value=0), maximum=100, length=180).pack_forget()
        self.traffic_progress_value = tk.DoubleVar(value=0)
        self.traffic_progress = ttk.Progressbar(
            actions, variable=self.traffic_progress_value, maximum=100, length=180
        )
        self.traffic_progress.pack(side="left", padx=(8, 6))
        ttk.Label(actions, textvariable=self.traffic_parameter_status, foreground="#315b66").pack(side="left", padx=(5, 0))
        self.traffic_network_mode_changed()

        summary = ttk.Frame(parent, padding=(12, 2))
        summary.grid(row=1, column=0, sticky="ew")
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
            state="readonly", values=("Before", "After"), width=9,
        )
        view_box.pack(side="left", padx=(6, 0))
        view_box.bind("<<ComboboxSelected>>", lambda _event: self.draw_traffic_map())
        ttk.Label(
            view_actions,
            text="Lane colour = route load · rack colour = handling-unit visits · purple = swapped · ◇ endpoint",
            foreground="#4d646d",
        ).pack(side="left", padx=(10, 0))
        area_actions = ttk.Frame(map_frame)
        area_actions.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(
            area_actions, text="Load map / label chilled zones",
            command=self.load_traffic_area_map,
        ).pack(side="left")
        ttk.Checkbutton(
            area_actions, text="Rectangle labeling", variable=self.traffic_area_mode,
            command=self.draw_traffic_map,
        ).pack(side="left", padx=(8, 4))
        ttk.Label(area_actions, text="Temperature zone ID").pack(side="left")
        ttk.Entry(area_actions, textvariable=self.traffic_area_id, width=8).pack(side="left", padx=(4, 8))
        ttk.Combobox(
            area_actions, textvariable=self.traffic_area_type, state="readonly",
            values=("Ambient", "Chilled"), width=10,
        ).pack(side="left")
        ttk.Button(
            area_actions, text="Clear labels", command=self.clear_traffic_areas,
        ).pack(side="left", padx=(8, 0))
        self.traffic_canvas = tk.Canvas(
            map_frame, background="white", highlightthickness=1,
            highlightbackground="#9aa8ae",
        )
        self.traffic_canvas.grid(row=2, column=0, sticky="nsew")
        self.traffic_canvas.bind("<Configure>", lambda _event: self.draw_traffic_map())
        self.traffic_canvas.bind("<ButtonPress-1>", self.traffic_canvas_press)
        self.traffic_canvas.bind("<B1-Motion>", self.traffic_canvas_drag)
        self.traffic_canvas.bind("<ButtonRelease-1>", self.traffic_canvas_release)
        self.enable_canvas_viewport(self.traffic_canvas)
        self.traffic_canvas.tag_bind("traffic_link", "<Button-1>", self.traffic_resource_click)

        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(details)
        tabs.grid(row=0, column=0, sticky="nsew")
        resource_tab, relocation_tab, rejected_tab, parameter_tab = (
            ttk.Frame(tabs), ttk.Frame(tabs), ttk.Frame(tabs), ttk.Frame(tabs)
        )
        tabs.add(resource_tab, text="Congested Resources")
        tabs.add(relocation_tab, text="Relocations")
        tabs.add(rejected_tab, text="Fixed / Rejected")
        tabs.add(parameter_tab, text="Parameters")
        self.traffic_resource_tree = self._traffic_tree(
            resource_tab,
            (("resource", "Resource", 135), ("before", "Before", 75),
             ("after", "After", 75), ("change", "Change", 75),
             ("capacity", "Capacity", 75)),
        )
        self.traffic_relocation_tree = self._traffic_tree(
            relocation_tab,
            (("unit", "Handling unit", 120), ("from", "From", 120),
             ("to", "To", 120), ("swap", "Swapped with", 110)),
        )
        self.traffic_relocation_tree.bind("<<TreeviewSelect>>", self.traffic_relocation_select)
        self.traffic_rejected_tree = self._traffic_tree(
            rejected_tab, (("unit", "Handling unit", 130), ("reason", "Reason", 330)),
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
        enabled = self.traffic_network_mode.get() == "Use generic network JSON"
        state = "normal" if enabled else "disabled"
        self.traffic_network_entry.configure(state=state)
        self.traffic_network_browse.configure(state=state)

    def load_traffic_area_map(self):
        try:
            path = Path(self.traffic_building_path.get()).expanduser().resolve()
            building = self.rmf_maps.load_building(path)
            _level, racks, _workstations, _unreachable = self.slotting.rack_distances(
                building
            )
            network = self.traffic.network_from_rmf(building)
            if self.traffic_storage_config_path.get().strip():
                config = self.layouts.load(
                    Path(self.traffic_storage_config_path.get()).expanduser()
                )
                zones = dict(config.get("zone_assignments") or {})
                locations = copy.deepcopy(config.get("location_attributes") or {})
                catalog = config.get("attribute_catalog") or self.attributes.starter_catalog()
            elif self.traffic_loaded_building_path == path:
                zones = dict(self.traffic_zone_assignments)
                locations = copy.deepcopy(self.traffic_location_attributes)
                catalog = self.traffic_attribute_catalog
            else:
                zones, locations = {}, {}
                catalog = self.attributes.starter_catalog()
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            messagebox.showerror("Traffic storage-area map", str(exc))
            return False
        locations.setdefault("Z01", {
            "chilled": False, **STANDARD_STORAGE_DEFAULTS,
        })
        self.traffic_building = building
        self.traffic_loaded_building_path = path
        self.traffic_racks = racks
        self.traffic_network = network
        self.traffic_zone_assignments = zones
        self.traffic_location_attributes = locations
        self.traffic_attribute_catalog = catalog
        self.slotting.apply_zone_local_aisles(
            building, racks, zones, "Z01"
        )
        self.traffic_area_mode.set(True)
        self.traffic_analysis = None
        self.traffic_result = None
        self.traffic_pipeline_result = None
        self.traffic_output_payload = None
        self.traffic_area_drag_start = self.traffic_area_drag_current = None
        self.traffic_status.set(
            "Temperature-zone labeling active: choose Ambient or Chilled and drag around real racks."
        )
        self.draw_traffic_map()
        return True

    def clear_traffic_areas(self):
        self.traffic_zone_assignments = {}
        self.traffic_location_attributes = {
            "Z01": {"chilled": False, **STANDARD_STORAGE_DEFAULTS}
        }
        if self.traffic_building:
            self.slotting.apply_zone_local_aisles(
                self.traffic_building, self.traffic_racks, {}, "Z01"
            )
        self.traffic_area_id.set("Z01")
        self.traffic_status.set("Cleared temperature labels; all racks are ambient Z01.")
        self.draw_traffic_map()

    def traffic_canvas_press(self, event):
        if not self.traffic_area_mode.get() or not self.traffic_building:
            return
        self.traffic_area_drag_start = (event.x, event.y)
        self.traffic_area_drag_current = (event.x, event.y)
        self.draw_traffic_map()

    def traffic_canvas_drag(self, event):
        if self.traffic_area_drag_start is None or not self.traffic_area_mode.get():
            return
        self.traffic_area_drag_current = (event.x, event.y)
        self.draw_traffic_map()

    def traffic_canvas_release(self, event):
        if self.traffic_area_drag_start is None or not self.traffic_area_mode.get():
            return
        self.traffic_area_drag_current = (event.x, event.y)
        zone = self.traffic_area_id.get().strip()
        if not zone:
            messagebox.showerror("Storage area", "Enter an area ID before labeling racks.")
            self.traffic_area_drag_start = self.traffic_area_drag_current = None
            return
        geometry = self._traffic_geometry()
        x1, y1 = self.traffic_area_drag_start
        x2, y2 = self.traffic_area_drag_current
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        selected = []
        for rack in self.traffic_racks:
            node = self.traffic_network.nodes.get(f"v:{rack['vertex_index']}")
            if node is None:
                continue
            x, y = self._traffic_point(node, geometry)
            x, y = self.canvas_viewport_point(self.traffic_canvas, x, y)
            if left - 7 <= x <= right + 7 and top - 7 <= y <= bottom + 7:
                self.traffic_zone_assignments[rack["waypoint"]] = zone
                selected.append(rack)
        area_type = self.traffic_area_type.get()
        self.traffic_location_attributes.setdefault(
            zone, {**STANDARD_STORAGE_DEFAULTS}
        )["chilled"] = area_type == "Chilled"
        self.slotting.apply_zone_local_aisles(
            self.traffic_building, self.traffic_racks,
            self.traffic_zone_assignments, "Z01",
        )
        self.traffic_area_drag_start = self.traffic_area_drag_current = None
        next_zone = self.slotting.next_zone_id(zone) if selected else zone
        if selected:
            self.traffic_area_id.set(next_zone)
        self.traffic_status.set(
            f"Labeled {len(selected)} rack(s) as {zone} · {area_type}. "
            + (
                f"Next area ID: {next_zone}. "
                if selected else "Area ID was not advanced. "
            )
            + "Edit capacities if the default profile does not match the equipment."
        )
        self.draw_traffic_map()

    def _traffic_requirement_gaps(self, sku_rows, levels, slots):
        """Return actual unsupported causes for the current temperature/slot model."""
        rack_counts = {}
        for rack in self.traffic_racks:
            zone = self.traffic_zone_assignments.get(rack["waypoint"], "Z01")
            rack_counts[zone] = rack_counts.get(zone, 0) + 1
        gaps = {}
        for sku in sku_rows:
            requirements = sku.get("sku_requirements") or {}
            profile = self.attributes.physical_profile(requirements)
            overweight = profile["storage_class"] in {
                "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT"
            }
            supported = False
            temperature_supported = False
            for zone, rack_count in rack_counts.items():
                if rack_count <= 0:
                    continue
                effective = self.traffic_location_attributes.get(zone, {})
                if self.attributes.hard_compatibility_issues(requirements, effective):
                    continue
                temperature_supported = True
                overrides = self.attributes.required_local_overrides(
                    requirements, effective, self.traffic_attribute_catalog
                )
                # Dimensions are handled by contiguous horizontal slot spans.
                non_dimension_overrides = {
                    key: value for key, value in overrides.items()
                    if key not in PHYSICAL_DIMENSION_KEYS
                    and not (overweight and key == PHYSICAL_WEIGHT_KEY)
                }
                if non_dimension_overrides:
                    continue
                if profile["data_status"] == "COMPLETE":
                    footprint = self.slotting.required_slot_footprint(
                        requirements, effective, levels, slots
                    )
                    if footprint is None:
                        continue
                supported = True
                break
            if supported:
                continue
            if not temperature_supported:
                label = (
                    "chilled zone unavailable"
                    if requirements.get("chilled") is True
                    else "ambient zone unavailable"
                )
            elif profile["data_status"] == "COMPLETE":
                label = "slot footprint exceeds configured levels/slots"
            else:
                label = "location attribute mismatch"
            gaps[label] = gaps.get(label, 0) + 1
        return gaps

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

    def start_traffic_analysis(self):
        self._start_traffic_work("analyze", False)

    def start_traffic_generation(self, use_adjusted=False):
        self._start_traffic_work("generate", use_adjusted)

    def _start_traffic_work(self, mode, use_adjusted):
        if self.traffic_worker and self.traffic_worker.is_alive():
            return
        try:
            building_path = Path(self.traffic_building_path.get()).expanduser().resolve()
            velocity_path = Path(self.traffic_velocity_path.get()).expanduser().resolve()
            order_path = Path(self.traffic_order_path.get()).expanduser().resolve()
            chilled_path = (
                Path(self.traffic_chilled_path.get()).expanduser().resolve()
                if self.traffic_chilled_path.get().strip() else None
            )
            storage_config_path = (
                Path(self.traffic_storage_config_path.get()).expanduser().resolve()
                if self.traffic_storage_config_path.get().strip() else None
            )
            network_path = (
                Path(self.traffic_network_path.get()).expanduser().resolve()
                if self.traffic_network_mode.get() == "Use generic network JSON"
                else None
            )
            start = date.fromisoformat(self.traffic_start_date.get()) if self.traffic_start_date.get().strip() else None
            end = date.fromisoformat(self.traffic_end_date.get()) if self.traffic_end_date.get().strip() else None
            affinity_weight = float(self.traffic_affinity_weight.get()) / 100.0
            if not 0 <= affinity_weight <= 1:
                raise ValueError("affinity weight must be between 0% and 100%")
            levels = int(self.traffic_levels.get())
            slots = int(self.traffic_slots.get())
            handling_unit = self.traffic_handling_unit.get()
            max_travel = (
                float(self.traffic_max_travel.get()) / 100.0
                if use_adjusted else None
            )
            hotspot = float(self.traffic_hotspot_percentile.get()) if use_adjusted else None
        except (ValueError, OSError) as exc:
            messagebox.showerror("Traffic-aware slotting", str(exc))
            return
        if use_adjusted and (not self.traffic_max_travel.get().strip() or not self.traffic_hotspot_percentile.get().strip()):
            messagebox.showerror("Traffic-aware slotting", "Generate an automatic suggestion before editing parameters.")
            return
        # A new attempt invalidates any prior saveable recommendation. If this
        # run cannot place every SKU, the UI must not offer a stale layout as
        # though it were the result of the current inputs.
        self.traffic_baseline_payload = None
        self.traffic_analysis = None
        self.traffic_result = None
        self.traffic_output_payload = None
        self.traffic_pipeline_result = None
        self._set_traffic_busy(False)
        inline_zones = {}
        inline_locations = {}
        inline_catalog = self.attributes.starter_catalog()
        if storage_config_path is None:
            if self.traffic_loaded_building_path != building_path:
                if not self.load_traffic_area_map():
                    return
            try:
                preview_rows = self.slotting.load_velocity(
                    velocity_path, self.traffic_attribute_catalog, chilled_path
                )
                gaps = self._traffic_requirement_gaps(preview_rows, levels, slots)
            except (OSError, ValueError, TypeError) as exc:
                messagebox.showerror("Traffic-aware slotting", str(exc))
                return
            if gaps:
                detail = "\n".join(
                    f"• {count} {label} SKU(s)"
                    for label, count in sorted(gaps.items())
                )
                needs_chilled_zone = any(
                    label.startswith("chilled ") for label in gaps
                )
                self.traffic_area_mode.set(needs_chilled_zone)
                self.draw_traffic_map()
                self.traffic_status.set(
                    "Chilled zones must be labeled before generation."
                    if needs_chilled_zone else
                    "The slot model does not have enough compatible capacity."
                )
                messagebox.showwarning(
                    "Chilled-zone labeling required"
                    if needs_chilled_zone else "Storage capacity adjustment required",
                    "The current storage areas cannot satisfy these inputs:\n\n"
                    f"{detail}\n\n"
                    + (
                        "Label only the actual chilled racks, then run the pipeline again. "
                        if needs_chilled_zone else
                        "Increase levels/slots or correct the physical input data, then run again. "
                    )
                    + "Oversize SKUs are placed by the slotting algorithm and reserve "
                    "contiguous slots/levels automatically; overweight footprints "
                    "start at the bottom level.",
                )
                return
            inline_zones = copy.deepcopy(self.traffic_zone_assignments)
            inline_locations = copy.deepcopy(self.traffic_location_attributes)
            inline_catalog = copy.deepcopy(self.traffic_attribute_catalog)
        self.traffic_area_mode.set(False)
        self.traffic_cancel_event.clear()
        self.traffic_progress_value.set(0)
        self.traffic_status.set("Starting traffic analysis…")
        self._set_traffic_busy(True)

        def report(current, total, message):
            self.traffic_messages.put(("progress", current, total, message))

        def worker():
            try:
                building = self.rmf_maps.load_building(building_path)
                if storage_config_path is not None:
                    storage_config = self.layouts.load(storage_config_path)
                    catalog = storage_config.get("attribute_catalog") or self.attributes.starter_catalog()
                    locations = storage_config.get("location_attributes") or {}
                    zones = storage_config.get("zone_assignments") or {}
                else:
                    catalog = inline_catalog
                    locations = inline_locations
                    zones = inline_zones
                sku_rows = self.slotting.load_velocity(
                    velocity_path, catalog, chilled_path
                )
                report(1, 7, "Loaded map, SKU demand, and storage rules")
                dataset = self.affinity.load_orders(
                    order_path,
                    progress=lambda current, total, message: report(current, total, message),
                    cancelled=self.traffic_cancel_event.is_set,
                )
                affinity_analysis = self.affinity.analyze(dataset, start, end)
                network = (
                    self.traffic.load_network(network_path)
                    if network_path is not None
                    else self.traffic.network_from_rmf(building)
                )
                pipeline = self.traffic.run_full_pipeline(
                    building, sku_rows, affinity_analysis, network,
                    affinity_weight=affinity_weight,
                    levels_per_rack=levels,
                    slots_per_level=slots,
                    handling_unit_type=handling_unit,
                    zone_assignments=zones,
                    attribute_catalog=catalog,
                    location_attributes=locations,
                    start_date=start,
                    end_date=end,
                    optimize_traffic=mode == "generate",
                    maximum_travel_increase=max_travel,
                    hotspot_percentile=hotspot,
                    source_building=str(building_path),
                    source_velocity=str(velocity_path),
                    source_chilled=str(chilled_path or ""),
                    source_orders=str(order_path),
                    source_storage_rules=str(storage_config_path or ""),
                    progress=report,
                    cancelled=self.traffic_cancel_event.is_set,
                )
                payload = pipeline.pretraffic_payload
                demand = pipeline.affinity_demand
                analysis = pipeline.pretraffic_analysis
                result = pipeline.optimization
                output_payload = pipeline.output_payload
                self.traffic_messages.put((
                    "done", payload, dataset, network, demand, analysis,
                    result, output_payload, pipeline,
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
        self.traffic_analyze_button.configure(state=state)
        self.traffic_generate_button.configure(state=state)
        self.traffic_regenerate_button.configure(
            state="disabled" if busy or self.traffic_result is None else "normal"
        )
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
                    "Generation stopped: label chilled zones or increase slot capacity."
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
                        "Label the real chilled racks, or increase levels/slots, "
                        if needs_chilled_zone else "Increase levels/slots, "
                    )
                    + "then generate again. Oversize placement and contiguous-slot "
                    "reservation are handled automatically.",
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
        self.traffic_selected_unit = None
        self.traffic_start_date.set(demand.start_date)
        self.traffic_end_date.set(demand.end_date)
        if result is not None:
            params = result.parameters
            self.traffic_max_travel.set(f"{params['maximum_travel_increase'] * 100:.4g}")
            self.traffic_hotspot_percentile.set(f"{params['hotspot_percentile']:.4g}")
            label = "Automatically suggested" if params["parameter_status"] == "AUTO_SUGGESTED" else "User adjusted"
            self.traffic_parameter_status.set(f"{label} from this demand and network.")
            self.traffic_view_mode.set("After")
        else:
            self.traffic_view_mode.set("Before")
            self.traffic_parameter_status.set("Analysis complete; generate to derive optimization parameters.")
        self.traffic_progress_value.set(100)
        self._set_traffic_busy(False)
        self.populate_traffic_results()
        self.draw_traffic_map()

    def populate_traffic_results(self):
        for tree in (
            self.traffic_resource_tree, self.traffic_relocation_tree,
            self.traffic_rejected_tree, self.traffic_parameter_tree,
        ):
            tree.delete(*tree.get_children())
        before = self.traffic_result.before if self.traffic_result else self.traffic_analysis
        after = self.traffic_result.after if self.traffic_result else before
        if before is None:
            return
        relocated = len({row["handling_unit_id"] for row in self.traffic_result.relocations}) if self.traffic_result else 0
        grouping = (
            self.traffic_pipeline_result.grouping_metrics
            if self.traffic_pipeline_result else {}
        )
        basic_visits = int(grouping.get(
            "basic_handling_unit_visits", before.demand.handling_unit_visits
        ))
        grouped_visits = int(grouping.get(
            "affinity_handling_unit_visits", before.demand.handling_unit_visits
        ))
        saved_fraction = float(grouping.get(
            "handling_unit_visit_reduction_fraction", 0.0
        ))
        self.traffic_kpis.set(
            f"Groups {before.demand.fulfillment_groups:,}  · ABC visits {basic_visits:,} → grouped {grouped_visits:,} "
            f"({saved_fraction * 100:+.2f}%)  · Mapped {len(before.mapped_units):,}  · "
            f"Raw peak {before.metrics['raw_peak_load']:.1f} → {after.metrics['raw_peak_load']:.1f}  · "
            f"Raw P95 {before.metrics['raw_p95_load']:.1f} → {after.metrics['raw_p95_load']:.1f}  · "
            f"Travel {before.metrics['expected_travel']:.1f} → {after.metrics['expected_travel']:.1f}  · Relocated {relocated:,}"
        )
        self.traffic_status.set(
            f"{len(before.unmapped_units):,} unmapped and {len(before.unreachable_units):,} unreachable units · "
            f"{len(before.demand.unmatched_skus):,} workbook SKUs absent from the layout · "
            f"hard validation {grouping.get('hard_validation_status', 'not run')} · "
            f"unverified physical SKUs {int(grouping.get('unverified_physical_sku_count', 0)):,}."
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
        if self.traffic_result:
            for row in self.traffic_result.relocations:
                self.traffic_relocation_tree.insert("", "end", values=(
                    row["handling_unit_id"], row["from"], row["to"], row["swap_with"],
                ))
            rejected = list(self.traffic_result.rejected_units)
            rejected.extend(
                {"handling_unit_id": unit, "reason": "no movement-network location mapping"}
                for unit in before.unmapped_units
            )
            rejected.extend(
                {"handling_unit_id": unit, "reason": "no route to a service endpoint"}
                for unit in before.unreachable_units
            )
            for row in rejected:
                self.traffic_rejected_tree.insert("", "end", values=(
                    row["handling_unit_id"], row["reason"],
                ))
            labels = {
                "parameter_status": "Parameter status",
                "maximum_travel_increase": "Maximum travel increase",
                "hotspot_percentile": "Hotspot percentile",
                "hotspot_threshold": "Baseline hotspot threshold",
                "baseline_peak_load": "Baseline peak load",
                "feasible_swap_count": "Feasible unit swaps",
                "empirical_candidate_count": "Pareto candidates",
            }
            if grouping:
                for label, value in (
                    ("ABC handling-unit visits", basic_visits),
                    ("Affinity-grouped visits", grouped_visits),
                    ("Visits saved", grouping["handling_unit_visits_saved"]),
                    ("Visit reduction", f"{saved_fraction * 100:.3f}%"),
                    ("Affinity weight", f"{float(self.traffic_affinity_weight.get()):.3g}%"),
                ):
                    self.traffic_parameter_tree.insert("", "end", values=(label, value))
            for key, value in self.traffic_result.parameters.items():
                if key == "maximum_travel_increase":
                    value = f"{value * 100:.4g}%"
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
            if self.traffic_result and self.traffic_view_mode.get() == "After"
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
        if self.traffic_area_drag_start and self.traffic_area_drag_current:
            x1, y1 = self.traffic_area_drag_start
            x2, y2 = self.traffic_area_drag_current
            self.traffic_canvas.create_rectangle(
                x1, y1, x2, y2, outline="#1261a0", width=2,
                dash=(5, 3), fill="", tags=("traffic_area_selection",),
            )
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
            if self.traffic_result and self.traffic_view_mode.get() == "After"
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
        swapped_bays = {
            str(value)
            for row in relocations for value in (row.get("from"), row.get("to"))
            if value
        }
        selected_rows = [
            row for row in relocations
            if highlight_unit and row.get("handling_unit_id") == highlight_unit
        ]
        selected_from = {str(row.get("from")) for row in selected_rows}
        selected_to = {str(row.get("to")) for row in selected_rows}
        racks = self._traffic_racks_for_view()
        positive_rack_visits = [
            rack["visits"] for rack in racks.values() if rack["visits"] > 0
        ]
        rack_heat_maximum = (
            float(np.percentile(positive_rack_visits, 95))
            if positive_rack_visits else 1.0
        )
        show_all_labels = len(racks) <= 60
        for node_id, rack in sorted(racks.items(), key=lambda item: item[1]["label"]):
            node = self.traffic_network.nodes[node_id]
            x, y = self._traffic_point(node, geometry)
            bay = rack["bay"]
            selected_source = bay in selected_from
            selected_destination = bay in selected_to
            swapped = bay in swapped_bays
            visit_ratio = min(1.0, rack["visits"] / rack_heat_maximum)
            fill = (
                self._traffic_heat_colour(visit_ratio)
                if rack["visits"] > 0 else "#f2f5f6"
            )
            outline = "#e07a1f" if selected_source else "#258b55" if selected_destination else "#7b2cbf" if swapped else "#344f5c"
            width = 4 if selected_source or selected_destination else 3 if swapped else 1
            size = 8 if selected_source or selected_destination else 6
            tags = ("traffic_rack", f"traffic_rack:{bay}") + (("swapped_rack",) if swapped else ())
            self.traffic_canvas.create_rectangle(
                x - size, y - size, x + size, y + size,
                fill=fill, outline=outline, width=width, tags=tags,
            )
            if show_all_labels or swapped or selected_source or selected_destination:
                suffix = " FROM" if selected_source else " TO" if selected_destination else ""
                self.traffic_canvas.create_text(
                    x, y - size - 5,
                    text=f"{rack['label']}{suffix} · {rack['visits']:,}",
                    fill="#6a1b83" if swapped else "#344f5c",
                    font=("TkDefaultFont", 7, "bold" if swapped else "normal"),
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
            analysis = self.traffic_result.after if self.traffic_result and self.traffic_view_mode.get() == "After" else self.traffic_analysis
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
        self.ops_search=tk.StringVar(); self.ops_source_sku=tk.StringVar(); self.ops_target_sku=tk.StringVar()
        self.ops_source_label=tk.StringVar(value="Source SKU");self.ops_target_label=tk.StringVar(value="Target SKU")
        self.ops_swap_mode=tk.StringVar(value="SKU slot")
        self.ops_status=tk.StringVar(value="Load a generated slotting layout to begin.")
        self.ops_details=tk.StringVar(value="Search for a SKU to show its current addresses and map position.")
        self.ops_payload=None; self.ops_building=None; self.ops_rows=[]; self.ops_racks=[]; self.ops_highlight_rack=None
        self.ops_shelf_selection=[];self.ops_sku_selection=[];self.ops_inventory_rows={}
        parent.columnconfigure(0,weight=1); parent.rowconfigure(1,weight=1)
        top=ttk.LabelFrame(parent,text="Slotting layout",padding=10); top.grid(row=0,column=0,sticky="ew",padx=12,pady=12); top.columnconfigure(1,weight=1)
        ttk.Label(top,text="Layout JSON").grid(row=0,column=0,sticky="w",padx=(0,8))
        ttk.Entry(top,textvariable=self.ops_layout_path).grid(row=0,column=1,sticky="ew")
        ttk.Button(top,text="Browse…",command=self.browse_ops_layout).grid(row=0,column=2,padx=(8,0))
        ttk.Button(top,text="Load layout",command=self.load_ops_layout).grid(row=0,column=3,padx=(6,0))
        ttk.Button(top,text="Save changes as…",command=self.save_ops_layout).grid(row=0,column=4,padx=(6,0))
        ttk.Label(top,textvariable=self.ops_status,foreground="#315b66").grid(row=1,column=0,columnspan=5,sticky="w",pady=(7,0))

        paned=ttk.Panedwindow(parent,orient="horizontal"); paned.grid(row=1,column=0,sticky="nsew",padx=12,pady=(0,12))
        map_frame=ttk.LabelFrame(paned,text="Current inventory layout",padding=8)
        control=ttk.Frame(paned,padding=8); paned.add(map_frame,weight=3); paned.add(control,weight=2)
        map_frame.columnconfigure(0,weight=1); map_frame.rowconfigure(0,weight=1)
        control.columnconfigure(0,weight=1); control.rowconfigure(2,weight=2); control.rowconfigure(5,weight=1)
        self.ops_canvas=tk.Canvas(map_frame,background="white",highlightthickness=1,highlightbackground="#9aa8ae")
        self.ops_canvas.grid(row=0,column=0,sticky="nsew"); self.ops_canvas.bind("<Configure>",lambda _event:self.draw_ops_layout())
        self.enable_canvas_viewport(self.ops_canvas)
        self.ops_canvas.tag_bind("ops_rack","<Button-1>",self.ops_rack_click)
        ttk.Label(map_frame,text="Rack colour = traffic-aware handling-unit visits (blue low → red high) · highlighted ring = searched SKU position · click a rack to inspect",foreground="#4d646d").grid(row=1,column=0,sticky="w",pady=(5,0))

        search=ttk.LabelFrame(control,text="1. Find SKU",padding=10); search.grid(row=0,column=0,sticky="ew",pady=(0,8)); search.columnconfigure(0,weight=1)
        ttk.Entry(search,textvariable=self.ops_search).grid(row=0,column=0,sticky="ew")
        ttk.Button(search,text="Search",command=self.search_ops_sku).grid(row=0,column=1,padx=(6,0))
        ttk.Label(control,textvariable=self.ops_details,justify="left",wraplength=470).grid(row=1,column=0,sticky="ew",pady=(0,10))

        inventory=ttk.LabelFrame(control,text="SELECTED RACK INVENTORY",padding=6); inventory.grid(row=2,column=0,sticky="nsew",pady=(0,8)); inventory.columnconfigure(0,weight=1); inventory.rowconfigure(0,weight=1)
        columns=("sku","class","flags","static","dynamic","unit")
        self.ops_inventory_tree=ttk.Treeview(inventory,columns=columns,show="headings",height=8)
        headings={"sku":"SKU","class":"ABC","flags":"Storage flags","static":"Static address","dynamic":"Dynamic address","unit":"Shelf / unit"}
        widths={"sku":100,"class":45,"flags":180,"static":190,"dynamic":220,"unit":110}
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
            visits_rebuilt=self.ensure_ops_traffic_demand(payload)
        except (OSError,ValueError,TypeError,json.JSONDecodeError,yaml.YAMLError) as exc:
            messagebox.showerror("Layout load failed",str(exc));return
        self.ops_payload=payload;self.ops_building=payload["building"];self.ops_rows=payload["assignments"];self.ops_racks=racks;self.ops_highlight_rack=None;self.ops_shelf_selection=[];self.ops_sku_selection=[]
        self.ops_source_sku.set("");self.ops_target_sku.set("")
        self.show_ops_rack_inventory(None)
        self.ops_log.delete(0,"end")
        for event in payload.get("operation_log",[]):self.ops_log.insert("end",event.get("message",str(event)))
        demand_status=" · traffic demand rebuilt from orders" if visits_rebuilt else ""
        self.ops_status.set(f"Loaded {len(self.ops_rows):,} SKU assignments · {len(racks)} racks · {workstations} workstations · {unreachable} unreachable racks{demand_status}")
        self.ops_details.set("Search for a SKU or click a rack to inspect current inventory addresses.");self.draw_ops_layout()

    def ensure_ops_traffic_demand(self,payload):
        """Backfill per-unit visits in older traffic layouts from their orders."""
        traffic_analysis=payload.setdefault("traffic_analysis",{})
        if isinstance(traffic_analysis.get("unit_visits"),dict):return False
        if not payload.get("traffic_configuration"):return False
        sources=payload.get("sources",{})
        order_source=sources.get("traffic_order_workbook") or sources.get("affinity_order_workbook")
        if not order_source:return False
        order_path=Path(order_source).expanduser()
        if not order_path.is_file():return False
        configuration=payload.get("traffic_configuration",{})
        try:
            start=date.fromisoformat(configuration["start_date"]) if configuration.get("start_date") else None
            end=date.fromisoformat(configuration["end_date"]) if configuration.get("end_date") else None
            dataset=self.affinity.load_orders(order_path)
            demand=self.traffic.build_demand(dataset,payload.get("assignments",[]),start,end)
        except (OSError,ValueError,TypeError,KeyError):
            return False
        traffic_analysis["unit_visits"]=dict(sorted(demand.unit_visits.items()))
        return True

    def save_ops_layout(self):
        if not self.ops_payload:
            messagebox.showinfo("Load layout","Load a slotting layout first.");return
        path=filedialog.asksaveasfilename(defaultextension=".slotting.json",filetypes=[("Slotting layout","*.slotting.json"),("JSON","*.json")],initialdir=str(Path(self.ops_layout_path.get()).expanduser().parent),initialfile=Path(self.ops_layout_path.get()).name)
        if not path:return
        self.ops_payload["assignments"]=self.ops_rows;self.ops_payload["modified_at"]=datetime.now(timezone.utc).isoformat()
        self.layouts.save_payload(self.ops_payload,Path(path));self.ops_layout_path.set(path);self.ops_status.set(f"Saved modified layout: {path}")

    def ops_geometry(self,vertices):
        xs=[float(v[0]) for v in vertices];ys=[float(v[1]) for v in vertices];min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
        width=max(300,self.ops_canvas.winfo_width());height=max(300,self.ops_canvas.winfo_height());padding=28
        scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y));return min_x,max_x,min_y,max_y,width,height,padding,scale

    def ops_screen_point(self,x,y,geometry):
        min_x,max_x,min_y,max_y,width,height,padding,scale=geometry;sx=padding+(float(x)-min_x)*scale
        sy=padding+(float(y)-min_y)*scale if self.ops_building.get("coordinate_system")=="reference_image" else height-padding-(float(y)-min_y)*scale
        return sx,sy

    @staticmethod
    def ops_rack_handling_unit_visits(rows, unit_visits):
        """Return traffic demand once per distinct handling unit in a rack."""
        units={str(row.get("handling_unit_id") or "") for row in rows}
        return sum(max(0,int(unit_visits.get(unit,0) or 0)) for unit in units if unit)

    def draw_ops_frequency_legend(self, maximum_frequency, available=True):
        """Draw the traffic-aware handling-unit visit scale."""
        x,y,swatch_width,swatch_height=12,12,34,10
        self.ops_canvas.create_rectangle(x-5,y-5,x+5*swatch_width+5,y+44,fill="white",outline="#9aa8ae",tags=("ops_legend",))
        self.ops_canvas.create_text(x,y,text="Handling-unit visits",anchor="nw",fill="#314d59",font=("TkDefaultFont",8,"bold"),tags=("ops_legend",))
        y+=17
        if not available:
            self.ops_canvas.create_text(x,y,text="Unavailable in this layout",anchor="nw",fill="#6d7f87",font=("TkDefaultFont",8),tags=("ops_legend",))
            return
        steps=5
        for index in range(steps):
            ratio=index/(steps-1)
            left=x+index*swatch_width
            self.ops_canvas.create_rectangle(left,y,left+swatch_width,y+swatch_height,fill=self._traffic_heat_colour(ratio),outline="",tags=("ops_legend",))
        self.ops_canvas.create_text(x,y+14,text="0",anchor="nw",fill="#314d59",font=("TkDefaultFont",7),tags=("ops_legend",))
        self.ops_canvas.create_text(x+steps*swatch_width,y+14,text=f"{maximum_frequency:g} visits",anchor="ne",fill="#314d59",font=("TkDefaultFont",7),tags=("ops_legend",))

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
        grouped={}
        for row in self.ops_rows:
            if row.get("assignment_status")=="ASSIGNED":grouped.setdefault(row.get("rack_id",""),[]).append(row)
        unit_visits=(self.ops_payload or {}).get("traffic_analysis",{}).get("unit_visits")
        demand_available=isinstance(unit_visits,dict)
        unit_visits=unit_visits if demand_available else {}
        rack_frequencies={
            rack_id:self.ops_rack_handling_unit_visits(rows,unit_visits)
            for rack_id,rows in grouped.items()
        }
        maximum_frequency=max(rack_frequencies.values(),default=0.0)
        shelf_racks={selection["rack_id"] for selection in self.ops_shelf_selection}
        for rack in self.ops_racks:
            x,y=self.ops_screen_point(rack["x"],rack["y"],geometry);rows=grouped.get(rack["rack_id"],[])
            frequency=rack_frequencies.get(rack["rack_id"],0.0)
            fill=self._traffic_heat_colour(frequency/maximum_frequency) if demand_available and frequency and maximum_frequency else "#aeb8bc"
            shelf_selected=rack["rack_id"] in shelf_racks;selected=rack["rack_id"]==self.ops_highlight_rack;radius=11 if shelf_selected else (9 if selected else 4)
            outline="#e07a1f" if shelf_selected else ("#087f8c" if selected else "white")
            self.ops_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline=outline,width=4 if shelf_selected else (3 if selected else 1),tags=("ops_rack",f"opsrack:{rack['rack_id']}"))
            if selected:
                self.ops_canvas.create_text(x,y-16,text=rack["rack_id"],fill="#065f69",font=("TkDefaultFont",9,"bold"))
        self.draw_ops_frequency_legend(maximum_frequency,demand_available)
        self.apply_canvas_viewport(self.ops_canvas)

    def show_ops_rack_inventory(self,rack_id):
        if not hasattr(self,"ops_inventory_tree"):return
        self.ops_inventory_rows={}
        self.ops_inventory_tree.delete(*self.ops_inventory_tree.get_children())
        if not rack_id:return
        rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
        rows.sort(key=lambda row:(int(row.get("storage_level") or 0),int(row.get("storage_slot") or 0),str(row.get("sku",""))))
        for row in rows:
            item=self.ops_inventory_tree.insert("","end",values=(row.get("sku",""),row.get("velocity_class",""),self.sku_storage_flags(row),row.get("static_address",""),row.get("dynamic_address",""),row.get("handling_unit_id","")))
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
            f"Current static address: {row.get('static_address','')}\nCurrent dynamic address: {row.get('dynamic_address','')}\n"
            f"Handling unit: {row.get('handling_unit_id','')} ({row.get('handling_unit_type','')})\nRMF grid position: {row.get('rmf_grid_address','')}\n"
            f"SKU requirements: {self.attributes.format_values(row.get('sku_requirements'))}\n"
            f"Effective location attributes: {self.attributes.format_values(effective)}\n"
            f"Compatibility: {row.get('compatibility_status','NOT_EVALUATED')}\n"
            f"Warnings / mismatch: {'; '.join(row.get('compatibility_issues', [])) or 'none'}"
        );self.draw_ops_layout()

    def search_ops_sku(self):
        if not self.ops_rows:
            messagebox.showinfo("Load layout","Load a slotting layout first.");return
        try:row=self.inventory.find_sku(self.ops_rows,self.ops_search.get())
        except ValueError as exc:messagebox.showerror("SKU search",str(exc));return
        self.show_ops_assignment(row)
        if self.ops_swap_mode.get()=="SKU slot":self.select_ops_sku_for_swap(row)
        else:self.ops_status.set(f"Located SKU {row['sku']} at {row['static_address']}")

    def ops_rack_click(self,event):
        item=self.ops_canvas.find_withtag("current")
        if not item:return
        tags=self.ops_canvas.gettags(item[0]);found=[tag.split(":",1)[1] for tag in tags if tag.startswith("opsrack:")]
        if not found:return
        rack_id=found[0];rows=[row for row in self.ops_rows if row.get("assignment_status")=="ASSIGNED" and row.get("rack_id")==rack_id]
        self.ops_highlight_rack=rack_id
        self.show_ops_rack_inventory(rack_id)
        units=sorted({str(row.get("handling_unit_id","")) for row in rows if row.get("handling_unit_id")})
        rack_attributes = {}
        if rows and self.ops_payload:
            bay_path = "/".join(str(rows[0].get("static_address", "")).split("/")[:3])
            rack_attributes, _sources = self.attributes.effective_attributes(
                bay_path, self.ops_payload.get("location_attributes", {})
            )
        chilled_count, exception_count = self.rack_storage_flag_counts(rows)
        self.ops_details.set(
            f"Rack {rack_id} · {len(rows)} assigned SKU(s)\nShelf / handling unit: "
            + (", ".join(units) if units else "empty")
            + f"\nStorage flags: CHILLED={chilled_count} · OVERSIZE / WEIGHT EXCEPTION={exception_count}"
            + f"\nEffective bay attributes: {self.attributes.format_values(rack_attributes)}"
        )
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
            raise ValueError("load the building YAML before editing attributes")
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
        self.ensure_zone_storage_settings()
        paths = self.attributes.hierarchy_paths(self.slot_racks, levels, slots)
        orphaned = sorted(set(self.slot_location_attributes) - set(paths))
        if orphaned:
            if confirm_orphans and not messagebox.askyesno(
                "Hierarchy changed",
                f"{len(orphaned)} attribute path(s) no longer exist after the zone or "
                "capacity change. Discard those local values?",
            ):
                raise ValueError("attribute update cancelled; restore the previous hierarchy")
            self.slot_location_attributes = {
                path: values for path, values in self.slot_location_attributes.items()
                if path in paths
            }
        self.slot_hierarchy_paths = paths
        return paths

    def ensure_zone_storage_settings(self):
        starter = self.attributes.starter_catalog()
        for key, definition in starter.items():
            self.slot_attribute_catalog.setdefault(key, definition)
        zones = sorted({rack["zone_id"] for rack in self.slot_racks})
        for zone in zones:
            values = self.slot_location_attributes.setdefault(zone, {})
            for key, value in STANDARD_STORAGE_DEFAULTS.items():
                values.setdefault(key, value)
            values.setdefault("chilled", False)
            values.setdefault(OVERSIZE_CAPABLE_KEY, False)
        self.slot_storage_initialized = True

    def open_zone_storage_settings(self):
        try:
            self.prepare_slot_attribute_hierarchy()
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Zone storage settings", str(exc)); return
        editor = ZoneStorageSettingsEditor(
            self.root,
            self.attributes,
            [path for path in self.slot_hierarchy_paths if "/" not in path],
            self.slot_location_attributes,
            self.apply_zone_storage_settings,
        )
        editor.grab_set()

    def apply_zone_storage_settings(self, location_attributes):
        self.slot_location_attributes = dict(location_attributes)
        unverified_note = (
            " Missing physical data will remain visibly unverified."
        )
        self.slot_summary.set(
            f"Saved storage settings for {len([p for p in location_attributes if '/' not in p])} zone(s)."
            + unverified_note
        )

    def open_attribute_editor(self):
        try:
            paths = self.prepare_slot_attribute_hierarchy()
        except (TypeError, ValueError) as exc:
            messagebox.showerror("Hierarchy attributes", str(exc)); return
        editor = HierarchyAttributeEditor(
            self.root,
            self.attributes,
            self.slot_attribute_catalog,
            self.slot_location_attributes,
            paths,
            self.apply_slot_attributes,
        )
        editor.grab_set()

    def apply_slot_attributes(self, catalog, location_attributes):
        self.slot_attribute_catalog = dict(catalog)
        self.slot_location_attributes = dict(location_attributes)
        self.slot_summary.set(
            f"Saved {len(self.slot_attribute_catalog)} attribute definition(s) and "
            f"{len(self.slot_location_attributes)} node(s) with local values."
        )

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
        source_chilled = payload.get("sources", {}).get("chilled_requirements_csv", "")
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
        self.slot_attribute_catalog = catalog or self.attributes.starter_catalog()
        self.slot_location_attributes = local
        self.slot_hierarchy_paths = paths
        self.slot_storage_initialized = any(
            "/" not in path
            and all(key in values for key in PHYSICAL_ATTRIBUTE_KEYS)
            for path, values in local.items()
        )
        self.slot_rows = payload.get("assignments", [])
        self.slot_zone_storage_types = payload.get("summary", {}).get(
            "zone_storage_types", {}
        )
        self.slot_output_path.set(str(layout_path))
        self.slot_selected_rack = None; self.slot_zone_mode.set(True)
        self.show_slotting_rows(self.slot_rows); self.draw_slotting_layout()
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

    def load_slot_building(self):
        try:
            path=Path(self.slot_building_path.get()).expanduser().resolve()
            project=self.rmf_maps.load_project(path)
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
        except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
            messagebox.showerror("Grid project load failed",str(exc)); return
        self.slot_building=building; self.slot_grid_project=project; self.slot_racks=racks; self.slot_loaded_path=path
        self.slot_handling_unit.set(project.storage_layout.handling_unit_type)
        self.slot_levels.set(str(project.storage_layout.levels_per_rack))
        self.slot_slots.set(str(project.storage_layout.slots_per_level))
        self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None
        self.slot_attribute_catalog=self.attributes.starter_catalog(); self.slot_location_attributes={}; self.slot_hierarchy_paths=[]
        self.slot_storage_initialized=False
        self.slot_zone_storage_types={}
        self.slot_zone.set("Z01")
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None; self.slot_zone_mode.set(True)
        self.show_slotting_rows([]); self.draw_slotting_layout()
        self.slot_summary.set(
            f"Loaded {len(racks)} racks, {len(project.storage_layout.buffers)} empty "
            f"{project.storage_layout.buffer_level} buffers and {workstations} "
            f"workstations · {unreachable} unreachable · assign zones by dragging rectangles"
        )
        self.slot_zone_detail.set(
            "Zone grouping mode is active. Enter a zone ID and drag a rectangle over a group of racks."
        )
        self.slot_rack_detail.set("Rack details will appear after slotting is generated.")
        self.slot_viewer_status.set(
            "Project loaded. Assign zones and generate a layout to view results."
        )

    def zone_mode_changed(self):
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
        self.draw_slotting_layout()

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

    def clear_slot_zones(self):
        if not self.slot_building: return
        self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None; self.slot_zone_mode.set(True)
        self.slot_zone_storage_types={}
        self.slot_zone.set("Z01")
        self.show_slotting_rows([]); self.draw_slotting_layout()
        self.slot_summary.set(f"Cleared zone assignments for {len(self.slot_racks)} racks.")
        self.slot_viewer_status.set(
            "Zones cleared. Generate a layout to refresh the result viewer."
        )

    def slot_canvas_press(self,event):
        if not self.slot_building or not self.slot_zone_mode.get(): return
        self.slot_zone_drag_start=(event.x,event.y); self.slot_zone_drag_current=(event.x,event.y)
        self.draw_slotting_layout()

    def slot_canvas_drag(self,event):
        if self.slot_zone_drag_start is None or not self.slot_zone_mode.get(): return
        self.slot_zone_drag_current=(event.x,event.y); self.draw_slotting_layout()

    def slot_canvas_release(self,event):
        if self.slot_zone_drag_start is None or not self.slot_zone_mode.get(): return
        self.slot_zone_drag_current=(event.x,event.y)
        zone=self.slot_zone.get().strip()
        if not zone:
            self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
            messagebox.showerror("Zone ID","Enter a zone ID before selecting racks."); return
        x1,y1=self.slot_zone_drag_start; x2,y2=self.slot_zone_drag_current
        left,right=sorted((x1,x2)); top,bottom=sorted((y1,y2))
        _,level=next(iter(self.slot_building["levels"].items())); geometry=self.slotting_geometry(level["vertices"], self.slot_zone_canvas)
        selected=[]
        for rack in self.slot_racks:
            x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry)
            x,y=self.canvas_viewport_point(self.slot_zone_canvas,x,y)
            if left-6<=x<=right+6 and top-6<=y<=bottom+6:
                self.slot_zone_assignments[rack["waypoint"]]=zone; rack["zone_id"]=zone; selected.append(rack)
        self.slotting.apply_zone_local_aisles(self.slot_building,self.slot_racks,self.slot_zone_assignments,"UNASSIGNED")
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
        zone_counts={}
        for value in self.slot_zone_assignments.values(): zone_counts[value]=zone_counts.get(value,0)+1
        remaining=len(self.slot_racks)-len(self.slot_zone_assignments)
        if selected:
            self.slot_rows=[]; self.slot_zone_storage_types={}; self.slot_selected_rack=None
            self.show_slotting_rows([])
            self.slot_viewer_status.set(
                "Zone plan changed. Generate again to refresh the result viewer."
            )
        if selected and self.slot_zone_auto.get(): self.slot_zone.set(self.slotting.next_zone_id(zone))
        self.slot_summary.set(f"Assigned {len(selected)} rack(s) to {zone} · zones: "+", ".join(f"{key}={value}" for key,value in sorted(zone_counts.items()))+f" · {remaining} unassigned · next ID {self.slot_zone.get()}")
        self.slot_zone_detail.set(
            f"Zone {zone}: selected {len(selected)} rack(s). The next rectangle will use {self.slot_zone.get()}."
        )
        self.slot_rack_detail.set("Rack details will appear after slotting is generated.")
        self.draw_slotting_layout()

    def run_slotting(self, use_adjusted=False):
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
            self.prepare_slot_attribute_hierarchy()
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
                    percent = 100.0 * current / max(1, total)
                    self.slot_summary.set(f"{message} · {percent:.0f}%")
                    self.root.update_idletasks()

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
                self.root.update_idletasks()
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
                rows, summary = self.slotting.generate_basic(
                    building, skus, levels, slots, self.slot_handling_unit.get(),
                    self.slot_zone.get(), self.slot_zone_assignments,
                    self.slot_attribute_catalog, self.slot_location_attributes,
                    storage_layout=self.slot_grid_project.storage_layout,
                )
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
            messagebox.showerror("Slotting generation failed", str(exc)); return
        self.slot_rows = rows
        self.slot_zone_storage_types = summary["zone_storage_types"]
        self.slotting.apply_zone_local_aisles(building,self.slot_racks,self.slot_zone_assignments,self.slot_zone.get())
        self.slot_selected_rack = None
        self.slot_zone_mode.set(True)
        self.show_slotting_rows(rows)
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
        self.slot_summary.set(
            f"Assigned {summary['assigned_count']:,}/{summary['sku_count']:,} SKUs · "
            f"unassigned {summary['unassigned_count']:,} · "
            f"temperature-zone shortage "
            f"{summary['unassigned_status_counts'].get('UNASSIGNED_NO_CHILLED_LOCATION', 0) + summary['unassigned_status_counts'].get('UNASSIGNED_NO_AMBIENT_LOCATION', 0):,} · "
            f"all slots occupied {summary['unassigned_no_capacity_count']:,} · "
            f"planned oversize segments {summary['auto_planned_oversize_segment_count']:,} · "
            f"{summary['rack_count']} racks ({summary['unreachable_rack_count']} unreachable) · "
            f"{summary['workstation_count']} workstations · {summary['zone_count']} zones · capacity {summary['capacity']:,} · "
            f"buffers occupied {summary['occupied_buffer_count']:,}/{summary['buffer_count']:,} "
            f"({summary['buffer_occupancy_rate'] * 100:.1f}%) · "
            f"unverified physical data {summary['unverified_oversize_count']:,} "
            f"({summary['assigned_unverified_count']:,} assigned with warning) · "
            f"auto slot overrides {summary['auto_overridden_slot_count']:,} · "
            f"generated zone types "
            + ", ".join(
                f"{zone}={storage_type}"
                for zone, storage_type in summary["zone_storage_types"].items()
            )
            + (f" · {affinity_summary}" if affinity_summary else "")
            + " · "
            f"saved to {self.slot_output_path.get()}"
        )

    def show_slotting_rows(self, rows):
        self.slot_tree.delete(*self.slot_tree.get_children())
        for row in rows[:1000]:
            self.slot_tree.insert("", "end", values=(row["sku_rank"], row["sku"], row["velocity_class"], self.sku_storage_flags(row), row["static_address"], row["dynamic_address"], row["handling_unit_type"], row["handling_unit_id"], row["assignment_status"]))

    def slotting_geometry(self, vertices, canvas=None):
        canvas = canvas or self.slot_canvas
        xs=[float(v[0]) for v in vertices]; ys=[float(v[1]) for v in vertices]
        min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
        width=max(300,canvas.winfo_width()); height=max(300,canvas.winfo_height()); padding=28
        scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y))
        return min_x,max_x,min_y,max_y,width,height,padding,scale

    def slotting_screen_point(self, x, y, geometry):
        min_x,max_x,min_y,max_y,width,height,padding,scale=geometry
        screen_x=padding+(float(x)-min_x)*scale
        if self.slot_building.get("coordinate_system")=="reference_image": screen_y=padding+(float(y)-min_y)*scale
        else: screen_y=height-padding-(float(y)-min_y)*scale
        return screen_x,screen_y

    def draw_slotting_layout(self):
        if hasattr(self, "slot_zone_canvas"):
            self._draw_slotting_canvas(
                self.slot_zone_canvas, zone_view=True, show_drag=True
            )
        if hasattr(self, "slot_canvas"):
            self._draw_slotting_canvas(
                self.slot_canvas, zone_view=False, show_drag=False
            )

    def _draw_slotting_canvas(self, canvas, *, zone_view, show_drag):
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
        assignments={}
        for row in self.slot_rows:
            if row["assignment_status"]=="ASSIGNED": assignments.setdefault(row["rack_id"],[]).append(row)
        class_colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
        zone_palette=("#6c8cd5","#9b70c7","#31a6a0","#d47b4c","#8ca63c","#c75d8b","#81756e","#3d8fbe")
        zones=sorted(set(self.slot_zone_assignments.values()))
        zone_colors={zone:zone_palette[index%len(zone_palette)] for index,zone in enumerate(zones)}
        for rack in self.slot_racks:
            x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry); rack_rows=assignments.get(rack["rack_id"],[])
            if zone_view: fill=zone_colors.get(self.slot_zone_assignments.get(rack["waypoint"],""),"#aeb8bc")
            else:
                rack_class = rack_rows[0].get("rack_velocity_class", "") if rack_rows else ""
                fill=class_colors.get(rack_class,"#7b8b92")
            radius=7 if rack["rack_id"]==self.slot_selected_rack else 4
            canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline="#087f8c" if radius==7 else "white",width=3 if radius==7 else 1,tags=("rack",f"rack:{rack['rack_id']}"))
        for index,vertex in enumerate(vertices):
            params=vertex[4] if len(vertex)>4 and isinstance(vertex[4],dict) else {}
            if "dropoff_ingestor" not in params: continue
            endpoint=str(self.slotting.typed_value(params["dropoff_ingestor"],vertex[3])); x,y=self.slotting_screen_point(vertex[0],vertex[1],geometry); r=6
            canvas.create_polygon(x,y-r,x+r,y,x,y+r,x-r,y,fill="#277da1",outline="white")
            canvas.create_text(x,y-11,text=endpoint,fill="#1d5d78",font=("TkDefaultFont",8,"bold"))
        self.apply_canvas_viewport(canvas)
        if show_drag and self.slot_zone_drag_start is not None and self.slot_zone_drag_current is not None:
            x1,y1=self.slot_zone_drag_start; x2,y2=self.slot_zone_drag_current
            canvas.create_rectangle(x1,y1,x2,y2,outline="#7b2cbf",width=2,dash=(5,3))
        if zone_view:
            counts={zone:sum(value==zone for value in self.slot_zone_assignments.values()) for zone in zones}
            self.slot_legend.set("Zone grouping · drag rectangle · "+(" · ".join(f"{zone}: {counts[zone]} racks" for zone in zones) if zones else "all racks unassigned"))

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
        display_zone = (
            planned_zone
            if planned_zone != zone_path and "_chill_" in planned_zone
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
        parent_line = (
            f"Parent zone: {zone_path}\n"
            if display_zone != zone_path
            else ""
        )
        return (
            f"Zone: {display_zone}\n"
            f"{parent_line}"
            f"Generated storage type: {planned_type or 'UNUSED'}\n"
            f"Attributes: {self.attributes.format_values(attributes_by_key)}"
        )

    def slot_rack_click(self, event):
        item=self.slot_canvas.find_withtag("current")
        if not item: return
        tags=self.slot_canvas.gettags(item[0]); rack_tags=[tag for tag in tags if tag.startswith("rack:")]
        if not rack_tags: return
        rack_id=rack_tags[0].split(":",1)[1]
        self.slot_selected_rack=rack_id
        rack=next((item for item in self.slot_racks if item["rack_id"]==rack_id),None)
        rows=[row for row in self.slot_rows if row["rack_id"]==rack_id and row["assignment_status"]=="ASSIGNED"]
        self.show_slotting_rows(rows); self.draw_slotting_layout()
        if not rack: return
        classes={label:sum(row["velocity_class"]==label for row in rows) for label in ("A","B","C")}
        unit_ids=sorted({row["handling_unit_id"] for row in rows})
        zone_path = rack.get("zone_id", "UNASSIGNED")
        bay_path = f"{rack.get('zone_id','UNASSIGNED')}/{rack['aisle_id']}/{rack['static_bay_id']}"
        _zone_detail, rack_override_detail = self.rack_attribute_detail_text(
            zone_path, bay_path
        )
        chilled_count, exception_count = self.rack_storage_flag_counts(rows)
        rack_class = rows[0].get("rack_velocity_class", "") if rows else ""
        rack_frequency_rank = rows[0].get("rack_frequency_rank", "") if rows else ""
        rack_pick_frequency = rows[0].get("rack_pick_frequency", "") if rows else ""
        self.slot_zone_detail.set(self.planned_zone_detail_text(zone_path, rows))
        self.slot_rack_detail.set(
            f"Static grid rack: {rack_id}\nPickup dispenser: {rack['pickup_dispenser_id']} · vertex {rack['vertex_index']}\n"
            f"Current static buffer address: {rows[0]['static_address'] if rows else rack.get('zone_id','UNASSIGNED')+'/'+rack['aisle_id']+'/'+rack['static_bay_id']}\n"
            f"Rack ABC: {rack_class or 'unassigned'} · frequency rank {rack_frequency_rank or 'n/a'} · picks {rack_pick_frequency or 0}\n"
            f"Assigned SKUs: {len(rows)} · A {classes['A']} / B {classes['B']} / C {classes['C']}\n"
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

    def generate_grid(self):
        try:
            spec = self.spec_from_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid grid", str(exc)); return
        if self.project.markers and not messagebox.askyesno("Reset grid", "Generating a new grid removes all rack and workstation markers. Continue?"):
            return
        self.push_undo()
        self.project = GridProject(spec)
        self.sync_grid_storage_controls()
        self.selected = None
        self.bulk_anchor = None
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
        self.grid_buffer_summary.set(
            f"{len(layout.buffers)} empty {layout.buffer_level} buffer(s) · "
            f"dynamic unit {layout.handling_unit_type}"
        )

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
            self.push_undo()
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

    def geometry(self):
        width = max(200, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        padding = 45
        scale = min((width - 2 * padding) / self.project.grid.width_m, (height - 2 * padding) / self.project.grid.length_m)
        return padding, scale, height

    def screen_point(self, column, row):
        padding, scale, height = self.geometry()
        x = padding + self.project.grid.x_coordinate(column) * scale
        y = height - padding - self.project.grid.y_coordinate(row) * scale
        return x, y

    def redraw(self):
        if not hasattr(self, "canvas"): return
        self.canvas.delete("all")
        spec = self.project.grid
        for row in range(spec.rows + 1):
            x1, y1 = self.screen_point(0, row); x2, y2 = self.screen_point(spec.columns, row)
            self.canvas.create_line(x1, y1, x2, y2, fill="#d7dfe2")
        for column in range(spec.columns + 1):
            x1, y1 = self.screen_point(column, 0); x2, y2 = self.screen_point(column, spec.rows)
            self.canvas.create_line(x1, y1, x2, y2, fill="#d7dfe2")
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
        for column, row in self.project.iter_positions():
            x, y = self.screen_point(column, row)
            marker = self.project.markers.get((column, row))
            fill = "#d1495b" if marker and marker.role == "workstation" else "#f3a712" if marker else "#50656e"
            r = radius + 3 if self.selected == (column, row) else radius
            outline = (
                "#087f8c" if self.selected == (column, row)
                else "#7b2cbf" if (column, row) in buffer_positions
                else fill
            )
            self.canvas.create_oval(x-r, y-r, x+r, y+r, fill=fill, outline=outline, width=3 if self.selected == (column, row) else 1)
            if marker:
                self.canvas.create_text(x, y-12, text=marker.endpoint_id, fill=fill, font=("TkDefaultFont", 8, "bold"))
        if self.bulk_anchor is not None:
            x, y = self.screen_point(*self.bulk_anchor)
            self.canvas.create_oval(x-9, y-9, x+9, y+9, outline="#7b2cbf", width=3)
        x0, y0 = self.screen_point(0, 0)
        self.canvas.create_text(x0, y0+20, text="(0, 0)", anchor="n", fill="#087f8c", font=("TkDefaultFont", 9, "bold"))
        self.apply_canvas_viewport(self.canvas)
        self.summary.set(f"{spec.columns} columns × {spec.rows} rows\n{spec.vertex_count:,} vertices · {spec.edge_count:,} edges")

    def nearest_position(self, event) -> GridPosition | None:
        padding, scale, height = self.geometry()
        event_x, event_y = self.canvas_viewport_inverse_point(
            self.canvas, event.x, event.y
        )
        physical_x = (event_x - padding) / scale
        physical_y = (height - padding - event_y) / scale
        column = min(
            range(self.project.grid.columns + 1),
            key=lambda value: abs(self.project.grid.x_coordinate(value) - physical_x),
        )
        row = min(
            range(self.project.grid.rows + 1),
            key=lambda value: abs(self.project.grid.y_coordinate(value) - physical_y),
        )
        if 0 <= column <= self.project.grid.columns and 0 <= row <= self.project.grid.rows:
            x, y = self.screen_point(column, row)
            if math.hypot(event_x-x, event_y-y) <= max(
                12,
                min(self.project.grid.spacing_m, self.project.grid.spacing_y_m)
                * scale * .35,
            ):
                return column, row
        return None

    def canvas_click(self, event):
        position = self.nearest_position(event)
        if position is None: return
        action = self.tool.get()
        if action == "clear":
            self.push_undo(); self.drag_undo_started = True
            self.invalidate_grid_buffers()
            self.project.markers.pop(position, None)
        elif action == "rack":
            self.push_undo(); self.drag_undo_started = True
            self.place_rack(position)
        elif action == "rack_rectangle":
            if self.bulk_anchor is None:
                self.bulk_anchor = position
                self.status.set(f"Rack rectangle starts at {position}. Click the opposite corner.")
            else:
                self.push_undo()
                c1, r1 = self.bulk_anchor; c2, r2 = position
                count = 0
                for row in range(min(r1,r2), max(r1,r2)+1):
                    for column in range(min(c1,c2), max(c1,c2)+1):
                        self.place_rack((column,row), redraw=False); count += 1
                self.bulk_anchor = None
                self.status.set(f"Placed {count} rack pickup points.")
        elif action == "workstation":
            self.push_undo()
            self.invalidate_grid_buffers()
            self.project.markers[position] = Marker("workstation", f"WS_{position[0]}_{position[1]}")
        self.selected = position
        self.update_selected_editor()
        self.redraw()

    def canvas_drag(self, event):
        action = self.tool.get()
        if action not in {"rack", "clear"}: return
        position = self.nearest_position(event)
        if position is None or position == self.selected: return
        if not self.drag_undo_started:
            self.push_undo(); self.drag_undo_started = True
        if action == "rack": self.place_rack(position, redraw=False)
        else:
            self.invalidate_grid_buffers()
            self.project.markers.pop(position, None)
        self.selected = position
        self.update_selected_editor()
        self.redraw()

    def canvas_release(self, _event):
        self.drag_undo_started = False

    def place_rack(self, position: GridPosition, redraw=True):
        prefix = self.rack_prefix.get().strip() or "RACK"
        self.invalidate_grid_buffers()
        self.project.markers[position] = Marker("rack", f"{prefix}_{position[0]}_{position[1]}")
        if redraw: self.redraw()

    def snapshot(self) -> dict:
        return self.project.to_project_dict()

    def restore_snapshot(self, snapshot: dict):
        self.project = GridProject.from_project_dict(snapshot)
        self.selected = None
        self.bulk_anchor = None
        self.map_name.set(self.project.grid.map_name)
        self.level_name.set(self.project.grid.level_name)
        self.width.set(str(self.project.grid.width_m))
        self.length.set(str(self.project.grid.length_m))
        self.spacing.set(str(self.project.grid.spacing_m))
        self.spacing_y.set(str(self.project.grid.spacing_y_m))
        self.sync_grid_storage_controls()
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
            self.role.set("none"); self.endpoint_id.set(""); return
        column, row = self.selected
        self.selected_coordinate.set(
            f"Column {column}, row {row}  →  "
            f"({self.project.grid.x_coordinate(column):g}, "
            f"{self.project.grid.y_coordinate(row):g}) m"
        )
        marker = self.project.markers.get(self.selected)
        self.role.set(marker.role if marker else "none")
        self.endpoint_id.set(marker.endpoint_id if marker else "")

    def apply_edit(self):
        if self.selected is None:
            messagebox.showinfo("Select a point", "Select a grid point first."); return
        role = self.role.get()
        before = self.snapshot()
        self.invalidate_grid_buffers()
        if role == "none": self.project.markers.pop(self.selected, None)
        else:
            endpoint = self.endpoint_id.get().strip()
            if not endpoint:
                self.restore_snapshot(before)
                messagebox.showerror("Endpoint ID", "Endpoint ID cannot be blank."); return
            self.project.markers[self.selected] = Marker(role, endpoint)
        try: self.project.validate()
        except ValueError as exc:
            self.restore_snapshot(before); messagebox.showerror("Invalid edit", str(exc)); return
        self.undo_stack.append(before)
        if len(self.undo_stack) > 100: self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.redraw(); self.status.set("Point edit applied.")

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
            self.update_selected_editor(); self.redraw(); self.status.set(f"Project loaded: {path}")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc: messagebox.showerror("Load failed", str(exc))

    def export_yaml_dialog(self):
        path = filedialog.asksaveasfilename(defaultextension=".building.yaml", filetypes=[("RMF building map", "*.building.yaml"), ("YAML", "*.yaml")], initialdir=str(DEFAULT_BUILDING_OUTPUT.parent), initialfile=DEFAULT_BUILDING_OUTPUT.name)
        if path:
            try:
                self.project.validate(); self.rmf_maps.export_building(self.project, Path(path))
                self.status.set(f"RMF map exported: {path}")
                messagebox.showinfo("Export complete", f"Generated {self.project.grid.vertex_count:,} vertices and {self.project.grid.edge_count:,} bidirectional edges.\n\n{path}")
            except (OSError, ValueError) as exc: messagebox.showerror("Export failed", str(exc))


def run_gui(initial_project: GridProject | None = None) -> None:
    root = tk.Tk()
    GridMapEditorApp(root, initial_project)
    root.mainloop()
