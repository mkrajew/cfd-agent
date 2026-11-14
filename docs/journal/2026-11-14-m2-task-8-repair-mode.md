# 2026-11-14 M2 Task 8: in-place repair

## 2026-11-14 milestone: in-place repair behind `repair_mode` implemented, default stays fresh

Task 8 implements ADR-0005: with `repair_mode=in_place` a retry continues in the previous attempt's files, while the default (`fresh`) keeps the M1 behaviour. `carry_over` judges scope on the destination path, skips symlinks, hidden files and non-regular files, and acts only on attempts 2 and later in `in_place` mode. `repair_mode` is recorded in the `RunRecord` and forwarded by `--detach`; `retry_text` now takes the carried files. `code-reviewer` returned `approve`; 1993 unit tests passed, and the symlink tests were also verified in the Linux image. Azure cost: zero. The branch was not pushed because the permission classifier denied the push in this session, so the PR waits for the owner.

## 2026-11-14 observation: system prompts still describe a fresh attempt directory

`case_setup.md`, `analyst.md`, `geometry.md` and `mesh.md` still say "fresh attempt directory", which contradicts `in_place` mode. They are to be made mode-neutral in Task 9, before the Task 17 experiment, so the experiment does not compare modes under contradictory instructions. The Analyst retry text now also says that `run_utility` results are not carried over.

## 2026-11-14 observation: minor items deferred after review

Deferred under the lean review workflow: no test for `in_place` combined with a free restart; `carry_over` walks the whole previous directory when `scope.writable` is empty; nothing guards against overwriting prepared inputs; `os.walk` has no `onerror`; the `loop.py` module docstring, the README and `CLAUDE.md` do not mention `--repair-mode`.
