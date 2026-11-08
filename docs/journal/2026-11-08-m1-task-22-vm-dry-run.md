# 2026-11-08 M1 execution, session 7: VM dry run (Task 22)

## 2026-11-08 decision: VM session driven through `az vm run-command invoke`

The owner approved in chat driving the VM with `az vm run-command invoke`, because the laptop SSH key has a passphrase and no agent was available. Consequence: run-command sessions do not count towards the 30-minute idle shutdown. The verify and replay scripts were copied to `/opt/cfdagent-src/scripts` as untracked files, because their commit was not yet on `main`. Code under test was commit c6e9b78, image `sha-c6e9b78`.

## 2026-11-08 decision: runs start from the host checkout (R54)

The CLI reads `benchmarks/` relative to the working directory (the open obstacle of 2026-11-06). On the VM, `cfdagent run --benchmark ...` is therefore started from `/opt/cfdagent-src`, the host checkout that contains `benchmarks/`. The runtime image deliberately does not ship `benchmarks/`, to keep blind evaluation intact. Verified on the VM: from another directory the CLI fails with `benchmark root benchmarks does not exist`.

## 2026-11-08 obstacle: sandbox mount check failed on `docker-init`

The first live local run of `verify_sandbox.py` failed the mount row on `/usr/sbin/docker-init`, a read-only bind mount added by `docker run --init`. An exemption was added and, after code review, tightened: exact path, read-only, source basename match, and virtual filesystem types for `/proc`, `/sys` and `/dev`.

## 2026-11-08 milestone: first real run of the VM host setup and a scripted replay on the VM

`vm-host-setup.sh` ran on the VM for the first time (form 2.2): exit 0 in 1 min 35 s including the image pull; the image takes 5.33 GB on disk. `verify_sandbox.py` passed 7/7 in about 30 s, also with the tightened mount check. The scripted replay completed in 58.2 s wall time and the Scorer passed (centreline errors 0.0049 and 0.0087). Upload finished with 253 files (28.3 MB) and the blob listing matched. `cfdagent list` and `cfdagent status` worked, and no active-run marker was left behind.

## 2026-11-08 experiment: cost of the dry run

The VM was up about 25 minutes over two starts, roughly $0.10. No LLM calls were made (scripted backend); the recorded run cost of $0.0076 is token counts priced as the primary model and was not billed. M1 spend so far is well below the $5 report threshold.

## 2026-11-08 observation: duplicate blob role assignment still present

The VM identity still has the old account-scoped Storage Blob Data Contributor assignment in addition to the container-scoped one. Removing it is step 5 of `docs/runbooks/vm-host.md` and awaits the owner's approval; it was not done in this session.
