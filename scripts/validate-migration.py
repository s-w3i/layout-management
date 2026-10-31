#!/usr/bin/env python3
"""Development-only comparison oracle; Python is never used by the application.

Run after building: python3 scripts/validate-migration.py --legacy /path/to/original
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2)+'\n')

def fixture(kind='amr',balance=False,cap=12):
    def rows(name):
        with (ROOT/'examples'/name).open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))
    attributes={row['sku']:{key:float(value) if key.startswith('max_item_') else value.lower()=='true' for key,value in row.items() if key!='sku' and value!=''} for row in rows('sku-attributes.csv')}
    stock={row['sku']:row for row in rows('stock.csv')}
    skus=[]
    for row in rows('velocity.csv'):
        row.update(stock[row['sku']]);row['sku_requirements']=attributes[row['sku']];skus.append(row)
    return {'project':json.loads((ROOT/f'examples/{kind}.grid.json').read_text()),'sku_rows':skus,'settings':{'strategy':'basic','maximum_same_sku_slots_per_rack':cap,'zone_workload_enabled':balance,'ergonomic_weight_heuristic':True}}

def compare(request,legacy_path,name):
    sys.path.insert(0,str(legacy_path))
    from warehouse_layout.domain import GridProject
    from warehouse_layout.slotting import SlottingService
    from warehouse_layout.attributes import StorageAttributeService
    destination=ROOT/'.data/oracle'/name
    destination.mkdir(parents=True,exist_ok=True)
    write(destination/'request.json',request)
    project=GridProject.from_project_dict(request['project'])
    service=SlottingService(attributes=StorageAttributeService(project.warehouse_storage_defaults))
    options=dict(levels_per_rack=project.storage_layout.levels_per_rack,slots_per_level=project.storage_layout.slots_per_level,handling_unit_type=project.storage_layout.handling_unit_type,zone_assignments=project.zone_assignments,attribute_catalog=project.resolve_attribute_catalog(),location_attributes=project.location_attributes,storage_layout=project.storage_layout,maximum_same_sku_slots_per_rack=request['settings']['maximum_same_sku_slots_per_rack'],zone_workload_enabled=request['settings']['zone_workload_enabled'])
    cached=destination/'python.json'
    fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    if cached.exists() and json.loads(cached.read_text()).get('fingerprint')==fingerprint:
        old=json.loads(cached.read_text())
    elif name=='amr-basic-off-cap12' and (ROOT/'.data/current-basic.json').exists():
        old=json.loads((ROOT/'.data/current-basic.json').read_text());old['fingerprint']=fingerprint;write(cached,old)
    else:
        start=time.perf_counter();rows,summary=service.generate_basic(project.to_building_dict(),request['sku_rows'],**options)
        old={'assignments':rows,'summary':summary,'elapsed_s':time.perf_counter()-start,'fingerprint':fingerprint};write(cached,old)
    start=time.perf_counter()
    run=subprocess.run([str(ROOT/'build/layout-engine'),str(destination/'request.json')],capture_output=True,text=True,check=False)
    native=json.loads(run.stdout)
    if run.returncode:raise RuntimeError(native)
    write(destination/'native.json',native)
    by_id={r['inventory_load_id']:r for r in old['assignments']}
    differences=[];exact=0
    for row in native['assignments']:
        previous=by_id.get(row['inventory_load_id'])
        same=previous and all(row.get(key)==previous.get(key) for key in ('sku','quantity_ea','assignment_status','storage_location_address','occupied_storage_location_addresses'))
        if same:exact+=1
        elif len(differences)<20:differences.append({'id':row['inventory_load_id'],'native':row.get('storage_location_address'),'python':previous.get('storage_location_address') if previous else None})
    result={'case':name,'input_sha256':fingerprint,'native_loads':len(native['assignments']),'python_loads':len(old['assignments']),'identical_load_placements':exact,'exact_placement_parity':exact==len(native['assignments'])==len(old['assignments']),'native_assigned':native['summary']['assigned_count'],'python_assigned':old['summary']['assigned_count'],'native_occupied_racks':native['summary']['final_occupied_rack_count'],'python_occupied_racks':old['summary']['final_occupied_rack_count'],'native_engine_s':native['summary']['generation_ms']/1000,'python_s':old['elapsed_s'],'validation':native['validation'],'differences':differences}
    print(json.dumps({key:result[key] for key in ('case','native_loads','identical_load_placements','exact_placement_parity','native_engine_s','python_s')}),flush=True)
    return result

def stock_comparison(legacy):
    sys.path.insert(0,str(legacy))
    from warehouse_layout.stock_determination import calculate_stock_requirements,calculate_rack_requirements
    request=fixture();orders=json.loads((ROOT/'examples/orders.json').read_text());request.update(command='stock',orders=orders,attributes={r['sku']:r['sku_requirements'] for r in request['sku_rows']})
    request['settings'].update(minimum_stock_days=2,buffer_stock_days=1)
    native=json.loads(subprocess.run([str(ROOT/'build/layout-engine')],input=json.dumps(request),capture_output=True,text=True,check=True).stdout)
    stock=calculate_stock_requirements(((r['sku'],date.fromisoformat(r['date']),r['quantity'],r['conversion']) for r in orders['daily']),2,1)
    physical=request['project']['warehouse_storage_defaults'];enriched,_=calculate_rack_requirements(stock,request['attributes'],tuple(physical[k] for k in ('max_item_length','max_item_width','max_item_height')),3,4,slot_max_weight=physical['max_item_weight'],rack_max_weight=150)
    by_id={r['sku']:r for r in enriched};keys=('total_required_ea','total_slotted_ea','uom_conversion_qty','units_per_slot','slots_per_unit','required_slots','required_racks')
    diff=[{'sku':r['sku'],'differences':{k:[r.get(k),by_id[r['sku']].get(k)] for k in keys if (r.get(k) or 0)!=(by_id[r['sku']].get(k) or 0)}} for r in native['rows']]
    diff=[r for r in diff if r['differences']]
    result={'sku_count':len(native['rows']),'exact_numeric_stock_parity':not diff,'mismatched_skus':len(diff),'differences':diff[:20]}
    write(ROOT/'.data/oracle/stock-native.json',native);write(ROOT/'.data/oracle/stock-python.json',enriched);print('STOCK',json.dumps(result),flush=True);return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--legacy',type=Path,required=True);parser.add_argument('--workers',type=int,default=2);args=parser.parse_args()
    jobs=[('amr-basic-off-cap12',fixture()),('amr-basic-on-cap12',fixture(balance=True)),('asrs-basic-off-cap12',fixture('asrs'))]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(compare,request,args.legacy,name) for name,request in jobs]
        results=[f.result() for f in futures]
    stock=stock_comparison(args.legacy)
    report={'generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'legacy_source':str(args.legacy),'legacy_source_hashes':{str(p.relative_to(args.legacy)):digest(p) for p in (args.legacy/'warehouse_layout').rglob('*.py')},'basic_cases':results,'stock':stock,'affinity_note':'See current-affinity-comparison.json and historical-affinity-comparison.json for complete-order affinity, physical validation, and measured objective differences. Exact affinity placement parity is not claimed.'}
    write(ROOT/'reports/migration-validation.json',report)
    if not all(r['exact_placement_parity'] for r in results) or not stock['exact_numeric_stock_parity']:sys.exit(1)

if __name__=='__main__':main()
