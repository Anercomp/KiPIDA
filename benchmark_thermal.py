"""Compare exact thermal assembly before/after optimization on the live board."""
import importlib.util
import sys
import time
import json
from pathlib import Path
import numpy as np
import kipy
from shapely.geometry import box, Point
from shapely.ops import unary_union
from extractor import GeometryExtractor
from thermal_solver import board_region

root = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('thermal_baseline', root/'thermal_solver_084_baseline.py')
baseline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = baseline
spec.loader.exec_module(baseline)
import thermal_solver as optimized
if '--synthetic' in sys.argv:
    region = box(0, 0, 30, 30).difference(box(1, 1, 3, 3))
    holes = unary_union([Point(x+.3, y+.3).buffer(.22, quad_segs=16)
                         for x in range(1, 30, 2) for y in range(1, 30, 2)])
    copper = {layer: region.difference(holes) for layer in (3, 4, 5, 34)}
    stackup = {'copper': {layer: {'thickness_mm': .035} for layer in copper},
               'substrate': [{'thickness_mm': 1.6}]}
    point = Point(15, 15)
    print('Synthetic benchmark: 30 mm board, four perforated copper layers, 225 holes per layer.', flush=True)
else:
    board = kipy.KiCad(timeout_ms=3000).get_board()
    print('Connected; reading GND copper for thermal benchmark.', flush=True)
    extractor = GeometryExtractor(board)
    stackup = extractor.get_board_stackup()
    region = board_region(board)
    copper = extractor.get_net_geometry('GND')
    point = region.representative_point()
    region = region.intersection(box(point.x-15, point.y-15, point.x+15, point.y+15))
    print(f'Benchmarking {region.area:.1f} mm² patch of actual board/GND geometry.', flush=True)
report = []
for step in (1, .5):
    outputs, times = {}, {}
    for name, module in [('baseline', baseline), ('optimized', optimized)]:
        started = time.perf_counter()
        grid = module.ThermalGrid(region, module.ThermalSettings(spacing_mm=step))
        build_seconds = time.perf_counter()-started
        grid.deposit([[point.x, point.y]], [.1])  # Synthetic heat, not board configuration.
        started = time.perf_counter()
        outputs[name] = grid.solve(stackup, copper, 'cpu')
        solve_seconds = time.perf_counter()-started
        times[name] = dict(grid_seconds=build_seconds, assembly_and_solve_seconds=solve_seconds,
                           total_seconds=build_seconds+solve_seconds)
        print(f'{step:g} mm {name}: {times[name]}', flush=True)
    difference = float(np.max(np.abs(outputs['baseline']['temperature_c']-outputs['optimized']['temperature_c'])))
    assert difference < 1e-8, difference
    record = dict(synthetic='--synthetic' in sys.argv, area_mm2=region.area, spacing_mm=step, cells=len(outputs['optimized']['xy']), timings=times,
                  speedup=times['baseline']['total_seconds']/times['optimized']['total_seconds'],
                  maximum_temperature_difference_c=difference)
    report.append(record)
    print(json.dumps(record), flush=True)
(root/'benchmark-thermal.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
