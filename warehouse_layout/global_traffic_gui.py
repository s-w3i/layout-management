"""Independent UI tab for exact/bounded global traffic slotting."""

from __future__ import annotations

import copy
import json
import math
import queue
import threading
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .affinity import AffinityCancelledError
from .config import (
    DEFAULT_GLOBAL_TRAFFIC_OUTPUT,
    DEFAULT_GRID_INPUT,
    DEFAULT_SLOTTING_OUTPUT,
    DEFAULT_TRAFFIC_INPUT,
    DEFAULT_VELOCITY_INPUT,
)
from .global_traffic import (
    GlobalTrafficCancelledError,
    GlobalTrafficSlottingService,
)
from .global_traffic_search import GlobalTrafficParameterSearch
from .traffic import (
    InsufficientStorageError,
    TrafficAwareSlottingService,
    TrafficCancelledError,
)


class GlobalTrafficOptimizerTab:
    """Own the new global pipeline without changing the original traffic tab."""

    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.root = app.root
        self.service = GlobalTrafficSlottingService(
            TrafficAwareSlottingService(app.attributes, app.slotting)
        )
        self.search = GlobalTrafficParameterSearch()
        self.result = None
        self.search_result = None
        self.selected_search_trial = None
        self.search_trials_by_item = {}
        self._populating_search_tree = False
        self.network = None
        self.messages = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        self.layout_path = tk.StringVar(value=str(DEFAULT_SLOTTING_OUTPUT))
        self.grid_path = tk.StringVar(value=str(DEFAULT_GRID_INPUT))
        self.velocity_path = tk.StringVar(value=str(DEFAULT_VELOCITY_INPUT))
        self.chilled_path = tk.StringVar()
        self.order_path = tk.StringVar(value=str(DEFAULT_TRAFFIC_INPUT))
        self.network_mode = tk.StringVar(value="Use embedded RMF map")
        self.network_path = tk.StringVar()
        self.initial_strategy = tk.StringVar(value="ABC")
        self.affinity_weight = tk.StringVar(value="50")
        self.start_date = tk.StringVar()
        self.end_date = tk.StringVar()
        self.time_limit = tk.StringVar(value="180")
        self.gap_percent = tk.StringVar(value="0")
        self.max_travel = tk.StringVar(value="0")
        self.max_controllable_p95 = tk.StringVar(value="0")
        self.max_relocations = tk.StringVar(value="50")
        self.search_screen_seconds = tk.StringVar(value="90")
        self.search_final_seconds = tk.StringVar(value="300")
        self.search_finalists = tk.StringVar(value="3")
        self.neighbourhood = tk.StringVar(value="Shared route resource")
        self.output_path = tk.StringVar(value=str(DEFAULT_GLOBAL_TRAFFIC_OUTPUT))
        self.view_mode = tk.StringVar(value="After")
        self.resource_view = tk.StringVar(value="Controllable")
        self.show_rack_heat = tk.BooleanVar(value=True)
        self.progress_value = tk.DoubleVar(value=0)
        self.status = tk.StringVar(
            value=(
                "Choose a saved layout, or generate an ABC/affinity baseline, "
                "then run global optimization."
            )
        )
        self.kpis = tk.StringVar(
            value=(
                "Solver —  · Gap —  · Peak — → —  · Travel — → —  · "
                "Relocations —"
            )
        )
        self._build()

    @staticmethod
    def _tree(parent, columns):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        names = tuple(item[0] for item in columns)
        tree = ttk.Treeview(parent, columns=names, show="headings")
        for name, heading, width in columns:
            tree.heading(name, text=heading)
            tree.column(name, width=width, anchor="w")
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return tree

    @staticmethod
    def _entry_row(
        parent, row, label, variable, browse_command=None, *, width=None
    ):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=2
        )
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(
            row=row, column=1, sticky="ew", padx=(8, 4), pady=2
        )
        button = None
        if browse_command:
            button = ttk.Button(
                parent, text="Browse…", command=browse_command
            )
            button.grid(row=row, column=2, pady=2)
        return entry, button

    def _build(self):
        parent = self.parent
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        settings = ttk.Frame(parent)
        settings.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        settings.columnconfigure(0, weight=1, uniform="global")
        settings.columnconfigure(1, weight=1, uniform="global")

        initial = ttk.LabelFrame(
            settings,
            text="Initial ABC / affinity generation · Full pipeline only",
            padding=8,
        )
        initial.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        initial.columnconfigure(1, weight=1)
        self._entry_row(
            initial, 0, "Grid project JSON", self.grid_path,
            lambda: self._browse(
                self.grid_path,
                (("Grid project", "*.grid.json"), ("JSON", "*.json")),
            ),
        )
        self._entry_row(
            initial, 1, "ABC SKU velocity CSV", self.velocity_path,
            lambda: self._browse(
                self.velocity_path, (("CSV", "*.csv"), ("All files", "*"))
            ),
        )
        self._entry_row(
            initial, 2, "Chilled SKU CSV (optional)", self.chilled_path,
            lambda: self._browse(
                self.chilled_path, (("CSV", "*.csv"), ("All files", "*"))
            ),
        )
        ttk.Label(initial, text="Initial strategy").grid(
            row=3, column=0, sticky="w", pady=2
        )
        strategy = ttk.Frame(initial)
        strategy.grid(
            row=3, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=2
        )
        strategy_box = ttk.Combobox(
            strategy,
            textvariable=self.initial_strategy,
            state="readonly",
            values=("ABC", "ABC + Affinity"),
            width=17,
        )
        strategy_box.pack(side="left")
        strategy_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_controls()
        )
        ttk.Label(strategy, text="Affinity weight %").pack(
            side="left", padx=(10, 3)
        )
        self.affinity_spin = ttk.Spinbox(
            strategy, from_=0, to=100,
            textvariable=self.affinity_weight, width=5,
        )
        self.affinity_spin.pack(side="left")
        ttk.Label(
            initial,
            text=(
                "This section is ignored when optimizing an existing layout."
            ),
            foreground="#4d646d",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        optimizer = ttk.LabelFrame(
            settings,
            text="Global congestion optimizer · Both workflows",
            padding=8,
        )
        optimizer.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        optimizer.columnconfigure(1, weight=1)
        self._entry_row(
            optimizer, 0, "Existing layout", self.layout_path,
            lambda: self._browse(
                self.layout_path,
                (("Slotting layout", "*.slotting.json"), ("JSON", "*.json")),
            ),
        )
        self._entry_row(
            optimizer, 1, "Order-history Excel", self.order_path,
            lambda: self._browse(
                self.order_path,
                (("Excel workbook", "*.xlsx"), ("All files", "*")),
            ),
        )
        ttk.Label(optimizer, text="Movement network").grid(
            row=2, column=0, sticky="w", pady=2
        )
        network_box = ttk.Combobox(
            optimizer,
            textvariable=self.network_mode,
            state="readonly",
            values=(
                "Use embedded RMF map",
                "Use network / grid project JSON",
            ),
            width=28,
        )
        network_box.grid(
            row=2, column=1, columnspan=2, sticky="w",
            padx=(8, 0), pady=2,
        )
        network_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_controls()
        )
        self.network_entry, self.network_button = self._entry_row(
            optimizer, 3, "Network JSON (optional)", self.network_path,
            lambda: self._browse(
                self.network_path,
                (("Movement or grid JSON", "*.json"), ("All files", "*")),
            ),
        )
        dates = ttk.Frame(optimizer)
        dates.grid(
            row=4, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Label(dates, text="Dates").pack(side="left")
        ttk.Entry(
            dates, textvariable=self.start_date, width=11
        ).pack(side="left", padx=(8, 3))
        ttk.Label(dates, text="to").pack(side="left")
        ttk.Entry(
            dates, textvariable=self.end_date, width=11
        ).pack(side="left", padx=(3, 5))
        ttk.Label(
            dates, text="YYYY-MM-DD; blank = all",
            foreground="#4d646d",
        ).pack(side="left")
        limits = ttk.Frame(optimizer)
        limits.grid(
            row=5, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Label(limits, text="Solve seconds").pack(side="left")
        ttk.Entry(
            limits, textvariable=self.time_limit, width=6
        ).pack(side="left", padx=(4, 9))
        ttk.Label(limits, text="Gap %").pack(side="left")
        ttk.Entry(
            limits, textvariable=self.gap_percent, width=5
        ).pack(side="left", padx=(4, 9))
        ttk.Label(limits, text="Max travel +%").pack(side="left")
        ttk.Entry(
            limits, textvariable=self.max_travel, width=5
        ).pack(side="left", padx=(4, 0))
        ttk.Label(limits, text="Controllable P95 +%").pack(
            side="left", padx=(9, 0)
        )
        ttk.Entry(
            limits, textvariable=self.max_controllable_p95, width=5
        ).pack(side="left", padx=(4, 0))
        ttk.Label(limits, text="Max relocated %").pack(
            side="left", padx=(9, 0)
        )
        ttk.Entry(
            limits, textvariable=self.max_relocations, width=5
        ).pack(side="left", padx=(4, 0))
        balance = ttk.Frame(optimizer)
        balance.grid(
            row=6, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Label(balance, text="Spatial balance").pack(side="left")
        ttk.Combobox(
            balance,
            textvariable=self.neighbourhood,
            state="readonly",
            values=("Shared route resource", "Zone"),
            width=22,
        ).pack(side="left", padx=(8, 0))
        search_limits = ttk.Frame(optimizer)
        search_limits.grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 2)
        )
        ttk.Label(search_limits, text="Auto-search").pack(side="left")
        ttk.Label(search_limits, text="Screen seconds").pack(
            side="left", padx=(8, 0)
        )
        ttk.Entry(
            search_limits,
            textvariable=self.search_screen_seconds,
            width=5,
        ).pack(side="left", padx=(4, 8))
        ttk.Label(search_limits, text="Final seconds").pack(side="left")
        ttk.Entry(
            search_limits,
            textvariable=self.search_final_seconds,
            width=5,
        ).pack(side="left", padx=(4, 8))
        ttk.Label(search_limits, text="Finalists").pack(side="left")
        ttk.Spinbox(
            search_limits,
            from_=1,
            to=9,
            textvariable=self.search_finalists,
            width=3,
        ).pack(side="left", padx=(4, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self.existing_button = ttk.Button(
            actions,
            text="Optimize Existing Layout Globally",
            command=lambda: self.start("existing_layout"),
        )
        self.existing_button.pack(side="left")
        self.full_button = ttk.Button(
            actions,
            text="Generate Layout + Globally Optimize",
            command=lambda: self.start("full_pipeline"),
        )
        self.full_button.pack(side="left", padx=(6, 0))
        self.search_button = ttk.Button(
            actions,
            text="Auto-search Best Layout",
            command=lambda: self.start("auto_search"),
        )
        self.search_button.pack(side="left", padx=(6, 0))
        self.cancel_button = ttk.Button(
            actions, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(6, 0))
        ttk.Entry(
            actions, textvariable=self.output_path
        ).pack(side="left", fill="x", expand=True, padx=(12, 4))
        ttk.Button(
            actions, text="Output…", command=self._browse_output
        ).pack(side="left")
        self.save_button = ttk.Button(
            actions, text="Save Layout", command=self.save, state="disabled"
        )
        self.save_button.pack(side="left", padx=(4, 0))

        feedback = ttk.Frame(parent)
        feedback.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        feedback.columnconfigure(0, weight=1)
        ttk.Progressbar(
            feedback, variable=self.progress_value, maximum=100
        ).grid(row=0, column=0, sticky="ew")
        ttk.Label(
            feedback, textvariable=self.status
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            feedback, textvariable=self.kpis,
            foreground="#284f62",
        ).grid(row=2, column=0, sticky="w")

        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        map_frame = ttk.LabelFrame(
            pane, text="Global congestion view", padding=6
        )
        details = ttk.LabelFrame(
            pane, text="Optimization result", padding=6
        )
        pane.add(map_frame, weight=3)
        pane.add(details, weight=2)
        map_frame.columnconfigure(0, weight=1)
        map_frame.rowconfigure(1, weight=1)
        view = ttk.Frame(map_frame)
        view.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(view, text="View").pack(side="left")
        view_box = ttk.Combobox(
            view,
            textvariable=self.view_mode,
            state="readonly",
            values=("Before", "After"),
            width=9,
        )
        view_box.pack(side="left", padx=(5, 12))
        view_box.bind("<<ComboboxSelected>>", lambda _event: self.draw_map())
        ttk.Label(view, text="Resources").pack(side="left", padx=(0, 4))
        resource_box = ttk.Combobox(
            view,
            textvariable=self.resource_view,
            state="readonly",
            values=("Controllable", "All"),
            width=13,
        )
        resource_box.pack(side="left", padx=(0, 12))
        resource_box.bind(
            "<<ComboboxSelected>>", lambda _event: self.draw_map()
        )
        ttk.Checkbutton(
            view,
            text="Rack picking-frequency heat",
            variable=self.show_rack_heat,
            command=self.draw_map,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(
            view,
            text=(
                "Lane/rack heat: blue low · yellow medium · red high · "
                "grey rack zero/off · purple border relocated"
            ),
            foreground="#4d646d",
        ).pack(side="left")
        self.canvas = tk.Canvas(
            map_frame,
            background="white",
            highlightthickness=1,
            highlightbackground="#9aa8ae",
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.draw_map())
        self.app.enable_canvas_viewport(self.canvas)

        details.columnconfigure(0, weight=1)
        details.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(details)
        notebook.grid(row=0, column=0, sticky="nsew")
        solver_tab = ttk.Frame(notebook)
        relocations_tab = ttk.Frame(notebook)
        balance_tab = ttk.Frame(notebook)
        search_tab = ttk.Frame(notebook)
        notebook.add(solver_tab, text="Solver Proof")
        notebook.add(relocations_tab, text="Relocations")
        notebook.add(balance_tab, text="Balance")
        notebook.add(search_tab, text="Auto-search Trials")
        self.solver_tree = self._tree(
            solver_tab,
            (
                ("stage", "Objective stage", 190),
                ("status", "Status", 70),
                ("value", "Value", 90),
                ("bound", "Best bound", 90),
                ("gap", "Gap", 70),
            ),
        )
        self.relocation_tree = self._tree(
            relocations_tab,
            (
                ("unit", "Handling unit", 120),
                ("source", "From", 120),
                ("target", "To", 120),
                ("replaced", "Location replaced", 120),
            ),
        )
        self.balance_tree = self._tree(
            balance_tab,
            (
                ("metric", "Metric", 210),
                ("before", "Before", 90),
                ("after", "After", 90),
                ("change", "Change", 90),
            ),
        )
        self.search_tree = self._tree(
            search_tab,
            (
                ("selected", "Plan", 45),
                ("phase", "Phase", 70),
                ("scenario", "Scenario", 90),
                ("status", "Status", 75),
                ("travel", "Travel +%", 75),
                ("relocated", "Relocated %", 80),
                ("peak", "Peak", 75),
                ("p95", "P95", 75),
                ("neighbourhood", "Neighbourhood", 100),
                ("moves", "Moves", 55),
            ),
        )
        self.search_tree.bind(
            "<<TreeviewSelect>>", self.select_search_trial
        )
        self._update_controls()

    def _update_controls(self):
        self.affinity_spin.configure(
            state=(
                "normal"
                if self.initial_strategy.get() == "ABC + Affinity"
                else "disabled"
            )
        )
        network_state = (
            "normal"
            if self.network_mode.get() != "Use embedded RMF map"
            else "disabled"
        )
        self.network_entry.configure(state=network_state)
        self.network_button.configure(state=network_state)

    def _browse(self, variable, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            variable.set(path)

    def _browse_output(self):
        current = Path(self.output_path.get()).expanduser()
        path = filedialog.asksaveasfilename(
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".slotting.json",
            filetypes=(
                ("Slotting layout", "*.slotting.json"),
                ("JSON", "*.json"),
            ),
        )
        if path:
            self.output_path.set(path)

    @staticmethod
    def _parse_date(value):
        return date.fromisoformat(value) if value.strip() else None

    def start(self, workflow):
        if self.worker and self.worker.is_alive():
            return
        try:
            order_path = Path(self.order_path.get()).expanduser().resolve()
            output_path = Path(
                self.output_path.get()
            ).expanduser().resolve()
            start = self._parse_date(self.start_date.get())
            end = self._parse_date(self.end_date.get())
            time_limit = float(self.time_limit.get())
            gap = float(self.gap_percent.get()) / 100.0
            max_travel = float(self.max_travel.get()) / 100.0
            max_controllable_p95 = (
                float(self.max_controllable_p95.get()) / 100.0
            )
            max_relocations = (
                float(self.max_relocations.get()) / 100.0
            )
            search_screen_seconds = float(
                self.search_screen_seconds.get()
            )
            search_final_seconds = float(
                self.search_final_seconds.get()
            )
            search_finalists = int(self.search_finalists.get())
            if time_limit < 0:
                raise ValueError("solve seconds cannot be negative")
            if not 0 <= gap <= 1:
                raise ValueError("gap must be between 0% and 100%")
            if max_travel < 0:
                raise ValueError("maximum travel increase cannot be negative")
            if max_controllable_p95 < 0:
                raise ValueError(
                    "controllable P95 increase cannot be negative"
                )
            if not 0 <= max_relocations <= 1:
                raise ValueError(
                    "maximum relocated percentage must be between 0 and 100"
                )
            if (
                search_screen_seconds <= 0
                or search_final_seconds <= 0
            ):
                raise ValueError(
                    "auto-search solve seconds must be positive"
                )
            if not 1 <= search_finalists <= 9:
                raise ValueError(
                    "auto-search finalists must be between 1 and 9"
                )
            neighbourhood_mode = (
                "zone"
                if self.neighbourhood.get() == "Zone"
                else "shared_resource"
            )
            network_path = (
                Path(self.network_path.get()).expanduser().resolve()
                if self.network_mode.get() != "Use embedded RMF map"
                else None
            )
            if workflow in {"existing_layout", "auto_search"}:
                layout_path = Path(
                    self.layout_path.get()
                ).expanduser().resolve()
                baseline = self.app.layouts.load(layout_path)
                # Reject incomplete or physically invalid layouts before
                # parsing a potentially very large order workbook.
                self.service.traffic.validate_traffic_baseline(
                    copy.deepcopy(baseline)
                )
                building = baseline["building"]
                full_inputs = None
            else:
                layout_path = None
                baseline = None
                grid_path = Path(
                    self.grid_path.get()
                ).expanduser().resolve()
                velocity_path = Path(
                    self.velocity_path.get()
                ).expanduser().resolve()
                chilled_path = (
                    Path(self.chilled_path.get()).expanduser().resolve()
                    if self.chilled_path.get().strip() else None
                )
                project = self.app.rmf_maps.load_project(grid_path)
                if (
                    project.storage_layout is None
                    or not project.storage_layout.buffers
                ):
                    raise ValueError(
                        "grid project has no storage buffers; assign and save "
                        "buffers in Grid Map Editor first"
                    )
                building = project.to_building_dict()
                strategy = (
                    "basic"
                    if self.initial_strategy.get() == "ABC"
                    else "abc_affinity"
                )
                affinity_weight = float(self.affinity_weight.get()) / 100.0
                if not 0 <= affinity_weight <= 1:
                    raise ValueError(
                        "affinity weight must be between 0% and 100%"
                    )
                full_inputs = (
                    project, grid_path, velocity_path, chilled_path,
                    strategy, affinity_weight,
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror("Global traffic optimizer", str(exc))
            return

        self.result = None
        self.search_result = None
        self.selected_search_trial = None
        self.search_trials_by_item = {}
        self.network = None
        self.cancel_event.clear()
        self.progress_value.set(0)
        self.status.set(
            "Loading order history and building the global model…"
        )
        self._busy(True)

        def report(current, total, message):
            self.messages.put(("progress", current, total, message))

        def worker():
            try:
                dataset = self.app.affinity.load_orders(
                    order_path,
                    progress=report,
                    cancelled=self.cancel_event.is_set,
                )
                network = (
                    self.service.traffic.load_network(network_path)
                    if network_path is not None
                    else self.service.traffic.network_from_rmf(building)
                )
                if workflow == "existing_layout":
                    result = self.service.optimize_existing_layout(
                        baseline,
                        dataset,
                        network,
                        start_date=start,
                        end_date=end,
                        maximum_travel_increase=max_travel,
                        time_limit_seconds=time_limit,
                        relative_gap_limit=gap,
                        maximum_controllable_p95_increase=(
                            max_controllable_p95
                        ),
                        maximum_relocation_fraction=max_relocations,
                        neighbourhood_mode=neighbourhood_mode,
                        baseline_path=str(layout_path),
                        source_orders=str(order_path),
                        progress=report,
                        cancelled=self.cancel_event.is_set,
                    )
                elif workflow == "auto_search":
                    def optimize_scenario(
                        scenario, seconds, trial_progress
                    ):
                        return self.service.optimize_existing_layout(
                            baseline,
                            dataset,
                            network,
                            start_date=start,
                            end_date=end,
                            maximum_travel_increase=(
                                scenario.maximum_travel_increase
                            ),
                            time_limit_seconds=seconds,
                            relative_gap_limit=gap,
                            maximum_controllable_p95_increase=(
                                max_controllable_p95
                            ),
                            maximum_relocation_fraction=(
                                scenario.maximum_relocation_fraction
                            ),
                            neighbourhood_mode=neighbourhood_mode,
                            baseline_path=str(layout_path),
                            source_orders=str(order_path),
                            progress=trial_progress,
                            cancelled=self.cancel_event.is_set,
                        )

                    search_result = self.search.search(
                        optimize_scenario,
                        screening_seconds=search_screen_seconds,
                        final_seconds=search_final_seconds,
                        finalist_count=search_finalists,
                        progress=report,
                        cancelled=self.cancel_event.is_set,
                    )
                    self.messages.put((
                        "search_done", search_result, network
                    ))
                    return
                else:
                    (
                        project, grid_path, velocity_path, chilled_path,
                        strategy, affinity_weight,
                    ) = full_inputs
                    catalog = self.app.attributes.normalize_catalog(
                        project.attribute_catalog
                    )
                    sku_rows = self.app.slotting.load_velocity(
                        velocity_path, catalog, chilled_path
                    )
                    affinity_source = (
                        self.app.affinity.analyze(dataset, start, end)
                        if strategy == "abc_affinity" else dataset
                    )
                    storage = project.storage_layout
                    result = self.service.run_full_pipeline(
                        building,
                        sku_rows,
                        affinity_source,
                        network,
                        initial_strategy=strategy,
                        affinity_weight=affinity_weight,
                        levels_per_rack=storage.levels_per_rack,
                        slots_per_level=storage.slots_per_level,
                        handling_unit_type=storage.handling_unit_type,
                        zone_assignments=copy.deepcopy(
                            project.zone_assignments
                        ),
                        attribute_catalog=catalog,
                        location_attributes=copy.deepcopy(
                            project.location_attributes
                        ),
                        storage_layout=storage,
                        start_date=start,
                        end_date=end,
                        maximum_travel_increase=max_travel,
                        time_limit_seconds=time_limit,
                        relative_gap_limit=gap,
                        maximum_controllable_p95_increase=(
                            max_controllable_p95
                        ),
                        maximum_relocation_fraction=max_relocations,
                        neighbourhood_mode=neighbourhood_mode,
                        source_grid_project=str(grid_path),
                        source_velocity=str(velocity_path),
                        source_chilled=str(chilled_path or ""),
                        source_orders=str(order_path),
                        progress=report,
                        cancelled=self.cancel_event.is_set,
                    )
                self.messages.put(("done", result, network))
            except (
                GlobalTrafficCancelledError,
                TrafficCancelledError,
                AffinityCancelledError,
            ) as exc:
                self.messages.put(("cancelled", str(exc)))
            except InsufficientStorageError as exc:
                self.messages.put(("capacity", str(exc)))
            except Exception as exc:
                self.messages.put(("error", exc))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()
        self.root.after(80, self.poll)

    def _busy(self, busy):
        state = "disabled" if busy else "normal"
        self.existing_button.configure(state=state)
        self.full_button.configure(state=state)
        self.search_button.configure(state=state)
        self.cancel_button.configure(
            state="normal" if busy else "disabled"
        )
        self.save_button.configure(
            state=(
                "disabled"
                if busy or self.result is None
                else "normal"
            )
        )

    def cancel(self):
        self.cancel_event.set()
        self.status.set("Cancelling global optimization…")

    def poll(self):
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "progress":
                _kind, current, total, status = message
                self.progress_value.set(
                    100 * current / max(1, total)
                )
                self.status.set(status)
            elif kind == "done":
                self.complete(message[1], message[2])
            elif kind == "search_done":
                search_result, network = message[1], message[2]
                self.search_result = search_result
                self.selected_search_trial = search_result.best_trial
                self.complete(search_result.best_result, network)
                self.status.set(
                    "Auto-search completed: viewing recommended trial "
                    f"{search_result.best_trial.scenario.scenario_id} "
                    f"({search_result.best_trial.phase}). Select any successful "
                    "trial to compare it, then click Save Layout."
                )
            elif kind in {"cancelled", "capacity"}:
                self._busy(False)
                self.progress_value.set(0)
                self.status.set(message[1])
            elif kind == "error":
                self._busy(False)
                self.progress_value.set(0)
                self.status.set(f"Global optimization failed: {message[1]}")
                messagebox.showerror(
                    "Global traffic optimizer", str(message[1])
                )
        if self.worker and self.worker.is_alive():
            self.root.after(80, self.poll)

    @staticmethod
    def _fmt(value):
        return f"{float(value):,.3f}"

    def complete(self, result, network):
        self.result = result
        self.network = network
        self.progress_value.set(100)
        self._busy(False)
        solver = result.solver
        gap_text = (
            f"{solver['relative_gap']:.2%}"
            if solver["relative_gap"] is not None
            else "unproven"
        )
        proof = (
            "fixed-route candidate optimum proven"
            if solver["global_optimum_proven"]
            else "best feasible layout within current solve"
        )
        excluded = int(
            solver.get("hard_validation", {}).get(
                "excluded_unassigned_sku_count", 0
            )
        )
        self.status.set(
            f"{solver['status']}: {proof}; "
            f"{len(result.relocations):,} handling units relocated."
            + (
                f" {excluded:,} unassigned SKU(s) were excluded and retained "
                "unchanged."
                if excluded else ""
            )
            + (
                " Network capacities are missing, so loads are relative."
                if solver.get("capacity_warning") else ""
            )
        )
        before, after = result.before.metrics, result.after.metrics
        controlled_before = result.balance_metrics[
            "controllable_resources_before"
        ]
        controlled_after = result.balance_metrics[
            "controllable_resources_after"
        ]
        self.kpis.set(
            f"Solver {solver['status']} · Gap {gap_text} · "
            f"Controllable peak {controlled_before['peak_load']:.3f} → "
            f"{controlled_after['peak_load']:.3f} · "
            f"P95 {controlled_before['p95_load']:.3f} → "
            f"{controlled_after['p95_load']:.3f} · "
            f"Travel {before['expected_travel']:.1f} → "
            f"{after['expected_travel']:.1f} · "
            f"Relocations {len(result.relocations):,} · "
            f"Excluded unassigned {excluded:,}"
        )
        self._populate_tables()
        self.draw_map()

    def _populate_tables(self):
        self._populating_search_tree = True
        for tree in (
            self.solver_tree,
            self.relocation_tree,
            self.balance_tree,
            self.search_tree,
        ):
            tree.delete(*tree.get_children())
        for row in self.result.solver["stages"]:
            self.solver_tree.insert("", "end", values=(
                row["stage"],
                row["status"],
                f"{row['objective_value']:,}",
                f"{row['best_bound']:,.0f}",
                f"{row['relative_gap']:.2%}",
            ))
        for row in self.result.relocations:
            self.relocation_tree.insert("", "end", values=(
                row["handling_unit_id"],
                row["from"],
                row["to"],
                row["location_replaced"],
            ))
        before = self.result.before.metrics
        after = self.result.after.metrics
        balance = self.result.balance_metrics
        rows = (
            (
                "Controllable resource peak",
                balance["controllable_resources_before"]["peak_load"],
                balance["controllable_resources_after"]["peak_load"],
            ),
            (
                "Controllable resource P90",
                balance["controllable_resources_before"]["p90_load"],
                balance["controllable_resources_after"]["p90_load"],
            ),
            (
                "Controllable resource P95",
                balance["controllable_resources_before"]["p95_load"],
                balance["controllable_resources_after"]["p95_load"],
            ),
            (
                "Structural/invariant peak",
                balance["invariant_resources_before"]["peak_load"],
                balance["invariant_resources_after"]["peak_load"],
            ),
            (
                "Expected travel",
                before["expected_travel"],
                after["expected_travel"],
            ),
            (
                "Peak zone normalized load",
                balance["zone_before"]["peak_normalized_load"],
                balance["zone_after"]["peak_normalized_load"],
            ),
            (
                "Peak neighbourhood normalized load",
                balance["neighbourhood_before"]["peak_normalized_load"],
                balance["neighbourhood_after"]["peak_normalized_load"],
            ),
        )
        for name, old, new in rows:
            self.balance_tree.insert("", "end", values=(
                name, self._fmt(old), self._fmt(new), self._fmt(new - old)
            ))
        if self.search_result is not None:
            selected_trial = (
                self.selected_search_trial
                or self.search_result.best_trial
            )
            selected_item = None
            self.search_trials_by_item = {}
            for index, trial in enumerate(self.search_result.trials):
                row = self.search.trial_row(
                    trial, selected_trial
                )
                is_recommended = trial is self.search_result.best_trial
                marker = (
                    "★✓" if is_recommended and row["selected"]
                    else "★" if is_recommended
                    else "✓" if row["selected"]
                    else ""
                )
                item = self.search_tree.insert("", "end", values=(
                    marker,
                    row["phase"],
                    row["scenario"],
                    row.get("solver_status") or (
                        "FAILED" if row["error"] else ""
                    ),
                    f"{row['maximum_travel_increase_percent']:g}",
                    f"{row['maximum_relocated_percent']:g}",
                    (
                        f"{row['controllable_peak']:.3f}"
                        if row.get("controllable_peak") is not None
                        else "—"
                    ),
                    (
                        f"{row['controllable_p95']:.3f}"
                        if row.get("controllable_p95") is not None
                        else "—"
                    ),
                    (
                        f"{row['neighbourhood_peak']:.3f}"
                        if row.get("neighbourhood_peak") is not None
                        else "—"
                    ),
                    row.get("relocation_count", "—"),
                ))
                self.search_trials_by_item[item] = trial
                if row["selected"]:
                    selected_item = item
            if selected_item is not None:
                self.search_tree.selection_set(selected_item)
                self.search_tree.focus(selected_item)
                self.search_tree.see(selected_item)
        self._populating_search_tree = False

    def select_search_trial(self, _event=None):
        if self._populating_search_tree or self.search_result is None:
            return
        selected_items = self.search_tree.selection()
        if not selected_items:
            return
        trial = self.search_trials_by_item.get(selected_items[0])
        if trial is None:
            return
        if not trial.accepted:
            self.status.set(
                f"Trial {trial.scenario.scenario_id} ({trial.phase}) failed "
                f"and has no layout to display: {trial.error}"
            )
            current = next(
                (
                    item for item, candidate
                    in self.search_trials_by_item.items()
                    if candidate is self.selected_search_trial
                ),
                None,
            )
            if current is not None:
                self._populating_search_tree = True
                self.search_tree.selection_set(current)
                self.search_tree.focus(current)
                self._populating_search_tree = False
            return
        if (
            trial is self.selected_search_trial
            and self.result is trial.result
        ):
            return
        self.selected_search_trial = trial
        self.complete(trial.result, self.network)
        recommendation = (
            " · auto-search recommendation"
            if trial is self.search_result.best_trial else ""
        )
        self.status.set(
            f"Viewing trial {trial.scenario.scenario_id} ({trial.phase})"
            f"{recommendation}. Save Layout will save this plan."
        )

    @staticmethod
    def _heat_colour(ratio):
        value = min(1.0, max(0.0, float(ratio)))
        if value < 0.5:
            blend = value / 0.5
            start, end = (50, 110, 180), (246, 190, 60)
        else:
            blend = (value - 0.5) / 0.5
            start, end = (246, 190, 60), (210, 55, 55)
        colour = tuple(
            round(start[index] + blend * (end[index] - start[index]))
            for index in range(3)
        )
        return "#" + "".join(f"{component:02x}" for component in colour)

    def _geometry(self):
        nodes = list(self.network.nodes.values())
        xs, ys = [node.x for node in nodes], [node.y for node in nodes]
        width = max(400, self.canvas.winfo_width())
        height = max(300, self.canvas.winfo_height())
        padding = 28
        scale = min(
            (width - 2 * padding) / max(max(xs) - min(xs), 1e-9),
            (height - 2 * padding) / max(max(ys) - min(ys), 1e-9),
        )
        return min(xs), min(ys), height, padding, scale

    @staticmethod
    def _point(node, geometry):
        min_x, min_y, height, padding, scale = geometry
        return (
            padding + (node.x - min_x) * scale,
            height - padding - (node.y - min_y) * scale,
        )

    def draw_map(self):
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        if self.result is None or self.network is None:
            self.canvas.create_text(
                max(200, self.canvas.winfo_width() / 2),
                max(150, self.canvas.winfo_height() / 2),
                text=(
                    "Run either global workflow to compare congestion "
                    "before and after."
                ),
                fill="#93a7b5",
            )
            self.app.apply_canvas_viewport(self.canvas)
            return
        analysis = (
            self.result.before
            if self.view_mode.get() == "Before"
            else self.result.after
        )
        assignment_rows = (
            self.result.baseline_payload["assignments"]
            if self.view_mode.get() == "Before"
            else self.result.assignments
        )
        geometry = self._geometry()
        resources = {
            row["resource_id"]: row for row in analysis.resources
        }
        controllable = set(
            self.result.solver.get("controllable_resources") or []
        )
        visible_resource_ids = (
            controllable
            if self.resource_view.get() == "Controllable"
            else set(resources)
        )
        positives = sorted(
            row["normalized_load"] for row in analysis.resources
            if (
                row["resource_id"] in visible_resource_ids
                and row["normalized_load"] > 0
            )
        )
        heat_max = (
            positives[min(len(positives) - 1, math.floor(0.95 * len(positives)))]
            if positives else 1.0
        )
        visible = {}
        for link in self.network.links:
            key = (tuple(sorted((link.start, link.end))), link.resource_id)
            visible.setdefault(key, link)
        for link in visible.values():
            start, end = (
                self.network.nodes[link.start],
                self.network.nodes[link.end],
            )
            x1, y1 = self._point(start, geometry)
            x2, y2 = self._point(end, geometry)
            load = resources.get(
                link.resource_id, {}
            ).get("normalized_load", 0)
            if link.resource_id not in visible_resource_ids:
                self.canvas.create_line(
                    x1, y1, x2, y2,
                    fill="#e6ebed",
                    width=1,
                    tags=("global_invariant_link",),
                )
                continue
            ratio = min(1.0, load / max(heat_max, 1e-9))
            self.canvas.create_line(
                x1, y1, x2, y2, fill="#d9e1e5", width=7,
                tags=("global_lane_background",),
            )
            self.canvas.create_line(
                x1, y1, x2, y2,
                fill=self._heat_colour(ratio),
                width=2 + 3 * math.sqrt(ratio),
                tags=("global_link", f"resource:{link.resource_id}"),
            )

        racks = {}
        for row in assignment_rows:
            if row.get("assignment_status") != "ASSIGNED":
                continue
            node_id = self.service.traffic._resolve_node(row, self.network)
            if node_id is None:
                continue
            rack = racks.setdefault(node_id, {
                "label": str(
                    row.get("static_bay_id")
                    or row.get("rack_id")
                    or node_id
                ),
                "units": set(),
            })
            rack["units"].add(str(row.get("handling_unit_id", "")))
        relocated = {
            row["handling_unit_id"] for row in self.result.relocations
        }
        for rack in racks.values():
            rack["visits"] = sum(
                self.result.demand.unit_visits.get(unit, 0)
                for unit in rack["units"]
            )
        positive_rack_visits = sorted(
            rack["visits"] for rack in racks.values()
            if rack["visits"] > 0
        )
        rack_heat_max = (
            positive_rack_visits[
                min(
                    len(positive_rack_visits) - 1,
                    math.floor(0.95 * len(positive_rack_visits)),
                )
            ]
            if positive_rack_visits else 1
        )
        for node_id, rack in racks.items():
            node = self.network.nodes[node_id]
            x, y = self._point(node, geometry)
            visits = rack["visits"]
            rack_ratio = min(
                1.0, visits / max(1, rack_heat_max)
            )
            rack_fill = (
                self._heat_colour(rack_ratio)
                if self.show_rack_heat.get() and visits > 0
                else "#eff4f5"
            )
            is_relocated = bool(rack["units"] & relocated)
            self.canvas.create_rectangle(
                x - 7, y - 7, x + 7, y + 7,
                fill=rack_fill,
                outline="#7b2cbf" if is_relocated else "#344f5c",
                width=3 if is_relocated else 1,
                tags=(
                    "global_rack",
                    "global_rack_heat",
                    f"rack_visits:{visits}",
                ),
            )
            self.canvas.create_text(
                x, y - 13,
                text=f"{rack['label']} · {visits:,}",
                fill="#56306a" if is_relocated else "#344f5c",
                font=("TkDefaultFont", 7, "bold" if is_relocated else "normal"),
            )
        for endpoint in self.network.endpoints:
            node = self.network.nodes[endpoint.node_id]
            x, y = self._point(node, geometry)
            self.canvas.create_polygon(
                x, y - 8, x + 8, y, x, y + 8, x - 8, y,
                fill="#224f88", outline="white", width=2,
                tags=("global_endpoint",),
            )
            self.canvas.create_text(
                x, y - 14,
                text=endpoint.endpoint_id or endpoint.node_id,
                fill="#224f88",
                font=("TkDefaultFont", 8, "bold"),
            )
        self.app.apply_canvas_viewport(self.canvas)

    def save(self):
        if self.result is None:
            return
        try:
            path = Path(self.output_path.get()).expanduser().resolve()
            if (
                self.search_result is not None
                and self.selected_search_trial is not None
            ):
                saved = self.search.save(
                    self.search_result,
                    path,
                    self.selected_search_trial,
                )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(self.result.output_payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                saved = None
        except (OSError, ValueError) as exc:
            messagebox.showerror("Save global layout", str(exc))
            return
        if saved is None:
            self.status.set(f"Saved global congestion layout to {path}")
        else:
            trial = self.selected_search_trial
            self.status.set(
                f"Saved selected trial {trial.scenario.scenario_id} "
                f"({trial.phase}) to {saved[0]}; comparison report: {saved[1]}"
            )
