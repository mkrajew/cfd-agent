# 2026-11-08 M1 execution, session 8: first live runs (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit ec6e075 (image `sha-ec6e075`), model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium.

## 2026-11-08 decision: spending envelope for the live runs

The owner approved in chat up to 2 live runs (one, plus at most one repeat after an infrastructure failure), with an M1 cap of $10 and a report to the owner at $5.

## 2026-11-08 obstacle: CLI ignored `CFDAGENT_IMAGE` (run 1, tool bug)

Run 1 (20261108-214047) failed after 37 s in the geometry stage with `docker: No such image: cfdagent:runtime` (exit 125). `cfdagent run` ignored `CFDAGENT_IMAGE` from `/etc/cfdagent/env` and always used `DEFAULT_IMAGE`. The Task 22 scripted replay and `verify_sandbox.py` passed the image explicitly, so the dry run did not catch it. Fixed test-first (2 new unit tests, whole suite 1743 passed) in commit `fix: read the sandbox image from CFDAGENT_IMAGE in the CLI` on branch `claude/m1-t23-live-run-025873`. For run 2 only, a local `docker tag` alias on the VM served as a workaround (no Azure change). Lesson: the dry run should exercise the real CLI entry point with the real environment file, not only a script that builds its own config.

Run 1 also showed that the first real model call from the VM through Entra ID works (planner ok after 2 attempts, $0.0074), that the upload with the container-scoped Blob role works (9 files; the old account-scoped role had been removed earlier the same day), and that the active-run marker works.

## 2026-11-08 decision: no prompt or tool tuning after the failed run

After run 2 failed, nothing was tuned: the owner's approval for spending was used up, and the generality rule forbids case-specific fixes. The triage below is recorded for the owner's decision.

## 2026-11-08 experiment: run 2, failure in case_setup

Run 2 (20261108-214751, same commit, alias in place) ended with status `failed` in `case_setup` after 3 of 3 attempts. Wall time 154 s; 106,219 tokens in, 21,238 out; LLM cost $0.0287. The run ended before the Solver Runner, so there is no Scorer comparison: this result is neither compared with reference data nor physically checked. 98 files were uploaded; the blobs were confirmed read-only from the laptop.

| Stage | Result | Detail |
|---|---|---|
| planner | ok, 2 attempts | attempt 1 rejected by the gate (`solver.end_time_s is 0 but must be a finite number greater than 0`); 24 s and 7 s |
| geometry | ok, 1 attempt | 15.8 s |
| mesh | ok, 3 attempts | `mesh.py` exited with code 1 in `assign_physical_groups` in attempts 1 (28 s) and 2 (41 s); attempt 3 ok (17.5 s) |
| case_setup | failed, 3 of 3 | 0.8 s, 7.7 s, 12.6 s; errors: no `system/controlDict`; no `0/` directory; no `system/controlDict` |

## 2026-11-08 observation: empty `stop` turns end the attempt early (not yet classified)

Four times the model ended a turn with `finish_reason` `stop`, empty text and no tool call: on the very first turn of case_setup attempt 1 (54 completion tokens), straight after a successful `write_file` result in case_setup attempts 2 and 3, and in mesh attempt 2. The loop treats a reply without a tool call as "done" and runs the gate. The gate fails because only one file was written, so the attempt is spent; the model wrote one file per attempt and never reached the rest. Usage reports `reasoning_tokens` as None (Foundry did not return them), so the stop may come from reasoning that was not returned or not sent back with the assistant tool-call message in the chat-completions history. This is a hypothesis, not verified. Geometry and mesh worked with multi-turn tool use, so the behaviour is not universal. Candidate general remedies for the owner (none implemented): treat an empty stop as "continue" with a short neutral nudge that does not consume an attempt, with a cap; check whether reasoning items must be passed back; try reasoning effort high. The mesh failures in `assign_physical_groups` are worth a look in the next triage; the traceback has not been analysed.

## 2026-11-08 experiment: cost of the live runs

The VM was up about 28 minutes from start to deallocate, about $0.10. LLM cost was $0.0074 + $0.0287 = $0.036. Total about $0.14, far below the $5 report threshold.

## 2026-11-08 milestone: first live runs, M1 acceptance not reached

Cavity Re=100 end to end without human intervention was not achieved: only 4 of 6 stages ran in run 2. This is a valid partial result. Live success rate so far is 0 of 2 (one infrastructure failure, one agent failure at case_setup).
