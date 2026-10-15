import math
from resource_budget import default_budget_gb, grid_estimate, check_budget
import wx
import wx.dataview
import sys
import os
import time
from simulation_cache import BoardSnapshot, MeshCache, mesh_bytes, rail_signature, signature
from simulation_timing import TimingHistory, format_duration

# Ensure plugin dir is in path to import modules
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from extractor import GeometryExtractor
from mesh import Mesher
from solver import Solver
from copper_geometry import pad_geometry, terminal_nodes
from ui.power_tree_panel import PowerTreePanel
from plotter import Plotter

class KiPIDA_MainDialog(wx.Dialog):
    def __init__(self, parent, board_adapter, project=None):
        super(KiPIDA_MainDialog, self).__init__(parent, title="Ki-PIDA: Power Integrity Analyzer", 
                                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        self.SetSize((1000, 700))
        self.SetMinSize((800, 500))
        
        self.board = board_adapter
        self.project = project
        self._mesh_cache = MeshCache()
        self._prepared_cache = None
        self._geometry_cache = {}
        self._thermal_grids = {}
        self._board_token = None
        self._last_electrical = None
        self._last_matrix_key = None
        self._last_thermal = None
        self._timing_history = TimingHistory()
        from pcb_overlay import OverlayCache
        self.overlay_cache = OverlayCache()
        
        self._init_ui()
        self.Center()
        
        # Redirect stdout/stderr to our log window
        class LogRedirector:
            def __init__(self, log_func):
                self.log_func = log_func
            def write(self, msg):
                if msg.strip():
                     self.log_func(msg.strip())
            def flush(self): pass
            
        sys.stdout = LogRedirector(self.log)
        sys.stderr = LogRedirector(self.log)
        
        self.log("Ki-PIDA UI Initialized.")
        if not self.board:
             self.log("ERROR: No board object connected. Plugin will not function properly.")
        else:
             self.log(f"Board object connected: {type(self.board)}")
        
        if self.project:
            self.log(f"Project: {self.project.name} at {self.project.path}")
        else:
            self.log("WARNING: No project object available.")

        
    def _init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 1. Notebook for tabs
        self.notebook = wx.Notebook(self)
        
        # Tab 1: Configuration (New Power Tree Panel)
        self.tab_config = wx.Panel(self.notebook)
        self.power_tree = PowerTreePanel(self.tab_config, self.board, project=self.project, log_callback=self.log)
        
        # Config Tab Layout
        config_sizer = wx.BoxSizer(wx.VERTICAL)
        config_sizer.Add(self.power_tree, 1, wx.EXPAND | wx.ALL, 5)
        
        # Global Settings (Grid Size, Drop %, Debug)
        sett_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        lbl_grid = wx.StaticText(self.tab_config, label="Mesh Resolution (mm):")
        self.txt_grid_size = wx.TextCtrl(self.tab_config, value="0.1", size=(60, -1))
        sett_sizer.Add(lbl_grid, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        sett_sizer.Add(self.txt_grid_size, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        lbl_drop = wx.StaticText(self.tab_config, label="Max Drop %:")
        self.txt_drop_pct = wx.TextCtrl(self.tab_config, value="5", size=(60, -1))
        sett_sizer.Add(lbl_drop, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        sett_sizer.Add(self.txt_drop_pct, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        self.chk_convergence = wx.CheckBox(self.tab_config, label="Check mesh convergence")
        self.chk_convergence.SetValue(True)
        sett_sizer.Add(self.chk_convergence, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.chk_debug = wx.CheckBox(self.tab_config, label="Enable Debug Log")
        sett_sizer.Add(self.chk_debug, 0, wx.ALIGN_CENTER_VERTICAL)
        
        config_sizer.Add(sett_sizer, 0, wx.EXPAND | wx.ALL, 5)
        compute_sizer = wx.WrapSizer(wx.HORIZONTAL)
        compute_sizer.Add(wx.StaticText(self.tab_config, label="Solver:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.choice_backend = wx.Choice(self.tab_config, choices=['Auto', 'CPU', 'GPU (CUDA)'])
        self.choice_backend.SetSelection(0)
        compute_sizer.Add(self.choice_backend, 0, wx.RIGHT, 20)
        compute_sizer.Add(wx.StaticText(self.tab_config, label="RAM budget (GB):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.choice_memory = wx.Choice(self.tab_config, choices=['Auto / Dynamic', 'Manual'])
        self.choice_memory.SetSelection(0)
        compute_sizer.Add(self.choice_memory, 0, wx.RIGHT, 5)
        self.txt_memory_gb = wx.TextCtrl(self.tab_config, value=f'{default_budget_gb():.1f}', size=(60, -1))
        self.txt_memory_gb.Disable()
        self.choice_memory.Bind(wx.EVT_CHOICE, lambda e: self.txt_memory_gb.Enable(self.choice_memory.GetSelection() == 1))
        compute_sizer.Add(self.txt_memory_gb, 0, wx.RIGHT, 10)
        self.memory_label = wx.StaticText(self.tab_config, label='Budget calculated at run time')
        compute_sizer.Add(self.memory_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.btn_calibrate = wx.Button(self.tab_config, label='Calibrate timing')
        self.btn_calibrate.Bind(wx.EVT_BUTTON, self.on_calibrate)
        compute_sizer.Add(self.btn_calibrate, 0)
        config_sizer.Add(compute_sizer, 0, wx.EXPAND | wx.ALL, 5)
        thermal_sizer = wx.WrapSizer(wx.HORIZONTAL)
        self.chk_thermal = wx.CheckBox(self.tab_config, label='Thermal estimate (copper + series + regulator heat)')
        self.chk_thermal.SetValue(True)
        thermal_sizer.Add(self.chk_thermal, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        for attr, label, default in [('txt_ambient', 'Ambient °C', '25'),
                                     ('txt_cooling', 'h per face W/m²K', '10'),
                                     ('txt_thermal_step', 'Grid mm', '1'),
                                     ('txt_substrate_k', 'Substrate k W/mK', '0.3')]:
            thermal_sizer.Add(wx.StaticText(self.tab_config, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            control = wx.TextCtrl(self.tab_config, value=default, size=(50, -1))
            setattr(self, attr, control)
            thermal_sizer.Add(control, 0, wx.RIGHT, 8)
        config_sizer.Add(thermal_sizer, 0, wx.EXPAND | wx.ALL, 5)
        self.tab_config.SetSizer(config_sizer)
        
        self.notebook.AddPage(self.tab_config, "Power Tree & Config")
        
        # Tab 2: Results
        self.tab_results = wx.Panel(self.notebook)
        self._init_results_tab(self.tab_results)
        self.notebook.AddPage(self.tab_results, "Results")
        
        # Tab 3: Log/Debug
        self.tab_log = wx.Panel(self.notebook)
        self._init_log_tab(self.tab_log)
        self.notebook.AddPage(self.tab_log, "Log")
        
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)
        
        # 2. Action Buttons (Bottom)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_run = wx.Button(self, label="Run Simulation")
        self.btn_thermal = wx.Button(self, label='Update Thermal Only')
        self.btn_thermal.Disable()
        self.btn_thermal.Bind(wx.EVT_BUTTON, self.on_thermal_only)
        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, "Close")
        
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_thermal, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        self.SetSizer(main_sizer)
        
        # Bind events
        self.btn_run.Bind(wx.EVT_BUTTON, self.on_run)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self.on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
        
        # Auto-scan board after UI is ready
        wx.CallAfter(self.power_tree.auto_scan)
    
    def _init_results_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Splitter: Top=Text Stats, Bottom=Notebook for plots
        self.result_splitter = wx.SplitterWindow(parent)
        
        self.pnl_text = wx.Panel(self.result_splitter)
        text_sizer = wx.BoxSizer(wx.VERTICAL)
        self.result_text = wx.TextCtrl(self.pnl_text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        text_sizer.Add(self.result_text, 1, wx.EXPAND | wx.ALL, 5)
        self.pnl_text.SetSizer(text_sizer)
        
        self.pnl_plots = wx.Panel(self.result_splitter)
        plot_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Notebook for results (3D, Layer1, Layer2...)
        self.results_notebook = wx.Notebook(self.pnl_plots)
        plot_sizer.Add(self.results_notebook, 1, wx.EXPAND | wx.ALL, 0)
        
        self.pnl_plots.SetSizer(plot_sizer)
        self._add_temperature_tab(None)
        
        self.result_splitter.SplitHorizontally(self.pnl_text, self.pnl_plots, 100)
        self.result_splitter.SetMinimumPaneSize(50)
        
        sizer.Add(self.result_splitter, 1, wx.EXPAND | wx.ALL, 5)
        parent.SetSizer(sizer)

    def _add_temperature_tab(self, thermal):
        """Keep temperature discoverable even without a successful thermal solve."""
        import logging
        if thermal:
            try:
                from ui.temperature_panel import TemperaturePanel
                from pcb_overlay import make_overlay_loader
                snapshot = getattr(self, '_simulation_board', None)
                loader = make_overlay_loader(snapshot if snapshot is not None else self.board)
                page = TemperaturePanel(self.results_notebook, thermal, loader, self.overlay_cache)
                self.results_notebook.AddPage(page, 'Temperature (board average)')
                return
            except Exception as exc:
                logging.getLogger('KiPIDA').exception('Interactive temperature view failed')
                message = f'Interactive temperature view could not be created: {exc}'
                self.log(message)
        else:
            error = getattr(self, 'thermal_error', None)
            if error:
                message = f'Temperature results unavailable:\n{error}'
            elif not self.chk_thermal.GetValue():
                message = 'Thermal analysis is disabled. Enable Thermal estimate in Power Tree & Config, then run the simulation.'
            else:
                message = 'No temperature results yet. Run Simulation with Thermal estimate enabled. If electrical validation fails, resolve that error first.'
        page = wx.ScrolledWindow(self.results_notebook, style=wx.HSCROLL | wx.VSCROLL)
        page.SetScrollRate(10, 10)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(page, label=message)
        label.Wrap(650)
        sizer.Add(label, 0, wx.ALL, 10)
        if thermal:
            peak = float(thermal['temperature_c'].max())
            sizer.Add(wx.StaticText(page, label=f'Highest simulated temperature: {peak:.2f} °C'), 0, wx.ALL, 10)
            bitmap = Plotter().plot_temperature(thermal)
            if bitmap:
                sizer.Add(wx.StaticBitmap(page, bitmap=bitmap), 0, wx.ALL, 5)
        else:
            button = wx.Button(page, label='Configure thermal analysis')
            def configure(event):
                self.chk_thermal.SetValue(True)
                self.notebook.SetSelection(0)
                self.chk_thermal.SetFocus()
            button.Bind(wx.EVT_BUTTON, configure)
            sizer.Add(button, 0, wx.ALL, 10)
        page.SetSizer(sizer)
        self.results_notebook.AddPage(page, 'Temperature (board average)')

    def _add_plot_tab(self, title, bitmap):
        """Helper to add a plot tab securely."""
        if not bitmap: return
        
        # Create a ScrolledWindow for the plot
        page = wx.ScrolledWindow(self.results_notebook, style=wx.HSCROLL | wx.VSCROLL)
        page.SetScrollRate(10, 10)
        
        img_ctrl = wx.StaticBitmap(page)
        img_ctrl.SetBitmap(bitmap)
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(img_ctrl, 1, wx.CENTER | wx.ALL, 5)
        page.SetSizer(sizer)
        
        self.results_notebook.AddPage(page, title)


    def _init_log_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.log_ctrl = wx.TextCtrl(parent, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        self.progress_stage = wx.StaticText(parent, label='Ready')
        sizer.Add(self.progress_stage, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        self.eta_label = wx.StaticText(parent, label='Time estimate: available after a run or calibration')
        sizer.Add(self.eta_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        self._eta_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._update_eta, self._eta_timer)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.progress_bar = wx.Gauge(parent, range=100)
        self.progress_percent = wx.StaticText(parent, label='0%', size=(45, -1))
        row.Add(self.progress_bar, 1, wx.EXPAND | wx.RIGHT, 8)
        row.Add(self.progress_percent, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 5)
        self._progress_value = 0
        parent.SetSizer(sizer)

    def _set_progress(self, value=None, stage=None, reset=False):
        if getattr(self, '_cancel_requested', False):
            raise InterruptedError('Simulation cancelled.')
        before = getattr(self, '_progress_value', 0)
        if value is not None:
            value = max(0, min(100, int(value)))
            self._progress_value = value if reset else max(getattr(self, '_progress_value', 0), value)
        if not reset and before == getattr(self, '_progress_value', 0) and (not stage or self.progress_stage.GetLabel() == stage):
            return
        if value is not None:
            self.progress_bar.SetValue(self._progress_value)
            self.progress_percent.SetLabel(f'{self._progress_value}%')
        if stage:
            self.progress_stage.SetLabel(stage)
        self._update_eta()
        wx.SafeYield()

    def _update_eta(self, event=None):
        if not getattr(self, '_run_started', None) or not getattr(self, '_running', False):
            return
        elapsed = time.perf_counter()-self._run_started
        estimate = getattr(self, '_eta_estimate', None)
        if estimate is None:
            message = 'ETA unavailable; recording this run for future estimates'
        else:
            low, high, count = estimate
            fraction = getattr(self, '_progress_value', 0)/100
            # Stage weights are not durations, so keep the displayed range broad.
            if .1 < fraction < .95:
                observed = elapsed/fraction
                low = min(low, observed*.7)
                high = max(high, observed*1.5)
            message = (f'Approx. remaining: {format_duration(max(0, low-elapsed))}–'
                       f'{format_duration(max(1, high-elapsed))} ({count} timing samples)')
            if elapsed > high:
                message = 'Taking longer than estimated; solver convergence may vary'
        self.eta_label.SetLabel(f'Elapsed: {format_duration(elapsed)} | {message}')

    def _memory_budget(self):
        from resource_budget import resolve_budget
        automatic = self.choice_memory.GetSelection() == 0
        amount = resolve_budget('auto' if automatic else self.txt_memory_gb.GetValue())
        label = 'New RAM allowance' if automatic else 'Run RAM limit'
        self.memory_label.SetLabel(f'{label}: {amount:.2f} GB')
        return amount

    def _clear_caches(self):
        self._mesh_cache.clear()
        if self._prepared_cache is not None:
            self._prepared_cache.clear()
        for grid in self._thermal_grids.values():
            grid.close()
        self._thermal_grids.clear()
        self._geometry_cache.clear()
        self._last_electrical = self._last_thermal = None
        self._last_matrix_key = None
        self.overlay_cache.invalidate()

    def _other_retained_bytes(self):
        return self._retained_memory_bytes()

    def _retained_memory_bytes(self, extra=()):
        """Count each owned host allocation once across all cache/result roots."""
        from network_cache import retained_host_bytes
        roots = [self._mesh_cache.entries, self._thermal_grids, self._last_electrical,
                 self._last_thermal, self.system_results, extra]
        if self._prepared_cache is not None:
            roots.append(self._prepared_cache.entries)
        return retained_host_bytes(roots)

    def _release_optional_ram(self, keep_meshes=(), keep_prepared=(), keep_grids=()):
        """Keep this pass's usable data; discard unrelated caches before retrying."""
        import gc
        meshes = {id(mesh) for mesh in keep_meshes}
        prepared = {id(item) for item in keep_prepared}
        grids = {id(item) for item in keep_grids}
        for key, (mesh, size) in list(self._mesh_cache.entries.items()):
            if id(mesh) not in meshes:
                self._mesh_cache.entries.pop(key)
                self._mesh_cache.bytes_used -= size
        if self._prepared_cache is not None:
            for key, (item, _) in list(self._prepared_cache.entries.items()):
                if id(item) not in prepared:
                    self._prepared_cache.entries.pop(key)
                    item.close()
        for key, grid in list(self._thermal_grids.items()):
            if id(grid) not in grids:
                self._thermal_grids.pop(key).close()
        self._last_electrical = self._last_thermal = None
        self.overlay_cache.invalidate()
        gc.collect()

    def _ram_error(self, error, phase, step, geometries, refined=False):
        from resource_budget import feasible_spacing
        suggestion = feasible_spacing(geometries, step, error.allowed_additional_bytes)
        text = f'{phase} at {step:g} mm: {error}'
        if suggestion is not None:
            if 'Thermal' in phase:
                text += f' Try thermal Grid mm >= {suggestion*(2 if refined else 1):g}.'
            else:
                text += f' Try Mesh Resolution >= {suggestion*(2 if refined else 1):g} mm.'
            if refined:
                text += ' Convergence checking also runs at half that spacing (approximately four times as many cells).'
            text += ' This is a memory-fit estimate; accuracy still requires the convergence check.'
        return ValueError(text)

    def on_thermal_only(self, event):
        self._thermal_only = True
        self.chk_thermal.SetValue(True)
        try:
            self.on_run(event)
        finally:
            self._thermal_only = False

    def on_calibrate(self, event):
        from simulation_timing import calibration
        backend = ('auto', 'cpu', 'gpu')[self.choice_backend.GetSelection()]
        self.btn_calibrate.Disable()
        try:
            self.log('Running a small separate timing calibration; results are approximate.')
            with wx.BusyCursor():
                features, timings = calibration(backend)
            self._timing_history.record(features, timings, calibration=True)
            self.eta_label.SetLabel('Timing calibration saved; larger or complex boards may differ substantially')
            self.log(f'Timing calibration: meshing {timings["mesh"]:.3f}s, electrical {timings["electrical"]:.3f}s, thermal {timings["thermal"]:.3f}s.')
        except Exception as exc:
            wx.MessageBox(str(exc), 'Timing calibration failed', wx.OK | wx.ICON_ERROR)
        finally:
            self.btn_calibrate.Enable()

    def log(self, msg):
        if getattr(self, '_cancel_requested', False):
            raise InterruptedError('Simulation cancelled.')
        if not hasattr(self, 'log_ctrl'): return
        self.log_ctrl.AppendText(msg + "\n")
        self.log_ctrl.ShowPosition(self.log_ctrl.GetLastPosition())
        if getattr(self, '_running', False) and 'Sparse solve:' in msg:
            self.progress_stage.SetLabel(msg)
        wx.SafeYield()
        
    def to_mm(self, val_nm):
        return val_nm / 1e6

    def _get_mesh_nodes(self, mesh, ref_des, pad_names, debug_mode):
        if debug_mode:
            self.log(f"  [_get_mesh_nodes] Looking up {ref_des} pads={pad_names}")
        nodes = []
        
        # Helper to get attribute value (property or getter)
        def _get_val(obj, attr_name, default=None):
            if obj is None: return default
            # Try property
            if hasattr(obj, attr_name):
                val = getattr(obj, attr_name)
                if val is not None: return val
            # Try getter
            for prefix in ["get_", ""]:
                method_name = prefix + attr_name
                if hasattr(obj, method_name):
                    try:
                        val = getattr(obj, method_name)()
                        if val is not None: return val
                    except: pass
            return default
        
        # Helper to get board items (same pattern as discovery.py)
        def get_board_items(attr_name):
            board = getattr(self, '_simulation_board', self.board)
            if hasattr(board, attr_name):
                return getattr(board, attr_name)
            method_name = f"get_{attr_name}"
            if hasattr(board, method_name):
                try: return getattr(board, method_name)()
                except: pass
            return []
        
        # Find the footprint
        found_fp = None
        footprints = get_board_items('footprints')
        
        for fp in footprints:
            # Get reference (same logic as discovery.py)
            ref = _get_val(fp, 'reference', _get_val(fp, 'ref_des', ''))
            if not ref:
                # Try reference_field for Kipy
                ref_field = _get_val(fp, 'reference_field')
                if ref_field:
                    text = _get_val(ref_field, 'text')
                    if text:
                        ref = _get_val(text, 'value', '')
            
            if ref == ref_des:
                found_fp = fp
                break
        
        if not found_fp:
            if debug_mode: self.log(f"  Warning: Footprint {ref_des} not found.")
            return []
        
        # Get pads (same logic as discovery.py)
        pads = _get_val(found_fp, 'pads')
        if pads is None:
            defn = _get_val(found_fp, 'definition')
            pads = _get_val(defn, 'pads', [])
        
        target_pads = []
        if not pad_names:
            target_pads = pads  # All pads
        else:
            for p in pads:
                # Get pad number/name
                pad_num = _get_val(p, 'number', _get_val(p, 'name', ''))
                if pad_num in pad_names:
                    target_pads.append(p)
        
        if not target_pads:
            if debug_mode: self.log(f"  No pads found for {ref_des} matching {pad_names}")
            return []
        missing = set(pad_names) - {_get_val(p, 'number', _get_val(p, 'name', '')) for p in target_pads}
        if missing:
            raise ValueError(f"Terminal {ref_des} has missing pads: {', '.join(sorted(missing))}.")
        
        for pad in target_pads:
            if _get_val(_get_val(pad, 'net'), 'name', '') != mesh.net_name:
                raise ValueError(f"Terminal {ref_des} is not on rail {mesh.net_name}.")
            shapes = pad_geometry(getattr(self, '_simulation_board', self.board), pad, mesh.copper_layers)
            mapped = terminal_nodes(mesh, shapes)
            if not mapped:
                raise ValueError(f"Pad {ref_des}.{_get_val(pad, 'number', '?')} has no mesh nodes. Reduce mesh spacing.")
            nodes.extend(mapped)
        return sorted(set(nodes))

    def _build_rail_dependency_graph(self, system_rails):
        """
        Build dependency graph from regulator connections.
        Returns: dict mapping rail_name -> list of rails it depends on
        """
        graph = {rail.net_name: [] for rail in system_rails}
        
        for rail in system_rails:
            for reg in rail.child_regulators:
                # reg.output_rail_name depends on rail.net_name (input)
                if reg.output_rail_name in graph:
                    graph[reg.output_rail_name].append(rail.net_name)
        
        return graph
    
    def _topological_sort_rails(self, system_rails):
        """
        Sort rails in dependency order (leaves first, roots last).
        Raises ValueError if cycle detected.
        Returns: list of PowerRail objects in solve order
        """
        graph = self._build_rail_dependency_graph(system_rails)
        
        # DFS-based topological sort with cycle detection
        visited = set()
        rec_stack = set()  # Recursion stack for cycle detection
        result = []
        
        def dfs(rail_name):
            if rail_name in rec_stack:
                raise ValueError(f"Cycle detected in power rail dependencies involving '{rail_name}'")
            
            if rail_name in visited:
                return
            
            visited.add(rail_name)
            rec_stack.add(rail_name)
            
            # Visit dependencies first
            for dep in graph.get(rail_name, []):
                dfs(dep)
            
            rec_stack.remove(rail_name)
            result.append(rail_name)
        
        # Process all rails
        for rail in system_rails:
            if rail.net_name not in visited:
                dfs(rail.net_name)
        
        # Reverse to get leaves-first order (output rails before input rails)
        result.reverse()
        
        # Convert rail names back to PowerRail objects
        rail_map = {r.net_name: r for r in system_rails}
        return [rail_map[name] for name in result]

    def on_run(self, event):
        self._running = True
        self._cancel_requested = False
        self._run_started = time.perf_counter()
        self._eta_estimate = None
        self._eta_timer.Start(1000)
        timings = {name: 0.0 for name in ('geometry', 'mesh', 'electrical', 'thermal', 'render')}
        self._thermal_pass_timings = []
        features = None
        from network_solver import solve_system, validate_power_feeds
        from network_cache import PreparedNetworkCache
        from resource_budget import default_budget_gb, grid_estimate, check_budget
        for name in ('btn_run', 'btn_thermal', 'btn_calibrate', 'power_tree'):
            control = getattr(self, name, None)
            if control and hasattr(control, 'Disable'):
                control.Disable()
        progress = getattr(self, '_set_progress', lambda *args, **kwargs: None)
        progress(0, 'Preparing simulation', reset=True)
        self.system_results = {}
        self.component_results = []
        self.thermal_result = None
        self.thermal_error = None
        self.notebook.SetSelection(2)
        try:
            rails = self.power_tree.rails
            active = {r.net_name for r in rails if r.sources or r.loads or r.child_regulators or r.series_components}
            for rail in rails:
                for link in rail.series_components:
                    active.update((link.net_a, link.net_b))
                for reg in rail.child_regulators:
                    active.update((reg.input_rail_name, reg.output_rail_name))
            rails = [r for r in rails if r.net_name in active]
            if not rails:
                raise ValueError("Assign a source and a load first.")
            if active != {r.net_name for r in rails}:
                raise ValueError("A component refers to a missing rail. Add the net or edit the component.")
            validate_power_feeds(rails)
            debug = self.chk_debug.GetValue()
            started = time.perf_counter()
            progress(1, 'Reading board revision')
            snapshot = BoardSnapshot(self.board)
            token = snapshot.cache_token
            if token is None or token != self._board_token:
                self._clear_caches()
            self._board_token = token
            self._simulation_board = snapshot
            extractor = GeometryExtractor(snapshot, debug=debug, log_callback=self.log)
            spacing = float(self.txt_grid_size.GetValue())
            if not math.isfinite(spacing) or spacing < 0.01:
                raise ValueError("Mesh spacing must be finite and at least 0.01 mm.")
            geometry_key = tuple(r.net_name for r in rails)
            geometry_hit = token is not None and geometry_key in self._geometry_cache
            if geometry_hit:
                stackup, geometry = self._geometry_cache[geometry_key]
                self.log('Reusing extracted copper and stackup from the unchanged board.')
            else:
                stackup = extractor.get_board_stackup()
                geometry = {}
                for index, rail in enumerate(rails):
                    progress(2+8*index/len(rails), f'Reading copper: {rail.net_name}')
                    geometry[rail.net_name] = extractor.get_net_geometry(rail.net_name)
                if token is not None:
                    # Retain one selected-net set; avoid unbounded polygon copies.
                    self._geometry_cache = {geometry_key: (stackup, geometry)}
            timings['geometry'] += time.perf_counter()-started
            budget = self._memory_budget()
            self._mesh_cache.set_budget(budget*1024**3*.25)
            if self._prepared_cache is None:
                self._prepared_cache = PreparedNetworkCache(max_bytes=int(budget*1024**3*.25))
            self._prepared_cache.set_budget(int(budget*1024**3*.25))
            backend_control = getattr(self, 'choice_backend', None)
            backend = ('auto', 'cpu', 'gpu')[backend_control.GetSelection()] if backend_control else 'auto'
            thermal_control = getattr(self, 'chk_thermal', None)
            electrical_end = 70 if thermal_control and thermal_control.GetValue() else 95
            refinement = self.chk_convergence.GetValue()
            electrical_key = (token, rail_signature(rails), spacing, backend, refinement)
            from dataclasses import asdict
            matrix_configuration = [asdict(rail) for rail in rails]
            for rail in matrix_configuration:
                for load in rail['loads']:
                    load['total_current'] = 0
            matrix_key = token, signature(matrix_configuration), backend
            electrical_hit = token is not None and self._last_electrical is not None and self._last_electrical[0] == electrical_key
            if getattr(self, '_thermal_only', False) and not electrical_hit:
                raise ValueError('The board or electrical configuration changed. Run Simulation before updating thermal analysis.')
            mesh_points = sum(grid_estimate(g, spacing)[0] for g in geometry.values())*(5 if refinement else 1)
            thermal_step = float(self.txt_thermal_step.GetValue())
            thermal_area = sum(g.area for geo in geometry.values() for g in geo.values())
            if thermal_control.GetValue():
                try:
                    from thermal_solver import board_region
                    thermal_area = board_region(snapshot).area
                except ValueError:
                    pass  # Thermal failure is reported separately after electrical validation.
            thermal_cells = thermal_area/max(.01, thermal_step**2)*(5 if refinement else 1)
            steps = [spacing, spacing/2] if refinement else [spacing]
            mesh_hit = token is not None and all(self._mesh_cache.get((token, net, step)) is not None for net in geometry for step in steps)
            features = dict(backend=backend, nets=len(rails), mesh_points=mesh_points,
                            thermal_cells=thermal_cells, thermal=thermal_control.GetValue(),
                            geometry_cached=geometry_hit, mesh_cached=electrical_hit or mesh_hit, electrical_cached=electrical_hit)
            features['prepared_cached'] = bool(mesh_hit and self._prepared_cache.entries)
            features['spacing_mm'] = spacing
            features['refinement'] = refinement
            features['thermal_spacing_mm'] = thermal_step
            features['cooling_w_m2k'] = self.txt_cooling.GetValue()
            features['passes'] = []
            self._eta_estimate = self._timing_history.estimate(features)
            self._update_eta()
            coarse_end = 10+(electrical_end-10)*.2 if refinement else electrical_end
            def run(step, start, end, retained_bytes=0, initial_results=None):
                from resource_budget import MemoryBudgetExceeded
                from network_cache import retained_host_bytes
                budget = self._memory_budget()  # Auto rechecks currently available RAM.
                self._mesh_cache.set_budget(budget*1024**3*.25)
                self._prepared_cache.set_budget(int(budget*1024**3*.25))
                self.log(f'Electrical {"refined" if retained_bytes else "coarse"} pass: spacing {step:g} mm.')
                estimates = [grid_estimate(geo, step) for geo in geometry.values()]
                keys = [(token, net, step) for net in geometry]
                cached = [self._mesh_cache.get(key) if token is not None else None for key in keys]
                # The estimate is a peak, not an allocation repeated from scratch.
                # Deduct this pass's already resident meshes/prepared operators.
                cached_map = {net: mesh for net, mesh in zip(geometry, cached) if mesh is not None}
                matching = []
                if matrix_key == getattr(self, '_last_matrix_key', None):
                    matching = [item for item, _ in self._prepared_cache.entries.values()
                                if all(cached_map.get(net) is mesh for net, mesh in item.meshes.items())]
                resident_for_pass = retained_host_bytes([cached, matching])
                full_peak = sum(e[1] for e in estimates)
                # Even an exact-matrix warm solve allocates RHS/result/work vectors.
                vector_workspace = sum(len(mesh.nodes)*64 for mesh in cached if mesh is not None)
                additional = max(vector_workspace, full_peak-resident_for_pass, 0)
                retained_cache = self._retained_memory_bytes((initial_results, cached))
                try:
                    estimated = check_budget(additional, budget, retained_cache)
                except MemoryBudgetExceeded:
                    self.log('Releasing unused caches and refreshing the RAM allowance; requested spacing is unchanged.')
                    self._release_optional_ram(keep_meshes=cached_map.values(), keep_prepared=matching)
                    budget = self._memory_budget()
                    retained_cache = self._retained_memory_bytes((initial_results, cached))
                    try:
                        estimated = check_budget(additional, budget, retained_cache)
                    except MemoryBudgetExceeded as error:
                        if initial_results is not None and any('mesh' in data for data in initial_results.values()):
                            from mesh_convergence import release_coarse_arrays
                            self.log('Low-memory refinement: releasing coarse mesh/voltage arrays; convergence statistics are retained. Warm interpolation is skipped.')
                            release_coarse_arrays(initial_results)
                            import gc
                            gc.collect()
                            budget = self._memory_budget()
                            retained_cache = self._retained_memory_bytes((initial_results, cached))
                            try:
                                estimated = check_budget(additional, budget, retained_cache)
                            except MemoryBudgetExceeded as retry:
                                phase = 'Refined electrical pass'
                                raise self._ram_error(retry, phase, step, list(geometry.values()), True) from retry
                        else:
                            phase = 'Refined electrical pass' if initial_results is not None else 'Coarse electrical pass'
                            raise self._ram_error(error, phase, step, list(geometry.values()), initial_results is not None) from error
                self.log(f'Mesh preflight: {sum(e[0] for e in estimates):,} candidate grid points; '
                         f'additional RAM {additional/1024**3:.2f} GB, retained {retained_cache/1024**3:.2f} GB, solver {backend}.')
                mesher = Mesher(snapshot, debug=debug, log_callback=self.log, memory_budget_gb=budget)
                meshes = {}
                total = max(1, sum(e[0] for e in estimates))
                completed = 0
                for (net, geo), estimate, key, reused in zip(geometry.items(), estimates, keys, cached):
                    base, weight = completed/total, estimate[0]/total
                    mesher.progress_callback = lambda fraction, base=base, weight=weight, net=net: progress(
                        start+(end-start)*.75*(base+weight*fraction), f'Meshing {net} at {step:g} mm')
                    mesher.progress_callback(0)
                    started = time.perf_counter()
                    if reused is not None:
                        meshes[net] = reused
                        self.log(f'Reusing {net} mesh at {step:g} mm.')
                    else:
                        features['mesh_cached'] = False
                        try:
                            meshes[net] = mesher.generate_mesh(net, geo, stackup, grid_size_mm=step)
                        except MemoryBudgetExceeded as error:
                            # Other applications can consume RAM after preflight.
                            self._release_optional_ram(keep_meshes=list(meshes.values())+list(cached_map.values()))
                            mesher.memory_budget_gb = self._memory_budget()
                            try:
                                meshes[net] = mesher.generate_mesh(net, geo, stackup, grid_size_mm=step)
                            except MemoryBudgetExceeded as retry:
                                phase = 'Refined electrical meshing' if initial_results is not None else 'Coarse electrical meshing'
                                raise self._ram_error(retry, phase, step, [geo], initial_results is not None) from retry
                        if token is not None:
                            self._mesh_cache.put(key, meshes[net])
                    timings['mesh'] += time.perf_counter()-started
                    completed += estimate[0]
                    mesher.progress_callback(1)
                progress(start+(end-start)*.75, 'Solving electrical networks')
                started = time.perf_counter()
                outcome = solve_system(rails, meshes, lambda mesh, ref, pads: self._get_mesh_nodes(mesh, ref, pads, debug), self.log, backend=backend,
                                       progress=lambda fraction: progress(start+(end-start)*(.75+.25*fraction), 'Solving electrical networks'),
                                       cache=self._prepared_cache if token is not None else None,
                                       initial_results=initial_results if initial_results and all('mesh' in data for data in initial_results.values()) else None)
                timings['electrical'] += time.perf_counter()-started
                features['prepared_cached'] = all(data['diagnostics'].get('prepared_cache_hit', False) for data in outcome[0].values())
                # Retain individual coarse/refined diagnostics for ETA tuning and profiling.
                features['passes'].append(dict(spacing_mm=step,
                    nodes=sum(len(mesh.nodes) for mesh in meshes.values()),
                    electrical_seconds=time.perf_counter()-started,
                    solver=[{key: data['diagnostics'].get(key) for key in
                             ('backend', 'gpu', 'preconditioner', 'iterations', 'network_setup_seconds',
                              'setup_seconds', 'iteration_seconds', 'rhs_transfer_seconds', 'validation_seconds')}
                            for data in outcome[0].values()]))
                return outcome
            if electrical_hit:
                _, results, parts = self._last_electrical
                self.log('Reusing validated electrical results and convergence check for identical inputs.')
                status = next(iter(results.values()))['diagnostics']['mesh_check']
                progress(electrical_end, 'Electrical results reused')
            else:
                results, parts = run(spacing, 10, coarse_end)
                status = 'unchecked'
            if refinement and not electrical_hit:
                retained = sum(len(data['mesh'].nodes)*32 +
                               len(data['mesh'].G_coo_data)*16 +
                               (data['mesh'].grid_lookup.grid.nbytes if hasattr(data['mesh'], 'grid_lookup') else 0)
                               for data in results.values())
                fine, fine_parts = run(spacing/2, coarse_end, electrical_end, retained, results)
                from mesh_convergence import compare_meshes
                failures = compare_meshes(results, fine, parts, fine_parts, self.log)
                if failures:
                    raise ValueError('Mesh not converged:\n'+'\n'.join(failures)+f'\nSparse solves passed, but spatial refinement did not. Try Mesh Resolution {spacing/2:g} mm; the next comparison will use {spacing/4:g} mm. Check narrow copper, pad and annular-ring resolution. Thermal analysis was not run.')
                results, parts = fine, fine_parts
                status = 'passed (rail drop/loss and series results within 5%)'
            for data in results.values():
                data['diagnostics']['mesh_check'] = status
            self.system_results, self.component_results = results, parts
            self._electrical_key = electrical_key
            self._last_matrix_key = matrix_key
            if token is not None:
                self._last_electrical = electrical_key, results, parts
            if thermal_control and thermal_control.GetValue():
                try:
                    started = time.perf_counter()
                    self._run_thermal(stackup, geometry, backend, budget)
                    features['thermal_cached'] = bool(self.thermal_result and self.thermal_result['diagnostics'].get('ambient_shift_only'))
                    timings['thermal'] += time.perf_counter()-started
                except InterruptedError:
                    raise
                except Exception as thermal_exc:
                    self.thermal_result = None
                    self.thermal_error = str(thermal_exc)
                    self.log(f'Thermal estimate failed: {thermal_exc}')
                    wx.MessageBox(str(thermal_exc), 'Thermal estimate failed; electrical results retained', wx.OK | wx.ICON_ERROR)
            progress(95, 'Rendering simulation results')
            started = time.perf_counter()
            self._update_results_ui()
            timings['render'] = time.perf_counter()-started
            progress(100, 'Completed — thermal estimate unavailable' if self.thermal_error else 'Completed')
            if features is not None and not self.thermal_error:
                features['thermal_passes'] = self._thermal_pass_timings
                self._timing_history.record(features, timings)
            self.eta_label.SetLabel(f'Completed in {format_duration(time.perf_counter()-self._run_started)}; timings saved for future estimates')
        except Exception as exc:
            self._cancel_requested = False
            self.system_results, self.component_results = {}, []
            self.thermal_result = None
            self.thermal_error = f'Simulation failed before a temperature result was available: {exc}'
            self._update_results_ui()
            self.log(f"System Solve Error: {exc}")
            progress(None, 'Cancelled' if isinstance(exc, InterruptedError) else 'Failed — see log')
            if not isinstance(exc, InterruptedError):
                wx.MessageBox(str(exc), "Simulation failed validation", wx.OK | wx.ICON_ERROR)
        finally:
            self._running = False
            self._eta_timer.Stop()
            for name in ('btn_run', 'btn_calibrate', 'power_tree'):
                control = getattr(self, name, None)
                if control and hasattr(control, 'Enable'):
                    control.Enable()
            self.btn_thermal.Enable(self._last_electrical is not None)

    def _run_thermal(self, stackup, geometry, backend, budget):
        from thermal_solver import ThermalSettings, ThermalGrid, board_region, add_copper_heat, interpolate_thermal
        from dataclasses import replace
        from shapely.ops import unary_union
        settings = ThermalSettings(ambient_c=float(self.txt_ambient.GetValue()),
                                   convection_w_m2k=float(self.txt_cooling.GetValue()),
                                   substrate_k_w_mk=float(self.txt_substrate_k.GetValue()),
                                   spacing_mm=float(self.txt_thermal_step.GetValue()))
        settings.validate()
        region = board_region(getattr(self, '_simulation_board', self.board))
        copper = {layer: unary_union([geo[layer] for geo in geometry.values() if layer in geo])
                  for layer in stackup['copper']}
        model_key = signature((getattr(self, '_electrical_key', None), stackup,
                               [(layer, geo.wkb_hex) for layer, geo in sorted(copper.items())], region.wkb_hex,
                               settings.convection_w_m2k, settings.substrate_k_w_mk,
                               settings.spacing_mm, self.chk_convergence.GetValue(), backend))
        if self._last_electrical is not None and self._last_thermal is not None and self._last_thermal[0] == model_key:
            old = self._last_thermal[1]
            result = dict(old)
            result['temperature_c'] = old['temperature_c']+(settings.ambient_c-old['settings'].ambient_c)
            result['settings'] = replace(settings, spacing_mm=old['settings'].spacing_mm)
            result['diagnostics'] = dict(old['diagnostics'], ambient_shift_only=True)
            result['timings'] = dict(grid_seconds=0, heat_mapping_seconds=0, assembly_solve_seconds=0)
            self.log('Reusing validated thermal rise; ambient temperature shifts the field without a solve.')
            self.thermal_result = result
            self._last_thermal = model_key, result
            self._set_progress(95, 'Thermal results reused')
            return
        links = {(link.ref_des, link.net_a, link.net_b): link
                 for rail in self.power_tree.rails for link in rail.series_components}
        def run(step, start, end, initial=None):
            from dataclasses import replace
            import time
            def progress(fraction, stage):
                self._set_progress(start+(end-start)*fraction, stage)
            progress(0, 'Building thermal grid')
            self.log(f'Thermal thin-board estimate: {step:g} mm grid; selected copper only; uniform thickness temperature.')
            started = time.perf_counter()
            grid_key = region.wkb_hex, step
            grid = self._thermal_grids.get(grid_key)
            if grid is None:
                # Keep at most a coarse/refined pair and release old solver workspaces.
                if len(self._thermal_grids) >= 2:
                    victim = next(iter(self._thermal_grids))
                    self._thermal_grids.pop(victim).close()
                from resource_budget import MemoryBudgetExceeded
                try:
                    grid = ThermalGrid(region, replace(settings, spacing_mm=step), self._memory_budget(),
                                       self._retained_memory_bytes((initial,)), progress=progress)
                except MemoryBudgetExceeded:
                    self.log('Releasing unused caches before thermal allocation; thermal spacing is unchanged.')
                    self._release_optional_ram(keep_meshes=[data['mesh'] for data in self.system_results.values()])
                    try:
                        grid = ThermalGrid(region, replace(settings, spacing_mm=step), self._memory_budget(),
                                           self._retained_memory_bytes((initial,)), progress=progress)
                    except MemoryBudgetExceeded as error:
                        phase = 'Thermal refined pass' if initial is not None else 'Thermal coarse pass'
                        text = f'{phase} at {step:g} mm: {error}'
                        from resource_budget import feasible_thermal_spacing
                        suggestion = feasible_thermal_spacing(region, step, error.allowed_additional_bytes)
                        if suggestion is not None:
                            text += f' Try thermal Grid mm >= {suggestion*(2 if initial is not None else 1):g}.'
                            if initial is not None:
                                text += ' Convergence also runs at half that spacing.'
                            text += ' This is a memory-fit estimate; thermal convergence must still pass.'
                        raise ValueError(text) from error
                self._thermal_grids[grid_key] = grid
            else:
                grid.settings = replace(settings, spacing_mm=step)
                grid.progress = progress
                self.log(f'Reusing thermal grid at {step:g} mm.')
            grid.heat.fill(0)
            grid_seconds = time.perf_counter()-started
            started = time.perf_counter()
            copper_w = add_copper_heat(grid, self.system_results, progress=lambda fraction: progress(.2+.25*fraction, 'Mapping copper heat'))
            series_w = 0.0
            for part in self.component_results:
                if part['loss_w'] <= 0:
                    continue
                series_w += part['loss_w']
                link = links[part['ref'], part['net_a'], part['net_b']]
                for net, pad in [(link.net_a, link.pad_a), (link.net_b, link.pad_b)]:
                    mesh = self.system_results[net]['mesh']
                    nodes = self._get_mesh_nodes(mesh, link.ref_des, [pad], self.chk_debug.GetValue())
                    coords = mesh.node_coords.take(nodes) if hasattr(mesh.node_coords, 'take') else [mesh.node_coords[n] for n in nodes]
                    grid.deposit(coords, [part['loss_w']/(2*len(nodes))]*len(nodes))
            from regulator_heat import add_regulator_heat
            regulator_w = add_regulator_heat(grid, self.system_results,
                getattr(self, '_simulation_board', self.board),
                lambda mesh, ref, pads: self._get_mesh_nodes(mesh, ref, pads, self.chk_debug.GetValue()), self.log)
            expected_heat = copper_w+series_w+regulator_w
            if not math.isclose(float(grid.heat.sum()), expected_heat, rel_tol=1e-8, abs_tol=1e-10):
                raise ValueError('Thermal copper/series/regulator heat mapping failed total-watt conservation.')
            heat_seconds = time.perf_counter()-started
            started = time.perf_counter()
            initial_rise = interpolate_thermal(initial, grid) if initial is not None else None
            solved = grid.solve(stackup, copper, backend, self.log, initial_rise=initial_rise)
            solved['heat_sources_w'] = dict(copper=copper_w, series=series_w, regulator=regulator_w)
            assembly_solve_seconds = time.perf_counter()-started
            solved['timings'] = dict(grid_seconds=grid_seconds, heat_mapping_seconds=heat_seconds,
                                    assembly_solve_seconds=assembly_solve_seconds)
            if hasattr(self, '_thermal_pass_timings'):
                self._thermal_pass_timings.append(dict(spacing_mm=step, **solved['timings'],
                    backend=solved['diagnostics']['backend'], iterations=solved['diagnostics']['iterations']))
            self.log(f'Thermal timings: grid {grid_seconds:.2f} s, heat mapping {heat_seconds:.2f} s, assembly/solve {assembly_solve_seconds:.2f} s.')
            return solved
        thermal_coarse_end = 75 if self.chk_convergence.GetValue() else 95
        result = run(settings.spacing_mm, 70, thermal_coarse_end)
        result['mesh_check'] = 'unchecked'
        if self.chk_convergence.GetValue():
            coarse_peak = float(result['temperature_c'].max()-settings.ambient_c)
            result = run(settings.spacing_mm/2, thermal_coarse_end, 95, result)
            peak = float(result['temperature_c'].max()-settings.ambient_c)
            if abs(peak-coarse_peak) > .05*max(peak, 1e-6):
                raise ValueError('Thermal peak rise changed by more than 5%. Reduce thermal grid spacing and rerun.')
            result['mesh_check'] = 'passed (peak rise within 5%)'
        result['overlay_layer_names'] = {lid: stackup['copper'][lid].get('name', str(lid)) for lid in stackup['layer_order']}
        result['overlay_cache_token'] = getattr(self, '_board_token', None)
        self.thermal_result = result
        if self._last_electrical is not None:
            self._last_thermal = model_key, result

    def _debug_plot_geo(self, extractor, geo):
        try:
            buf = extractor.plot_geometry(geo)
            if buf:
                img = wx.Image(buf, wx.BITMAP_TYPE_PNG)
                self.results_notebook.DeleteAllPages()
                self._add_plot_tab("Geometry Debug", wx.Bitmap(img))
        except: pass

    def _debug_plot_mesh(self, mesher, mesh, stackup):
        try:
            plotter = Plotter(debug=self.chk_debug.GetValue())
            bmp = plotter.plot_3d_mesh(mesh, stackup)
            if bmp:
                self.results_notebook.DeleteAllPages()
                self._add_plot_tab("Mesh Debug", bmp)
        except: pass

    def _update_results_ui(self):
        # Populate text stats
        txt = "System Simulation Results:\n==========================\n"
        for net, data in self.system_results.items():
            vmin, vmax, drop = data['stats']
            txt += f"Rail: {net}\n"
            txt += f"  Range: {vmin:.4f} - {vmax:.4f} V\n"
            txt += f"  Drop:  {drop:.4f} V\n"
            diag = data.get('diagnostics', {})
            txt += f"  Copper loss: {diag.get('copper_loss_w', 0):.6g} W\n"
            txt += f"  Solver: {diag.get('backend', 'CPU')}, {diag.get('preconditioner', 'direct')}, {diag.get('iterations', 0)} iterations, {diag.get('solve_seconds', 0):.2f} s\n"
            txt += f"  Network setup: {diag.get('network_setup_seconds', 0):.2f} s; RHS solves: {diag.get('linear_rhs_solves', 1)}; final warm start: {diag.get('warm_start', False)}\n"
            txt += f"  Solved nodes: {len(data['mesh'].nodes):,}\n"
            txt += f"  Mesh convergence: {diag.get('mesh_check', 'unchecked')}\n"
            txt += f"  Current balance error: {diag.get('balance_a', 0):.3g} A\n\n"
        parts = getattr(self, 'component_results', [])
        for part in parts:
            current = part['current_a']
            drop = part['drop_v']
            txt += f"Series: {part['ref']} ({part['net_a']} -> {part['net_b']})\n"
            txt += f"  State: {'connected' if part['closed'] else 'open'}\n"
            txt += f"  Current A -> B: {current:.6g} A\n" if current is not None else "  Current: indeterminate ideal loop or unpowered terminal\n"
            txt += f"  Voltage A - B: {drop:.6g} V\n" if drop is not None else "  Voltage drop: unpowered terminal\n"
            txt += f"  Component loss: {part['loss_w']:.6g} W\n\n"
        txt += f"Total copper loss: {sum(d['diagnostics']['copper_loss_w'] for d in self.system_results.values()):.6g} W\n"
        txt += f"Total series component loss: {sum(p['loss_w'] for p in parts):.6g} W\n"
        from regulator_heat import regulator_losses
        regulators = regulator_losses(self.system_results)
        for reg in regulators:
            txt += f"Regulator: {reg['name']} ({reg['input_rail']} -> {reg['output_rail']})\n"
            txt += f"  Input: {reg['input_voltage_v']:.6g} V, {reg['input_current_a']:.6g} A, {reg['input_power_w']:.6g} W\n"
            txt += f"  Output: {reg['output_voltage_v']:.6g} V, {reg['output_current_a']:.6g} A, {reg['output_power_w']:.6g} W\n"
            txt += f"  Regulator device heat: {reg['loss_w']:.6g} W\n"
            txt += f"  Heat placement: {reg['input_ref']}" + (f" / {reg['output_ref']} (equal split)" if reg['output_ref'] != reg['input_ref'] else '') + '\n'
        txt += f"Total regulator loss: {sum(reg['loss_w'] for reg in regulators):.6g} W\n"
        thermal = getattr(self, 'thermal_result', None)
        if thermal:
            temperatures = thermal['temperature_c']
            txt += f"\nThermal estimate (steady-state board average):\n"
            txt += f"  Range: {temperatures.min():.2f} to {temperatures.max():.2f} °C\n"
            txt += f"  Ambient: {thermal['settings'].ambient_c:g} °C; h per face: {thermal['settings'].convection_w_m2k:g} W/m²K\n"
            txt += f"  Heat: {thermal['input_w']:.6g} W; cooling: {thermal['rejected_w']:.6g} W; balance error: {thermal['balance_w']:.3g} W\n"
            heat = thermal.get('heat_sources_w', {})
            txt += f"  Heat sources: copper {heat.get('copper', 0):.6g} W; series {heat.get('series', 0):.6g} W; regulators {heat.get('regulator', 0):.6g} W\n"
            txt += f"  Thermal mesh: {thermal['mesh_check']}\n"
            txt += '  Selected copper only. Uniform temperature through thickness. Regulator losses deposited at configured footprint centres (pad contacts if unavailable). Load/package junction models, radiation and temperature feedback omitted.\n'
        if getattr(self, 'thermal_error', None):
            txt += f"\nThermal estimate unavailable: {self.thermal_error}\n"
        self.result_text.SetValue(txt)
        
        # Clear existing plot tabs
        self.results_notebook.DeleteAllPages()
        self._add_temperature_tab(thermal)
        
        if not self.system_results:
            return
        
        # Get stackup once
        try:
            extractor = GeometryExtractor(self.board)
            stackup = extractor.get_board_stackup()
        except: 
            stackup = None
        
        # Get Drop % from UI for coloring scale
        try:
            drop_pct_ui = float(self.txt_drop_pct.GetValue())
            if drop_pct_ui < 0: drop_pct_ui = 0
            if drop_pct_ui > 100: drop_pct_ui = 100
        except:
            drop_pct_ui = 5.0
        
        debug_mode = self.chk_debug.GetValue()
        plotter = Plotter(debug=debug_mode)

        # Shared board coordinates and an absolute voltage scale across solved nets.
        meshes = {}
        layers = set()
        for net, data in self.system_results.items():
            mesh = data['mesh']
            mesh.results = data['results']
            meshes[net] = mesh
            if hasattr(mesh.node_coords, 'parts'):
                layers.update(int(p[0, 2]) for p in mesh.node_coords.parts if len(p))
            else:
                layers.update(int(c[2]) for c in mesh.node_coords.values())
        combined_notebook = wx.Notebook(self.results_notebook)
        combined_min = min(d['stats'][0] for d in self.system_results.values())
        combined_max = max(d['stats'][1] for d in self.system_results.values())
        for lid in sorted(layers):
            name = str(lid)
            if stackup and lid in stackup.get('copper', {}):
                name = stackup['copper'][lid].get('name', name)
            bitmap = plotter.plot_combined_layer(meshes, lid, name, combined_min, combined_max)
            if bitmap:
                page = wx.ScrolledWindow(combined_notebook, style=wx.HSCROLL | wx.VSCROLL)
                page.SetScrollRate(10, 10)
                image = wx.StaticBitmap(page, bitmap=bitmap)
                sizer = wx.BoxSizer(wx.VERTICAL)
                sizer.Add(image, 1, wx.CENTER | wx.ALL, 5)
                page.SetSizer(sizer)
                combined_notebook.AddPage(page, name)
        if combined_notebook.GetPageCount():
            self.results_notebook.AddPage(combined_notebook, 'All Rails')
        else:
            combined_notebook.Destroy()
        
        # Create nested tabs for each rail
        for rail_name, data in self.system_results.items():
            # Create rail-level notebook
            rail_notebook = wx.Notebook(self.results_notebook)
            
            mesh = data['mesh']
            mesh.results = data['results']
            vmin, vmax, _ = data['stats']
            
            # Override vmin for plot based on drop %
            nominal = vmax
            plot_vmin = nominal * (1.0 - drop_pct_ui / 100.0)
            
            # Add 3D plot tab
            bmp_3d = plotter.plot_3d_mesh(mesh, stackup, vmin=plot_vmin, vmax=vmax)
            if bmp_3d:
                page_3d = wx.ScrolledWindow(rail_notebook, style=wx.HSCROLL | wx.VSCROLL)
                page_3d.SetScrollRate(10, 10)
                img_ctrl_3d = wx.StaticBitmap(page_3d)
                img_ctrl_3d.SetBitmap(bmp_3d)
                sizer_3d = wx.BoxSizer(wx.VERTICAL)
                sizer_3d.Add(img_ctrl_3d, 1, wx.CENTER | wx.ALL, 5)
                page_3d.SetSizer(sizer_3d)
                rail_notebook.AddPage(page_3d, "3D View")
            
            # Add layer tabs
            unique_layers = [int(part[0, 2]) for part in mesh.node_coords.parts if len(part)] if hasattr(mesh.node_coords, 'parts') else list(set(n[2] for n in mesh.node_coords.values()))
            unique_layers.sort()
            
            for lid in unique_layers:
                # Get Layer Name
                l_name = str(lid)
                if stackup and 'copper' in stackup and lid in stackup['copper']:
                    l_name = stackup['copper'][lid].get('name', str(lid))
                
                bmp_2d = plotter.plot_layer_2d(mesh, lid, stackup, vmin=plot_vmin, vmax=vmax, layer_name=l_name)
                if bmp_2d:
                    page_2d = wx.ScrolledWindow(rail_notebook, style=wx.HSCROLL | wx.VSCROLL)
                    page_2d.SetScrollRate(10, 10)
                    img_ctrl_2d = wx.StaticBitmap(page_2d)
                    img_ctrl_2d.SetBitmap(bmp_2d)
                    sizer_2d = wx.BoxSizer(wx.VERTICAL)
                    sizer_2d.Add(img_ctrl_2d, 1, wx.CENTER | wx.ALL, 5)
                    page_2d.SetSizer(sizer_2d)
                    rail_notebook.AddPage(page_2d, l_name)
            
            # Add rail notebook as a page in the main results notebook
            self.results_notebook.AddPage(rail_notebook, rail_name)
        
        # Switch to Results tab
        self.notebook.SetSelection(1)

    def on_close(self, event):
        if getattr(self, '_running', False):
            self._cancel_requested = True
            if hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        self.EndModal(wx.ID_CANCEL)

    def on_destroy(self, event):
        if event.GetEventObject() is self:
            self._eta_timer.Stop()
            self._clear_caches()
        event.Skip()
