# whose-noise

**Whose noise is it?** This project splits the run-to-run variance of tau-bench verdicts into three parts: the user simulator, the agent, and the task. It does this for a small open agent (Qwen3-4B on a local llama.cpp server), talking to English users and to Hindi/Hinglish users who type in romanized, code-mixed text.

> Status: pre-pilot. The code and tests exist, but there are **no results yet**. See
> [Project status](#project-status) and [PREREGISTRATION.md](PREREGISTRATION.md) (DRAFT).

---

## Problem

tau-bench and tau2-bench ([Yao et al. 2024, arXiv:2406.12045](https://arxiv.org/abs/2406.12045)) put a
tool-using agent in a conversation with an *LLM-simulated user*. The benchmark scores the final database
state (and the actions) against a gold annotation. Each verdict therefore comes from two stochastic
LLMs, the agent and the simulator, and a single run mixes their noise.

[tau2-bench issue #540](https://github.com/sierra-research/tau2-bench/issues/540) (opened 2026-09-12)
recomputed run-to-run spread from the published trajectory files, without re-running anything. In retail
(114 tasks), **33-46% of tasks flip their verdict across identical runs of the June 2025 baselines, and
36-55% flip for the leaderboard submissions**. Two single runs must differ by roughly 7-9 points before the
gap is outside repetition noise. The issue also says the number "includes the user simulator's sampling, so
it is a floor for the benchmark as published, not for the agent alone". Nobody has split that floor into its
parts.

The split matters most where the simulator is weakest. Multilingual tau-bench variants are appearing
(see [Related work](#related-work)), but we found no work that tests whether an English-vs-Hindi gap is
larger than the noise floor *of the Hindi simulator itself*. Romanized, code-mixed Hindi is the register Indian users
actually type in chat.

## Research questions

**RQ1: Noise decomposition.** For a fixed small agent (Qwen3-4B, temperature 0) on the 29 judge-free
tasks of the tau2 retail test split, how much of the verdict variance comes from:
- the **task** (some tasks are hard and some are easy),
- the **user simulator** (which LLM plays the user, both as a mean shift and as a change in *which* tasks pass,
  i.e. the task x simulator interaction),
- **re-run noise**, with task and simulator held fixed? At temperature 0 this noise comes from serving
  nondeterminism, not from sampling. We then try to attribute it to the agent side or the simulator side.

**RQ2: Language beyond the noise floor.** Does the agent's success rate with Hindi/Hinglish text users
(tau2 `tau-multilingual` Hindi pack, `romanized` and `code_mixed` text-input styles) differ from English by
more than the run-to-run noise floor estimated in RQ1? Does the language change the *share* of variance
owned by the simulator?

Hypotheses, the primary endpoint, the stopping rules and the analysis plan are in
[PREREGISTRATION.md](PREREGISTRATION.md). That file is a DRAFT and gets frozen before the main runs.

## Why it matters

- **Leaderboard claims.** Issue #540 shows that adjacent leaderboard entries are often indistinguishable.
  If most of that noise belongs to the simulator, the fix is a better or less noisy simulator. If it
  belongs to the agent, more trials per task are needed. The two remedies are different.
- **Multilingual evaluation.** A "Hindi is worse" finding is only meaningful if the gap is larger than what a
  re-run of the same Hindi condition would produce, and only if it does not come from the simulator
  breaking down in Hindi rather than the agent.
- **Small open agents.** Most published tau2 numbers are for frontier APIs. A 4B model on a laptop GPU
  shows what a student or a small team can actually deploy and evaluate, at no per-token cost on the agent
  side.

## Related work

Each entry below was checked against its arXiv abstract or the GitHub API on 2026-10-11. "Does not
cover" is based on the abstract or the repository contents. Read the full papers before relying on these
gaps.

| Work | What it does | What it does **not** cover (relative to this project) |
|---|---|---|
| tau-bench, Yao et al. 2024, [arXiv:2406.12045](https://arxiv.org/abs/2406.12045) | Tool-agent-user benchmark with an LLM-simulated user. Introduces **pass^k** to measure consistency over repeated trials. | pass^k measures *total* inconsistency. It does not say which component (agent or simulator) causes it. |
| tau2-bench `tau-multilingual` branch ([sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench/tree/tau-multilingual), PR #546) | Language packs, including Hindi with personas `rishika_hindi_v1` / `imran_hindi_v1`. Text-input styles `romanized` and `code_mixed` for Hindi. Paired retail task sets (`retail_hi` keeps the English task prose and evaluation criteria, with `_hi` ids). | No variance decomposition. No separation of simulator noise from agent noise. The Hindi pack ships no text-mode guideline file (only a voice one). |
| BabelArena, Kuang et al. 2026, [arXiv:2609.23490](https://arxiv.org/abs/2609.23490) | 23-language, task-aligned adaptation of four agent-benchmark families (16,146 instances). Studies cross-language disparities, failure types and token cost. | Scored with frontier models. The abstract mentions no repeat-run noise floor and no romanized or code-mixed user input. |
| SEATauBench, Nguyen et al. 2026, [arXiv:2606.28715](https://arxiv.org/abs/2606.28715) | Adapts tau2-bench to 5 Southeast Asian languages, with localization of conversation, tools and domain added step by step. | Does not cover Hindi/Hinglish. Its focus is localization depth, not the source of run-to-run noise. |
| Lost in Simulation, Seshadri et al. 2026, [arXiv:2601.17087](https://arxiv.org/abs/2601.17087) | Human user study (US, India, Kenya, Nigeria) on tau-bench retail. Success changes by up to 9 points across user LLMs. Simulated users are a worse proxy for AAVE and Indian English speakers. | Compares simulators with humans. It does not decompose repeat-run variance, and it does not study Hindi or code-mixed input. |
| UserProxyBench, Jain & Sandhu 2026, [arXiv:2609.38043](https://arxiv.org/abs/2609.38043) | Scores the *user* proxy's fidelity to its instructions (User Fidelity Score). Swapping the proxy with the agent fixed moves mean reward by 15.2 points. | Agent is a frontier model (GPT-5.5). English only. Measures fidelity, not a variance decomposition with repeats. |
| Miller 2024, [arXiv:2411.00640](https://arxiv.org/abs/2411.00640) | Error bars for evals: clustered SE, the law-of-total-variance split into task and re-sample terms, paired differences, power. | Generic statistics. Not about multi-agent (agent + simulator) noise. We reuse its framework. |

## Setup summary

Full Windows instructions are in [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md). In short:

1. **tau2-bench** checked out next to this repo (`../tau2-bench`). It is v1.0.1, CLI `tau2`, installed with
   `uv sync --extra voice`. The extra is a workaround for a missing `websockets` dependency in the core
   install. The Hindi runs (RQ2) need the `tau-multilingual` branch.
2. **Agent**: `llama-server` (llama.cpp) serving `Qwen3-4B-Instruct-2507` Q4_K_M on
   `http://127.0.0.1:8080/v1`, alias `qwen3-4b`. tau2 reaches it as `openai/qwen3-4b`.
3. **User simulator**: Groq `openai/gpt-oss-120b` (free plan), with other simulators for RQ1. Keys go
   in `tau2-bench/.env`, **never** in this repo.
4. **This repo**: `uv sync`, then `uv run pytest`.

## How to run

All commands are run from the `whose-noise` repo root.

```bash
# 1. Check the analysis code
uv sync
uv run ruff check .
uv run pytest

# 2. Plan a grid without running anything (prints the exact tau2 commands and the
#    estimated simulator token cost against the daily budget). `--config PATH` also works.
#    A grid is refused (exit 1) when (a) tasks with NL assertions would run without the
#    judge's API key (they would end in infrastructure_error after spending simulator
#    tokens; use "exclude_nl_assertions": true under "tasks", add the key, or pass
#    --allow-missing-judge-key), or (b) an existing results.json under the same run name
#    was made with a different agent/simulator llm, llm_args, seed, trials or domain.
uv run python scripts/run_grid.py configs/pilot.json --dry-run
uv run python scripts/run_grid.py configs/rq1_simulator_swap.json --dry-run

# 3. Run for real (llama-server must be up; checks it with a health probe)
uv run python scripts/run_grid.py configs/pilot.json
#    Large grids: run the first K of N task chunks; re-running resumes (tau2 --auto-resume)
uv run python scripts/run_grid.py configs/rq1_simulator_swap.json --only gptoss120b --task-chunk 1/6

# 4. Summarise one or more runs: success, pass^k with task-bootstrap CI, flip rate,
#    per-task table, failed actions, token use, Groq budget line
uv run python -m whose_noise.analyze ../tau2-bench/data/simulations/pilot1_qwen4b_gptoss120b
uv run python -m whose_noise.analyze RUN_DIR_1 RUN_DIR_2 --k 1 2 3 --csv results/summary.csv
```

The variance decomposition (`whose_noise.variance`) has no CLI yet. Use it from Python:

```python
from whose_noise.tau2_results import load_runs
from whose_noise.variance import decompose_variance, noise_floor

df = load_runs(["../tau2-bench/data/simulations/<run_a>", "../tau2-bench/data/simulations/<run_b>"])
valid = df[~df["is_infra_error"]].assign(success=lambda d: d["success"].astype(bool))
# n_reps=3 applies the pre-registered rule (PREREGISTRATION section 8: incomplete tasks
# are dropped whole). Check dec.n_tasks_input, dec.dropped_tasks_success_rate and
# dec.kept_tasks_success_rate: a gap means the drop is not at random. Without n_reps the
# repeat count is chosen to keep >= 90% of tasks (repeats are cut instead of tasks).
dec = decompose_variance(
    valid,
    sim_col="user_llm",
    n_reps=3,
    n_boot=10_000,
    seed=20261011,  # report the seed
)
nf = noise_floor(valid, sim_col="user_llm", n_eval_tasks=29, trials_per_run=3, n_reps=3)
```

Report every share next to its raw component, CI and `frac_boot_nonpositive`: truncated
shares are biased upward for small components (see the `variance` module docstring);
`ComponentEstimate.raw_share` and `NestedDecomposition.raw_noise_shares` give the
untruncated ratios. `flip_rate_mixed_tasks` is conditional on observed mixing and is NaN
with one simulator; compare `flip_rate` (with R stated) against tau2-bench issue #540.

To compare Hindi and English, strip the `_hi` / `_en` id suffix before calling
`compare_decompositions` or `metrics.paired_bootstrap_diff`. Both functions pair on identical task ids.

### Repository layout

| Path | Purpose |
|---|---|
| `src/whose_noise/tau2_results.py` | `results.json` -> one row per simulation. Infra errors are kept but flagged (`success` is NA). |
| `src/whose_noise/metrics.py` | pass^k (unbiased, per task), task-cluster bootstrap, paired bootstrap difference, exact McNemar, Wilson CI. |
| `src/whose_noise/variance.py` | Balanced ANOVA variance components (crossed task x simulator design; nested seed design), task bootstrap, noise floor, synthetic data with known components. |
| `src/whose_noise/analyze.py` | CLI text report per run. |
| `scripts/run_grid.py` | Builds and runs `tau2 run` commands from a JSON config, with a daily-budget guard and a secret scan (stdlib only). |
| `configs/` | `pilot.json` (5 tasks x 3 trials), `rq1_simulator_swap.json` (40 test tasks x 3 trials x simulators). |
| `tests/` | Hand-computed expected values and synthetic data with known ground truth. |
| `docs/RESEARCH_NOTES.md` | Statistics spec, tau2 seeding facts, design notes. |

## Project status

No numbers are filled in until they come out of `analyze.py` on a committed `results.json`. Cells marked
"-" have not been run.

| Experiment | Agent | User simulator | Language | Tasks x trials | Valid sims | pass^1 [95% CI] | pass^3 | Flip rate | Infra errors |
|---|---|---|---|---|---|---|---|---|---|
| week2 smoke | Qwen3-4B | gpt-oss-120b | en | 3 x 1 | 2 of 3 | n/a (smoke) | - | - | 1 of 3 |
| pilot1 (in progress, 12 of 15 sims) | Qwen3-4B | gpt-oss-120b | en | 5 x 3 | 6 (tasks 0, 1 only) | 0.500 (no CI: 2 tasks) | 0.000 | 2 of 2 tasks | 6 of 12 (NL-judge auth, see below) |
| RQ1 simulator swap | Qwen3-4B | gpt-oss-120b / qwen3.8-27b / (gemini) | en | 29 x 3 each | - | - | - | - | - |
| RQ2 English | Qwen3-4B | gpt-oss-120b | en | 29 x 3 | - | - | - | - | - |
| RQ2 Hindi romanized | Qwen3-4B | gpt-oss-120b | hi (romanized) | 29 x 3 | - | - | - | - | - |
| RQ2 Hinglish code-mixed | Qwen3-4B | gpt-oss-120b | hi (code_mixed) | 29 x 3 | - | - | - | - | - |

The week2 smoke run is a plumbing check (task 0 passed, task 1 failed, task 2 hit an infra error). It is
not evidence about the agent. The pilot1 row is a partial snapshot taken on 2026-10-11 with `analyze.py`.
Tasks 2-4 have NL assertions, and tau2 scores those with a hard-coded OpenAI judge. Without
`OPENAI_API_KEY` that judge fails, and the whole conversation is discarded. Even at temperature 0, both
tasks that did run flipped verdict across trials (task 0: 1/0/0, task 1: 1/0/1). That is the effect this
project measures, but with 2 tasks it is an anecdote, not a result.

## Where this breaks and why

These limits are known in advance. Each one is a threat to validity, not a bug to fix later.

1. **Temperature 0 is not determinism.** Both models run at temperature 0, so every repeat-run flip comes
   from serving nondeterminism: provider batching on Groq (whose `seed` is documented as best-effort), and
   prompt-cache / batch effects in llama.cpp. That noise depends on server load and configuration, which
   we do not control on Groq. The noise floor we measure is a property of *this serving setup*, not of
   the models.
2. **Agent and simulator seeds are coupled in tau2.** `orchestrator.py` passes the same seed to the agent
   and the user, and `runner/batch.py` gives every task in trial *r* the same seed. The stock CLI cannot
   vary the user seed with the agent seed fixed. RQ1 therefore varies the *simulator model* (crossed
   design), and attributes re-run noise by checking which message first diverges between repeats (see
   `docs/RESEARCH_NOTES.md`), not by crossing seeds.
3. **Few simulators means the "simulator" mean component is not estimable.** With 2-3 simulators, the
   task bootstrap treats them as fixed. A Monte Carlo check of our estimator gave about 75% coverage for a
   nominal 95% interval at S = 3. We report per-simulator means and the task x simulator component
   instead.
4. **Floor effects.** All variance components shrink as the success rate moves towards 0 or 1. If
   Qwen3-4B solves very few retail tasks, every component looks small and the shares are unstable. The
   mean success rate is always reported next to any share.
5. **Small n.** There are 29 judge-free test tasks. CLT intervals are too narrow at this size (Bowyer et al. 2025,
   [arXiv:2503.01747](https://arxiv.org/abs/2503.01747)), so the task-cluster bootstrap is the primary
   interval. Even so, only effects of roughly 15-20 points or more can be detected (the exact MDE is
   computed from the pilot). A null result on RQ2 is weak evidence of no difference.
6. **Free-tier quotas shape the design.** Groq's free plan allows 200K tokens/day per model, and
   gpt-oss-120b uses about 6-9K tokens per simulation. That is about 22-33 simulations/day per simulator, so
   one 29 x 3 condition (87 simulations) takes about 3-4 days. Rate-limit errors show up as `infrastructure_error`, and these are
   re-run, not counted as failures. If they are not re-run, the design is unbalanced.
7. **Hindi infrastructure errors may not be random.** If Hindi runs crash or hit `max_steps` more often,
   dropping them biases the comparison. Infra-error rates are reported for each language as an outcome in
   their own right.
8. **The simulator might be the thing that fails in Hindi.** A lower Hindi score can mean that the
   *simulator* stops following its instructions in Hindi (premature disclosure, wrong details), not that the
   agent fails. A sample of failed transcripts is read by hand and labelled agent-fault or simulator-fault.
   The Hindi pack's persona wording (e.g. "high tolerance for an English-only agent") also differs from the
   English default user, so "language" is partly confounded with "persona".
9. **Branch and version drift.** The Hindi pack only exists on the `tau-multilingual` branch, which has
   diverged from `main` (9 commits ahead, 7 behind as of 2026-10-11). English and Hindi conditions for RQ2
   must run on the *same* commit, and every `results.json` records `info.git_commit`.
10. **The LLM judge is a fourth noise source.** 11 of the 40 retail test tasks carry NL assertions. tau2
    scores them with a hard-coded LLM judge (`gpt-4.1-2025-04-14` on `main`, `gpt-5.4-mini-2026-03-17`
    on `tau-multilingual`, set in `src/tau2/config.py`). That judge is paid, needs `OPENAI_API_KEY`,
    differs between the two branches, and adds variance of its own. The primary analysis therefore uses
    the **29 judge-free test tasks**, whose reward is a deterministic DB/action check. Ids are listed in
    PREREGISTRATION.md.
11. **One agent, one domain.** The results describe Qwen3-4B-Instruct-2507 Q4_K_M on retail. They may not
    transfer to other agents, quantizations or domains (airline, telecom).

## License

MIT. Tasks and data from tau2-bench keep their own license (see the tau2-bench repository).
