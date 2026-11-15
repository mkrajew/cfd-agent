# Forces and force coefficients: `forces` and `forceCoeffs`

Scope: the function objects `forces` and `forceCoeffs` of OpenFOAM ESI v2512 with an
incompressible (kinematic) solver, and how to take their reference values from the task text.
Statements marked "checked" were run on a small 2D case with `simpleFoam`. The output files
are described in `openfoam-function-objects.md`.

## Rules

1. Every reference value comes from the task or the plan, never from habit: the density
   `rhoInf` (kg/m^3), the reference velocity `magUInf` (m/s), the reference length `lRef` (m),
   the reference area `Aref` (m^2), the centre of rotation `CofR`, the drag and lift
   directions. If the task gives a Reynolds number, a length and a velocity instead of a fluid,
   derive `nu = U L / Re` and state which density you assumed (a density of 1 makes forces
   "per unit density" numerically equal to the kinematic ones); write that in the notes.
2. A kinematic solver (`simpleFoam`, `pimpleFoam`, `icoFoam`) carries `p/rho` and `nu`.
   The force function objects need the density to give newtons: `rho rhoInf;` and
   `rhoInf <density>;`. Checked: the force scales linearly with `rhoInf` (a factor 1000 in
   `rhoInf` gave a factor 1000 in every force component). Without `rho rhoInf;` the run stops
   with "Could not find rho:rho in database". A compressible solver has a `rho` field and no
   `rhoInf` is given for `forces` in its stock set-up (pressure is in Pa there).
3. `forces`: `type forces; libs (forces); patches (<body patches>); rho rhoInf; rhoInf <rho>;
   CofR (x y z); writeControl timeStep; writeInterval 1;`. `patches` lists the wall patches of
   the body only (names as in `constant/polyMesh/boundary`), not the domain boundaries. A name
   that matches nothing is only a warning ("Cannot find any patch or group names matching"),
   and the output stays at zero.
4. `forceCoeffs`: the same entries plus `liftDir (...)`, `dragDir (...)`, `pitchAxis (...)`,
   `magUInf`, `lRef`, `Aref`. `CofR` is required: without it the run stops with "Entry
   'origin' not found". `rhoInf` is read as well (the stock include calls it "redundant, but currently read in"). Directions are unit vectors:
   `dragDir` along the free stream, `liftDir` perpendicular to it in the flow plane,
   `pitchAxis` perpendicular to both (the out-of-plane axis of a 2D case).
5. The coefficient is `Cd = Fd / (0.5 * rhoInf * magUInf^2 * Aref)` (checked: `Cd` in
   `coefficient.dat` equals `total_x` of `force.dat` divided by that denominator), and moment
   coefficients divide by `0.5 * rhoInf * magUInf^2 * Aref * lRef` (checked the same way).
   Choose `Aref` as the projected frontal area of the body, normal to `dragDir`.
6. 2D cases: the mesh is one cell thick, so the force is for that slice of depth `thickness`.
   Use `Aref = lRef_projected * thickness` with the mesh thickness (not 1 m) so that `Cd`
   is the true 2D coefficient. A force per unit span is `F / thickness`.
7. Axisymmetric (wedge) cases: the force is for a slice of `angle` degrees. The force on the
   whole body is `F * 360 / angle`; `Aref` must be the area of the same slice, or the whole
   area with the whole force, never one of each (see `axisymmetric-wedge.md`).
8. `force.dat` has columns `total_x total_y total_z`, `pressure_x ...`, `viscous_x ...`
   (header in the file); `moment.dat` the same for moments about `CofR`; `coefficient.dat`
   has `Cd Cd(f) Cd(r) Cl Cl(f) Cl(r) CmPitch CmRoll CmYaw Cs Cs(f) Cs(r)` (f and r: front and
   rear halves along `dragDir`). Read the header for the order and for the axes it prints
   (`sideDir`, `pitchAxis`): they can be the negative of the vectors you gave, which flips
   the sign of the moment coefficients.
9. Reading the result: a steady run's value is the last row, after the residuals have fallen
   (the first rows are the start-up transient). An unsteady run oscillates: report the mean
   over a whole number of periods after the start-up transient, plus the amplitude and the
   frequency of the oscillation, never one sample. Say whether the quantity is converged
   (steady between the last write times) or not.
10. Forces depend on wall resolution: the viscous part needs cells that resolve the boundary
    layer, the pressure part needs the pressure gradient resolved near the body. Judge the
    result against the mesh, and say when the mesh cannot resolve it.

## Snippet

```
forcesBody
{
    type forceCoeffs;  libs (forces);
    patches (bodyWall);
    rho rhoInf;  rhoInf <density>;
    CofR (<x> <y> <z>);
    liftDir (0 1 0);  dragDir (1 0 0);  pitchAxis (0 0 1);
    magUInf <U>;  lRef <L>;  Aref <A>;
    writeControl timeStep;  writeInterval 1;
}
```

## Common mistakes

- Missing `rhoInf` or `CofR` (the run stops), or `rhoInf 1` for a fluid whose density is not 1
  while the task asks for newtons.
- `patches` naming the whole domain boundary, or a wrong name (warning only, zeros).
- `Aref` or `lRef` not the reference of the task (a different length than the one the task
  names) and `Aref` for 1 m of depth in a case that is `thickness` thick.
- `dragDir` not along the incoming flow, so drag and lift swap or mix.
- Averaging the start-up transient into the mean.
- Mixing a slice force with a full-body reference area in an axisymmetric case.
- Using `forceCoeffs` alone and later needing the raw force: write `forces` as well (it is the
  raw series; coefficients are derived from it).
