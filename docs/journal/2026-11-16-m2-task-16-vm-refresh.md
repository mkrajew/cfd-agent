# 2026-11-16 M2 Task 16: VM refresh and scripted replay of the M2 pipeline

## 2026-11-16 milestone: the VM runs the M2 pipeline end to end with a scripted model

The VM was started (106 s) and set up at commit 052833f (`main` with M2 Tasks 2-15, image `sha-052833f`) through `az vm run-command`: host setup exit 0 in 63 s, `verify_sandbox.py` 7 of 7 PASS in 35 s. The scripted replay of `cavity_pipeline.json` ended `completed` in 56.8 s wall time at a scripted cost of $0.0111. The Critic returned `success` with 4 of 4 checks, the Scorer passed, the Critic agreed with the Scorer, and the upload of 254 files was confirmed read-only (254 blobs, 29.7 MB). No live LLM was used; the VM ran about 10 minutes, about $0.04. Steps and per-stage times are in `docs/runbooks/m2-runs.md`.

## 2026-11-16 obstacle: the first `az` call failed with AuthorizationFailed

The Azure CLI on the laptop was signed in to a different account than the one that owns `rg-cfdagent`. The owner logged in to the project account and the work continued; the runbook now says to check the signed-in account before the first call.

## 2026-11-16 observation: the replay does not exercise the Task 9 input path, a separate probe does

The scripted fixture scripts do not read `../../casespec.json`, so the replay alone proved nothing about it. It was verified in a real Docker sandbox on the VM with an ad-hoc probe (not committed) run through `SandboxClient` `run_script` with `cwd` `attempts/geometry-1`: it printed the content of the file. This closes the open point "input path is reasoned, not verified in a sandbox" of `docs/journal/2026-11-14-m2-task-9-directive-checks.md`.

## 2026-11-16 observation: fixed costs dominate the idle bill; a saving awaits the owner's decision

Cost Management for `rg-cfdagent`, 2026-10-01 to 2026-11-16 (EUR): Foundry tokens 2.47, OS disk 2.67 (Standard SSD, 64 GB), public IP 1.94 (Standard, static), VM compute 1.91, storage 0.07, total 9.06. The fixed monthly part is about $8.6 (disk about $4.8, IP about $3.65, storage about $0.1). Releasing the public IP while the project is idle would save about $3.65/month at the price of losing SSH (access only through `az vm run-command`). Nothing was changed; the decision is the owner's. The VM was deallocated at the end and the state verified.
