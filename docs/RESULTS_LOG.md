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

## Engineering notes that affect results

- Tasks with NL assertions need an OpenAI judge; without a key they end in
  `infrastructure_error` at evaluation. Primary task set = 29 judge-free test tasks.
- Groq per-minute limits require `"retry_strategy": "exponential_backoff_retry"` in simulator
  `llm_args`; LiteLLM's default `constant_retry` retries instantly and kills whole tasks.
- Groq daily quota is per model and rolling (24 h), so quota from the previous evening carries over.
