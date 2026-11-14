# 2026-11-14 M2 Task 3: calibration of `cavity-re1000`

## 2026-11-14 milestone: hand-written steady solution for `cavity-re1000` reproduces the Ghia reference

A hand-written steady `simpleFoam` case (`tests/fixtures/handwritten/cavity_re1000`), the driver `scripts/run_calibration.py` and `tests/calibration` (new `calibration` pytest marker) check that the benchmark's acceptance threshold is reachable. The mesh is 128 x 128 x 1 = 16384 cells. The run ended after 1094 SIMPLE iterations through `residualControl` (p and U at 1e-6 on initial residuals; `endTime` 20000 is only a cap). Solver wall time was 13.5 s and total time 19.3 s on the laptop, which is a lower bound for the VM. Maximum errors against Ghia are 0.0054 for u (at y = 0.9531) and 0.0105 for v (at x = 0.9531), against a threshold of 0.05. `--n-procs 2` gives the same errors (1089 iterations). Azure cost was zero.

## 2026-11-14 decision: N = 128 chosen by self-convergence, not by lowest error against Ghia

Uniform mesh study (maximum error u / v): N = 64 0.0183 / 0.0158 (483 it), N = 96 0.0081 / 0.0047 (770), N = 128 0.0054 / 0.0105 (1094), N = 160 0.0069 / 0.0128 (1466), N = 256 0.0058 / 0.0162 (2723). The error against Ghia is not monotone because the Ghia 129 x 129 data is itself under-resolved at the extremes: Richardson-extrapolated extrema are u_min -0.3882, v_max 0.3766 and v_min -0.5268, against Ghia's -0.3829, 0.3710 and -0.5155. N = 128 was chosen because it differs from N = 256 by at most 0.004 (u) and 0.007 (v). A converged solution scores about 0.006 (u) and 0.017 (v), so the 0.05 threshold has about 3x headroom and no change to the benchmark criteria (D4) is needed. Sampling with 513 instead of 129 points changes the errors by at most 2e-5. The pass boundary is N = 48 (0.034 / 0.040, narrow pass); N = 40 fails (0.052 / 0.063).

## 2026-11-14 experiment: scheme and residual-tolerance sensitivity at N = 128

Convection scheme: first-order upwind fails (0.073 / 0.074), central differencing passes (0.003 / 0.0125). `residualControl` on p and U (profile shift measured against the 1e-6 run): 1e-3 gives 260 iterations, shift 3.7e-2, error 0.038 / 0.034 (nearly fails); 1e-4 gives 413 iterations, shift 5e-3; 1e-5 gives 658 iterations, shift 1e-3; 1e-8 gives 1976 iterations, shift 1.1e-4. Tutorial-style tolerances therefore nearly fail this benchmark, which is useful when interpreting agent failures later.

## 2026-11-14 observation: ESI v2512 and parallel-output details

In ESI v2512 `simpleFoam` reads `constant/turbulenceProperties` (not `momentumTransport`) with `simulationType laminar`. With `--n-procs 2` the sets output repeats the point on the processor interface (130 rows for 129 points), so `post.py` drops exact duplicate rows.

## 2026-11-14 observation: reviews found stale-import, mesh-value and comment errors

`cfd-reviewer` returned `changes` (wrong mesh value, one wrong comment, N-dependent numbers in comments), then `approve`. The first draft of the `fvSchemes` comment claimed central differencing was unusable; the reviewer measured otherwise and the comment was corrected. `code-reviewer` returned `changes` (the script imported the image's stale installed `cfdagent`; `--n-procs` greater than 1 failed without explanation), then `approve`. One fix round each.

## 2026-11-14 decision: minor items deferred

Not done in this task: tool exceptions in `run_calibration.py` escape as tracebacks instead of a FAILED STEP row; there are no tests for the `--n-procs` range check or for missing fixture files; the script does not warn when `residualControl` did not end the run (only the pytest test asserts it); `--keep` into a non-empty directory gives a traceback.
