# 2026-11-15 M2 Task 5: calibration of `cylinder-re20` and `cylinder-re100`

## 2026-11-15 milestone: hand-written solutions of both cylinder benchmarks pass the Scorer

Hand-written, LLM-free cases `tests/fixtures/handwritten/cylinder_re20` (steady `simpleFoam`, 1 rank) and `cylinder_re100` (transient `pimpleFoam`, 4 ranks) pass the Scorer, so both benchmarks are achievable with the project's tools (PR 84). Task 3's `run_calibration.py` was reused unchanged; nothing in `src/`, `benchmarks/` or `scripts/` changed. Verified in the image (OpenFOAM v2512, Gmsh 4.15.2): `pytest -m calibration tests/calibration/test_cylinder.py` gives 2 passed in 485 s; host suite 2146 passed, 140 skipped. `cfd-reviewer` and `code-reviewer` both returned `approve`; three wrong comments they found were fixed. Azure cost was zero (local Docker only).

## 2026-11-15 decision: open lateral and outlet boundaries instead of slip walls

Lateral and outlet boundaries use `inletOutlet` for U and fixed p = 0, so they add no blockage; a closed lateral boundary (slip or symmetry) acts as blockage and the design estimated 2.2 % Cd overshoot even at a lateral half-height of 60D. The design kept slip walls on a 120D x 240D domain only as a second fallback; it was not needed, because `inletOutlet` on the sides was stable in `simpleFoam` and `pimpleFoam` without any relaxation change.

## 2026-11-15 decision: committed domain S (30D upstream, 60D downstream, +-30D), chosen by a two-size check

The committed domain is S = 30D / 60D / +-30D; a scratch domain L = 60D / 120D / +-60D with identical near-field cell sizes served as check. The criterion, proposed by `cfd-reviewer`, was a quarter of the benchmark threshold for the S to L change: Cd and `cd_mean` at most 1.25 %, L/D at most 2.5 %, St at most 1.0 %. Measured S to L: Re 20 Cd -0.79 % and L/D -1.95 %; Re 100 `cd_mean` -0.39 % and St -0.24 %. All criteria were met, and S and L each pass the benchmark on their own. Cd increments halve per doubling (blockage-like); extrapolating Cd(S, L, XL) at Re 20 gives about 2.006 for an unbounded stream, so S is about 1.5 % high in Cd.

## 2026-11-15 decision: skewed-inlet trigger for shedding, St from Cl, and guards in `post.py`

An unstructured quad mesh is not mirror-symmetric, so shedding would start only from round-off; a `uniformFixedValue` inlet with a vector `table` (v = 0.1 U for 3 s, ramped to 0) triggers it, and it worked as written with no extra utility. St is taken from the Cl frequency because Cd oscillates at exactly 2 f (3.2735 Hz, measured from the Cd record and equal to twice the Cl frequency), which would give 2 St. `post.py` computes `cd_mean` over whole periods of the second half of the record and raises, instead of writing a number, if the window holds fewer than 10 periods, the Cl amplitude is below 0.1, or the amplitude changes by more than 2 % between the window halves.

## 2026-11-15 experiment: Re 20 baseline, residual and mesh checks

Domain S, 14 057 cells, 414 SIMPLEC iterations ended by `residualControl` 1e-6 (cap 20000), 17 s total (solver 7 s, 1 rank; 4 ranks gave no gain, 19 s). Cd 2.0374 (reference 2.05, error 0.62 %, threshold 5 %); L/D 0.9268 (reference 0.93, error 0.35 %, threshold 10 %). Tightening to 1e-7 (501 iterations) changed Cd by 0.0003 % and L/D by 0.0001 %. Mesh coarse / baseline / fine (7 718 / 14 057 / 25 530 cells): Cd 2.0378 / 2.0374 / 2.0362, L/D 0.9446 / 0.9268 / 0.9210.

## 2026-11-15 experiment: Re 100 baseline and mesh check

Domain S, 25 444 cells, 21 128 steps (mean dt 1.42e-3 s), 4 ranks: 471 s on an idle machine and 639 s with the host busy (about 0.027 s per step). The Cl amplitude is stationary at 0.338 from about 9 s; the window 15-30 s holds 24.6 periods. St 0.16367 (band 0.160-0.170), `cd_mean` 1.33429 (reference 1.34, error 0.43 %, threshold 5 %); neither changes with the window start (skip 0.3-0.6) or the number of whole periods used. Meshes B / A / F (15 769 / 25 444 / 48 622 cells; 300 s / 639 s / 1981 s): St 0.16289 / 0.16367 / 0.16417, `cd_mean` 1.3393 / 1.3343 / 1.3327, monotone and converging; all inside the benchmark thresholds.

## 2026-11-15 obstacle: the stationarity guard rejected the XL domain at Re 100

In the extra domain XL (120D / 240D / +-120D) `post.py` refused the 30 s record because the Cl amplitude differed by 2.1 % between the window halves: the inlet is 12 s of convection away, so the start perturbation reaches the cylinder only at 12-15 s and shedding is stationary from about 18 s. The guard worked as intended. The XL numbers (St 0.16288, `cd_mean` 1.3277) come from a hand-analysed window t >= 18 s and are an extra, not a criterion; running XL through `post.py` would need `endTime` of about 45 s. The general lesson is that `endTime` and the window must assume the perturbation has left the window (about up/U seconds); a comment in the `controlDict` was deferred.

## 2026-11-15 obstacle: Re 20 L/D scatters between neighbouring meshes and is not monotone in domain size

On the baseline mesh L/D is 0.927, 0.909 and 0.925 for S, L and XL; changing only the wake-box size on S scatters it between 0.886 and 0.927, while L agrees (0.913 for both sizes tried). On the finer mesh the trend is nearly monotone (0.921, 0.904, 0.907). This was attributed to mesh scatter of an unstructured wake, not to the domain, since Cd spreads by only 0.1 % over the same variants; the worst L/D (0.886, error 4.8 %) is still inside the 10 % threshold. The attribution is a judgement and is recorded as such; committing the fine Re 20 mesh (30 s) to reduce the scatter was deferred.

## 2026-11-15 observation: three design estimates were wrong

The design note estimated a maximum aspect ratio of about 200 for the 0.0005 m wall cell; `checkMesh` in this version reports 2.37 (Re 20) and 3.80 (Re 100) because it does not count the 0.1 m thickness. The Re 100 wall time was estimated at 18-40 min on 4 ranks; it was 8-11 min, well under the 60 min flag, because most cells are far-field and the cost is the near field. Cell counts were 14 057 (estimate 15-25k) and 25 444 (estimate 25-35k). The Gmsh `BoundaryLayer` field worked as written (it must also be activated with `setAsBoundaryLayer`), so no isotropic fallback was needed.

## 2026-11-15 experiment: reference check of the cylinder values (carry-over 6)

Confirmed first-hand from the full text of Fornberg (1980), J. Fluid Mech. 98, Tables 3 and 6: Cd at Re 20 Fornberg 2.0001, Dennis and Chang (1970) 2.045 (as quoted by Fornberg), Takami and Keller 2.0027, Nieuwstadt and Keller 2.053, Dennis (1973) 1.998; wake end 2.82 radii from the centre, hence L = 0.91 D from the rear surface (numerical range 0.89-0.94 D). The `benchmark.toml` values "Dennis and Chang 2.045", "Fornberg 2.000" and "Fornberg 0.91 D" are therefore confirmed. Not confirmed, because the originals are not available online here: Dennis and Chang's own tables, Tritton (1959, about 2.09), Coutanceau and Bouard (1977, 0.93 D), the Williamson (1988/1989) fit coefficients (the arithmetic gives 0.1643 at Re 100; some papers quote 0.167 for parallel shedding, inside the band) and Park et al. (1998) 1.33 and 0.165. `cfd-reviewer` found the unconfirmed values plausible from recollection, which is marked as such. Nothing in `benchmark.toml` was edited.

## 2026-11-15 decision: open owner item D4, Re 20 Cd reference left unchanged

Nothing was changed; the owner has to decide. `cfd-reviewer` notes that the Re 20 Cd reference 2.05 sits at the top of the accurate numerical values (1.998-2.01; 2.045 and 2.053 are finite-domain overshoots), so the band 1.95-2.15 is lenient upward and a 7 % overshoot still passes. The option is reference 2.025 with threshold 0.04 (band 1.944-2.106), which still passes every variant measured here. Two more points for the owner: the `benchmark.toml` comment on experimental Cd 1.39-1.40 at Re 100 is unverified, and the citation text "taken from secondary tables" is now partly out of date. This is not a blocker for the PR.

## 2026-11-15 decision: minor review findings deferred

Not done: committing the fine Re 20 mesh, unit tests for the `post.py` guards, a longer Re 20 Cd convergence guard window (the test also asserts that `residualControl` ended the run), support for more than the 3-argument form of `post.py`, assertions on the scored quantity names, and the `controlDict` comment on the perturbation leaving the window.
