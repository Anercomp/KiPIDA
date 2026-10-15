from copper_geometry import pad_geometry, polygon_from_api, drill_geometry
import logging
import math
try:
    from shapely.geometry import LineString, Polygon, MultiPolygon, Point, box
    from shapely.ops import unary_union
    from shapely import affinity
except ImportError:
    LineString = Polygon = MultiPolygon = Point = box = unary_union = affinity = None

try:
    import kipy
    import kipy.board
    import os
    import tempfile
    import sys
    HAS_KIPY = True
except ImportError:
    HAS_KIPY = False

# Constants mapping (approximate)
# Needs to be verified against kipy enums
# For now defining local helpers
def to_mm(val_nm):
    return val_nm / 1e6

class GeometryExtractor:
    def __init__(self, board, debug=False, log_callback=None):
        self.board = board
        self.debug = debug
        self.log_callback = log_callback
        self.logger = logging.getLogger('KiPIDA.GeometryExtractor')
        if debug:
            self.logger.setLevel(logging.DEBUG)
            # Clear any existing handlers to avoid duplicates
            self.logger.handlers.clear()
            if log_callback:
                # Use custom handler that calls the UI log callback
                class UILogHandler(logging.Handler):
                    def __init__(self, callback):
                        super().__init__()
                        self.callback = callback
                    def emit(self, record):
                        msg = self.format(record)
                        self.callback(msg)
                
                handler = UILogHandler(log_callback)
                handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
                self.logger.addHandler(handler)
            else:
                # Fallback to stderr
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
                self.logger.addHandler(handler)
    def _get_val(self, obj, attr_name, default=None):
        """Robustly get attribute value from object (property or getter)."""
        if obj is None: return default
        if hasattr(obj, attr_name):
            val = getattr(obj, attr_name)
            if val is not None: return val
        for prefix in ["get_", ""]:
            method_name = prefix + attr_name
            if hasattr(obj, method_name):
                try:
                    val = getattr(obj, method_name)()
                    if val is not None: return val
                except: pass
        return default

    def _get_board_items(self, attr_name):
        """Robustly get items from board (property or getter)."""
        if hasattr(self.board, attr_name):
            return getattr(self.board, attr_name)
        method_name = f"get_{attr_name}"
        if hasattr(self.board, method_name):
            try: return getattr(self.board, method_name)()
            except: pass
        return []

    # Cache for stackup data
    _stackup_cache = None

    def get_board_stackup(self):
        """
        Extracts physical stackup properties.
        Returns a dict with copper data and substrate segments.
        """
        # Return cached stackup if available
        if self._stackup_cache is not None:
            return self._stackup_cache
        
        # Try Protobuf API first
        if HAS_KIPY:
            try:
                stackup = self._get_stackup_protobuf()
                if stackup:
                    self._stackup_cache = stackup
                    return stackup
            except Exception as e:
                if hasattr(self.board, 'get_stackup'):
                    raise ValueError("Cannot read the board's physical stackup. Check the KiCad API connection and board setup.") from e
                msg = f"Protobuf API stackup extraction failed: {e}. Falling back to defaults."
                if self.debug:
                    self.logger.warning(msg)
                else:
                    print(msg)
        
        return self._get_stackup_defaults()

    def _get_stackup_protobuf(self):
        """Extract stackup using the new KiCad Protobuf API (kipy)."""
        if self.debug:
            self.logger.debug("Attempting stackup extraction via Protobuf API (kicad-python)...")
            
        try:
            # Note: We assume self.board is already a kipy Board object connected to KiCad
            if not self.board:
                return None
            
            stackup = self.board.get_stackup()
            
            rho_copper = 1.68e-5 # Ohm-mm
            copper_data = {}
            layer_order = []
            substrates = []
            
            last_copper = None
            
            # Helper to find dielectric constant
            def get_epsilon_r(dielectric_layer):
                # Check for sub-layers (KiCad 9 structure)
                # BoardStackupDielectricLayer -> layers (list of BoardStackupDielectricProperties)
                if hasattr(dielectric_layer, 'layers') and dielectric_layer.layers:
                    return dielectric_layer.layers[0].epsilon_r
                return 4.4 # Default FR4
            
            def get_material(dielectric_layer):
                if hasattr(dielectric_layer, 'layers') and dielectric_layer.layers:
                    return dielectric_layer.layers[0].material_name
                return "FR4"

            # Iterate layers
            for i, layer in enumerate(stackup.layers):
                # layer is BoardStackupLayer
                
                type_val = layer.type
                thickness_mm = layer.thickness / 1e6
                
                is_copper = False
                is_dielectric = False
                
                # Check layer type
                # type 1 = Copper, type 2 = Dielectric
                if type_val == 1 or str(type_val) == 'BS_COPPER' or 'COPPER' in str(type_val):
                    is_copper = True
                elif type_val == 2 or str(type_val) == 'BS_DIELECTRIC' or 'DIELECTRIC' in str(type_val):
                    is_dielectric = True
                # Ignore other types (silkscreen, mask, paste, etc.)

                if is_copper:
                    lid = layer.layer # The actual KiCad Layer ID 
                    name = layer.user_name
                    
                    copper_data[lid] = {
                        'name': name,
                        'thickness_mm': thickness_mm if thickness_mm > 0 else 0.035
                    }
                    layer_order.append(lid)
                    
                    # Link pending substrate
                    if len(substrates) > 0 and substrates[-1]['between'][1] is None:
                        substrates[-1]['between'][1] = lid
                        
                    last_copper = lid
                    
                elif is_dielectric:
                    if last_copper is not None:
                        if len(substrates) == 0 or substrates[-1]['between'][1] is not None:
                            d_layer = layer.dielectric
                            eps = get_epsilon_r(d_layer)
                            mat = get_material(d_layer)
                            
                            substrates.append({
                                'thickness_mm': thickness_mm,
                                'between': [last_copper, None],
                                'material': mat,
                                'epsilon_r': eps
                            })
                        else:
                            substrates[-1]['thickness_mm'] += thickness_mm

            result = {
                'copper': copper_data,
                'layer_order': layer_order,
                'substrate': substrates,
                'resistivity': rho_copper
            }
            if not copper_data:
                raise ValueError("The board stackup contains no copper layers.")
            
            if self.debug:
                self.logger.debug("Extracted Stackup Details:")
                for lid in layer_order:
                    data = copper_data[lid]
                    self.logger.debug(f"  Copper Layer {lid} ('{data['name']}'): thickness = {data['thickness_mm']:.4f} mm")
                
                for i, sub in enumerate(substrates):
                    l1, l2 = sub['between']
                    n1 = copper_data.get(l1, {}).get('name', str(l1))
                    n2 = copper_data.get(l2, {}).get('name', str(l2)) if l2 is not None else "Bottom"
                    self.logger.debug(f"  Dielectric {i+1} (between {n1} and {n2}): thickness = {sub['thickness_mm']:.4f} mm, eps_r = {sub['epsilon_r']:.2f}")
            
            return result
        except Exception as e:
            msg = str(e)
            if "Timed out" in msg:
                 self.logger.warning("Protobuf API timed out (Deadlock). Falling back.")
            if self.debug:
                self.logger.debug(f"Protobuf extraction error: {e}")
            raise e

    def _get_stackup_defaults(self):
        """Returns a default 2-layer stackup (Top/Bottom Copper, FR4 dielectric)."""
        msg = "Using hardcoded default stackup (2-Layer)."
        self.logger.warning(msg)
        if self.log_callback:
            self.log_callback(msg + " Verify the stackup before trusting results.")

        rho_copper = 1.68e-5 # Ohm-mm
        
        # Default to F.Cu (0) and B.Cu (31)
        if HAS_KIPY:
            from kipy.proto.board.board_types_pb2 import BoardLayer
            f_cu_id, b_cu_id = BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu
        else:
            f_cu_id, b_cu_id = 0, 31
        
        copper_data = {
            f_cu_id: {'name': 'F.Cu', 'thickness_mm': 0.035},
            b_cu_id: {'name': 'B.Cu', 'thickness_mm': 0.035}
        }
        
        layer_order = [f_cu_id, b_cu_id]
        
        substrates = [{
            'thickness_mm': 1.6,
            'between': [f_cu_id, b_cu_id],
            'material': 'FR4',
            'epsilon_r': 4.4
        }]
        
        result = {
            'copper': copper_data,
            'layer_order': layer_order,
            'substrate': substrates,
            'resistivity': rho_copper
        }
        
        self._stackup_cache = result
        return result

    def get_net_geometry(self, net_name):
        """
        Extracts and merges geometry for a specific net; None includes all nets.
        Returns a dictionary: { layer_id: shapely.geometry.Polygon }
        """
        drill_voids = {}
        layer_shapes = {} # { layer_id: [shapely_objects] }

        def add_shape(layer, shape):
            if layer not in layer_shapes:
                layer_shapes[layer] = []
            layer_shapes[layer].append(shape)
            
        def is_copper(lid):
            stackup = self.get_board_stackup()
            return lid in stackup['copper']
            
        # 1. Process Tracks
        tracks = self._get_board_items('tracks')
        
        # Preserve manufactured copper dimensions.
        safety_buffer = 0.0

        for track in tracks:
            net = self._get_val(track, 'net')
            t_net_name = self._get_val(net, 'name', "")
            
            if net_name is not None and t_net_name != net_name:
                continue
                    
            start = self._get_val(track, 'start')
            end = self._get_val(track, 'end')
            width_mm = to_mm(self._get_val(track, 'width', 0))
            layer = self._get_val(track, 'layer', -1)
            
            if width_mm <= 0: width_mm = 0.2
            
            p0 = (to_mm(self._get_val(start, 'x', 0)), to_mm(self._get_val(start, 'y', 0)))
            p1 = (to_mm(self._get_val(end, 'x', 0)), to_mm(self._get_val(end, 'y', 0)))
            
            # Check for Arc
            # Attributes: center, radius, start_angle, end_angle (or angle)
            is_arc = False
            mid = self._get_val(track, 'mid')
            if mid:
                 is_arc = True
            
            if is_arc:
                # Discretize arc with higher resolution
                pm = (to_mm(self._get_val(mid, 'x', 0)), to_mm(self._get_val(mid, 'y', 0)))
                
                center = self._get_val(track, 'center')
                radius = self._get_val(track, 'radius')
                
                # Check directly for angles
                start_angle = self._get_val(track, 'start_angle')
                end_angle = self._get_val(track, 'end_angle')
                
                points = []
                
                if center and radius and start_angle is not None and end_angle is not None:
                    cx = to_mm(self._get_val(center, 'x', 0))
                    cy = to_mm(self._get_val(center, 'y', 0))
                    r_mm = to_mm(radius)
                    
                    # Convert 0.1 degrees (KiCad default) to radians? 
                    # Need to verify unit. Usually integers in 1/10 degree or similar, but
                    # kipy might normalize. 
                    
                    # Safe approach: Calculate angles ourselves from coordinates to be sure
                    # atan2 returns radians -pi to pi
                    a_start = math.atan2(p0[1] - cy, p0[0] - cx)
                    a_mid = math.atan2(pm[1] - cy, pm[0] - cx)
                    a_end = math.atan2(p1[1] - cy, p1[0] - cx)
                    
                    # Handle wrapping. 
                    # Cross product to determine direction? 
                    # Simple way: check if mid is between start/end in one direction
                    
                    # Normalize angles to 0-2pi
                    if a_start < 0: a_start += 2*math.pi
                    if a_mid < 0: a_mid += 2*math.pi
                    if a_end < 0: a_end += 2*math.pi
                    
                    # Determine sort order
                    # Case 1: Start < Mid < End
                    # Case 2: Wrap around 0/2pi
                    
                    # Just discretize from Start to End passing through Mid
                    # Total swept angle?
                    
                    # Vector arithmetic:
                    v_start = (p0[0]-cx, p0[1]-cy)
                    v_mid = (pm[0]-cx, pm[1]-cy)
                    v_end = (p1[0]-cx, p1[1]-cy)
                    
                    # We can pick N points. 
                    # Just use 8 segments (9 points) for smoothness
                    points = [p0]
                    
                    # We need to know which WAY to go (CW or CCW). Mid tells us.
                    # We can just construct two arcs (Start->Mid) and (Mid->End)
                    # 4 segments for Start->Mid, 4 for Mid->End = 8 total
                    
                    for (ps, pe) in [(p0, pm), (pm, p1)]:
                        a1 = math.atan2(ps[1] - cy, ps[0] - cx)
                        a2 = math.atan2(pe[1] - cy, pe[0] - cx)
                        
                        # Find shortest diff? kipy arcs usually shortest path?
                        # Actually arc direction is defined.
                        # Simple interpolation:
                        # But wait, atan2 discontinuity.
                        
                        diff = a2 - a1
                        if diff > math.pi: diff -= 2*math.pi
                        if diff < -math.pi: diff += 2*math.pi
                        
                        steps = 4
                        for i in range(1, steps + 1):
                            ang = a1 + diff * (i / steps)
                            px = cx + r_mm * math.cos(ang)
                            py = cy + r_mm * math.sin(ang)
                            points.append((px, py))
                            
                else:
                    # Fallback to 3 points
                    points = [p0, pm, p1]
                    
                line = LineString(points)
                # Cap style 1 (Round) is good for arcs
                poly = line.buffer(width_mm / 2 + safety_buffer, cap_style=1)
            else:
                if p0 == p1:
                    # Circular pad (VIA-like track?)
                    poly = Point(p0).buffer(width_mm / 2 + safety_buffer)
                else:
                    line = LineString([p0, p1])
                    poly = line.buffer(width_mm / 2 + safety_buffer, cap_style=1)
            
            if is_copper(layer):
                add_shape(layer, poly)

        # 1b. Process Vias (Essental for vertical connectivity mesh nodes)
        vias = self._get_board_items('vias')
        for via in vias:
            net = self._get_val(via, 'net')
            v_net_name = self._get_val(net, 'name', "")
            
            if net_name is not None and v_net_name != net_name:
                continue
                
            pos = self._get_val(via, 'position')
            x_mm = to_mm(self._get_val(pos, 'x', 0))
            y_mm = to_mm(self._get_val(pos, 'y', 0))
            
            width_mm = to_mm(self._get_val(via, 'diameter', self._get_val(via, 'width', 0)))
            drill_mm = to_mm(self._get_val(via, 'drill_diameter', 0))
            if width_mm <= drill_mm or drill_mm <= 0:
                raise ValueError("Via diameter/drill unavailable or invalid.")
            hole = Point(x_mm, y_mm).buffer(drill_mm / 2)
            poly = Point(x_mm, y_mm).buffer(width_mm / 2).difference(hole)
            
            # Vias exist on all layers in their pair (usually through-all)
            layers = self._get_val(via, 'layers')
            if not layers:
                ps = self._get_val(via, 'padstack')
                if ps:
                    layers = self._get_val(ps, 'layers')
            
            if not layers:
                lp = self._get_val(via, 'layer_pair')
                if lp:
                    s_lid, e_lid = min(lp), max(lp)
                    stackup = self.get_board_stackup()
                    layers = [l for l in stackup['copper'].keys() if s_lid <= l <= e_lid]
                else:
                    # Default to all copper layers if no info
                    stackup = self.get_board_stackup()
                    layers = list(stackup['copper'].keys())
            
            for lid in layers:
                if is_copper(lid):
                    drill_voids.setdefault(lid, []).append(hole)
                    add_shape(lid, poly)
                    
        # 2. Authoritative board-coordinate pad copper from the IPC API.
        for fp in self._get_board_items('footprints'):
            pads = self._get_val(fp, 'pads')
            if pads is None:
                pads = self._get_val(self._get_val(fp, 'definition'), 'pads', [])
            for pad in pads:
                if net_name is not None and self._get_val(self._get_val(pad, 'net'), 'name', '') != net_name:
                    continue
                if self._get_val(pad, 'pad_type') == 1:
                    for lid in self.get_board_stackup()['copper']:
                        drill_voids.setdefault(lid, []).append(drill_geometry(pad))
                for lid, shape in pad_geometry(self.board, pad, self.get_board_stackup()['copper']).items():
                    add_shape(lid, shape)

        # 3. Process Zones
        zones = self._get_board_items('zones')
        for zone in zones:
            net = self._get_val(zone, 'net')
            z_net_name = self._get_val(net, 'name', "")
            
            if net_name is not None and z_net_name != net_name:
                continue
                
            # Filled Polygons (dict mapping layer_id -> list of polygon objects)
            filled_polygons = self._get_val(zone, 'filled_polygons', {})
            
            # filled_polygons is a dict: {layer_id: [polygon_object, ...]}
            if isinstance(filled_polygons, dict):
                for layer_id, poly_list in filled_polygons.items():
                    if not is_copper(layer_id):
                        continue
                    
                    if not isinstance(poly_list, (list, tuple)):
                        poly_list = [poly_list]
                        
                    for poly_obj in poly_list:
                        try:
                            p = polygon_from_api(poly_obj)
                        except ValueError as exc:
                            raise ValueError(f"Filled zone on net {net_name}, layer {layer_id}: {exc}") from exc
                        if not p.is_empty:
                            add_shape(layer_id, p)

        # 4. Merge
        merged_geometry = {}
        for layer, shapes in layer_shapes.items():
            if not shapes:
                continue
            merged = unary_union(shapes)
            # Filled zones may include a drilled location; the physical hole remains empty.
            if drill_voids.get(layer):
                merged = merged.difference(unary_union(drill_voids[layer]))
            merged_geometry[layer] = merged
            
        return merged_geometry

    def get_board_copper_geometry(self):
        """All-net copper for visual overlays, without an electrical solve."""
        return self.get_net_geometry(None)

    def plot_geometry(self, geometry_by_layer):
        """Visualize the extracted geometry as a 2D plot."""
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(10, 8))
            layer_colors = {0: 'red', 2: 'blue', 31: 'green'}
            
            for layer_id, geom in geometry_by_layer.items():
                if geom.is_empty: continue
                color = layer_colors.get(layer_id, 'gray')
                
                if geom.geom_type == 'Polygon': polys = [geom]
                elif geom.geom_type == 'MultiPolygon': polys = list(geom.geoms)
                else: continue
                
                for poly in polys:
                    x, y = poly.exterior.xy
                    ax.fill(x, y, alpha=0.5, fc=color, ec='black', linewidth=0.5, label=f'Layer {layer_id}')
                    for interior in poly.interiors:
                        x, y = interior.xy
                        ax.fill(x, y, alpha=1.0, fc='white', ec='black', linewidth=0.5)
            
            ax.set_aspect('equal')
            ax.invert_yaxis()
            plt.tight_layout()
            import os
            output_path = os.path.join(os.path.dirname(__file__), 'debug_geometry.png')
            plt.savefig(output_path, format='png')
            if self.debug:
                 self.logger.debug(f"Saved debug plot to {output_path}")
            plt.close(fig)
            return output_path
        except Exception as e:
            if self.debug: self.logger.error(f"Plotting failed: {e}")
            return None
