"""ETA samples are optional, validated and comparable to current work."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simulation_timing import TimingHistory, calibration, format_duration, STAGES


def features(**changes):
    value = dict(backend='cpu', nets=2, mesh_points=1000, thermal_cells=100, thermal=True)
    value.update(changes)
    return value


class TestTimingHistory(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'timings.json'
        self.history = TimingHistory(self.path)
        self.timings = dict(geometry=1, mesh=2, electrical=3, thermal=4, render=5)

    def test_missing_corrupt_and_malformed_history_is_ignored(self):
        self.assertIsNone(self.history.estimate(features()))
        for data in ('broken JSON', '[]', '{"records":{}}', '{"records":[null,{},1]}'):
            self.path.write_text(data)
            history = TimingHistory(self.path)
            self.assertEqual(history.records, [])
            self.assertIsNone(history.estimate(features()))

    def test_same_work_and_linear_scaling_use_approximate_ranges(self):
        self.history.record(features(), self.timings)
        low, high, count = self.history.estimate(features())
        self.assertEqual((low, high, count), (7.5, 30, 1))
        low, high, count = self.history.estimate(features(nets=4, mesh_points=2000, thermal_cells=200))
        self.assertEqual((low, high, count), (15, 60, 1))
        self.assertIsNone(self.history.estimate(features(backend='gpu')))
        self.history.hardware = 'different computer'
        self.assertIsNone(self.history.estimate(features()))

    def test_invalid_samples_do_not_enter_history_or_mutate_valid_record(self):
        self.history.record(features(), self.timings)
        for value in (float('nan'), float('inf'), -1, True, '2'):
            bad = self.timings | {'mesh': value}
            self.history.record(features(), bad)
        self.assertEqual(len(self.history.records), 1)
        source_features = features()
        source_timings = self.timings.copy()
        self.history.record(source_features, source_timings)
        source_features['mesh_cached'] = True
        source_timings['mesh'] = 0
        self.assertNotIn('mesh_cached', self.history.records[-1]['features'])
        self.assertEqual(self.history.records[-1]['timings']['mesh'], 2)

    def test_cached_runs_do_not_train_fresh_work_and_skipped_current_work_is_removed(self):
        self.history.record(features(mesh_cached=True, electrical_cached=True), self.timings | {'mesh': 0, 'electrical': 0})
        self.assertIsNone(self.history.estimate(features()))
        self.history.record(features(), self.timings)
        low, high, count = self.history.estimate(features(geometry_cached=True, mesh_cached=True, electrical_cached=True))
        self.assertEqual((low, high, count), (4.5, 18, 2))  # Thermal + render only.
        self.assertEqual(self.history.estimate(features(thermal=False)), (5.5, 22, 1))

    def test_thermal_and_prepared_solver_state_must_be_comparable(self):
        self.history.record(features(thermal=False), self.timings | {'thermal': 0})
        self.assertIsNone(self.history.estimate(features()))
        self.history.record(features(prepared_cached=True), self.timings)
        self.assertIsNone(self.history.estimate(features()))
        self.assertEqual(self.history.estimate(features(prepared_cached=True)), (7.5, 30, 1))

    def test_history_limit_persistence_and_optional_write_failure(self):
        for index in range(85):
            self.history.record(features(), self.timings | {'mesh': index})
        self.assertEqual(len(self.history.records), 80)
        self.assertEqual(self.history.records[0]['timings']['mesh'], 5)
        restored = TimingHistory(self.path)
        self.assertEqual(restored.records, self.history.records)
        with patch.object(Path, 'replace', side_effect=OSError('Read-only directory')):
            self.history.record(features(), self.timings)
        self.assertEqual(len(self.history.records), 80)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_calibration_has_wider_uncertainty_and_finite_real_stage_timings(self):
        self.history.record(features(), self.timings, calibration=True)
        self.assertEqual(self.history.estimate(features()), (3.75, 60, 1))
        benchmark_features, benchmark_timings = calibration('cpu')
        self.assertEqual(set(benchmark_timings), set(STAGES))
        self.assertTrue(all(math.isfinite(value) and value > 0 for value in benchmark_timings.values()))
        self.assertGreater(benchmark_features['mesh_points'], 1000)
        self.assertEqual(benchmark_features['thermal_cells'], 400)
        self.history = TimingHistory(self.path.with_name('calibration.json'))
        self.history.record(benchmark_features, benchmark_timings, calibration=True)
        self.assertIsNotNone(self.history.estimate(benchmark_features))

    def test_missing_stage_and_malformed_runtime_records_are_ignored(self):
        self.history.record(features(), {'geometry': 1})
        self.history.records.extend([None, {'hardware': self.history.hardware, 'features': None}])
        self.assertIsNone(self.history.estimate(features()))
        self.assertIsNone(self.history.estimate(features(mesh_points=float('nan'))))
        self.assertIsNone(self.history.estimate(features(mesh_points=10**1000)))

    def test_duration_format(self):
        self.assertEqual(format_duration(-4), '0s')
        self.assertEqual(format_duration(15.2), '15s')
        self.assertEqual(format_duration(70), '1m 10s')
        self.assertEqual(format_duration(3670), '1h 1m')


if __name__ == '__main__':
    unittest.main()
