# 2026-11-15 M2 Task 4: calibration of `pipe-re500`

## 2026-11-15 milestone: hand-written steady solution for `pipe-re500` reproduces the Hagen-Poiseuille reference

A hand-written solution (`tests/fixtures/handwritten/pipe_re500/`: `geometry.py` in CadQuery, `mesh.py` in Gmsh, `case/` for `simpleFoam`, `post.py` using `foam_results`) and `tests/calibration/test_pipe_re500.py` (two tests, red first because the fixture directory was missing, then green) check that the benchmark thresholds are reachable. The mesh is 5000 cells (250 axial x 20 radial). The run ended after 135 SIMPLE iterations through `residualControl`; total wall time was about 10 s (solver 2.4 s) on the laptop, 1 rank, in the `cfdagent:dev` image. The profile error is 0.0036 against a threshold of 0.03 (normalised by 2 U_mean), and the pressure drop is 1.607 Pa against the reference 1.6 Pa (error 0.0046, threshold 0.05). The reference is the analytic Hagen-Poiseuille solution, so this is a comparison with reference data, not only physical checks. Azure cost was zero.

## 2026-11-15 decision: axisymmetric wedge instead of a 3D pipe

The case is a 5 degree wedge, one flat-faced cell through the angle, symmetric about the xy plane, with the axis along x and prisms next to the axis. `cfd-reviewer` confirmed the wedge over 3D: a 3D mesh would need about 100x the cells and add no information for a laminar axisymmetric flow. The 3D variant is untested. Pressure: the solver gives kinematic p, so `post.py` multiplies by rho = 1000 (from the task) and area-averages with weight r. The profile is normalised by the mesh wall radius R cos(a/2), so r/R spans exactly 0 to 1.

## 2026-11-15 experiment: mesh independence

Profile error / pressure drop in Pa: 125x10 0.0097 / 1.601; 250x10 0.0097 / 1.602; 250x20 0.0036 / 1.607; 500x20 0.0036 / 1.607; 250x40 0.0014 / 1.609; 500x40 0.0015 / 1.609. Axial doubling changes nothing; the radial error falls about 2.5x per doubling. The pressure drop settles 0.6% above the analytic value: about 0.2% from the flat wall (R cos(a/2) instead of R) and about 0.4% from residual entrance development between x = 0.35 and 0.45 m.

## 2026-11-15 experiment: entrance-length check of the sampling plane

The centreline velocity reaches 99% of 2 U at x = 0.285 m (Langhaar estimate 0.0575 Re D = 0.2875 m). u_c/U is 1.9918 at x = 0.35 m and 1.9978 at 0.45 m; dp/dx is -16.13 Pa/m at x = 0.3475 m and -16.04 Pa/m at 0.4475 m against -16.0 Pa/m fully developed. The flow is therefore fully developed at 0.35 m for the purposes of the benchmark (pressure-drop bias about 0.4% against a 5% threshold).

## 2026-11-15 obstacle: `residualControl` on U never converged in the wedge case

The normalised Uz residual is zero by symmetry (max |Uz| about 1e-15), so it is round-off noise near 1e-2 and never falls below the tolerance. The run went to the 20000-iteration cap (190 s) while Ux was at 3e-11, Uy at 3e-9 and p at 9e-11. Component keys (Ux, Uy) appeared to work (88 iterations), but `cfd-reviewer` showed that `residualControl` keys are field names and component keys are silently ignored, so only p stopped the run. The final setting is `p 1e-8` only, verified by solution invariance: tightening to 1e-9 or 1e-10 leaves the profile unchanged within 3e-6.

## 2026-11-15 obstacle: 1e-6 on p was too loose on finer meshes

At 500x40 the profile error was 0.0127 with 1e-6 and 0.0018 with 1e-8, so convergence at a fixed tolerance depends on the mesh. This is why the tolerance is 1e-8.

## 2026-11-15 observation: gates accepted the wedge without changes

The geometry gate and the mesh gate both accepted a wedge touching the axis (prisms) with no code change. The mesh gate tolerance (1% relative per axis) also makes the CAD box acceptable for the mesh box (ymax R against R cos(a/2)). The integration guard test `test_wedge_case_passes_geometry_and_mesh_gates` in `tests/integration/test_mesh.py` records this. An optional `expected_mesh_bbox` in `scripts/run_calibration.py` was built first and removed after `cfd-reviewer` pointed out it was unnecessary (stay in scope).

## 2026-11-15 observation: review rounds

Round 1: `cfd-reviewer` and `code-reviewer` both returned `changes` (the `residualControl` justification was false; the pressure-drop bias explanation was incomplete; one number was wrong, 1.8e-3 against 1.5e-3 depending on the sampling; the optional scope was to be removed). Round 2: both returned `approve`.

## 2026-11-15 observation: general lessons for later knowledge notes (Task 10)

Candidate general rules found in this task, not yet added to any knowledge note: `residualControl` keys are field names and component keys are silently ignored; in a wedge case the symmetric component residual is round-off noise and cannot be used as a stop criterion; verify convergence by solution invariance under tighter tolerances and confirm from the log which residual triggered "SIMPLE solution converged"; compare the sampling plane with the entrance length; the solver's kinematic p must be multiplied by rho for a pressure drop in Pa.
