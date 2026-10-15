# Adaptive meshing investigation

The current electrical discretization uses a uniform spacing on each net. Refining
that spacing by a factor of two produces approximately four times as many in-plane
unknowns. The most promising next mesher would therefore retain larger cells in
broad copper areas and subdivide around narrow traces, pad edges, via annuli and
large voltage gradients.

This release keeps the existing validated discretization. Simply changing cell
spacing locally would change resistance incorrectly: the current stamps, pad
lookup, distributed barrel contacts, plotting gradients and refinement comparison
assume a uniform grid. The investigation below defines the required replacement
and acceptance criteria before an adaptive option is exposed in the UI.

## Proposed finite-volume design

1. Partition each copper layer using a balanced quadtree, with neighboring cells
   differing by at most one level. Clip cells to copper polygons and holes. Keep
   distinct conductive portions of a cell separate; a cell must not bridge a slot.
2. Assign voltage unknowns to conductive cell centers. For each shared conductive
   face, stamp a symmetric conductance equal to copper conductivity times copper
   thickness times shared face length divided by center-to-center distance. Split
   hanging faces into individual conservative contacts. The resulting matrix must
   remain symmetric with zero row sums before boundary elimination.
3. Integrate pad and via contact regions over their intersecting cells. Preserve
   equipotential terminal contraction, total cylindrical barrel conductance and
   source/load current exactly when contact cells are subdivided.
4. First refine for geometry (narrow widths, slots, pad edges and annuli), then use
   local flux/energy indicators after a solve to refine high-error regions. Cap
   refinement by the same dynamic RAM budget and preserve a minimum physical
   feature size. Do not rely on temperature or voltage extrema alone as an error
   estimator.
5. Keep the electrical solver's residual, current balance and regulator operating
   point validation. Compare independently refined adaptive meshes for voltage
   drop, copper loss, series current and loss, and the set of powered islands.
   A small sparse residual does not establish spatial accuracy.
6. Adapt plotting to cell polygons and face currents instead of assuming one
   `grid_step`. Thermal heat transfer must integrate each electrical cell's loss
   into thermal cells while conserving total watts.

## Acceptance fixtures and measurements

| Fixture | Required result |
| --- | --- |
| Uniform rectangular conductor | Resistance approaches length / (conductivity × thickness × width); compare with a fine uniform reference. |
| Narrow neck between wide pours | Voltage drop and integrated loss agree with a converged uniform reference; report cell reduction and total elapsed time. |
| Diagonal trace, curved edge and polygon hole | No false connection across missing copper; current conservation at cut and hanging faces. |
| Separate islands within one large cell | Unpowered islands remain excluded; loads on them fail rather than borrow a nearby voltage. |
| Distributed through-hole and via annulus | Preserve analytical barrel resistance, contact symmetry and total current as the cell partition changes. |
| Zero-ohm fuse and nonzero series resistor | Preserve equipotential contacts, component current and component loss. |
| Multiple regulator branches | Match the uniform model's validated operating point and reject voltage collapse identically. |
| Electrical-to-thermal heat mapping | Preserve total heat and compare peak temperature and thermal energy balance with independent refinement. |

Record geometry/assembly/setup/iteration/validation/plotting times separately,
along with retained RAM, peak RAM and VRAM. Accept an adaptive implementation only
when it improves end-to-end performance at comparable error, including preparation
and refinement costs. Until these fixtures pass, session mesh/operator caching and
conservative refined warm starts offer speedups without replacing the physical
discretization.
