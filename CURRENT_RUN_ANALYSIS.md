# KiPIDA current-run performance analysis — 14 October 2026

The active run was inspected without stopping it or changing its solver settings.
No production code or installed plugin files were changed for this analysis.
`py-spy` was installed only in the development environment for read-only sampling.

## Observed run

- KiPIDA process: 39120, installed release 0.8.5.
- Configured electrical spacing: 0.1 mm, with refinement enabled. The sampled
  stack is in the half-spacing electrical pass (0.05 mm), before thermal analysis.
- Reduced conductance matrix: **4,112,165 free voltage unknowns**. The assembled
  mesh has 4,112,176 nodes and 11 connected components.
- Backend: **CUDA CG**, not CPU fallback. RTX 3080 Ti snapshot: **84% GPU
  utilization**, 5,758 MiB used out of 12,288 MiB, power draw about 322 W.
- The outer regulator loop is on `iteration=18` (the **19th network solve**),
  with one outgoing regulator input current and two passive series links.
- At the scalar snapshot, the inner CG solver was at **6,085 iterations**, with
  a maximum of 24,334 and relative residual tolerance `1e-10`.
- Current regulator input: **4.262973270422416 A**. Last computed target:
  **4.262973291594054 A**. Difference: **2.12e-8 A**, or 21.2 nanoamps. The last
  input-terminal voltage is **11.984692850565896 V**.

The target in this snapshot is from the preceding completed regulator update;
the next target is computed after the currently active full-grid solve finishes.
The trace demonstrates tiny late-stage updates triggering another full solve.

## What the repetition means

Switching-regulator input power depends on its actual input voltage. The code
therefore solves the upstream network, recomputes `Iin=Pout/(efficiency*Vin)`,
then repeats. This physical coupling is necessary. Reconstructing the entire
network on every small current update is unnecessary: the board geometry,
conductances, passive links and voltage-source boundaries do not change during
this loop. Only current-load values change.

The implementation permits 40 passes, halves each current update, and stops at
`abs(old-new) <= 1e-10 + 1e-9*abs(new)`. For this 4.263 A current the stopping
threshold is approximately **4.36 nanoamps**. The observed remaining mismatch
is only about five times that threshold, but still triggers expensive solves.

## Sampled bottlenecks

Thirty seconds were sampled at 50 Hz with nonblocking process reads. There
were 1,482 successful samples and six failed reads. These percentages describe
the sampled window, not the entire run or a CUDA kernel timing profile.

| Work | Share of Python stack samples | Interpretation |
|---|---:|---|
| Sparse GPU backend solve | about 61.5% | Thousands of CG steps repeated for each regulator update |
| Restamping copper matrix entries | about 28.4% | Python `array.extend` of NumPy values on unchanged copper edges |
| Other assembly, validation, mapping and logging | remaining approximately 10% | Additional work repeated around each solve |

The GPU is genuinely active. Enabling GPU again, changing thermal grid spacing,
or adding more RAM does not address the sampled bottleneck. This sample does
not establish the performance of the later thermal phase.

## Recommended changes, in priority order

1. **Prepare each network once per mesh resolution.** Cache terminal mappings,
   ideal-contact contraction, the copper matrix, connectivity, fixed/free node
   masks and reduced matrix. Subsequent regulator passes only update the RHS.
   Keep the GPU CSR matrix and diagonal/preconditioner allocated for this loop
   instead of uploading and freeing them each pass. Invalidate these caches
   when geometry, spacing, links or voltage-source boundaries change. This
   eliminates most of the measured repeated stamping work.

2. **Warm-start CG.** Pass the previous voltage-deviation solution as `x0`.
   The current CuPy call omits it and starts over, even for nanoamp changes.
   Keep the same fixed/free node ordering and baseline voltage convention.
   Preserve all residual/current-balance checks. CuPy explicitly supports both
   `x0` and a preconditioner `M`: [CG API](https://docs.cupy.dev/en/stable/reference/generated/cupyx.scipy.sparse.linalg.cg.html).
   The expected benefit is substantial late in the regulator loop; it needs
   validation on this board rather than an assumed iteration-count reduction.

3. **Replace the regulator's expensive full-grid fixed-point loop with a small
   port model.** For fixed linear copper, regulator terminal voltage is an
   affine function of its input currents. Solve for the base voltage and one
   unit-current response per regulator; then solve the constant-power equations
   on that small system. This run has only one changing regulator input. Its
   reduced problem can use a Thevenin quadratic with the physically appropriate
   high-voltage solution, while detecting insufficient supply/no real solution.
   Multiple regulators require a coupled small nonlinear solve with positivity
   and convergence safeguards. Reconstruct and validate the final full-grid
   voltage solution. This removes most repeated multi-million-node solves.

4. **Make regulator convergence accuracy meaningful.** The present 1e-9 relative
   current target and unconditional half damping prolong late-stage convergence.
   Evaluate an error budget based on regulator power consistency and changes in
   rail drop/copper loss, initially testing a 1e-6 relative outer-current target.
   Do not equate the 5% mesh refinement criterion with a universal accuracy bound,
   or weaken linear residual/current-balance checks indiscriminately. Use adaptive
   damping or a safeguarded Newton update if retaining the full-grid loop.

5. **Improve preconditioning for the first large solve.** The existing
   preconditioner is only inverse-diagonal (Jacobi). Benchmark symmetric
   multigrid or another SPD-compatible preconditioner against the real board.
   A CPU AMG prototype can be built with [PyAMG](https://pyamg.readthedocs.io/en/latest/generated/pyamg.multilevel.html),
   but a CPU AMG callback inside every GPU iteration would introduce transfers
   and should not be treated as a GPU solution. Measure setup time, reuse across
   RHS updates and memory consumption as well as iteration count.

6. **Expose the nested progress.** Log rail/network, coarse/refined pass, outer
   regulator pass, inner CG count, input-current change and stopping target.
   Report setup/assembly/transfer/solve/validation times separately. Currently
   restarting the inner iteration counter looks like repeating the same stage
   without useful progress context. The overall percentage cannot predict the
   remaining duration of these nested iterations.

The first implementation should combine matrix reuse and warm starts, then
benchmark the reduced regulator-port model. Increasing the iteration cap alone
would usually make the wait longer and would not remove this redundancy.

## Evidence files

- `current-run-profile.json`: nonblocking sampled stacks, speedscope format.
- `current-run-stack.txt`: call stack and local-state snapshot. This is local
  diagnostic data and contains board terminal/net identifiers.
- Relevant implementation: `network_solver.py` (`solve_system`, `solve_group`,
  `stamp_edges`), `electrical_solver.py` (matrix preparation), `solver_backend.py`
  (zero-start CG, Jacobi preconditioner and allocation cleanup).
