"""Reproduce the affinity rack-group comparison using the saved stock and simulation inputs."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from amr_simulation.inputs import assign_workstations, load_workload, racks_from_layout, validate_inputs
from amr_simulation.models import MotionProfile, SimulationConfig
from current_heat_simulation.astar_planner import WarehouseMap
from current_heat_simulation.batch import execute_batch, file_hash, fingerprint
from current_heat_simulation.reporting import generate_report
from current_heat_simulation.run_metrics import write_json
from warehouse_layout.affinity import AffinityService
from warehouse_layout.domain import StorageLayout
from warehouse_layout.slotting import SlottingService
from warehouse_layout.slotting_repository import SlottingLayoutRepository


OLD_RESULTS = ROOT / "current_heat_simulation/results/affinity_zone_on_max_same_sku_1_to_6_10_dates"
OUTPUT = ROOT / "current_heat_simulation/results/affinity_rack_balance_revision"


def source_hashes():
    return {str(path.relative_to(ROOT)): file_hash(path)
            for package in ("warehouse_layout", "current_heat_simulation", "amr_simulation")
            for path in sorted((ROOT / package).rglob("*.py"))}


def inventory_sources(payload):
    grouped = defaultdict(list)
    for row in payload["assignments"]:
        grouped[str(row["sku"])].append(row)
    result = []
    for copies in grouped.values():
        row = dict(copies[0])
        row["required_slots"] = int(row.get("required_slot_count") or len(copies))
        row["total_slotted_ea"] = sum(int(copy.get("quantity_ea") or 0) for copy in copies)
        result.append(row)
    return result


def check_inventory(rows, source, cap, max_weight):
    expected = {row["sku"]: int(row["total_slotted_ea"]) for row in source}
    actual = Counter()
    rack_cells = Counter()
    rack_weights = Counter()
    occupied = set()
    for row in rows:
        actual[row["sku"]] += int(row.get("quantity_ea") or 0)
        if row["assignment_status"] != "ASSIGNED":
            continue
        for unit in row["occupied_handling_units"]:
            key = unit["rack_id"], unit["storage_level"], unit["storage_slot"]
            if key in occupied:
                raise AssertionError(f"duplicate occupied cell: {key}")
            occupied.add(key)
            rack_cells[row["sku"], unit["rack_id"]] += 1
            if unit["rack_id"] != row["rack_id"]:
                raise AssertionError("occupied footprint differs from row rack")
        rack_weights[row["rack_id"]] += float(row.get("inventory_load_weight_kg") or 0)
    if dict(actual) != expected:
        raise AssertionError("stock quantities changed")
    if max(rack_cells.values(), default=0) > cap:
        raise AssertionError("same-SKU physical-cell limit exceeded")
    if max_weight and max(rack_weights.values(), default=0) > max_weight + 1e-9:
        raise AssertionError("whole-rack weight limit exceeded")
    return {"inventory_quantities_preserved": True, "unique_occupied_cells": len(occupied),
            "same_sku_limit_respected": True, "whole_rack_weight_respected": True,
            "assigned_loads": sum(row["assignment_status"] == "ASSIGNED" for row in rows),
            "unassigned_loads": sum(row["assignment_status"] != "ASSIGNED" for row in rows)}


def verify_orders(original, workload, orders, output):
    """Require identical original-date tasks even if workbook packaging changed."""
    def canonical(tasks):
        return {task.task_id: {"store_id": task.store_id, "line_counts": task.line_counts,
                              "source_lines": task.source_lines, "release_seconds": task.release_seconds}
                for task in tasks}

    snapshot = json.loads((OLD_RESULTS / "workload_snapshot.json").read_text())["days"]
    expected = {task["task_id"]: {key: task[key] for key in
                                ("store_id", "line_counts", "source_lines", "release_seconds")}
                for tasks in snapshot.values() for task in tasks}
    actual = canonical([task for task in workload.tasks if task.task_date.isoformat() in snapshot])
    if actual != expected:
        raise AssertionError("original-date store/SKU/line-count tasks differ from the saved benchmark")
    current_hash = file_hash(orders)
    original_hash = original["inputs"][str(orders)]
    verification_path = output / "orders_input_verification.json"
    full_fingerprint = fingerprint(canonical(workload.tasks))
    if verification_path.exists():
        verified = json.loads(verification_path.read_text())
        if (verified.get("current_orders_sha256") == current_hash
                and verified.get("original_orders_sha256") == original_hash
                and verified.get("full_current_workload_fingerprint") == full_fingerprint
                and verified.get("uncached_reader_matches_cache")):
            return verified
    if current_hash != original_hash:
        direct = load_workload(orders, use_cache=False)
        if canonical(direct.tasks) != canonical(workload.tasks):
            raise AssertionError("cached workload differs from the workbook's direct contents")
    verified = {
        "current_orders_sha256": current_hash, "original_orders_sha256": original_hash,
        "uncached_reader_matches_cache": True, "original_dates_match_exactly": True,
        "main_date_task_count": len(actual), "main_date_source_lines": sum(task["source_lines"] for task in actual.values()),
        "main_workload_fingerprint": fingerprint(actual), "full_current_workload_fingerprint": full_fingerprint,
        "original_full_training_content_verified": current_hash == original_hash,
        "scope": "Exact simulation demand verified for the original ten dates; full historical training equivalence is unverified when workbook binaries differ.",
    }
    write_json(verification_path, verified)
    return verified


def generate(output, limits):
    repository = SlottingLayoutRepository()
    source_path = ROOT / "resources/map/amr/affinity_zone_on_max_same_sku_1.slotting.json"
    original = json.loads((OLD_RESULTS / "manifest.json").read_text())["identity"]
    for cap in limits:
        saved_path = ROOT / f"resources/map/amr/affinity_zone_on_max_same_sku_{cap}.slotting.json"
        original_path = str(ROOT / f"resources/map/affinity_zone_on_max_same_sku_{cap}.slotting.json")
        if file_hash(saved_path) != original["inputs"][original_path]:
            raise AssertionError(f"cap {cap} source layout differs from the original benchmark")
    payload = repository.load(source_path)
    source = inventory_sources(payload)
    orders = ROOT / "resources/data/Sample Data.xlsx"
    service = SlottingService()
    catalog = service.attributes.normalize_catalog(payload["attribute_catalog"])
    print("Loading historical affinity groups...", flush=True)
    analysis = AffinityService.analyze(AffinityService().load_orders(orders))
    identity = {"source_layout": file_hash(source_path), "orders": file_hash(orders),
                "code": source_hashes(), "limits": limits,
                "source_sku_count": len(source), "source_inventory_load_count": len(payload["assignments"]),
                "training_date_range": [analysis.start_date.isoformat(), analysis.end_date.isoformat()]}
    write_json(output / "generation_manifest.json", identity)
    results = []
    for cap in limits:
        started = perf_counter()
        layout_path = output / "layouts" / f"revised_max_same_sku_{cap}.slotting.json"
        generation_key = fingerprint({**identity, "limits": None, "cap": cap})
        if layout_path.exists():
            saved = repository.load(layout_path)
            if saved["summary"].get("generation_fingerprint") == generation_key:
                print(f"Cap {cap}: reusing verified layout", flush=True)
                results.append({"cap": cap, "path": str(layout_path), "audit": saved["summary"]["inventory_audit"]})
                continue
        print(f"Generating cap {cap} with {len(source)} SKUs...", flush=True)
        storage = StorageLayout.from_dict(payload["storage_layout"])
        attributes = copy.deepcopy(payload["location_attributes"])
        rows, summary = service.generate_abc_affinity(
            copy.deepcopy(payload["building"]), copy.deepcopy(source), analysis, 1.0,
            levels_per_rack=payload["rack_capacity"]["levels"],
            slots_per_level=payload["rack_capacity"]["slots_per_level"],
            handling_unit_type=payload["handling_unit_type"],
            zone_assignments=payload["zone_assignments"], attribute_catalog=catalog,
            location_attributes=attributes, storage_layout=storage,
            zone_workload_enabled=True, maximum_same_sku_slots_per_rack=cap,
        )
        audit = check_inventory(rows, source, cap, storage.machine_carrying_capacity.get("max_item_weight"))
        old = repository.load(ROOT / f"resources/map/amr/affinity_zone_on_max_same_sku_{cap}.slotting.json")
        if audit["unassigned_loads"] > old["summary"]["unassigned_count"]:
            raise AssertionError("revised assignment coverage is worse than the saved layout")
        summary.update(inventory_audit=audit, generation_fingerprint=generation_key,
                       generation_wall_seconds=perf_counter() - started)
        repository.save(rows, payload["building"], summary, layout_path,
                        strategy="abc_affinity", handling_unit_type=payload["handling_unit_type"],
                        levels_per_rack=storage.levels_per_rack, slots_per_level=storage.slots_per_level,
                        zone_assignments=payload["zone_assignments"], attribute_catalog=catalog,
                        location_attributes=attributes, source_grid_project=str(ROOT / "resources/map/amr/map1_1.grid.json"),
                        source_velocity=payload["sources"]["sku_velocity_csv"],
                        source_chilled=payload["sources"]["sku_attributes_csv"], source_affinity=str(orders),
                        affinity_configuration=summary["affinity_tuning"], storage_layout=storage)
        results.append({"cap": cap, "path": str(layout_path), "audit": audit,
                        "estimated_presentations": summary["rack_workload"]["total_presentations"],
                        "occupied_racks": summary["final_occupied_rack_count"],
                        "generation_wall_seconds": summary["generation_wall_seconds"]})
        write_json(output / "generation_results.json", results)
        print(json.dumps(results[-1]), flush=True)
    write_json(output / "generation_results.json", results)
    return results


def generate_all(output, limits, workers):
    if workers <= 1 or len(limits) == 1:
        return generate(output, limits)
    results = []
    for cap in limits:
        cached = output / "layouts" / f"revised_max_same_sku_{cap}.slotting.json"
        destination = output / "generation" / f"cap_{cap}" / "layouts" / cached.name
        if cached.exists() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(cached.read_bytes())
    with ProcessPoolExecutor(max_workers=min(workers, len(limits))) as pool:
        futures = {pool.submit(generate, output / "generation" / f"cap_{cap}", [cap]): cap for cap in limits}
        for future in as_completed(futures):
            results.extend(future.result())
            results.sort(key=lambda row: row["cap"])
            write_json(output / "generation_results.json", results)
    return results


def simulate(output, generated, workers, dates=None, phase="simulation"):
    original = json.loads((OLD_RESULTS / "manifest.json").read_text())["identity"]
    grid = ROOT / "resources/map/amr/map1_1.grid.json"
    orders = ROOT / "resources/data/Sample Data.xlsx"
    if file_hash(grid) != original["inputs"]["/home/usern/layout_management_master/resources/map/map1_1.grid.json"]:
        raise AssertionError("simulation map differs from the saved benchmark")
    amr = dict(original["amr"])
    amr["spawn_nodes"] = tuple(amr["spawn_nodes"])
    amr["workstations"] = tuple(amr["workstations"])
    amr["motion"] = MotionProfile(**amr["motion"])
    amr = SimulationConfig(**amr)
    amr.validate()
    workload = load_workload(orders)
    orders_verification = verify_orders(original, workload, orders, output)
    mapping = dict(original["mapping"])
    if dates is not None:
        mapping = assign_workstations(workload.tasks, replace(amr, store_workstation_overrides=mapping))
        if any(mapping[store] != station for store, station in original["mapping"].items()):
            raise AssertionError("additional-date mapping changed an existing store assignment")
    selected_dates = set(original["dates"] if dates is None else dates)
    days = defaultdict(list)
    for task in workload.tasks:
        if task.task_date.isoformat() in selected_dates:
            days[task.task_date.isoformat()].append(task)
    warehouse_map = WarehouseMap(grid)
    layouts = {}
    repository = SlottingLayoutRepository()
    for row in generated:
        path = Path(row["path"])
        name = path.name.removesuffix(".slotting.json")
        payload = repository.load(path)
        racks = racks_from_layout(payload["assignments"])
        validation = validate_inputs(warehouse_map.project, warehouse_map.router, racks,
                                     [task for daily in days.values() for task in daily], amr, mapping)
        write_json(output / phase / name / "validation_report.json", validation.to_dict())
        if not validation.valid:
            raise AssertionError(f"preflight failed for {name}")
        layouts[name] = {"racks": racks, "strategy": "abc_affinity", "path": str(path)}
    identity = {"config": original["config"], "amr": amr.snapshot(), "mapping": mapping,
                "grid_hash": file_hash(grid), "orders_hash": file_hash(orders), "dates": sorted(days),
                "layouts": {name: file_hash(Path(value["path"])) for name, value in layouts.items()},
                "code": source_hashes(), "max_seconds": original["max_seconds"],
                "orders_verification": orders_verification}
    simulation_output = output / phase
    key = fingerprint(identity)
    manifest = simulation_output / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text())["fingerprint"] != key:
        raise AssertionError("simulation fingerprint changed; preserve prior results and use a new output directory")
    write_json(manifest, {"fingerprint": key, "identity": identity, "baseline_layout": next(iter(layouts))})
    write_json(simulation_output / "store_workstation_mapping.json", mapping)
    write_json(simulation_output / "config_snapshot.json", {"current_heat": original["config"], "amr": amr.snapshot(), "max_seconds": original["max_seconds"]})
    write_json(simulation_output / "workload_snapshot.json", {
        "orders_file": str(orders), "release_rule": "all store/day groups at t=0",
        "days": {day: [{"task_id": task.task_id, "store_id": task.store_id, "line_counts": task.line_counts,
                        "source_lines": task.source_lines, "release_seconds": 0} for task in daily]
                 for day, daily in sorted(days.items())},
    })
    shared = {"fingerprint": key, "config": original["config"], "grid": grid, "days": dict(days),
              "layouts": layouts, "mapping": mapping, "amr": amr,
              "headless": True, "max_seconds": original["max_seconds"]}
    results = execute_batch(shared, simulation_output, workers, resume=True)
    generate_report(simulation_output, results, layouts, sorted(days), next(iter(layouts)), warehouse_map)
    if not all(row.get("success_flag") for row in results):
        raise AssertionError("one or more simulation days failed; inspect diagnostics before comparing")
    return results


def compare_saved_results(output, generated, results):
    """Read original day outputs without replacing their files or checkpoints."""
    original = json.loads((OLD_RESULTS / "manifest.json").read_text())["identity"]
    comparison = output / "comparison"
    layouts, combined = {}, []
    for entry in generated:
        cap = entry["cap"]
        for source, name in (
            (OLD_RESULTS / f"affinity_zone_on_max_same_sku_{cap}", f"original_max_same_sku_{cap}"),
            (output / "simulation" / f"revised_max_same_sku_{cap}", f"revised_max_same_sku_{cap}"),
        ):
            layouts[name] = {"strategy": "abc_affinity"}
            for day in original["dates"]:
                source_day = (source / day).resolve()
                target_day = comparison / name / day
                target_day.parent.mkdir(parents=True, exist_ok=True)
                if not target_day.exists():
                    os.symlink(source_day, target_day, target_is_directory=True)
                row = json.loads((source_day / "summary.json").read_text())
                row["layout"] = name
                combined.append(row)
    baseline = "original_max_same_sku_2" if 2 in {entry["cap"] for entry in generated} else next(iter(layouts))
    generate_report(comparison, combined, layouts, original["dates"], baseline,
                    WarehouseMap(ROOT / "resources/map/amr/map1_1.grid.json"))


def validate_additional_dates(output, generated, results, workers):
    original = json.loads((OLD_RESULTS / "manifest.json").read_text())["identity"]
    scores = defaultdict(list)
    for row in results:
        scores[row["layout"]].append(row["line_throughput_per_hour"])
    winner = max(scores, key=lambda name: sum(scores[name]) / len(scores[name]))
    selected = next(entry for entry in generated if Path(entry["path"]).name.removesuffix(".slotting.json") == winner)
    workload = load_workload(ROOT / "resources/data/Sample Data.xlsx")
    available = sorted({task.task_date.isoformat() for task in workload.tasks} - set(original["dates"]))
    days = sorted({available[int((len(available) - 1) * fraction)] for fraction in (.25, .5, .75)})
    old = {"cap": selected["cap"], "path": str(ROOT / f"resources/map/amr/affinity_zone_on_max_same_sku_{selected['cap']}.slotting.json")}
    write_json(output / "additional_date_selection.json", {
        "selected_layout": winner, "selected_cap": selected["cap"], "dates": days,
        "selection": "highest_mean_throughput_across_all_ten_completed_dates",
        "caveat": "Additional dates were not used to choose the limit but are included in the historical affinity training dataset.",
    })
    return simulate(output, [old, selected], workers, dates=days, phase="additional_dates")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limits", type=int, nargs="+", default=list(range(1, 7)))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args()
    if args.workers < 1 or any(cap < 1 for cap in args.limits) or len(args.limits) != len(set(args.limits)):
        parser.error("workers and unique same-SKU limits must be positive")
    generated = generate_all(args.output.resolve(), args.limits, args.workers)
    if not args.generate_only:
        results = simulate(args.output.resolve(), generated, args.workers)
        compare_saved_results(args.output.resolve(), generated, results)
        validate_additional_dates(args.output.resolve(), generated, results, args.workers)
        if sorted(args.limits) == list(range(1, 7)):
            from report_affinity_rack_balance import build_report
            build_report(args.output.resolve())


if __name__ == "__main__":
    main()
