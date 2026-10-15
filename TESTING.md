# Electrical accuracy update

Latest release: **0.9.0**, with reusable multigrid solves and regulator terminal models. Earlier sections
describe the release history; current thermal features and limitations appear
at the end of this document.

The shared KiPIDA folder contains the updated source. Original core files are in
`review_backup/`. The PCM ZIP beside the folder contains only runtime files and package metadata;
it excludes the test environment, caches, backups, and development scripts.

## Test in KiCad

1. Use KiCad **9.0.1 or newer** and enable its IPC API.
2. In KiCad's Plugin and Content Manager choose **Install from File** and select
   `KiPIDA-PCM.zip` beside this folder. It contains root `metadata.json` and a
   `plugins/` payload, validated against the official KiCad schemas.
   `KiPIDA-fixed.zip` is now an identical compatibility copy of this PCM installer.
3. Restart KiCad. If you previously installed KiPIDA manually, remove that old
   manual copy to avoid two plugins with the same identifier. Keep your project
   configuration files. This build uses the original KiPIDA identifier.
4. Open a board with a physical stackup, refill zones, and launch KiPIDA. The
   plugin runtime requires NumPy, SciPy, Shapely, Matplotlib, wxPython, and
   kicad-python, listed in `requirements.txt`. The shared `.venv/` is only for tests.
5. Keep **Check mesh convergence** enabled. Start with 0.1 mm spacing. If a pad
   or annular ring has no mesh nodes, or refinement changes voltage drop/total
   loss by more than 5%, halve the spacing and run again.
6. Verify voltage drop, total copper loss (W), current balance, and mesh status.
   Unmapped or disconnected loads now stop the simulation with an explanation.
7. Try a regulator chain: a 5 V / 1 A load supplied by a 10 V input at 80%
   efficiency draws 0.625 A at its input when the local input voltage is 10 V.
   Copper and series drops increase switching-regulator input current.

The previous ZIP was a manual-extraction archive, which caused the PCM error
about missing metadata.json. Neither the plugin icon nor the packaged PNGs
contain iCCP or cHRM chunks; those warnings have not been traced to KiPIDA.

## Applied changes

- Removed duplicate regulator input-current stamping.
- Removed 0.05 mm copper widening on each side of traces and vias.
- Use KiCad's pad polygons and actual copper layers for terminals and extraction.
- Preserve polygon holes and prevent neighbor links across copper gaps.
- Weight lateral conductance by the copper width at each cell face.
- Use finished drill diameter, plating thickness, and physical stackup distances
  for via/PTH barrels. Slotted PTH barrels use the slot perimeter.
- Do not attach vertical connections to nodes outside the contact geometry.
- Map IPC layer IDs correctly in the offline fallback; connected-board stackup
  failures stop the run instead of substituting a guessed two-layer stackup.
- Eliminate voltage boundaries while preserving matrix symmetry; reject invalid
  boundaries, non-finite solutions, excessive residuals, and current imbalance.
- Exclude unloaded, unpowered copper islands from voltage results and plots.
- Clear results on failure so earlier or partial results are not shown as valid.
- Compare coarse and half-spacing meshes, using the refined results after a pass.
- Report total copper loss and correct the README's unimplemented feature claims.

## Verification

Run from this folder in PowerShell:

```powershell
$env:MPLCONFIGDIR = Join-Path (Get-Location) '.mpl-cache'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

39 checks passed, with one GUI import check skipped because wxPython is unavailable
in the isolated test environment. Checks cover analytical rectangular-trace
resistance at three mesh spacings, copper loss, holes, gaps, via conductance and
vertical connectivity, real IPC pad polygon types, disconnected loads, sparse node
IDs, terminal layer restrictions, and production rail orchestration. The production
orchestration check also exercises successful and rejected mesh refinement.

The live KiCad connection and dialog still need your test.

## Model limits

This update implements electrical corrections and total Joule power loss. It does
not add local current-density plots or temperature prediction. Copper resistivity
is fixed. Explicitly stitched nets are solved together; return-path resistance is omitted.

Via/PTH plating defaults to **25 micrometres**, logged during meshing. The model
accepts a `via_plating_mm` stackup field, but a user-editable plating control is
not yet present. As of 0.9.0, barrel conductance is distributed around the finished
hole perimeter rather than injected at one node. Local three-dimensional
current crowding within the barrel wall is still not resolved.

The 5% refinement check covers rail voltage drop, integrated copper loss, and
series-component current/drop/loss, not local
peak current density. Curved API polygon boundaries are approximated by segments
of at most two degrees. The rectangular grid can require small spacing for narrow
or diagonal features. Grid size is limited by the estimated RAM/VRAM budgets
instead of a fixed two-million-point cap (see 0.7.0 below).

## Series components (0.6.0)

1. Select the supply rail and set its source and supply voltage at the actual
   input connector. Put the load at its real downstream terminal.
2. Click **+ Series Component**, choose F1, and select its two pads. The pad nets
   come from the board; missing nets are added to the rail list automatically.
3. Enter the component resistance in **milliohms**, using a datasheet or measured
   value. For example, 20 mOhm at 2 A produces 40 mV drop and 0.08 W loss.
   Zero means an ideal connection. Clear **Connected** to model an open fuse.
4. Remove any artificial source or load previously placed at F1 to analyze
   the two sides separately. The fuse is now the connection between those sides.
   The intermediate rail's nominal voltage does not clamp its solved voltage.
5. Run with mesh convergence enabled. Read signed A-to-B component current,
   A-minus-B voltage, component loss, and separate total copper/component losses.
6. Double-click a series row on either rail to edit it; **- Remove** deletes
   the link. Save Config persists it. Existing version 1.0 files still load;
   newly saved files use version 1.1 and require this updated plugin.

Passive links are bidirectional and can form branched networks. Contacts are
lumped equipotential electrodes within each selected pad. Ideal-link loop currents
may be indeterminate and are labelled accordingly. Regulator dependencies must be
acyclic, with distinct input/output networks and uniquely attributable output
terminals. Switching inputs use an iterated constant-power load; failures to
converge stop the solve. Linear-regulator quiescent current is omitted.

This is a fixed-resistance electrical model: component watts estimate Joule heat
production, not temperature, fuse trip time, or a coupled thermal solution.

## Startup troubleshooting

The installed launcher appends startup details to `%TEMP%/kipida_entry.log`
and explicitly raises the analysis dialog on Windows. Connection failures
produce a foreground error with the exception and log path.

If clicking the toolbar icon produces no new log entry, KiCad has not invoked
the launcher. Restart the PCB Editor and KiCad manager to reload the plugin,
and check that the tooltip identifies **Ki-PIDA Simulation**. Keep the board
open when launching. The shared folder also contains `Start KiPIDA.cmd`, a
direct launcher for this user's KiCad 9 installed runtime; it bypasses toolbar
dispatch and connects to the running board without editing it.

## Windows toolbar bridge (0.6.1)

KiCad 9's IPC toolbar dispatch did not start the launcher on the tested system,
although directly starting the same installed runtime opened the dialog. This
build registers a minimal pcbnew ActionPlugin toolbar bridge on Windows. It
launches the isolated runtime without importing analysis dependencies inside
KiCad. The electrical analysis and board access still use IPC. The manifest's
IPC action remains available as **Ki-PIDA Simulation (IPC)** with its toolbar
button hidden, avoiding duplicate icons after reload.

Reload Python action plugins or reopen the PCB Editor after this update. The
new toolbar tooltip is **Open KiPIDA (KiCad 9 toolbar bridge)**. Bridge launch
events are appended to `%TEMP%/kipida_toolbar.log`; analysis startup is logged
in `%TEMP%/kipida_entry.log`. Missing runtime files produce a KiCad error dialog.
42 executable checks pass, plus one GUI import check is skipped in the isolated
test environment. The real KiCad-managed Python runtime also exercises the
bridge launch with the open board. Actual toolbar dispatch needs user testing.

## Series dialog fix (0.6.2)

KiCad IPC footprint references contain BoardText objects; the series dialog
now extracts their string values before sorting component choices. Constructor
failures are logged and displayed rather than appearing as an inactive button.
Regression coverage includes actual FootprintInstance, Field, BoardText and Pad
protobuf wrappers. 44 executable checks pass with one GUI import check skipped.
The dialog was also constructed and displayed against the user's open board.

## Series chain list and defaults (0.6.3)

The component list shows every series link in the selected net's connected
chain, including open links and links farther downstream. Each row identifies
both nets and pads; editing and deletion use the link's owning rail. A newly
added link is selected and scrolled into view. If it belongs to a separate
chain, its first rail is selected automatically so the new link is visible.
New series components default to 0 mOhm (ideal connection); editing preserves
the configured resistance. Series, source/load and regulator component choices
use natural reference sorting (R2, R3, R10, R29); series pad numbers also sort
numerically. 48 executable checks pass with one GUI import check skipped.
Real wx controls verified downstream rows, deletion, default and ordering.

## Filled-zone boundary reconstruction (0.6.4)

Some IPC filled polygons express holes or islands with doubled zero-width
boundary seams. Shapely considers these rings self-intersecting. The parser
now reconstructs valid area polygons and discards leftover line geometry.
It accepts the reconstruction only when the copper area is preserved within
a tolerance of max(1e-12 mm2, 1e-10 times the original area). Repairs that
change copper area still fail, with the net/layer and validity reason included.
This does not enlarge copper or treat a boundary seam as a conductive bridge.

All 66 filled-zone polygons on the open board passed the corrected parser.
52 executable tests passed, with one GUI import skipped. New regressions cover
bridged holes, disconnected islands, genuine crossing rejection, and explicit
holes. The complete simulation of the user's unsaved configuration still needs
a rerun. Save Config before closing and reopening KiPIDA to load this update.

## Large meshes and optional CUDA (0.7.0)

Save Config, then close and reopen KiPIDA to load the update. The new Solver
control offers Auto, CPU and GPU (CUDA). Auto tries CUDA for at least 50,000
free voltage unknowns and falls back to CPU if CUDA is unavailable or fails
its checks. Explicit GPU reports failure instead of falling back. Results and
the log show the backend, iteration count and solve time.

The default RAM budget is the smaller of 8 GB and 25% of available RAM. Preflight
uses a conservative estimate covering mesh arrays, network assembly and solver
workspaces, capped at 70% of currently available RAM. It is an estimate, not an
operating-system allocation quota. Refinement has four times as many candidate
grid points and also retains the coarse mesh; increase the budget if this step
exceeds it. Start with 8 GB; 16 GB can accommodate larger refinements on this
64 GB machine when sufficient RAM remains available. Keep convergence enabled.
GPU workspaces are checked against free VRAM before allocation.

Coordinates, voltage results, conductance entries and network node mappings now
use compact arrays. Rasterization and copper-face evaluation run in bounded
batches. Pad/via lookups use local grid windows. Geometry extraction, meshing,
assembly and validation still run on CPU; only the sparse solve uses CUDA.
Large plots sample approximately 200,000 points for display; all electrical
nodes participate in the solve and loss/current checks. Plot titles identify
sampling. This release still does not implement current-density or temperature
maps.

CUDA is optional. Install `requirements-gpu.txt` using the plugin's own Python
runtime, not KiCad's embedded pcbnew Python or the development `.venv`:

```powershell
& '<plugin-runtime-python.exe>' -m pip install --no-cache-dir -r requirements-gpu.txt
```

This user's KiCad 9 runtime already has CuPy 14.2.0 and CUDA 12 component wheels
installed and verified on the RTX 3080 Ti (12 GB VRAM, driver 566.36). The PCM
archive includes the optional requirements file, but does not force a CUDA
installation for other users. CPU analysis uses `requirements.txt`.

Validation on this machine:

- The CPU test suite executes 65 checks, with three environment-dependent checks
  skipped (wx import and two optional real-CUDA checks). Real wx controls and
  compact voltage rendering were verified separately against the open board.
- Four backend tests pass in the actual plugin runtime, including real CUDA
  voltage/loss/current balance and stitched rails with ideal and resistive fuses.
- Analytical 262,144-node grid: CPU CG 5.65 s, CUDA CG 1.96 s; maximum CPU/GPU
  voltage difference 7.13e-14 V (`benchmark-gpu-512.json`).
- Analytical 2,359,296-node grid: CUDA CG 9.58 s; maximum analytical error
  8.58e-12 V and residual 1.03e-13 A (`benchmark-gpu-1536.json`). No CPU timing
  was measured at this size. These timings cover the reduced sparse solve.
- The open PowerCreep board's GND geometry at 0.1 mm successfully meshes
  5,889,624 candidate points into 3,439,501 copper nodes across four layers in
  82.75 s (`benchmark-live-gnd.json`). This is a meshing check, not an electrical
  simulation of the user's unsaved source/load configuration.

`benchmark_gpu.py` reproduces the analytical grid benchmark. GPU CG still has
a convergence limit; very ill-conditioned networks may require different mesh
spacing or CPU analysis. Finite-value, residual, source/load balance, topology
and mesh-refinement checks remain in force for either backend.

## Combined layer voltage plots (0.7.1)

Results now opens with an **All Rails** notebook. Each copper-layer tab overlays
every solved rail present on that layer in its original PCB X/Y coordinates,
with net labels and a single absolute voltage color scale shared across layers.
The scale spans the full solved system voltage range; it is independent of the
per-rail Max Drop % setting. Individual rail tabs retain their detailed drop
scales. Copper gaps and separate nets are not interpolated or electrically joined
by this display. Unpowered nodes are excluded. Each combined layer distributes
an approximately 200,000-point display budget across its rails; full electrical
results remain unchanged.

67 CPU-suite checks pass with three environment-dependent checks skipped.
Regressions cover board positions, layer filtering, omitted unpowered nodes,
common color normalization and an empty layer. The actual KiCad wx runtime also
checks the combined result notebook and plots. Save Config and reopen KiPIDA,
then rerun the simulation to populate **Results > All Rails > F.Cu/B.Cu/etc.**

## Experimental thermal analysis (0.8.0)

Save Config and reopen KiPIDA. Enable **Thermal estimate (copper + series + regulator heat)**
before Run Simulation. Defaults are ambient 25 °C, h=10 W/(m² K) on EACH board
face, 1 mm thermal grid, substrate k=0.3 W/(m K), and copper k=400 W/(m K).
These are editable modeling assumptions (copper k currently fixed), not measured
properties or an automatically determined convection coefficient. Thermal controls
currently reset when the dialog is reopened; Save Config persists the power tree.
Use suitable substrate and cooling values for your board and environment.

The new **Temperature (board average)** tab uses the actual closed board-level
Edge.Cuts outline, including cutouts. Segments, arcs, circles and rectangles are
supported; unsupported shapes or open contours fail explicitly. It uses a
cell-centered finite-volume steady-state thin-board model:

`-div((k_sub*t_sub + sum(k_cu*t_cu))*grad(T)) + 2*h*(T-Tambient) = heat/area`

Copper contributions are restricted to supplied copper geometries at each cell
face. Dielectric thickness comes from the physical stackup. Cell boundary areas
and face widths are clipped to Edge.Cuts; the board edge is insulated. Convection
acts on both broad faces. See [thin-structure heat transfer theory](https://doc.comsol.com/6.4/doc/com.comsol.help.heat/heat_ug_theory.07.028.html)
for the thickness-integrated formulation.

Every solved copper edge supplies `G*(Va-Vb)^2` watts, divided between its
endpoints, including electrical via/PTH loss. The mapped power must match each
rail's electrical copper-loss total. Series component loss is split equally
between its two pad contacts. All this series loss is assumed to enter the PCB.
The solver checks finite temperatures, nonnegative temperature rise, residual
and total heat rejection. With **Check mesh convergence** enabled, a second
thermal solve at half spacing must change peak temperature rise by no more than
5%; the refined map is then displayed. Thermal failure retains valid electrical
results and explains why the temperature estimate is unavailable. RAM checks
include the retained electrical mesh; Auto/CPU/GPU also applies to thermal solves.

Limits of this FIRST thermal model:

- Temperature is uniform through board thickness; the plot is board-average,
  not separate layer temperatures or component junction temperatures.
- Only electrically selected rail copper contributes to spreading. Unselected
  copper planes, packages, heatsinks, mounting screws and other heat paths are
  omitted. All board dielectric still participates.
- Loads and regulators do not yet contribute their own device dissipation.
  Applied load power is not treated as copper heat. Radiation and airflow
  calculation are omitted; h is a prescribed per-face coefficient.
- Electrical copper resistivity is fixed; temperature feedback and iterative
  electrothermal coupling are not implemented. This is a first-pass estimate
  rather than a sign-off temperature prediction.
- Features below thermal grid spacing may be unresolved; refine the grid,
  particularly around narrow cutouts or localized heat sources.

Verification: eight thermal regressions cover analytical uniform cooling,
two-cell heat spreading, copper conductance, cutouts/islands, partial boundary
cell area, electrical-to-thermal power conservation including vias, refinement,
invalid inputs and outline parsing. The full suite has 76 checks, with four
environment-dependent skips in the CPU test environment. Five backend checks
pass in the actual runtime, including an analytical thermal CUDA solve.
The real KiCad UI pipeline was tested on PowerCreep's 14,480.24 mm² outline:
14,619 thermal cells with 2.08 W of SYNTHETIC copper/series test heat and
6.22e-15 W heat-balance error. The user's actual configured power tree still
needs a thermal run. `check_thermal_ui.py` reproduces that read-only smoke check.

## Temperature inspection (0.8.1)

The Temperature tab displays **Highest simulated temperature**, its PCB X/Y
coordinates, and a cyan cross at that thermal cell. This maximum is computed
from all cells in the current successful thermal result. It is a simulation
estimate, not a sensor reading or an accumulated maximum across previous runs.

Move the mouse over the temperature map to read PCB X/Y in mm and the cell's
temperature in °C. Outside the board, in a cutout, or outside the plot, the
readout clears instead of showing a nearby cell or a stale temperature. The
display Y axis is inverted to match the other board plots; the readout restores
KiCad's PCB Y coordinates. The temperature is the solved cell value, without
interpolation below the thermal mesh resolution. The toolbar supports zoom,
pan, reset and saving the plot; the pointer readout follows the current view.

Four new CPU regressions cover peak coordinates, cell/gap lookup, partial-cell
cutouts and invalid/outside pointers. The actual wx canvas smoke test verifies
the maximum label, Matplotlib mouse-event coordinate transform, temperature
readout, PCB Y sign and rebuilding the result tab. The full suite runs 80 checks
with four environment-dependent skips in the CPU environment.

## Transparent PCB overlay (0.8.2)

In the Temperature tab, enable **PCB copper overlay**, select a copper layer,
and adjust **Opacity** from 0% (invisible) to 100% (opaque). The default is 35%.
Cyan copper and the white Edge.Cuts outline overlay the temperature map in the
same board coordinates, with pad/via and zone holes preserved. The maximum
temperature marker stays above the overlay. This shows copper and outline;
silkscreen and 3D component bodies are not rendered.

The first activation reads ALL net copper, including electrically unselected
and unconnected copper, from the current board. Geometry is cached for that
result view so toggling, opacity changes and layer switching do not repeatedly
read KiCad. Board changes require a fresh simulation/view. No electrical or
thermal solve runs when the overlay changes. The thermal calculation still uses
only electrically selected copper; this display does not add unselected copper
to the heat-spreading model. Zoom, cursor temperatures and temperature scaling
remain unchanged by visibility or opacity controls.

The full suite runs 83 checks with four environment-dependent skips. New
regressions cover PCB coordinate flipping, hole winding, islands and all-net
extraction. The real KiCad wx check exercises the complete board overlay,
layer selection, slider alpha, caching, preserved zoom and mouse temperature.

## Temperature tab visibility fix (0.8.3)

Thermal analysis is now enabled by default on opening KiPIDA. The Temperature
tab is created immediately and remains present when analysis is disabled, before
the first run, or after a validation failure. Without a thermal result it shows
the reason and a **Configure thermal analysis** button. That button enables
thermal analysis and returns to configuration; Run Simulation still starts the
calculation explicitly.

If interactive canvas construction fails, the tab shows the diagnostic, maximum
temperature and a static temperature map instead of removing the tab or clearing
valid electrical results. The exception is also written to the startup log.
The real wx checks cover initial/default state, disabled state, thermal failure,
the configuration button and a forced canvas exception with static fallback.

## Simulation progress (0.8.4)

The Log tab now has a bottom progress bar, a percentage at its right and the
current stage above it. Each simulation resets to 0%. It tracks geometry
extraction, copper mesh batches, validated network groups, refinement, thermal
grid/heat mapping/assembly/solve and result rendering. Progress is a weighted
stage-completion estimate, not elapsed time or an ETA. Mesh work is weighted
by candidate grid points; refinement receives four times the coarse weight.
Sparse solves have no reliable remaining-work estimate, so the percentage holds
while iteration messages update the stage text. It advances when validation
finishes. Completed results reach 100%; partial completion with a failed thermal
estimate is labeled explicitly. Failed/cancelled runs retain their last value
and show that status. UI updates occur on changed percentages/stages rather
than on every batch, and progress callbacks also honor cancellation.

Three new regressions verify monotonic mesh callbacks, validated network-group
completion and thermal-grid/solve callbacks. The real wx production-flow check
verifies coarse/refined progress, the percentage label, 100% completion, reset,
failure and cancellation (`check_progress_ui.py`).

## Thermal performance and toolbar fix (0.8.5)

The thermal model and grid resolution are unchanged. Copper geometry is clipped
to the board once per layer, then prepared for repeated geometric queries.
Interior thermal cells/faces use their full area/width directly; exterior
cells/faces use zero. Only boundary intersections need exact clipping. Heat
deposit uses vectorized accumulation into cells. The log now reports thermal
grid creation, heat mapping and assembly/solve timings for each resolution.

A deterministic synthetic 30×30 mm board with one cutout and four copper layers,
each with 225 holes, was compared against the unchanged 0.8.4 solver on this
machine (`benchmark_thermal.py --synthetic`, `benchmark-thermal.json`).
At 1 mm: 6.32 s before, 0.045 s after. At 0.5 mm: 19.45 s before, 0.49 s after
(about 40×). These cover thermal grid creation plus assembly/solve, excluding
electrical analysis, heat mapping and plotting. Maximum temperature difference
was zero in these runs. Actual board/configuration speedups will depend on
geometry. Whole-board baseline comparison was stopped because it was too slow;
the current board also returned busy to a later API attempt.

For faster preliminary runs, increasing Thermal Grid spacing from 1 to 2 mm
reduces candidate thermal cells by about fourfold, at the cost of spatial
resolution. Keep refinement checking enabled; localized peaks may need the
finer grid. Auto already selects CPU/CUDA appropriately for system size; CUDA
only accelerates the sparse solve, not geometry assembly.

The temperature toolbar is for plot navigation and image export:

- Home restores the original view; Back/Forward navigate view history.
- Pan moves the view; Zoom selects a rectangular area (toggle again to exit).
- Save exports the plotted image, including visible overlays.

The Subplot configuration tool was removed. It opens a separate unmanaged
window from the modal KiPIDA dialog, which can leave it unresponsive; this
single-plot layout is already automatic. Close the old KiPIDA dialog and reopen
to load this update. The revised toolbar is tested in a real modal wx dialog,
including normal closure (`check_thermal_toolbar.py`). Two new regressions
check one-time copper clipping and accumulated heat deposits; the full CPU
suite runs 88 checks with four environment-dependent skips.

## Prepared networks, regulator ports and mesh diagnostics (0.9.0)

Save Config, close KiPIDA and reopen to load all new modules. PyAMG 5.3.0 is
installed in this user's isolated KiCad runtime; `requirements.txt` now includes
`pyamg>=5.3,<6` for other installations. No global KiCad Python installation is
modified. The PCM archive includes all required new runtime modules.

Applied changes:

- Prepare terminal mappings, ideal-contact contraction, copper edges, matrix,
  topology, voltage boundaries and reduced free-node system once per network
  at each mesh resolution. Power-tree/link data is snapshotted during the run.
- Reuse CPU direct factors or CPU/GPU preconditioners and GPU CSR storage across
  right-hand sides. Warm-start CG from the previous voltage deviations, or from
  a reconstructed terminal-response solution for final validation.
- Replace the regulator's 40-pass full-network fixed-point loop with a small
  terminal-response model. One active switching regulator uses a base RHS, one
  unit-current RHS and a warm-started final RHS. Zero RHS is solved analytically.
  M switching regulators use M+2 RHS solves; the nonlinear Newton/backtracking
  loop runs only on M port variables. Linear regulators draw their known output
  current and need no outer loop. Zero-power switching ports need no unit solve.
- The singleton model chooses the stable high-voltage quadratic root. Coupled
  ports use safeguarded Newton steps and detect nonpassive models, unpowered
  inputs, voltage collapse and unstable operating points. Final regulator power
  must agree with the reconstructed full-network voltage. Linear residual and
  source/load-balance tolerances have not been relaxed.
- Use symmetric algebraic multigrid for large systems when available. CPU uses
  PyAMG; CUDA uploads the hierarchy and evaluates a symmetric weighted-Jacobi
  V-cycle with a small dense coarse inverse entirely on GPU during CG. There is
  no per-iteration CPU AMG callback/transfer. RAM/VRAM setup checks and logged
  Jacobi fallback remain available. Hierarchy construction occurs on CPU and
  can add a significant one-time setup cost; it is reused for subsequent RHS.
- Bulk matrix stamps use typed-array byte copies instead of Python elementwise
  extension. Cached solver resources are released when the network pass ends;
  new meshes/resolutions and changed boundaries prepare a new system.
- Log coarse/refined spacing, network name, base/unit-port/final RHS stage,
  cached setup, preconditioner, inner iterations and warm starts. Backend timings
  separate setup, RHS transfer, iterations/result transfer and CPU residual checks.

Mesh-convergence fixes and limits:

- Grid coordinates are rounded to 12 decimal places in mm to suppress arithmetic
  drift that excluded nodes exactly on copper boundaries. This precision is one
  million times finer than KiCad's nanometre coordinates; copper is not widened.
- Via/PTH conductance is distributed around the finished hole/slot perimeter.
  Samples map to annular copper contacts per layer, with parallel edge weights
  summing to the same analytic plated-barrel conductance. Contacts are restricted
  to actual pads/rings, and unresolved rings still fail explicitly. Slots use the
  actual rotated drill geometry. This avoids shrinking a single injection point
  on refinement and retains hole voids and physical plating thickness.
- The comparison now logs coarse/refined voltage drop, copper loss and series
  current/drop/loss with percentage and absolute changes. Component matching uses
  identity rather than list order. Powered topology changes also fail.
- Relative change must remain within 5%, with explicit absolute noise tolerances
  of 1 µV, 1 µA and 10 nW. These prevent near-zero roundoff from causing meaningless
  relative failures. Material voltage/loss differences still fail. Failure shows
  the exact metric and next recommended spacing; thermal analysis is not run.
- The user's 3.43e-11 A residual confirms a numerically solved matrix, not spatial
  convergence. The reported +12V failure could still require a finer grid; it has
  not been hidden or declared fixed without a matching board/configuration run.

Validation:

- 98 CPU-suite tests, with five environment-dependent skips, including prepared
  factors, source-boundary invalidation, CPU AMG/warm starts, multiple regulator
  ports, collapse rejection, matrix/terminal reuse, coarse/fine metrics, near-zero
  comparison noise, and distributed-ring conductance/convergence.
- Six GPU/backend tests pass in the actual runtime, including positive symmetric
  GPU AMG, hierarchy reuse, warm starts, voltage/loss balance and thermal accuracy.
- Actual wx production-flow checks exercise three RHS regulator solves in both
  coarse and refined passes, result rendering, progress/reset/failure/cancellation.
- Analytical 262,144-node CPU benchmark: Jacobi first solve 2,065 iterations/6.02 s;
  AMG first solve 16 iterations/1.68 s including setup; reused AMG solve 8/0.21 s.
- Analytical 2,359,296-node GPU benchmark: Jacobi first solve 5,948 iterations/9.54 s;
  AMG first solve 36 iterations/15.98 s INCLUDING 15.50 s setup. The next AMG RHS
  needs 16 iterations/0.17 s, versus warm-started Jacobi 2,779/4.34 s. This illustrates
  the setup/reuse tradeoff; a single RHS is not automatically faster with AMG.
  Maximum analytic voltage errors stay below 3e-11 V. Reports are in
  `benchmark-prepared-cpu-512.json` and `benchmark-prepared-gpu-1536.json`.
- The saved PowerCreep configuration inspected during this request contains no
  regulators, so it does not match the profiled run. A fresh Save Config is needed
  to reproduce that exact operating point and certify its +12V mesh comparison.

## 0.9.1 — selected-entry removal and recovery

- Component rows use single selection and retain the exact owner and object for
  sources, loads, outgoing/incoming regulators and shared series links. Remove
  shows its selected target and is disabled until a row is selected; rebuilding
  the list clears selection, preventing repeated clicks deleting another entry.
- Undo Remove restores deleted objects to their original collection and position
  in reverse order. This history lasts for the current dialog session and resets
  when loading a configuration. Removal does not delete PCB footprints or pads.
- Removing a shared link can hide a detached chain in the current rail view;
  those other links remain stored on their original rails.
- Missing source checks now name the unpowered network and explain how to add or
  restore its feed. The simulation checks this before copper extraction/meshing.
  Nominal rail voltage does not create a source automatically.
- Validation: 100 CPU-suite tests (95 passed, five environment skips), real wx
  removal checks covering equal-valued objects, incoming regulators, shared links,
  repeated clicks and multi-step undo, plus production simulation/progress checks
  verifying missing feeds fail before geometry extraction.

## 0.10.0 — configuration files, run reuse and responsive overlays

- Save As and Open Config use native file dialogs. Save writes the chosen file;
  cancelled/invalid imports preserve edits and Undo history. Writes are atomic;
  UTF-8/BOM configuration files are supported.
- Auto / Dynamic RAM is the default. It recalculates available physical memory
  with OS/KiCad headroom before large stages, displays the effective GB budget,
  bounds retained meshes/operators and releases caches when space is needed.
  Manual GB limits remain available; spacing is never silently altered.
- Run timings are stored in the user's temporary directory as
  `kipida-run-timings-v1.json`. The Log tab shows elapsed time and an approximate
  ETA range based on matching hardware/backend/cache state and estimated work.
  Coarse/refined numerical diagnostics and thermal pass timings are recorded.
  Calibrate timing runs a small independent extraction/mesh/electrical/thermal/
  rendering benchmark. It cannot predict difficult geometry or convergence
  exactly; calibration-only estimates use a wider range. Failed runs do not
  train the successful-run timing model.
- Board inputs are captured as copied protobuf snapshots and fingerprinted,
  including unsaved geometry, zones, footprints, shapes and stackup. Authoritative
  pad polygon requests are memoized on the capture thread. Meshes and geometry
  are reused only for matching board/spacing inputs; unsupported adapters disable
  cross-run result caching rather than guessing a revision.
- Prepared network matrices, factors/AMG hierarchies, GPU data and exact-mesh
  prior solutions survive compatible load-current changes. Keys include copper
  conductances, topology, source voltages/terminals, load terminals and backend.
  Convergence checks/results are reused only for identical electrical inputs.
- Update Thermal Only verifies that board/electrical inputs still match before
  reusing copper/component losses. Thermal grids/operators survive heat changes;
  cooling, conductivity, copper/stackup or backend changes rebuild the operator.
  Ambient-only changes shift the validated field exactly under this linear model.
- Refined electrical guesses respect layer and copper-island identity. Direct
  solves and easy AMG cases skip interpolation because measured preparation cost
  can exceed iteration savings. Thermal coarse-cell guesses reject paths crossing
  Edge.Cuts voids; residual, heat balance and spatial checks remain required.
- Copper overlays now use neutral grey outlines without fills, including holes.
  Extraction and immutable segment preparation run on a worker with its own IPC
  connection. Shared caches, one collection per layer and debounced blitting keep
  toggles/opacity responsive. Destroyed views drop late callbacks safely.
- The adaptive-meshing investigation is complete in ADAPTIVE_MESHING.md. No
  adaptive discretization is enabled: uniform stencils and contact mapping must
  be replaced and validated before mixed-size cells can produce reliable results.

Validation:

- 141 CPU-suite tests: 136 passed, five environment-dependent skips. Added native
  file workflow/atomic-write tests, snapshot and cache invalidation, safe refined
  starts, history corruption/scaling, Auto RAM and thermal operator reuse.
- Six actual CUDA backend tests passed. Explicit GPU calibration also passed.
- Real wx checks: configuration dialogs, Remove/Undo, production progress and
  cancellation, repeated-run mesh/operator/result reuse, changed-board and
  changed-electrical invalidation, thermal-only/ambient/cooling changes, asynchronous
  overlays, closed views and saved overlay figures.
- Live-board snapshot: 1,533 tracks, 306 footprints, 52 zones captured in 0.243 s;
  authoritative +12V geometry extraction found four copper layers in 0.443 s.
  Synthetic 2.08 W thermal UI fixture on the actual board preserved heat balance
  and peak/cursor/layer/opacity behavior (14,619 thermal cells).
- 263,169-node analytical benchmark: CPU changed-load retained solve 0.424 s versus
  fresh 1.954 s; GPU retained 0.199 s versus fresh 1.568 s. Maximum voltage
  differences were below 2e-12 V. These are controlled fixtures, not claimed
  speedups for the user's full PowerCreep operating point. Reports are
  benchmark-session-cache-cpu.json and benchmark-session-cache-gpu.json.

## 0.10.1 — Dynamic RAM admission and low-memory refinement

- Fixed false refusals caused by adding already-resident allocations to future
  allocations and comparing both against already-decreased system free RAM.
  Dynamic budgets preserve their mode through nested mesher/thermal checks and
  recalculate the allowance for future allocations. Manual caps count retained
  plus new run storage, with a separate physical-headroom guard for new storage.
- Shared host buffers are counted once across meshes, prepared groups and result
  views. Device arrays are excluded from host RAM accounting; CUDA retains its
  independent free-VRAM checks. Factor/hierarchy growth is reflected in cache
  accounting rather than freezing the size at first insertion.
- Preflight subtracts compatible already-resident mesh/operator storage from the
  phase peak and reserves work vectors for warm solves. Unused caches are evicted
  before retrying, while the selected pass's useful cached meshes are retained.
- Under refinement pressure, coarse mesh/voltage arrays can be discarded. All
  coarse voltage-drop, copper-loss, powered-net and series-component comparison
  inputs remain present. Warm interpolation is then skipped. The refined mesh,
  residual validation and 5% spatial comparison retain their original settings.
- Errors now distinguish coarse/refined electrical and thermal passes, additional
  versus retained RAM, current physical RAM and Dynamic/Manual allowance. Spacing
  suggestions use the fresh-grid estimate and account for the halved refinement
  step. Requested spacing is never changed automatically.
- The reported 58.01 GB versus 18.45 GB at 0.025 mm does not prove that the exact
  configuration will fit after this fix. If convergence is enabled, an additional
  0.0125 mm pass is requested. A genuinely oversized pass is still refused before
  allocation; a corrected estimate is needed from the actual rerun.

Validation: 159 CPU-suite tests (154 passed, five environment skips), plus 21
managed-runtime CUDA/cache/accounting tests (including all six CUDA checks).
`check_dynamic_ram_ui.py` models large native allocations without consuming GiB
buffers: fresh coarse/refined/thermal admission with shrinking free RAM, compatible
changed-load cache reuse, coarse-array release followed by successful fine solve,
and a true oversized refinement with actionable error and unchanged spacing.
Production progress/cancellation and general reuse UI checks also passed.

## 0.10.2 — regulator dissipation enters the thermal solve

- Previously, efficiency increased the electrical input current but regulator
  device heat was omitted from the thermal RHS. Each regulator now records local
  input voltage/current, output voltage/current and `loss = Pin - Pout` once on
  its input rail. Series-component results remain a separate API/list.
- Linear loss uses the solved local input voltage, not nominal upstream voltage:
  `(Vin_local - Vout) * Iout`, with quiescent current still omitted. Switching loss
  uses the final full-network operating point and configured efficiency. Negative
  dissipation beyond numerical tolerance rejects an inconsistent model.
- Device heat is deposited at the configured footprint centre. Using the same
  actual regulator reference for input and output places all device heat there;
  distinct input/output references split heat equally between their centres.
  Missing or off-board centres fall back to mapped terminal contacts. These are
  board heat-source approximations, not package/junction or thermal-pad models.
- The results summary shows each regulator's input/output powers, device heat and
  placement, plus total regulator loss. Thermal totals separate copper, series
  and regulator watts and verify that deposition conserves their sum.
- Electrical mesh comparison also checks regulator input current and loss. The
  low-memory coarse reference retains these records. Cached electrical solves
  recompute loss when current/efficiency changes; ambient-only thermal reuse keeps
  the same heat-source breakdown.

Validation: 166 CPU-suite tests (161 passed, five environment skips). Seven new
tests cover linear/switching energy balance, unity efficiency/no load, invalid
linear boost, cached efficiency changes, temperature rise and watt conservation,
body/fallback/split placement, duplicate records and refined loss comparison.
Real wx production checks verify 6.9 W linear heat (7.1 W including copper),
1.25 W switching heat at 5 W output/80% efficiency, heat balance, summary display
and cached ambient shifts. Production coarse/refined progress checks also pass.
