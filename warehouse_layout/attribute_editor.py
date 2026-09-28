"""Tk editor for typed attributes on the static storage hierarchy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from .attributes import (
    AttributeDefinition,
    CORE_ATTRIBUTE_KEYS,
    OVERSIZE_CAPABLE_KEY,
    PHYSICAL_ATTRIBUTE_KEYS,
    StorageAttributeService,
)


def confirm_inactive_attributes(parent, keys):
    """Return whether the user chose to save unconfigured flags as inactive."""
    dialog = tk.Toplevel(parent)
    dialog.title("Unassigned attributes")
    dialog.transient(parent)
    result = [False]
    ttk.Label(dialog, text="No zone value is assigned for: " + ", ".join(sorted(keys)), wraplength=500).pack(padx=20, pady=20)
    actions = ttk.Frame(dialog)
    actions.pack(padx=20, pady=(0, 20))
    def save():
        result[0] = True
        dialog.destroy()
    ttk.Button(actions, text="Back to edit", command=dialog.destroy).pack(side="left", padx=5)
    ttk.Button(actions, text="Save and make inactive", command=save).pack(side="left", padx=5)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result[0]


class HierarchyAttributeEditor(tk.Toplevel):
    """Edit the attribute catalog and local values without mutating on cancel."""

    LEVEL_NAMES = ("Zone", "Aisle", "Bay", "Level", "Slot")

    def __init__(
        self,
        parent,
        service: StorageAttributeService,
        catalog,
        location_attributes: dict[str, dict],
        hierarchy_paths: list[str],
        on_apply,
        *, source="", summary=None, on_import=None,
    ):
        super().__init__(parent)
        self.title("Layout Attributes")
        self.geometry("1380x800")
        self.transient(parent)
        self.service = service
        self.catalog = dict(service.normalize_catalog(catalog))
        self.location_attributes = deepcopy(location_attributes)
        self.enabled = {key for key, item in self.catalog.items() if item.enabled}
        self.hierarchy_paths = list(hierarchy_paths)
        self.on_apply = on_apply
        self.on_import = on_import
        self.source = source
        self.summary = summary or {}
        self.source_columns = {key: item.get("source_column", key)
                               for key, item in self.summary.get("attributes", {}).items()}
        self.import_status = tk.StringVar()

        self.attribute_key = tk.StringVar()
        self.attribute_label = tk.StringVar()
        self.attribute_type = tk.StringVar(value="boolean")
        self.attribute_match = tk.StringVar(value="exact")
        self.attribute_unit = tk.StringVar()
        self.attribute_choices = tk.StringVar()
        self.attribute_hierarchy = tk.StringVar()
        self.assignment_attribute = tk.StringVar()
        self.assignment_value = tk.StringVar()
        self.selection_status = tk.StringVar(
            value="Select one or more hierarchy nodes to assign an attribute."
        )
        self._build_ui()
        controls = ttk.Frame(self)
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        ttk.Button(controls, text="Enable / disable selected attribute", command=self.toggle_attribute).pack(side="left")
        ttk.Label(controls, text="Oversize-capable storage is always enabled.").pack(side="left", padx=12)
        ttk.Button(controls, text="Configure zones…", command=self.configure_zones).pack(side="right")
        self._refresh_catalog()
        self._populate_hierarchy()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def toggle_attribute(self):
        selected = self.catalog_tree.selection()
        if not selected or selected[0] == OVERSIZE_CAPABLE_KEY:
            return
        key = selected[0]
        if key in self.enabled:
            self.enabled.remove(key)
        else:
            self.enabled.add(key)
        self._refresh_catalog()

    def _click_enabled(self, event):
        if self.catalog_tree.identify_column(event.x) == "#1":
            key = self.catalog_tree.identify_row(event.y)
            if key:
                self.catalog_tree.selection_set(key)
                self.toggle_attribute()
                self.catalog_tree.selection_set(key)
                return "break"

    def import_csv(self):
        if self.on_import is None:
            return
        imported = self.on_import()
        if not imported:
            self.grab_set()
            return
        old_keys = set(self.catalog)
        self.catalog = dict(self.service.normalize_catalog(imported["catalog"]))
        self.enabled = (self.enabled & set(self.catalog)) | {
            key for key, item in self.catalog.items() if item.enabled and key not in old_keys
        }
        self.source = imported["source"]
        self.summary = imported["summary"]
        self.source_columns = {key: item.get("source_column", key)
                               for key, item in self.summary.get("attributes", {}).items()}
        self._refresh_catalog()
        self.grab_set()

    def configure_zones(self):
        from .zone_settings_editor import ZoneStorageSettingsEditor
        zones = [path for path in self.hierarchy_paths if "/" not in path]
        if not zones:
            messagebox.showinfo("Configure zones", "Assign racks to zones in the grid before configuring zone values.", parent=self)
            return
        def apply(values):
            self.location_attributes = deepcopy(values)
            self._refresh_catalog()
        editor = ZoneStorageSettingsEditor(
            self, self.service, zones, self.location_attributes, apply,
            {key: replace(item, enabled=key in self.enabled) for key, item in self.catalog.items()},
        )
        editor.grab_set()
        self.wait_window(editor)
        self.grab_set()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=2)
        self.columnconfigure(1, weight=3)
        self.rowconfigure(1, weight=1)
        header = ttk.LabelFrame(self, text="1. Import CSV → 2. Select attributes → 3. Configure zones", padding=10)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 0))
        ttk.Button(header, text="Import SKU attributes CSV…", command=self.import_csv,
                   state="normal" if self.on_import else "disabled").pack(side="left", padx=(0, 12))
        ttk.Label(header, textvariable=self.import_status).pack(side="left")

        hierarchy_frame = ttk.LabelFrame(
            self, text="Static hierarchy (Ctrl/Shift for bulk selection)", padding=8
        )
        hierarchy_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        hierarchy_frame.columnconfigure(0, weight=1)
        hierarchy_frame.rowconfigure(0, weight=1)
        self.hierarchy_tree = ttk.Treeview(
            hierarchy_frame,
            columns=("level",),
            show="tree headings",
            selectmode="extended",
        )
        self.hierarchy_tree.heading("#0", text="Address node")
        self.hierarchy_tree.heading("level", text="Level")
        self.hierarchy_tree.column("#0", width=270)
        self.hierarchy_tree.column("level", width=75, anchor="center")
        tree_scroll = ttk.Scrollbar(
            hierarchy_frame, orient="vertical", command=self.hierarchy_tree.yview
        )
        self.hierarchy_tree.configure(yscrollcommand=tree_scroll.set)
        self.hierarchy_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.hierarchy_tree.bind("<<TreeviewSelect>>", self._hierarchy_selected)

        right = ttk.Frame(self, padding=(0, 10, 10, 10))
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(2, weight=2)

        catalog_frame = ttk.LabelFrame(right, text="Layout attributes — click a checkbox to enable", padding=8)
        catalog_frame.grid(row=0, column=0, sticky="nsew")
        catalog_frame.columnconfigure(0, weight=1)
        catalog_frame.rowconfigure(0, weight=1)
        self.catalog_tree = ttk.Treeview(
            catalog_frame,
            columns=("enabled", "label", "status", "type", "source", "key", "match", "unit", "hierarchy"),
            show="headings",
            height=5,
        )
        headings = {
            "enabled": "Use",
            "status": "Zone configuration",
            "key": "Key",
            "label": "Label",
            "type": "Type",
            "match": "Matching",
            "unit": "Unit",
            "hierarchy": "Hierarchy level",
            "source": "CSV column",
        }
        for column in headings:
            self.catalog_tree.heading(column, text=headings[column])
            self.catalog_tree.column(column, width=120 if column == "key" else 105)
        self.catalog_tree.column("enabled", width=65, stretch=False, anchor="center")
        self.catalog_tree.column("label", width=180, stretch=False)
        self.catalog_tree.column("status", width=185, stretch=False)
        self.catalog_tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
        self.catalog_tree.bind("<<TreeviewSelect>>", self._catalog_selected)
        self.catalog_tree.bind("<Button-1>", self._click_enabled)
        scroll = ttk.Scrollbar(catalog_frame, orient="horizontal", command=self.catalog_tree.xview)
        scroll.grid(row=6, column=0, columnspan=6, sticky="ew")
        self.catalog_tree.configure(xscrollcommand=scroll.set)
        vertical = ttk.Scrollbar(catalog_frame, orient="vertical", command=self.catalog_tree.yview)
        vertical.grid(row=0, column=6, sticky="ns")
        self.catalog_tree.configure(yscrollcommand=vertical.set)

        fields = (
            ("Key", self.attribute_key),
            ("Label", self.attribute_label),
            ("Unit", self.attribute_unit),
        )
        for index, (label, variable) in enumerate(fields):
            column = (index % 2) * 3
            row = 1 + index // 2
            ttk.Label(catalog_frame, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 5), pady=3
            )
            ttk.Entry(catalog_frame, textvariable=variable).grid(
                row=row, column=column + 1, columnspan=2, sticky="ew", pady=3
            )
        ttk.Label(catalog_frame, text="Value type").grid(
            row=3, column=0, sticky="w", pady=3
        )
        ttk.Combobox(
            catalog_frame,
            textvariable=self.attribute_type,
            values=("boolean", "number"),
            state="disabled",
            width=13,
        ).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(catalog_frame, text="Matching rule").grid(
            row=3, column=3, sticky="w", pady=3
        )
        ttk.Combobox(
            catalog_frame,
            textvariable=self.attribute_match,
            values=("exact", "capacity"),
            state="disabled",
            width=13,
        ).grid(row=3, column=4, sticky="w", pady=3)
        ttk.Label(catalog_frame, text="Zone hierarchy level").grid(
            row=4, column=0, sticky="w", pady=3
        )
        ttk.Spinbox(
            catalog_frame,
            textvariable=self.attribute_hierarchy,
            from_=1,
            to=99,
            width=8,
        ).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(
            catalog_frame,
            text="Hierarchy level controls attribute ordering.",
            foreground="#4d646d",
        ).grid(row=4, column=2, columnspan=4, sticky="w", padx=(6, 0))
        ttk.Button(
            catalog_frame, text="Update details", command=self.save_definition
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 2))

        assignment_frame = ttk.LabelFrame(
            right, text="Local value for selected nodes", padding=8
        )
        assignment_frame.grid(row=1, column=0, sticky="ew", pady=8)
        assignment_frame.columnconfigure(1, weight=1)
        ttk.Label(assignment_frame, text="Attribute").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.assignment_attribute_box = ttk.Combobox(
            assignment_frame,
            textvariable=self.assignment_attribute,
            state="readonly",
        )
        self.assignment_attribute_box.grid(row=0, column=1, sticky="ew")
        ttk.Label(assignment_frame, text="Value").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=5
        )
        ttk.Entry(assignment_frame, textvariable=self.assignment_value).grid(
            row=1, column=1, sticky="ew", pady=5
        )
        ttk.Button(
            assignment_frame,
            text="Apply to selected nodes",
            command=self.apply_local_value,
        ).grid(row=2, column=1, sticky="w")
        ttk.Button(
            assignment_frame,
            text="Clear local value",
            command=self.clear_local_value,
        ).grid(row=2, column=2, sticky="w", padx=(6, 0))
        ttk.Label(
            assignment_frame,
            textvariable=self.selection_status,
            foreground="#4d646d",
            wraplength=620,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(7, 0))

        effective_frame = ttk.LabelFrame(
            right, text="Effective values for focused node", padding=8
        )
        effective_frame.grid(row=2, column=0, sticky="nsew")
        effective_frame.columnconfigure(0, weight=1)
        effective_frame.rowconfigure(0, weight=1)
        self.effective_tree = ttk.Treeview(
            effective_frame,
            columns=("attribute", "local", "effective", "source"),
            show="headings",
            height=7,
        )
        for column, label, width in (
            ("attribute", "Attribute", 130),
            ("local", "Local value", 100),
            ("effective", "Effective value", 110),
            ("source", "Inherited / local source", 260),
        ):
            self.effective_tree.heading(column, text=label)
            self.effective_tree.column(column, width=width)
        self.effective_tree.grid(row=0, column=0, sticky="nsew")

        actions = ttk.Frame(right)
        actions.grid(row=3, column=0, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="left")
        ttk.Button(actions, text="Save changes", command=self.commit).pack(
            side="left", padx=(7, 0)
        )

    def _populate_hierarchy(self) -> None:
        self.hierarchy_tree.delete(*self.hierarchy_tree.get_children())
        for path in self.hierarchy_paths:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            label = path.rsplit("/", 1)[-1]
            level_index = min(path.count("/"), len(self.LEVEL_NAMES) - 1)
            self.hierarchy_tree.insert(
                parent, "end", iid=path, text=label,
                values=(self.LEVEL_NAMES[level_index],), open=level_index < 2,
            )

    def _refresh_catalog(self) -> None:
        zones = [path for path in self.hierarchy_paths if "/" not in path]
        self.import_status.set(
            f"{Path(self.source).name if self.source else 'No CSV imported'} · "
            f"{int(self.summary.get('sku_count', 0)):,} SKUs · "
            f"{len(self.enabled - {OVERSIZE_CAPABLE_KEY})} active CSV attributes"
        )
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        ordered_keys = sorted(
            self.catalog,
            key=lambda key: (
                self.catalog[key].hierarchy_level is None,
                self.catalog[key].hierarchy_level or 10**9,
                key,
            ),
        )
        for key in ordered_keys:
            definition = self.catalog[key]
            count = sum(self.location_attributes.get(zone, {}).get(key) is not None for zone in zones)
            configuration = f"Configured in {count} of {len(zones)} zones" if count else "Unassigned"
            self.catalog_tree.insert(
                "", "end", iid=key,
                values=(
                    "Required" if key == OVERSIZE_CAPABLE_KEY else "☑" if key in self.enabled else "☐",
                    definition.label + (" (System)" if key == OVERSIZE_CAPABLE_KEY else ""),
                    configuration,
                    definition.value_type,
                    self.source_columns.get(key, "System" if key == OVERSIZE_CAPABLE_KEY else key),
                    definition.key,
                    definition.match_rule,
                    definition.unit,
                    definition.hierarchy_level or "Physical / system",
                ),
            )
        keys = sorted(self.enabled)
        self.assignment_attribute_box.configure(values=keys)
        if self.assignment_attribute.get() not in self.enabled:
            self.assignment_attribute.set(keys[0] if keys else "")
        self._refresh_effective_values()

    def clear_definition_form(self) -> None:
        self.attribute_key.set("")
        self.attribute_label.set("")
        self.attribute_type.set("boolean")
        self.attribute_match.set("exact")
        self.attribute_unit.set("")
        self.attribute_choices.set("")
        self.attribute_hierarchy.set("")

    def _catalog_selected(self, _event=None) -> None:
        selection = self.catalog_tree.selection()
        if not selection:
            return
        definition = self.catalog[selection[0]]
        self.attribute_key.set(definition.key)
        self.attribute_label.set(definition.label)
        self.attribute_type.set(definition.value_type)
        self.attribute_match.set(definition.match_rule)
        self.attribute_unit.set(definition.unit)
        self.attribute_choices.set(", ".join(definition.choices))
        self.attribute_hierarchy.set(
            "" if definition.hierarchy_level is None
            else str(definition.hierarchy_level)
        )
        self.assignment_attribute.set(definition.key)

    def save_definition(self) -> None:
        try:
            key = self.attribute_key.get().strip()
            if key not in self.catalog:
                raise ValueError("Import a CSV to discover attributes before editing them")
            hierarchy_raw = self.attribute_hierarchy.get().strip()
            if key in PHYSICAL_ATTRIBUTE_KEYS:
                value_type = "number"
                match_rule = "capacity"
                hierarchy_level = None
            elif key == OVERSIZE_CAPABLE_KEY:
                value_type = "boolean"
                match_rule = "exact"
                hierarchy_level = None
            else:
                value_type = "boolean"
                match_rule = "exact"
                hierarchy_level = (
                    int(hierarchy_raw)
                    if hierarchy_raw else 1 + max(
                        (
                            definition.hierarchy_level or 0
                            for definition in self.catalog.values()
                        ),
                        default=0,
                    )
                )
            definition = AttributeDefinition(
                key=key,
                label=self.attribute_label.get().strip(),
                value_type=value_type,
                match_rule=match_rule,
                unit=self.attribute_unit.get().strip(),
                hierarchy_level=hierarchy_level,
            )
            definition.validate()
            old = self.catalog.get(definition.key)
            if old and old.value_type != definition.value_type:
                for path, values in self.location_attributes.items():
                    if definition.key in values:
                        values[definition.key] = self.service.parse_value(
                            definition, values[definition.key]
                        )
            self.catalog[definition.key] = definition
        except ValueError as exc:
            messagebox.showerror("Invalid attribute", str(exc), parent=self)
            return
        self._refresh_catalog()
        self.catalog_tree.selection_set(definition.key)
        self.assignment_attribute.set(definition.key)

    def delete_definition(self) -> None:
        key = self.attribute_key.get().strip()
        if key not in self.catalog:
            return
        if key in CORE_ATTRIBUTE_KEYS:
            messagebox.showinfo(
                "Core attribute",
                f"'{key}' is required by automated physical and chilled slotting.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Delete attribute",
            f"Delete '{key}' and all local values assigned to it?",
            parent=self,
        ):
            return
        del self.catalog[key]
        for path in list(self.location_attributes):
            self.location_attributes[path].pop(key, None)
            if not self.location_attributes[path]:
                del self.location_attributes[path]
        self.clear_definition_form()
        self._refresh_catalog()

    def _hierarchy_selected(self, _event=None) -> None:
        selected = self.hierarchy_tree.selection()
        if selected:
            self.selection_status.set(
                f"{len(selected)} node(s) selected. Focus: {selected[-1]}"
            )
        self._refresh_effective_values()

    def _refresh_effective_values(self) -> None:
        if not hasattr(self, "effective_tree"):
            return
        self.effective_tree.delete(*self.effective_tree.get_children())
        selected = self.hierarchy_tree.selection()
        if not selected:
            return
        path = selected[-1]
        local = self.location_attributes.get(path, {})
        effective, sources = self.service.effective_attributes(
            path, self.location_attributes
        )
        for key in sorted(self.catalog):
            source = sources.get(key, "")
            source_text = ""
            if source:
                source_text = "local" if source == path else source
            self.effective_tree.insert(
                "", "end", values=(
                    self.catalog[key].label,
                    local.get(key, ""),
                    effective.get(key, ""),
                    source_text,
                )
            )

    def apply_local_value(self) -> None:
        paths = self.hierarchy_tree.selection()
        key = self.assignment_attribute.get()
        if not paths:
            messagebox.showinfo("Select nodes", "Select at least one hierarchy node.", parent=self)
            return
        if key not in self.enabled:
            messagebox.showinfo("Select attribute", "Select an attribute.", parent=self)
            return
        try:
            value = self.service.parse_value(
                self.catalog[key], self.assignment_value.get()
            )
        except ValueError as exc:
            messagebox.showerror("Invalid value", str(exc), parent=self)
            return
        for path in paths:
            self.location_attributes.setdefault(path, {})[key] = value
        self.selection_status.set(
            f"Set {key}={value} locally on {len(paths)} node(s)."
        )
        self._refresh_catalog()

    def clear_local_value(self) -> None:
        paths = self.hierarchy_tree.selection()
        key = self.assignment_attribute.get()
        if not paths or key not in self.catalog:
            return
        changed = 0
        for path in paths:
            values = self.location_attributes.get(path)
            if values and key in values:
                del values[key]
                changed += 1
                if not values:
                    del self.location_attributes[path]
        self.selection_status.set(
            f"Cleared {changed} local value(s); inherited values now apply."
        )
        self._refresh_catalog()

    def commit(self) -> None:
        from dataclasses import replace
        unassigned = [key for key in self.enabled if key != OVERSIZE_CAPABLE_KEY
                      and not any("/" not in path and values.get(key) is not None
                                  for path, values in self.location_attributes.items())]
        if unassigned:
            if not confirm_inactive_attributes(self, unassigned):
                return
            self.enabled.difference_update(unassigned)
        self.catalog = {key: replace(item, enabled=key in self.enabled)
                        for key, item in self.catalog.items()}
        try:
            normalized = self.service.validate_location_attributes(
                self.location_attributes, self.catalog, self.hierarchy_paths, ignore_unknown=True
            )
        except ValueError as exc:
            messagebox.showerror("Invalid attributes", str(exc), parent=self)
            return
        for path, values in normalized.items():
            self.location_attributes.setdefault(path, {}).update(values)
        self.on_apply(dict(self.catalog), self.location_attributes)
        self.destroy()
