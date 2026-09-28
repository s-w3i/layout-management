"""Edit active attributes on one or several warehouse zones."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk, messagebox


class ZoneStorageSettingsEditor(tk.Toplevel):
    def __init__(self, parent, service, zones, location_attributes, on_apply,
                 attribute_catalog=None, warehouse_storage_defaults=None):
        super().__init__(parent)
        self.title("Zone Settings")
        self.geometry("1180x740")
        self.minsize(850, 550)
        self.transient(parent)
        self.service = service
        self.location_attributes = deepcopy(location_attributes)
        self.on_apply = on_apply
        self.catalog = {key: item for key, item in service.normalize_catalog(attribute_catalog).items() if item.enabled}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.zone_ids = sorted(set(zones))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.comparison_status = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.comparison_status).pack(side="left")
        self.differences_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Show differing attributes only", variable=self.differences_only,
                        command=self.refresh_comparison).pack(side="right")
        comparison = ttk.Frame(frame)
        comparison.grid(row=1, column=0, sticky="nsew")
        comparison.columnconfigure(0, weight=1)
        comparison.rowconfigure(0, weight=1)
        self.zones = ttk.Treeview(comparison, columns=("zone", *("attribute_" + key for key in self.catalog)),
                                  show="headings", selectmode="extended", height=10)
        self.zones.heading("zone", text="Zone")
        self.zones.column("zone", width=110, minwidth=90, stretch=False, anchor="w")
        for key, definition in self.catalog.items():
            self.zones.column("attribute_" + key, width=max(145, len(definition.label) * 7), minwidth=110,
                              stretch=False, anchor="center")
        self.zones.tag_configure("even", background="#f0f5f7")
        self.zones.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(comparison, orient="vertical", command=self.zones.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(comparison, orient="horizontal", command=self.zones.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.zones.configure(xscrollcommand=horizontal.set)
        self.zones.configure(yscrollcommand=vertical.set)
        for index, zone in enumerate(self.zone_ids):
            self.zones.insert("", "end", iid=zone, tags=("even",) if index % 2 == 0 else ())
        ttk.Label(frame, text="Columns marked • differ between zones. Unset means no value is configured. Ctrl/Shift selects zones for bulk editing.").grid(row=2, column=0, sticky="w", pady=8)
        right = ttk.LabelFrame(frame, text="Edit selected zones", padding=8)
        self.edit_frame = right
        right.grid(row=3, column=0, sticky="ew")
        self.fields, self.sources = {}, {}
        for row, (key, definition) in enumerate(self.catalog.items()):
            ttk.Label(right, text=definition.label + (f" ({definition.unit})" if definition.unit else "")).grid(row=row, column=0, sticky="w", pady=4)
            variable = tk.StringVar()
            self.fields[key] = variable
            widget = (ttk.Combobox(right, textvariable=variable, values=("", "Yes", "No"), state="readonly", width=12)
                      if definition.value_type == "boolean" else ttk.Entry(right, textvariable=variable, width=14))
            widget.grid(row=row, column=1, padx=8)
            ttk.Button(right, text="Apply", command=lambda k=key: self.apply_value(k)).grid(row=row, column=2)
            ttk.Button(right, text="Clear", command=lambda k=key: self.clear_value(k)).grid(row=row, column=3, padx=4)
            self.sources[key] = tk.StringVar()
            ttk.Label(right, textvariable=self.sources[key]).grid(row=row, column=4, sticky="w")
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(actions, text="Apply each edit to update the comparison table, then save.").pack(side="left")
        ttk.Button(actions, text="Save zone settings", command=self.commit).pack(side="right")
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.zones.bind("<<TreeviewSelect>>", self.refresh)
        self.refresh_comparison()
        if self.zone_ids:
            self.zones.selection_set(self.zone_ids[0])
            self.refresh()

    def selected(self):
        return list(self.zones.selection())

    @staticmethod
    def display_value(value):
        return "Yes" if value is True else "No" if value is False else "Unset" if value is None else str(value)

    def refresh_comparison(self):
        effective = {zone: self.service.effective_attributes(zone, self.location_attributes)[0]
                     for zone in self.zone_ids}
        differing = []
        for key, definition in self.catalog.items():
            varies = len({values.get(key) for values in effective.values()}) > 1
            if varies:
                differing.append("attribute_" + key)
            label = definition.label + (f" ({definition.unit})" if definition.unit else "")
            self.zones.heading("attribute_" + key, text=label + (" •" if varies else ""))
        for zone, values in effective.items():
            self.zones.item(zone, values=(zone, *(self.display_value(values.get(key)) for key in self.catalog)))
        self.zones.configure(displaycolumns=("zone", *differing) if self.differences_only.get() else "#all")
        self.comparison_status.set(f"{len(self.zone_ids)} zones · {len(differing)} attributes differ")

    def refresh(self, event=None):
        selected = self.selected()
        self.edit_frame.configure(text="Edit selected zones: " + (", ".join(selected) if len(selected) <= 4 else f"{len(selected)} zones"))
        for key in self.catalog:
            resolved = [self.service.effective_attributes(zone, self.location_attributes) for zone in self.selected()]
            actual = [values.get(key) for values, _ in resolved]
            mixed = bool(actual) and any(item != actual[0] for item in actual)
            value = actual[0] if actual and not mixed else None
            self.fields[key].set("Yes" if value is True else "No" if value is False else "" if value is None else str(value))
            self.sources[key].set("Mixed values" if mixed else "Unset" if value is None else ", ".join(sorted({sources.get(key, "unset") for _, sources in resolved})))

    def apply_value(self, key):
        raw = self.fields[key].get().strip()
        if not raw:
            return self.clear_value(key)
        try:
            value = self.service.parse_value(self.catalog[key], raw)
            if self.catalog[key].value_type == "number" and value <= 0:
                raise ValueError("Capacity must be greater than zero")
        except ValueError as exc:
            messagebox.showerror("Invalid zone value", str(exc), parent=self)
            return
        for zone in self.selected():
            self.location_attributes.setdefault(zone, {})[key] = value
        self.refresh_comparison()
        self.refresh()

    def clear_value(self, key):
        for zone in self.selected():
            self.location_attributes.get(zone, {}).pop(key, None)
        self.refresh_comparison()
        self.refresh()

    def commit(self):
        if self.on_apply(self.location_attributes) is not False:
            self.destroy()
