# Pre-registration: Whose noise is it?

> **DRAFT - NOT FROZEN.** This document is still being edited. Before the first **main** run
> (anything outside the pilots in section 9) it must be frozen:
> 1. resolve every `TO VERIFY` / `TBD` item below,
> 2. change this banner to `FROZEN on <date> at commit <hash>`,
> 3. commit and tag it (the author does all git operations himself).
>
> After the freeze, the frozen text is never edited. Every change goes in the **Deviations log**
> (section 12), with a date and a reason, and any analysis affected by a deviation is reported as
> exploratory.

| Field | Value |
|---|---|
| Title | Whose noise is it? Decomposing tau-bench reliability noise for a small open agent with English and Hindi/Hinglish text users |
| Author | Nilay Joshi (Graphic Era Hill University) |
| Draft date | 2026-10-11 |
| Code | this repository (`whose_noise` package, `scripts/run_grid.py`) |
| Benchmark | tau2-bench v1.0.1, retail domain. RQ2 uses the `tau-multilingual` branch (HEAD `8ff48b8e` on 2026-10-11; `TBD` the exact commit at freeze) |

---

## 1. Research questions

- **RQ1 (noise decomposition).** With the agent fixed, how is the variance of the binary tau2 verdict
  split between task, user simulator (mean shift, and task x simulator interaction) and re-run noise?
  How much of the re-run noise starts on the agent side and how much on the simulator side?
- **RQ2 (language).** Is the agent's success rate with a Hindi romanized text user different from
  English by more than the run-to-run noise floor? Is the same true for Hinglish code-mixed text?

## 2. Hypotheses and estimands

All estimands are on the probability scale. They are defined on the task population that the 29
judge-free retail test tasks stand for (section 4).

| ID | Type | Estimand | Prediction | Decision rule |
|---|---|---|---|---|
| H1a | confirmatory | Re-run (residual) component `E[p_ts (1 - p_ts)]`, with the agent and simulator at temperature 0 | > 0. Serving nondeterminism alone makes verdicts flip. | Lower bound of the 95% task-bootstrap CI > 0 |
| H1b | confirmatory | `task_x_simulator` component (Design A) | > 0. Changing the simulator model changes *which* tasks pass, not just the mean. | Lower bound of the 95% CI > 0 |
| H1c | exploratory | Share of repeat pairs whose first divergent message is a **user** message (first-divergence attribution, RESEARCH_NOTES section 5) | No directional prediction. Reported with a 95% task-bootstrap CI. | - |
| H2 | **primary** confirmatory | `Delta_rom = pass^1(hi_romanized) - pass^1(en)`, paired by task | != 0 (two-sided) | Paired task-cluster bootstrap 95% CI excludes 0 |
| H2b | secondary | `Delta_cm = pass^1(hi_code_mixed) - pass^1(en)` | != 0 | Same rule. Holm correction across {H2, H2b} reported alongside. |
| H2c | secondary | Difference in infra-error rate and in `max_steps` / `too_many_errors` termination rate, hi vs en | none | Descriptive, with Wilson CIs per condition |
| H2d | exploratory | Residual (re-run) component in hi vs en (`compare_decompositions`) | none | 95% CI of the difference |

"Beyond the noise floor" is defined operationally. H2 holds only if the paired CI excludes 0. We
**also** report `|Delta_rom|` next to the noise-floor band of the English condition
(`variance.noise_floor`, 95% band for two identical single runs on the same 29 tasks x 3 trials). The
band is context for the reader, not a second test.

## 3. Outcome

- Unit: one tau2 simulation (one task, one trial, one condition).
- `y = 1` if `reward_info` is not null and `|reward - 1| <= 1e-6`, else `0`. This is the same as tau2's
  `is_successful`.
- `termination_reason == "infrastructure_error"`, or a null `reward_info`, is **missing**, not 0.
  `whose_noise.tau2_results` flags these rows (`is_infra_error`), and they are excluded from every rate.

## 4. Task set

- Domain: retail. Split: `test` from `data/tau2/domains/retail/split_tasks.json` (40 tasks).
- **Primary set: the 29 test tasks with no NL assertions.** Their reward is a deterministic DB/action
  check, with no LLM judge:
  `5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102`
- Excluded (NL assertions scored by a hard-coded LLM judge, see RESEARCH_NOTES section 6):
  `36, 38, 39, 40, 45, 60, 62, 68, 70, 108, 111`.
- Rationale: the judge is a third stochastic model. Its model differs between `main`
  (`gpt-4.1-2025-04-14`) and `tau-multilingual` (`gpt-5.4-mini-2026-03-17`). It needs a paid
  `OPENAI_API_KEY`, and when that key is missing the whole conversation is lost (pilot1, tasks 2-4).
- Hindi task ids are `<id>_hi` (task set `retail_hi`). The branch's `retail_hi` keeps the English task
  prose and `evaluation_criteria` unchanged for all 114 tasks, so pairing on the stripped id is valid.
  `retail_hi_identity` is **not** used, because it changes names, addresses and the initial DB.
- The train split (74 tasks) is reserved for pilots and debugging and never enters a confirmatory
  analysis.

## 5. Conditions and fixed settings

| Setting | Value |
|---|---|
| Agent | Qwen3-4B-Instruct-2507, GGUF Q4_K_M (unsloth), llama.cpp `llama-server` release b11552 (Windows CUDA 12.4 build), `--jinja -ngl all -c 16384`, alias `qwen3-4b`. `TO VERIFY` before freeze: default serving (auto slots = 4, unified KV, prompt cache on, as in the pilot log) vs `-np 1 --no-cache-prompt`. Decided by the determinism audit (RESEARCH_NOTES section 5). Whatever is chosen is fixed for all runs. |
| Agent args | `openai/qwen3-4b`, `api_base http://127.0.0.1:8080/v1`, temperature 0.0 |
| User simulator (RQ2 and RQ1 reference) | `groq/openai/gpt-oss-120b`, temperature 0.0 |
| Extra simulators (RQ1) | `groq/qwen/qwen3.8-27b`, temperature 0.0. Optional third: `gemini/gemini-3.8-flash` (`TBD`: include only if its quota allows a balanced grid) |
| Trials per cell | R = 3 |
| tau2 seed | 300 (tau2 derives the per-trial seeds from it; the agent and user get the same seed) |
| Concurrency | `--max-concurrency 1` |
| English condition | default tau2 retail user (`retail` task set on the same branch commit as Hindi) |
| Hindi romanized | `--task-set-name retail_hi --user-persona-id rishika_hindi_v1 --text-input-style romanized` |
| Hinglish code-mixed | `--task-set-name retail_hi --user-persona-id rishika_hindi_v1 --text-input-style code_mixed` |
| Branch | All RQ2 conditions on the **same** `tau-multilingual` commit. RQ1 also runs there, so the English / gpt-oss-120b cell is shared between RQ1 and RQ2. |

The persona is pinned to `rishika_hindi_v1` (described as having "high tolerance for an English-only
agent") rather than sampled with `--user-persona-id hi`. Sampling would mix two personas with opposite
English tolerance into the language effect. `TO VERIFY` in the Hindi pilot: whether the pack's
`agent_language_clause` is added to the agent's system prompt automatically, and whether a single
persona can be pinned on a text run.

## 6. Design and sample size

- **RQ1, Design A (crossed):** 29 tasks x S simulators x 3 repeats, English, everything else fixed.
  S = 2 is the minimum (S = 3 if quota allows).
- **RQ2:** 29 tasks x 3 conditions (en, hi_romanized, hi_code_mixed) x 3 repeats, gpt-oss-120b
  simulator.
- Total simulations (S = 2): RQ1 2 x 87 + RQ2 2 x 87 extra (the en cell is shared) = **348**.
- Budget (gpt-oss-120b, Groq free plan, 200K tokens/day): 3 x 87 = 261 simulations x 6-9K tokens is
  about 1.6-2.3M tokens, i.e. **8-12 days** on that one model. Hindi token use per simulation is
  **unmeasured** and may be higher. The Hindi pilot measures it and this estimate gets updated before
  the freeze.
- **Power (illustrative only, not a result).** The paired SE of `Delta` is `SD(d_t) / sqrt(29)`, where
  `d_t` is the per-task difference of mean success. If `SD(d_t)` = 0.3, then SE ~ 0.056 and the MDE at
  80% power / alpha 0.05 ~ 2.8 x SE ~ **0.16**. If `SD(d_t)` = 0.4, the MDE ~ 0.21. The real `SD(d_t)`
  is estimated from the pilots and written here at freeze (`TBD`). Smaller true differences are likely
  to be missed, and a null H2 will be reported as "not detected", not as "no difference".

## 7. Analysis plan

Everything below uses this repo's code. Bootstrap seed 20261011 (`variance.DEFAULT_BOOTSTRAP_SEED`),
B = 10,000 replicates, percentile intervals, resampling **tasks** (whole task slabs, all conditions
together for paired contrasts).

1. Load runs with `tau2_results.load_runs`. Report simulations, infra errors and termination reasons for
   each condition.
2. Report pass^1, pass^2 and pass^3 for each condition (`metrics.pass_hat_k`, the unbiased estimator of
   Yao et al. 2024) with task-cluster bootstrap CIs (`metrics.cluster_bootstrap_ci`).
3. **H1a/H1b:** `variance.decompose_variance(valid, sim_col="user_llm")`. Report all four raw
   components, the truncated shares and the mean success rate `p_bar`. The `simulator` component is
   reported but **not interpreted**, because its CI under-covers with S <= 3 (RESEARCH_NOTES section
   4). Also report `cell_flip_rate` (with R = 3), `within_cell_disagreement` and
   `between_simulator_disagreement`.
4. **H2/H2b:** `metrics.paired_bootstrap_diff(outcomes_hi, outcomes_en, k=1)` on stripped task ids.
   Report the estimate, the 95% CI and Holm-adjusted conclusions. Sensitivity checks (not
   confirmatory): (a) the exact McNemar test on per-task majority verdicts (`metrics.mcnemar_exact`);
   (b) the same contrast for pass^3.
5. **Noise floor:** `variance.noise_floor(valid_en, sim_col=None, n_eval_tasks=29, trials_per_run=3)`.
6. **H2d:** `variance.compare_decompositions(en, hi, design="crossed")` with a single simulator, which
   gives task and residual only.
7. **Failure analysis (exploratory):** for each condition, sample up to 20 failed simulations (seeded:
   `numpy.random.default_rng(20261011)`). Label each one `agent-fault` / `simulator-fault` / `both` /
   `unclear` with the rubric in RESEARCH_NOTES section 7. If a second labeller is available, report
   Cohen's kappa on a 20-item overlap.

## 8. Missing data and exclusions

- Infra errors are re-run with the same command (`--auto-resume` retries them), up to **3** extra
  attempts per cell.
- If a cell is still incomplete after that, the **whole task** is dropped from every condition in
  that analysis (never single runs). Dropped tasks are listed. If more than 3 of the 29 tasks are
  dropped, this is reported as a deviation.
- Infra-error rates are reported for each condition **before** re-runs (H2c), because failures that
  depend on the language are a finding.
- No simulation is excluded for any other reason, such as a transcript that "looks broken". Such cases
  are labelled in step 7, not removed.

## 9. Pilots (not confirmatory)

- `pilot1`: retail train tasks 0-4 x 3 trials, English, on `main`. Used for plumbing, token
  measurement and timing.
- `TBD` Hindi pilot: 3-5 judge-free **train** tasks x 2 trials per Hindi style. It checks the flags in
  section 5, the token use and the infra-error rate.
- Pilot data are never pooled with main-run data.

## 10. Stopping rules

- Each condition runs until every cell of the design has 3 valid repeats, or until the missing-data
  rule in section 8 applies. No condition is stopped early because of its interim results.
- The H2 contrast is **not computed** until all three RQ2 conditions are complete. `analyze.py` may be
  run on single conditions during collection (to monitor infra errors), but never on hi and en
  together.

## 11. What would change the conclusions

- If `p_bar` < 0.1 or > 0.9 in English, the components are floor- or ceiling-limited. Shares are then
  reported but flagged as uninformative.
- If the infra-error rate differs between hi and en by more than 10 points, H2 is reported together
  with a worst-case bound that counts the extra Hindi infra errors as failures.

## 12. Deviations log

| Date | Section | Change | Reason |
|---|---|---|---|
| - | - | - | - |
