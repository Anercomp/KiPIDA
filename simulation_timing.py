"""Approximate stage-based ETA from local timings, never a convergence promise."""
from pathlib import Path
import json
import math
import os
import platform
import statistics
import tempfile
import time

STAGES = ('geometry', 'mesh', 'electrical', 'thermal', 'render')


def _finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _valid_features(features):
    if not isinstance(features, dict) or features.get('backend') not in ('auto', 'cpu', 'gpu'):
        return False
    if any(not _finite_number(features.get(name, 0)) for name in ('nets', 'mesh_points', 'thermal_cells')):
        return False
    return all(isinstance(value, bool) for name, value in features.items()
               if name.endswith('_cached') or name == 'thermal')


def _valid_record(record):
    return (isinstance(record, dict) and isinstance(record.get('hardware'), str)
            and _valid_features(record.get('features')) and isinstance(record.get('timings'), dict)
            and isinstance(record.get('calibration', False), bool)
            and all(_finite_number(value) for value in record['timings'].values()))


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds//60}m {seconds%60}s'
    return f'{seconds//3600}h {(seconds%3600)//60}m'


class TimingHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(tempfile.gettempdir())/'kipida-run-timings-v1.json'
        self.records = []
        self.hardware = platform.node()+'|'+platform.machine()+'|'+platform.processor()
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
            records = document.get('records', []) if isinstance(document, dict) else []
            if isinstance(records, list):
                self.records = [record for record in records if _valid_record(record)][-80:]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def record(self, features, timings, calibration=False):
        record = dict(hardware=self.hardware, features=features, timings=timings,
                      calibration=calibration, timestamp=time.time())
        if not _valid_record(record):
            return
        try:
            # A caller can update its feature flags as stages complete. Retain an
            # independent sample and reject unsupported/non-finite JSON values.
            record = json.loads(json.dumps(record, allow_nan=False))
        except (TypeError, ValueError):
            return
        self.records.append(record)
        self.records = self.records[-80:]
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='.kipida-timings-', suffix='.tmp', dir=self.path.parent)
            os.close(descriptor)
            temporary = Path(name)
            temporary.write_text(json.dumps({'records': self.records}, allow_nan=False), encoding='utf-8')
            temporary.replace(self.path)
        except OSError:
            pass  # History is optional; a completed simulation remains valid.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def estimate(self, features):
        if not _valid_features(features):
            return None
        candidates = [r for r in self.records if _valid_record(r) and r.get('hardware') == self.hardware
                      and r.get('features', {}).get('backend') == features.get('backend')]
        estimates = []
        calibration_samples = []
        work = {'geometry': 'nets', 'mesh': 'mesh_points', 'electrical': 'mesh_points',
                'thermal': 'thermal_cells', 'render': 'nets'}
        for record in candidates[-15:]:
            previous = record['features']; timings = record['timings']; total = 0
            valid = True
            for stage in STAGES:
                if features.get(stage+'_cached', False) or (stage == 'thermal' and not features.get('thermal')):
                    continue
                if previous.get(stage+'_cached', False):
                    # A skipped mesh/solve is not a benchmark of fresh work.
                    valid = False; break
                if stage == 'electrical' and previous.get('prepared_cached', False) != features.get('prepared_cached', False):
                    valid = False; break
                units = features.get(work[stage], 1)
                old_units = previous.get(work[stage], 0)
                if stage == 'thermal' and not previous.get('thermal'):
                    valid = False; break
                if units and (not old_units or stage not in timings):
                    valid = False; break
                total += timings.get(stage, 0)*units/max(1, old_units)
            if valid and math.isfinite(total):
                estimates.append(max(.1, total))
                calibration_samples.append(record.get('calibration', False))
        if not estimates:
            return None
        middle = statistics.median(estimates)
        # Geometry complexity and iterative convergence cannot be inferred from size alone.
        calibrated_only = all(calibration_samples)
        low, high = (.25, 4) if calibrated_only else (.5, 2)
        return middle*low, max(middle*high, max(estimates)), len(estimates)


def calibration(backend='cpu'):
    """Small separate board-shaped benchmark of production numerical paths."""
    from types import SimpleNamespace as NS
    from shapely.geometry import box
    from mesh import Mesher
    from electrical_solver import Solver
    from extractor import GeometryExtractor
    from resource_budget import grid_estimate
    from thermal_solver import ThermalGrid, ThermalSettings
    stackup = {'copper': {3: {'thickness_mm': .035}}, 'substrate': [{'thickness_mm': 1.6}],
               'resistivity': 1.68e-5}
    outline = NS(nodes=[NS(point=NS(x=x*1000000, y=y*1000000))
                        for x, y in ((0, 0), (20, 0), (20, 20), (0, 20))])
    zone = NS(net=NS(name='BENCH'), filled_polygons={3: [NS(outline=outline, holes=[])]})
    board = NS(tracks=[], vias=[], footprints=[], zones=[zone])
    extractor = GeometryExtractor(board)
    extractor._stackup_cache = stackup
    started = time.perf_counter()
    geometry = extractor.get_net_geometry('BENCH')
    extraction = time.perf_counter()-started
    started = time.perf_counter()
    mesh = Mesher(NS(footprints=[], vias=[])).generate_mesh('BENCH', geometry, stackup, .2)
    meshing = time.perf_counter()-started
    solver = Solver(backend=backend)
    started = time.perf_counter()
    try:
        solver.solve(mesh, [{'node_id': 0, 'voltage': 12}], [{'node_id': len(mesh.nodes)-1, 'current': 1}])
    finally:
        solver.close()
    electrical = time.perf_counter()-started
    started = time.perf_counter()
    grid = ThermalGrid(box(0, 0, 20, 20), ThermalSettings())
    try:
        grid.heat[:] = grid.areas*100
        thermal_result = grid.solve(stackup, geometry, backend)
    finally:
        grid.close()
    thermal = time.perf_counter()-started
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    started = time.perf_counter()
    figure = Figure(figsize=(4, 3))
    try:
        canvas = FigureCanvasAgg(figure)
        axis = figure.add_subplot()
        axis.imshow(thermal_result['temperature_c'].reshape(grid.ids.shape), origin='lower')
        canvas.draw()
    finally:
        figure.clear()
    rendering = time.perf_counter()-started
    return (dict(backend=backend, nets=1, mesh_points=grid_estimate(geometry, .2)[0],
                 thermal_cells=len(grid.heat), thermal=True),
            dict(geometry=extraction, mesh=meshing, electrical=electrical, thermal=thermal, render=rendering))
