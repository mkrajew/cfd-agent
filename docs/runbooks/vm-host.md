# Runbook: VM host setup, metadata firewall, SSH limits, shutdown deferral

Applies to the project VM `vm-cfdagent`. It covers `scripts/vm-host-setup.sh` and the files it installs from
`infra/vm/`, the container-scoped blob role in `infra/modules/roles.bicep`, and how to verify and roll back
each of them. Read `infra/CLAUDE.md` first: creating, changing or deleting Azure resources needs the owner's
explicit approval; starting and deallocating the VM inside the approved envelope does not.

Values in angle brackets are placeholders. Nothing here is committed with real values: take them from the
deployment outputs or from your own machine.

## What is applied how

`customData` (cloud-init) is create-time only, so the files below reach a running VM through
`scripts/vm-host-setup.sh`, which installs them from its checkout of the repository. New VMs get the same files
from `infra/vm/cloud-init.yaml` (through `infra/modules/vm.bicep`), so both paths end in the same state.

| Item | Running VM (this runbook) | New VM |
|---|---|---|
| Host environment `/opt/cfdagent-host`, `/etc/cfdagent/env`, `/usr/local/bin/cfdagent` | `vm-host-setup.sh` | not part of cloud-init; run `vm-host-setup.sh` after the first boot |
| Metadata firewall (`cfdagent-iptables` script and unit) | `vm-host-setup.sh` | cloud-init |
| sshd drop-in `20-cfdagent-ssh-extra.conf` | `vm-host-setup.sh`, guarded by `sshd -t` | cloud-init |
| `idle-shutdown.sh --daily`, daily service, daily timer with 25 attempts | `vm-host-setup.sh` | cloud-init |
| State and runs directories owned by the admin user | `vm-host-setup.sh` | cloud-init |
| Container-scoped blob role, Entra-only AI account | Bicep deployment (owner approval) | Bicep deployment |

The cost of everything in this runbook is zero: no Azure resource is created. The VM bills $0.208/h while it
runs (about 0.5 h for the setup and the checks, about $0.10).

## 1. Prepare

1. The Task 9 commit (or the ref you want) must be pushed to the public repository, and the image tag must be
   published if you want the pull to succeed (tags are `sha-<7 characters>`, `latest` and release versions).
2. Start the VM: `infra/scripts/vm-up.ps1`. Wait until `ssh cfdagent true` works.
3. Look up the two endpoints (read-only):
   - `<llm-endpoint>`: deployment output `aiEndpoint` (`https://<account>.services.ai.azure.com/openai/v1/`).
   - `<storage-url>`: `https://<storageAccountName>.blob.core.windows.net`, with `storageAccountName` from the
     deployment outputs.

## 2. Run the setup script

Over SSH (keep this session open until section 4 is done):

```bash
scp scripts/vm-host-setup.sh cfdagent:/tmp/vm-host-setup.sh
ssh cfdagent sudo bash /tmp/vm-host-setup.sh <repo-ref> \
    --llm-endpoint <llm-endpoint> --storage-account-url <storage-url>
```

`<repo-ref>` is a commit id, a branch or a tag. Optional: `--image <tag-or-reference>` (default `sha-<7>` for
a commit id, otherwise `latest`), `--user <admin-user>` (default `azureuser`), `--shutdown-time <HHMM>` and
`--timezone <zone>` (defaults 2300 and Europe/Warsaw, as in `main.bicep`).

Alternative without SSH, from the owner's machine (`az vm run-command` runs as root and is a write call on the
VM, so it needs the same approval as any `az` call that changes something):

```powershell
az vm run-command invoke --resource-group rg-cfdagent --name vm-cfdagent --command-id RunShellScript `
    --scripts "@scripts/vm-host-setup.sh" `
    --parameters "<repo-ref>" "--llm-endpoint" "<llm-endpoint>" "--storage-account-url" "<storage-url>"
```

The script is idempotent: run it again at any time, for example at the final commit of a milestone (Task 22).
It prints a summary at the end. Exit status 3 means everything is in place except the image pull.

What it does, in order: installs `git` and `python3-venv`; clones or updates `/opt/cfdagent-src`; builds
`/opt/cfdagent-host` and pip-installs the repository (host dependencies only, no `toolchain` extra); writes
`/etc/cfdagent/env` (mode 0640, group of the admin user); creates `~/runs` and `/var/lib/cfdagent`; installs the
`/usr/local/bin/cfdagent` wrapper; installs the scripts and units and enables the firewall and both timers; installs the
sshd drop-in (only after `sshd -t` accepts it; the previous drop-in is put back if not); pulls the image.
Until the CLI of Task 21 is merged the wrapper points to a console script that does not exist yet and the
script says so; run it again with a later ref.

## 3. Verify

### 3.1 Metadata firewall (S2), once

The executors run with `--network none`; the DOCKER-USER rules are the second layer for any container that
does get a bridge network. The host itself is not affected: the orchestrator reaches IMDS for its tokens.

```bash
sudo cfdagent-iptables status                 # all metadata rules present in DOCKER-USER
sudo iptables -S DOCKER-USER                  # 2 ACCEPT (udp/tcp 53 to the resolver), 2 DROP, then Docker's RETURN
sudo cfdagent-idle-shutdown --check           # expects "check ok": the host can still use IMDS and ARM
```

The check from a throwaway bridge container. Run it first with the firewall stopped, to prove that the test
can fail, then with the firewall running:

```bash
IMAGE=$(. /etc/cfdagent/env && echo "$CFDAGENT_IMAGE")
probe() {
  docker run --rm --network bridge "$IMAGE" python -c '
import socket
for host, port in (("169.254.169.254", 80), ("168.63.129.16", 80), ("168.63.129.16", 32526), ("168.63.129.16", 53), ("1.1.1.1", 443)):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        print(host, port, "REACHABLE")
    except OSError as e:
        print(host, port, "not reachable:", e)
'
}
sudo systemctl stop cfdagent-iptables; probe     # control: the first lines say REACHABLE (IMDS answers)
sudo systemctl start cfdagent-iptables; probe    # expected: 169.254.169.254:80, 168.63.129.16:80 and :32526 not reachable (timed out); 168.63.129.16:53 and 1.1.1.1:443 REACHABLE
```

The WireServer ports and the resolver may behave differently from the expectation above; what must hold is
that both metadata addresses are unreachable on the HTTP ports, that `1.1.1.1:443` stays reachable (so the
test is not just a dead network) and that name resolution inside a bridge container still works. Record the
two outputs in the pull request.

Persistence: `systemctl is-enabled cfdagent-iptables` prints `enabled`. After `sudo systemctl restart docker`
the status check must still pass (the unit is `Requires=docker.service`, so it is restarted with Docker).
Containers started with `--network host` bypass these rules; the sandbox never uses them.

### 3.2 SSH limits (S8)

```bash
sudo sshd -T | grep -Ei 'maxauthtries|allowusers|passwordauth|permitroot'   # 3, azureuser, no, no
sudo journalctl -u ssh --since -1h | tail -n 20
sudo fail2ban-client status sshd
```

Open a second `ssh cfdagent` session and confirm that it works before closing the first one. `AllowUsers` lets
only the admin user in; if the check ever locks you out, use the rollback below through `az vm run-command`.
The `docker` group is root-equivalent, so the key passphrase is the real access control.

### 3.3 Shutdown deferral (E)

```bash
systemctl list-timers 'cfdagent-*'
systemctl cat cfdagent-daily-shutdown.timer | grep -c '^OnCalendar='          # 25
systemctl cat cfdagent-daily-shutdown.service | grep ExecStart                 # ... --daily
sudo -u <admin-user> touch /var/lib/cfdagent/active-run
sudo cfdagent-idle-shutdown --daily --dry-run       # deferred: active run
rm /var/lib/cfdagent/active-run
sudo cfdagent-idle-shutdown --daily --dry-run       # dry run: would deallocate (daily shutdown)
```

`cfdagent run` creates `/var/lib/cfdagent/active-run` and removes it when the run ends. A marker older than
4 hours is ignored, so a crashed run cannot keep the VM up for more than 4 hours past the shutdown time. The
daily timer fires at 23:00 and then every 10 minutes until 03:00 (Europe/Warsaw), so a deferred shutdown
happens within 10 minutes after the run has ended; at 03:00 the last attempt only defers if the marker is
younger than 4 hours. The 30-minute idle shutdown is unchanged and already treats a running `cfdagent` as busy.

### 3.4 Host environment

```bash
cfdagent --help || ls /opt/cfdagent-host/bin       # works once Task 21 is merged
stat -c '%a %U:%G' /etc/cfdagent/env               # 640 root:<admin group>
docker image ls ghcr.io/mkrajew/cfd-agent
```

Then deallocate: `infra/scripts/vm-down.ps1` (or wait for the idle shutdown).

## 4. Entra-only redeploy and container-scoped blob role (S7)

This part changes Azure resources: show the owner the `what-if` and this cost note first.

- Cost: no new billable resource (role assignments and the account setting are free); expected change in the
  monthly estimate: none.
- Order: (1) `infra/scripts/deploy.ps1 -WhatIfOnly` (pass `budgetStartDate=2026-10-01T00:00:00Z` if the date is
  after 1 November), (2) owner approval, (3) deploy, (4) verify, (5) with a second approval, delete the old
  account-scoped assignment.
- Expected what-if: the AI account changes `disableLocalAuth` to true; one new role assignment (Storage Blob
  Data Contributor for the VM identity, scope `.../containers/runs`); nothing else. The old assignment at the
  storage account scope is **not** removed by the deployment, because its name is derived from the scope; it
  stays until it is deleted explicitly (step 5). Until then the container scoping has no effect.
- **Stop and ask if the what-if shows the VM, its disk, its NIC or its IP being replaced, or an error about
  `customData`.** The VM module embeds the cloud-init files, and they changed in this task (new firewall,
  SSH and timer files), so a full redeploy of `main.bicep` modifies `osProfile.customData` of the existing VM,
  which Azure does not allow. If that happens, do not recreate the VM; deploy only the pieces that matter, as
  group-scope deployments of `infra/modules/roles.bicep` (parameters `vmName`, `vmPrincipalId` from
  `az vm show --query identity.principalId`, `storageAccountName`, `aiAccountName`, `ownerObjectId`) and
  `infra/modules/foundry.bicep` (or `az cognitiveservices account update` for `disableLocalAuth`), after the
  owner approved that variant.

Verification after the deployment (read-only unless noted):

```powershell
az cognitiveservices account show -g rg-cfdagent -n <ai-account> --query properties.disableLocalAuth   # true
az cognitiveservices account keys list -g rg-cfdagent -n <ai-account>                                  # must fail
az role assignment list --assignee <vm-principal-id> --all --query "[].{role:roleDefinitionName,scope:scope}"
```

On the VM (identity token, no keys): a `gpt-oss-120b` call through `cfdagent`'s LLM client or the spike script
still works; a blob upload to `runs` works; a write to any other container is denied
(`AuthorizationPermissionMismatch`). Create no test container just for this: use an existing one, or leave the
denial check to the first run that touches only `runs`.

Step 5 (after approval): delete the assignment whose scope is the storage account and whose role is
`Storage Blob Data Contributor` for the VM principal: `az role assignment delete --assignee <vm-principal-id>
--role "Storage Blob Data Contributor" --scope <storage-account-resource-id>`. Check with the list command
above that only the `runs` container assignment remains.

## 5. Rollback

| Item | Command (on the VM, with sudo) |
|---|---|
| sshd drop-in | `rm /etc/ssh/sshd_config.d/20-cfdagent-ssh-extra.conf; sshd -t && systemctl reload ssh` |
| Firewall | `systemctl disable --now cfdagent-iptables` (the stop action removes the rules); remove `/etc/systemd/system/cfdagent-iptables.service` and `/usr/local/sbin/cfdagent-iptables` for a full removal, then `systemctl daemon-reload` |
| Deferral | edit `/etc/systemd/system/cfdagent-daily-shutdown.service` so that ExecStart ends in `--force`, `systemctl daemon-reload`; the timer may keep its 25 entries |
| Host environment | `rm -r /opt/cfdagent-host /opt/cfdagent-src /usr/local/bin/cfdagent /etc/cfdagent` |
| Blob role | re-create the account-scoped assignment (Bicep: `scope: storage` in `roles.bicep`); needs approval |
| Entra-only | `disableLocalAuth=false` in the parameters, redeploy; needs approval |

If an SSH change locks you out, the VM can still be reached through `az vm run-command invoke` (no SSH) to
remove the drop-in.
