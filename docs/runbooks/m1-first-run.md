# Runbook: M1 dry run and first live run on the VM

Status: the dry run (sections 1-4) was executed on 2026-11-08 at commit `c6e9b78`; the observed numbers are in
each section. The first live runs (section 5, Task 23) were executed the same day at commit `ec6e075`. Read
`infra/CLAUDE.md` and `docs/runbooks/vm-host.md` first. Nothing here creates, changes or deletes an Azure
resource. Starting and deallocating the VM are inside the approved M0-M2 envelope (the VM bills about
$0.208/h while it runs). The dry run calls no model (scripted conversation): its cost is VM time only.

Placeholders in angle brackets come from the deployment outputs or from your own machine; nothing real is
committed here.

## 1. Start the VM

From the repository root on the laptop (PowerShell):

```powershell
infra/scripts/vm-up.ps1
ssh cfdagent true        # repeat until it answers
```

Observed (2026-11-08): the VM was started with `az vm start`. The session was driven without SSH
through `az vm run-command invoke` (the laptop SSH key has a passphrase and no agent was running): each
script was written to a local file outside the repository and passed with `--scripts "@<file>"`. Run-command
sessions do **not** count for the 30-minute idle shutdown, so do not leave the VM waiting between steps.
On the Windows laptop `az` is `az.cmd`: a `--query` with brackets or quotes breaks in `cmd`; fetch `-o json`
and filter in PowerShell instead.

## 2. Host setup at the merged commit

Choose `<commit>`: the full commit id on `main` that contains Tasks 1-22 and whose image `sha-<7>` is
published. Run `scripts/vm-host-setup.sh <commit> --llm-endpoint <llm-endpoint> --storage-account-url
<storage-url>` in one of the two forms described in `docs/runbooks/vm-host.md`, sections 1 and 2
(2.1 over SSH, 2.2 through `az vm run-command`). Pin the commit; do not use a branch name. The script is
safe to re-run and updates `/opt/cfdagent-src`, `/opt/cfdagent-host`, `/etc/cfdagent/env` and pulls the image.

Check the result on the VM:

```bash
grep -E 'CFDAGENT_(COMMIT|IMAGE)' /etc/cfdagent/env     # the commit and image just installed
docker image ls ghcr.io/mkrajew/cfd-agent
cfdagent --help
```

Observed: form 2.2 of `docs/runbooks/vm-host.md` (run-command wrapper, full 40-character commit id), first
attempt, exit status 0, 1 min 35 s including the image pull. Image `ghcr.io/mkrajew/cfd-agent:sha-c6e9b78`:
5.33 GB (`docker image ls`; `docker image inspect` reports 1.16 GB). The host has
4 vCPU, 15 GB RAM and 48 GB free disk; metadata firewall rules present; `cfdagent-idle-shutdown --check` ok;
`/var/lib/cfdagent` and `~/runs` belong to the admin user. `cfdagent --help` lists run, status, logs, list.

## 3. Verify the sandbox

On the VM, in the host checkout, with the host environment (no pytest needed):

```bash
cd /opt/cfdagent-src
set -a; . /etc/cfdagent/env; set +a
/opt/cfdagent-host/bin/python scripts/verify_sandbox.py          # image from CFDAGENT_IMAGE
# or: ... scripts/verify_sandbox.py --image <image>
```

It prints one row per check (PASS or FAIL with short evidence) and exits 0 only if all seven pass: no network
(public IP and metadata addresses unreachable, only `lo`), no credentials in the environment, read-only
root, `#codeStream` refused, `mpirun -np 4 --use-hwthread-cpus`, a timeout leaves no container, and the run
directory is the only mount. It takes about two minutes and removes its temporary run directory. **Any FAIL
blocks Task 23**; fix it in a `fix:` pull request.

On the Windows laptop with Docker Desktop and the local image the same script printed 7/7 PASS
(`--image cfdagent:dev` and `--image cfdagent:runtime`).

Observed on the VM (28 s, exit 0):

```
CHECK                              RESULT  EVIDENCE
no network                         PASS    public IP:443, metadata:80, wireserver:80 and DNS blocked; only lo present
no credentials in the environment  PASS    planted secrets absent from the 43 environment variables and PID 1
read-only root filesystem          PASS    root ro; /tmp rw,noexec,nosuid,nodev; /job rw; no capabilities; no_new_privs=1
#codeStream refused                PASS    refused with the general hint; control case accepted; no marker, no compiled code
mpirun -np 4 --use-hwthread-cpus   PASS    4 ranks started and finished under --cap-drop ALL
timeout leaves no container        PASS    killed after 20 s; script had started; no container left
run directory is the only mount    PASS    only /job comes from the host (the run directory); 27 mounts in all
7/7 checks passed
```

The scripts were copied to `/opt/cfdagent-src/scripts/` (untracked) because the commit that adds them was not yet
on `main`; after the pull request is merged and the host is set up at that commit they are part of the checkout.

## 4. Dry run with the scripted backend

```bash
cd /opt/cfdagent-src
set -a; . /etc/cfdagent/env; set +a
/opt/cfdagent-host/bin/python scripts/replay_scripted_run.py tests/fixtures/scripted/cavity_pipeline.json
```

The run uses the docker sandbox and the real upload. Expected: status `completed`, Scorer `passed: True`,
the last upload line `upload: done, <N> file(s)`, exit status 0. The summary shows the run id, the wall time
per stage, the per-criterion errors, the run directory size and the blob-listing command.

Observed on the VM (62 s including start-up, exit 0): run `20261108-195840-lid-driven-cavity-flow-at-re-100`,
status `completed`, wall time 58.2 s, cost $0.0076 (scripted token counts priced as the primary model).
Stages: planner 0.0 s, geometry 6.0 s, mesh 4.0 s, case_setup 3.3 s, solver_runner 42.3 s, analyst 2.4 s
(one attempt each). Scorer passed: `u_vertical_centreline` error 0.0049, `v_horizontal_centreline` error
0.0087, both within the thresholds. Run directory 35.4 MiB on disk (28.3 MB uploaded); upload line
`upload: done, 253 file(s)`. Afterwards `cfdagent list` and `cfdagent status <run-id>` on the VM report the run
as `completed`, and `/var/lib/cfdagent` holds no `active-run` marker.

### Check the Blob contents (read-only)

From the laptop (`az login` with the data-reader role) or the VM:

```powershell
az storage blob list --auth-mode login --account-name <account> --container-name runs --prefix <run-id>/ --query "[].name" -o tsv
```

The folder `<run-id>/` must hold `summary.md`, `run_record.json`, `results/comparison.json`, `casespec.json`,
`analysis.md` and the logs. The `az` call is read-only.

Observed (read-only, from the laptop): 253 blobs under the run prefix, matching the 253 uploaded files. Top-level
entries include `summary.md`, `run_record.json`, `run.log`, `status.json`, `task.md`, `casespec.json`,
`analysis.md`, `llm_transcript.jsonl`, `case.tar.zst`, `results/comparison.json`, `results/quantities.json`,
`logs/solver_run.json`, `geometry/`, `mesh/` and `sandbox/`.

## 5. First live run (Task 23)

Only after sections 3 and 4 passed. The command-line interface reads `benchmarks/` relative to the current
directory, and the runtime image does not ship `benchmarks/` (blind evaluation), so start it from the host
checkout. The runs directory stays `CFDAGENT_RUNS_DIR` (absolute, from the environment file):

```bash
cd /opt/cfdagent-src
cfdagent run --benchmark cavity-re100 --detach        # prints the run id
cfdagent status <run-id>
cfdagent logs <run-id> --follow
```

`/usr/local/bin/cfdagent` loads `/etc/cfdagent/env` itself. The cost cap per run is $1.00 by default; a live
run needs the owner's go-ahead for any campaign above $5.

Observed (2026-11-08, Task 23, commit `ec6e075`, image `sha-ec6e075`; host setup exit 0, `verify_sandbox.py` 7/7 PASS):

- Run 1 (`20261108-214047-...`) failed after 37 s in the geometry stage with `docker: No such image:
  cfdagent:runtime`. Cause: `cfdagent run` ignored `CFDAGENT_IMAGE` from the environment file and used the
  default tag. Fixed in the CLI (`fix: read the sandbox image from CFDAGENT_IMAGE`); on the VM the
  run was repeated after `docker tag <pulled image> cfdagent:runtime`, a local alias that creates no Azure
  resource. After the fix is on `main` and the host is set up at that commit, no alias is needed.
  The first run still proved three things: the Planner's real model call through Entra ID works from the VM
  (2 attempts, $0.0074), the upload with the container-scoped Blob role works (9 files), and the active-run
  marker was written and removed.
- Run 2 (`20261108-214751-...`): status `failed` in `case_setup` after 3 attempts, 154 s, $0.0287, 106,219 tokens
  in, 21,238 out. Planner ok after 2 attempts, geometry ok after 1, mesh ok after 3, case setup failed 3 of 3.
  No Scorer comparison exists (the run ended before the Solver Runner). The upload reported 98 files and the
  blobs were confirmed read-only from the laptop. Details and the triage are in the journal entry
  `docs/journal/2026-11-08-m1-task-23-first-live-run.md`.

## 6. Deallocate

```powershell
infra/scripts/vm-down.ps1
```

Skip this only if Task 23 continues immediately. The idle shutdown (30 minutes) and the daily shutdown
(23:00 Europe/Warsaw) are the safety net, not the plan.

Observed: the VM ran about 20 minutes in the session (start to deallocate), about $0.07; no LLM cost. The
benchmark lookup was checked from the VM: `cfdagent run --benchmark <unknown-id>` run from `/opt/cfdagent-src`
lists the five known benchmarks, and from any other directory it fails with `benchmark root benchmarks does
not exist`.
