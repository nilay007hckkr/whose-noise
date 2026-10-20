# Results log

Running record of experiments, in date order. Numbers are copied from `analyze.py` /
`variance.py` output; nothing here is final. Small samples: treat as pilot evidence.

## 2026-10-11 to 2026-10-12: RQ1 chunk 1 (simulator swap, English)

**Setup.** Agent `openai/qwen3-4b` (Qwen3-4B-Instruct-2507 Q4_K_M, llama.cpp b11552, RTX 3050 6 GB,
temperature 0). Retail domain, first 8 of the 29 judge-free test tasks
(`5 9 12 17 18 26 27 32`), 3 trials, seed 300. Only the user simulator differs.
Config: `configs/rq1_simulator_swap.json` (`--task-chunk 1/4`).

| Simulator | Run | Valid sims | pass^1 | pass^2 | Flip rate |
|---|---|---|---|---|---|
| groq/qwen/qwen3.8-27b | `rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908` | 24/24 | 0.917 | 0.875 | 1/8 tasks |
| groq/openai/gpt-oss-120b | `rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638` | 21/24 (3 quota errors) | 0.458 | 0.333 | 3/8 tasks |

- Paired task-bootstrap difference in pass^1 (gpt-oss minus qwen): **-0.46, 95% CI [-0.71, -0.21]**
  (approximate: 8 tasks, percentile bootstrap under-covers below ~20 tasks).
- McNemar on per-task majority outcome: 3 vs 0 discordant tasks, p = 0.25 (underpowered).
- Variance decomposition (Design A, first 2 repeats per cell, 8 tasks x 2 simulators):
  simulator 41% (share CI 14-67%), residual 44% (14-67%), task 11% (0-29%), task x simulator 3% (0-36%).
  With S = 2 the simulator component is effectively a fixed contrast, not a population variance.

## 2026-10-12: failure audit of RQ1 chunk 1

All 13 failed (non-infra) simulations were judged by two independent LLM judges with different
lenses (neutral root-cause; simulator-fidelity audit), plus a tiebreak judge on disagreement.
Inputs and raw verdicts: `results/audit/`.

| Simulator | SIMULATOR_ERROR | AGENT_ERROR | SHARED | Failures |
|---|---|---|---|---|
| gpt-oss-120b | 5 | 5 | 1 | 11 |
| qwen3.8-27b | 2 | 0 | 0 | 2 |

Judge agreement on root cause: 12/13 (raw, not chance-corrected).

Recurring simulator failures (gpt-oss-120b): "Yes" sent together with `###STOP###` so the agent
cannot act (3 cases); ignoring a scripted conditional switch (task 5, twice); withholding
`known_info` (task 17); choosing an option that contradicts its own goal (task 9).
qwen3.8-27b: invented "no payment method" on task 32 (both its failures).

Recurring agent failures (Qwen3-4B): return-before-exchange on a single-change order, then a
false success claim (task 27, twice); "exchanging" an item for the identical item id (task 18);
invented variants plus tool-call loop (task 5 trial 2).

Interpretation (pilot): roughly half of the 46-point gap is simulator unfaithfulness; the rest
is agent fragility that the gpt-oss conversations triggered. Passing runs were not audited, so
simulator "rescues" are under-counted.

## 2026-10-12: Hinglish (romanized) smoke test

Branch `tau-multilingual` @ 8ff48b8 with `patches/tau2-multilingual-8ff48b8-windows-run-lock.patch`.
`--task-set-name retail_hi --task-ids 0_hi --user-persona-id hi --text-input-style romanized
--no-llm-communicate-judge`, user simulator qwen3.8-27b, seed 42, 1 trial.

- Result: PASS (reward 1.0, DB match).
- Script leakage: 7 of 8 customer turns contained Devanagari although the style is romanized
  (5.7% of letters).
- Cost: user simulator used 31,458 tokens (English mean ~8.9K); first user prompt is already
  ~2.7K tokens. At Groq's 200K tokens/day that is ~6 Hindi simulations per day per model.

## 2026-10-19 to 2026-10-20: RQ1 through chunk 3/8 (12 tasks)

Same setup as chunk 1; tasks now `5 9 12 17 18 26 27 32 33 42 49 51` (first 12 of the 29
judge-free test tasks), 3 trials each, both simulators, 72 simulations, 0 infra errors
(the 3 gpt-oss quota errors from chunk 1 were refilled by `--auto-resume`).

| Simulator | pass^1 | pass^2 | pass^3 |
|---|---|---|---|
| groq/qwen/qwen3.8-27b | 0.722 | 0.667 | 0.667 |
| groq/openai/gpt-oss-120b | 0.389 | 0.194 | 0.083 |

Per task (fraction passed, gpt-oss / qwen): 5 0/1, 9 .67/1, 12 1/1, 17 .67/1, 18 .33/1,
26 .67/1, 27 0/1, 32 0/.33, 33 .33/0, 42 0/0, 49 .33/1, 51 .67/.33.

- Paired task-bootstrap difference in pass^1 (gpt-oss minus qwen): **-0.33, 95% CI [-0.58, -0.08]**
  (approximate: 12 tasks < 20). McNemar on per-task majority: 4 vs 1 discordant, p = 0.375.
- Variance decomposition (Design A, R = 3): residual 45% (share CI 23-71%), task x simulator 21%
  (0-40%), task 17% (0-43%), simulator 17% (0-49%). Compared with 8 tasks, the simulator main
  effect shrank and the interaction grew: tasks 33 and 51 favour gpt-oss, so the simulator changes
  *which* tasks pass, not only the average.
- Within-cell flip rate 0.375 (CI 0.21-0.54); between-simulator disagreement 0.46 (0.30-0.65).
- Noise floor (`variance.noise_floor`, 12 tasks, single runs): expected |pass^1 difference| between
  two identical runs 0.115 (CI 0.086-0.138); 95% band **+/-0.28**. On a 12-task set, single-run
  differences under ~28 points are indistinguishable from run-to-run noise.
- Anomaly: qwen3.8 task 42 trial 0 has `duration` 88,675 s because the laptop slept / a request
  hung mid-conversation; the conversation then completed normally (30 messages, `user_stop`,
  reward 0). Kept as a valid outcome. Mitigation: per-simulation `--timeout` backstop (pending).

## 2026-10-20: failure audit #2 (failures added by chunks 2-3, plus task 53 trial 0)

Same method as audit #1. Inputs and raw verdicts: `results/audit/failed_cases_rq1_chunk2_3.json`,
`results/audit/audit_rq1_chunk2_3_raw.json`. 20 cases (gpt-oss 12, qwen 8). Raw judge agreement
17/20 (Cohen's kappa ~0.69); all disagreements were on the SHARED boundary.

| Simulator | SIMULATOR_ERROR | SHARED | AGENT_ERROR | Failures |
|---|---|---|---|---|
| gpt-oss-120b, batch 2 | 1 | 5 | 6 | 12 |
| qwen3.8-27b, batch 2 | 0 | 0 | 8 | 8 |
| gpt-oss-120b, both audits | 6 | 6 | 11 | 23 |
| qwen3.8-27b, both audits | 2 | 0 | 8 | 10 |

(Both audits cover 13 tasks: the 12 RQ1 tasks plus gpt-oss task 53 trial 0 from chunk 4.)

- Tasks 33 and 42 fail under both simulators and are agent limitations: on 33 the agent claims a
  partial cancellation is possible, then cancels the whole order; on 42 (four writes) it skips the
  order-address writes and claims success. A judge checked `tau2/domains/retail/tools.py`: no
  ordering trap, gold is reachable; but no run passed task 42, so solvability is argued, not shown.
- The simulator gap is concentrated on the first 8 tasks; on the 5 newer tasks failures are nearly
  equal (gpt-oss 9, qwen 8).
- Simulator-dependent agent errors: 8 of 11 gpt-oss AGENT failures are on tasks where the qwen
  runs had no agent failure. Root-cause labels miss this; it still contributes to the gap.
- Share of the gpt-oss minus qwen failure gap (13 failures) that involves the simulator:
  pure SIM 4/13 (~31%), SHARED counted half 7/13 (~54%), SIM + all SHARED 10/13 (~77%).
- Premature "Yes ... ###STOP###" decided 3 more gpt-oss runs (6 in total across both audits).
- Judges made at least one wrong claim about tool code (item change blocking address changes);
  verdicts unaffected, but judge statements about code need spot checks.

## Engineering notes that affect results

- Tasks with NL assertions need an OpenAI judge; without a key they end in
  `infrastructure_error` at evaluation. Primary task set = 29 judge-free test tasks.
- Groq per-minute limits require `"retry_strategy": "exponential_backoff_retry"` in simulator
  `llm_args`; LiteLLM's default `constant_retry` retries instantly and kills whole tasks.
- Groq daily quota is per model and rolling (24 h), so quota from the previous evening carries over.
