# Setup on Linux (CachyOS, from 2026-11-07)

The project moved from Windows to CachyOS around 2026-11-02 (same laptop, RTX 3050 6 GB). This file
records how the RQ3 environment was rebuilt; `docs/SETUP_WINDOWS.md` describes the setup RQ1, the
pilots and RQ3 day 1 ran on. Folder layout as on Windows, under `/home/nj/NILAY/Reaserch`.

## Restored state (2026-11-07)

| Piece | Value |
|---|---|
| OS | CachyOS, kernel 7.3.1-1-cachyos, NVIDIA driver 615.78.08 |
| whose-noise | `main` at `839eb80`, tags `prereg-v1..v3`; `uv sync` (Python 3.13.18); 459 tests pass |
| `tau2-bench-v2-base` | shallow clone at `7aad81c`; `uv sync --extra voice` (Python 3.12.16); `uv.lock` unchanged |
| `tau2-bench-v2-fix` | shallow clone at `2fb1ca5` (a separate clone; on Windows it was a worktree of the base clone); same install. `git diff 7aad81c 2fb1ca5` = the PR's 7 files |
| Line endings | both checkouts use `core.autocrlf=true`, so files have CRLF line endings as on Windows. tau2 stores sha256 hashes of the raw bytes of the language pack and rule files and refuses to resume a run when they change; with LF files they did not match day 1. Now `pack_sha256` = `cd14ad9a...` and `runtime_rules_sha256` = `794587eb...`, as in the Windows results. The text the models see is the same either way (YAML and Python read both line endings alike) |
| llama.cpp | release b11552, official `llama-b11552-bin-ubuntu-cuda-12.8-x64.tar.gz` (sha256 `8443251b...ae7f`) + `cudart-llama-b11552-bin-ubuntu-cuda-12.8-x64.tar.gz` (sha256 `e8b04db5...a607`), both checked against the release digests; build 11552, commit `23b0202a1`, GNU 13.3.0. Windows used `win-cuda-12.4-x64` |
| Model | `models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf`, unsloth revision `a06e946bb6`, 2,497,281,120 bytes, sha256 `3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597` (= the Hugging Face LFS id). Almost certainly the file used on Windows: the repo has had no commit since 2025-08-20 and `SETUP_WINDOWS.md` downloads from it, but the Windows copy's hash was not recorded |

Day-1 RQ3 results and both `.env` files were carried over from Windows unchanged. A tarball of every
project folder before the restore: `../_backup-20261107-pre-restore.tar.gz`.

## Daily run

1. Agent server (leave this terminal open):

   ```bash
   cd /home/nj/NILAY/Reaserch
   ./tools/llama.cpp-b11552/llama-b11552/llama-server -m models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
     --jinja -ngl all -c 16384 --load-mode none --cache-ram 512 --port 8080 --alias qwen3-4b \
     >> llama_server.log 2>&1
   ```

   The CUDA runtime libraries from the cudart tarball are copied next to `llama-server` (the
   binaries look for them in their own folder). Check: `./llama-server --list-devices` shows CUDA0.
   Startup log: `n_slots = 4, n_ctx_slot = 16384, kv_unified = 'true'`, as on Windows.

2. RQ3 chunk `k` (both versions the same day; fix first on even `k`, base first on odd `k`). The
   configs still hold the Windows `tau2_dir`; `--tau2-dir` overrides it (not part of the run name):

   ```bash
   cd /home/nj/NILAY/Reaserch/whose-noise
   uv run python scripts/run_grid.py --config configs/rq3_pr702_fix.json  --tau2-dir ../tau2-bench-v2-fix  --task-chunk k/15
   uv run python scripts/run_grid.py --config configs/rq3_pr702_base.json --tau2-dir ../tau2-bench-v2-base --task-chunk k/15
   ```

   Add `--dry-run` first to see the plan and the budget lines.

   From chunk 3 on, `scripts/rq3_day.sh` does steps 1-3 by itself: it picks the next chunk from the
   results files, starts and stops the agent server, runs both versions in the section 14 order and
   writes `../rq3_state/day_<date>_chunk<k>.log`. A systemd user timer starts it at minute 07 of every
   hour; it acts only 25 h after the last attempt began (stamp `../rq3_state/last_attempt`, set to
   day 2's start, 2026-11-07 01:02 IST) and stops once all 29 tasks are complete. It runs while the
   user is logged in (linger is off) and blocks sleep during a run. Units, outside the repo:

   ```ini
   # ~/.config/systemd/user/rq3-day.service
   [Service]
   Type=oneshot
   ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle --who=whose-noise --why="RQ3 day" /home/nj/NILAY/Reaserch/whose-noise/scripts/rq3_day.sh
   TimeoutStartSec=6h
   # ~/.config/systemd/user/rq3-day.timer
   [Timer]
   OnCalendar=*-*-* *:07:00
   Persistent=true
   [Install]
   WantedBy=timers.target
   ```

   Status: `systemctl --user list-timers rq3-day.timer`; stop: `systemctl --user disable --now rq3-day.timer`.
   Test without running anything: `RQ3_DRY=1 scripts/rq3_day.sh`.

3. Operational checks for the day's tasks (no outcomes printed), then read every merged-turn flag:

   ```bash
   uv run python scripts/rq3_daily_check.py <task ids of chunk k>
   ```
