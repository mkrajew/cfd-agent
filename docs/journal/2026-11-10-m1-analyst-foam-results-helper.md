# 2026-11-10 M1 execution: a tested results reader for the Analyst (foam_results)

Nothing in this file was compared with reference data. The helper has been verified against OpenFOAM's own utilities and PyVista on test meshes only; it has not yet been used by the live model.

## 2026-11-10 observation: the Analyst was the farthest stage in live runs 15, 18 and 20

In runs 15, 18 and 20 the Analyst was the stage the pipeline reached furthest. In run 20 its `post.py` crashed twice: once on a PyVista file-reader error on the case, once on an error in the model's own hand-written field parser. This is remedy candidate (I) of the 2026-11-10 blank-recovery journal entry: give the Analyst a tested way to read OpenFOAM results instead of letting it write a parser each time.

## 2026-11-10 decision: a pure Python/numpy reader module, not PyVista

Chosen: a tested module `cfdagent.tools.foam_results` in the sandbox image, a pure Python/numpy reader of the ASCII case, documented in the Analyst prompt with a placeholder skeleton. The Analyst still writes `quantities.json` and `analysis.md` itself; the helper only reads and samples. The ASCII format is guaranteed by the case-setup check (`writeFormat ascii`).

PyVista was rejected after checks in the image: `pv.read` on the case directory and on `controlDict` fails with "could not be read automatically". The OpenFOAM reader works only through an empty `*.foam` file in the case directory, which would mean writing into the case, and the gate forbids that for the run-directory case. In addition `cell_to_point_creation` has to be turned off to keep cell values, VTK holds 32-bit floats, and in pyvista 0.49.1 the reader has no attribute `case_type`, so decomposed cases are handled differently.

## 2026-11-10 milestone: foam_results implemented and verified

The module provides times, fields as numpy arrays, cell centres, cell volumes, face centres and face areas rebuilt from `polyMesh` with OpenFOAM's formulas, patch values (`zeroGradient` takes the adjacent cell, `noSlip` gives 0, `phi` on symmetry and wedge patches gives 0), line sampling (`linear` is the cell value plus a least-squares gradient, exact for linear fields; `nearest`), and a solver-log summary (longest run, residuals, iteration count). Points on a boundary face take the patch value.

Cell centres and volumes match `postProcess` (`writeCellCentres`, `writeCellVolumes`) within the 8-digit output precision, on a regular mesh and on a sheared, graded `blockMesh` mesh. Unit tests on the host: 1954 passed. Docker integration tests: 9 in `test_foam_results_openfoam.py` compare against OpenFOAM, PyVista's reader and a script run in a clean working directory; the whole integration suite gave 246 passed before the final fixes. Performance: 160k cells load in 2.5 s, a 1000-point line takes 3.5 s.

## 2026-11-10 experiment: mutation check of foam_results.py

About 30 mutants of `foam_results.py` were tried. Six survived the first round, which led to new tests (trapezoid centroid, triangle and pentagon face geometry, tetrahedron and prism, patch values in the gradient, log choice); in the final round all mutants were killed.

## 2026-11-10 observation: review findings fixed, minors deferred

`cfd-reviewer` found that end points of a line were a fit instead of the patch value; fixed. Other findings fixed: the skeleton's steadiness check compared with time 0, `phi` raised on symmetry-type patches, `value $internalField` broke parsing, mesh symlinks, zlib errors, and the choice of solver log. Deferred minors: ambiguous snapping at corners between two valued patches, a stale value on a `zeroGradient` patch, a swallowed malformed patch value, and cyclic `phi` raising in the skeleton.

## 2026-11-10 milestone: not yet exercised by the live model

The helper has not been used by the live model. After the owner merges and the image is published, the parent session re-runs the live series to see whether the Analyst stage now completes. Azure cost of this work: none.
