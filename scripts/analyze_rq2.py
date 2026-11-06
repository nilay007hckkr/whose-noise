"""Pre-registered RQ2 analysis (PREREGISTRATION.md sections 2-11, amendment A1, ``prereg-v2``).

Usage (from the whose-noise repo root)::

    uv run python scripts/analyze_rq2.py \
        --en-plain PATH --hi-romanized PATH --hi-code-mixed PATH \
        [--out-dir results/rq2_analysis] [--n-boot 10000] [--seed 20261011] \
        [--retries-exhausted TASK ...] \
        [--pre-rerun-infra ARM=N ... --pre-rerun-source TEXT] [--analysis-timestamp ISO]

PATH is the tau2 ``results.json`` (or run directory) of one arm on the ``tau-multilingual``
branch (``8ff48b8``), gpt-oss-120b as the user, R = 2, the first 12 judge-free retail test
tasks. Everywhere the pre-registration says ``en_pack``, A1 reads ``en_plain``. The script
only READS its inputs and writes ``report.md`` and ``results.json`` to ``--out-dir``.

What it computes
----------------
* Section 8: a cell (task, arm) needs R = 2 valid simulations; a task with an infra-error
  cell awaits re-run until ``--retries-exhausted`` names it, and is then dropped from every
  arm (more than 1 dropped task is a deviation). Section 10: the H2 contrasts are computed
  only when all three arms are complete (``--allow-partial`` overrides; never confirmatory).
* Step 2: pass^1, pass^2 per arm (task-cluster bootstrap).
* H2 (primary), H2b, H2e: paired pass^1 contrasts on base task ids, 95% CIs; Holm across
  the three: ordered by two-sided bootstrap p-value, the i-th is tested with the
  (1 - alpha / (3 - i + 1)) percentile CI of the same draws, step-down. Sensitivity (not
  confirmatory): exact McNemar on per-task majority verdicts (with R = 2 a task's majority
  is a pass only if both trials pass) and the pass^2 contrasts.
* Step 5: noise floor of the ``en_plain`` arm (``variance.noise_floor``, trials_per_run 2 as
  in section 7, and 1 for two single runs as in section 2), shown next to each |Delta|.
* H2c: infra-error rates (before re-runs from ``--pre-rerun-infra`` if given, and in the
  files) and ``max_steps`` / ``too_many_errors`` rates per arm, Wilson CIs. Section 11: if a
  Hindi arm's infra-error rate exceeds en_plain's by more than 10 points, its contrast with
  en_plain gets a worst-case bound that turns that many Hindi passes into failures.
* H2d: task and residual components per arm and the paired difference Hindi - en_plain
  (``variance.compare_decompositions``, one simulator); section 11 p_bar flag.
* H2f and A1 item 5: fidelity screens per arm (Wilson CIs, split by pass / fail) and the
  script-leak share; caveats when the romanized arm is mostly Devanagari (> 50% of customer
  letters) or when merged turns, first-turn stops or premature stops differ from en_plain by
  more than 10 points; the exploratory re-analysis of H2 and H2b without simulations flagged
  by any of those three screens.

Bootstrap: seed 20261011, B = 10,000, percentile intervals, resampling tasks; with 12 tasks
the percentile interval under-covers, which the report states next to the intervals.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from whose_noise import fidelity, metrics, variance
from whose_noise.tau2_results import DuplicateTrialError, load_runs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_rq1 as rq1  # noqa: E402  (frozen; generic helpers only)
import analyze_rq3 as rq3  # noqa: E402  (frozen; generic helpers only)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "whose_noise.rq2_analysis/v1"
TASKS = rq1.PREREG_TASKS[:12]  # section 4: the first 12 judge-free test tasks
REPS = 2
N_BOOT, SEED, ALPHA = 10_000, variance.DEFAULT_BOOTSTRAP_SEED, 0.05
COMMIT, TAU2_SEED = "8ff48b8", 300
EN, ROM, CM = "en_plain", "hi_romanized", "hi_code_mixed"
ARMS = (EN, ROM, CM)
HINDI = (ROM, CM)
# What each arm records in results.json info, and its run-name hash (config + tau2_env: the
# language-only personas of A1 change the hash, so a matching hash shows the patch setting).
ARM_INFO = {
    EN: {"task_set_name": "retail", "user_persona_id": None, "text_input_style": None},
    ROM: {"task_set_name": "retail_hi", "user_persona_id": "hi", "text_input_style": "romanized"},
    CM: {"task_set_name": "retail_hi", "user_persona_id": "hi", "text_input_style": "code_mixed"},
}
ARM_HASH = {EN: "c1a8dad", ROM: "c9f6a09", CM: "c0da38e"}
CONTRASTS = {"H2": (ROM, EN), "H2b": (CM, EN), "H2e": (CM, ROM)}
SCREENS = (
    "premature_stop",
    "stop_after_unexecuted_confirmation",
    "transfer_requested_by_customer",
    "merged_turns",
    "first_turn_stop",
    "script_leakage",
)
A1_SCREENS = ("merged_turns", "first_turn_stop", "premature_stop")
MAX_DROPPED = 1  # section 8: more than 1 dropped RQ2 task is a deviation
GAP_RULE = 0.10  # sections 11 and 13 (A1 item 5): 10 points
LEAK_RULE = 0.50  # section 11: romanized customer letters mostly Devanagari
UNDERCOVER = "With {n} tasks (fewer than 20) percentile intervals under-cover (section 7)"


def flag(arm: str) -> str:
    return "--" + arm.replace("_", "-")


# ---------------------------------------------------------------------------
# Loading, protocol checks, section 8
# ---------------------------------------------------------------------------


def load(paths: Mapping[str, str]) -> tuple[pd.DataFrame, list[rq1.InputRun], list[str]]:
    inputs = [rq1.read_input(arm, paths[arm]) for arm in ARMS]
    if len({inp.results_path for inp in inputs}) < len(inputs):
        raise rq1.InputError("the three inputs must be three different results files")
    try:
        runs = load_runs([inp.results_path for inp in inputs], on_duplicates="raise")
    except DuplicateTrialError as exc:
        raise rq1.InputError(f"duplicated trials: {exc}") from exc
    runs["role"] = runs["results_path"].map({str(i.results_path): i.role for i in inputs})
    runs = fidelity.add_fidelity(runs)  # matches transcripts by the ids in the files
    runs["task_id"] = runs["task_id"].astype(str).str.removesuffix("_hi")  # pair on base ids
    return runs, inputs, protocol_checks(inputs, runs)


def protocol_checks(inputs: Sequence[rq1.InputRun], runs: pd.DataFrame) -> list[str]:
    out: list[str] = []
    agents = sorted({str(x) for x in runs["agent_llm"].dropna()})
    if len(agents) != 1:
        out.append(f"agent differs between or within files: {agents}")
    for inp in inputs:
        info, arm = inp.info, inp.role
        commit = str(info.get("git_commit") or "")
        if not commit.startswith(COMMIT):
            out.append(f"{flag(arm)}: tau2 git_commit {commit or 'not recorded'} is not {COMMIT}")
        if not inp.run_name.endswith(ARM_HASH[arm]):
            out.append(
                f"{flag(arm)}: run {inp.run_name} does not end in the piloted hash "
                f"{ARM_HASH[arm]} (wrong arm, or different settings / tau2_env)"
            )
        for key, want in ARM_INFO[arm].items():
            if info.get(key) != want:
                out.append(f"{flag(arm)}: info.{key} = {info.get(key)!r}, expected {want!r}")
        llms = sorted({str(x) for x in runs.loc[runs["role"] == arm, "user_llm"].dropna()})
        if llms != ["groq/openai/gpt-oss-120b"]:
            out.append(f"{flag(arm)}: user simulator(s) {llms}, expected gpt-oss-120b")
        if info.get("seed") != TAU2_SEED:
            out.append(f"{arm}: tau2 seed {info.get('seed')} != {TAU2_SEED}")
        if info.get("num_trials") not in (None, REPS):
            out.append(f"{arm}: num_trials {info.get('num_trials')} != {REPS}")
        if info.get("target_language_directive_version") != "v3":
            out.append(f"{arm}: target_language_directive_version is not v3 (section 5)")
        if info.get("agent_caller_locale_context") is not True:
            out.append(f"{arm}: agent_caller_locale_context is not on (section 5)")
        for side in ("agent_info", "user_info"):
            temp = ((info.get(side) or {}).get("llm_args") or {}).get("temperature")
            if temp != 0.0:
                out.append(f"{arm}: {side} temperature {temp} is not 0.0")
    return out


def completeness(runs: pd.DataFrame, retries_exhausted: Sequence[str]) -> dict[str, Any]:
    rows = []
    for task in TASKS:
        states = {}
        for arm in ARMS:
            sub = runs[(runs["role"] == arm) & (runs["task_id"] == task)]
            n_valid = int((~sub["is_infra_error"]).sum())
            if n_valid >= REPS:
                states[arm] = rq1.COMPLETE
            elif len(sub) < REPS:
                states[arm] = rq1.NOT_YET_RUN
            else:
                states[arm] = rq1.INFRA_INCOMPLETE
        confirmed = task in set(retries_exhausted) and rq1.INFRA_INCOMPLETE in states.values()
        rows.append(
            {
                "task_id": task,
                "status": rq1._task_status(list(states.values()), confirmed),
                "cells": states,
            }
        )
    by = {
        s: [r["task_id"] for r in rows if r["status"] == s]
        for s in (rq1.COMPLETE, rq1.PENDING, rq1.AWAITING_RERUN, rq1.DROPPED)
    }
    return {
        "mode": "final" if not by[rq1.PENDING] and not by[rq1.AWAITING_RERUN] else "partial",
        "n_planned": len(TASKS),
        "analysed_tasks": by[rq1.COMPLETE],
        "dropped_tasks": by[rq1.DROPPED],
        "deviation_too_many_dropped": len(by[rq1.DROPPED]) > MAX_DROPPED,
        "pending_tasks": by[rq1.PENDING],
        "awaiting_rerun_tasks": by[rq1.AWAITING_RERUN],
        "per_task": rows,
    }


# ---------------------------------------------------------------------------
# Contrasts with Holm, noise floor
# ---------------------------------------------------------------------------


def paired_draws(
    a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]], k: int, n_boot: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Per-task pass^k differences and their bootstrap means (as paired_bootstrap_diff)."""
    shared = [t for t in a if t in b]
    pa = metrics.pass_hat_k_per_task({t: a[t] for t in shared}, k)
    pb = metrics.pass_hat_k_per_task({t: b[t] for t in shared}, k)
    d = np.array([pa[t] - pb[t] for t in shared], dtype=float)
    idx = np.random.default_rng(seed).integers(0, d.size, size=(n_boot, d.size))
    return d, d[idx].mean(axis=1)


def contrast(
    a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]], n_boot: int, seed: int
) -> tuple[dict[str, Any], np.ndarray]:
    d, draws = paired_draws(a, b, 1, n_boot, seed)
    lo, hi = metrics._percentile_interval(draws, ALPHA)
    p_boot = min(1.0, 2 * min(float((draws <= 0).mean()), float((draws >= 0).mean())))
    res = {"estimate": float(d.mean()), "ci_low": lo, "ci_high": hi, "n_tasks": int(d.size)}
    return {**res, "p_boot": p_boot}, draws


def holm(results: dict[str, dict[str, Any]], draws: Mapping[str, np.ndarray]) -> list[str]:
    """Holm step-down on bootstrap p-values, decided by CIs at the Holm level; returns order."""
    order = sorted(results, key=lambda h: (results[h]["p_boot"], h))
    going = True
    for i, h in enumerate(order):
        level = ALPHA / (len(order) - i)
        lo, hi = metrics._percentile_interval(draws[h], level)
        going = going and (lo > 0 or hi < 0)
        results[h].update(
            holm_rank=i + 1,
            holm_alpha=level,
            holm_ci_low=lo,
            holm_ci_high=hi,
            differs=going,
            verdict="DIFFERS (Holm)" if going else "not detected at this sample size",
        )
    return order


def hypotheses(valid: pd.DataFrame, n_boot: int, seed: int) -> dict[str, Any]:
    succ = {arm: rq3.per_task(valid, arm) for arm in ARMS}
    results, draws = {}, {}
    for h, (a, b) in CONTRASTS.items():
        results[h], draws[h] = contrast(succ[a], succ[b], n_boot, seed)
        results[h]["contrast"] = f"pass^1({a}) - pass^1({b})"
        b01, b10, p = metrics.mcnemar_exact(rq1.majority(succ[a]), rq1.majority(succ[b]))
        results[h]["sensitivity"] = {
            "mcnemar_majority": {f"{a}_fail_{b}_pass": b01, f"{a}_pass_{b}_fail": b10, "p": p},
            "pass^2": dict(
                zip(
                    ("estimate", "ci_low", "ci_high"),
                    metrics.paired_bootstrap_diff(
                        succ[a], succ[b], k=2, n_boot=n_boot, alpha=ALPHA, seed=seed
                    ),
                    strict=True,
                )
            ),
        }
    order = holm(results, draws)
    return {"order": order, **results}


def noise_floor(valid: pd.DataFrame, n_tasks: int, n_boot: int, seed: int) -> dict[str, Any]:
    en = valid[valid["role"] == EN]
    out = {}
    for k in (REPS, 1):
        nf = variance.noise_floor(
            en,
            n_eval_tasks=n_tasks,
            trials_per_run=k,
            sim_col=None,
            n_reps=REPS,
            on_unbalanced="raise",
            n_boot=n_boot,
            seed=seed,
            alpha=ALPHA,
        )
        out[f"trials_per_run_{k}"] = {
            "expected_abs_diff": rq1.stat_dict(nf.expected_abs_diff),
            "band": rq1.stat_dict(nf.band),
        }
    return out


# ---------------------------------------------------------------------------
# H2c, H2d, H2f / A1 item 5, section 11
# ---------------------------------------------------------------------------


def infra_and_terminations(
    runs: pd.DataFrame, valid: pd.DataFrame, scope: Sequence[str], pre: Mapping[str, int]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in ARMS:
        allsims = runs[(runs["role"] == arm) & runs["task_id"].isin(set(scope))]
        sub = valid[valid["role"] == arm]
        ends = sub["termination_reason"].value_counts()
        out[arm] = {
            "infra_errors_in_file": rq3.wilson(int(allsims["is_infra_error"].sum()), len(allsims)),
            "infra_errors_before_reruns": rq3.wilson(pre[arm], len(allsims))
            if arm in pre
            else None,
            "max_steps": rq3.wilson(int(ends.get("max_steps", 0)), len(sub)),
            "too_many_errors": rq3.wilson(int(ends.get("too_many_errors", 0)), len(sub)),
            "termination_reasons": {str(k): int(v) for k, v in sorted(ends.items())},
        }
    return out


def infra_bounds(h2c: Mapping[str, Any], valid: pd.DataFrame) -> dict[str, Any]:
    """Section 11: worst-case contrast when a Hindi arm has > 10 points more infra errors."""
    pre_given = h2c[EN]["infra_errors_before_reruns"] is not None
    key = "infra_errors_before_reruns" if pre_given else "infra_errors_in_file"
    out: dict[str, Any] = {"rates_used": key}
    en_pass = float(valid.loc[valid["role"] == EN, "success"].mean())
    for arm in HINDI:
        gap = h2c[arm][key]["rate"] - h2c[EN][key]["rate"]
        entry: dict[str, Any] = {"rate_gap": gap, "triggered": bool(gap > GAP_RULE)}
        if entry["triggered"]:
            sub = valid[valid["role"] == arm]
            extra = h2c[arm][key]["count"] - h2c[EN][key]["count"]
            worst = max(0, int(sub["success"].sum()) - extra) / len(sub)
            entry.update(extra_infra_errors=extra, worst_case_delta=worst - en_pass)
        out[arm] = entry
    return out


def decompositions(valid: pd.DataFrame, n_boot: int, seed: int) -> dict[str, Any]:
    """H2d: task / residual per arm and the paired difference Hindi - en_plain."""
    by = {arm: valid[valid["role"] == arm] for arm in ARMS}
    out: dict[str, Any] = {}
    for arm in HINDI:
        diff = variance.compare_decompositions(
            by[EN],
            by[arm],
            design="crossed",
            labels=(EN, arm),
            n_boot=n_boot,
            seed=seed,
            alpha=ALPHA,
            sim_col=None,
            n_reps=REPS,
            on_unbalanced="raise",
        )
        out[arm] = {
            name: {k: float(v) for k, v in row.items()}
            for name, row in diff.iterrows()
            if name in ("task", "residual", "share_residual", "p_bar")
        }
    means = {arm: float(by[arm]["success"].mean()) for arm in ARMS}
    out["section11_p_bar"] = rq1.section11(float(valid["success"].mean()), means)
    return out


def screens(valid: pd.DataFrame) -> dict[str, Any]:
    """H2f / A1 item 5: screen rates per arm, split by pass / fail, and script leakage."""
    out: dict[str, Any] = {}
    for arm in ARMS:
        sub = valid[valid["role"] == arm]
        entry: dict[str, Any] = {}
        for s in SCREENS:
            entry[s] = {
                "all": rq3.wilson(int(sub[s].sum()), len(sub)),
                "in_passes": int(sub.loc[sub["success"], s].sum()),
                "in_failures": int(sub.loc[~sub["success"], s].sum()),
            }
        letters = float(sub["customer_letters"].sum())
        dev = float(sub["devanagari_letters"].sum())
        entry["devanagari_share_of_customer_letters"] = dev / letters if letters else math.nan
        entry["script_leak_share_mean"] = float(sub["script_leak_share"].astype(float).mean())
        out[arm] = entry
    return out


def caveats(scr: Mapping[str, Any]) -> list[str]:
    out = []
    rom_dev = scr[ROM]["devanagari_share_of_customer_letters"]
    if math.isfinite(rom_dev) and rom_dev > LEAK_RULE:
        out.append(
            f"section 11: hi_romanized customer letters are {rom_dev:.0%} Devanagari (> 50%): the "
            "arm did not deliver its input style; H2 and H2e are reported with this caveat"
        )
    for arm in HINDI:
        for s in A1_SCREENS:
            gap = scr[arm][s]["all"]["rate"] - scr[EN][s]["all"]["rate"]
            if abs(gap) > GAP_RULE:
                out.append(
                    f"A1 item 5: {s} rate differs between {arm} and en_plain by {gap:+.0%} "
                    f"(> 10 points): the {'H2' if arm == ROM else 'H2b'} contrast partly measures "
                    "simulator behaviour"
                )
    return out


def sensitivity(valid: pd.DataFrame, n_boot: int, seed: int) -> dict[str, Any]:
    """A1 item 5 (exploratory): H2 and H2b without simulations flagged by the three screens."""
    keep = valid[~valid[list(A1_SCREENS)].any(axis=1)]
    succ = {arm: rq3.per_task(keep, arm) for arm in ARMS}
    return {
        "excluded_per_arm": {
            arm: int((valid["role"] == arm).sum() - (keep["role"] == arm).sum()) for arm in ARMS
        },
        **{
            h: rq3.paired(succ[a], succ[b], n_boot, seed)
            for h, (a, b) in CONTRASTS.items()
            if h in ("H2", "H2b")
        },
    }


# ---------------------------------------------------------------------------
# Run, report, CLI
# ---------------------------------------------------------------------------


def parse_pre(items: Sequence[str]) -> dict[str, int]:
    out = {}
    for item in items:
        arm, _, n = item.partition("=")
        if arm not in ARMS or not n.isdigit():
            raise rq1.InputError(f"--pre-rerun-infra {item!r}: use ARM=N with ARM in {ARMS}")
        out[arm] = int(n)
    if out and set(out) != set(ARMS):
        raise rq1.InputError("--pre-rerun-infra must give all three arms")
    return out


def code_hashes() -> dict[str, str]:
    package = Path(variance.__file__).resolve().parent
    files = [Path(__file__).resolve(), Path(rq1.__file__).resolve(), Path(rq3.__file__).resolve()]
    files += [package / f"{m}.py" for m in ("metrics", "variance", "tau2_results", "fidelity")]
    return {p.relative_to(REPO_ROOT).as_posix(): rq1.sha256_file(p) for p in files}


def run_analysis(args: argparse.Namespace) -> dict[str, Any]:
    pre = parse_pre(args.pre_rerun_infra)
    if pre and not (args.pre_rerun_source or "").strip():
        raise rq1.InputError("--pre-rerun-infra needs --pre-rerun-source (how it was counted)")
    runs, inputs, warnings_ = load({arm: getattr(args, arm) for arm in ARMS})
    status = completeness(runs, args.retries_exhausted)
    if status["mode"] != "final" and not args.allow_partial:
        raise rq1.InputError(
            "data are partial (section 8): the H2 contrasts are computed only when all three "
            "arms are complete (section 10); pass --allow-partial to override"
        )
    tasks = status["analysed_tasks"]
    if len(tasks) < 2:
        raise rq1.InputError("fewer than 2 tasks complete in all three arms: nothing to analyse")
    valid, problems = rq1.analysis_frame(runs, tasks, REPS)[0], []
    if int(valid["customer_turns"].isna().sum()):
        problems.append(
            "some valid simulations have no matched transcript: screens count as not flagged"
        )
    for col in SCREENS:
        valid[col] = valid[col].astype("boolean").fillna(False).astype(bool)
    warnings_ += problems
    scope = [t for t in TASKS if t not in set(status["dropped_tasks"])]
    succ = {arm: rq3.per_task(valid, arm) for arm in ARMS}
    h2c = infra_and_terminations(runs, valid, scope, pre)
    scr = screens(valid)
    res: dict[str, Any] = {
        "schema": SCHEMA,
        "analysis_timestamp": args.analysis_timestamp or datetime.now().astimezone().isoformat(),
        "inputs": {
            i.role: {
                "path": i.path_arg,
                "run_name": i.run_name,
                "sha256": i.sha256,
                "git_commit": i.info.get("git_commit"),
                "timeout": i.info.get("timeout"),
            }
            for i in inputs
        },
        "settings": {"n_boot": args.n_boot, "seed": args.seed, "alpha": ALPHA, "reps": REPS},
        "prereg": rq1.read_git_info(REPO_ROOT, tag="prereg-v2"),
        "code_sha256": code_hashes(),
        "python": platform.python_version(),
        "protocol_warnings": warnings_,
        "section8": status,
        "pass_k": {
            arm: rq1.pass_k_block(succ[arm], (1, 2), args.n_boot, args.seed) for arm in ARMS
        },
        "hypotheses": hypotheses(valid, args.n_boot, args.seed),
        "noise_floor_en_plain": noise_floor(valid, len(tasks), args.n_boot, args.seed),
        "H2c": h2c,
        "H2c_pre_rerun_source": args.pre_rerun_source,
        "section11_infra": infra_bounds(h2c, valid),
        "H2d": decompositions(valid, args.n_boot, args.seed),
        "H2f": scr,
        "caveats": caveats(scr),
        "sensitivity_without_flagged": sensitivity(valid, args.n_boot, args.seed),
    }
    reasons = []
    if status["mode"] != "final":
        reasons.append("data are partial (section 8): decisions are provisional")
    if (args.n_boot, args.seed) != (N_BOOT, SEED):
        reasons.append(f"B / seed differ from the pre-registered {N_BOOT} / {SEED}")
    if warnings_:
        reasons.append("protocol warnings (see below)")
    res["provisional_reasons"] = reasons
    res["label"] = "PROVISIONAL" if reasons else "CONFIRMATORY"
    return res


def _screen_cell(x: Mapping[str, Any]) -> str:
    return f"{rq3._count_ci(x['all'])} (pass {x['in_passes']} / fail {x['in_failures']})"


def _ci(d: Mapping[str, Any], key: str = "estimate") -> str:
    return rq1.fmt_ci(d, 3, key)


def render_report(res: Mapping[str, Any]) -> str:
    s8, hyp, nf = res["section8"], res["hypotheses"], res["noise_floor_en_plain"]
    band = nf[f"trials_per_run_{REPS}"]["band"]["estimate"]
    out = [
        "# RQ2 analysis: Hindi and Hinglish users vs the English control",
        "",
        f"**{res['label']}**"
        + (": " + "; ".join(res["provisional_reasons"]) if res["provisional_reasons"] else ""),
        "",
        f"Analysis {res['analysis_timestamp']}; B = {res['settings']['n_boot']}, seed "
        f"{res['settings']['seed']}; pre-registration `prereg-v2` "
        f"({(res['prereg'] or {}).get('tag_commit') or 'tag not found'}), amendment A1.",
        "",
        rq1.md_table(
            ["Arm", "Run", "tau2 commit", "info.timeout (s)", "sha256"],
            [
                [
                    a,
                    i["run_name"],
                    str(i["git_commit"])[:7],
                    rq1.fmt(i["timeout"], 0),
                    i["sha256"][:16],
                ]
                for a, i in res["inputs"].items()
            ],
        ),
        "",
        f"Section 8: mode **{s8['mode']}**, {len(s8['analysed_tasks'])} of {s8['n_planned']} "
        f"tasks analysed; dropped {rq1.task_list(s8['dropped_tasks'])}"
        + (" (more than 1: deviation)" if s8["deviation_too_many_dropped"] else "")
        + f"; pending {rq1.task_list(s8['pending_tasks'])}; awaiting re-run "
        f"{rq1.task_list(s8['awaiting_rerun_tasks'])}. "
        + UNDERCOVER.format(n=len(s8["analysed_tasks"]))
        + ".",
        "",
        "## pass^k per arm",
        "",
        rq1.md_table(
            ["Arm", "pass^1", "pass^2"],
            [[a, _ci(res["pass_k"][a]["pass^1"]), _ci(res["pass_k"][a]["pass^2"])] for a in ARMS],
        ),
        "",
        "## H2 (primary), H2b, H2e: paired pass^1 contrasts, Holm across the three",
        "",
        rq1.md_table(
            [
                "Hypothesis",
                "Contrast",
                "Estimate [95% CI]",
                "bootstrap p",
                "Holm alpha",
                "CI at Holm level",
                "Verdict",
                f"|Delta| vs noise band ({rq1.fmt(band)})",
            ],
            [
                [
                    h,
                    hyp[h]["contrast"],
                    _ci(hyp[h]),
                    rq1.fmt(hyp[h]["p_boot"]),
                    rq1.fmt(hyp[h]["holm_alpha"], 4),
                    f"[{rq1.fmt(hyp[h]['holm_ci_low'])}, {rq1.fmt(hyp[h]['holm_ci_high'])}]",
                    f"**{hyp[h]['verdict']}**",
                    "inside" if abs(hyp[h]["estimate"]) <= band else "outside",
                ]
                for h in CONTRASTS
            ],
        ),
        "",
        "The noise band (two identical en_plain runs of 2 trials on these tasks, section 7 "
        "step 5) is context, not a second test. A null is reported as not detected at this "
        "sample size.",
        "",
        "Sensitivity (not confirmatory):",
        "",
        rq1.md_table(
            ["Hypothesis", "pass^2 difference [95% CI]", "McNemar on majority (p)"],
            [
                [
                    h,
                    _ci(hyp[h]["sensitivity"]["pass^2"]),
                    rq1.fmt(hyp[h]["sensitivity"]["mcnemar_majority"]["p"]),
                ]
                for h in CONTRASTS
            ],
        ),
        "",
    ]
    if res["caveats"]:
        out += ["## Caveats (sections 11 and 13)", "", *(f"- {c}" for c in res["caveats"]), ""]
    h2c, bounds = res["H2c"], res["section11_infra"]
    out += [
        "## H2c: infra errors and terminations (Wilson 95% CI)",
        "",
        rq1.md_table(
            [
                "Arm",
                "infra before re-runs",
                "infra left in file",
                "max_steps",
                "too_many_errors",
                "terminations",
            ],
            [
                [
                    a,
                    rq3._count_ci(h2c[a]["infra_errors_before_reruns"])
                    if h2c[a]["infra_errors_before_reruns"]
                    else "not given",
                    rq3._count_ci(h2c[a]["infra_errors_in_file"]),
                    rq3._count_ci(h2c[a]["max_steps"]),
                    rq3._count_ci(h2c[a]["too_many_errors"]),
                    ", ".join(f"{k} {v}" for k, v in h2c[a]["termination_reasons"].items()),
                ]
                for a in ARMS
            ],
        ),
        "",
        f"Section 11 infra rule ({bounds['rates_used']}): "
        + "; ".join(
            f"{a} gap {rq1.fmt(bounds[a]['rate_gap'])}"
            + (
                f", worst-case Delta {rq1.fmt(bounds[a]['worst_case_delta'])}"
                if bounds[a]["triggered"]
                else ""
            )
            for a in HINDI
        )
        + ".",
        "",
        "## H2d: task and residual components, Hindi - en_plain",
        "",
        rq1.md_table(
            ["Arm", "Statistic", "en_plain", "Hindi arm", "Difference [95% CI]"],
            [
                [
                    a,
                    n,
                    rq1.fmt(v[f"estimate_{EN}"], 4),
                    rq1.fmt(v[f"estimate_{a}"], 4),
                    rq1.fmt_ci(v, 4, "difference"),
                ]
                for a in HINDI
                for n, v in res["H2d"][a].items()
                if n != "section11_p_bar"
            ],
        ),
        "",
        "Section 11 p_bar: shares flagged as uninformative: "
        f"{res['H2d']['section11_p_bar']['shares_flagged_uninformative']}.",
        "",
        "## H2f and A1 item 5: fidelity screens per arm (count, Wilson 95% CI)",
        "",
        rq1.md_table(
            ["Screen", *ARMS],
            [
                [
                    s,
                    *(_screen_cell(res["H2f"][a][s]) for a in ARMS),
                ]
                for s in SCREENS
            ]
            + [
                [
                    "Devanagari share of customer letters",
                    *(rq1.fmt(res["H2f"][a]["devanagari_share_of_customer_letters"]) for a in ARMS),
                ]
            ],
        ),
        "",
        "## Exploratory (A1 item 5): H2 and H2b without simulations flagged by merged turns, "
        "first-turn stops or premature stops",
        "",
        "Excluded per arm: "
        + ", ".join(
            f"{a} {n}" for a, n in res["sensitivity_without_flagged"]["excluded_per_arm"].items()
        ),
        "",
        rq1.md_table(
            ["Hypothesis", "Estimate [95% CI]", "Tasks"],
            [
                [
                    h,
                    _ci(res["sensitivity_without_flagged"][h]),
                    str(res["sensitivity_without_flagged"][h]["n_tasks"]),
                ]
                for h in ("H2", "H2b")
            ],
        ),
        "",
    ]
    if res["protocol_warnings"]:
        out += ["## Protocol warnings", "", *(f"- {w}" for w in res["protocol_warnings"]), ""]
    out += ["## Code", "", *(f"- `{k}` {v[:16]}" for k, v in res["code_sha256"].items()), ""]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for arm in ARMS:
        p.add_argument(flag(arm), dest=arm, required=True, help=f"results.json of arm {arm}")
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "results" / "rq2_analysis")
    p.add_argument("--n-boot", type=int, default=N_BOOT)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--retries-exhausted", nargs="*", default=[], metavar="TASK")
    p.add_argument(
        "--pre-rerun-infra",
        nargs="*",
        default=[],
        metavar="ARM=N",
        help="infra errors on first attempts per arm (from the run logs)",
    )
    p.add_argument("--pre-rerun-source", help="how --pre-rerun-infra was counted")
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="analyse before all arms are complete (never confirmatory)",
    )
    p.add_argument("--analysis-timestamp", help="pin the timestamp recorded in the outputs")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        res = run_analysis(args)
    except rq1.InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(
        json.dumps(rq1.to_jsonable(res), indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (args.out_dir / "report.md").write_text(render_report(res), encoding="utf-8")
    print(f"{res['label']}: wrote {args.out_dir / 'report.md'} and results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
