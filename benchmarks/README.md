# Benchmarks

This directory holds the **development benchmarks** of `cfd-agent` (design document, section 9): for
each case a natural-language task, literature reference data and the acceptance criteria that the
deterministic Scorer applies to the simulated quantities.

**Agents must never read this directory.** Reference data and acceptance criteria are not mounted
in the executor sandbox and not part of the knowledge base (design section 9.2, blind evaluation).
Only the orchestrator hands the contents of `task.md` to the Planner; the Scorer reads the rest after
the Critic's verdict is final. Nothing under `src/` may mention these cases (generality rule).

## Layout

```
benchmarks/<id>/
  benchmark.toml    id, family, task_file, citation, [[criteria]] (see src/cfdagent/bench/registry.py)
  task.md           the task, written as a user would write it
  reference/*.csv   reference profiles, header "x,value"
```

Paths in `benchmark.toml` are relative to the benchmark directory. The TOML accepts only the keys of
the registry models; physical parameters therefore cannot be stored in it (see the pipe case).

| id | family | quantities (Scorer keys) | reference |
|---|---|---|---|
| `cavity-re100` | lid-driven cavity | `u_vertical_centreline`, `v_horizontal_centreline` (profiles) | Ghia, Ghia and Shin (1982), Tables I and II |
| `cavity-re1000` | lid-driven cavity | same | same |
| `pipe-re500` | laminar pipe flow | `u_profile_fully_developed` (profile), `pressure_drop_fully_developed` | Hagen-Poiseuille (analytic) |
| `cylinder-re20` | cylinder in cross-flow, steady | `cd`, `recirculation_length_over_d` | Dennis and Chang (1970), Fornberg (1980), Park et al. (1998), Tritton (1959), Coutanceau and Bouard (1977) |
| `cylinder-re100` | cylinder in cross-flow, periodic shedding | `strouhal` (range), `cd_mean` | Williamson (1988, 1989), Park et al. (1998) |

Full citations are in the `citation` field of each `benchmark.toml`.

## How the references were obtained

- **Cavity (Ghia et al.).** Transcribed from Table I (u on the vertical line through the geometric
  centre, p. 398) and Table II (v on the horizontal line through the geometric centre, p. 399), Re =
  100 and 1000 columns, all 17 points per profile including both wall points. The numbers were read
  from a scan of the paper and cross-checked against two independent transcriptions. The coordinates
  are given to four decimals as printed; the actual grid points of the 129x129 solution are multiples
  of 1/128, so the rounding moves a point by at most 5e-5 (at most about 1e-3 in the value where the
  profile is steepest, near the lid; far below the thresholds). Each CSV lists x in ascending order, with x the coordinate along
  the line (y for u, x for v) and the value in units of the lid speed.
- **Pipe.** `pipe-re500/make_reference.py` generates `reference/u_profile.csv` from the
  Hagen-Poiseuille profile and checks the pressure-drop reference in `benchmark.toml` against
  `32*mu*L*U_mean/D^2`. The parameters are constants in the script (not in `benchmark.toml`, whose
  schema forbids extra keys) and must be kept in sync with `task.md`. Run
  `python benchmarks/pipe-re500/make_reference.py --check` to verify the committed CSV.
- **Cylinder.** Scalar values from published experiments and converged numerical solutions for an
  unbounded domain, taken from secondary comparison tables (the original papers were not available).
  Where the literature scatters, the reference is the middle of the spread (Re = 20) or an interval
  (Strouhal number at Re = 100). The reasoning is in the comments of each `benchmark.toml`.

## Requirements on a run that is scored against these references

- **Profiles must be sampled wall to wall.** The reference profiles include the wall points (x = 0
  and x = 1 for the cavity, r/R = 0 and 1 for the pipe). The Scorer interpolates the simulated
  profile onto the reference abscissae and refuses to extrapolate, so a simulated line that stops
  short of a wall makes the criterion fail. This also applies to hand-written cases used as
  fixtures.
- **Cylinder domain.** The task asks for an isolated cylinder in an unbounded stream and gives no
  domain size; the agent has to choose a domain large enough that its boundaries do not influence
  the result. The references are for the unbounded case. Blockage raises Cd and shifts the other
  quantities; for example, computations on a 40D x 20D domain quoted in the literature give Cd of
  2.16 to 2.23 at Re = 20 against 2.00 to 2.09 for large domains, which exceeds the 5 % threshold.
- **Normalised quantities.** Profiles are reported as u/U or v/U (cavity, with U = 1 m/s) and u/U_mean
  (pipe), and positions as fractions of the cavity side or the pipe radius. `normalise_by` in the
  TOML is the factor in the units of the reported profile (1 for the cavity, 2 for the pipe, the
  centreline velocity in units of the mean velocity).

## Changing a benchmark

Thresholds and references are fixed by `cfd-reviewer` and approved by the owner. A threshold is
never loosened to make a run pass without a new owner decision, recorded in the journal.
