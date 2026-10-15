# Ki-PIDA (KiCad Power Integrity & Delivery Analyzer)

Ki-PIDA is a native KiCad plugin designed for Direct Current (DC) Power Integrity (PI) analysis. It allows PCB designers to simulate DC voltage drops (IR drop) and total copper power loss directly within the KiCad Pcbnew environment, eliminating the need for expensive proprietary tools or complex external workflows.

## 🚀 Why Ki-PIDA?

Modern electronics operate with tight voltage margins. An IR drop of just 30mV can lead to system instability in sub-1.0V Socs. High current densities also pose thermal risks and reliability hazards like electromigration. 

Ki-PIDA democratizes high-end PI analysis by:
- **Ensuring Stability:** Detect voltage violations at the layout stage.
- **Reducing Iterations:** Identify "neck-down" regions and hotspots before prototyping.
- **Seamless Workflow:** Interactive layout-driven analysis without leaving KiCad.

## ✨ Key Features

- **Native Integration:** Built for KiCad 9.0+ using the Python Scripting API.
- **Power Tree Management:** Auto-discover power rails and manage complex hierarchies including VRM efficiency modeling.
- **Hybrid 2.5D Solver:** Fast and accurate simulation using an optimized resistive mesh approach.
- **Electrical Validation:** Source/load balance, residual checks, and mesh refinement. Current-density maps are not yet implemented.
- **Experimental Thermal Estimate:** Steady-state board-average temperatures from copper, series-component and regulator losses, with adjustable ambient, cooling and substrate conductivity. See the model assumptions in [TESTING.md](TESTING.md#experimental-thermal-analysis-080).
- **Multi-Rail Analysis:** Simulate complex power trees with nested regulators (Buck, LDO) and enforce correct dependency solving.
- **Project Persistence:** Automatically saves your power tree configuration (sources, loads, regulators) in the project directory, so you don't lose your setup.
- **Visual Feedback:** Voltage plots per power rail, with total copper loss and mesh-convergence status.
- **Large Meshes:** Compact array storage and RAM preflight replace the fixed two-million-grid-point cap. Optional CUDA accelerates the sparse electrical solve.
- **Reusable Solves:** Cached matrices, warm starts and optional CPU/GPU multigrid. Switching-regulator coupling uses a small terminal-response model instead of repeatedly rebuilding the full mesh.

## 📦 Installation

Ki-PIDA is designed to run within the KiCad 9.0+ environment. Follow these steps to install and enable the plugin:

### 1. Enable the KiCad API
Ki-PIDA communicates with KiCad via the new IPC-based API.
1. Open KiCad.
2. Go to **Preferences** > **Common**.
3. Under the **API** section, check the box for **Enable API**.
4. Restart KiCad if prompted.
![alt text](image-3.png)

### 2. Install the Plugin

Use KiCad 9.0.1 or newer. In **Plugin and Content Manager > Install from File**,
select `KiPIDA-PCM.zip` from the shared parent folder, then restart KiCad.
The archive has root `metadata.json`, IPC runtime selection, and the required
`plugins/` layout. The old `KiPIDA-fixed.zip` filename now contains the same PCM package.

For a manual install, copy this source folder (without `.venv`, caches, backups,
and development tooling) into a subfolder of KiCad's IPC plugin directory.
On Windows this is normally `Documents/KiCad/<version>/plugins`, rather than
`AppData/Roaming/kicad/<version>/plugins` used by older plugin workflows.
See the [official IPC plugin documentation](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/).

The plugin runtime needs the libraries listed in `requirements.txt`, including
wxPython. The `.venv` in this shared folder is an isolated test environment, not
KiCad's plugin runtime. See [TESTING.md](TESTING.md) for the test procedure.

Version 0.7.0 adds **Solver: Auto / CPU / GPU (CUDA)** and a **RAM budget (GB)**
control. CUDA dependencies are optional in `requirements-gpu.txt`; they are
already installed in this user's KiCad 9 runtime. See [large-mesh setup and
benchmark results](TESTING.md#large-meshes-and-optional-cuda-070). Geometry
meshing remains on CPU. Save Config and reopen KiPIDA after updating.

---

## 📖 Tutorial: Your First IR Drop Analysis

Follow these steps to perform a DC Power Integrity analysis on your board.

### 1. Launch the Plugin
Open your PCB layout in KiCad Pcbnew and click the **Ki-PIDA** icon in the top toolbar to open the analyzer.

### 2. Review Discovered Power Rails
- **Auto-Discovery:** Upon launch, Ki-PIDA scans your board for power rails and attempts to load any existing configuration from `kipida_config.json` in your project folder.
- **Add Roots:** Identify your main input rails (e.g., `+12V_IN`, `VBUS`).
- **Define Regulators:** Use the **+ Regulator** button to create relationships between rails (e.g., `12V -> 5V`). Ki-PIDA supports:
    - **Linear Regulators (LDOs):** Pass current 1:1 from input to output.
    - **Switching Regulators (Buck/Boost):** Conserve power based on efficiency (e.g., 90%).
    - **Multi-Output Support:** Handle PMICs where one component drives multiple output rails.

![alt text](image-4.png)

### 3. Add Sources (VRMs / Power Inputs)
Identify where power enters this net:
1. Click **+ Source**.
2. Select the source component (e.g., a regulator `U1` or connector `J1`).
3. In the dialog, check the **Pads** that are connected to the power net.
4. Click **OK**.

### 4. Add Loads (Integrated Circuits / Sinks)
Identify the components consuming power:
1. Click **+ Load**.
2. Select the sink component (e.g., MCU `U2` or FPGA `U3`).
3. Enter the **Total Current (A)** consumed by this component (e.g., `0.5` for 500mA).
4. Check the **Pads** through which the current is drawn.
5. Click **OK**.

![alt text](image-5.png)

### 5. Run the Simulation
Before running, you can adjust the **Mesh Resolution (mm)**. Start at `0.1 mm` and leave **Check mesh convergence** enabled. Narrow traces and annular rings may require finer spacing. The check repeats the solve at half spacing and rejects changes above 5% in voltage drop or total copper loss. It does not establish convergence of local peak current density.
- Click **Run Simulation**.
- The solver analyzes the power tree topology to determine the correct solution order (Leaf-to-Root) for current propagation.
- Example: For a `12V -> 5V -> 3.3V` chain, it solves 3.3V first, applies that load to the 5V rail, solves 5V, and finally solves the 12V input.

### 6. Analyze Results
Once "Simulation Success" appears, the UI will jump to the **Results** tab.

- **Rail Selection:** A tab will be created for each power rail in your system.
- **Per-Rail Visualization:** Inside each rail's tab, you can view:
    - **3D View:** A 3D voltage plot of the entire net.
    - **Layer Views:** Individual 2D heatmaps for every layer containing copper for that net.

> [!TIP]
> Use the **Enable Debug Log** checkbox if you encounter issues during meshing or solving to see more detail in the Log tab.

## 🛠️ Technical Overview (For Developers)

Ki-PIDA is built on a modular architecture designed for performance and maintainability.

### Architecture
- **Extractor (`extractor.py`):** Interfaces with the KiCad API to pull filled zone geometry, track layouts, and physical stackup data.
- **Mesher (`mesh.py`):** Discretizes continuous copper geometry into a 2D/3D resistive grid (Rasterization).
- **Solver (`solver.py`):** Uses a conductance matrix with voltage-boundary elimination and SciPy sparse solving to solve the electrical system.
- **Plotter (`plotter.py`):** Generates voltage plots, displayed as images in the plugin dialog.

### Methodology
The tool utilizes a **Hybrid 2.5D Finite Difference Method (FDM)**. It represents PCB layers as 2D grids of resistors connected vertically by via/PTH resistor elements. This provides the ideal balance between the speed of a 2D solver and the accuracy of a full 3D FEM for planar PCB structures.

### Stack
- **Languages:** Python 3.9+
- **UI:** wxPython
- **Math:** NumPy & SciPy
- **Geometry:** Shapely

## � Current State (Alpha)

As of the current version, Ki-PIDA implements a functional end-to-end pipeline for DC IR drop analysis.

### Capabilities:
- **Comprehensive Extraction:** Extracts tracks, pads, and filled zones (respecting thermal reliefs and voids) from KiCad 9.0+ boards.
- **3D Meshing Engine:** Converts geometry into a resistive mesh across multiple layers, correctly modeling via and PTH conductances.
- **Robust Linear Solver:** Solves the circuit using SciPy's sparse matrix backend. Includes island detection to warn about floating sections of copper that could cause numerical issues.
- **Automated Diagnostics:** Detects isolated copper nodes and disjoint electrical islands during the solve phase.

### User Experience:
- **Automated Rail Discovery:** Instantly find power nets based on zone connectivity.
- **Granular Control:** Assign sources and loads down to the individual pad level.
- **In-Memory Visualization:** Instant generation of color-coded heatmaps to inspect voltage distribution without exporting files.

## �🗺️ Roadmap

- **Phase 1 (Current):** Validated DC IR drop, total copper loss, and power tree UI.
- **Next:** Current-density and local-loss maps, followed by board-wide thermal prediction.
- **Phase 2:** AC Impedance Analysis ($Z$ vs Frequency) and decoupling capacitor optimization.
- **Phase 3:** Full 3D Thermal modeling with airflow convection.

### TODOs completed in 0.10.0

- [x] Eliminate UI lag when activating the **PCB copper overlay**. Profile copper
  extraction, overlay preparation and rendering; cache reusable geometry and
  display data, and move expensive preparation off the UI thread where safe.
  Keep activation, layer selection and transparency controls responsive.
- [x] Add **grey outlines only** for the PCB copper overlay, drawing copper
  boundaries without coloured fills so the overlay does not interfere with the
  temperature scale. Retain layer selection and adjustable outline opacity.
- [x] Add an **Auto / Dynamic RAM** budget option alongside the manual GB limit.
  Determine the budget from currently available RAM while reserving headroom for
  KiCad, the OS and other applications. Recheck before large allocations and
  account for retained meshes, refinement and cached solver data. Display the
  effective budget; manage cache retention and batching within it, and report
  when the requested mesh cannot fit rather than silently changing accuracy.
- [x] Add **Save Config As…** and **Open Config…** with native file explorer dialogs,
  allowing multiple named `.kipida.json` configurations to be saved and opened
  instead of using only the fixed project configuration file.
- [x] Estimate simulation duration and show remaining time alongside progress.
  Record timings for meshing, solver setup/iterations, refinement and thermal
  analysis from previous runs, including mesh size, spacing, backend/hardware
  and relevant settings. Use comparable runs to estimate subsequent simulations;
  offer a short separate calibration benchmark when history is insufficient.
  Update the estimate as stages complete and show an approximate range when
  confidence is low, since geometry and solver convergence affect runtime.

### Prioritized simulation speedups completed in 0.10.0

Keep Python for implementation and KiCad integration. Measure representative
run timings before and after each change; retain electrical, thermal and mesh
convergence validation. Apply the RAM budget to all retained cache data.

- [x] **Priority 1 — Cache geometry and meshes between runs.** Reuse extraction
  and coarse/refined meshes when only loads change. Invalidate affected entries
  when PCB geometry, stackup, mesh spacing or relevant meshing settings change.
- [x] **Priority 2 — Retain prepared solvers between runs.** Reuse matrix
  factors, preconditioners, GPU allocations and previous solutions when the
  matrix and source boundary mapping remain compatible. Rebuild affected solver
  state when topology, conductance, boundary mapping or backend changes.
- [x] **Priority 3 — Rerun thermal analysis independently.** Reuse electrical
  losses when only thermal settings change; reuse thermal matrices when only
  heat inputs change. Under the current linear model, an ambient-only change
  can shift the temperature field without another solve.
- [x] **Priority 4 — Warm-start refined solves from coarse results.** Interpolate
  voltage and temperature fields onto the refined meshes, respecting disconnected
  regions and electrical boundaries. Keep residual and convergence checks.
  Skip interpolation for direct solves and easy AMG cases where setup costs
  outweigh the likely iteration savings.
- [x] **Priority 5 — Reuse validated convergence results.** Skip repeated
  coarse/refined comparisons for identical board/configuration/settings inputs.
  Invalidate the result when changes affect current paths or heat distribution.
- [x] **Priority 6 — Investigate adaptive meshing.** Refine narrow traces, pads,
  vias and regions with large solution gradients while using larger cells in
  broad, smooth regions. Treat this as a larger mesher redesign and verify
  connectivity, conservation and convergence against the uniform-grid solver.
  Investigation and acceptance fixtures are in [ADAPTIVE_MESHING.md](ADAPTIVE_MESHING.md).
  Adaptive meshing is a future implementation; 0.10.0 retains the validated
  uniform-grid model.

Use **Save As...** / **Open Config...** in the rail panel. RAM defaults to
**Auto / Dynamic**; select **Manual** to enter a GB budget. The Log tab shows
an approximate remaining-time range once history or **Calibrate timing** is
available. **Update Thermal Only** reuses electrical results after verifying
that the board and electrical settings are unchanged. Caches are local to the
open KiPIDA dialog and are released when it closes.

In 0.10.1, Dynamic's displayed allowance applies to **new allocations** using
currently free physical RAM. Retained meshes/operators are counted once and
already reduce that free RAM. Manual budgets cap retained plus new run memory.
Under pressure, unused caches and optional coarse warm-start arrays are released;
the convergence statistics and requested spacing are preserved. If a pass still
cannot fit, its error identifies the actual spacing and suggests a larger one.

## Electrical accuracy fixes and testing

See [TESTING.md](TESTING.md) for changes, validation coverage, and the KiCad test procedure.

## Stitching rails through components

Version 0.6.0 adds **+ Series Component** for fuses, resistors, connectors and
closed switches. Choose two pads and a resistance in milliohms to solve their
nets together. Component current, voltage drop and Joule loss appear in Results.
See TESTING.md for setup and model limits.
