# ADR-0009: Claude merges reviewed pull requests; related fixes may share one pull request

- **Status:** Accepted, 2026-11-24 (owner approval in chat, during Task 17 of the M2 live development series)
- **Deciders:** the owner

## Context

Design section 8 and CLAUDE.md rule 2 said that the owner merges every pull request. In the Task 17 live development series each general fix also went into its own pull request. A series that found four fixes therefore needed four review cycles and several waits for the owner before the next live series could start. Most of the elapsed time went into those waits, not into the work. The owner asked why the project takes so long and approved three changes.

## Decision

1. **Claude merges.** The main session may merge a pull request itself, as a merge commit, once all required CI checks are green and `code-reviewer` (and `cfd-reviewer` where rule 3 requires it) returned `approve`. The owner gets a plain-language summary after the merge. Branch protection of `main` stays. Pushing to `main` directly, force pushes, admin overrides and merging with failing or pending checks stay forbidden.
2. **Batched fixes.** Related general fixes found in one live series may go into one pull request. Each fix keeps its own failing test first and its own conventional commit, and the review covers the whole diff.
3. **Stronger model comparison.** One or two live runs may use a stronger model than gpt-oss-120b, if one can be deployed on the subscription within the envelope, to separate model errors from pipeline errors. This is a measurement, not a change of the default model. The Azure rules of CLAUDE.md rule 4 apply unchanged.

The owner keeps approval of design decisions and ADRs, of CFD results at milestones, and of Azure work outside the standing approval.

## Alternatives considered

- **Keep owner merges.** Rejected by the owner: it made the owner a bottleneck for changes that had already passed CI and an independent review.
- **Merge without review.** Rejected: reviews caught real bugs in M1 and M2 (path escape, Courant bypass, surviving mutants), so the review gate stays and now carries more weight.

## Consequences

- Fewer waits between a live series and the next one.
- Review is the last independent check before `main`; a `changes` or `reject` verdict still blocks the merge.
- The PR description still states what, why, how verified and the Azure cost impact, so the owner can audit merged work afterwards.
