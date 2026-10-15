"""Conservative board heat placement for the solved regulator power balance."""
import math
from copper_geometry import value


def regulator_losses(results):
    records, seen = [], set()
    for data in results.values():
        for record in data.get('regulator_losses', []):
            if record['name'] in seen:
                raise ValueError(f'Duplicate regulator heat record: {record["name"]}.')
            loss = record['loss_w']
            if not math.isfinite(loss) or loss < 0:
                raise ValueError(f'Invalid regulator heat: {record["name"]}.')
            seen.add(record['name'])
            records.append(record)
    return records


def _reference(footprint):
    ref = value(footprint, 'reference', value(footprint, 'ref_des', ''))
    if not ref:
        ref = value(value(value(footprint, 'reference_field'), 'text'), 'value', '')
    return ref


def add_regulator_heat(grid, results, board, resolve, log=None):
    records = regulator_losses(results)
    if not records:
        return 0.0
    footprints = value(board, 'footprints')
    if footprints is None:
        getter = getattr(board, 'get_footprints', None)
        footprints = getter() if getter else []
    footprints = {_reference(fp): fp for fp in footprints}
    total = 0.0
    for record in records:
        loss = record['loss_w']
        if loss == 0:
            continue
        terminals = [(record['input_ref'], record['input_rail'], record['input_pads']),
                     (record['output_ref'], record['output_rail'], record['output_pads'])]
        refs = list(dict.fromkeys(ref for ref, _, _ in terminals))
        points, watts = [], []
        for ref in refs:
            position = value(footprints.get(ref), 'position')
            center = (position.x/1e6, position.y/1e6) if position is not None else None
            if center is not None:
                from shapely.geometry import Point
                if not grid.region.covers(Point(center)):
                    center = None
            if center is not None:
                points.append(center); watts.append(loss/len(refs))
            else:
                # Missing/off-board body origin: use all configured pad contacts
                # for this footprint and conserve its assigned share of watts.
                contacts = []
                for terminal_ref, net, pads in terminals:
                    if terminal_ref != ref:
                        continue
                    mesh = results[net]['mesh']
                    nodes = resolve(mesh, ref, pads)
                    if not nodes:
                        raise ValueError(f'Cannot place regulator heat for {record["name"]}: {ref} has no mapped pads.')
                    coordinates = mesh.node_coords.take(nodes) if hasattr(mesh.node_coords, 'take') else [mesh.node_coords[n] for n in nodes]
                    contacts.extend((float(x), float(y)) for x, y, _ in coordinates)
                contacts = list(dict.fromkeys(contacts))
                points.extend(contacts)
                watts.extend([loss/(len(refs)*len(contacts))]*len(contacts))
        grid.deposit(points, watts)
        total += loss
        if log:
            placement = ', '.join(refs)
            log(f'Thermal regulator heat: {record["name"]}, {loss:.6g} W at {placement}'
                + (' (split equally between configured footprints).' if len(refs) > 1 else '.'))
    return total
