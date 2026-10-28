# Research journal

A curated, factual record of the build process. The final report is written from it (together with ADRs, run records and the design document), so it records what happened and why, including failures. It is maintained by the `scribe` agent, which is invoked at every milestone and at the end of each work session.

## Files

One file per work session or topic: `docs/journal/YYYY-MM-DD-<slug>.md`, for example `2026-10-27-design.md`. Entries are append-only; a changed decision gets a new entry that refers to the old one.

## Entry format

Every entry starts with a level-2 heading:

```
## <YYYY-MM-DD> <kind>: <title>
```

where `<kind>` is exactly one of:

| Kind | Content |
|---|---|
| `decision` | What was chosen, the alternatives and the reason |
| `obstacle` | The problem, then the solution (or that it is unresolved) |
| `milestone` | What is now done and how it was verified |
| `experiment` | What was run, with the numbers: model, parameters, counts, cost, time, errors |
| `observation` | How an agent behaved, with a concrete example |

## Rules

- At most a few sentences per entry. Test for every sentence: "would this sentence appear in the report?" If not, cut it.
- Facts and numbers from logs, run records or the conversation; never invented. Mark uncertain numbers as uncertain.
- The human is "the owner". No personal names, e-mail addresses, IP addresses, subscription IDs, keys or other identifiers. Resource names such as `rg-cfdagent-probe` are fine.
- English only.
- Larger architecture decisions are also recorded as ADRs in `docs/adr/`.
