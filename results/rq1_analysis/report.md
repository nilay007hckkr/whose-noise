# RQ1 analysis: whose noise is it?

> Confirmatory run: final data, the pre-registered settings and no protocol warning.

| Item | Value |
|---|---|
| Analysis timestamp | 2026-10-25T21:05:02+05:30 |
| Repo commit (HEAD) | 5eb3d6a4252ee1f64f12a27d44fbc0403bf8b25d (refs/heads/main) |
| Pre-registration tag | prereg-v1 -> 6a1f66191b5ad4b392ab9f61cb6e523a8f6be2bb (HEAD differs) |
| Git note | read from .git files without running git; uncommitted changes are not visible this way (the sha256 of the analysis code is recorded instead) |
| PREREGISTRATION.md sha256 | a13c478197463da7c33a7813d1a7bce3ea84b4dfa5977340e69d2bcbd23465f8 |
| Analysis code sha256 (first 12 hex; full in results.json) | scripts/analyze_rq1.py: 7e7607e9ded5<br>src/whose_noise/metrics.py: a8885d2f0cc9<br>src/whose_noise/variance.py: d1d75436a0b5<br>src/whose_noise/tau2_results.py: 49dead9d0453<br>src/whose_noise/fidelity.py: 618b71c2154f<br>src/whose_noise/divergence.py: f7d3ac4c3b63<br>src/whose_noise/analyze.py: 3dc79725346c |
| Bootstrap | seed 20261011, B = 10000, percentile, resampling tasks, 95% intervals |
| Cache-ram cutoff (section 12) | 2026-10-12T20:23:00 IST |
| Input gpt-oss-120b | `../tau2-bench/data/simulations/rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638/results.json`<br>sha256 `0ccd666fb22c65cff9031a44222ebeffd4be3ab246158aa3557739633d7ddd1d`<br>run `rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638`, user `groq/openai/gpt-oss-120b`, agent `openai/qwen3-4b`, 87 simulations, tau2 commit 4ce7c039, seed 300 |
| Input qwen3.8-27b | `../tau2-bench/data/simulations/rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908/results.json`<br>sha256 `be1625c512c7cd4c03b0374c3d287607a67bfbb615a6c6792431adc110e70a46`<br>run `rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908`, user `groq/qwen/qwen3.8-27b`, agent `openai/qwen3-4b`, 87 simulations, tau2 commit 4ce7c039, seed 300 |

## Key results

- Data: FINAL; analysed tasks 29 of 29; dropped 0; pending 0; awaiting infra re-runs 0.
- pass^1 gpt-oss-120b: 0.310 [0.172, 0.448]; pass^3: 0.138 [0.034, 0.276]
- pass^1 qwen3.8-27b: 0.563 [0.391, 0.736]; pass^3: 0.517 [0.345, 0.690]
- p_bar 0.437 [0.305, 0.569]; residual 0.0690 [0.0345, 0.1092]; task_x_simulator 0.0868 [0.0361, 0.1289]
- H1a: SUPPORTED (lower bound 0.0345 > 0)
- H1b: SUPPORTED (lower bound 0.0361 > 0)
- Section 11 floor/ceiling flag: not flagged
- Noise floor (gpt-oss cells, 29 tasks, single runs): 95% band +/-0.166
- Section 12 (descriptive; confounded with task/time; variants chosen post hoc, none primary): residual before_before 0.0667 [0.0000, 0.1667] vs after_after 0.0649 [0.0238, 0.1157]; within mixed cells, cross minus same-period -0.0893 [-0.1389, -0.0357]
- H1c (exploratory) share_user, pooled: 0.716 [0.640, 0.795] of 148 diverging pairs; after_after pairs only 0.800 [0.713, 0.885] of 110 (the user simulator speaks first: see the H1c caveats)

## Section 8: missing data and the analysis set

**FINAL DATA:** every planned task is complete for both simulators or dropped under section 8. Infra errors are re-run with the same command (`--auto-resume`), up to 3 extra attempts per cell; a task is dropped whole from every condition only when a cell is still incomplete after that. tau2 deletes an infra-error simulation from results.json before re-running it, so the file cannot show how many re-runs were made: a cell that still holds an infra error is **awaiting re-run** until the user confirms its re-runs are exhausted (`--retries-exhausted TASK ...`, or `extra_attempts >= 3` in a `--rerun-evidence` file built from the run logs).

- Dropped tasks (0): (none)
- **Analysed (complete for both simulators, 29):** 5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102
- Complete for gpt-oss-120b, not dropped (29): 5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102
- Complete for qwen3.8-27b, not dropped (29): 5, 9, 12, 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102

Per task: successes / valid repeats (simulations run) for each simulator.

| task | gpt-oss-120b | qwen3.8-27b | status |
|---|---|---|---|
| 5 | 0/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 9 | 2/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 12 | 3/3 valid (3 run) | 3/3 valid (3 run), 1 extra attempts | complete |
| 17 | 2/3 valid (3 run) | 3/3 valid (3 run), 1 extra attempts | complete |
| 18 | 1/3 valid (3 run) | 3/3 valid (3 run), 1 extra attempts | complete |
| 26 | 2/3 valid (3 run), 1 extra attempts | 3/3 valid (3 run), 1 extra attempts | complete |
| 27 | 0/3 valid (3 run), 1 extra attempts | 3/3 valid (3 run), 1 extra attempts | complete |
| 32 | 0/3 valid (3 run), 1 extra attempts | 1/3 valid (3 run), 1 extra attempts | complete |
| 33 | 1/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 42 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 49 | 1/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 51 | 2/3 valid (3 run) | 1/3 valid (3 run) | complete |
| 53 | 2/3 valid (3 run) | 2/3 valid (3 run) | complete |
| 55 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 56 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 61 | 3/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 64 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 65 | 3/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 71 | 0/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 74 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 77 | 3/3 valid (3 run) | 3/3 valid (3 run) | complete |
| 79 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 86 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 90 | 2/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 94 | 0/3 valid (3 run) | 0/3 valid (3 run) | complete |
| 97 | 0/3 valid (3 run) | 3/3 valid (3 run), 1 extra attempts | complete |
| 100 | 0/3 valid (3 run) | 3/3 valid (3 run), 1 extra attempts | complete |
| 101 | 0/3 valid (3 run) | 0/3 valid (3 run), 1 extra attempts | complete |
| 102 | 0/3 valid (3 run) | 3/3 valid (3 run) | complete |

## Step 1: simulations, infra errors, termination reasons

| simulator | scope | sims | infra | valid | successes | tasks | termination reasons | infra errors |
|---|---|---|---|---|---|---|---|---|
| gpt-oss-120b | file | 87 | 0 | 87 | 27 | 29 | user_stop=87 | - |
| gpt-oss-120b | analysed set | 87 | 0 | 87 | 27 | 29 | user_stop=87 | - |
| qwen3.8-27b | file | 87 | 0 | 87 | 49 | 29 | user_stop=87 | - |
| qwen3.8-27b | analysed set | 87 | 0 | 87 | 49 | 29 | user_stop=87 | - |

Infra errors are missing data, not failures (section 3). The counts above are the current file state, i.e. AFTER `--auto-resume` re-runs replaced infra errors.

**Infra errors before re-runs (section 8).**

| simulator | infra before re-runs | sims in file | rate | infra in file now |
|---|---|---|---|---|
| gpt-oss-120b | 3 | 87 | 0.034 | 0 |
| qwen3.8-27b | 9 | 87 | 0.103 | 0 |

Source (`--rerun-evidence`): results/logs/rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638.log and results/logs/rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908.log: tau2 summary blocks ('Infra Errors N') at the end of each invocation and the following 'Resuming run from ...' lines. gpt-oss: 3 first-attempt infra errors (2026-10-12, Groq TPD; tasks 26, 27, 32 trial 2), each re-run once and valid. qwen3.8: 6 (2026-10-11, Groq TPD; tasks 12, 17, 18, 26, 27, 32 trial 2) and 3 (2026-10-24, Groq TPD; tasks 97, 100, 101 trial 2), each re-run once and valid. Compiled 2026-10-25.. results.json cannot give these counts: tau2 --auto-resume deletes infra-error simulations before re-running them, so the file shows infra errors AFTER re-runs only. The the rate divides by the (task, trial) simulations in the file, each first attempted once.

## Step 2: pass^k per simulator (task-cluster bootstrap 95% CI)

| simulator | tasks | pass^1 | pass^2 | pass^3 |
|---|---|---|---|---|
| gpt-oss-120b | 29 | 0.310 [0.172, 0.448] | 0.207 [0.092, 0.333] | 0.138 [0.034, 0.276] |
| qwen3.8-27b | 29 | 0.563 [0.391, 0.736] | 0.529 [0.345, 0.701] | 0.517 [0.345, 0.690] |

Unbiased pass^k estimator (Yao et al. 2024) averaged over the analysed tasks.

### Exploratory, not in the RQ1 analysis plan: paired simulator contrast (gpt-oss-120b minus qwen3.8-27b)

Section 7 step 2 asks only for pass^k per condition in RQ1; the paired bootstrap and McNemar are pre-registered for H2, not for RQ1. These were added after RQ1 data collection began. An interval that excludes 0 here is not a pre-registered finding.

| contrast | estimate [95% CI] |
|---|---|
| pass^1 | -0.253 [-0.425, -0.092] |
| pass^3 | -0.379 [-0.552, -0.207] |

McNemar on per-task majority verdicts (exploratory): gpt-oss fails & qwen passes 8, gpt-oss passes & qwen fails 2, exact p = 0.109.

## Step 3: variance decomposition (Design A, H1a / H1b)

`variance.decompose_variance(valid, sim_col='user_llm')` on 29 tasks x 2 simulators x 3 repeats. Raw components are unbiased and may be negative; truncated shares are biased upward for small components; untruncated shares are approximately unbiased but may leave [0, 1].

| component | raw [95% CI] | frac boot <= 0 | share, truncated [CI] | share, untruncated [CI] |
|---|---|---|---|---|
| task | 0.0809 [0.0208, 0.1352] | 0.003 | 0.305 [0.080, 0.532] | 0.305 [0.080, 0.532] |
| simulator (not interpreted) | 0.0282 [0.0008, 0.0862] | 0.018 | 0.106 [0.003, 0.293] | 0.106 [0.003, 0.293] |
| task_x_simulator | 0.0868 [0.0361, 0.1289] | 0.000 | 0.328 [0.142, 0.496] | 0.328 [0.142, 0.496] |
| residual | 0.0690 [0.0345, 0.1092] | 0.000 | 0.260 [0.126, 0.422] | 0.260 [0.126, 0.422] |

| statistic | estimate [95% CI] |
|---|---|
| p_bar | 0.437 [0.305, 0.569] |
| cell_flip_rate (R = 3) | 0.207 [0.103, 0.328] |
| within_cell_disagreement | 0.138 [0.069, 0.218] |
| between_simulator_disagreement | 0.368 [0.234, 0.513] |
| excess_between_disagreement (exploratory, not in the plan) | 0.230 [0.088, 0.387] |
| flip_rate_mixed_tasks (point; exploratory, not in the plan) | 0.333 |

Simulator means: gpt-oss-120b 0.310, qwen3.8-27b 0.563. Sum of raw components 0.2648 vs p_bar (1 - p_bar) = 0.2460 (equal in expectation, not in a finite sample).

reported, not interpreted: with S = 2 simulators the task bootstrap treats them as fixed and this interval under-covers (PREREGISTRATION section 7 step 3).

## Pre-registered decisions (section 2)

| hypothesis | type | estimand | estimate [95% CI] | rule | verdict |
|---|---|---|---|---|---|
| H1a | confirmatory | residual (re-run) component E[p(1-p)] | 0.0690 [0.0345, 0.1092] | lower bound of the 95% task-bootstrap CI > 0 | SUPPORTED (lower bound 0.0345 > 0) |
| H1b | confirmatory | task_x_simulator component | 0.0868 [0.0361, 0.1289] | lower bound of the 95% task-bootstrap CI > 0 | SUPPORTED (lower bound 0.0361 > 0) |

A verdict of NOT SUPPORTED means not detected at this sample size, not evidence of absence. H1a is read together with the section 12 sensitivity analysis below. Everything else in this report is descriptive or exploratory.

## Section 11: p_bar floor / ceiling check

p_bar < 0.1 or > 0.9 in an arm: components are floor- or ceiling-limited; shares are reported but flagged as uninformative.

| arm | p_bar | outside [0.1, 0.9] |
|---|---|---|
| pooled (decomposition) | 0.437 | False |
| gpt-oss-120b | 0.310 | False |
| qwen3.8-27b | 0.563 | False |

Shares flagged as uninformative: no.

## Step 5: noise floor on the gpt-oss-120b cells

`variance.noise_floor(sim_col=None, n_eval_tasks=29)`: the gap in pass^1 between two identical runs on the same tasks.

| trials per run | residual [CI] | sd of D [CI] | E\|D\| [CI] | 95% band [CI] |
|---|---|---|---|---|
| 3 | 0.1034 [0.0460, 0.1609] | 0.049 [0.033, 0.061] | 0.039 [0.026, 0.049] | +/-0.096 [0.064, 0.119] |
| 1 | 0.1034 [0.0460, 0.1609] | 0.084 [0.056, 0.105] | 0.067 [0.045, 0.084] | +/-0.166 [0.110, 0.206] |

## Section 12: `--cache-ram` sensitivity (descriptive)

Simulations started before vs after 2026-10-12T20:23:00 IST (llama-server `--cache-ram 512` added). Task-cluster bootstrap 95% CIs; bootstrap draws in which a class is empty are left out (n undefined). Classes with fewer than 5 tasks, or with p_bar outside [0.1, 0.9] (section 11), are flagged: their intervals are not interpretable. With fewer than 20 tasks per class the percentile intervals under-cover, so an interval that excludes 0 here is not by itself evidence of a --cache-ram effect.

**Confound:** The period is not randomised: simulations before the change are the earliest runs (the first task chunk and, for each simulator, the first trials), and the after period holds all later tasks and calendar days. Period is therefore confounded with task, simulator mix, trial index and time (e.g. drift in the hosted simulator). Differences are descriptive and cannot be attributed to --cache-ram.

**Variants:** The deviation entry asks for a before/after comparison of the residual but does not fix how. The three variants here (pair level, within mixed cells, cell level) were chosen after RQ1 data collection began; none is primary. H1c (exploratory section below) attributes the same residual and is also split by these period classes.

**(a) Pair level (uses every simulation).** Every unordered pair of repeats in a cell is before_before, after_after or cross; residual = half the pair disagreement rate (unbiased for E[p(1-p)] within a period).

| pair class | pairs | disagreeing | tasks | residual [CI] | n undefined | flag |
|---|---|---|---|---|---|---|
| before_before | 15 | 2 | 8 | 0.0667 [0.0000, 0.1667] | 1 |  |
| after_after | 131 | 17 | 26 | 0.0649 [0.0238, 0.1157] | 0 |  |
| cross | 28 | 5 | 8 | 0.0893 [0.0357, 0.1389] | 1 |  |
| after_after - before_before |  |  |  | -0.0018 [-0.1174, 0.0888] | 1 |  |

**(b) Within mixed cells (matched on task and simulator).** Cells with repeats in both periods: cross-period pairs vs same-period pairs of the same cells. If the change shifted a cell's success probability, cross pairs disagree more. Still confounded with trial order and calendar time.

| pair type | pairs | disagreeing | tasks | residual [CI] | n undefined |
|---|---|---|---|---|---|
| same_period | 14 | 5 | 8 | 0.1786 [0.0714, 0.2778] | 1 |
| cross | 28 | 5 | 8 | 0.0893 [0.0357, 0.1389] | 1 |
| cross - same_period |  |  |  | -0.0893 [-0.1389, -0.0357] | 1 |

**(c) Cell level.** Cells whose 3 repeats all started in one period (else mixed); residual = MS_E; within-cell flip rate with R = 3.

| cell class | cells | tasks | residual (MS_E) [CI] | flip rate [CI] | p_bar | flag |
|---|---|---|---|---|---|---|
| before | 2 | 2 | 0.0000 [0.0000, 0.0000] | 0.000 [0.000, 0.000] | 1.000 | few tasks, p_bar floor/ceiling |
| after | 42 | 21 | 0.0556 [0.0152, 0.1053] | 0.167 [0.045, 0.316] | 0.349 |  |
| mixed | 14 | 8 | 0.1190 [0.0476, 0.1852] | 0.357 [0.143, 0.556] | 0.619 |  |
| after - before |  |  | 0.0556 [0.0152, 0.1053] | 0.167 [0.045, 0.316] |  | not interpretable |

Composition (who is in each class):

| cell class | simulator | cells | tasks |
|---|---|---|---|
| before | gpt-oss-120b | 0 | (none) |
| before | qwen3.8-27b | 2 | 5, 9 |
| after | gpt-oss-120b | 21 | 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102 |
| after | qwen3.8-27b | 21 | 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102 |
| mixed | gpt-oss-120b | 8 | 5, 9, 12, 17, 18, 26, 27, 32 |
| mixed | qwen3.8-27b | 6 | 12, 17, 18, 26, 27, 32 |

| pair class | simulator | pairs | disagreeing | residual (point) |
|---|---|---|---|---|
| before_before | gpt-oss-120b | 3 | 1 | 0.1667 |
| before_before | qwen3.8-27b | 12 | 1 | 0.0417 |
| after_after | gpt-oss-120b | 68 | 13 | 0.0956 |
| after_after | qwen3.8-27b | 63 | 4 | 0.0317 |
| cross | gpt-oss-120b | 16 | 4 | 0.1250 |
| cross | qwen3.8-27b | 12 | 1 | 0.0417 |

Simulations per period: gpt-oss-120b before 11, after 76, unknown 0; qwen3.8-27b before 18, after 69, unknown 0. Tasks with runs before the cutoff: gpt-oss-120b: 5, 9, 12, 17, 18, 26, 27, 32; qwen3.8-27b: 5, 9, 12, 17, 18, 26, 27, 32.

## Exploratory: H1c first-divergence attribution

`whose_noise.divergence` (RESEARCH_NOTES section 5) on the analysed tasks: 174 repeat pairs over 29 tasks. share_user = pairs whose first differing message is a user (simulator) message / diverging pairs (the pre-registered H1c estimand). Task-cluster bootstrap 95% CIs; draws in which a row has no diverging pair define no share and are left out of its interval (n undefined). Rows with fewer than 5 tasks are flagged as not interpretable. Exploratory, no prediction.

**How to read the shares (caveats).** (1) Turn order: tau2 opens every conversation with a scripted agent greeting ("Hi! How can I help you today?", not a generation), then the user simulator generates first. A user divergence at its first turn means the agent never generated on identical input in that pair, so share_user mixes per-generation noise with the simulator's earlier chance to diverge. The context table below gives the identical-input generations per side and a per-generation divergence rate. (2) Seeds: tau2 derives one seed per trial and sends it to BOTH the agent and the user LLM, so two repeats send identical messages with different `seed` values. llama.cpp at temperature 0 is expected to ignore the seed (not verified for the pinned build) and Groq documents `seed` as best-effort, so a divergence names a component but cannot separate serving nondeterminism from a seed effect inside it.

| subset | group | tasks | pairs | identical | diverging | user | assistant | tool | length | other | share_user [CI] | share_agent [CI] | n undefined | flag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | gpt-oss-120b | 29 | 87 | 0 | 87 | 81 | 6 | 0 | 0 | 0 | 0.931 [0.862, 0.977] | 0.069 [0.023, 0.138] | 0 |  |
| all | qwen3.8-27b | 29 | 87 | 26 | 61 | 25 | 36 | 0 | 0 | 0 | 0.410 [0.218, 0.592] | 0.590 [0.408, 0.782] | 0 |  |
| all | pooled | 29 | 174 | 26 | 148 | 106 | 42 | 0 | 0 | 0 | 0.716 [0.640, 0.795] | 0.284 [0.205, 0.360] | 0 |  |
| verdict_differs | gpt-oss-120b | 9 | 18 | 0 | 18 | 16 | 2 | 0 | 0 | 0 | 0.889 [0.727, 1.000] | 0.111 [0.000, 0.273] | 0 |  |
| verdict_differs | qwen3.8-27b | 3 | 6 | 0 | 6 | 3 | 3 | 0 | 0 | 0 | 0.500 [0.000, 1.000] | 0.500 [0.000, 1.000] | 387 | few tasks: not interpretable |
| verdict_differs | pooled | 10 | 24 | 0 | 24 | 19 | 5 | 0 | 0 | 0 | 0.792 [0.571, 1.000] | 0.208 [0.000, 0.429] | 0 |  |

### H1c by period class of the two repeats (descriptive)

Pair classes by the start times of the two repeats around the section 12 cutoff. A cross pair compares two llama-server setups (prompt-cache restores can change the agent's floating-point results), not two re-runs under one setup. Period is confounded with task chunk, trial order and calendar date, so the split is descriptive. This is the H1c side of the section 12 sensitivity analysis.

- gpt-oss-120b: tasks with after_after pairs 17, 18, 26, 27, 32, 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102; tasks with NO after_after pair (only before_before / cross): 5, 9, 12.
- qwen3.8-27b: tasks with after_after pairs 33, 42, 49, 51, 53, 55, 56, 61, 64, 65, 71, 74, 77, 79, 86, 90, 94, 97, 100, 101, 102; tasks with NO after_after pair (only before_before / cross): 5, 9, 12, 17, 18, 26, 27, 32.

| subset | simulator | period class | tasks | pairs | identical | diverging | user | assistant | tool | length | other | share_user [CI] | share_agent [CI] | n undefined | flag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | gpt-oss-120b | before_before | 3 | 3 | 0 | 3 | 2 | 1 | 0 | 0 | 0 | 0.667 [0.000, 1.000] | 0.333 [0.000, 1.000] | 436 | few tasks: not interpretable |
| all | gpt-oss-120b | after_after | 26 | 68 | 0 | 68 | 63 | 5 | 0 | 0 | 0 | 0.926 [0.851, 0.986] | 0.074 [0.014, 0.149] | 0 |  |
| all | gpt-oss-120b | cross | 8 | 16 | 0 | 16 | 16 | 0 | 0 | 0 | 0 | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1 |  |
| all | qwen3.8-27b | before_before | 8 | 12 | 4 | 8 | 0 | 8 | 0 | 0 | 0 | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 22 |  |
| all | qwen3.8-27b | after_after | 21 | 63 | 21 | 42 | 25 | 17 | 0 | 0 | 0 | 0.595 [0.366, 0.804] | 0.405 [0.196, 0.634] | 0 |  |
| all | qwen3.8-27b | cross | 6 | 12 | 1 | 11 | 0 | 11 | 0 | 0 | 0 | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 14 |  |
| verdict_differs | gpt-oss-120b | before_before | 1 | 1 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 3587 | few tasks: not interpretable |
| verdict_differs | gpt-oss-120b | after_after | 8 | 13 | 0 | 13 | 11 | 2 | 0 | 0 | 0 | 0.846 [0.667, 1.000] | 0.154 [0.000, 0.333] | 0 |  |
| verdict_differs | gpt-oss-120b | cross | 4 | 4 | 0 | 4 | 4 | 0 | 0 | 0 | 0 | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 137 | few tasks: not interpretable |
| verdict_differs | qwen3.8-27b | before_before | 1 | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 3542 | few tasks: not interpretable |
| verdict_differs | qwen3.8-27b | after_after | 2 | 4 | 0 | 4 | 3 | 1 | 0 | 0 | 0 | 0.750 [0.500, 1.000] | 0.250 [0.000, 0.500] | 1248 | few tasks: not interpretable |
| verdict_differs | qwen3.8-27b | cross | 1 | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 3542 | few tasks: not interpretable |

**Sensitivity: after_after pairs only** (both repeats under `--cache-ram 512`):

| subset | group | tasks | pairs | identical | diverging | user | assistant | tool | length | other | share_user [CI] | share_agent [CI] | n undefined | flag |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | gpt-oss-120b | 26 | 68 | 0 | 68 | 63 | 5 | 0 | 0 | 0 | 0.926 [0.848, 0.986] | 0.074 [0.014, 0.152] | 0 |  |
| all | qwen3.8-27b | 21 | 63 | 21 | 42 | 25 | 17 | 0 | 0 | 0 | 0.595 [0.364, 0.800] | 0.405 [0.200, 0.636] | 0 |  |
| all | pooled | 26 | 131 | 21 | 110 | 88 | 22 | 0 | 0 | 0 | 0.800 [0.713, 0.885] | 0.200 [0.115, 0.287] | 0 |  |
| verdict_differs | gpt-oss-120b | 8 | 13 | 0 | 13 | 11 | 2 | 0 | 0 | 0 | 0.846 [0.667, 1.000] | 0.154 [0.000, 0.333] | 3 |  |
| verdict_differs | qwen3.8-27b | 2 | 4 | 0 | 4 | 3 | 1 | 0 | 0 | 0 | 0.750 [0.500, 1.000] | 0.250 [0.000, 0.500] | 1241 | few tasks: not interpretable |
| verdict_differs | pooled | 8 | 17 | 0 | 17 | 14 | 3 | 0 | 0 | 0 | 0.824 [0.625, 1.000] | 0.176 [0.000, 0.375] | 3 |  |

### H1c context: turn order and identical-input generations (descriptive)

Per pair, every message up to and including the first divergence was generated on identical input in both repeats (all messages for identical pairs; those before the divergence for length pairs). Rate = first divergences / identical-input generations of that side. The scripted greeting and tool results are not generations. No interval: a description of exposure, not an estimand.

| group | side | identical-input generations | first divergences | rate |
|---|---|---|---|---|
| gpt-oss-120b | agent | 44 | 6 | 0.136 |
| gpt-oss-120b | user | 106 | 81 | 0.764 |
| qwen3.8-27b | agent | 600 | 36 | 0.060 |
| qwen3.8-27b | user | 344 | 25 | 0.073 |
| pooled | agent | 644 | 42 | 0.065 |
| pooled | user | 450 | 106 | 0.236 |

- gpt-oss-120b: 68 of 87 diverging pairs first diverged at a user turn before any agent generation on identical input. First-divergence index (message position x pairs): assistant: 2x5, 14x1; user: 1x68, 3x8, 9x1, 11x4.
- qwen3.8-27b: 12 of 61 diverging pairs first diverged at a user turn before any agent generation on identical input. First-divergence index (message position x pairs): assistant: 2x12, 6x2, 8x2, 10x6, 12x7, 14x4, 16x1, 22x2; user: 1x12, 3x2, 9x3, 13x2, 17x2, 19x2, 21x1, 23x1.
- pooled: 80 of 148 diverging pairs first diverged at a user turn before any agent generation on identical input. First-divergence index (message position x pairs): assistant: 2x17, 6x2, 8x2, 10x6, 12x7, 14x5, 16x1, 22x2; user: 1x80, 3x10, 9x4, 11x4, 13x2, 17x2, 19x2, 21x1, 23x1.

## Exploratory: simulator fidelity (deterministic transcript checks)

`whose_noise.fidelity` (the `analyze --fidelity` logic) on the analysed tasks. Screens, not verdicts: flagged transcripts must be read. Rate CIs: task-cluster bootstrap of the pooled rate over valid simulations.

| simulator | premature_stop | stop_after_unexecuted_confirmation | transfer_requested_by_customer |
|---|---|---|---|
| gpt-oss-120b | 0.126 [0.069, 0.195] | 0.138 [0.069, 0.207] | 0.126 [0.057, 0.218] |
| qwen3.8-27b | 0.000 [0.000, 0.000] | 0.023 [0.000, 0.069] | 0.115 [0.034, 0.218] |

### gpt-oss-120b (analysed tasks)

```
counts (rate) per outcome; infra errors excluded; deterministic transcript checks (whose_noise.fidelity)
text_input_style: none recorded; leak script: n/a (no language-pack style)
outcome  sims premature_stop stop_after_unexec_confirm customer_asks_transfer script_leakage leak_share customer_turns agent_turns
   pass    27       0 (0.00)                  0 (0.00)               4 (0.15)              -        n/a            6.0         6.0
   fail    60      11 (0.18)                 12 (0.20)               7 (0.12)              -        n/a            6.9         7.2
    all    87      11 (0.13)                 12 (0.14)              11 (0.13)              -        n/a            6.6         6.9
premature_stop: task 9 trial 1 (fail), task 18 trial 0 (fail), task 26 trial 2 (fail), task 27 trial 2 (fail), task 32 trial 0 (fail), task 42 trial 0 (fail), task 64 trial 0 (fail), task 64 trial 1 (fail), task 74 trial 1 (fail), task 94 trial 1 (fail), task 102 trial 2 (fail)
stop_after_unexecuted_confirmation: task 9 trial 1 (fail), task 18 trial 0 (fail), task 26 trial 2 (fail), task 27 trial 2 (fail), task 32 trial 0 (fail), task 42 trial 0 (fail), task 64 trial 0 (fail), task 64 trial 1 (fail), task 74 trial 1 (fail), task 90 trial 2 (fail), task 94 trial 1 (fail), task 102 trial 2 (fail)
transfer_requested_by_customer: task 12 trial 0 (pass), task 12 trial 1 (pass), task 12 trial 2 (pass), task 51 trial 0 (fail), task 53 trial 0 (fail), task 55 trial 2 (fail), task 65 trial 1 (pass), task 74 trial 2 (fail), task 86 trial 0 (fail), task 100 trial 2 (fail), task 102 trial 1 (fail)
```

### qwen3.8-27b (analysed tasks)

```
counts (rate) per outcome; infra errors excluded; deterministic transcript checks (whose_noise.fidelity)
text_input_style: none recorded; leak script: n/a (no language-pack style)
outcome  sims premature_stop stop_after_unexec_confirm customer_asks_transfer script_leakage leak_share customer_turns agent_turns
   pass    49       0 (0.00)                  2 (0.04)               4 (0.08)              -        n/a            7.8         8.5
   fail    38       0 (0.00)                  0 (0.00)               6 (0.16)              -        n/a            8.0         8.2
    all    87       0 (0.00)                  2 (0.02)              10 (0.11)              -        n/a            7.9         8.4
stop_after_unexecuted_confirmation: task 65 trial 1 (pass), task 65 trial 2 (pass)
transfer_requested_by_customer: task 12 trial 0 (pass), task 12 trial 1 (pass), task 12 trial 2 (pass), task 32 trial 0 (fail), task 32 trial 2 (fail), task 51 trial 0 (fail), task 51 trial 1 (fail), task 55 trial 0 (fail), task 65 trial 0 (pass), task 101 trial 2 (fail)
```

Wider scope: every valid simulation of the planned tasks that are not dropped under section 8 (includes tasks outside the analysis set, e.g. pending ones).

| simulator | sims | premature_stop | stop_after_unexecuted_confirmation | transfer_requested_by_customer |
|---|---|---|---|---|
| gpt-oss-120b | 87 | 11 (0.13) | 12 (0.14) | 11 (0.13) |
| qwen3.8-27b | 87 | 0 (0.00) | 2 (0.02) | 10 (0.11) |

## Step 8: failure analysis (LLM judges; not run here)

step 8 labels failures with LLM judges; this script makes no LLM calls. See `results/audit/`: audit_rq1_chunk1_raw.json, audit_rq1_chunk2_3_raw.json, failed_cases_rq1_chunk1.json, failed_cases_rq1_chunk2_3.json.

## Protocol checks and warnings

- none

## Deviations and caveats (section 12 of the pre-registration)

- RQ1 data collection began on 2026-10-11 against an unfrozen draft; the frozen document lists every RQ1-relevant change made since then.
- `--cache-ram 512` was added at 2026-10-12 20:23 IST (sensitivity analysis above).
- One qwen3.8 simulation (task 42, trial 0) stalled ~24 h and then completed; it is kept (duration is not an outcome).
- Erratum to the deviations log (recorded in docs/RESULTS_LOG.md; the frozen text is not edited): the simulator `num_retries 8` / exponential-backoff change and the RQ1 run-name switch are dated 2026-10-12 there, but the data show that for qwen3.8-27b they happened on 2026-10-11 at about 16:51 IST (the unused run c813736 has no retries; cd30908, created with them, starts 16:52), before its first valid simulation; the gpt-oss-120b run ce7e638 was created with them and starts 2026-10-12 20:13 IST.
