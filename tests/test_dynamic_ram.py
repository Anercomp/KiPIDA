"""Admission checks distinguish resident memory from future allocations."""
import unittest
from unittest.mock import patch

from shapely.geometry import box

from resource_budget import GIB, MemoryBudgetExceeded, check_budget, feasible_spacing, feasible_thermal_spacing, grid_estimate, resolve_budget


class TestDynamicRam(unittest.TestCase):
    def test_thermal_spacing_suggestion_counts_partial_boundary_cells(self):
        region = box(0, 0, 100.3, 32.7)
        step = feasible_thermal_spacing(region, .1, 1024**2)
        import math
        self.assertLessEqual(math.ceil(100.3/step)*math.ceil(32.7/step)*1000, 1024**2)
        self.assertIsNone(feasible_thermal_spacing(region, .1, 0))
    def test_modes_remain_numeric_and_preserve_dynamic_selection(self):
        with patch('resource_budget.available_ram', return_value=10*GIB):
            automatic, dynamic, manual = resolve_budget('Auto'), resolve_budget('Dynamic'), resolve_budget('8')
        self.assertIsInstance(automatic, float)
        self.assertTrue(automatic.dynamic)
        self.assertTrue(dynamic.dynamic)
        self.assertFalse(manual.dynamic)
        self.assertAlmostEqual(automatic, 6.5)
        self.assertEqual(manual, 8)

    def test_resident_coarse_and_cache_memory_is_not_subtracted_twice(self):
        with patch('resource_budget.available_ram', return_value=10*GIB):
            budget = resolve_budget('dynamic')
        # Allocating a coarse mesh reduced free RAM from 10 GiB to 4 GiB.
        # The 2 GiB refined pass still fits the 2.6 GiB current allowance.
        with patch('resource_budget.available_ram', return_value=4*GIB):
            self.assertEqual(check_budget(2*GIB, budget, retained_bytes=6*GIB), 8*GIB)

    def test_dynamic_admission_rechecks_ram_after_external_pressure(self):
        with patch('resource_budget.available_ram', return_value=10*GIB):
            budget = resolve_budget('Auto')
        with patch('resource_budget.available_ram', return_value=GIB):
            with self.assertRaises(MemoryBudgetExceeded) as failure:
                check_budget(2*GIB, budget, retained_bytes=6*GIB)
        error = failure.exception
        self.assertTrue(error.dynamic)
        self.assertEqual(error.available_bytes, GIB)
        self.assertLessEqual(error.allowed_additional_bytes, .65*GIB)
        self.assertEqual(error.additional_bytes, 2*GIB)

    def test_dynamic_admission_can_grow_when_other_apps_release_ram(self):
        with patch('resource_budget.available_ram', return_value=GIB):
            budget = resolve_budget('Auto')
        with patch('resource_budget.available_ram', return_value=10*GIB):
            self.assertEqual(check_budget(5*GIB, budget, retained_bytes=2*GIB), 7*GIB)

    def test_cached_run_with_no_future_allocation_does_not_need_free_ram(self):
        with patch('resource_budget.available_ram', return_value=10*GIB):
            budget = resolve_budget('Auto')
        with patch('resource_budget.available_ram', return_value=0):
            self.assertEqual(check_budget(0, budget, retained_bytes=8*GIB), 8*GIB)

    def test_manual_cap_counts_retained_once_and_physical_guard_only_additional(self):
        budget = resolve_budget('8')
        with patch('resource_budget.available_ram', return_value=4*GIB):
            self.assertEqual(check_budget(2*GIB, budget, retained_bytes=6*GIB), 8*GIB)
            with self.assertRaises(MemoryBudgetExceeded) as failure:
                check_budget(2*GIB, budget, retained_bytes=7*GIB)
        self.assertFalse(failure.exception.dynamic)
        self.assertEqual(failure.exception.allowed_additional_bytes, GIB)

    def test_manual_limit_cannot_override_physical_headroom(self):
        with patch('resource_budget.available_ram', return_value=GIB):
            with self.assertRaises(MemoryBudgetExceeded):
                check_budget(.8*GIB, resolve_budget('64'), retained_bytes=.3*GIB)

    def test_invalid_estimates_are_rejected_before_allocation(self):
        for amount in (-1, float('nan'), float('inf')):
            with self.subTest(amount=amount), self.assertRaisesRegex(ValueError, 'nonnegative'):
                check_budget(amount, 8)
            with self.subTest(retained=amount), self.assertRaisesRegex(ValueError, 'nonnegative'):
                check_budget(0, 8, amount)

    def test_suggested_spacing_fits_estimate_and_does_not_change_request(self):
        geometries, requested = [{3: box(0, 0, 100, 100)}], .0125
        suggestion = feasible_spacing(geometries, requested, 2*GIB)
        self.assertGreater(suggestion, requested)
        self.assertLessEqual(sum(grid_estimate(geo, suggestion)[1] for geo in geometries), 2*GIB)
        self.assertEqual(requested, .0125)
        self.assertIsNone(feasible_spacing(geometries, requested, 0))
        self.assertIsNone(feasible_spacing(geometries, requested, -GIB))


if __name__ == '__main__':
    unittest.main()
