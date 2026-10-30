# infra/CLAUDE.md

Rules for anything under `infra/`: Bicep, VM scripts, budget and shutdown policy. Read this before changing it. The source of truth is sections 3, 7 and 14 of `docs/design/2026-10-28-cfd-agent-design.md`.

## Approval rule

**Creating, changing or deleting Azure resources always needs the owner's explicit approval in chat first.** Every `az` invocation triggers a permission prompt in `.claude/settings.json`; that prompt is a safety net that the owner answers, not a substitute for asking first. Read-only commands (`list`, `show`, `what-if`, `validate`) are fine.

Inside an owner-approved spending envelope, starting, stopping or deallocating the project VM and running tests or benchmarks need no extra chat approval. Creating, changing or deleting resources, and any campaign estimated above $5, always need explicit chat approval first.

## Subscription limits (verified 2026-10-27)

- Subscription "Azure for Students", **$100 credit**.
- Allowed regions (policy): `italynorth` (default), `spaincentral`, `belgiumcentral`, `denmarkeast`, `austriaeast`. Anything else is rejected.
- Compute quota: **6 vCPU per region**, **at most 4 vCPU per VM family**; spot/low-priority 3 vCPU. The VM is `Standard_D4as_v4` (4 vCPU, 16 GB) in `belgiumcentral`, Ubuntu 24.04 (owner decision 2026-10-28: italynorth has no usable 4-vCPU size, `Standard_F4s_v2` is unavailable there and in most allowed regions).
- **No GPU**: quota 0 for every GPU family; increase requests were rejected. Do not design anything that needs a GPU.
- LLMs run serverless in Azure AI Foundry (pay per token). `gpt-oss-120b` is verified working; `gpt-oss-20b` is not deployable.
- Resource group `rg-cfdagent` in `italynorth`. The probe group `rg-cfdagent-probe` is deleted only after the IaC-managed environment exists and the owner approves.
- Azure CLI is not on PATH: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`.

## Mandatory safeguards

- **Daily auto-shutdown** of the VM (time as a parameter, Europe/Warsaw). `Microsoft.DevTestLab/schedules` is not offered in any allowed region, so it is a systemd timer on the VM.
- **Idle shutdown:** on-VM timer deallocates the VM after 30 minutes with no SSH session and no running `cfdagent`; its identity may deallocate only that VM.
- **Budget:** subscription budget of $100 with alerts at 25, 50, 75 and 90 percent; the notification address is a parameter, never a literal in the repository.
- SSH (22) is open to the Internet (owner decision 2026-10-29, the owner changes networks and uses a VPN); compensated by key-only authentication (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin no`), `MaxAuthTries 3`, `AllowUsers <admin user>` and `fail2ban` on the VM. No other inbound port is opened.
- Docker containers cannot reach the Azure metadata endpoints (IMDS, WireServer) over a bridge network: `cfdagent-iptables.service` (DOCKER-USER chain); executors also run with `--network none`.
- The VM identity writes blobs only in the `runs` container (role assigned at container scope).
- Keyless access: system-assigned managed identity to Foundry and Blob (Entra ID). A key in Key Vault only if a model rejects Entra. Least privilege on every role assignment.
- No secrets, keys, IP addresses, subscription IDs or e-mail addresses in files; use parameters or environment variables.

## Cost estimate rule

Every infra change is accompanied by a cost estimate in the commit or PR description: hourly and monthly cost of each new or changed resource, taken from the Azure retail price API for the resource's own region (VM, network and storage: `belgiumcentral`; AI Services: `italynorth`), and its effect on the envelope.

Reference prices: VM `Standard_D4as_v4` (belgiumcentral) $0.208/h; `gpt-oss-120b` $0.15 per 1M input tokens and $0.60 per 1M output tokens.

Envelopes (owner's decision, 2026-10-28): M0-M2 development at most $30; M3 campaign at most $45; reserve about $25 is never spent without a separate decision. Track spend from run records and report it at each milestone.
