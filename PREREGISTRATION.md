# Pre-registration: Whose noise is it?

> **FROZEN on 2026-10-20.** Reference: the git tag `prereg-v1` (the commit that adds this text).
> Frozen **before** the first RQ2 simulation. RQ1 data collection had already started on 2026-10-11
> against an earlier unfrozen draft of this document; section 12 records every RQ1-relevant change
> made since then. The frozen text is never edited. Every later change goes in the **Deviations log**
> (section 12) with a date and a reason, and any analysis affected by a deviation is reported as
> exploratory.

| Field | Value |
|---|---|
| Title | Whose noise is it? Decomposing tau-bench reliability noise for a small open agent with English and Hindi/Hinglish text users |
| Author | Nilay Joshi (Graphic Era Hill University) |
| Draft date | 2026-10-11; frozen 2026-10-20 |
| Code | this repository (`whose_noise` package, `scripts/run_grid.py`) |
| Benchmark | tau2-bench v1.0.1, retail domain. RQ1 runs on `main` at `4ce7c039`. RQ2 runs on the `tau-multilingual` branch at `8ff48b8e` plus the Windows run-lock patch in `patches/` (no behavioural change) |

---

## 1. Research questions

- **RQ1 (noise decomposition).** With the agent fixed, how is the variance of the binary tau2 verdict
  split between task, user simulator (mean shift, and task x simulator interaction) and re-run noise?
- **RQ2 (language).** Is the agent's success rate with a Hindi romanized text user different from
  English by more than the run-to-run noise floor? Is the same true for Hinglish code-mixed text?

## 2. Hypotheses and estimands

All estimands are on the probability scale. RQ1 estimands are defined on the task population that the
29 judge-free retail test tasks stand for; RQ2 estimands on the 12 tasks of section 4.

| ID | Type | Estimand | Prediction | Decision rule |
|---|---|---|---|---|
| H1a | confirmatory | Re-run (residual) component `E[p_ts (1 - p_ts)]`, with the agent and simulator at temperature 0 | > 0. Serving nondeterminism alone makes verdicts flip. | Lower bound of the 95% task-bootstrap CI > 0 |
| H1b | confirmatory | `task_x_simulator` component (Design A) | > 0. Changing the simulator model changes *which* tasks pass, not just the mean. | Lower bound of the 95% CI > 0 |
| H1c | exploratory | Share of repeat pairs whose first divergent message is a **user** message (first-divergence attribution, RESEARCH_NOTES section 5) | No directional prediction. Reported with a 95% task-bootstrap CI. | - |
| H2 | **primary** confirmatory | `Delta_rom = pass^1(hi_romanized) - pass^1(en_pack)`, paired by task | != 0 (two-sided) | Paired task-cluster bootstrap 95% CI excludes 0, Holm-adjusted across {H2, H2b, H2e} |
| H2b | secondary | `Delta_cm = pass^1(hi_code_mixed) - pass^1(en_pack)` | != 0 | Same rule |
| H2e | secondary | `Delta_style = pass^1(hi_code_mixed) - pass^1(hi_romanized)` | != 0 | Same rule |
| H2c | secondary | Difference in infra-error rate and in `max_steps` / `too_many_errors` termination rate, hi vs en | none | Descriptive, with Wilson CIs per condition |
| H2d | exploratory | Residual (re-run) component in hi vs en (`compare_decompositions`) | none | 95% CI of the difference |
| H2f | exploratory | Simulator-fidelity rates per arm (`whose_noise.fidelity`: premature stop, script leakage, customer transfer requests) | none | Descriptive, with Wilson CIs |

What H2 measures: on the branch, a Hindi arm changes the user's language **and** adds the pack's
Hindi agent-language clause to the agent prompt. `Delta_rom` and `Delta_cm` are therefore the effect of
deploying the agent for Hindi-speaking users (user language plus matching agent instruction), not of
the user's language alone. H2e holds the agent clause fixed and isolates the typing style.

"Beyond the noise floor" is defined operationally: a contrast holds only if its Holm-adjusted paired CI
excludes 0. We **also** report each `|Delta|` next to the noise-floor band of the `en_pack` arm
(`variance.noise_floor`, 95% band for two identical single runs on the same 12 tasks). The band is
context for the reader, not a second test.

## 3. Outcome

- Unit: one tau2 simulation (one task, one trial, one condition).
- `y = 1` if `reward_info` is not null and `|reward - 1| <= 1e-6`, else `0`. This is the same as tau2's
  `is_successful`.
- `termination_reason == "infrastructure_error"`, or a null `reward_info`, is **missing**, not 0.
  `whose_noise.tau2_results` flags these rows (`is_infra_error`), and they are excluded from every rate.
- Wall-clock duration is not an outcome. A simulation that completes after a stall (laptop sleep, a
  hung request) is valid.

## 4. Task set

- Domain: retail. Split: `test` from `data/tau2/domains/retail/split_tasks.json` (40 tasks).
- **RQ1: the 29 test tasks with no NL assertions.** Their reward is a deterministic DB/action check,
  with no LLM judge:
  `5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102`
- **RQ2: the first 12 of those in split-file order:** `5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51`.
  Chosen for the free-tier budget (section 6) before any RQ2 data existed.
- Excluded (NL assertions scored by a hard-coded LLM judge, see RESEARCH_NOTES section 6):
  `36, 38, 39, 40, 45, 60, 62, 68, 70, 108, 111`.
- Rationale: the judge is a third stochastic model. Its model differs between `main`
  (`gpt-4.1-2025-04-14`) and `tau-multilingual` (`gpt-5.4-mini-2026-03-17`). It needs a paid
  `OPENAI_API_KEY`, and when that key is missing the whole conversation is lost (pilot1, tasks 2-4).
- RQ2 task ids are `<id>_en` (task set `retail_en`) and `<id>_hi` (task set `retail_hi`). Both packs
  keep the English task prose, `user_scenario`, `evaluation_criteria` and `initial_state` of `main`
  (checked by `run_grid.py` before every run), so pairing on the stripped id is valid.
  `retail_hi_identity` is **not** used, because it changes names, addresses and the initial DB.
- The train split (74 tasks) is reserved for pilots and debugging and never enters a confirmatory
  analysis.

## 5. Conditions and fixed settings

| Setting | Value |
|---|---|
| Agent | Qwen3-4B-Instruct-2507, GGUF Q4_K_M (unsloth), llama.cpp `llama-server` release b11552 (Windows CUDA 12.4 build), `--jinja -ngl all -c 16384 --load-mode none --cache-ram 512`, alias `qwen3-4b`, default slots (4) with unified KV and prompt caching on. RTX 3050 6 GB laptop GPU |
| Agent args | `openai/qwen3-4b`, `api_base http://127.0.0.1:8080/v1`, temperature 0.0 |
| User simulator (RQ2 and RQ1 reference) | `groq/openai/gpt-oss-120b`, temperature 0.0, `num_retries 8`, `retry_strategy exponential_backoff_retry` |
| Extra simulator (RQ1) | `groq/qwen/qwen3.8-27b`, same args. S = 2: a third simulator (Gemini) is not included because its free tier allows 5 requests/minute |
| Trials per cell | RQ1: R = 3. RQ2: R = 2 |
| tau2 seed | 300 (tau2 derives the per-trial seeds from it; the agent and user get the same seed) |
| Concurrency | `--max-concurrency 1` (`--workers 0` on the branch) |
| Request timeout | litellm `REQUEST_TIMEOUT=300` s per HTTP request (set by `run_grid.py`); tau2's `--timeout` is **not** used because it grades a timed-out simulation as a failure |
| RQ2 English arm (`en_pack`) | `--task-set-name retail_en --user-persona-id en` |
| RQ2 Hindi romanized (`hi_romanized`) | `--task-set-name retail_hi --user-persona-id hi --text-input-style romanized` |
| RQ2 Hinglish code-mixed (`hi_code_mixed`) | `--task-set-name retail_hi --user-persona-id hi --text-input-style code_mixed` |
| RQ2 flags on every arm | `--no-llm-communicate-judge --target-language-directive-version v3 --agent-caller-locale-context` (the last two are the branch defaults, pinned) |
| Branch | All RQ2 arms on the **same** `tau-multilingual` commit (above), run by `configs/rq2_option_a.json` |

Persona: `--user-persona-id hi` (not a pinned persona). On the branch, `hi` picks the pack persona
whose gender matches the task's caller (`tau2.multilingual.registry.resolve_task_persona`), so the
customer's Hindi first-person grammar agrees with the customer's name. A pinned persona such as
`rishika_hindi_v1` would use female agreement under a male name on 2 of the 12 tasks (33, 49). The
persona is therefore a deterministic function of the task and its variance is part of task variance.

## 6. Design and sample size

- **RQ1, Design A (crossed):** 29 tasks x 2 simulators x 3 repeats, English, on `main` = 174
  simulations.
- **RQ2 (Option A, chosen 2026-10-20):** 12 tasks x 3 arms (`en_pack`, `hi_romanized`,
  `hi_code_mixed`) x 2 repeats, gpt-oss-120b simulator, on the branch = 72 simulations.
- **The sample size is fixed.** It is not extended, shrunk or re-weighted because of interim RQ2
  results. A larger RQ2 study, if any, is a new pre-registration on new tasks.
- Budget (gpt-oss-120b, Groq free plan, 200K tokens/day per model, shared with RQ1): measured ~11.8K
  tokens per English simulation; Hindi estimated ~42K (one smoke simulation measured 31,458 with
  qwen3.8-27b). RQ2 needs about 2.3M simulator tokens, i.e. about 12 days at one task per day, all 3
  arms of a task on the same day.
- **Power (illustrative only, not a result).** The paired SE of `Delta` is `SD(d_t) / sqrt(12)`, where
  `d_t` is the per-task difference of mean success. If `SD(d_t)` = 0.3, SE ~ 0.087 and the MDE at 80%
  power / alpha 0.05 ~ 2.8 x SE ~ **0.24**; if 0.4, MDE ~ **0.32**. Only large language effects are
  detectable at this size. A null H2 is reported as "not detected at this sample size", never as "no
  difference".

## 7. Analysis plan

Everything below uses this repo's code. Bootstrap seed 20261011 (`variance.DEFAULT_BOOTSTRAP_SEED`),
B = 10,000 replicates, percentile intervals, resampling **tasks** (whole task slabs, all conditions
together for paired contrasts). With fewer than 20 tasks the percentile interval under-covers; this is
stated next to every RQ2 interval.

1. Load runs with `tau2_results.load_runs`. Report simulations, infra errors and termination reasons for
   each condition.
2. Report pass^1, pass^2 and pass^3 (pass^2 for RQ2) for each condition (`metrics.pass_hat_k`, the
   unbiased estimator of Yao et al. 2024) with task-cluster bootstrap CIs (`metrics.cluster_bootstrap_ci`).
3. **H1a/H1b:** `variance.decompose_variance(valid, sim_col="user_llm")`. Report all four raw
   components, the truncated and untruncated shares and the mean success rate `p_bar`. The `simulator`
   component is reported but **not interpreted**, because its CI under-covers with S = 2. Also report
   `cell_flip_rate`, `within_cell_disagreement` and `between_simulator_disagreement`.
4. **H2/H2b/H2e:** `metrics.paired_bootstrap_diff` on stripped task ids. Report each estimate, its 95% CI
   and the Holm-adjusted conclusion across the three. Sensitivity checks (not confirmatory): (a) the
   exact McNemar test on per-task majority verdicts (`metrics.mcnemar_exact`); (b) the same contrasts for
   pass^2.
5. **Noise floor:** `variance.noise_floor` on the `en_pack` arm (`sim_col=None, n_eval_tasks=12,
   trials_per_run=2`), and on RQ1's gpt-oss-120b cells for the 29 tasks.
6. **H2d:** `variance.compare_decompositions(en, hi, design="crossed")` with a single simulator, which
   gives task and residual only.
7. **H2f:** `analyze.py --fidelity` per arm, split by passing and failing simulations.
8. **Failure analysis (exploratory):** every failed simulation is labelled `AGENT_ERROR` /
   `SIMULATOR_ERROR` / `SHARED` / `TASK_OR_GRADER_ISSUE` by two independent LLM judges with different
   instructions (root-cause and simulator-fidelity lenses) plus a tiebreak judge on disagreement, as in
   `results/audit/`. Raw agreement and Cohen's kappa are reported. A human-labelled sample of at least
   20 cases is used to estimate judge accuracy before these labels support any claim.

## 8. Missing data and exclusions

- Infra errors are re-run with the same command (`--auto-resume` retries them), up to **3** extra
  attempts per cell.
- If a cell is still incomplete after that, the **whole task** is dropped from every condition in
  that analysis (never single runs). Dropped tasks are listed. If more than 3 tasks of RQ1, or more
  than 1 task of RQ2, are dropped, this is reported as a deviation.
- Infra-error rates are reported for each condition **before** re-runs (H2c), because failures that
  depend on the language are a finding.
- No simulation is excluded for any other reason, such as a transcript that "looks broken" or a long
  wall-clock duration. Such cases are labelled in step 8, not removed.

## 9. Pilots and smoke tests (not confirmatory)

- `pilot1`: retail train tasks 0-4 x 3 trials, English, on `main`. Used for plumbing, token
  measurement and timing.
- `smoke_hi_romanized_qwen38`: train task `0_hi`, 1 trial, romanized Hindi, qwen3.8-27b simulator, on
  the branch (2026-10-12). Used to check the flags, the token cost and the Windows patch.
- No other Hindi data exist before this freeze. Pilot and smoke data are never pooled with RQ1 or RQ2
  data; their run names differ, so tau2 cannot resume one into the other.

## 10. Stopping rules

- Each condition runs until every cell of the design has its planned valid repeats, or until the
  missing-data rule in section 8 applies. No condition is stopped early because of its interim results.
- RQ2 runs all 3 arms of the same task chunk on the same day, so that day-to-day changes in the hosted
  simulator do not line up with the language contrast.
- The H2 contrasts are **not computed** until all three RQ2 arms are complete. `analyze.py` may be run
  on single arms during collection (to monitor infra errors, cost and fidelity), but never on hi and en
  together.

## 11. What would change the conclusions

- If `p_bar` < 0.1 or > 0.9 in an arm, components are floor- or ceiling-limited. Shares are then
  reported but flagged as uninformative.
- If the infra-error rate differs between hi and en by more than 10 points, H2 is reported together
  with a worst-case bound that counts the extra Hindi infra errors as failures.
- If the romanized arm's customer turns are mostly Devanagari (fidelity script-leakage share > 50%),
  H2 and H2e are reported with that caveat, because the arm did not deliver its intended input style.

## 12. Deviations log

Entries dated before 2026-10-20 are changes made while this document was an unfrozen draft and RQ1
data were already being collected. They are listed so that RQ1 can be read with them in mind.

| Date | Section | Change | Reason |
|---|---|---|---|
| 2026-10-11 | 5 | RQ1 runs on `main` (`4ce7c039`), not on the branch as the draft said | RQ1 started before the branch was set up; `main` and the branch have byte-identical retail tasks. RQ2's English arm is `en_pack` on the branch instead of a shared RQ1 cell |
| 2026-10-11 | 5 | llama-server `--load-mode none` added before the first RQ1 simulation | Memory-mapped weights used ~2.8 GB host RAM; weights already sit fully on the GPU, so no numerical effect is expected |
| 2026-10-12 | 5 | llama-server `--cache-ram 512` added at 20:23 IST; RQ1 simulations before that ran with the default 8192 MiB host prompt cache | The host prompt cache grew the server to 5.9 GB RAM and risked low-memory kills. Prompt-cache restores can change floating-point results slightly, so the RQ1 residual component may differ before/after; a sensitivity analysis compares the two periods |
| 2026-10-12 | 5 | Simulator `llm_args` gained `num_retries 8` and exponential backoff; the RQ1 run names changed and the earlier qwen3.8 run (1 infra-error simulation) is not used | Per-minute rate limits killed whole simulations under litellm's instant retries; retries do not change conversation content |
| 2026-10-19 | 3 | One RQ1 simulation (qwen3.8-27b, task 42, trial 0) stalled ~24 h (laptop sleep or hung request) and then completed normally; kept | Section 3: duration is not an outcome |
| 2026-10-20 | 5 | litellm `REQUEST_TIMEOUT=300` added for all later runs | Bounds a hung request; a failed request is retried and does not change graded outcomes |
| 2026-10-20 | 2, 4, 6, 9 | RQ2 reduced to Option A (12 tasks x 3 arms x 2 repeats) and H2e (style contrast) added, before any RQ2 data | Free-tier budget: Hindi simulations cost ~3.5x English; decided by the author on 2026-10-20 |
