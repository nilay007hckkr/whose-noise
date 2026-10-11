# Research notes

Working notes behind [PREREGISTRATION.md](../PREREGISTRATION.md). This file is allowed to change.
Statements about tau2-bench internals were read from the source of the local checkout
(`D:/Programming/Reaserch/tau2-bench`, `main` @ `4ce7c03`) unless stated otherwise. Statements about
the `tau-multilingual` branch were read through the GitHub API / raw files on 2026-10-11.

Contents:
1. Data model and outcome
2. pass^k
3. Standard errors for repeated binary outcomes
4. Variance components (Designs A and B)
5. First-divergence attribution (planned)
6. tau2-bench facts that shape the design
7. Fault-labelling rubric
8. The `tau-multilingual` Hindi pack
9. Budget arithmetic
10. Sources and verification status
11. Open questions

---

## 1. Data model and outcome

The unit is one tau2 simulation. `whose_noise.tau2_results.load_runs` turns one or more
`results.json` files into a long table with one row per simulation: `run_name, task_id, trial, seed,
agent_llm, user_llm, termination_reason, is_infra_error, reward, success, db_match, token counts,
duration_s, failed_write_actions, first_failed_action, extra_tool_calls, error_type, error_stage`.

Outcome rule (same as tau2 `metrics/agent_metrics.py::is_successful`):

```
y = 1  if reward_info is not null and |reward_info.reward - 1| <= 1e-6
y = 0  otherwise
y = missing  if termination_reason == "infrastructure_error" or reward_info is null
```

Infra errors are missing data, not failures. tau2's own metrics drop them too. All estimators assume a
**balanced** array (same number of valid repeats in every cell). Incomplete tasks are dropped whole,
never single runs, and the code warns when it does this (`on_unbalanced="drop"`) or raises
(`"raise"`).

Termination reasons seen so far: `user_stop`, `agent_stop`, `max_steps`, `too_many_errors`,
`infrastructure_error`. Only the last one is missing data. `max_steps` and `too_many_errors` are real
failures (y = 0), but they are also reported separately, because they could be language-dependent.

## 2. pass^k

Yao et al. 2024 (arXiv:2406.12045, Sec. 3). For task t with n_t valid trials, c_t of them successful,
and k <= n_t:

```
pass^k_t = C(c_t, k) / C(n_t, k)          (C(c, k) = 0 when c < k)
pass^k   = (1/T) * sum_t pass^k_t
pass@k_t = 1 - C(n_t - c_t, k) / C(n_t, k)  (Chen et al. 2021, arXiv:2107.03374)
```

`C(c,k)/C(n,k)` is the hypergeometric probability that a random k-subset of the n trials is all
successes. That makes it an unbiased estimator of `p_t^k` (a U-statistic). pass^1 is the mean success
rate. `E[pass^k_t] = p_t^k`, so the gap between pass^1 and pass^k grows with within-task noise.

Rules in `metrics.py`: `ValueError` if k > n_t (tau2 does the same). With unequal n_t, cap k at min n_t.

Hand-computed test vectors (n = 4), used in `tests/test_metrics.py`:

| c | k | pass^k_t |
|---|---|---|
| 3 | 2 | 3/6 = 0.5 |
| 2 | 2 | 1/6 |
| 1 | 2 | 0 |
| 4 | 4 | 1 |
| 3 | 1 | 0.75 |

Two tasks (n=4, c=3) and (n=4, c=2), k = 2: (0.5 + 1/6) / 2 = 1/3.

## 3. Standard errors for repeated binary outcomes

Source: Miller 2024, "Adding Error Bars to Evals" (arXiv:2411.00640).

The task is the unit. With R repeats per task, let `s_t = mean_r y_tr` and `mu_hat = mean_t s_t`:

```
SE_CLT = sqrt( [1/(T-1) * sum_t (s_t - mu_hat)^2] / T )      (Miller Eq. 1 on task means)
```

**Never** pool T x R runs as if they were independent. The pooled Wilson interval that `analyze.py`
prints is labelled as too narrow for exactly this reason.

Law of total variance (Miller Sec. 3):

```
Var(mu_hat) = ( Var_t(p_t) + E_t[p_t (1 - p_t)] / R ) / T
```

More repeats only shrink the second term. This is why tau2's run-to-run noise does not go away with
more trials per task: the task term stays. With T = 29 the CLT is unreliable (Bowyer et al. 2025,
arXiv:2503.01747), so the **task-cluster bootstrap is the primary interval** and CLT SEs are only a
cross-check.

Paired difference between conditions A and B on the same tasks: `d_t = s_t(A) - s_t(B)`,
`SE = sd(d_t) / sqrt(T)` (Miller Eq. 7). The bootstrap version is `metrics.paired_bootstrap_diff`.
Power: `MDE ~ (z_{1-alpha/2} + z_{power}) * SE ~ 2.8 * SE` at alpha = 0.05 and 80% power
(Miller Eqs. 9-10).

## 4. Variance components

Implemented in `whose_noise.variance`, which uses balanced ANOVA / method-of-moments estimators
(Searle, Casella & McCulloch 1992). The EMS formulas below were derived independently and checked by
Monte Carlo against synthetic data with known components (`generate_synthetic`, see
`tests/test_variance.py`).

### Design A: crossed task x simulator (used for RQ1)

Model: `y_tsr ~ Bernoulli(p_ts)`, `p_ts = mu + a_t + b_s + c_ts`, with independent zero-mean effects.
T tasks, S simulators, R repeats.

```
MS_T  = S R sum_t (ybar_t.. - ybar)^2 / (T-1)
MS_S  = T R sum_s (ybar_.s. - ybar)^2 / (S-1)
MS_TS = R sum_ts (ybar_ts. - ybar_t.. - ybar_.s. + ybar)^2 / ((T-1)(S-1))
MS_E  = sum_tsr (y_tsr - ybar_ts.)^2 / (T S (R-1))

E[MS_E]  = res
E[MS_TS] = res + R txs
E[MS_T]  = res + R txs + S R task
E[MS_S]  = res + R txs + T R sim

task = (MS_T - MS_TS) / (S R)     sim = (MS_S - MS_TS) / (T R)
txs  = (MS_TS - MS_E) / R         res = MS_E  (estimates E[p_ts (1 - p_ts)])
```

The four components add up to `p_bar (1 - p_bar)` (law of total variance). Raw estimates can be
negative. They are kept raw inside bootstrap draws and truncated at 0 only for reported shares
(`max(c_k, 0) / sum_j max(c_j, 0)`).

Related descriptive quantities:
- `cell_flip_rate`: the fraction of cells whose R repeats are not all equal. Its expectation per cell is
  `1 - p^R - (1-p)^R`, which **grows with R**, so R is always reported next to it. This is the quantity
  closest to issue #540's "tasks whose verdict flips".
- `within_cell_disagreement`: P(two repeats of a cell disagree) = `2 * MS_E`. Unbiased for any R >= 2.
- `between_simulator_disagreement`: `E[between - within] = 2 (sim + txs)`.

**Coverage caveat.** The task bootstrap treats simulators as fixed. In a Monte Carlo check at S = 3,
the `sim` component's nominal 95% interval covered about 75% of the time. With 2-3 simulators we
therefore report per-simulator means and `txs`, and do not interpret `sim`. Owen's pigeonhole
bootstrap (resample tasks and simulators independently, arXiv:0712.1111) is the principled fix, but it
needs many more simulators than we can afford.

**Floor effects.** Every component scales with `p_bar (1 - p_bar)`. Always report `p_bar`. Shares are
more comparable than raw components, but they become unstable near 0 or 1. The latent-scale (logit)
ICC literature (Nakagawa & Schielzeth 2010) covers this. We stay on the probability scale because it
is the scale pass^1 is reported on.

### Design B: seeds crossed within task (implemented, but **not runnable with stock tau2**)

For each task: U user seeds x A agent seeds x D duplicates with identical seeds. Components: task,
user, agent, user x agent, nondeterminism (`decompose_nested`).

Why it cannot be run now: tau2 couples the seeds (section 6.1). Every trial gives the agent and the
user the *same* seed, and every task in trial r gets the same seed. On top of that, both models run at
temperature 0, so the seeds barely matter. A real Design B would need a small tau2 patch that sets
the user and agent seeds separately, plus temperature > 0. That would be a different experiment
(sampling noise rather than serving noise). It is kept as future work. The code and its tests stay,
because they validate the estimator on synthetic data.

## 5. First-divergence attribution (planned, not yet implemented)

Goal: attribute re-run noise (the residual in Design A) to the agent side or the simulator side,
without seed control.

Idea: take two repeats i, j of the same task, simulator and condition. Walk their message lists in
order and find the first index m where they differ. Before m the two conversations are identical, so
both generators at m saw **identical input**. The role of message m (`assistant`, `user` or `tool`)
therefore names the component that behaved nondeterministically first.

```
for each cell (task, condition, simulator), each unordered pair of repeats (i, j):
    m = first index where normalize(msg_i[m]) != normalize(msg_j[m])  (or none)
    label = role(msg_i[m])  in {assistant, user, tool, none(identical), length}
share_user  = #pairs labelled user      / #pairs with a divergence
share_agent = #pairs labelled assistant / #pairs with a divergence
```

- `normalize` compares `content` (whitespace-stripped) and `tool_calls` (name + canonical-JSON
  arguments). It ignores ids, timestamps, `usage` and `generation_time_seconds`.
- A `tool` divergence given identical tool calls would mean the environment is nondeterministic. We
  expect none. If one appears, it is a bug to report.
- `length`: one conversation is a strict prefix of the other (one side stopped).
- Pairs that diverge but have the **same** verdict still tell us whose noise it is. We report shares
  for all diverging pairs, and separately for pairs with different verdicts.
- Caveat: only the **first** divergence can be attributed. After it the inputs differ, and later
  differences are consequences.
- CI: task-cluster bootstrap over cells.
- Complementary **agent determinism audit**: send the identical recorded agent request (from a saved
  conversation prefix) to llama-server 10 times, then 10 more times with other requests in between.
  If every response is identical, the agent side is deterministic under our serving settings, and
  any residual noise must start at the simulator (Groq). Groq documents `seed` as best-effort and
  returns a `system_fingerprint`. Record it if tau2/litellm exposes it (`TO VERIFY`).

Status: needs a new function (suggested: `whose_noise.divergence.first_divergence(df_messages)`). It
reads `messages` from results.json, which `tau2_results` currently does not keep in the table.

## 6. tau2-bench facts that shape the design

### 6.1 Seeding (from `src/tau2/runner/batch.py` and `src/tau2/orchestrator.py`)
- `random.seed(config.seed)`, then one `randint(0, 1e6)` per trial. **Every task in trial r gets
  the same seed.**
- `orchestrator.py` (around lines 525-528) calls `agent.set_seed(seed)` and `user.set_seed(seed)`
  with the same value, so the agent and user seeds are coupled.
- Both pilot runs use temperature 0.0 for agent and user. Any flip seen there therefore comes from
  nondeterminism in serving (provider batching, KV-cache reuse), not from sampling.
- llama.cpp server: the default `seed` is -1 (random), and its README warns that `cache_prompt` can
  make results nondeterministic, because batch sizes differ between prompt processing and token
  generation.

### 6.2 The NL-assertion judge
- `src/tau2/config.py`: `DEFAULT_LLM_NL_ASSERTIONS = "gpt-4.1-2025-04-14"` on `main`. On
  `tau-multilingual` it is `"gpt-5.4-mini-2026-03-17"`. This is hard-coded: there is no CLI flag, and
  it is used in `evaluator/evaluator_nl_assertions.py`.
- The judge is called only when a task's `evaluation_criteria.nl_assertions` is non-empty. Tasks
  without assertions return reward 1.0 for that part without any LLM call.
- Retail: 40 of 114 tasks have NL assertions (29 train, 11 test). Judge-free test tasks: 29 (ids
  in PREREGISTRATION section 4).
- Without `OPENAI_API_KEY` the judge raises `AuthenticationError`. tau2 then records
  `infrastructure_error` **with an empty message list**: the conversation (and its Groq tokens) is
  lost, and `tau2 evaluate-trajs` cannot re-score it. This is what happened to pilot1 tasks 2-4
  (6 of 12 simulations, `error_stage = evaluation` in `analyze.py`).
- Consequence: the pilot and main configs should use judge-free tasks only, unless a paid judge key is
  added on purpose. The judge would also be a third stochastic component that this design does not
  separate.

### 6.3 Other
- The core install lacks `websockets`. Install with `uv sync --extra voice`.
- Windows console: set `PYTHONUTF8=1`, otherwise emoji in tau2 output crash the run.
- `--auto-resume` skips finished simulations and retries infra errors. Removing tasks from a resumed
  run is refused, which is why `run_grid.py --task-chunk K/N` uses cumulative prefixes.

## 7. Fault-labelling rubric (for failed simulations)

Read the transcript next to the task's `user_scenario` and the gold actions.

| Label | Rule |
|---|---|
| `simulator-fault` | The user simulator broke its instructions in a way that made success impossible or much harder: gave wrong IDs or details, asked for something not in the scenario, stopped (`###STOP###`) before the goal, switched language or register against its style, or gave information that contradicts the scenario. |
| `agent-fault` | The user behaved correctly and the agent made a wrong or missing tool call, broke the policy, did an extra write (`extra_tool_calls`), or ran out of steps while the user was cooperative. |
| `both` | Both of the above, and either one alone would have caused the failure. |
| `unclear` | Cannot tell from the transcript. Write one sentence on why. |

Label without looking at the verdict of the paired run in the other language. The language itself
cannot be hidden from the labeller, so blinding is only partial.

## 8. The `tau-multilingual` Hindi pack (checked 2026-10-11 through the GitHub API)

- Branch HEAD `8ff48b8e` (2026-09-19, PR #546 "Release corrected multilingual results and
  reproducibility fixes"). It has **diverged** from `main` (9 ahead / 7 behind; merge-base
  `2174a603`). The 7 main-only commits are voice/leaderboard changes and do not touch text-mode
  retail. Both branches are pyproject version 1.0.1. Its `uv.lock` only references pypi.org /
  files.pythonhosted.org, so `uv sync` should work outside Sierra.
- `data/tau2/domains/retail/tasks.json` is byte-identical on both branches.
- `data/tau2/multilingual/hi/retail_tasks_hi.json`: 114 tasks with ids `<id>_hi`. `user_scenario` and
  `evaluation_criteria` are equal to `main` for all 114. The prose is English, and the language comes
  from the persona and directives. Task set name: `retail_hi` (registered by filename).
- `retail_tasks_hi_identity.json` is **not** paired: it uses Indian identities and a localized initial
  DB, and only 40 of 114 evaluation criteria equal `main`. It is not used.
- Personas: `rishika_hindi_v1` (female, IN-MH, "fast dense Hinglish; high tolerance for an
  English-only agent") and `imran_hindi_v1` (male, IN-DL, "patient Urdu-inflected Hindi; low
  tolerance for an English-only agent").
- Text input styles for `hi` (pack.yaml, closed catalog `tau2.multilingual.text_input_catalog`):
  native script (the default), `romanized` (e.g. "mera naam Rahul hai"), and `code_mixed` (e.g.
  "account mein problem aa rahi hai"). `diacritic_free` is not declared for Hindi.
- CLI flags (branch `src/tau2/cli.py`): `--task-set-name`, `--user-persona-id` (a persona id or a
  bare language code), and `--text-input-style` (TEXT mode only; needs a language-pack run whose
  pack declares the style).
- Pack-level agent clause: "Respond in Hindi using Devanagari script for Hindi and Latin script for
  English insertions; mirror the user's code-switching register; keep all tool calls, arguments, and
  structured outputs in English exactly as specified." `TO VERIFY` whether it reaches the agent's
  system prompt on our text runs.
- Only a **voice** guideline file exists for Hindi (`guidelines/simulation_guidelines_voice_hi.md`).
  There is no text-mode Hindi guideline file.
- The branch's default user LLM is `gpt-5.5`. We always pass `--user-llm` explicitly.

## 9. Budget arithmetic

Measured (pilot1, 6 valid simulations on retail train tasks 0-1, `analyze.py`, 2026-10-11): about
63K agent tokens and about 6.1K user tokens per simulation, with a mean duration of 39 s. Earlier
smoke runs gave 60-90K agent and 6-9K user tokens per task.

| Resource | Limit | Per simulation | Simulations/day |
|---|---|---|---|
| Groq gpt-oss-120b tokens | 200K TPD (free plan) | 6-9K | 22-33 |
| Groq requests | 1K RPD | ~5-20 user turns | 50-200 (not binding) |
| Groq TPM | 8K | a single simulation can approach it | keep `--max-concurrency 1` |
| Local agent | GPU time only | ~40 s | not binding |

Groq limits are **per model**, so different simulators (RQ1) can run on the same day. All RQ2
conditions use gpt-oss-120b and share one daily quota. The budget per condition is 29 x 3 = 87
simulations, i.e. about 3-4 days on gpt-oss-120b. Hindi token use is not yet measured. BabelArena
(arXiv:2609.23490) reports up to about 2x input tokens for lower-resource languages, so the Hindi
pilot must measure it before any schedule is fixed.

## 10. Sources and verification status

"Verified" means the cited content was checked against the primary page (arXiv abstract, GitHub API,
source code) on or before 2026-10-11. Abstract-level checks do not cover claims made only in a
paper's body.

| Source | Used for | Verified |
|---|---|---|
| Yao, Shinn, Razavi, Narasimhan 2024, tau-bench, [arXiv:2406.12045](https://arxiv.org/abs/2406.12045) | pass^k definition | yes (abstract plus the paper's pass^k definition) |
| [tau2-bench issue #540](https://github.com/sierra-research/tau2-bench/issues/540) | 33-46% / 36-55% retail flips; "floor for the benchmark as published, not for the agent alone" | yes (issue body via API) |
| Miller 2024, [arXiv:2411.00640](https://arxiv.org/abs/2411.00640) | Clustered SE, variance decomposition, paired SE, power | yes |
| Bowyer, Aitchison, Ivanova 2025, [arXiv:2503.01747](https://arxiv.org/abs/2503.01747) | CLT too narrow at small n | yes |
| Chen et al. 2021, [arXiv:2107.03374](https://arxiv.org/abs/2107.03374) | Unbiased pass@k | yes |
| Madaan et al. 2024, [arXiv:2406.10229](https://arxiv.org/abs/2406.10229) | Seed variance in benchmarks (related work) | yes |
| Owen 2007, pigeonhole bootstrap, [arXiv:0712.1111](https://arxiv.org/abs/0712.1111) | Crossed-design bootstrap | yes |
| Owen & Eckles 2012, [arXiv:1106.2125](https://arxiv.org/abs/1106.2125) | Pigeonhole bootstrap is mildly conservative | **no** (search result only) |
| Searle, Casella, McCulloch 1992, *Variance Components* (Wiley) | ANOVA estimators | **no** (catalogue record only; the formulas were checked by Monte Carlo instead) |
| Nakagawa & Schielzeth 2010, Biol. Rev. 85:935-956, doi:10.1111/j.1469-185X.2010.00141.x | Latent-scale ICC caveat | **no** (repository record, not the publisher page) |
| Fagerland, Lydersen, Laake 2013, BMC Med Res Methodol 13:91, doi:10.1186/1471-2288-13-91 | Mid-p McNemar recommendation | **no** (full text not accessed). We use the exact test, which is the conservative choice. |
| Seshadri et al. 2026, Lost in Simulation, [arXiv:2601.17087](https://arxiv.org/abs/2601.17087) | Simulator vs human gaps; Indian English | yes (abstract) |
| Jain & Sandhu 2026, UserProxyBench, [arXiv:2609.38043](https://arxiv.org/abs/2609.38043) | Proxy swap moves reward 15.2 points | yes (abstract) |
| Kuang et al. 2026, BabelArena, [arXiv:2609.23490](https://arxiv.org/abs/2609.23490) | Multilingual agent benchmark; token cost | yes (abstract) |
| Nguyen et al. 2026, SEATauBench, [arXiv:2606.28715](https://arxiv.org/abs/2606.28715) | tau2 adaptation to SEA languages | yes (abstract) |
| [Groq API reference](https://console.groq.com/docs/api-reference) | `seed` is best-effort; `system_fingerprint` | yes |
| [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) | `seed` default -1; `cache_prompt` nondeterminism | yes |
| tau2-bench source (local `main` @ 4ce7c03; `tau-multilingual` @ 8ff48b8e via raw files) | Seeding, judge, Hindi pack, CLI flags | yes (read directly) |

## 11. Open questions (resolve before freezing the pre-registration)

1. Does `--user-persona-id rishika_hindi_v1` with `--text-input-style romanized` run with
   `--task-set-name retail_hi` on a text run, and does the agent receive the agent language clause?
2. Can llama-server be made deterministic? The pilot server log (`llama_server.log`) shows
   `n_slots = 4, kv_unified = 'true'` (the defaults of `-np -1` = auto) and prompt-cache entries being
   evicted. Both are known sources of nondeterminism. Release b11552 has the flags `-np 1`,
   `--no-cache-prompt` and `--cache-ram 0`. Run the determinism audit (section 5) with the default
   server and with `-np 1 --no-cache-prompt`. Then pick one setting and freeze it. Disabling the prompt
   cache re-processes the whole prompt every turn. The pilot measured about 1.9K prompt tokens/s, so
   expect simulations to be several times slower. The serving setting could also be added as an
   explicit factor ("agent serving: default vs deterministic"), which would answer how much of the
   noise the agent side owns directly.
3. Is `groq/qwen/qwen3.8-27b` available on the free plan with these limits? Its token use is
   unmeasured (the estimate in `configs/rq1_simulator_swap.json` is padded).
4. English control: should it also use a persona (to separate "language" from "persona"), or the
   default tau2 user? The current plan uses the default user. The confound is listed as a limitation.
5. Should the configs switch from tasks 0-4 / the 40-task split to judge-free ids? This needs a
   config change: `"tasks": {"ids": [...]}` with the 29 ids.
