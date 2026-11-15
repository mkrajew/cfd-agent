# OpenFOAM ESI v2512 function objects: names, libraries, output files

Scope: the function objects in the `functions` dictionary of `system/controlDict` that write
results for post-processing, as checked on OpenFOAM ESI v2512. Stock examples are in
`$FOAM_ETC/caseDicts/postProcessing/<group>/` (`numerical`, `forces`, `fields`, `probes`,
`surfaceFieldValue`, `pressure`, `minMax`, `flowRate`, `visualization`) and in the tutorials.

## Rules

1. The sub-dictionary name is the function-object name, and results go to
   `postProcessing/<name>/<time>/<file>`. Name each function object after the output it
   produces; an analysis step finds the files by that name. `<time>` is the start time (`0`)
   for series files and the write time for sets and surfaces.
2. The residual history function object is `type solverInfo;` `libs (utilityFunctionObjects);`
   with `fields (p U);`, `writeControl timeStep; writeInterval 1;`. `type residuals;` is not
   a valid type in v2512: the solver prints only the warning "Unknown function type
   residuals", runs on, and writes nothing. The file is `solverInfo.dat`; its header line
   lists the columns `<field>_solver`, `<field>_initial`, `<field>_final`, `<field>_iters`,
   `<field>_converged` (a vector has one set per solved component, for example `Ux_initial`).
3. Output files and layout (one row per write, comment lines start with `#`; read the header
   for the columns instead of hard-coding them):

   | `type` | `libs` | file in `postProcessing/<name>/<time>/` |
   |---|---|---|
   | `forces` | `(forces)` | `force.dat`, `moment.dat`: total, pressure, viscous x y z |
   | `forceCoeffs` | `(forces)` | `coefficient.dat`: `Cd Cd(f) Cd(r) Cl Cl(f) Cl(r) CmPitch ...` |
   | `sets` | `(sampling)` | `<set>_<fields>.xy`, columns: coordinate, then the field components |
   | `surfaces` | `(sampling)` | `<surface>.vtp` (`surfaceFormat vtk`) |
   | `probes` | `(sampling)` | one file per field (`p`, `U`), one column per location |
   | `surfaceFieldValue` | `(fieldFunctionObjects)` | `surfaceFieldValue.dat`, `areaAverage(p)` ... |
   | `wallShearStress` | `(fieldFunctionObjects)` | `wallShearStress.dat` (min, max per patch) |

   Other types in the stock files: `yPlus`, `fieldMinMax`, `volFieldValue`, `CourantNo`,
   `pressure`, `streamLine`, `writeObjects`.
   `forces`/`forceCoeffs` entries are in the note `forces-and-coefficients.md`.
4. Time-series outputs (`solverInfo`, `forces`, `forceCoeffs`, `probes`, `surfaceFieldValue`):
   `writeControl timeStep; writeInterval 1;` gives one row per time step (per iteration for a
   steady solver). `sets` and `surfaces` use `writeControl writeTime;` and are written together
   with the fields, so choose the run's write interval with that in mind. A steady run that
   stops on `residualControl` writes a final time directory, and the sets are in that one.
5. `sets` with a `uniform` line: `axis` (the coordinate written in column 1: `x`, `y`, `z`),
   `start`, `end`, `nPoints` are all mandatory. `interpolationScheme cellPoint;`
   `setFormat raw;` `fields (U p);`. Points outside the mesh are dropped silently (a line of 41
   points that leaves the domain gave only the rows inside it), so compare the row count with
   `nPoints`; put the end points on or inside the boundary, not outside. For vector fields
   the file has three columns per field, even in a 2D case.
6. `surfaces` with a plane: `type cuttingPlane; planeType pointAndNormal;
   pointAndNormalDict { point (...); normal (...); } interpolate true;`. The point and normal
   must sit in a `pointAndNormalDict` sub-dictionary, otherwise the run stops with
   "Entry 'pointAndNormalDict' not found".
7. A mean or flux over a patch: `type surfaceFieldValue; libs (fieldFunctionObjects);
   regionType patch; name <patch>; operation areaAverage; fields (p U); writeFields false;`.
   Other operations in the stock files: `sum`, `areaIntegrate`, `min`, `max`. For an interior plane use
   `regionType sampledSurface; name <label>;` with a `sampledSurfaceDict` holding the surface
   dictionary of rule 6 (the file is `surfaceFieldValue.dat` too).
8. `probes`: `fields (p U); probeLocations ((x y z) ...);` with locations inside the domain.
9. A wrong patch name in `patches (...)` is only a warning ("Cannot find any patch or group
   names matching ..."); the function object runs and writes zeros or nothing. Compare the
   names with `constant/polyMesh/boundary`.
10. `libs` takes the library without prefix or suffix, for example `libs (sampling);`; the stock
    `forces.cfg` uses `libs ("libforces.so");`, both work. A kinematic incompressible solver
    gives pressure as p/rho, in m^2/s^2.
11. Dictionary code (`#codeStream`, `coded` function objects, `systemCall`) is disabled in this
    environment; use only the built-in types.

## Snippet: residuals plus a profile line

```
functions
{
    solverInfo { type solverInfo; libs (utilityFunctionObjects); fields (p U);
                 writeControl timeStep; writeInterval 1; }
    profile
    {
        type sets; libs (sampling); writeControl writeTime;
        interpolationScheme cellPoint; setFormat raw; fields (U p);
        sets ( line { type uniform; axis y; start (x0 y0 zmid); end (x0 y1 zmid); nPoints n; } );
    }
}
```

## Common mistakes

- `type residuals;` (older name): nothing is written; use `solverInfo`.
- A `sets` line with end points outside the meshed domain: silently fewer rows.
- Reading `postProcessing/<name>/0/` for a set: sets are in the directory of the write time.
- Hard-coded column numbers: the file name lists the fields in column order (a request
  `fields (U p)` produced `<set>_p_U.xy`, with p first); read the name or the header.
- `writeControl writeTime` for a series that should have one row per step.
