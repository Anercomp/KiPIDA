import unittest
from models import PowerRail, SeriesComponent, series_links_for_rail
from ui.sorting import natural_key


class TestSeriesChainView(unittest.TestCase):
    def test_two_links_visible_from_every_rail_in_chain(self):
        a, b, c, x = [PowerRail(name) for name in ('A', 'B', 'C', 'X')]
        first = SeriesComponent('F1', 'A', '1', 'B', '2')
        second = SeriesComponent('R3', 'B', '1', 'C', '2', closed=False)
        unrelated = SeriesComponent('R29', 'X', '1', 'Y', '2')
        a.series_components.append(first)
        b.series_components.append(second)
        x.series_components.append(unrelated)
        for net in ('A', 'B', 'C'):
            visible = series_links_for_rail([a, b, c, x], net)
            self.assertEqual([link for _, link in visible], [first, second])
            self.assertIs(visible[1][0], b)
        b.series_components.remove(second)
        self.assertEqual(series_links_for_rail([a, b, c, x], 'A'), [(a, first)])
        self.assertEqual(series_links_for_rail([a, b, c, x], 'C'), [])

    def test_cycle_and_branch_do_not_duplicate_rows(self):
        rails = [PowerRail(n) for n in ('A', 'B', 'C', 'D')]
        links = [SeriesComponent(f'R{i}', a, '1', b, '2') for i, (a, b) in enumerate(
                 [('A', 'B'), ('B', 'C'), ('C', 'A'), ('B', 'D')])]
        rails[0].series_components.extend(links)
        self.assertEqual([link for _, link in series_links_for_rail(rails, 'D')], links)

    def test_reference_numbers_sort_numerically(self):
        self.assertEqual(sorted(['R29', 'R3', 'R100', 'R2', 'R10', 'C12', 'C2'], key=natural_key),
                         ['C2', 'C12', 'R2', 'R3', 'R10', 'R29', 'R100'])
        self.assertEqual(sorted(['10', '3', '29', '2'], key=natural_key), ['2', '3', '10', '29'])

    def test_default_resistance_is_zero(self):
        self.assertEqual(SeriesComponent('R3', 'A', '1', 'B', '2').resistance_ohm, 0)
