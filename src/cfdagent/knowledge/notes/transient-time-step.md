# Transient runs: time step from a Courant target, and write control

Scope: `deltaT`, `adjustTimeStep`, `maxCo`, `endTime` and the run's write control in
`system/controlDict` for transient incompressible solvers (`icoFoam`, `pimpleFoam`) in OpenFOAM
ESI v2512. Statements marked "checked" were run on a small 2D case with `pimpleFoam`.

## Rules

1. The Courant number of a cell is `Co = |u| dt / dx`: velocity times time step over the cell
   size in the flow direction. The solver prints `Courant Number mean: ... max: ...` at every
   step; the maximum is the number that matters.
2. Choose `dt` from a target, not from habit: `dt = Co_target * dx_min / |u|_max`. Take
   `dx_min` along the flow where the velocity is high (not the thinnest wall cell where the
   flow is slow), and `|u|_max` as the reference velocity times a margin of 1.5 to 2 where the
   flow accelerates around a feature. Compute it with the calculator and write the numbers in
   the notes.
3. Targets (general guidance, not run here): a PISO-type fixed step (`icoFoam`, or `pimpleFoam` with `nOuterCorrectors 1`)
   wants `Co` of 1 or below; 0.5 is a safe choice. `pimpleFoam` with `nOuterCorrectors` greater
   than 1 (and relaxation) tolerates more. Implicit diffusion is stable at any step, so the
   diffusion number does not limit `dt`.
4. Resolve the time scale of the physics: use at least 20 to 50 steps per period of an unsteady
   phenomenon you want to capture (estimate the period from a length and a velocity of the
   problem), and enough time for the flow to develop: several flow-through times `L / U`
   (`L` the length of the domain, `U` the reference velocity), or the diffusion time across the
   thickness of a developing shear flow, whichever is relevant. A symmetric set-up grows an
   instability only from round-off, so include the growth time in `endTime` or perturb the
   initial field slightly.
5. Adjustable step: `adjustTimeStep yes; maxCo 0.5; maxDeltaT <dt_max>;` with `deltaT` as the
   first step. Checked: the maximum Courant number stayed at the target (0.4998 for
   `maxCo 0.5`) and `deltaT` changed every step. `maxCo` is bound by the same stability limit
   as in rule 3. Time-series output then has rows at irregular times.
6. Write control with an adjustable step (checked, all three with the same case):
   `writeControl adjustableRunTime; writeInterval 0.25;` writes exactly at 0.25, 0.5, ... and
   the solver shortens a step to hit them, and `endTime` is hit exactly.
   `writeControl timeStep; writeInterval 20;` writes every 20 steps, at irregular times
   (0.0618, 0.1177, ...). `writeControl runTime;` with `adjustTimeStep yes` wrote at the first
   step after each interval and ended past `endTime` (1.001096 for an `endTime` of 1). With
   `timeStep` the run also ended past `endTime`, and the final time was not written because
   the step count did not land on it (re-checked on v2512).
   Use `adjustableRunTime` whenever `adjustTimeStep yes`.
7. With a fixed step, `writeControl timeStep; writeInterval N;` with `N = interval / deltaT`,
   a whole number of at least 1, or `writeControl runTime;` with an interval that is a
   multiple of `deltaT`. Keep `endTime` a multiple of the write interval so that the final
   time is written.
8. Write interval sizing: aim for about 10 to 50 written time directories, so that the last
   two can be compared to see whether the solution is still changing, and storage stays small.
   `purgeWrite 0` keeps all of them. `writeFormat ascii; writePrecision` at least 6.
   Function-object series (forces, probes, `solverInfo`) have their own
   `writeControl timeStep; writeInterval 1;` and are independent of the run's write interval.
9. Time schemes: `ddtSchemes { default Euler; }` is first order and robust; `backward` is
    second order and is the choice where phase or frequency of an unsteady flow matters.
    Convection in a transient run: `Gauss linearUpwind grad(U)` (no `bounded`).
10. `pimpleFoam` linear solvers need an entry for the final pressure solve, for example
    `pFinal { $p; relTol 0; }`, and `"U.*"` (matches `UFinal`). `PIMPLE` holds
    `nCorrectors`, `nOuterCorrectors`, `nNonOrthogonalCorrectors`, and `pRefCell` and
    `pRefValue` when no patch fixes the pressure.

## Snippet: adjustable step

```
application     pimpleFoam;
startFrom       startTime;   startTime  0;
stopAt          endTime;     endTime    <end_time>;
deltaT          <first_dt>;
adjustTimeStep  yes;
maxCo           0.5;
maxDeltaT       <dt_max>;
writeControl    adjustableRunTime;
writeInterval   <end_time / 20>;
purgeWrite      0;
```

## Common mistakes

- `deltaT` copied from another case: the mesh and velocity decide it (rule 2).
- Maximum velocity taken as the inlet velocity when the flow accelerates around an obstacle.
- `adjustTimeStep yes` with `writeControl timeStep` or `runTime`: irregular names or overshoot.
- `writeInterval` in seconds with `writeControl timeStep` (it counts steps), or a number of
  steps with `adjustableRunTime` (it is a time).
- `endTime` too short for the flow to develop, or for the instability to grow and be sampled
  over several periods.
- Fixed `deltaT` with a Courant number above 1 in a PISO run with one outer corrector.
