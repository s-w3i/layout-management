"""Typed, inheritable storage-location attributes and compatibility rules."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable


ATTRIBUTE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VALUE_TYPES = ("boolean", "number", "text", "choice")
MATCH_RULES = ("exact", "capacity")
PHYSICAL_DIMENSION_KEYS = (
    "max_item_length",
    "max_item_width",
    "max_item_height",
)
PHYSICAL_WEIGHT_KEY = "max_item_weight"
PHYSICAL_ATTRIBUTE_KEYS = (*PHYSICAL_DIMENSION_KEYS, PHYSICAL_WEIGHT_KEY)
CORE_ATTRIBUTE_KEYS = ("chilled", *PHYSICAL_ATTRIBUTE_KEYS)
STANDARD_STORAGE_DEFAULTS = {
    "max_item_length": 15,
    "max_item_width": 16,
    "max_item_height": 13,
    "max_item_weight": 250,
}
OVERSIZE_STORAGE_DEFAULTS = {
    "max_item_length": 150,
    "max_item_width": 50,
    "max_item_height": 95,
    "max_item_weight": 640,
}


@dataclass(frozen=True)
class AttributeDefinition:
    """Definition shared by locations and SKU requirement columns."""

    key: str
    label: str
    value_type: str
    match_rule: str = "exact"
    unit: str = ""
    choices: tuple[str, ...] = ()

    def validate(self) -> None:
        if not ATTRIBUTE_KEY_PATTERN.fullmatch(self.key):
            raise ValueError(
                f"attribute key '{self.key}' must start with a lowercase letter and "
                "contain only lowercase letters, numbers, and underscores"
            )
        if not self.label.strip():
            raise ValueError(f"attribute '{self.key}' must have a label")
        if self.value_type not in VALUE_TYPES:
            raise ValueError(
                f"attribute '{self.key}' has unsupported type '{self.value_type}'"
            )
        if self.match_rule not in MATCH_RULES:
            raise ValueError(
                f"attribute '{self.key}' has unsupported matching rule "
                f"'{self.match_rule}'"
            )
        if self.match_rule == "capacity" and self.value_type != "number":
            raise ValueError(
                f"attribute '{self.key}' can use capacity matching only with number type"
            )
        if self.value_type == "choice" and not self.choices:
            raise ValueError(f"choice attribute '{self.key}' must define choices")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value_type": self.value_type,
            "match_rule": self.match_rule,
            "unit": self.unit,
            "choices": list(self.choices),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AttributeDefinition":
        definition = cls(
            key=str(value.get("key", "")).strip(),
            label=str(value.get("label", "")).strip(),
            value_type=str(value.get("value_type", "text")).strip().lower(),
            match_rule=str(value.get("match_rule", "exact")).strip().lower(),
            unit=str(value.get("unit", "")).strip(),
            choices=tuple(
                str(choice).strip()
                for choice in value.get("choices", [])
                if str(choice).strip()
            ),
        )
        definition.validate()
        return definition


class StorageAttributeService:
    """Build hierarchy paths, resolve inheritance, and match SKU requirements."""

    @staticmethod
    def starter_catalog() -> dict[str, AttributeDefinition]:
        definitions = (
            AttributeDefinition("chilled", "Chilled", "boolean", "exact"),
            AttributeDefinition(
                "max_item_length", "Maximum item length", "number", "capacity",
                "source length unit",
            ),
            AttributeDefinition(
                "max_item_width", "Maximum item width", "number", "capacity",
                "source length unit",
            ),
            AttributeDefinition(
                "max_item_height", "Maximum item height", "number", "capacity",
                "source length unit",
            ),
            AttributeDefinition(
                "max_item_weight", "Maximum item weight", "number", "capacity",
                "source weight unit",
            ),
        )
        return {definition.key: definition for definition in definitions}

    @staticmethod
    def has_physical_catalog(catalog) -> bool:
        definitions = StorageAttributeService.normalize_catalog(catalog)
        return all(key in definitions for key in PHYSICAL_ATTRIBUTE_KEYS)

    @staticmethod
    def physical_profile(
        requirements: dict[str, Any] | None,
        standard_defaults: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Classify one-unit physical requirements against standard storage."""
        requirements = requirements or {}
        defaults = standard_defaults or STANDARD_STORAGE_DEFAULTS
        values: dict[str, float] = {}
        missing: list[str] = []
        for key in PHYSICAL_ATTRIBUTE_KEYS:
            raw = requirements.get(key)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value) or value <= 0:
                missing.append(key)
            else:
                values[key] = value
        if missing:
            return {
                "data_status": "MISSING",
                "storage_class": "UNVERIFIED_OVERSIZE",
                "missing_fields": missing,
                "values": values,
            }
        item_dimensions = sorted(values[key] for key in PHYSICAL_DIMENSION_KEYS)
        standard_dimensions = sorted(float(defaults[key]) for key in PHYSICAL_DIMENSION_KEYS)
        oversize = any(
            item > capacity
            for item, capacity in zip(item_dimensions, standard_dimensions)
        )
        overweight = values[PHYSICAL_WEIGHT_KEY] > float(
            defaults[PHYSICAL_WEIGHT_KEY]
        )
        if oversize and overweight:
            storage_class = "OVERSIZE_AND_OVERWEIGHT"
        elif oversize:
            storage_class = "OVERSIZE"
        elif overweight:
            storage_class = "OVERWEIGHT"
        else:
            storage_class = "STANDARD"
        return {
            "data_status": "COMPLETE",
            "storage_class": storage_class,
            "missing_fields": [],
            "values": values,
        }

    @staticmethod
    def is_oversize_location(
        effective: dict[str, Any],
        standard_defaults: dict[str, float] | None = None,
    ) -> bool:
        """Return whether a location provides capacity above the standard profile."""
        defaults = standard_defaults or STANDARD_STORAGE_DEFAULTS
        try:
            return all(
                float(effective[key]) > 0 for key in PHYSICAL_ATTRIBUTE_KEYS
            ) and any(
                float(effective[key]) > float(defaults[key])
                for key in PHYSICAL_ATTRIBUTE_KEYS
            )
        except (KeyError, TypeError, ValueError):
            return False

    def evaluate_location(
        self,
        requirements: dict[str, Any] | None,
        effective: dict[str, Any],
        catalog,
        *,
        allow_unverified: bool = True,
        standard_defaults: dict[str, float] | None = None,
        requirements_are_normalized: bool = False,
        physical_profile: dict[str, Any] | None = None,
    ) -> tuple[bool, list[str], str]:
        """Evaluate generic attributes plus rotation-aware physical constraints."""
        definitions = self.normalize_catalog(catalog)
        requirements = (
            requirements or {}
            if requirements_are_normalized
            else self.validate_requirements(requirements or {}, definitions)
        )
        physical_enabled = all(key in definitions for key in PHYSICAL_ATTRIBUTE_KEYS)
        profile = physical_profile or self.physical_profile(
            requirements, standard_defaults
        )
        issues: list[str] = []

        generic_requirements = {
            key: value
            for key, value in requirements.items()
            if key not in PHYSICAL_ATTRIBUTE_KEYS
        }
        issues.extend(
            self.compatibility_issues(generic_requirements, effective, definitions)
        )
        if not physical_enabled:
            return not issues, issues, "COMPATIBLE" if not issues else "INCOMPATIBLE"

        oversize_location = self.is_oversize_location(effective, standard_defaults)
        if profile["data_status"] == "MISSING":
            missing_labels = [
                definitions[key].label for key in profile["missing_fields"]
            ]
            if not allow_unverified:
                issues.append("physical data is incomplete: " + ", ".join(missing_labels))
            elif not oversize_location:
                issues.append(
                    "physical data is incomplete and the target is not oversize-capable: "
                    + ", ".join(missing_labels)
                )
            if issues:
                return False, issues, "INCOMPATIBLE"
            return True, [
                "physical fit is unverified; missing " + ", ".join(missing_labels)
            ], "UNVERIFIED"

        for key in PHYSICAL_ATTRIBUTE_KEYS:
            if key not in effective:
                issues.append(f"{definitions[key].label}: location value is not defined")
        if not any(key not in effective for key in PHYSICAL_ATTRIBUTE_KEYS):
            item_dimensions = sorted(
                float(requirements[key]) for key in PHYSICAL_DIMENSION_KEYS
            )
            location_dimensions = sorted(
                float(effective[key]) for key in PHYSICAL_DIMENSION_KEYS
            )
            if any(
                item > capacity
                for item, capacity in zip(item_dimensions, location_dimensions)
            ):
                issues.append(
                    "item dimensions "
                    + " × ".join(f"{value:g}" for value in item_dimensions)
                    + " do not fit location dimensions "
                    + " × ".join(f"{value:g}" for value in location_dimensions)
                    + " in any allowed rotation"
                )
            if float(requirements[PHYSICAL_WEIGHT_KEY]) > float(
                effective[PHYSICAL_WEIGHT_KEY]
            ):
                issues.append(
                    f"Maximum item weight: requires {requirements[PHYSICAL_WEIGHT_KEY]} "
                    f"source weight unit, location provides "
                    f"{effective[PHYSICAL_WEIGHT_KEY]} source weight unit"
                )
        return not issues, issues, "COMPATIBLE" if not issues else "INCOMPATIBLE"

    @staticmethod
    def hard_compatibility_issues(
        requirements: dict[str, Any] | None, effective: dict[str, Any]
    ) -> list[str]:
        """Only chilled/ambient separation is a non-overridable allocation rule."""
        requirements = requirements or {}
        if "chilled" not in requirements:
            return []
        if "chilled" not in effective:
            return ["Chilled: location value is not defined"]
        if effective["chilled"] is not requirements["chilled"]:
            return [
                f"Chilled: requires {requirements['chilled']}, "
                f"location is {effective['chilled']}"
            ]
        return []

    def required_local_overrides(
        self,
        requirements: dict[str, Any] | None,
        effective: dict[str, Any],
        catalog,
    ) -> dict[str, Any]:
        """Return child-local values needed to satisfy every soft requirement."""
        definitions = self.normalize_catalog(catalog)
        requirements = requirements or {}
        overrides: dict[str, Any] = {}

        dimension_requirements = {
            key: requirements[key]
            for key in PHYSICAL_DIMENSION_KEYS
            if key in requirements
        }
        dimensions_fit = False
        if len(dimension_requirements) == len(PHYSICAL_DIMENSION_KEYS) and all(
            key in effective for key in PHYSICAL_DIMENSION_KEYS
        ):
            item_dimensions = sorted(
                float(dimension_requirements[key])
                for key in PHYSICAL_DIMENSION_KEYS
            )
            location_dimensions = sorted(
                float(effective[key]) for key in PHYSICAL_DIMENSION_KEYS
            )
            dimensions_fit = all(
                item <= capacity
                for item, capacity in zip(item_dimensions, location_dimensions)
            )
        if not dimensions_fit:
            for key, required in dimension_requirements.items():
                try:
                    actual = float(effective.get(key, 0))
                except (TypeError, ValueError):
                    actual = 0
                if actual < float(required):
                    overrides[key] = required

        for key, required in requirements.items():
            if key == "chilled" or key in PHYSICAL_DIMENSION_KEYS:
                continue
            definition = definitions.get(key)
            if definition is None:
                continue
            actual = effective.get(key)
            if definition.match_rule == "capacity":
                try:
                    satisfied = float(actual) >= float(required)
                except (TypeError, ValueError):
                    satisfied = False
            else:
                satisfied = actual == required
            if not satisfied:
                overrides[key] = required
        return overrides

    @staticmethod
    def normalize_catalog(
        catalog: dict[str, Any] | Iterable[dict[str, Any] | AttributeDefinition] | None,
    ) -> dict[str, AttributeDefinition]:
        if not catalog:
            return {}
        if isinstance(catalog, dict) and all(
            isinstance(value, AttributeDefinition) for value in catalog.values()
        ):
            return catalog
        values = catalog.values() if isinstance(catalog, dict) else catalog
        normalized: dict[str, AttributeDefinition] = {}
        for value in values:
            definition = (
                value
                if isinstance(value, AttributeDefinition)
                else AttributeDefinition.from_dict(value)
            )
            definition.validate()
            if definition.key in normalized:
                raise ValueError(f"duplicate attribute key: {definition.key}")
            normalized[definition.key] = definition
        return normalized

    @classmethod
    def serialize_catalog(cls, catalog) -> list[dict[str, Any]]:
        normalized = cls.normalize_catalog(catalog)
        return [normalized[key].to_dict() for key in sorted(normalized)]

    @staticmethod
    def hierarchy_paths(
        racks: list[dict], levels_per_rack: int, slots_per_level: int
    ) -> list[str]:
        if levels_per_rack < 1 or slots_per_level < 1:
            raise ValueError("levels and slots per level must be at least 1")
        paths: set[str] = set()
        for rack in racks:
            zone = str(rack.get("zone_id", "")).strip()
            aisle = str(rack.get("aisle_id", "")).strip()
            bay = str(rack.get("static_bay_id", "")).strip()
            if not zone or not aisle or not bay:
                raise ValueError("rack hierarchy is missing zone, aisle, or bay")
            zone_path = zone
            aisle_path = f"{zone_path}/{aisle}"
            bay_path = f"{aisle_path}/{bay}"
            paths.update((zone_path, aisle_path, bay_path))
            for level in range(1, levels_per_rack + 1):
                level_path = f"{bay_path}/L{level:02d}"
                paths.add(level_path)
                for slot in range(1, slots_per_level + 1):
                    paths.add(f"{level_path}/S{slot:02d}")
        return sorted(paths, key=lambda path: (path.count("/"), path))

    @staticmethod
    def ancestors(path: str) -> list[str]:
        parts = [part for part in path.split("/") if part]
        return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]

    def effective_attributes(
        self, path: str, location_attributes: dict[str, dict[str, Any]] | None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        effective: dict[str, Any] = {}
        sources: dict[str, str] = {}
        local_by_path = location_attributes or {}
        for ancestor in self.ancestors(path):
            local = local_by_path.get(ancestor, {})
            if not isinstance(local, dict):
                raise ValueError(f"location attributes for '{ancestor}' must be an object")
            for key, value in local.items():
                effective[key] = value
                sources[key] = ancestor
        return effective, sources

    @staticmethod
    def parse_value(definition: AttributeDefinition, raw: Any) -> Any:
        if definition.value_type == "boolean":
            if isinstance(raw, bool):
                return raw
            value = str(raw).strip().lower()
            if value in {"true", "yes", "y", "1"}:
                return True
            if value in {"false", "no", "n", "0"}:
                return False
            raise ValueError(
                f"{definition.label} must be true/false, yes/no, or 1/0"
            )
        if definition.value_type == "number":
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{definition.label} must be a number") from exc
            if not math.isfinite(value):
                raise ValueError(f"{definition.label} must be a finite number")
            return int(value) if value.is_integer() else value
        value = str(raw).strip()
        if not value:
            raise ValueError(f"{definition.label} cannot be blank")
        if definition.value_type == "choice" and value not in definition.choices:
            raise ValueError(
                f"{definition.label} must be one of: {', '.join(definition.choices)}"
            )
        return value

    def validate_location_attributes(
        self,
        location_attributes: dict[str, dict[str, Any]] | None,
        catalog,
        valid_paths: Iterable[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        definitions = self.normalize_catalog(catalog)
        allowed_paths = set(valid_paths) if valid_paths is not None else None
        normalized: dict[str, dict[str, Any]] = {}
        for path, values in (location_attributes or {}).items():
            if allowed_paths is not None and path not in allowed_paths:
                raise ValueError(f"attributes reference unknown hierarchy path: {path}")
            if not isinstance(values, dict):
                raise ValueError(f"location attributes for '{path}' must be an object")
            parsed: dict[str, Any] = {}
            for key, raw in values.items():
                if key not in definitions:
                    raise ValueError(f"location '{path}' uses unknown attribute '{key}'")
                parsed[key] = self.parse_value(definitions[key], raw)
            if parsed:
                normalized[path] = parsed
        return normalized

    def requirements_from_row(self, row: dict, catalog) -> dict[str, Any]:
        definitions = self.normalize_catalog(catalog)
        requirements: dict[str, Any] = {}
        for column, raw in row.items():
            if not str(column).startswith("req_") or raw is None or str(raw).strip() == "":
                continue
            key = str(column)[4:]
            if key not in definitions:
                raise ValueError(f"unknown SKU requirement column: {column}")
            requirements[key] = self.parse_value(definitions[key], raw)
        return requirements

    def validate_requirements(
        self, requirements: dict[str, Any] | None, catalog
    ) -> dict[str, Any]:
        definitions = self.normalize_catalog(catalog)
        if requirements is None:
            return {}
        if not isinstance(requirements, dict):
            raise ValueError("SKU requirements must be an object")
        normalized: dict[str, Any] = {}
        for key, raw in requirements.items():
            if key not in definitions:
                raise ValueError(f"unknown SKU requirement '{key}'")
            normalized[key] = self.parse_value(definitions[key], raw)
        return normalized

    def compatibility_issues(
        self, requirements: dict[str, Any] | None, effective: dict[str, Any], catalog
    ) -> list[str]:
        definitions = self.normalize_catalog(catalog)
        issues: list[str] = []
        for key, required in (requirements or {}).items():
            definition = definitions.get(key)
            if definition is None:
                issues.append(f"unknown requirement '{key}'")
                continue
            if key not in effective:
                issues.append(f"{definition.label}: location value is not defined")
                continue
            actual = effective[key]
            if definition.match_rule == "capacity":
                try:
                    matches = float(actual) >= float(required)
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    unit = f" {definition.unit}" if definition.unit else ""
                    issues.append(
                        f"{definition.label}: requires {required}{unit}, "
                        f"location provides {actual}{unit}"
                    )
            elif actual != required:
                issues.append(
                    f"{definition.label}: requires {required!s}, location is {actual!s}"
                )
        return issues

    def availability_issues(
        self,
        requirements: dict[str, Any] | None,
        effective_locations: Iterable[dict[str, Any]],
        catalog,
    ) -> list[str]:
        """Explain why a requirement set has no match among available locations."""
        definitions = self.normalize_catalog(catalog)
        locations = list(effective_locations)
        issues: list[str] = []
        for key, required in (requirements or {}).items():
            definition = definitions.get(key)
            if definition is None:
                issues.append(f"Unknown requirement: {key}")
                continue
            values = [location[key] for location in locations if key in location]
            missing = len(locations) - len(values)
            unit = f" {definition.unit}" if definition.unit else ""
            if definition.match_rule == "capacity" and values:
                numeric = [float(value) for value in values]
                detail = f"available maximum is {max(numeric):g}{unit}"
            elif values:
                distinct = sorted({str(value) for value in values})
                preview = ", ".join(distinct[:5])
                if len(distinct) > 5:
                    preview += ", …"
                detail = f"available values: {preview}"
            else:
                detail = "attribute is undefined on every available location"
            if missing and values:
                detail += f"; undefined on {missing} location(s)"
            issues.append(
                f"{definition.label} requires {required}{unit}; {detail}"
            )
        return issues or ["No available location satisfies the SKU requirements"]

    def check_location(
        self,
        requirements: dict[str, Any] | None,
        path: str,
        location_attributes: dict[str, dict[str, Any]] | None,
        catalog,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        effective, _sources = self.effective_attributes(path, location_attributes)
        issues = self.compatibility_issues(requirements, effective, catalog)
        return not issues, issues, effective

    @staticmethod
    def format_values(values: dict[str, Any] | None) -> str:
        if not values:
            return "none"
        return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
