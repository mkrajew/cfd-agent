# Knowledge notes: rules and format

These notes are short, curated rules for the tools of the pipeline (Gmsh, CadQuery, OpenFOAM
ESI v2512). They sit next to the OpenFOAM tutorials: a note tells you what is true in general, a
tutorial shows a complete example. Notes are searched first.

## How to use them (agents)

1. `search_knowledge(query)` returns hits from the notes before hits from the tutorials. All
   query words must occur in a file's path or text, so search with one or two API names,
   dictionary keywords or file names (the name in an error message works well), and use fewer
   words when nothing is found. Note hits are shown as `note:<file>`, tutorial hits as a path.
2. `read_knowledge("note:<file>")` reads a whole note, for example `note:gmsh-api.md`. A note is
   under 150 lines; read it completely instead of guessing from one hit. This README is
   readable the same way (`note:README.md`) and may appear among the hits; the table below
   says which note to open.
3. A note states rules and the reason for them. Check every number against your own plan: a
   note never contains the values of your task, only how to derive them.
4. When a tool error message and a note disagree, the error message describes what happened;
   re-read the note's "Common mistakes" and the exact line the error names.

## Notes in this directory

| File | Content |
|---|---|
| `gmsh-api.md` | Gmsh Python API: OCC kernel, synchronize, one-cell layer, physical groups, MSH 2.2 |
| `cadquery-api.md` | CadQuery primitives, booleans, revolve, STEP export and units |
| `openfoam-function-objects.md` | v2512 function-object names, libraries, output files and columns |
| `steady-solver-setup.md` | Steady SIMPLE set-up: relaxation, `residualControl`, schemes, pressure level |
| `transient-time-step.md` | Time step from a Courant target, `adjustTimeStep`, write control |
| `forces-and-coefficients.md` | `forces` and `forceCoeffs` entries and reference values |
| `axisymmetric-wedge.md` | Wedge geometry, patches and pitfalls of axisymmetric cases |

## Rules for the content of a note

1. **General knowledge only (generality rule).** A note never names a benchmark or a test case,
   a specific Reynolds number, mesh size or expected result. Examples in a note use symbolic
   names (`length`, `nu`, `n_cells`), not numbers of a case.
2. **Only verified statements.** Every rule was checked on the installed version (OpenFOAM v2512,
   the Gmsh and CadQuery of the image), by running a snippet or a small case, or read from the
   installed `$FOAM_ETC` and tutorials. A rule that was not checked is either left out or
   labelled "not run here" in the text.
3. **No blind evaluation leak.** Never put reference data, acceptance criteria or thresholds of
   a benchmark into a note.
4. **English, plain markdown, under 150 lines per note.**

## Structure of a note

1. A short title.
2. One paragraph of scope: which tool or topic, which version, what the note does not cover.
3. Numbered rules, each concrete and checkable, with the reason in one clause.
4. Minimal snippets (a few lines, symbolic values), only where the exact form matters.
5. A "Common mistakes" list: the misuse, the visible symptom, the fix.
6. Keywords: the API names, dictionary keywords and file names appear literally in the text,
   because search matches the text.
