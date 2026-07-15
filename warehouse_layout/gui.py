"""Tkinter user interface for map editing, slotting, and inventory demos."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

from .attribute_editor import HierarchyAttributeEditor
from .attributes import (
    PHYSICAL_ATTRIBUTE_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)
from .config import (
    DEFAULT_BUILDING_INPUT,
    DEFAULT_BUILDING_OUTPUT,
    DEFAULT_CHILLED_INPUT,
    DEFAULT_SLOTTING_OUTPUT,
    DEFAULT_VELOCITY_INPUT,
)
from .domain import GridPosition, GridProject, GridSpec, Marker
from .inventory import InventoryService
from .rmf import RmfMapService
from .slotting import SlottingLayoutRepository, SlottingService
from .zone_settings_editor import ZoneStorageSettingsEditor


class GridMapEditorApp:
    """Coordinate the three-tab desktop UI and application services."""

    @staticmethod
    def sku_storage_flags(row):
        """Return compact, operator-facing storage markers for an SKU row."""
        flags = []
        requirements = row.get("sku_requirements") or {}
        if requirements.get("chilled") is True:
            flags.append("CHILLED")

        physical_class = str(row.get("physical_storage_class") or "").upper()
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
        self.slotting = SlottingService(self.rmf_maps, self.attributes)
        self.layouts = SlottingLayoutRepository()
        self.inventory = InventoryService(self.slotting, self.attributes)
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
        self.selected_coordinate = tk.StringVar(value="No grid point selected")
        self.role = tk.StringVar(value="none")
        self.endpoint_id = tk.StringVar()
        self.summary = tk.StringVar()
        self.status = tk.StringVar(value="Bottom-left grid point is (0, 0)")
        self._build_ui()
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        self.root.bind_all("<Control-Shift-Z>", self.redo)
        self.redraw()

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")
        map_tab = ttk.Frame(notebook)
        slotting_tab = ttk.Frame(notebook)
        operations_tab = ttk.Frame(notebook)
        notebook.add(map_tab, text="Grid Map Editor")
        notebook.add(slotting_tab, text="Inventory Slotting")
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
            ("Distance per grid (m)", self.spacing),
        ]
        for row, (label, variable) in enumerate(fields, start=1):
            ttk.Label(left, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(left, textvariable=variable, width=19).grid(row=row, column=1, sticky="ew", pady=3)
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
        ttk.Button(left, text="Save editable project…", command=self.save_project_dialog).grid(row=23, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="Load editable project…", command=self.load_project_dialog).grid(row=24, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="Export RMF building YAML…", command=self.export_yaml_dialog).grid(row=25, column=0, columnspan=2, sticky="ew", pady=(10, 3))

        self.canvas = tk.Canvas(canvas_frame, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Button-1>", self.canvas_click)
        self.canvas.bind("<B1-Motion>", self.canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        ttk.Label(canvas_frame, textvariable=self.status).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._build_slotting_tab(slotting_tab)
        self._build_operations_tab(operations_tab)

    def _build_slotting_tab(self, parent):
        self.slot_building_path = tk.StringVar(value=str(DEFAULT_BUILDING_INPUT))
        self.slot_velocity_path = tk.StringVar(value=str(DEFAULT_VELOCITY_INPUT))
        self.slot_chilled_path = tk.StringVar(value=str(DEFAULT_CHILLED_INPUT))
        self.slot_output_path = tk.StringVar(value=str(DEFAULT_SLOTTING_OUTPUT))
        self.slot_strategy = tk.StringVar(value="basic")
        self.slot_handling_unit = tk.StringVar(value="AMR shelf")
        self.slot_zone = tk.StringVar(value="Z01")
        self.slot_levels = tk.StringVar(value="1")
        self.slot_slots = tk.StringVar(value="6")
        self.slot_summary = tk.StringVar(value="Choose the inputs and generate a slotting layout.")
        self.slot_rack_detail = tk.StringVar(value="Generate a layout, then click a rack to inspect it.")
        self.slot_building = None
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
        self.slot_legend = tk.StringVar(value="Load the building map, then drag a rectangle to assign rack zones.")

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        form = ttk.LabelFrame(parent, text="Slotting inputs", padding=12)
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Building YAML").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_building_path).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_building_path, [("RMF building YAML", "*.building.yaml"), ("YAML", "*.yaml"), ("All files", "*")])).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Button(form, text="Load map", command=self.load_slot_building).grid(row=0, column=3, padx=(6, 0), pady=4)
        ttk.Label(form, text="ABC SKU velocity CSV").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_velocity_path).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_velocity_path, [("CSV", "*.csv"), ("All files", "*")])).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(form, text="Chilled SKU CSV (optional)").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_chilled_path).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=lambda: self.browse_slot_input(self.slot_chilled_path, [("CSV", "*.csv"), ("All files", "*")])).grid(row=2, column=2, padx=(8, 0), pady=4)

        ttk.Label(form, text="Strategy").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(form, textvariable=self.slot_strategy, state="readonly", values=("basic",), width=18).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(form, text="Handling unit").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Combobox(form, textvariable=self.slot_handling_unit, state="readonly", values=("AMR shelf", "Tote", "Pallet"), width=18).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(form, text="Zone ID").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_zone, width=20).grid(row=5, column=1, sticky="w", pady=4)
        zone_actions = ttk.Frame(form)
        zone_actions.grid(row=5, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(zone_actions, text="Rectangle zone selection", variable=self.slot_zone_mode, command=self.zone_mode_changed).pack(side="left")
        ttk.Checkbutton(zone_actions, text="Auto next ID", variable=self.slot_zone_auto).pack(side="left", padx=(6,0))
        ttk.Button(zone_actions, text="Clear zones", command=self.clear_slot_zones).pack(side="left", padx=(6,0))

        capacity = ttk.Frame(form)
        capacity.grid(row=6, column=1, sticky="w", pady=4)
        ttk.Label(form, text="Rack capacity").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Label(capacity, text="Levels").pack(side="left")
        ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_levels, width=5).pack(side="left", padx=(5, 14))
        ttk.Label(capacity, text="Slots per level").pack(side="left")
        ttk.Spinbox(capacity, from_=1, to=100, textvariable=self.slot_slots, width=5).pack(side="left", padx=5)

        ttk.Label(form, text="Output layout JSON").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(form, textvariable=self.slot_output_path).grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="Browse…", command=self.browse_slot_output).grid(row=7, column=2, padx=(8, 0), pady=4)
        slot_actions = ttk.Frame(form)
        slot_actions.grid(row=8, column=1, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Button(slot_actions, text="Zone storage settings…", command=self.open_zone_storage_settings).pack(side="left")
        ttk.Button(slot_actions, text="Advanced attributes…", command=self.open_attribute_editor).pack(side="left", padx=(6, 0))
        ttk.Button(slot_actions, text="Load previous layout…", command=self.load_slotting_configuration).pack(side="left", padx=(6, 0))
        ttk.Button(slot_actions, text="Generate slotting layout", command=self.run_slotting, style="Accent.TButton").pack(side="left", padx=(12, 0))
        ttk.Label(form, textvariable=self.slot_summary, foreground="#315b66").grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))

        result = ttk.LabelFrame(parent, text="Interactive slotting layout", padding=8)
        result.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        result.columnconfigure(0, weight=1); result.rowconfigure(0, weight=1)
        paned = ttk.Panedwindow(result, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew")
        layout_view = ttk.Frame(paned)
        rack_view = ttk.Frame(paned)
        paned.add(layout_view, weight=3); paned.add(rack_view, weight=2)
        layout_view.columnconfigure(0, weight=1); layout_view.rowconfigure(0, weight=1)
        rack_view.columnconfigure(0, weight=1); rack_view.rowconfigure(2, weight=1)

        self.slot_canvas = tk.Canvas(layout_view, background="white", highlightthickness=1, highlightbackground="#9aa8ae")
        self.slot_canvas.grid(row=0, column=0, sticky="nsew")
        self.slot_canvas.bind("<Configure>", lambda _event: self.draw_slotting_layout())
        self.slot_canvas.bind("<ButtonPress-1>", self.slot_canvas_press)
        self.slot_canvas.bind("<B1-Motion>", self.slot_canvas_drag)
        self.slot_canvas.bind("<ButtonRelease-1>", self.slot_canvas_release)
        self.slot_canvas.tag_bind("rack", "<Button-1>", self.slot_rack_click)
        ttk.Label(layout_view, textvariable=self.slot_legend, foreground="#4d646d").grid(row=1, column=0, sticky="w", pady=(5, 0))

        ttk.Label(rack_view, text="RACK DETAILS", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w", padx=8)
        ttk.Label(rack_view, textvariable=self.slot_rack_detail, justify="left", wraplength=450).grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
        columns = ("rank", "sku", "class", "flags", "static", "dynamic", "unit_type", "unit_id", "status")
        tree_frame = ttk.Frame(rack_view)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=8)
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
        ttk.Button(rack_view, text="Show all assignments", command=lambda: self.show_slotting_rows(self.slot_rows)).grid(row=3, column=0, sticky="w", padx=8, pady=(7, 0))

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
        self.ops_canvas.tag_bind("ops_rack","<Button-1>",self.ops_rack_click)
        ttk.Label(map_frame,text="Highlighted ring = searched SKU position · click a rack to inspect its contents",foreground="#4d646d").grid(row=1,column=0,sticky="w",pady=(5,0))

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
        except (OSError,ValueError,TypeError,json.JSONDecodeError,yaml.YAMLError) as exc:
            messagebox.showerror("Layout load failed",str(exc));return
        self.ops_payload=payload;self.ops_building=payload["building"];self.ops_rows=payload["assignments"];self.ops_racks=racks;self.ops_highlight_rack=None;self.ops_shelf_selection=[];self.ops_sku_selection=[]
        self.ops_source_sku.set("");self.ops_target_sku.set("")
        self.show_ops_rack_inventory(None)
        self.ops_log.delete(0,"end")
        for event in payload.get("operation_log",[]):self.ops_log.insert("end",event.get("message",str(event)))
        self.ops_status.set(f"Loaded {len(self.ops_rows):,} SKU assignments · {len(racks)} racks · {workstations} workstations · {unreachable} unreachable racks")
        self.ops_details.set("Search for a SKU or click a rack to inspect current inventory addresses.");self.draw_ops_layout()

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
        colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
        shelf_racks={selection["rack_id"] for selection in self.ops_shelf_selection}
        for rack in self.ops_racks:
            x,y=self.ops_screen_point(rack["x"],rack["y"],geometry);rows=grouped.get(rack["rack_id"],[]);hot=sorted((r.get("velocity_class","") for r in rows),key=lambda c:{"A":0,"B":1,"C":2}.get(c,9));fill=colors.get(hot[0] if hot else "","#aeb8bc")
            shelf_selected=rack["rack_id"] in shelf_racks;selected=rack["rack_id"]==self.ops_highlight_rack;radius=11 if shelf_selected else (9 if selected else 4)
            outline="#e07a1f" if shelf_selected else ("#087f8c" if selected else "white")
            self.ops_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline=outline,width=4 if shelf_selected else (3 if selected else 1),tags=("ops_rack",f"opsrack:{rack['rack_id']}"))
            if selected:
                self.ops_canvas.create_text(x,y-16,text=rack["rack_id"],fill="#065f69",font=("TkDefaultFont",9,"bold"))

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
            row.get("static_address", ""), local
        )
        self.ops_details.set(
            f"SKU: {row.get('sku','')} · ABC class {row.get('velocity_class','')} · quantity {row.get('total_quantity_ea','')} EA\n"
            f"Storage flags: {self.sku_storage_flags(row)}\n"
            f"Physical class: {row.get('physical_storage_class','NOT_EVALUATED')} · data {row.get('physical_data_status','NOT_EVALUATED')}\n"
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
            return
        try:
            layout_path = Path(path).expanduser().resolve()
            payload = self.layouts.load(layout_path)
            building = payload["building"]
            _, racks, workstations, unreachable = self.slotting.rack_distances(building)
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
            messagebox.showerror("Layout configuration load failed", str(exc)); return
        source_building = payload.get("sources", {}).get("building_yaml", "")
        source_velocity = payload.get("sources", {}).get("sku_velocity_csv", "")
        source_chilled = payload.get("sources", {}).get("chilled_requirements_csv", "")
        if source_building:
            self.slot_building_path.set(source_building)
        if source_velocity:
            self.slot_velocity_path.set(source_velocity)
        self.slot_chilled_path.set(source_chilled)
        self.slot_building = building
        self.slot_racks = racks
        self.slot_loaded_path = Path(self.slot_building_path.get()).expanduser().resolve()
        self.slot_zone_assignments = zones
        self.slot_levels.set(str(levels)); self.slot_slots.set(str(slots))
        self.slot_strategy.set(payload.get("strategy", "basic"))
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
        self.slot_selected_rack = None; self.slot_zone_mode.set(False)
        self.show_slotting_rows(self.slot_rows); self.draw_slotting_layout()
        self.slot_summary.set(
            f"Restored {len(racks)} racks, {len(set(zones.values()))} zones, "
            f"{len(local)} attributed nodes and {len(self.slot_rows):,} assignments · "
            f"{workstations} workstations · {unreachable} unreachable"
        )
        self.slot_rack_detail.set(
            "Previous layout configuration restored. Edit attributes or regenerate."
        )

    def load_slot_building(self):
        try:
            path=Path(self.slot_building_path.get()).expanduser().resolve()
            building=self.rmf_maps.load_building(path)
            _,racks,workstations,unreachable=self.slotting.rack_distances(building)
        except (OSError,ValueError,TypeError,yaml.YAMLError) as exc:
            messagebox.showerror("Building map load failed",str(exc)); return
        self.slot_building=building; self.slot_racks=racks; self.slot_loaded_path=path
        self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None
        self.slot_attribute_catalog=self.attributes.starter_catalog(); self.slot_location_attributes={}; self.slot_hierarchy_paths=[]
        self.slot_storage_initialized=False
        self.slot_zone_storage_types={}
        self.slot_zone.set("Z01")
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None; self.slot_zone_mode.set(True)
        self.show_slotting_rows([]); self.draw_slotting_layout()
        self.slot_summary.set(f"Loaded {len(racks)} racks and {workstations} workstations · {unreachable} unreachable · assign zones by dragging rectangles")
        self.slot_rack_detail.set("Zone grouping mode is active. Enter a zone ID and drag a rectangle over a group of racks.")

    def zone_mode_changed(self):
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
        self.draw_slotting_layout()

    def clear_slot_zones(self):
        if not self.slot_building: return
        self.slot_zone_assignments={}; self.slot_rows=[]; self.slot_selected_rack=None; self.slot_zone_mode.set(True)
        self.slot_zone_storage_types={}
        self.slot_zone.set("Z01")
        self.show_slotting_rows([]); self.draw_slotting_layout()
        self.slot_summary.set(f"Cleared zone assignments for {len(self.slot_racks)} racks.")

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
        _,level=next(iter(self.slot_building["levels"].items())); geometry=self.slotting_geometry(level["vertices"])
        selected=[]
        for rack in self.slot_racks:
            x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry)
            if left-6<=x<=right+6 and top-6<=y<=bottom+6:
                self.slot_zone_assignments[rack["waypoint"]]=zone; rack["zone_id"]=zone; selected.append(rack)
        self.slotting.apply_zone_local_aisles(self.slot_building,self.slot_racks,self.slot_zone_assignments,"UNASSIGNED")
        self.slot_zone_drag_start=None; self.slot_zone_drag_current=None
        zone_counts={}
        for value in self.slot_zone_assignments.values(): zone_counts[value]=zone_counts.get(value,0)+1
        remaining=len(self.slot_racks)-len(self.slot_zone_assignments)
        if selected and self.slot_zone_auto.get(): self.slot_zone.set(self.slotting.next_zone_id(zone))
        self.slot_summary.set(f"Assigned {len(selected)} rack(s) to {zone} · zones: "+", ".join(f"{key}={value}" for key,value in sorted(zone_counts.items()))+f" · {remaining} unassigned · next ID {self.slot_zone.get()}")
        self.slot_rack_detail.set(f"Zone {zone}: selected {len(selected)} rack(s). The next rectangle will use {self.slot_zone.get()}.")
        self.draw_slotting_layout()

    def run_slotting(self):
        try:
            if self.slot_strategy.get() != "basic":
                raise ValueError("only the basic strategy is available in this demo")
            current_path=Path(self.slot_building_path.get()).expanduser().resolve()
            if self.slot_building is None or self.slot_loaded_path!=current_path:
                raise ValueError("load the selected building YAML before generating")
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
            rows, summary = self.slotting.generate_basic(
                building, skus, levels, slots, self.slot_handling_unit.get(),
                self.slot_zone.get(), self.slot_zone_assignments,
                self.slot_attribute_catalog, self.slot_location_attributes,
            )
            self.layouts.save(
                rows, building, summary, Path(self.slot_output_path.get()).expanduser(),
                strategy=self.slot_strategy.get(),
                handling_unit_type=self.slot_handling_unit.get(),
                levels_per_rack=levels, slots_per_level=slots,
                zone_assignments=self.slot_zone_assignments,
                attribute_catalog=self.slot_attribute_catalog,
                location_attributes=self.slot_location_attributes,
                source_building=str(current_path),
                source_velocity=str(Path(self.slot_velocity_path.get()).expanduser().resolve()),
                source_chilled=(
                    str(Path(self.slot_chilled_path.get()).expanduser().resolve())
                    if self.slot_chilled_path.get().strip()
                    else ""
                ),
            )
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            messagebox.showerror("Slotting generation failed", str(exc)); return
        self.slot_rows = rows
        self.slot_zone_storage_types = summary["zone_storage_types"]
        self.slotting.apply_zone_local_aisles(building,self.slot_racks,self.slot_zone_assignments,self.slot_zone.get())
        self.slot_selected_rack = None
        self.slot_zone_mode.set(False)
        self.show_slotting_rows(rows)
        self.draw_slotting_layout()
        self.slot_rack_detail.set("Click a coloured rack point on the map to inspect its assignments.")
        self.slot_summary.set(
            f"Assigned {summary['assigned_count']:,}/{summary['sku_count']:,} SKUs · "
            f"unassigned {summary['unassigned_count']:,} · "
            f"temperature-zone shortage "
            f"{summary['unassigned_status_counts'].get('UNASSIGNED_NO_CHILLED_LOCATION', 0) + summary['unassigned_status_counts'].get('UNASSIGNED_NO_AMBIENT_LOCATION', 0):,} · "
            f"all slots occupied {summary['unassigned_no_capacity_count']:,} · "
            f"{summary['rack_count']} racks ({summary['unreachable_rack_count']} unreachable) · "
            f"{summary['workstation_count']} workstations · {summary['zone_count']} zones · capacity {summary['capacity']:,} · "
            f"unverified physical data {summary['unverified_oversize_count']:,} "
            f"({summary['assigned_unverified_count']:,} assigned with warning) · "
            f"auto slot overrides {summary['auto_overridden_slot_count']:,} · "
            f"generated zone types "
            + ", ".join(
                f"{zone}={storage_type}"
                for zone, storage_type in summary["zone_storage_types"].items()
            )
            + " · "
            f"saved to {self.slot_output_path.get()}"
        )

    def show_slotting_rows(self, rows):
        self.slot_tree.delete(*self.slot_tree.get_children())
        for row in rows[:1000]:
            self.slot_tree.insert("", "end", values=(row["sku_rank"], row["sku"], row["velocity_class"], self.sku_storage_flags(row), row["static_address"], row["dynamic_address"], row["handling_unit_type"], row["handling_unit_id"], row["assignment_status"]))

    def slotting_geometry(self, vertices):
        xs=[float(v[0]) for v in vertices]; ys=[float(v[1]) for v in vertices]
        min_x,max_x,min_y,max_y=min(xs),max(xs),min(ys),max(ys)
        width=max(300,self.slot_canvas.winfo_width()); height=max(300,self.slot_canvas.winfo_height()); padding=28
        scale=min((width-2*padding)/max(1e-9,max_x-min_x),(height-2*padding)/max(1e-9,max_y-min_y))
        return min_x,max_x,min_y,max_y,width,height,padding,scale

    def slotting_screen_point(self, x, y, geometry):
        min_x,max_x,min_y,max_y,width,height,padding,scale=geometry
        screen_x=padding+(float(x)-min_x)*scale
        if self.slot_building.get("coordinate_system")=="reference_image": screen_y=padding+(float(y)-min_y)*scale
        else: screen_y=height-padding-(float(y)-min_y)*scale
        return screen_x,screen_y

    def draw_slotting_layout(self):
        if not hasattr(self,"slot_canvas"): return
        self.slot_canvas.delete("all")
        if not self.slot_building: return
        _,level=next(iter(self.slot_building["levels"].items())); vertices=level.get("vertices",[])
        if not vertices: return
        geometry=self.slotting_geometry(vertices)
        for lane in level.get("lanes",[]):
            if len(lane)<2 or lane[0]>=len(vertices) or lane[1]>=len(vertices): continue
            a,b=vertices[lane[0]],vertices[lane[1]]; x1,y1=self.slotting_screen_point(a[0],a[1],geometry); x2,y2=self.slotting_screen_point(b[0],b[1],geometry)
            self.slot_canvas.create_line(x1,y1,x2,y2,fill="#d9e0e3",width=1)
        assignments={}
        for row in self.slot_rows:
            if row["assignment_status"]=="ASSIGNED": assignments.setdefault(row["rack_id"],[]).append(row)
        class_colors={"A":"#d1495b","B":"#f3a712","C":"#4c9f70"}
        zone_palette=("#6c8cd5","#9b70c7","#31a6a0","#d47b4c","#8ca63c","#c75d8b","#81756e","#3d8fbe")
        zones=sorted(set(self.slot_zone_assignments.values()))
        zone_colors={zone:zone_palette[index%len(zone_palette)] for index,zone in enumerate(zones)}
        zone_view=self.slot_zone_mode.get() or not self.slot_rows
        for rack in self.slot_racks:
            x,y=self.slotting_screen_point(rack["x"],rack["y"],geometry); rack_rows=assignments.get(rack["rack_id"],[])
            if zone_view: fill=zone_colors.get(self.slot_zone_assignments.get(rack["waypoint"],""),"#aeb8bc")
            else:
                hottest=rack_rows[0]["velocity_class"] if rack_rows else ""; fill=class_colors.get(hottest,"#7b8b92")
            radius=7 if rack["rack_id"]==self.slot_selected_rack else 4
            self.slot_canvas.create_oval(x-radius,y-radius,x+radius,y+radius,fill=fill,outline="#087f8c" if radius==7 else "white",width=3 if radius==7 else 1,tags=("rack",f"rack:{rack['rack_id']}"))
        for index,vertex in enumerate(vertices):
            params=vertex[4] if len(vertex)>4 and isinstance(vertex[4],dict) else {}
            if "dropoff_ingestor" not in params: continue
            endpoint=str(self.slotting.typed_value(params["dropoff_ingestor"],vertex[3])); x,y=self.slotting_screen_point(vertex[0],vertex[1],geometry); r=6
            self.slot_canvas.create_polygon(x,y-r,x+r,y,x,y+r,x-r,y,fill="#277da1",outline="white")
            self.slot_canvas.create_text(x,y-11,text=endpoint,fill="#1d5d78",font=("TkDefaultFont",8,"bold"))
        if self.slot_zone_drag_start is not None and self.slot_zone_drag_current is not None:
            x1,y1=self.slot_zone_drag_start; x2,y2=self.slot_zone_drag_current
            self.slot_canvas.create_rectangle(x1,y1,x2,y2,outline="#7b2cbf",width=2,dash=(5,3))
        if zone_view:
            counts={zone:sum(value==zone for value in self.slot_zone_assignments.values()) for zone in zones}
            self.slot_legend.set("Zone grouping · drag rectangle · "+(" · ".join(f"{zone}: {counts[zone]} racks" for zone in zones) if zones else "all racks unassigned"))
        else:
            self.slot_legend.set("Rack colour: A = red · B = orange · C = green · Empty = grey · ◆ Workstation = blue")

    def slot_rack_click(self, event):
        if self.slot_zone_mode.get(): return
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
        bay_path = f"{rack.get('zone_id','UNASSIGNED')}/{rack['aisle_id']}/{rack['static_bay_id']}"
        effective, _sources = self.attributes.effective_attributes(
            bay_path, self.slot_location_attributes
        )
        chilled_count, exception_count = self.rack_storage_flag_counts(rows)
        self.slot_rack_detail.set(
            f"Static grid rack: {rack_id}\nPickup dispenser: {rack['pickup_dispenser_id']} · vertex {rack['vertex_index']}\n"
            f"Current static address: {rows[0]['static_address'] if rows else rack.get('zone_id','UNASSIGNED')+'/'+rack['aisle_id']+'/'+rack['static_bay_id']+'/L--/S--'}\n"
            f"Generated zone storage type: {self.slot_zone_storage_types.get(rack.get('zone_id', ''), 'UNUSED')}\n"
            f"Assigned SKUs: {len(rows)} · A {classes['A']} / B {classes['B']} / C {classes['C']}\n"
            f"Storage flags: CHILLED={chilled_count} · OVERSIZE / WEIGHT EXCEPTION={exception_count}\n"
            f"Physical classes: "
            + (", ".join(
                f"{key}={sum(row.get('physical_storage_class') == key for row in rows)}"
                for key in ("STANDARD", "OVERSIZE", "OVERWEIGHT", "OVERSIZE_AND_OVERWEIGHT", "UNVERIFIED_OVERSIZE")
                if any(row.get('physical_storage_class') == key for row in rows)
            ) or "not evaluated") + "\n"
            f"Handling unit(s): {', '.join(unit_ids) if unit_ids else 'none'}\n"
            f"Effective bay attributes: {self.attributes.format_values(effective)}"
        )

    def spec_from_inputs(self) -> GridSpec:
        spec = GridSpec(float(self.width.get()), float(self.length.get()), float(self.spacing.get()), self.map_name.get().strip(), self.level_name.get().strip())
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
        self.selected = None
        self.bulk_anchor = None
        self.update_selected_editor()
        self.redraw()
        self.status.set("Grid generated. All neighbouring points are connected bidirectionally.")

    def geometry(self):
        width = max(200, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        padding = 45
        scale = min((width - 2 * padding) / self.project.grid.width_m, (height - 2 * padding) / self.project.grid.length_m)
        return padding, scale, height

    def screen_point(self, column, row):
        padding, scale, height = self.geometry()
        x = padding + column * self.project.grid.spacing_m * scale
        y = height - padding - row * self.project.grid.spacing_m * scale
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
        radius = max(2, min(5, self.geometry()[1] * spec.spacing_m * 0.10))
        for column, row in self.project.iter_positions():
            x, y = self.screen_point(column, row)
            marker = self.project.markers.get((column, row))
            fill = "#d1495b" if marker and marker.role == "workstation" else "#f3a712" if marker else "#50656e"
            r = radius + 3 if self.selected == (column, row) else radius
            outline = "#087f8c" if self.selected == (column, row) else fill
            self.canvas.create_oval(x-r, y-r, x+r, y+r, fill=fill, outline=outline, width=3 if self.selected == (column, row) else 1)
            if marker:
                self.canvas.create_text(x, y-12, text=marker.endpoint_id, fill=fill, font=("TkDefaultFont", 8, "bold"))
        if self.bulk_anchor is not None:
            x, y = self.screen_point(*self.bulk_anchor)
            self.canvas.create_oval(x-9, y-9, x+9, y+9, outline="#7b2cbf", width=3)
        x0, y0 = self.screen_point(0, 0)
        self.canvas.create_text(x0, y0+20, text="(0, 0)", anchor="n", fill="#087f8c", font=("TkDefaultFont", 9, "bold"))
        self.summary.set(f"{spec.columns} columns × {spec.rows} rows\n{spec.vertex_count:,} vertices · {spec.edge_count:,} edges")

    def nearest_position(self, event) -> GridPosition | None:
        padding, scale, height = self.geometry()
        column = round((event.x - padding) / (self.project.grid.spacing_m * scale))
        row = round((height - padding - event.y) / (self.project.grid.spacing_m * scale))
        if 0 <= column <= self.project.grid.columns and 0 <= row <= self.project.grid.rows:
            x, y = self.screen_point(column, row)
            if math.hypot(event.x-x, event.y-y) <= max(12, self.project.grid.spacing_m*scale*.35):
                return column, row
        return None

    def canvas_click(self, event):
        position = self.nearest_position(event)
        if position is None: return
        action = self.tool.get()
        if action == "clear":
            self.push_undo(); self.drag_undo_started = True
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
        else: self.project.markers.pop(position, None)
        self.selected = position
        self.update_selected_editor()
        self.redraw()

    def canvas_release(self, _event):
        self.drag_undo_started = False

    def place_rack(self, position: GridPosition, redraw=True):
        prefix = self.rack_prefix.get().strip() or "RACK"
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
        self.selected_coordinate.set(f"Column {column}, row {row}  →  ({column*self.project.grid.spacing_m:g}, {row*self.project.grid.spacing_m:g}) m")
        marker = self.project.markers.get(self.selected)
        self.role.set(marker.role if marker else "none")
        self.endpoint_id.set(marker.endpoint_id if marker else "")

    def apply_edit(self):
        if self.selected is None:
            messagebox.showinfo("Select a point", "Select a grid point first."); return
        role = self.role.get()
        before = self.snapshot()
        if role == "none": self.project.markers.pop(self.selected, None)
        else:
            endpoint = self.endpoint_id.get().strip()
            if not endpoint:
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
