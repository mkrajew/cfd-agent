# ADR-0003: Infrastructure deviations from the baseline

- **Status:** Accepted, 2026-10-29
- **Deciders:** the owner

## Context

ADR-0001 makes the approved design document the architecture baseline and requires a new ADR when an architectural decision changes. While deploying the infrastructure for M0, the Azure subscription turned out to differ from what the design assumed. Each deviation below was decided by the owner (or, for the hardening items, accepted by the owner) and the design document was updated alongside. This ADR collects them in one place.

## Decision

1. **VM size and region.** The VM is `Standard_D4as_v4` (4 vCPU, 16 GB, $0.208/h) in `belgiumcentral`, not `Standard_F4s_v2` in `italynorth`. `Standard_F4s_v2` is `NotAvailableForSubscription` in `italynorth` and unrestricted only in `denmarkeast` ($0.27/h); the v7 sizes in `italynorth` have quota 0. Network, VM and storage follow the VM (`vmLocation`); the resource group and the AI Services account stay in `italynorth`, where the models are available. Owner decision 2026-10-28.
2. **Daily shutdown is a systemd timer.** `Microsoft.DevTestLab/schedules` is not offered in any allowed region. A timer on the VM (23:00 Europe/Warsaw, parameters `shutdownTime` and `shutdownTimeZone`) calls the ARM deallocate action with `--force`. The time applies only when the VM is created, because `customData` cannot change on an existing VM. Owner decision 2026-10-28.
3. **Idle shutdown on the VM.** A timer every 5 minutes deallocates the VM after 30 consecutive idle minutes. The VM identity has a custom role limited to read and deallocate on that one VM. Verified live on 2026-10-29: the VM deallocated itself.
4. **SSH open to the Internet, key only.** The design said SSH only from the owner's address. The owner changes networks and uses a university VPN, so the NSG allows port 22 from any source (`*`; the `Internet` service tag excludes Azure-owned ranges). Compensating controls: key-only login with password, keyboard-interactive and root login disabled in an sshd drop-in, and `fail2ban` with an sshd jail. No other inbound port. Owner decision 2026-10-29. Open items for M1: `MaxAuthTries`, `AllowUsers`, watching `journalctl -u ssh`.
5. **Models.** Two serverless models are deployed (`gpt-oss-120b`, `Llama-3.3-70B-Instruct`); `qwen3-32b` is rejected by ARM on this account and was not replaced (ADR-0002).
6. **AI account authentication.** The Bicep default is now `disableLocalAuth = true` (Entra ID only), and storage sets `allowCrossTenantReplication = false`. Entra works for both models in the spike, and both `gpt-oss-120b` and `Llama-3.3-70B-Instruct` answered through the VM's managed identity on 2026-10-29 (verified, no keys). The deployed account keeps key authentication until an owner-approved redeploy. A redeploy after 1 November 2026 must pass `budgetStartDate=2026-10-01T00:00:00Z`.

## Consequences

- Costs: fixed about $8.70 per month (public IP, disk, storage) plus $0.208/h while the VM runs. The M0 to M2 estimate of $24 to $31 slightly exceeds the $30 envelope at its upper end and is watched at each milestone.
- A public SSH port is an accepted risk. The sandbox work in M1 (no network and no instance-metadata access from tool containers, a scrubbed environment) matters more because of it.
- `customData` is immutable: changes to the shutdown scripts or timers need `scp` plus `systemctl daemon-reload`, or recreating the VM (`infra/README.md`, "Redeploying").
- Design sections 3, 7 and 14.2 already carry these decisions; `infra/CLAUDE.md` and `infra/README.md` describe the operating details.
