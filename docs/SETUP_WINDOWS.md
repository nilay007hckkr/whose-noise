# Setup on Windows

Tested layout (Windows 11, NVIDIA GPU, Git Bash and PowerShell 5.1):

```
D:\Programming\Reaserch\
  tau2-bench\            tau2-bench main (v1.0.1), CLI `tau2`
  tau2-bench-ml\         (RQ2 only) tau2-bench `tau-multilingual` branch
  whose-noise\           this repo
  tools\llama.cpp-b11552\llama-server.exe
  models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf
```

Commands are given for **PowerShell** unless marked *Git Bash*. You run all git commands yourself.
None of the steps below needs a git write operation apart from the clones.

---

## 1. Prerequisites

- NVIDIA driver recent enough for CUDA 12.4. Check with `nvidia-smi`.
- `uv` (it manages Python; you do not need a separate Python install):

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  uv --version
  ```

- Make Python use UTF-8 everywhere. tau2 prints emoji, which crash the cp1252 console without this:

  ```powershell
  setx PYTHONUTF8 1          # persistent; open a NEW terminal afterwards
  $env:PYTHONUTF8 = "1"      # current PowerShell session only
  ```

  *Git Bash:* `export PYTHONUTF8=1` (add it to `~/.bashrc` to keep it).

## 2. tau2-bench

```powershell
cd D:\Programming\Reaserch
git clone https://github.com/sierra-research/tau2-bench.git
cd tau2-bench
uv sync --extra voice      # "--extra voice" pulls in websockets, which the core install is missing
uv run tau2 --help
```

For **RQ2 (Hindi)** you need the `tau-multilingual` branch. Put it in a separate folder so `main`
stays untouched:

```powershell
cd D:\Programming\Reaserch
git clone --branch tau-multilingual --single-branch https://github.com/sierra-research/tau2-bench.git tau2-bench-ml
cd tau2-bench-ml
git log -1 --format="%H %cd"      # write this hash into PREREGISTRATION.md
uv sync --extra voice
uv run tau2 run --help | Select-String "text-input-style|user-persona-id|task-set-name"
```

### API keys (`.env` in the tau2-bench folder, never in this repo)

Create `D:\Programming\Reaserch\tau2-bench\.env` (and a copy in `tau2-bench-ml\` if you use it):

```
GROQ_API_KEY=your_groq_key_here
GEMINI_API_KEY=your_gemini_key_here
```

Rules: one `KEY=value` per line, **no spaces around `=`**, no quotes. `.env` is already in tau2-bench's
`.gitignore` and in this repo's. Never paste keys into `configs/*.json`, because `run_grid.py` rejects
configs that contain key-like values.

`OPENAI_API_KEY` is **not** needed when you run judge-free tasks only (see RESEARCH_NOTES section 6.2).
If it is missing and a task has NL assertions, tau2 records an `infrastructure_error` with
`AuthenticationError`, and the conversation is lost.

## 3. Local agent: llama.cpp + Qwen3-4B

1. From <https://github.com/ggml-org/llama.cpp/releases/tag/b11552> download
   - `llama-b11552-bin-win-cuda-12.4-x64.zip`
   - `cudart-llama-bin-win-cuda-12.4-x64.zip` (CUDA runtime DLLs; needed if the CUDA toolkit is not installed)

   Extract both into `D:\Programming\Reaserch\tools\llama.cpp-b11552\`.

2. Model (about 2.5 GB):

   ```powershell
   cd D:\Programming\Reaserch
   mkdir models -ErrorAction SilentlyContinue
   curl.exe -L -o models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
   ```

3. Start the server (leave this terminal open):

   ```powershell
   cd D:\Programming\Reaserch
   .\tools\llama.cpp-b11552\llama-server.exe -m models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf --jinja -ngl all -c 16384 --load-mode none --cache-ram 512 --port 8080 --alias qwen3-4b
   ```

   - `--load-mode none` (this build's replacement for `--no-mmap`) loads the weights straight onto the GPU
     instead of memory-mapping the 2.5 GB file, so the process uses ~0.75 GB of system RAM instead of ~2.8 GB.
   - `--cache-ram 512` caps the host-RAM prompt cache. The default in this build is 8192 MiB; during a
     long run it grew the server to ~5.9 GB RAM on a 16 GB laptop and triggered low-memory kills.
   - `--jinja` turns on the model's chat template, which tool calling needs.
   - `--alias qwen3-4b` is the model name that tau2 sends (`openai/qwen3-4b`).
   - With these flags the server picks 4 parallel slots with a unified KV cache, and prompt caching is
     on. That is the setting the pilot ran with. For the **determinism audit** add
     `-np 1 --no-cache-prompt`. This is slower, because the whole prompt is re-processed every turn.
     Use one setting for the whole study and write it into PREREGISTRATION.md.

   To keep a log: append `*> llama_server.log` (PowerShell) or `> llama_server.log 2>&1` (Git Bash).

4. Health check from a second terminal:

   ```powershell
   curl.exe http://127.0.0.1:8080/health
   curl.exe http://127.0.0.1:8080/v1/models
   ```

## 4. This repo

```powershell
cd D:\Programming\Reaserch\whose-noise
uv sync
uv run ruff check .
uv run pytest
```

## 5. First runs

Use `run_grid.py` rather than typing `tau2 run` by hand. In PowerShell 5.1, JSON arguments such as
`--agent-llm-args '{"api_base": ...}'` lose their inner double quotes when they are passed to a native
program. `run_grid.py` calls tau2 through `subprocess`, so it avoids that problem, and it also sets
`PYTHONUTF8`.

```powershell
cd D:\Programming\Reaserch\whose-noise
uv run python scripts/run_grid.py configs/pilot.json --dry-run     # read the commands and budget first
uv run python scripts/run_grid.py configs/pilot.json               # real run (llama-server must be up)
uv run python -m whose_noise.analyze ..\tau2-bench\data\simulations\pilot1_qwen4b_gptoss120b
```

If you want a manual tau2 call, use *Git Bash*, where single quotes keep the JSON intact:

```bash
cd /d/Programming/Reaserch/tau2-bench
PYTHONUTF8=1 uv run tau2 run --domain retail \
  --agent-llm openai/qwen3-4b \
  --agent-llm-args '{"api_base": "http://127.0.0.1:8080/v1", "api_key": "local", "temperature": 0.0}' \
  --user-llm groq/openai/gpt-oss-120b --user-llm-args '{"temperature": 0.0}' \
  --task-ids 5 --num-trials 1 --seed 300 --max-concurrency 1 --save-to smoke_task5
```

Hindi smoke test (*Git Bash*, in `tau2-bench-ml`). It is **unverified** until it has run once (see
RESEARCH_NOTES section 11). Task ids in `retail_hi` carry a `_hi` suffix:

```bash
cd /d/Programming/Reaserch/tau2-bench-ml
PYTHONUTF8=1 uv run tau2 run --domain retail --task-set-name retail_hi \
  --user-persona-id rishika_hindi_v1 --text-input-style romanized \
  --agent-llm openai/qwen3-4b \
  --agent-llm-args '{"api_base": "http://127.0.0.1:8080/v1", "api_key": "local", "temperature": 0.0}' \
  --user-llm groq/openai/gpt-oss-120b --user-llm-args '{"temperature": 0.0}' \
  --task-ids 0_hi --num-trials 1 --seed 300 --max-concurrency 1 --save-to smoke_hi_rom
```

Results land in `<tau2 folder>\data\simulations\<save-to>\results.json`.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: websockets` when tau2 starts | core install lacks the dependency | `uv sync --extra voice` |
| `UnicodeEncodeError: 'charmap' codec ...` | console is cp1252 | `PYTHONUTF8=1` (section 1) |
| `infrastructure_error` with `AuthenticationError` and `error_stage = evaluation` in `analyze.py` | the task has NL assertions and no `OPENAI_API_KEY` is set for the judge | use judge-free task ids, or add a paid key on purpose |
| `AuthenticationError` for Groq/Gemini | key missing, or `.env` has spaces or quotes | fix `.env` and re-run the same command (`--auto-resume`) |
| `RateLimitError` / HTTP 429 from Groq | free plan: 30 RPM, 8K TPM, 1K RPD, 200K TPD per model | keep `--max-concurrency 1`; continue the next day with `--task-chunk` |
| `Connection refused` to 127.0.0.1:8080 | llama-server is not running, or is on a different port | start it (section 3), then check `/health` |
| llama-server RAM keeps growing during a run (GBs) / server killed for low memory | host-RAM prompt cache (`--cache-ram`, default 8192 MiB) and mmap'd weights | restart with `--load-mode none --cache-ram 512` (section 3) |
| `RateLimitError` from Groq **per minute** (TPM/ITPM) kills whole tasks | LiteLLM's default `constant_retry` retries instantly | keep `"num_retries": 8, "retry_strategy": "exponential_backoff_retry"` in the simulator `llm_args` (configs do this) |
| Agent never calls tools / outputs raw `<tool_call>` text | server started without `--jinja` | restart with `--jinja` |
| Context-length error from llama-server | conversation is longer than the per-slot context | raise `-c`, or use `-np 1` so one slot gets the whole context |
| `cudart64_12.dll` / `cublas64_12.dll` not found | CUDA runtime missing | extract `cudart-llama-bin-win-cuda-12.4-x64.zip` next to `llama-server.exe` |
| `uv run pytest` fails while building `whose-noise` | `README.md` missing (pyproject declares it) | keep `README.md` in the repo root |
