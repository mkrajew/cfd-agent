# 2026-11-14 M2 Task 2: time-series helpers for force histories

## 2026-11-14 milestone: function-object readers and spectral helpers added to `tools/foam_results.py`

`FunctionObjectTable`, `read_function_object`, `dominant_frequency` and `period_average` are implemented, with 61 unit tests and integration tests on real pimpleFoam output (ESI v2512). The integration tests cover the formats observed there: `force.dat` and `moment.dat` with flat headers, `coefficient.dat` with names such as `Cd(f)`, and probe files with vector values and a `# Time 0 1` header. Review is in PR 78. Azure cost was zero.

## 2026-11-14 decision: reader and estimator rules

Header rule: the last `#` line starting with `Time` names the columns; vector columns expand by 3, 6 or 9 values per name, and mixed scalar and vector headers are resolved from token widths. Restart join: the later start-time directory wins from its first time, rewinds count as restarts, and header-only files are skipped. `dominant_frequency` skips the first half of the record, applies a Hann window, zero-pads 8 times and refines the peak with a log-parabola fit; frequencies below one cycle per record are excluded and the 3-period criterion is checked on the estimated frequency. Measured error was at most 0.11 % at about 3 periods. `period_average` uses the trapezoid rule on uneven steps and floors the period count with a 1e-9 tolerance.

## 2026-11-14 observation: review found a start-up bias and a wrong-harmonic trap

The first test did not tell apart dropping the first part of the record from dropping the last; fixed in the one round of fixes. `cfd-reviewer` found that the default `period_average(n_periods=None)` averages the start-up transient, which gave a +5.9 % bias in a synthetic case. The analyst prompt now tells the agent to pass `n_periods` and to average the preceding window as a drift check. Force components can oscillate at a multiple of the shedding frequency (Cd at 2f, Cl at f), so the prompt also tells the agent to take the fundamental from the transverse component. The first version rejected the mixed scalar and vector header that `surfaceFieldValue` writes; the fix round resolves it from token widths.

## 2026-11-14 decision: known limitations deferred

Not done in this task: linear detrending (a drifting signal gives a misleading "simulate longer" error), a Nyquist or aliasing check, handling of re-run `_<startTime>` files in v2512 (`force_0.dat` next to `force.dat` is silently stale), the 6-significant-digit Time column that can create repeated times at small time steps, and legacy OpenFOAM Foundation header forms.

## 2026-11-14 observation: second review approved, remaining limitations

The second review (`code-reviewer`) approved the branch after checking each fix with mutations and a real `surfaceFieldValue` run in the image. Limitations it recorded for later: `dominant_frequency` returns a confident but meaningless frequency for round-off-level noise (the constant-signal check is relative to the signal magnitude), so the analyst must report the oscillation amplitude next to the frequency; the real `surfaceFieldValue` header is covered by a unit fixture only, not by an integration test; the prompt warnings have no substring guard in the drift test; float32 scalars are rejected as arguments; a NaN time silently drops the rows before it; symlink rejection has no test.
