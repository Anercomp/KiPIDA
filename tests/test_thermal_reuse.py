import unittest
from unittest.mock import patch
import numpy as np
from shapely.geometry import box
from thermal_solver import ThermalGrid, ThermalSettings, interpolate_thermal

STACKUP = {'substrate': [{'thickness_mm': 1.6}], 'copper': {3: {'thickness_mm': .035}}}


class TestThermalReuse(unittest.TestCase):
    def test_changed_heat_reuses_matrix_and_matches_fresh(self):
        grid = ThermalGrid(box(0, 0, 4, 4), ThermalSettings())
        try:
            grid.heat[0] = .01
            grid.solve(STACKUP, {}, 'cpu')
            context = grid._system
            grid.heat[0] = .02
            with patch.object(grid, '_assemble', side_effect=AssertionError('Should reuse assembly')):
                reused = grid.solve(STACKUP, {}, 'cpu')
            self.assertIs(grid._system, context)
            self.assertTrue(reused['diagnostics']['thermal_matrix_reused'])
            fresh = ThermalGrid(grid.region, grid.settings)
            fresh.heat[0] = .02
            try:
                np.testing.assert_allclose(reused['temperature_c'], fresh.solve(STACKUP, {}, 'cpu')['temperature_c'])
            finally:
                fresh.close()
            grid.settings.convection_w_m2k = 20
            rebuilt = grid.solve(STACKUP, {}, 'cpu')
            self.assertIsNot(grid._system, context)
            self.assertFalse(rebuilt['diagnostics']['thermal_matrix_reused'])
        finally:
            grid.close()

    def test_coarse_temperature_guess_cannot_cross_cutout(self):
        region = box(0, 0, 4, 4).difference(box(1, 1, 3, 3))
        coarse_grid = ThermalGrid(region, ThermalSettings(spacing_mm=1))
        fine_grid = ThermalGrid(region, ThermalSettings(spacing_mm=.5))
        coarse_grid.heat[:] = coarse_grid.areas*100
        coarse = coarse_grid.solve(STACKUP, {}, 'cpu')
        try:
            guess = interpolate_thermal(coarse, fine_grid)
            fine_grid.heat[:] = fine_grid.areas*100
            actual = fine_grid.solve(STACKUP, {}, 'cpu', initial_rise=guess)
            np.testing.assert_allclose(actual['temperature_c'], 30, atol=1e-8)
            self.assertLess(actual['balance_w'], 1e-9)
        finally:
            coarse_grid.close(); fine_grid.close()
