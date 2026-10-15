
import unittest
import sys
import os
import math

try:
    from shapely.geometry import LineString, Polygon, Point, box
    import shapely.affinity
except ImportError:
    pass

# Include plugin dir
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

# 1. Pcbnew Mock
class MockPcbnew:
    PAD_SHAPE_CIRCLE = 1
    PAD_SHAPE_RECT = 2
    PAD_SHAPE_ROUNDRECT = 3
    PAD_SHAPE_OVAL = 4
    
    class ActionPlugin: pass
    class PCB_VIA: pass
    class PCB_TRACK: pass
    
    @staticmethod
    def ToMM(val): return val / 1000000.0
    
    @staticmethod
    def IsCopperLayer(lid): return lid in [0, 31]

if 'pcbnew' not in sys.modules:
    sys.modules['pcbnew'] = MockPcbnew

import pcbnew

# 2. Other Mocks
class MockBoard:
    def __init__(self):
        self.tracks = []
        self.footprints = []
        self.zones = []
        self.design_settings = MockDesignSettings()
        self.layers_copper = {0: "F_Cu", 31: "B_Cu"} # Example IDs

    def GetDesignSettings(self): return self.design_settings
    def GetEnabledLayers(self): return MockSeq(list(self.layers_copper.keys()))
    def IsLayerCopper(self, layer_id): return layer_id in self.layers_copper
    def GetLayerName(self, layer_id): return self.layers_copper.get(layer_id, "Unknown")
    def FindNet(self, name): return MockNet(1, name)
    def GetTracks(self): return self.tracks
    def GetFootprints(self): return self.footprints
    def GetZones(self): return self.zones
    def Zones(self): return self.zones

class MockSeq:
    def __init__(self, items): self.items = items
    def Seq(self): return self.items

class MockDesignSettings:
    def GetStackupDescriptor(self): return None # Not implemented in mock yet

class MockNet:
    def __init__(self, code, name):
        self.code = code
        self.name = name
    def GetNetCode(self): return self.code

class MockTrack(MockPcbnew.PCB_TRACK):
    def __init__(self, net_code, layer, start, end, width):
        self.net_code = net_code
        self.layer = layer
        self.start = MockPoint(start[0], start[1])
        self.end = MockPoint(end[0], end[1])
        self.width = width
        self.classname = "PCB_TRACK"
    def GetNetCode(self): return self.net_code
    def GetLayer(self): return self.layer
    def GetWidth(self): return self.width
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetClass(self): return self.classname

class MockPoint:
    def __init__(self, x, y): self.x, self.y = x, y

from extractor import GeometryExtractor

class TestGeometryExtractor(unittest.TestCase):
    def setUp(self):
        self.board = MockBoard()
        self.extractor = GeometryExtractor(self.board, debug=False, log_callback=None)
        
    def test_stackup_defaults(self):
        from kipy.proto.board.board_types_pb2 import BoardLayer
        stackup = self.extractor.get_board_stackup()
        self.assertEqual(stackup['layer_order'], [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu])
        self.assertEqual(stackup['copper'][BoardLayer.BL_F_Cu]['thickness_mm'], .035)

    def test_track_extraction(self):
        from types import SimpleNamespace as NS
        from kipy.proto.board.board_types_pb2 import BoardLayer
        lid = BoardLayer.BL_F_Cu
        self.board.tracks.append(NS(net=NS(name='TestNet'), layer=lid,
            start=NS(x=0, y=0), end=NS(x=10000000, y=0), width=500000))
        geo = self.extractor.get_net_geometry('TestNet')
        self.assertIn(lid, geo)
        self.assertAlmostEqual(geo[lid].area, 5 + math.pi*.25**2, delta=.01)
        self.assertAlmostEqual(geo[lid].bounds[3]-geo[lid].bounds[1], .5)
