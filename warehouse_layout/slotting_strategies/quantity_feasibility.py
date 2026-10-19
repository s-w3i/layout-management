"""Keep a complete feasible stock plan while affinity changes placement order."""

from collections import Counter
import json


def cell_key(position):
    return str(position["rack_id"]), int(position["level"]), int(position["slot"])


class QuantityFeasibilityPlan:
    """Exchange future slot reservations without stranding inventory copies."""

    def __init__(self, seed_rows, positions, same_sku_cap, rack_max_weight):
        self.positions = {cell_key(position): position for position in positions}
        self.profiles = {
            key: (key[1], json.dumps(position["effective_location_attributes"], sort_keys=True), position["planned_storage_type"])
            for key, position in self.positions.items()
        }
        self.rows = {str(row["inventory_load_id"]): row for row in seed_rows}
        self.placements = {}
        self.owners = {}
        self.placed = set()
        self.counts = Counter()
        self.weights = Counter()
        self.cap = same_sku_cap
        self.max_weight = rack_max_weight
        self.empty_order = sorted(self.positions, key=lambda key: (
            self.positions[key]["distance_m"], self.positions[key]["rack_rank"], key,
        ))
        for load, row in self.rows.items():
            if row.get("assignment_status") != "ASSIGNED":
                continue
            cells = tuple((str(unit["rack_id"]), int(unit["storage_level"]), int(unit["storage_slot"]))
                          for unit in row["occupied_handling_units"])
            if any(key not in self.positions or key in self.owners for key in cells):
                raise ValueError("quantity feasibility seed contains invalid or overlapping cells")
            self.placements[load] = cells
            for key in cells:
                self.owners[key] = load
                self.counts[str(row["sku"]), key[0]] += 1
            self.weights[cells[0][0]] += float(row.get("inventory_load_weight_kg") or 0)

    def valid_changes(self, moves):
        counts, weights = Counter(), Counter()
        for load, cells in moves.items():
            row = self.rows[load]
            weight = float(row.get("inventory_load_weight_kg") or 0)
            old = self.placements.get(load, ())
            for key in old:
                counts[str(row["sku"]), key[0]] -= 1
            for key in cells:
                counts[str(row["sku"]), key[0]] += 1
            if old:
                weights[old[0][0]] -= weight
            weights[cells[0][0]] += weight
        return (
            all(self.counts[key] + delta <= self.cap for key, delta in counts.items())
            and (self.max_weight is None or all(
                self.weights[rack] + delta <= self.max_weight + 1e-9 for rack, delta in weights.items()
            ))
        )

    def propose(self, load, occupied):
        """A direct exchange or empty equivalent cell retains the full stock plan."""
        desired = tuple(cell_key(position) for position in occupied)
        old = self.placements.get(load, ())
        if set(desired) == set(old):
            return {}
        displaced = {self.owners[key] for key in desired if key in self.owners and self.owners[key] != load}
        if not displaced:
            moves = {load: desired}
            return moves if self.valid_changes(moves) else None
        if len(desired) != 1 or len(old) > 1 or len(displaced) != 1:
            return None
        owner = next(iter(displaced))
        row = self.rows[owner]
        owner_cells = self.placements[owner]
        if (owner in self.placed or len(owner_cells) != 1
                or row.get("constrained_feasibility_reserved")
                or row.get("physical_storage_class") not in (None, "", "STANDARD", "NOT_EVALUATED")):
            return None
        for destination in (*old, *self.empty_order):
            if destination in desired:
                continue
            if destination in self.owners and self.owners[destination] != load:
                continue
            if self.profiles[destination] != self.profiles[owner_cells[0]]:
                continue
            moves = {load: desired, owner: (destination,)}
            if self.valid_changes(moves):
                return moves
        return None

    def commit(self, load, moves):
        for changed in moves:
            row = self.rows[changed]
            old = self.placements.get(changed, ())
            for key in old:
                del self.owners[key]
                self.counts[str(row["sku"]), key[0]] -= 1
            if old:
                self.weights[old[0][0]] -= float(row.get("inventory_load_weight_kg") or 0)
        for changed, cells in moves.items():
            row = self.rows[changed]
            self.placements[changed] = cells
            for key in cells:
                self.owners[key] = changed
                self.counts[str(row["sku"]), key[0]] += 1
            self.weights[cells[0][0]] += float(row.get("inventory_load_weight_kg") or 0)
        self.placed.add(load)
