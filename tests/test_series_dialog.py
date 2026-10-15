import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from kipy.board_types import FootprintInstance, Pad


class TestSeriesComponentDiscovery(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1]/'ui'/'series_dialog.py'
        spec = importlib.util.spec_from_file_location('tested_series_dialog', path)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'wx': NS(Dialog=object)}):
            spec.loader.exec_module(self.module)

    def test_real_ipc_reference_objects_and_definition_pads(self):
        footprints = []
        for ref in ('F2', 'F1'):
            template = FootprintInstance()
            template.reference_field.text.value = ref
            proto = template.proto
            for number, net in [('1', 'VIN'), ('2', 'VOUT')]:
                pad = Pad().proto
                pad.number = number
                pad.net.name = net
                proto.definition.items.add().Pack(pad)
            footprint = FootprintInstance(proto)
            self.assertEqual(footprint.reference_field.text.value, ref)
            footprints.append(footprint)
        components = self.module.component_terminals(NS(get_footprints=lambda: footprints))
        self.assertEqual(sorted(components), ['F1', 'F2'])
        self.assertEqual(components['F1'], [('1', 'VIN'), ('2', 'VOUT')])

    def test_plain_reference_and_single_net_filter(self):
        footprints = [NS(reference='R1', pads=[NS(number='1', net=NS(name='A')), NS(number='2', net=NS(name='B'))]),
                      NS(reference='C1', pads=[NS(number='1', net=NS(name='A')), NS(number='2', net=NS(name='A'))])]
        self.assertEqual(self.module.component_terminals(NS(footprints=footprints)), {'R1': [('1', 'A'), ('2', 'B')]})
