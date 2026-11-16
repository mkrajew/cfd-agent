# Runbook: M2 VM refresh and scripted replay (Task 16)

Status: executed on 2026-11-16 at commit `052833f5489e8988d1ccd0154300c16974d5605c` (`main` after the merge of
M2 Tasks 2-15). Read `infra/CLAUDE.md`, `docs/runbooks/vm-host.md` and `docs/runbooks/m1-first-run.md` first; this
runbook only adds what is new for M2 and the numbers observed. Nothing here creates, changes or deletes an Azure
resource. The VM bills about $0.208/h while it runs; no model is called (scripted conversation), so the cost is VM time
only.

Placeholders in angle brackets come from the deployment outputs or from your own machine; nothing real is committed here.

## 1. Start the VM

```powershell
infra/scripts/vm-up.ps1        # or: az vm start --resource-group rg-cfdagent --name vm-cfdagent
```

First check that `az` is signed in to the account that owns `rg-cfdagent` (the Azure for Students subscription). A
different signed-in account fails at the first call with `AuthorizationFailed`; fixing it is an `az login` by the
owner. Observed: `az vm start` took 106 s.

The session was driven without SSH through `az vm run-command invoke` (form 2.2 of `docs/runbooks/vm-host.md`): each
script is a local file outside the repository passed with `--scripts "@<file>"`. Run-command scripts run as root, so
the commands that write to the run directory go through `sudo -u azureuser -H bash -c '...'`, which keeps
`/home/azureuser/runs` owned by the admin user. Run-command sessions do not count for the 30-minute idle shutdown:
do not leave the VM waiting between steps.

## 2. Host setup at the merged commit

The image `sha-052833f` was published by the CI of that commit (all seven jobs green). The wrapper is the one from
`docs/runbooks/vm-host.md` section 2.2 with `REF` set to the full 40-character commit id, `<llm-endpoint>` from the
deployment output `aiEndpoint` and `<storage-url>` built from `storageAccountName` (both read with
`az deployment group show -g rg-cfdagent -n foundry|storage --query properties.outputs...`).

Observed: first attempt, exit status 0, 63 s including the image pull (`Downloaded newer image for
ghcr.io/mkrajew/cfd-agent:sha-052833f`). `/etc/cfdagent/env` then holds `CFDAGENT_COMMIT` and `CFDAGENT_IMAGE` for that
commit.

## 3. Verify the sandbox

```bash
cd /opt/cfdagent-src
set -a; . /etc/cfdagent/env; set +a
/opt/cfdagent-host/bin/python scripts/verify_sandbox.py
```

Observed: **7/7 PASS**, exit 0, 35 s (about 63 s through run-command including its start-up). Same rows as in
`docs/runbooks/m1-first-run.md` section 3: no network, no credentials in the environment, read-only root, `#codeStream`
refused, `mpirun -np 4 --use-hwthread-cpus`, a timeout leaves no container, the run directory is the only mount.

## 4. Scripted replay of the M2 pipeline

```bash
cd /opt/cfdagent-src
set -a; . /etc/cfdagent/env; set +a
/opt/cfdagent-host/bin/python scripts/replay_scripted_run.py tests/fixtures/scripted/cavity_pipeline.json
```

Run it as the admin user (`sudo -u azureuser -H bash -c '...'` from a run-command script). Docker sandbox, real
Scorer, real upload; the model is the scripted backend. The script exits 0 only when the status is `completed`, the
Critic's verdict is `success`, the comparison passed and the upload finished.

Observed (exit 0, 62 s including start-up): run `20261116-153700-lid-driven-cavity-flow-at-re-100`.

- Status `completed`, wall time 56.8 s, cost $0.0111 (scripted token counts priced as the primary model).
- Stages (one attempt each): planner 0.0 s, geometry 5.8 s, mesh 4.0 s, case_setup 3.4 s, solver_runner 41.0 s,
  analyst 2.5 s, critic 0.0 s.
- Critic: `success`, 4 of 4 checks passed, no send-backs. Scorer `passed: True`: `u_vertical_centreline` error
  0.00488, `v_horizontal_centreline` error 0.00874, both within the thresholds. Critic agreed with the Scorer: yes.
- Run directory 35.4 MiB on disk; upload line `upload: done, 254 file(s)` (M1 dry run: 253).

### Blob contents (read-only, from the laptop)

```powershell
az storage blob list --auth-mode login --account-name <account> --container-name runs --prefix <run-id>/ --query "[].name" -o json
```

Observed: 254 blobs under the run prefix, matching the upload line, 29.7 MB in all. Top-level entries include
`analysis.md`, `casespec.json`, `boundary_manifest.json`, `case.tar.zst`, `attempts/<stage>-<n>/` per stage and
`.sandbox/` (request and response files of the sandbox calls).

### The input path `../../casespec.json` (Task 9) in a real sandbox

The scripts of the scripted fixture do not read `casespec.json`, so the replay alone does not exercise this path. It
was verified separately on the VM, with the same `SandboxClient` in `docker` mode that a run uses: a run directory
with `casespec.json` at its root and a script `attempts/geometry-1/probe.py` that prints
`open('../../casespec.json').read()`, run through the `run_script` operation with `cwd` `attempts/geometry-1`.
Observed: the call succeeded and the script printed the file's content, so a generated geometry or mesh script can
read the plan from its attempt directory as the prompts tell it to. The probe was an ad-hoc script, not committed.

## 5. Cost of the fixed items (read-only)

Cost Management, scoped to `rg-cfdagent`, actual cost 2026-10-01 to 2026-11-16, grouped by resource (the API
reports EUR):

| Resource | Cost (EUR) | Note |
|---|---|---|
| Foundry account (model tokens) | 2.47 | variable: M1 live runs |
| OS disk `osdisk-cfdagent` | 2.67 | Standard SSD, 64 GB, billed also while the VM is deallocated |
| Public IP `pip-cfdagent` | 1.94 | Standard SKU, static, billed all the time |
| VM `vm-cfdagent` | 1.91 | compute, only while running |
| Storage account | 0.07 | Standard LRS, Hot |
| VM bandwidth | 0.002 | |
| Total | 9.06 | |

The fixed part, which does not depend on use: the disk about $4.8/month (Standard SSD E6 in `belgiumcentral`, list
price), the static public IP about $3.65/month ($0.005/h, Standard SKU) and the storage account about $0.1/month,
together about $8.6/month, in line with the $8.70 of the plan. The disk size cannot be reduced in place (M1 observed 48 GB free
of 64 GB, so a 32 GB disk would save about $2.4/month, but it means a new VM). The one saving that does
not need a rebuild is releasing the public IP while the project is idle (about $3.65/month), at the price of losing
SSH: access would go through `az vm run-command` only, and a new address would have to be set in the SSH config. It
is the owner's decision; nothing was changed.

## 6. Deallocate

```powershell
az vm deallocate --resource-group rg-cfdagent --name vm-cfdagent     # or infra/scripts/vm-down.ps1
az vm get-instance-view -g rg-cfdagent -n vm-cfdagent -o json       # statuses: "VM deallocated"
```

Observed: deallocation took 33 s and the status read `VM deallocated`. The VM ran about 10 minutes in the session
(start to deallocate), about $0.04, no model cost. Remember to start it again (`vm-up.ps1`) and refresh the host
setup to the then-current commit before the live series of Task 17: the script is safe to re-run at any ref.
