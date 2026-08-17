"""Compact editor for the core storage settings inherited from each zone."""

from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, ttk

from .attributes import (
    PHYSICAL_ATTRIBUTE_KEYS,
    STANDARD_STORAGE_DEFAULTS,
    StorageAttributeService,
)


class ZoneStorageSettingsEditor(tk.Toplevel):
    """Edit chilled and physical capacities without exposing catalog details."""

    def __init__(
        self,
        parent,
        service: StorageAttributeService,
        zones: list[str],
        location_attributes: dict[str, dict],
        on_apply,
        attribute_catalog=None,
        warehouse_storage_defaults=None,
    ):
        super().__init__(parent)
        self.title("Zone Storage Settings")
        self.geometry("920x480")
        self.transient(parent)
        self.service = service
        self.zones = sorted(set(zones))
        self.location_attributes = deepcopy(location_attributes)
        self.on_apply = on_apply
        self.catalog = service.normalize_catalog(attribute_catalog)
        self.warehouse_storage_defaults = dict(
            warehouse_storage_defaults or STANDARD_STORAGE_DEFAULTS
        )
        self.active_keys = set(self.catalog)
        self.selected_zone = tk.StringVar()
        self.chilled = tk.BooleanVar(value=False)
        self.oversize_capable = tk.BooleanVar(value=False)
        self.capacity_values = {
            key: tk.StringVar() for key in PHYSICAL_ATTRIBUTE_KEYS
        }
        self.status = tk.StringVar(
            value=(
                "Slotting follows these saved zone values and does not create "
                "or split zones automatically."
            )
        )
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = (
            "zone", "chilled", "oversize", "length", "width", "height", "weight"
        )
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        labels = {
            "zone": "Zone", "chilled": "Chilled", "oversize": "Oversize capable",
            "length": "Max length",
            "width": "Max width", "height": "Max height",
            "weight": "Max weight",
        }
        widths = {
            "zone": 80, "chilled": 70, "oversize": 115,
            "length": 100, "width": 100,
            "height": 100, "weight": 100,
        }
        for column in columns:
            self.tree.heading(column, text=labels[column])
            self.tree.column(column, width=widths[column], anchor="center")
        self.tree.grid(row=0, column=0, columnspan=8, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._selected)

        ttk.Label(frame, text="Selected zone").grid(row=1, column=0, sticky="w", pady=(12, 3))
        ttk.Label(frame, textvariable=self.selected_zone).grid(row=2, column=0, sticky="w")
        chilled_check = ttk.Checkbutton(frame, text="Chilled area", variable=self.chilled)
        chilled_check.grid(
            row=2, column=1, sticky="w", padx=8
        )
        if "chilled" not in self.active_keys:
            chilled_check.configure(state="disabled")
        oversize_check = ttk.Checkbutton(
            frame, text="Oversize capable", variable=self.oversize_capable
        )
        oversize_check.grid(row=2, column=2, sticky="w", padx=8)
        if "oversize_capable" not in self.active_keys:
            oversize_check.configure(state="disabled")
        field_labels = {
            "max_item_length": "Max length (m)",
            "max_item_width": "Max width (m)",
            "max_item_height": "Max height (m)",
            "max_item_weight": "Max weight (kg)",
        }
        for index, key in enumerate(PHYSICAL_ATTRIBUTE_KEYS, start=3):
            ttk.Label(frame, text=field_labels[key]).grid(row=1, column=index, sticky="w", padx=4, pady=(12, 3))
            entry = ttk.Entry(frame, textvariable=self.capacity_values[key], width=12)
            entry.grid(
                row=2, column=index, sticky="ew", padx=4
            )
            if key not in self.active_keys:
                entry.configure(state="disabled")
        ttk.Button(frame, text="Update selected zone", command=self.update_selected).grid(
            row=2, column=7, sticky="ew", padx=(10, 0)
        )

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(14, 0))
        ttk.Button(actions, text="Reset all to standard", command=self.reset_standard).pack(side="left")
        ttk.Label(actions, textvariable=self.status, foreground="#4d646d").pack(side="left", padx=10)
        ttk.Button(actions, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="Save zone settings", command=self.commit).pack(side="right", padx=6)

        ttk.Label(
            frame,
            text=(
                "Only chilled storage is predefined by the user. Maximum fields "
                "may remain empty (unbounded), or be entered as planning inputs. "
                "Slotting does not generate oversize segments automatically. "
                "Dimensions use metres and weight uses kilograms. Production limits "
                "must be checked against the racks."
            ),
            foreground="#8a4b08",
            wraplength=870,
        ).grid(row=4, column=0, columnspan=8, sticky="w", pady=(12, 0))

    def _zone_values(self, zone: str) -> dict:
        return self.location_attributes.setdefault(zone, {})

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for zone in self.zones:
            values = self._zone_values(zone)
            self.tree.insert(
                "", "end", iid=zone,
                values=(
                    zone,
                    "N/A" if "chilled" not in self.active_keys
                    else "Yes" if values.get("chilled") is True else "No",
                    "N/A" if "oversize_capable" not in self.active_keys
                    else "Yes" if values.get("oversize_capable") is True else "No",
                    *(
                        "N/A" if key not in self.active_keys
                        else "" if values.get(key) is None else values.get(key, "")
                        for key in PHYSICAL_ATTRIBUTE_KEYS
                    ),
                ),
            )
        if self.zones:
            zone = self.selected_zone.get()
            if zone not in self.zones:
                zone = self.zones[0]
            self.tree.selection_set(zone)
            self.tree.focus(zone)
            self._selected()

    def _selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        zone = selection[0]
        values = self._zone_values(zone)
        self.selected_zone.set(zone)
        self.chilled.set(values.get("chilled") is True)
        self.oversize_capable.set(values.get("oversize_capable") is True)
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            value = values.get(key, "")
            self.capacity_values[key].set("" if value is None else str(value))

    def update_selected(self) -> bool:
        zone = self.selected_zone.get()
        if not zone:
            return False
        try:
            parsed = {}
            for key, variable in self.capacity_values.items():
                if key not in self.active_keys:
                    continue
                raw = variable.get().strip()
                parsed[key] = None if not raw else float(raw)
            if any(value is not None and value <= 0 for value in parsed.values()):
                raise ValueError("entered dimensions and weight limits must be greater than zero")
        except ValueError as exc:
            messagebox.showerror("Invalid zone capacity", str(exc), parent=self)
            return False
        values = self._zone_values(zone)
        values.update({
            key: None if value is None else int(value) if value.is_integer() else value
            for key, value in parsed.items()
        })
        if "chilled" in self.active_keys:
            values["chilled"] = self.chilled.get()
        if "oversize_capable" in self.active_keys:
            values["oversize_capable"] = self.oversize_capable.get()
        self.status.set(f"Updated {zone}; save settings to apply it.")
        self._refresh()
        return True

    def reset_standard(self) -> None:
        for zone in self.zones:
            values = self._zone_values(zone)
            values.update({
                key: value for key, value in self.warehouse_storage_defaults.items()
                if key in self.active_keys
            })
            if "chilled" in self.active_keys:
                values.setdefault("chilled", False)
            if "oversize_capable" in self.active_keys:
                values.setdefault("oversize_capable", False)
        self.status.set("Applied the warehouse storage defaults to every zone.")
        self._refresh()

    def commit(self) -> None:
        if self.selected_zone.get() and not self.update_selected():
            return
        self.on_apply(self.location_attributes)
        self.destroy()
