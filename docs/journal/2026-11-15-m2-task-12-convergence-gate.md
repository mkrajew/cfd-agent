# 2026-11-15 M2 Task 12: Solver Runner convergence gate

## 2026-11-15 decision: the gate is an executor op, a failed gate of a run that happened is not retried, and the diagnosis is one structured call

Implements ADR-0006 decisions 6-8, test-first. The gate (`tools/convergence.py`) runs inside the container as the executor op `convergence_report`; the host Solver Runner stage asks for it after an ok run. `periodic_outputs` is a required `CaseSpec` field, because the strict-schema test forbids defaults. A failed gate of a run that did happen is not retried by the Solver Runner: one structured diagnosis call (`SolverDiagnosis`, through the new `Stage.diagnosis_schema` and `take_diagnosis` hooks) routes the failure, and an invalid reply defaults to Case Setup with the gate errors as the instruction. The free-text diagnosis turn stays only for "the solver was not run".

## 2026-11-15 observation: the first code review returned `changes` on five points

The `code-reviewer` found a `max_length` in the strict diagnosis schema, a report-less executor answer treated as a gate failure, a steady window measured relative to the table instead of the run, a plan that did not tell Case Setup about periodic outputs, and a cavity-re100 fixture that declared a steady plan. All five were fixed in round 1.

## 2026-11-15 experiment: gate result of the five hand-written calibrations

Run in the `cfdagent:dev` image with `scripts/run_calibration.py --gate`, OpenFOAM v2512, one run each, with the thresholds of ADR-0006 (`final_residual_max` 1e-4, `steady_window` 0.1, `steady_rel_change` 1e-3, `min_periods` 10, `periodic_rel_change` 0.02). **All five calibrations are laminar**; the thresholds are validated for laminar flow only.

| calibration | solver | plan (time_dependence / periodic_outputs) | Scorer | gate | evidence |
|---|---|---|---|---|---|
| cavity-re100 | icoFoam, 40 s, 8000 steps | steady / false | PASS | FAIL as first run, PASS after the owner's decision below | no stopping criterion and no monitored quantity in the case; final initial residuals Ux 9e-12, Uy 1e-11, p 2e-9 |
| cavity-re1000 | simpleFoam | steady / false | PASS | PASS | `residualControl` stopped the run at iteration 1094 |
| pipe-re500 | simpleFoam (wedge) | steady / false | PASS | PASS | `residualControl` at iteration 135 |
| cylinder-re20 | simpleFoam | steady / false | PASS | PASS | `residualControl` at iteration 692 |
| cylinder-re100 | pimpleFoam, 4 ranks, 30 s | transient / true | PASS | PASS | Cl: 24.6 periods, f = 1.637, mean change 3.2e-6, amplitude change 3.9e-6 |

The cavity-re100 failure is genuine: the hand-written case predates decision 8 (icoFoam has no `residualControl`, and the case writes no time history), so the gate cannot show steadiness whatever the thresholds are. Nothing in the case was changed to fit the gate until the owner decided (next entries).

## 2026-11-15 experiment: margins of the monitored-change and periodicity branches

Monitored-change branch, on a scratch copy of cavity-re100 with one added `probes` function object (not committed, Scorer results unchanged): the largest relative change over the last tenth of the record was 1.5e-2 at a record end of 10 s, 1.8e-3 at 15 s, 1.9e-4 at 20 s and 8.7e-8 at 40 s (limit 1e-3); it passes from about 20 s. For cylinder-re20 (`residualControl` ignored on purpose) all 12 force-coefficient columns change by at most 2.6e-5, a margin of 38; with a window of 0.5 the largest change (Cl) is 1.0e-3, at the limit. Periodicity branch, on cylinder-re100: records cut at 8, 10 and 12 s fail (6.4, 8.2 and 9.8 periods in the second half; amplitude change 1.3e-1, 3.7e-2, 1.5e-3), records of 14 s and longer pass (11.5 periods; 24.6 at 30 s). The binding limit is `min_periods` (a record of about 13.5 s); once the second half is stationary the 2 % agreement is met by a factor of 100 or more. Strouhal number 0.1637 from f = 1.637 (D = 0.1 m, U = 1 m/s).

## 2026-11-15 decision: three rule changes required by `cfd-reviewer`, no threshold changed

`cfd-reviewer` confirmed all five thresholds on the evidence and required three rule changes, all implemented in fix round 2. (1) The swirl residual `Uz` of an axisymmetric case is not judged: the hand-written pipe case keeps `Uz` at 2.9e-2 at convergence while the other components are below 1e-6, so the monitored branch would reject a physically steady wedge run. (2) A periodic history must be written at least 20 times per period of its dominant oscillation: without the guard, white noise and an aliased 1.6 Hz signal passed. (3) `residualControl` counts only in the `SIMPLE` dictionary: in `PIMPLE` it ends only the outer loop, so the run goes on to `endTime` (checked by the reviewer on v2512). The sampling guard does not close the hole: white noise written at every step still passes in about 1 % of random trials (12 to 16 % without the guard) and lightly coloured noise in up to about 8 %, so the 2 % limit holds for clean laminar periodic regimes only. After round 2 both reviewers returned `approve`.

## 2026-11-15 decision: two extras beyond the plan text, accepted by the reviewers

A Planner gate rule rejects `periodic_outputs` together with a steady plan, and the sampling guard and the swirl exemption above go beyond the plan. Both reviewers accepted them; they are recorded in the ADR-0006 Amendments (proposed, awaiting the owner).

## 2026-11-15 obstacle: a unit test appeared to fail once during the first review

The failure was the author's own mutation check running in the same worktree, not a flaky test. The mutation check itself used 16 mutants of `tools/convergence.py`, and the unit tests caught all of them.

## 2026-11-15 milestone: Task 12 implemented, test-first, no live model call

Unit suite: 2425 passed, 143 skipped on Windows. Integration suite in the `cfdagent:dev` image after the last code change: 254 passed, 1 skipped. `ruff` and pre-commit are clean. Azure cost: zero. The gate is verified on five laminar hand-written solutions and on scripted and integration cases, not on a real model's output or on turbulent flow.

## 2026-11-15 decision: the owner has the hand-written cavity-re100 case monitored by a probe

The owner agreed in chat to the recommended option: one `probes` function object (two points, `p` and `U`, written every step) was added to `tests/fixtures/handwritten/cavity_re100/case/system/controlDict`, the monitor that decision 8 now demands of an agent. No threshold, no plan entry and no benchmark file changed; the scripted Case Setup fixture `case_setup_ok.json` was regenerated from the hand-written dictionaries, because a unit test keeps the two equal. Run through `scripts/run_calibration.py --gate` in the image, the committed case passes the gate (`monitored_change`, 8 monitored quantities, largest relative change 8.6e-08 over the last tenth of the run, final initial residuals 9e-12 to 2e-9) and the Scorer errors are unchanged (0.0049 and 0.0087, thresholds 0.05). All five merged calibrations now pass the gate.

## 2026-11-15 observation: still open for the owner

The ADR-0006 Amendments (the three rules made explicit, decision 8 amended to `SIMPLE` only) are confirmed by merging the pull request, together with the open points listed at the end of that section: a run ended by `residualControl` passes whatever the tolerances are; there is no spectral-concentration guard against noise (white noise at every step still passes in about 1 % of trials); one force-coefficient column is judged and no other mean is compared; a periodic flow without a body can never pass; the diagnosis routes only to Case Setup or Mesh. Nothing in the gate has been exercised on a turbulent or a real-model run.
