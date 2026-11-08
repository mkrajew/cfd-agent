# Runbook: M1 dry run and first live run on the VM (DRAFT)

Status: **draft.** Commands are exact; every `TODO(observed)` is filled in after the first VM session. Read
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

TODO(observed): time from `vm-up` to a working SSH session.

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

TODO(observed): form used, `vm-host-setup.sh` exit status, image tag, image size (`docker image ls`).

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

TODO(observed): the table printed on the VM and its duration.

## 4. Dry run with the scripted backend

```bash
cd /opt/cfdagent-src
set -a; . /etc/cfdagent/env; set +a
/opt/cfdagent-host/bin/python scripts/replay_scripted_run.py tests/fixtures/scripted/cavity_pipeline.json
```

The run uses the docker sandbox and the real upload. Expected: status `completed`, Scorer `passed: True`,
the last upload line `upload: done, <N> file(s)`, exit status 0. The summary shows the run id, the wall time
per stage, the per-criterion errors, the run directory size and the blob-listing command.

TODO(observed): run id, per-stage wall times, total wall time, Scorer errors per criterion, run directory
size, number of uploaded files.

### Check the Blob contents (read-only)

From the laptop (`az login` with the data-reader role) or the VM:

```powershell
az storage blob list --auth-mode login --account-name <account> --container-name runs --prefix <run-id>/ --query "[].name" -o tsv
```

The folder `<run-id>/` must hold `summary.md`, `run_record.json`, `results/comparison.json`, `casespec.json`,
`analysis.md` and the logs. The `az` call is read-only; every `az` call still triggers a permission prompt.

TODO(observed): the listing (names and count).

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

## 6. Deallocate

```powershell
infra/scripts/vm-down.ps1
```

Skip this only if Task 23 continues immediately. The idle shutdown (30 minutes) and the daily shutdown
(23:00 Europe/Warsaw) are the safety net, not the plan.

TODO(observed): total VM uptime of the session and its cost (uptime x $0.208/h).
