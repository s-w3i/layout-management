"""Configuration and map-authoritative slot/aisle validation."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Crane:
    id: str
    workstation: str
    rows: tuple[int, int]
    home: tuple[float, float, float]


@dataclass(frozen=True)
class Tote:
    id: str
    sku: str
    address: str
    rack: str
    level: int
    slot: int
    crane: str
    position: tuple[float, float, float]


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} must be a number')
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f'{label} must be finite and {"positive" if positive else "nonnegative"}')
    return float(value)


def coordinate(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return float(value)


def load_config(path):
    try:
        config = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f'invalid YAML: {exc}') from exc
    if not isinstance(config, dict):
        raise ValueError('configuration must be a YAML mapping')
    try:
        for axis in 'xyz':
            for key in ('speed_mps', 'acceleration_mps2'):
                number(config['motion'][axis][key], f'motion.{axis}.{key}', True)
        for key in ('picking_seconds', 'extraction_dwell_seconds', 'replacement_dwell_seconds'):
            number(config['timing'][key], f'timing.{key}')
        number(config['workstation_height_m'], 'workstation_height_m')
        number(config['simulation']['max_seconds'], 'simulation.max_seconds', True)
        for section, key, minimum in [('simulation', 'workers', 1), ('display', 'width', 1200),
                                      ('display', 'height', 900), ('display', 'fps', 1)]:
            value = config[section][key]
            if type(value) is not int or value < minimum:
                raise ValueError(f'{section}.{key} must be an integer >= {minimum}')
        number(config['display']['time_scale'], 'display.time_scale', True)
        for key in ('map_file', 'orders_file'):
            if not isinstance(config[key], str) or not config[key]:
                raise ValueError(f'{key} must be a path string')
        for key, value_type in [('aisles', dict), ('layout_files', str)]:
            if not isinstance(config[key], list) or not config[key] or any(
                    not isinstance(value, value_type) or not value for value in config[key]):
                raise ValueError(f'{key} must be a nonempty list of {value_type.__name__} values')
    except (KeyError, TypeError) as exc:
        raise ValueError(f'missing or malformed configuration: {exc}') from exc
    return config


def load_map(path, config):
    data = json.loads(Path(path).read_text())
    if data.get('schema') != 'rmf_grid_map_editor/v2':
        raise ValueError('expected an rmf_grid_map_editor/v2 map')
    grid = data['grid']
    dx = number(grid['spacing_m'], 'grid.spacing_m', True)
    dy = number(grid.get('spacing_y_m', dx), 'grid.spacing_y_m', True)
    stations = {m['endpoint_id']: m for m in data['markers'] if m['role'] == 'workstation'}
    racks = {f"G{m['column']}_{m['row']}": m for m in data['markers'] if m['role'] == 'rack'}
    cranes, used_rows, used_stations = [], set(), set()
    for raw in config['aisles']:
        name, station, rows = raw['id'], raw['workstation'], tuple(raw['rack_rows'])
        if not isinstance(name, str) or not name or name in {c.id for c in cranes}:
            raise ValueError(f'duplicate or invalid crane ID: {name}')
        if station not in stations or station in used_stations:
            raise ValueError(f'unknown or shared workstation: {station}')
        marker = stations[station]
        if len(rows) != 2 or any(type(r) is not int for r in rows) or set(rows) != {marker['row']-1, marker['row']+1}:
            raise ValueError(f'{name}: rack_rows must be the two rows adjacent to its workstation aisle')
        if used_rows.intersection(rows):
            raise ValueError(f'{name}: rack rows cannot be shared between cranes')
        used_rows.update(rows)
        used_stations.add(station)
        cranes.append(Crane(name, station, tuple(sorted(rows)),
                            (marker['column']*dx, marker['row']*dy, config['workstation_height_m'])))
    if {m['row'] for m in racks.values()} != used_rows or set(stations) != used_stations:
        raise ValueError('aisle configuration must cover every mapped rack row and workstation')
    geometry = {}
    for slot in data['storage_layout']['slots']:
        key = (slot['rack_id'], slot['level'], slot['slot'])
        if key in geometry or key[0] not in racks:
            raise ValueError(f'duplicate slot or unknown rack: {key}')
        pos = tuple(coordinate(slot[f'center_{a}'], f'{key}.center_{a}') for a in 'xyz')
        rack = racks[key[0]]
        if abs(pos[1]-rack['row']*dy) > 1e-6 or abs(pos[0]-rack['column']*dx) > dx/2:
            raise ValueError(f'slot outside its rack: {key}')
        geometry[key] = pos
    return data, sorted(cranes, key=lambda c: c.id), geometry, racks


def load_layout(path, cranes, geometry, racks, required_skus):
    """Return compact records and every preflight error, without using stale source paths."""
    data = json.loads(Path(path).read_text())
    errors, totes, occupied, ids = [], [], set(), set()
    row_cranes = {row: c for c in cranes for row in c.rows}
    if data.get('schema') != 'inventory_slotting_layout/v2':
        errors.append('expected inventory_slotting_layout/v2 layout')
    for index, row in enumerate(data.get('assignments', [])):
        try:
            if row.get('assignment_status') != 'ASSIGNED':
                continue
            if row.get('handling_unit_type') != 'Tote' or row.get('occupied_slot_count', 1) != 1:
                raise ValueError('only single-slot totes are supported')
            key = (row['rack_id'], row['storage_level'], row['storage_slot'])
            if key not in geometry:
                raise ValueError(f'unknown slot: {key}')
            position = tuple(
                coordinate(row[f'center_{a}'], f'center_{a}') for a in 'xyz'
            ) if all(f'center_{a}' in row for a in 'xyz') else geometry[key]
            if any(abs(a-b) > 1e-6 for a, b in zip(position, geometry[key])):
                raise ValueError(f'layout/map slot geometry mismatch: {key}')
            crane = row_cranes[racks[key[0]]['row']]
            address, tote_id, sku = str(row['buffer_id']), str(row['handling_unit_id']), str(row['sku'])
            expected_address = f'B-{key[0]}/L{key[1]:02d}/S{key[2]:02d}'
            if address != expected_address or not tote_id or not sku:
                raise ValueError(f'invalid tote identity/address: {address}')
            if key in occupied or tote_id in ids:
                raise ValueError(f'duplicate occupied slot or tote ID: {address}')
            occupied.add(key)
            ids.add(tote_id)
            totes.append(Tote(tote_id, sku, address, key[0], key[1], key[2], crane.id, position))
        except (KeyError, ValueError, TypeError) as exc:
            errors.append(f'assignment {index}: {exc}')
    missing = sorted(set(required_skus)-{t.sku for t in totes})
    if missing:
        errors.append(f'{len(missing)} missing/inaccessible demand SKUs: {", ".join(missing)}')
    counts = Counter(t.crane for t in totes)
    validation = {'valid': not errors, 'errors': errors, 'missing_skus': missing,
                  'totes': len(totes), 'skus': len({t.sku for t in totes}),
                  'map_slots': len(geometry), 'totes_per_crane': dict(counts)}
    return {'strategy': data.get('strategy'), 'totes': totes, 'validation': validation}


def sku_index(totes):
    indexed = defaultdict(list)
    for tote in sorted(totes, key=lambda t: (t.crane, t.address)):
        indexed[tote.sku].append(tote)
    return dict(indexed)
