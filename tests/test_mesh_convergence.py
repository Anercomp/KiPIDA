import unittest
from mesh_convergence import compare_meshes, release_coarse_arrays


def result(drop, loss):
    return {'+12V': {'stats': (12-drop, 12, drop), 'diagnostics': {'copper_loss_w': loss}}}


class TestMeshConvergence(unittest.TestCase):
    def test_low_memory_reference_preserves_all_convergence_checks(self):
        coarse = result(.01, .04)
        coarse['+12V'].update(mesh=object(), results=object())
        parts = [dict(ref='F1', net_a='A', net_b='B', current_a=1, drop_v=.01, loss_w=.01)]
        fine = result(.013, .05)
        fine_parts = [dict(parts[0], current_a=2, loss_w=.02)]
        expected = compare_meshes(coarse, fine, parts, fine_parts)
        release_coarse_arrays(coarse)
        self.assertNotIn('mesh', coarse['+12V'])
        self.assertNotIn('results', coarse['+12V'])
        self.assertEqual(compare_meshes(coarse, fine, parts, fine_parts), expected)
        self.assertEqual(len(expected), 4)
    def test_reports_actual_drop_and_loss_failure(self):
        log = []
        failures = compare_meshes(result(.01, .04), result(.013, .05), [], [], log.append)
        self.assertEqual(len(failures), 2)
        self.assertIn('coarse 0.01 V, refined 0.013 V', failures[0])
        self.assertIn('copper loss', failures[1])
        self.assertEqual(len(log), 2)

    def test_microvolt_and_nanowatt_noise_do_not_fail_relative_check(self):
        self.assertEqual(compare_meshes(result(1e-9, 1e-12), result(1e-8, 1e-11), [], []), [])
        self.assertTrue(compare_meshes(result(1e-5, .1), result(2e-5, .1), [], []))

    def test_component_matching_uses_identity_not_list_order(self):
        parts = [dict(ref=r, net_a='A', net_b='B', current_a=1, drop_v=.01, loss_w=.01) for r in ('F1', 'R1')]
        self.assertEqual(compare_meshes(result(.01, .1), result(.01, .1), parts, list(reversed(parts))), [])
