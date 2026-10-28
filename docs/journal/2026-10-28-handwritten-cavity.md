# Journal: hand-written cavity (2026-10-28)

## 2026-10-28 experiment: hand-written lid-driven cavity (Re = 100) passes against Ghia et al.

The hand-written case (CadQuery, Gmsh 64 x 64 x 1 cells, `icoFoam` serial, dt = 0.005 s with a maximum Courant number of 0.30, endTime 40 s, no LLM) passes both `cavity-re100` criteria in the image. The maximum absolute errors against the Ghia et al. Re = 100 tables are 0.0049 for `u_vertical_centreline` (worst at y = 0.8516) and 0.0087 for `v_horizontal_centreline` (worst at x = 0.8594), against the approved threshold of 0.05. The centreline profiles changed by about 1e-8 (in units of the lid speed) between the last two write times, 35 s and 40 s. The solver took 23 s and the whole integration test 29 s on the owner's workstation (container-local temporary directory); the CI runtime has not been measured yet.

## 2026-10-28 experiment: the remaining error against Ghia et al. does not shrink with the mesh

The same case on 32, 48, 64, 96 and 128 cells per side (dt scaled to keep the Courant number near 0.3) gave maximum errors of 0.0042, 0.0040, 0.0049, 0.0049 and 0.0048 for u and 0.0080, 0.0076, 0.0087, 0.0088 and 0.0091 for v. Richardson extrapolation of the 64 and 128 solutions gives u_min = -0.2141, v_max = 0.1796 and v_min = -0.2539, against -0.2109, 0.1753 and -0.2453 in the Ghia et al. tables, so the 0.005 to 0.009 gap is the tabulated 129 x 129 solution's own discretisation error, not an error of the OpenFOAM setup. The threshold of 0.05 is unaffected and stays; the sentence in `benchmarks/cavity-re100/benchmark.toml` that the reference uncertainty is far below 0.001 holds for the transcription only, and the owner may want it reworded.
