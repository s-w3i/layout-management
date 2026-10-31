#!/usr/bin/env python3
"""Development-only affinity comparison against fresh Python and saved output."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from importlib.util import spec_from_file_location,module_from_spec
ROOT=Path(__file__).resolve().parents[1]
spec=spec_from_file_location('migration',ROOT/'scripts/validate-migration.py');migration=module_from_spec(spec);spec.loader.exec_module(migration)

def native(request):
    start=time.perf_counter();result=subprocess.run([str(ROOT/'build/layout-engine')],input=json.dumps(request),capture_output=True,text=True)
    value=json.loads(result.stdout)
    if result.returncode:raise RuntimeError(value)
    return value,time.perf_counter()-start

def metrics(value):
    return {k:v for k,v in value.items() if not isinstance(v,(list,dict))}

def crosscheck_replay(request, assignments, analysis, native_score):
    from warehouse_layout.domain import GridProject
    from warehouse_layout.slotting import SlottingService
    from warehouse_layout.slotting_strategies.affinity_pipeline import OrderReplay
    geometry,_=native({'command':'geometry','project':request['project']})
    positions={(r['id'],1,1):{'zone_id':r['zone']} for r in geometry['racks']}
    model=OrderReplay(SlottingService(),GridProject.from_project_dict(request['project']).to_building_dict(),analysis,assignments,positions,request['settings']['store_workstation_mapping'])
    result=metrics(model.evaluate(assignments))
    names={'deliveries':'deliveries','uncovered_groups':'uncovered_groups','round_trip_metres':'round_trip_distance_m','peak_station_work_seconds':'peak_station_work_s','peak_zone_work_seconds_per_rack':'maximum_zone_work_s_per_rack'}
    passed=all(abs(float(result[a])-float(native_score[b]))<1e-6 for a,b in names.items())
    if not passed:raise AssertionError({'python_replay':result,'native_replay':metrics(native_score)})
    return {'passed':passed,'python_replay':result}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--legacy',type=Path,required=True);parser.add_argument('--date',default='2023-01-03');parser.add_argument('--historical-only',action='store_true');args=parser.parse_args()
    sys.path.insert(0,str(args.legacy))
    from warehouse_layout.domain import GridProject
    from warehouse_layout.slotting import SlottingService
    from warehouse_layout.attributes import StorageAttributeService
    from warehouse_layout.affinity import AffinityService
    orders=json.loads((ROOT/'examples/orders.json').read_text());mapping=json.loads((ROOT/'examples/store-workstation-mapping.json').read_text())
    destination=ROOT/'.data/oracle/affinity';destination.mkdir(parents=True,exist_ok=True)
    if args.historical_only:
        saved=args.legacy/'resources/map/amr/affinity_zone_on_max_same_sku_2.slotting.json';reference=json.loads(saved.read_text())
        rows={};quantities={}
        for row in reference['assignments']:
            sku=row['sku'];quantities[sku]=quantities.get(sku,0)+float(row['quantity_ea'])
            if sku not in rows:rows[sku]={key:row[key] for key in ['sku','velocity_class','pick_frequency','total_required_ea','units_per_slot','slots_per_unit','required_racks','sku_requirements']};rows[sku]['required_slots']=row['required_slot_count']
        for sku,row in rows.items():row['total_slotted_ea']=quantities[sku]
        request=migration.fixture(balance=True,cap=2);request['sku_rows']=list(rows.values());request['orders']={'baskets':orders['baskets']};request['settings'].update(strategy='abc_affinity',store_workstation_mapping=mapping)
        result,seconds=native(request);migration.write(destination/'historical-native.json',result)
        oldScore,_=native(dict(request,command='score',assignments=reference['assignments']))
        newScore,_=native(dict(request,command='score',assignments=result['assignments']))
        report={'reference':str(saved),'reference_sha256':migration.digest(saved),'input_mode':'stock, requirements and load counts reconstructed from the saved assignments; same current grid and same supplied store/workstation mapping used to score both','settings':request['settings'],'native_validation':result['validation'],'reference_validation':oldScore['validation'],'native_score':metrics(newScore['score']),'reference_score':metrics(oldScore['score']),'native_engine_s':result['summary']['generation_ms']/1000,'same_requested_loads':len(result['assignments'])==len(reference['assignments']),'same_assigned_quantity':sum(float(r['quantity_ea']) for r in result['assignments'] if r['assignment_status']=='ASSIGNED')==sum(float(r['quantity_ea']) for r in reference['assignments'] if r['assignment_status']=='ASSIGNED')}
        migration.write(ROOT/'reports/historical-affinity-comparison.json',report);print(json.dumps(report),flush=True);return
    selected=date.fromisoformat(args.date);request=migration.fixture();request['settings'].update(strategy='abc_affinity',start_date=args.date,end_date=args.date,store_workstation_mapping=mapping);request['orders']={'baskets':[b for b in orders['baskets'] if b['date']==args.date]}
    migration.write(destination/'fresh-request.json',request)
    cache=destination/'cache';cache.mkdir(exist_ok=True)
    workbook=args.legacy/'resources/data/Sample Data.xlsx';cache_name=hashlib.sha256(str(workbook.resolve()).encode()).hexdigest()[:20]+'.npz';existing=args.legacy/'.cache/rmf_grid_map_editor/affinity'/cache_name
    if existing.exists():shutil.copyfile(existing,cache/cache_name)
    service=AffinityService(cache);dataset=service.load_orders(workbook,progress=lambda read,total,message:print(message,read,flush=True) if read==total else None);analysis=service.analyze(dataset,selected,selected)
    old_path=destination/'fresh-python.json';fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    if old_path.exists() and json.loads(old_path.read_text()).get('fingerprint')==fingerprint:previous=json.loads(old_path.read_text())
    else:
        project=GridProject.from_project_dict(request['project']);slotting=SlottingService(attributes=StorageAttributeService(project.warehouse_storage_defaults));start=time.perf_counter()
        rows,summary=slotting.generate_abc_affinity(project.to_building_dict(),request['sku_rows'],analysis,levels_per_rack=3,slots_per_level=4,handling_unit_type='AMR shelf',zone_assignments=project.zone_assignments,attribute_catalog=project.resolve_attribute_catalog(),location_attributes=project.location_attributes,storage_layout=project.storage_layout,maximum_same_sku_slots_per_rack=12,zone_workload_enabled=False,store_workstation_mapping=mapping)
        previous={'assignments':rows,'summary':summary,'elapsed_s':time.perf_counter()-start,'fingerprint':fingerprint};migration.write(old_path,previous)
    result,seconds=native(request);migration.write(destination/'fresh-native.json',result)
    oldScore,_=native(dict(request,command='score',assignments=previous['assignments']));newScore,_=native(dict(request,command='score',assignments=result['assignments']))
    by_id={r['inventory_load_id']:r for r in previous['assignments']};identical=sum(r['storage_location_address']==by_id[r['inventory_load_id']]['storage_location_address'] for r in result['assignments'])
    report={'date':args.date,'input_sha256':fingerprint,'requested_loads':len(result['assignments']),'identical_placements':identical,'exact_affinity_placement_parity':identical==len(result['assignments']),'native_validation':newScore['validation'],'python_validation':oldScore['validation'],'native_score':metrics(newScore['score']),'python_score':metrics(oldScore['score']),'native_engine_s':result['summary']['generation_ms']/1000,'python_s':previous['elapsed_s'],'note':'Native complete-order affinity ports the packing, quantity-plan reservations, equivalent-rack moves and multi-SKU bundle refinement. Exact placement parity and replay objectives are measured separately; physical validity is checked independently.'}
    report['independent_python_replay']={'native_layout':crosscheck_replay(request,result['assignments'],analysis,newScore['score']),'python_layout':crosscheck_replay(request,previous['assignments'],analysis,oldScore['score'])}
    migration.write(ROOT/'reports/current-affinity-comparison.json',report);print(json.dumps(report),flush=True)

if __name__=='__main__':main()
