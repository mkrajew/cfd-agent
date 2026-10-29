# Infrastructure

Bicep for the cfd-agent research environment. One subscription-scope deployment creates the resource
group `rg-cfdagent` and everything in it. Read `infra/CLAUDE.md` first: **nothing is deployed without the
owner's explicit approval**, and every change carries a cost estimate.

## What is deployed

| Resource | Details |
|---|---|
| Resource group | `rg-cfdagent` in `italynorth` |
| Network (`vmLocation`) | VNet `vnet-cfdagent`, NSG `nsg-cfdagent` (one inbound allow rule: SSH 22 from the Internet; everything else denied), Standard static public IP `pip-cfdagent` |
| VM (`vmLocation`) | `vm-cfdagent`, `Standard_D4as_v4` (4 vCPU, 16 GB), Ubuntu 24.04 LTS (Canonical `ubuntu-24_04-lts` / `server`, Gen2), SSH key only, system-assigned identity, 64 GiB StandardSSD_LRS OS disk |
| Daily shutdown | systemd timer on the VM, 23:00 `Europe/Warsaw` (parameters `shutdownTime`, `shutdownTimeZone`; applied only when the VM is created, see "Redeploying") |
| Idle shutdown | systemd timer every 5 min; deallocates after 30 idle minutes (see below) |
| Storage (`vmLocation`) | StorageV2, LRS, Hot, shared keys disabled, TLS 1.2, no public blob access, container `runs`, lifecycle: Cool after 30 days |
| AI Services | account `cfdagent-ai-<hash>` (custom subdomain) with deployments `gpt-oss-120b` and `Llama-3.3-70B-Instruct` (GlobalStandard, pay per token) |
| Roles | VM identity: Storage Blob Data Contributor (storage), Cognitive Services OpenAI User (AI account), custom `cfdagent VM Deallocate` (read + deallocate, assigned on the VM only). Owner: Storage Blob Data Reader (storage), Cognitive Services OpenAI User (AI account) |
| Budget | subscription budget `budget-cfdagent`, 100 USD per month, actual-cost alerts at 25/50/75/90 % |

Outputs: `vmPublicIp`, `aiEndpoint` (OpenAI v1 base URL `https://<account>.services.ai.azure.com/openai/v1/`),
`storageAccountName`, `resourceGroupName`, `vmName`.

### Decisions that differ from the first sketch

- **SSH is open to the Internet (owner decision 2026-10-29).** The owner switches networks and uses a university
  VPN, so a source-IP rule is not workable. The NSG allows TCP 22 from `Internet`; there is no `ownerIp`
  parameter. Compensating controls, installed by cloud-init: `PasswordAuthentication no`,
  `KbdInteractiveAuthentication no` and `PermitRootLogin no` in
  `/etc/ssh/sshd_config.d/10-cfdagent-hardening.conf`, and `fail2ban` with an sshd jail (5 failures in 10 minutes
  ban the address for 1 hour). Authentication is by the ed25519 key only. Check with
  `sudo fail2ban-client status sshd` and `sudo sshd -T | grep -Ei 'passwordauth|kbdinteractive|permitroot'`.

- **VM region and size (owner decision 2026-10-28).** `italynorth` has no usable 4-vCPU size for this
  subscription (`Standard_F4s_v2` is `NotAvailableForSubscription`, the available v7 sizes have quota 0;
  checked with `az vm list-skus --all` and `az vm list-usage`). Network, VM and storage therefore use
  `vmLocation` (default `belgiumcentral`) with `Standard_D4as_v4`. The resource group and the AI account
  stay in `italynorth`. Alternatives that were considered: `Standard_F4s_v2` in `denmarkeast` (0.27 USD/h),
  `Standard_D4s_v4` in `spaincentral` (0.214 USD/h).
- **Daily shutdown on the VM.** `Microsoft.DevTestLab/schedules` is not offered in any allowed region, so the
  daily shutdown is a systemd timer (`cfdagent-daily-shutdown.timer`) that runs the same deallocate call
  with `--force`.
- **`qwen3-32b` is not deployed.** ARM preflight rejects it on an AIServices account in `italynorth`
  (`GlobalStandard` and `DeveloperTier` both "not supported by the model"). Add it to `modelDeployments`
  if a later model list shows it deployable.
- **Local auth on the AI account stays enabled** (`disableLocalAuth = false`) until Entra access is verified
  for each deployment (M0 spike). `Cognitive Services OpenAI User` may not cover non-OpenAI models such as
  Llama; if the spike shows that, add `Cognitive Services User` for the VM identity.

## Parameters from the environment

`main.bicepparam` reads these with `readEnvironmentVariable()`. Nothing personal is committed.

| Variable | Meaning | `deploy.ps1` default |
|---|---|---|
| `CFDAGENT_SSH_PUBLIC_KEY` | SSH public key of the admin user | contents of `~/.ssh/id_cfdagent.pub` |
| `CFDAGENT_OWNER_OBJECT_ID` | owner's Entra object id | `az ad signed-in-user show --query id -o tsv` |
| `CFDAGENT_BUDGET_EMAIL` | budget alert address | none, must be set |

## Commands

Azure CLI is not on PATH on the owner's machine: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`
(the scripts find it).

```powershell
# lint (no Azure access needed)
az bicep build --file infra/main.bicep

# preview, creates nothing
$env:CFDAGENT_BUDGET_EMAIL = '<address>'
infra/scripts/deploy.ps1 -WhatIfOnly

# deploy, only after the owner approved the what-if and the cost estimate
infra/scripts/deploy.ps1

# daily use
infra/scripts/vm-up.ps1              # start, print the ssh command
infra/scripts/vm-down.ps1            # deallocate
```

Copy `infra/scripts/ssh-config.example` into `~/.ssh/config` and fill in the `vmPublicIp` output to use
`ssh cfdagent`.

## Idle shutdown

`infra/vm/idle-shutdown.sh` is installed as `/usr/local/sbin/cfdagent-idle-shutdown` and run every 5 minutes
by `cfdagent-idle-shutdown.timer`. The VM is busy when `who` lists a session, an established SSH
connection exists (covers Remote-SSH, which has no tty), a `cfdagent` process runs, or a container from
`ghcr.io/mkrajew/cfd-agent` runs. After 30 consecutive idle minutes (start time in
`/var/lib/cfdagent/idle-since`, never earlier than the boot) it calls the ARM `deallocate` action with the
managed-identity token from IMDS. The identity may only read and deallocate this one VM.

The decision logic is tested without Azure by `tests/unit/test_idle_shutdown.py`, which runs the script with
stub commands (`who`, `ss`, `pgrep`, `docker`, `curl`, `wall`) first on `PATH`, plus `--dry-run`. The test is
skipped on Windows and runs in CI and in the Linux image. `shellcheck infra/vm/*.sh` runs in CI and in
pre-commit.

`cfdagent-idle-shutdown --check` gets an IMDS token and reads the VM through ARM. It proves the identity, the
role assignment and the network path without deallocating anything. Every deallocation (idle, `--force` from the
daily timer) is tried 3 times with a growing pause; a failure is logged (`IMDS token failed (HTTP n)` or
`ARM returned n: <body>`, never the token), the script exits non-zero and keeps its state, so the next timer run
tries again. Read the log with `journalctl -u cfdagent-idle-shutdown -u cfdagent-daily-shutdown`.

## Verifying a fresh deployment

Over SSH on the VM (`ssh cfdagent`), after the first boot:

```bash
systemctl list-timers 'cfdagent-*'          # both timers listed with a next run time
sudo cfdagent-idle-shutdown --dry-run       # prints the decision, no Azure call
sudo cfdagent-idle-shutdown --check         # expects: check ok (role assignments may take a few minutes)
sudo fail2ban-client status sshd            # sshd jail active
sudo sshd -T | grep -Ei 'passwordauth|kbdinteractive|permitroot'   # no, no, no
docker image ls ghcr.io/mkrajew/cfd-agent   # pre-pulled image, if the package is public
sudo tail -n 50 /var/log/cloud-init-output.log   # package install, timers, image pull
cloud-init status --long
```

From the owner's machine: `az vm get-instance-view` shows the VM running,
`ssh cfdagent docker run --rm ghcr.io/mkrajew/cfd-agent:latest icoFoam -help` works, and
`az consumption budget show --budget-name budget-cfdagent` exists. Then `infra/scripts/vm-down.ps1` must end in
`VM deallocated`.

## Redeploying and tearing down

- **`customData` is create-time.** cloud-init runs once and Azure does not allow changing `customData` on an existing
  VM. A redeploy that changes `idle-shutdown.sh`, the units, `shutdownTime` or `shutdownTimeZone` therefore fails
  or has no effect on the running VM. Either copy the new files over SSH (`scp`, then `sudo systemctl
  daemon-reload && sudo systemctl restart cfdagent-*.timer`) or recreate the VM (delete it and its OS disk, then
  deploy again; the static IP keeps its address if the IP resource is kept).
- **Soft-deleted AI account.** Deleting the resource group soft-deletes the Cognitive Services account and keeps
  its custom subdomain reserved. Before deploying again, purge it:
  `az cognitiveservices account list-deleted`, then
  `az cognitiveservices account purge --location italynorth --resource-group rg-cfdagent --name <account>`.
  Otherwise the next deployment fails with a subdomain conflict. Deleting needs the owner's approval.
- **Budget start date** defaults to the first day of the current month at deployment time, so a later redeploy moves
  it to the new month.

## Cost estimate (Azure retail prices API, belgiumcentral, checked 2026-10-28)

| Item | Price | Per month |
|---|---|---|
| VM `Standard_D4as_v4` Linux running (belgiumcentral) | 0.208 USD/h | only while running |
| OS disk StandardSSD E6 (64 GiB) | 4.80 USD/month plus about 0.0026 USD per 10K operations | 4.80 |
| Public IP, Standard static IPv4 | 0.005 USD/h, **billed also while the VM is deallocated** | 3.65 |
| Storage Hot LRS | about 0.02 USD/GB/month | under 0.2 for the first GB-scale runs |
| VNet, NSG, roles, budget, AI account | free | 0 |
| Foundry `gpt-oss-120b` | 0.15 USD / 1M input tokens, 0.60 USD / 1M output tokens | per token |
| Foundry `Llama-3.3-70B-Instruct` (global) | 0.71 USD / 1M input tokens, 0.71 USD / 1M output tokens | per token |

Fixed cost while the environment exists: about 8.7 USD/month. To stop it completely, delete the resource
group (needs the owner's approval; the public IP changes on the next deployment).
