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

## 2026-10-20: deterministic simulator-fidelity scan of RQ1 (exploratory)

`analyze.py --fidelity` (`whose_noise.fidelity`, regex/structure checks, no LLM) on snapshot copies of
both RQ1 runs (gpt-oss 48 sims incl. chunk 4 tasks; qwen3.8 36 sims), every simulation, passing ones
included.

| Simulator | premature_stop in failures | in passes | customer asks for transfer |
|---|---|---|---|
| gpt-oss-120b | 6 of 29 | 0 of 19 | 6 of 48 (3 are task 12, whose script asks for a human) |
| qwen3.8-27b | 0 of 10 | 0 of 26 | 7 of 36 (3 are task 12) |

- The 6 flagged gpt-oss simulations (tasks 9 t1, 18 t0, 26 t2, 27 t2, 32 t0, 42 t0) are exactly the
  cases the LLM-judge audits labelled premature "Yes ... ###STOP###": two independent methods agree.
- Never flagged in a passing simulation, so the audits' failure-only sampling did not hide cases.
- Same 12 tasks: gpt-oss 6/36 vs qwen 0/36, Fisher exact p = 0.025 (treats sims as independent); the
  6 stops are on 6 different tasks; gpt-oss rate 0.167, task-bootstrap 95% CI [0.083, 0.250].
- Upper bound on its share of the gap: if all 6 had passed, gpt-oss pass^1 would be at most
  20/36 = 0.556 vs qwen 0.722, so premature stops account for at most ~0.17 of the 0.33 gap.
- Upstream context: the bug is known (tau2-bench issues #182 and #297, both open; #297 proposes a
  simulator-guideline fix and reports retail pass^1 54% -> 80% with one model, Doubao, as agent and
  user). Not yet shown upstream: that its rate depends strongly on the simulator model under identical
  guidelines (17% vs 0% here). Candidate follow-up (needs a pre-registered addendum before running):
  re-run RQ1 with #297's guideline and test whether the simulator gap shrinks.

## 2026-10-21: agent determinism audit (exploratory; RESEARCH_NOTES section 5)

`scripts/determinism_audit.py`: real agent requests rebuilt from recorded qwen3.8-run conversations
(retail policy as system prompt, retail tool schemas, conversation prefix), sent to the same
llama-server settings as RQ1 (temperature 0), with no experiment running.

| Probe | A: 10 identical requests back to back | B: 10 identical, each after a different request |
|---|---|---|
| task 5 trial 0, cut 10/30 | 1 distinct output | 2 distinct outputs |
| task 9 trial 0, cut 14/26 | 2 distinct | 2 distinct |
| task 12 trial 0, cut 26/34 (next turn is a tool call) | 1 distinct | 1 distinct |

- The agent is **not** deterministic at temperature 0 under our serving settings: 2 of 3 probes gave
  more than one output for byte-identical input, once even back to back.
- The differences were small wording changes in text replies ("I have successfully" vs "I've
  successfully"; a different second half of a sentence); the tool-call probe was stable. In a
  multi-turn conversation such a change alters the simulator's input and can push the whole dialogue
  onto a different path, so the agent side contributes to RQ1's residual (re-run) component.
- Interleaving other requests changed the output of one probe that was stable back to back, which
  points at prompt-cache / batching effects in llama.cpp. This is consistent with the 2026-10-12
  `--cache-ram` deviation mattering for the residual (sensitivity analysis pending).
- Only 3 probes x 20 calls: shows that agent-side nondeterminism exists, not how often it decides a
  verdict (that is H1c's job).

## 2026-10-21: erratum to the pre-registration deviations log; infra errors before re-runs

PREREGISTRATION.md is frozen and not edited; this entry corrects it for readers of RQ1.

- **Wrong date in section 12.** The row "Simulator `llm_args` gained `num_retries 8` and
  exponential backoff; the RQ1 run names changed ..." is dated 2026-10-12. The run files
  and logs show:
  - qwen3.8-27b: the unused run `rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_c813736` has user
    `llm_args` `{"temperature": 0.0}` (no retries) and one simulation (task 5, trial 0,
    `infrastructure_error`) at 2026-10-11 16:50:42 IST; its log ends at 16:51:13. The run used
    for RQ1, `..._cd30908`, has `num_retries 8` and `exponential_backoff_retry` in `info`, and
    the `argv` of its first invocation (log header 2026-10-11 16:52:09) already passes them;
    its first simulation started at 16:52:20. For qwen3.8 the change
    and the run-name switch therefore happened on **2026-10-11 at about 16:51 IST**, before its
    first valid RQ1 simulation.
  - gpt-oss-120b: run `rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638` was created with the
    retry settings (log starts 2026-10-12 20:12:29, first simulation 20:13:06 IST), so for this
    simulator the change happened before 2026-10-12 20:13 IST.
  - Every RQ1 simulation used in the analysis ran with the retry settings.
- **The `--cache-ram` cutoff (2026-10-12 20:23 IST) is consistent with the data.** gpt-oss task
  12 trial 1 ended at 20:22:53 and the next simulation started at 20:23:17 (a 24 s gap; about
  1 s elsewhere). No simulation straddles the cutoff. Timestamps are naive local IST.
- **Infra errors before re-runs (PREREGISTRATION section 8).** Counted from
  `results/logs/<run_name>.log` ("failed permanently after 4 attempts" lines and the
  per-invocation summaries): gpt-oss-120b **3** (tasks 26, 27, 32, trial 2; first invocation,
  2026-10-12) and qwen3.8-27b **6** (tasks 12, 17, 18, 26, 27, 32, trial 2; first invocation,
  2026-10-11). Each cell was refilled once by the next `--auto-resume` and is now valid
  (gpt-oss on 2026-10-19, qwen3.8 on 2026-10-12 between 20:50 and 20:55 IST, i.e. AFTER the
  cache-ram cutoff, so those 6 qwen3.8 cells are mixed-period in the section 12 analysis). The
  chunk-1 table above shows qwen3.8 as 24/24 because it was written after that refill. tau2
  `--auto-resume` deletes infra-error simulations before re-running them, so results.json
  cannot show these counts: `scripts/analyze_rq1.py` reads them from `--rerun-evidence`.

## 2026-10-25: RQ1 FINAL (confirmatory, pre-registered analysis)

`scripts/analyze_rq1.py` (B = 10,000, seed 20261011), report in `results/rq1_analysis/report.md`
(+ `results.json`, `rerun_evidence.json`). Final data: 29 of 29 tasks x 3 trials x 2 simulators =
174 valid simulations, 0 dropped, 0 protocol warnings. Infra errors before re-runs: gpt-oss 3/87,
qwen3.8 9/87 (all Groq daily-quota errors, each re-run once).

| Hypothesis | Estimate [95% CI] | Rule | Verdict |
|---|---|---|---|
| H1a residual (re-run) component | 0.0690 [0.0345, 0.1092] | lower bound > 0 | **SUPPORTED** |
| H1b task x simulator component | 0.0868 [0.0361, 0.1289] | lower bound > 0 | **SUPPORTED** |

Descriptive (pre-registered reporting, not tests):

| Simulator | pass^1 | pass^2 | pass^3 |
|---|---|---|---|
| gpt-oss-120b | 0.310 [0.172, 0.448] | 0.207 [0.092, 0.333] | 0.138 [0.034, 0.276] |
| qwen3.8-27b | 0.563 [0.391, 0.736] | 0.529 [0.345, 0.701] | 0.517 [0.345, 0.690] |

- Variance shares: task x simulator 0.328, task 0.305, residual 0.260, simulator 0.106 (not
  interpreted, S = 2). p_bar 0.437; cell flip rate 0.207; between-simulator disagreement 0.368.
- Noise floor (gpt-oss cells, 29 tasks): two identical single runs differ by up to +/-0.166 in
  pass^1 (95% band); +/-0.096 for 3-trial runs.
- Section 12 (`--cache-ram`, descriptive): no visible shift in the residual (pair-level after - before
  -0.002 [-0.117, 0.089]); confounded with task and time.
- Exploratory: paired contrast pass^1 gpt-oss minus qwen -0.253 [-0.425, -0.092]; McNemar on per-task
  majority 8 vs 2, p = 0.109. H1c share_user pooled 0.716 [0.640, 0.795]; per identical-input
  generation, gpt-oss's user turns diverged 0.764 of the time, qwen3.8's 0.073, the agent 0.060-0.136.
- Exploratory fidelity: premature_stop gpt-oss 11/87 (all in failed simulations, on 10 tasks),
  qwen3.8 0/87; two-sided Fisher exact p = 0.0007 (by task 10/29 vs 0/29, p = 0.0008).
- Verification (2026-10-25): an independent recomputation with separate code (no repo imports)
  matched every number above (112 comparisons, 0 mismatches). Every flagged transcript was read: all
  11 are real "agree + `###STOP###` before the write" events, but in 4 (gpt-oss 9/1, 64/0, 64/1,
  94/1) the agreed action was already wrong, so the stop did not cost a correct action. 7 involve an
  expected action (18/0, 26/2, 27/2, 32/0, 42/0, 74/1, 102/2); in only 3 (18/0, 26/2, 27/2) was the
  stop the only or likely deciding problem. The looser screen's 2 qwen3.8 flags (task 65, trials 1-2)
  are the user declining optional help, not premature stops.
- **Correction of the earlier interim claim** ("premature stops account for up to about half the
  gap", 12-task entry above): that was a loose upper bound on 12 tasks. On the final 29 tasks the
  pass^1 gap of 0.253 would be 0.218 if the 3 decisive runs had passed and 0.172 even if all 7 had.
  Premature stops explain only part of the simulator gap; most of it comes from other differences.
- Upstream context: tau2-bench v2.0.0 (2026-10-20) moved the simulator guidelines into
  `src/tau2/user/guidelines.py`; the "do not end the conversation prematurely" bullet is there only
  in the voice templates, not the text ones that RQ1 used.

## 2026-10-26: gpt-oss-120b merges turns on the branch; RQ2 amendment A1 (language-only personas)

First RQ2 cell (`en_pack`, task `5_en`, 2 trials, frozen settings): both simulations ended after one
customer message in 2 s, reward 0. The message held the whole call: "...I don't have my email
handy.Okay, I'm ready. Let's get those exchanges started.Sure, just confirm the lamp exchange.No, I'm
only confirming the lamp swap...Okay, great. That's all I needed. Thanks, bye. ###STOP###". The run was
stopped before the Hindi arms started. Diagnosis (diagnostic runs `diag_enpack_task5`,
`diag_hirom_task5` on the branch; direct replays of the logged request; none of it is RQ2 data):

- **Cause = the persona block.** On the branch the simulator prompt carries the language-pack persona
  (for `en`: `lisa_en_v1`, a clipped New Jersey office manager with many example lines, including the
  closing "Okay, great, that's all I needed, thanks, bye"). On `main` that slot is empty. The LLM call
  path for gpt-oss is the same on both (no Responses-API bridge; it applies only to gpt-5 ids).
- **The joins have no space** ("handy.Okay", "started.Sure"), so the model most likely emitted several
  separate final messages that the provider joined. Repeats of the same request at temperature 0 vary
  (Groq is not deterministic).
- First-turn replays with the logged `5_en` request (the agent's greeting only):

| Simulator / setting | Broken first turns | Notes |
|---|---|---|
| gpt-oss-120b, default (medium) effort | **6 of 8** (3 in tau2 + 3 of 5 direct: 2 merged calls, 1 `###STOP###` in the first message) | 1,467-1,674 tokens when merged |
| gpt-oss-120b, `reasoning_effort` low | 0 of 4 | ~60-70 output tokens per turn |
| gpt-oss-120b, `reasoning_effort` high | 0 of 4 | 577-1,089 output tokens per turn |
| qwen3.8-27b (default) | 0 of 4 | 35 output tokens |

- With the Hindi prompt (`5_hi`, romanized, persona `rishika_hindi_v1`): gpt-oss medium 3 of 3 fine,
  gpt-oss low 3 of 3 fine (Latin-script Hindi, female agreement "chahti hoon"); qwen3.8-27b 2 of 2
  typed **Devanagari** although told to romanize (as in the 2026-10-12 smoke run, 7 of 8 turns), so
  switching simulator would void the romanized vs code-mixed contrast (H2e).
- Persona per task (`resolve_task_persona`, gender-matched): `en` gives `lisa_en_v1` on 10 of RQ2's 12
  tasks (`matt_en_v1` on 33, 49); `hi` gives `rishika_hindi_v1` on the same 10 (`imran_hindi_v1` on 33,
  49). So the error would have hit the English control far more than the Hindi arms.
- **RQ1 is unaffected** (no persona block on `main`): new screens on all 174 RQ1 simulations find 0
  first-turn stops and 1 merged message, "yes.yes." (gpt-oss task 102 trial 0: two identical one-word
  replies, no invented content; not one of the counted premature stops). qwen3.8 0, pilot1 0.

**First fix tried and withdrawn: `reasoning_effort: "low"`.** Decided in the morning (it fixed
turn 1 of `5_en`: 0 of 4) and tested in pilot `rq2pilot` (task 53, outside RQ2's 12, personas
`lisa_en_v1` / `rishika_hindi_v1`, 1 trial x 3 arms, criteria fixed in advance). It **failed**:
`en_pack` merged turns at customer turn 2 ("...I'm not sure what it is.Okay, what else do you need to
verify me?My name's Sofia Li, and my zip code is 78260.###STOP###"); `hi_romanized` ended the call at
turn 2 while giving its login details ("mera naam Sofia Li hai aur mera zip code 78260
hai.###STOP###"); `hi_code_mixed` was a normal 5-turn call. The two excluded `5_en` runs and the
pilot are not RQ2 data.

**Ablation at the failing turn** (turn 2 of `53_en`, history from the pilot, 4 calls each):

| Prompt variant | default (medium) | low |
|---|---|---|
| branch prompt as is | 1/4 broken | 4/4 |
| without the "Note: You still need to use special tokens like ###STOP###" line | 0/4 | 3/4 |
| without the persona block | 0/4 | 0/4 |
| without both (= the `main` prompt) | 0/4 | 0/4 |

So the persona block causes the error; low effort helps turn 1 but makes turn 2 worse.

**Persona-lite** (gender line + pragmatics clause 0 = the language profile, + the Hindi localization
block; attitude clauses dropped), default effort:

| Test point | branch prompt as is | persona-lite |
|---|---|---|
| `5_en`, turn 1 | 6/8 broken | 1/4 |
| `53_en`, turn 2 | 1/4 | 0/4 |
| `53_hi` romanized, turn 2 | 0/3 | 0/3 |

**Decision (the author, 2026-10-26): amendment A1** (PREREGISTRATION section 13, a draft until the
pilot passes and `prereg-v2` is tagged): language-only personas in all arms through
`patches/tau2-multilingual-8ff48b8-language-only-personas.patch` and `TAU2_PERSONA_CLAUSES=first`
(`tau2_env` in the config), default effort, everything else as frozen. The patched code renders
exactly the persona-lite prompts tested above (checked character for character for `53_en` and
`5_hi`; without the switch it renders the logged branch prompt exactly). New pilot `rq2pilot2`
(tasks 53 and 56 x 1 trial x 3 arms) with pass criteria in section 13. Caveat: 1 broken turn in 11
remains possible with persona-lite, so `merged_turns` / `first_turn_stop` are reported per arm with a
sensitivity analysis. Also seen: persona `rishika_hindi_v1` slipped once into masculine agreement
("मैं बस चाहता हूँ") in the code-mixed pilot.

Pilot `rq2pilot2` result: pending (needs a fresh gpt-oss-120b quota window).

## Engineering notes that affect results

- Tasks with NL assertions need an OpenAI judge; without a key they end in
  `infrastructure_error` at evaluation. Primary task set = 29 judge-free test tasks.
- Groq per-minute limits require `"retry_strategy": "exponential_backoff_retry"` in simulator
  `llm_args`; LiteLLM's default `constant_retry` retries instantly and kills whole tasks.
- Groq daily quota is per model and rolling (24 h), so quota from the previous evening carries over.
