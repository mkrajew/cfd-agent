# Steady incompressible set-up (`simpleFoam`, SIMPLE algorithm)

Scope: the dictionaries of a steady, incompressible, single-phase case in OpenFOAM ESI v2512:
`controlDict`, `fvSchemes`, `fvSolution`. Statements marked "checked" were run on a small
laminar 2D case; turbulence models add fields and wall functions that are not covered here.

## Rules

1. `application simpleFoam;`. A steady solver has no physical time: `ddtSchemes { default
   steadyState; }`, `deltaT 1;`, and `endTime` is the maximum number of iterations. The run ends
   earlier with "SIMPLE solution converged in N iterations" when `residualControl` is met, and
   writes that last iteration. If the run reaches `endTime`, it has not converged; set
   `endTime` well above the expected count (hundreds to a few thousand iterations).
2. `residualControl` lives inside the `SIMPLE` dictionary of `system/fvSolution`:
   `SIMPLE { residualControl { p 1e-5; U 1e-5; } }`. It tests the initial residual (before the
   solve of the iteration), the largest over the components for a vector. The keys are field
   names (`p`, `U`): a component key such as `Ux` is accepted and silently ignored (checked), so
   compare the last initial residuals of each field in the solver log with the targets. A looser target stops earlier but
   leaves the solution still changing, and a target that is enough on a coarse mesh can be too
   loose on a fine one; choose it by invariance: tighten it by two orders of magnitude and
   check that the quantities you report do not move (and no longer change between the last
   write times).
3. Under-relaxation goes in a top-level `relaxationFactors` dictionary of `fvSolution`,
   beside `solvers` and `SIMPLE`, never inside `SIMPLE`:
   `relaxationFactors { fields { p 0.3; } equations { U 0.7; } }`.
   Checked on a small laminar case: with the same dictionary placed inside `SIMPLE`, and with
   no relaxation at all, no message appeared, the solver ran unrelaxed and crashed with a
   floating point exception.
4. Two usual pairings (both checked): classic SIMPLE `fields { p 0.3; } equations { U 0.7; }`;
   SIMPLEC, `consistent yes;` inside `SIMPLE`, with `equations { U 0.9; }` and no pressure
   relaxation (the pressure equation is consistent with the relaxed momentum equation).
   Turbulence equations (`k`, `epsilon`, `omega`) are relaxed in `equations` like `U`.
5. Convection schemes (`divSchemes`): `div(phi,U) bounded Gauss linearUpwind grad(U);` is the
   usual second-order choice, `bounded Gauss upwind` the robust first-order fallback,
   `bounded Gauss limitedLinear 1` a limited central scheme. `bounded` removes the continuity
   error term and matters for steady solvers only. Use upwind-biased schemes where the cell
   Reynolds number (local velocity times cell size over `nu`) is well above 2; `Gauss linear`
   is only for cells where it is about 2 or less. Turbulence quantities: `bounded Gauss upwind`
   or `limitedLinear 1`. `linearUpwind` needs a gradient scheme named in its argument
   (`grad(U)`), which the `default Gauss linear;` of `gradSchemes` supplies.
6. `divSchemes` must also hold the viscous stress term the incompressible solvers read:
   `div((nuEff*dev2(T(grad(U))))) Gauss linear;`. Use `default none;` so a missing scheme is
   reported by name instead of being guessed.
7. Pressure level. With a fixed-value pressure patch (typically the outlet) the level is set
   and `pRefCell` is not needed (checked: giving it anyway is ignored, so leave it out). With no
   such patch (all walls and velocity inlets) the
   pressure is defined up to a constant: set `pRefCell 0; pRefValue 0;` inside `SIMPLE`
   (`pRefPoint (x y z)` instead of the cell works too). Without it the solver stops with
   "Unable to set reference cell for field p. Please supply either pRefCell or pRefPoint".
8. Linear solvers: `p { solver GAMG; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; }`,
   `U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }`. A
   relative tolerance of 0.1 inside each iteration is enough; the outer iteration converges the
   non-linearity. Mesh non-orthogonality: `nNonOrthogonalCorrectors 0` for a near-orthogonal
   mesh, 1 or 2 otherwise, with `Gauss linear corrected` and `snGradSchemes corrected`.
9. `writeControl timeStep; writeInterval <N>;` writes every N iterations. A large interval is
   fine for a run that stops on `residualControl`, because the last iteration is written
   anyway. Function objects that write per iteration use `writeInterval 1` of their own.
10. Residual histories: the function object `solverInfo` (see `openfoam-function-objects.md`); a plateau of the residuals, instead of a steady fall by
    orders of magnitude, means the solution is not settling (unsteady physics, a poor mesh or
    a scheme that is too aggressive).
11. Domain length for a developing flow. A flow that enters with a uniform profile needs a
    length before it is fully developed; compare any fully developed reference (a profile, a
    pressure gradient) only downstream of it. For laminar flow in a circular duct the
    entrance length is about `0.0575 * Re * D` (`Re = U_mean * D / nu`, `D` the diameter;
    checked: the centreline velocity reached 99 % of its final value at that distance, and the
    pressure gradient was within 1 % of the fully developed one a little downstream). The
    correlation is for a uniform inlet profile (Langhaar); imposing a fully developed profile
    at the inlet is the alternative to a long domain. The pressure drop between two stations
    that both lie beyond the entrance length is the fully developed gradient times their
    distance; one that starts in the entrance region also includes the extra drop of the
    development.
    Other cross-sections and turbulent flow have other lengths (not run here).

## Snippet: `fvSolution` for a laminar steady case

```
solvers
{
    p { solver GAMG; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; }
    U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }
}
SIMPLE
{
    nNonOrthogonalCorrectors 0;
    consistent yes;
    residualControl { p 1e-5; U 1e-5; }
}
relaxationFactors { equations { U 0.9; } }
```

## Common mistakes

- `relaxationFactors` inside `SIMPLE`: ignored, no message, no relaxation, divergence.
- `pRefCell` missing when no patch fixes the pressure (the run stops), or added without need.
- `Gauss linear` for convection at a high cell Reynolds number (oscillations, divergence).
- `deltaT` and `endTime` thought of as seconds: they count iterations.
- Linear-solver `tolerance` close to or above the `residualControl` values: keep it far below.
- `Ux` or `Uy` as `residualControl` keys: ignored without a message; use the field names.
- Wedge (axisymmetric) cases: the `U` residual stays above the target because of the
  component normal to the wedge planes; control on `p` (see `axisymmetric-wedge.md`).
- Sampling a fully developed reference inside the entrance region (rule 11).
