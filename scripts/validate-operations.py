#!/usr/bin/env python3
"""Development-only movement comparison against the desktop implementation."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import time

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--legacy', type=Path, required=True)
    parser.add_argument('--layout', type=Path, help='Optional saved workspace or layout to compare')
    args = parser.parse_args()
    sys.path.insert(0, str(args.legacy))
    import numpy as np
    from warehouse_layout.slotting_strategies.affinity_support import handling_unit_visit_metrics
    order_path = ROOT / 'examples/orders.json'
    orders = json.loads(order_path.read_text())
    baskets = orders['baskets']
    names = sorted({str(sku) for b in baskets for sku in b['skus']})
    sku_ids = {name: i for i, name in enumerate(names)}
    stores = {name: i for i, name in enumerate(sorted({b['store'] for b in baskets}))}
    days = {b['date']: date.fromisoformat(b['date']).toordinal() for b in baskets}
    dates, store_ids, sku_indices = [], [], []
    for b in baskets:
        for sku in b['skus']:
            dates.append(days[b['date']]); store_ids.append(stores[b['store']]); sku_indices.append(sku_ids[str(sku)])
    dataset = SimpleNamespace(skus=np.array(names), dates=np.array(dates), store_indices=np.array(store_ids), sku_indices=np.array(sku_indices))
    analysis = SimpleNamespace(dataset=dataset, start_date=date.fromordinal(min(days.values())), end_date=date.fromordinal(max(days.values())), store_day_count=len({(b['date'], b['store']) for b in baskets}))
    cases = [
        ('reference-amr-layout', ROOT / '.data/oracle/amr-basic-off-cap12/native.json'),
        ('reference-asrs-layout', ROOT / '.data/oracle/asrs-basic-off-cap12/native.json'),
    ]
    if args.layout:
        cases.insert(0, ('saved-layout', args.layout.resolve()))
    reports = []
    for name, path in cases:
        payload = json.loads(path.read_text())
        layout = payload.get('result') or payload
        request = dict(command='movement', assignments=layout['assignments'], handling_unit_type=layout['handling_unit_type'], orders={'baskets': baskets})
        start = time.perf_counter()
        process = subprocess.run([str(ROOT / 'build/layout-engine')], input=json.dumps(request), capture_output=True, text=True, check=True)
        native = json.loads(process.stdout)
        elapsed = time.perf_counter() - start
        old = handling_unit_visit_metrics(analysis, layout['assignments'], layout['handling_unit_type'])
        keys = ['handling_unit_id', 'rack_id', 'visit_rank', 'visit_count', 'visit_rate', 'visit_share', 'cumulative_visit_share', 'movement_class']
        new_units = [{key: row.get(key) for key in keys} for row in native['units']]
        old_units = [{key: row.get(key) for key in keys} for row in old['units']]
        passed = new_units == old_units and all(native[key] == old[key] for key in ['fulfillment_group_count', 'total_handling_unit_visits', 'unit_count', 'ranking_level'])
        report = dict(case=name, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), assigned_loads=sum(r['assignment_status']=='ASSIGNED' for r in layout['assignments']), unit_count=native['unit_count'], estimated_visits=native['total_handling_unit_visits'], exact_movement_parity=passed, native_wall_seconds=elapsed)
        if not passed:
            report['first_differences'] = [(a,b) for a,b in zip(new_units,old_units) if a!=b][:3]
        reports.append(report)
        print(json.dumps(report), flush=True)
    result = dict(order_groups=analysis.store_day_count, orders_sha256=hashlib.sha256(order_path.read_bytes()).hexdigest(), cases=reports, all_passed=all(r['exact_movement_parity'] for r in reports), model='Historical handling-unit visit estimates; no traffic simulation or inventory depletion')
    (ROOT/'reports').mkdir(parents=True, exist_ok=True)
    (ROOT/'reports/movement-validation.json').write_text(json.dumps(result, indent=2)+'\n')
    if not result['all_passed']: sys.exit(1)

if __name__ == '__main__': main()
