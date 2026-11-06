"""Pre-registered RQ3 analysis (PREREGISTRATION.md section 14, git tag ``prereg-v3``).

Usage (from the whose-noise repo root)::

    uv run python scripts/analyze_rq3.py \
        --base-gptoss PATH --base-qwen PATH --fix-gptoss PATH --fix-qwen PATH \
        [--out-dir results/rq3_analysis] [--n-boot 10000] [--seed 20261011] \
        [--retries-exhausted TASK ...] [--hand-labels labels.csv --blind-key key.json] \
        [--analysis-timestamp ISO]

    # before the analysis: the blinded sheet for hand-reading the flagged stops
    uv run python scripts/analyze_rq3.py <the four inputs> --export-blind-sheet DIR

PATH is a tau2 ``results.json`` (or its run directory) of one cell: version ``base``
(tau2-bench ``7aad81c``) or ``fix`` (PR #702, ``2fb1ca5``) x simulator gpt-oss-120b or
qwen3.8-27b. The script only READS its inputs (no tau2 process, LLM call, network or git
command) and writes ``report.md`` and ``results.json`` to ``--out-dir``.

What it computes (section 14)
-----------------------------
* Section 8: a cell (task, version, simulator) needs R = 3 valid simulations; a task with
  a cell still holding an infra error is awaiting re-run until ``--retries-exhausted``
  names it, and is then dropped from every cell. Data are FINAL when each of the 29
  tasks is complete or dropped; otherwise only complete tasks are analysed and every
  decision is provisional.
* H3a (confirmatory): ``D_stop = rate_fix - rate_base`` of the strict ``premature_stop``
  screen with gpt-oss-120b; paired task-cluster bootstrap; supported if the 95% CI lies
  entirely below 0.
* H3b (confirmatory): ``DiD = gap_base - gap_fix`` with ``gap = pass^1(qwen3.8) -
  pass^1(gpt-oss)`` per version; paired task-cluster bootstrap over the four cells;
  supported if the lower bound is > 0. Weakly powered: a null is "not detected at this
  sample size".
* H3c (secondary): ``pass^1(fix) - pass^1(base)`` per simulator (paired CI) and pass^1..3
  per cell (task-cluster CI).
* H3d (secondary, descriptive): per cell, strict and loose stop screens, merged turns,
  first-turn stops, customer turns, termination reasons (``max_steps`` among them) and
  infra errors, with Wilson CIs; hand-read counts from ``--hand-labels``.
* H3e (exploratory): Design A variance components within each version and the paired
  difference fix - base (``variance.compare_decompositions``).
* Exploratory (deviation 2026-10-31): H3a-H3c again without the simulations flagged by
  ``merged_turns`` or ``first_turn_stop``; a contrast uses the tasks that keep at least one
  simulation in each cell it needs.

Bootstrap: seed 20261011, B = 10,000, percentile intervals, resampling tasks; every call
uses the same seed. Fidelity screens come from ``whose_noise.fidelity``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import secrets
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from whose_noise import fidelity, metrics, variance
from whose_noise.tau2_results import DuplicateTrialError, load_runs, read_results

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze_rq1 as rq1  # noqa: E402  (frozen RQ1 script; only its generic helpers are used)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "whose_noise.rq3_analysis/v1"
TASKS = rq1.PREREG_TASKS  # section 14: the 29 judge-free retail test tasks
REPS = 3
N_BOOT, SEED, ALPHA = 10_000, variance.DEFAULT_BOOTSTRAP_SEED, 0.05
TAU2_SEED = 300
BASE, FIX = "base", "fix"
VERSIONS = (BASE, FIX)
GPTOSS, QWEN = rq1.GPTOSS, rq1.QWEN
SIMS = (GPTOSS, QWEN)
SIM_HINTS = {GPTOSS: "gpt-oss", QWEN: "qwen"}
COMMITS = {BASE: "7aad81c", FIX: "2fb1ca5"}
STRICT, LOOSE = "premature_stop", "stop_after_unexecuted_confirmation"
SCREENS = (STRICT, LOOSE, "merged_turns", "first_turn_stop", "transfer_requested_by_customer")
MERGE_SCREENS = ("merged_turns", "first_turn_stop")
LABEL_VALUES = {"expected_action": ("yes", "no"), "deciding": ("yes", "likely", "no")}
UNDERCOVER = "Fewer than 20 tasks: percentile intervals under-cover (section 7)"


def cell(version: str, sim: str) -> str:
    return f"{version}/{sim}"


CELLS = tuple(cell(v, s) for v in VERSIONS for s in SIMS)
INPUT_FLAGS = {
    cell(v, s): f"--{v}-{'gptoss' if s == GPTOSS else 'qwen'}" for v in VERSIONS for s in SIMS
}


def input_paths(args: argparse.Namespace) -> dict[str, str]:
    return {c: getattr(args, INPUT_FLAGS[c][2:].replace("-", "_")) for c in CELLS}


# ---------------------------------------------------------------------------
# Loading, protocol checks, section 8
# ---------------------------------------------------------------------------


def load(paths: Mapping[str, str]) -> tuple[pd.DataFrame, list[rq1.InputRun], list[str]]:
    """Long table of the four cells with fidelity columns, the inputs and protocol warnings."""
    inputs = [rq1.read_input(c, paths[c]) for c in CELLS]
    if len({inp.results_path for inp in inputs}) < len(inputs):
        raise rq1.InputError("the four inputs must be four different results files")
    try:
        runs = load_runs([inp.results_path for inp in inputs], on_duplicates="raise")
    except DuplicateTrialError as exc:
        raise rq1.InputError(f"duplicated trials: {exc}") from exc
    role = {str(inp.results_path): inp.role for inp in inputs}
    runs["role"] = runs["results_path"].map(role)
    runs = fidelity.add_fidelity(runs)
    return runs, inputs, protocol_checks(inputs, runs)


def protocol_checks(inputs: Sequence[rq1.InputRun], runs: pd.DataFrame) -> list[str]:
    """Recorded settings against section 14 (warnings; any warning makes the run provisional)."""
    out: list[str] = []
    agents = sorted({str(x) for x in runs["agent_llm"].dropna()})
    if len(agents) != 1:
        out.append(f"agent differs between or within files: {agents} (RQ3 fixes the agent)")
    for inp in inputs:
        version, sim = inp.role.split("/")
        info = inp.info
        commit = str(info.get("git_commit") or "")
        if not commit.startswith(COMMITS[version]):
            out.append(
                f"{INPUT_FLAGS[inp.role]}: tau2 git_commit {commit or 'not recorded'} is not "
                f"{COMMITS[version]}: wrong file, or base and fix swapped?"
            )
        llms = sorted({str(x) for x in runs.loc[runs["role"] == inp.role, "user_llm"].dropna()})
        if len(llms) != 1 or SIM_HINTS[sim] not in llms[0].lower():
            out.append(f"{INPUT_FLAGS[inp.role]}: user simulator(s) {llms}, expected {sim}")
        if info.get("seed") != TAU2_SEED:
            out.append(f"{inp.role}: tau2 seed {info.get('seed')} != {TAU2_SEED}")
        if info.get("num_trials") not in (None, REPS):
            out.append(f"{inp.role}: num_trials {info.get('num_trials')} != {REPS}")
        for side in ("agent_info", "user_info"):
            temp = ((info.get(side) or {}).get("llm_args") or {}).get("temperature")
            if temp != 0.0:
                out.append(f"{inp.role}: {side} temperature {temp} is not 0.0")
    return out


def completeness(runs: pd.DataFrame, retries_exhausted: Sequence[str]) -> dict[str, Any]:
    """Section 8 (as in section 14): task status from its four cells."""
    rows = []
    for task in TASKS:
        states = {}
        for c in CELLS:
            sub = runs[(runs["role"] == c) & (runs["task_id"] == task)]
            n_valid = int((~sub["is_infra_error"]).sum())
            if n_valid >= REPS:
                states[c] = rq1.COMPLETE
            elif len(sub) < REPS:
                states[c] = rq1.NOT_YET_RUN
            else:
                states[c] = rq1.INFRA_INCOMPLETE
        confirmed = task in set(retries_exhausted) and rq1.INFRA_INCOMPLETE in states.values()
        status = rq1._task_status(list(states.values()), confirmed)
        rows.append({"task_id": task, "status": status, "cells": states})
    by = {
        s: [r["task_id"] for r in rows if r["status"] == s]
        for s in (rq1.COMPLETE, rq1.PENDING, rq1.AWAITING_RERUN, rq1.DROPPED)
    }
    return {
        "mode": "final" if not by[rq1.PENDING] and not by[rq1.AWAITING_RERUN] else "partial",
        "n_planned": len(TASKS),
        "analysed_tasks": by[rq1.COMPLETE],
        "dropped_tasks": by[rq1.DROPPED],
        "pending_tasks": by[rq1.PENDING],
        "awaiting_rerun_tasks": by[rq1.AWAITING_RERUN],
        "extra_tasks_ignored": rq1.natural_sorted(
            [t for t in runs["task_id"].unique() if t not in set(TASKS)]
        ),
        "per_task": rows,
    }


def analysis_set(runs: pd.DataFrame, tasks: Sequence[str]) -> tuple[pd.DataFrame, list[str]]:
    """Valid simulations of ``tasks`` (first R per cell); screens as booleans."""
    valid, _ = rq1.analysis_frame(runs, tasks, REPS)
    problems = []
    unmatched = int(valid["customer_turns"].isna().sum())
    if unmatched:
        problems.append(
            f"{unmatched} valid simulation(s) without a matched transcript: their fidelity "
            "screens count as not flagged"
        )
    for col in SCREENS:
        valid[col] = valid[col].astype("boolean").fillna(False).astype(bool)
    return valid, problems


# ---------------------------------------------------------------------------
# H3a-H3c
# ---------------------------------------------------------------------------


def per_task(valid: pd.DataFrame, c: str, col: str = "success") -> dict[str, list[bool]]:
    out: dict[str, list[bool]] = {}
    sub = valid[valid["role"] == c]
    for task, value in zip(sub["task_id"], sub[col], strict=True):
        out.setdefault(str(task), []).append(bool(value))
    return out


def task_mean_rate(outcomes: Mapping[str, Sequence[bool]]) -> float:
    """pass^1 = mean over tasks of the per-task rate (equals the pooled rate when balanced)."""
    return metrics.pass_hat_k(outcomes, 1) if outcomes else math.nan


def paired(
    a: Mapping[str, Sequence[bool]], b: Mapping[str, Sequence[bool]], n_boot: int, seed: int
) -> dict[str, Any]:
    """pass^1(a) - pass^1(b) on the tasks in both, paired task-cluster bootstrap."""
    shared = [t for t in a if t in b]
    if not shared:
        return {"estimate": math.nan, "ci_low": math.nan, "ci_high": math.nan, "n_tasks": 0}
    d, lo, hi = metrics.paired_bootstrap_diff(a, b, k=1, n_boot=n_boot, alpha=ALPHA, seed=seed)
    return {"estimate": d, "ci_low": lo, "ci_high": hi, "n_tasks": len(shared)}


def diff_in_diff(
    succ: Mapping[str, Mapping[str, Sequence[bool]]], n_boot: int, seed: int
) -> dict[str, Any]:
    """DiD = gap_base - gap_fix, gap = pass^1(qwen) - pass^1(gpt-oss), paired over tasks.

    Per task ``d_t = (q_b - g_b) - (q_f - g_f)`` from the four cells' success rates; each
    replicate resamples tasks once for all four cells (as ``paired_bootstrap_diff`` does).
    """
    shared = [t for t in succ[CELLS[0]] if all(t in succ[c] for c in CELLS)]
    if not shared:
        return {"estimate": math.nan, "ci_low": math.nan, "ci_high": math.nan, "n_tasks": 0}
    p = {c: np.array([np.mean(succ[c][t]) for t in shared]) for c in CELLS}
    gap = {v: p[cell(v, QWEN)] - p[cell(v, GPTOSS)] for v in VERSIONS}
    d = gap[BASE] - gap[FIX]
    idx = np.random.default_rng(seed).integers(0, d.size, size=(n_boot, d.size))
    lo, hi = metrics._percentile_interval(d[idx].mean(axis=1), ALPHA)
    return {
        "estimate": float(d.mean()),
        "ci_low": lo,
        "ci_high": hi,
        "n_tasks": len(shared),
        "gap_base": float(gap[BASE].mean()),
        "gap_fix": float(gap[FIX].mean()),
    }


def contrasts(valid: pd.DataFrame, n_boot: int, seed: int) -> dict[str, Any]:
    succ = {c: per_task(valid, c) for c in CELLS}
    stop = {v: per_task(valid, cell(v, GPTOSS), STRICT) for v in VERSIONS}
    h3a = paired(stop[FIX], stop[BASE], n_boot, seed)
    h3a.update(
        rate_base=task_mean_rate(stop[BASE]),
        rate_fix=task_mean_rate(stop[FIX]),
        rule="95% CI entirely below 0",
        supported=bool(math.isfinite(h3a["ci_high"]) and h3a["ci_high"] < 0),
    )
    h3b = diff_in_diff(succ, n_boot, seed)
    h3b.update(
        rule="lower bound of the 95% CI > 0",
        supported=bool(math.isfinite(h3b["ci_low"]) and h3b["ci_low"] > 0),
    )
    h3c = {s: paired(succ[cell(FIX, s)], succ[cell(BASE, s)], n_boot, seed) for s in SIMS}
    for s in SIMS:
        h3c[s].update(
            pass1_base=task_mean_rate(succ[cell(BASE, s)]),
            pass1_fix=task_mean_rate(succ[cell(FIX, s)]),
        )
    return {"H3a": h3a, "H3b": h3b, "H3c": h3c}


# ---------------------------------------------------------------------------
# H3d, H3e, hand-read counts
# ---------------------------------------------------------------------------


def wilson(k: int, n: int) -> dict[str, Any]:
    lo, hi = metrics.wilson_ci(k, n, ALPHA) if n else (math.nan, math.nan)
    return {"count": k, "n": n, "rate": k / n if n else math.nan, "ci_low": lo, "ci_high": hi}


def descriptives(runs: pd.DataFrame, valid: pd.DataFrame, scope: Sequence[str]) -> dict[str, Any]:
    """H3d per cell on the analysis set; infra errors on every simulation of ``scope``."""
    out: dict[str, Any] = {}
    for c in CELLS:
        sub = valid[valid["role"] == c]
        allsims = runs[(runs["role"] == c) & runs["task_id"].isin(set(scope))]
        n = len(sub)
        ends = sub["termination_reason"].value_counts()
        out[c] = {
            "valid_simulations": n,
            **{s: wilson(int(sub[s].sum()), n) for s in SCREENS},
            "max_steps": wilson(int(ends.get("max_steps", 0)), n),
            "customer_turns_mean": float(sub["customer_turns"].mean()) if n else math.nan,
            "termination_reasons": {str(k): int(v) for k, v in sorted(ends.items())},
            "infra_errors_in_file": wilson(int(allsims["is_infra_error"].sum()), len(allsims)),
        }
    return out


def decompositions(valid: pd.DataFrame, n_boot: int, seed: int) -> dict[str, Any]:
    """H3e: Design A per version and the paired difference fix - base."""
    sim_role = {
        str(llm): role.split("/")[1]
        for llm, role in valid[["user_llm", "role"]].drop_duplicates().itertuples(index=False)
    }
    by_version = {v: valid[valid["role"].str.startswith(v + "/")] for v in VERSIONS}
    out: dict[str, Any] = {v: rq1.step3(by_version[v], sim_role, n_boot, seed) for v in VERSIONS}
    diff = variance.compare_decompositions(
        by_version[BASE],
        by_version[FIX],
        design="crossed",
        labels=(BASE, FIX),
        n_boot=n_boot,
        seed=seed,
        alpha=ALPHA,
        sim_col="user_llm",
        n_reps=REPS,
        on_unbalanced="raise",
    )
    out["difference_fix_minus_base"] = {
        str(name): {k: float(v) for k, v in row.items()} for name, row in diff.iterrows()
    }
    return out


def blind_id(salt: str, role: str, task: str, trial: int) -> str:
    """Opaque id; the random salt lives only in the key, so ids cannot be recomputed."""
    return hashlib.sha256(f"{salt}|{role}|{task}|{trial}".encode()).hexdigest()[:10]


def analysed(args: argparse.Namespace) -> tuple[pd.DataFrame, list[rq1.InputRun], list[str], dict]:
    """Load, apply section 8, refuse partial data unless allowed (outcomes wait for the end)."""
    runs, inputs, warnings_ = load(input_paths(args))
    status = completeness(runs, args.retries_exhausted)
    if status["mode"] != "final" and not args.allow_partial:
        raise rq1.InputError(
            "data are partial (section 8): RQ3 outcomes are analysed only when collection is "
            "complete (section 14); pass --allow-partial to override"
        )
    if len(status["analysed_tasks"]) < 2:
        raise rq1.InputError("fewer than 2 tasks complete in all four cells: nothing to analyse")
    return runs, inputs, warnings_, status


def flagged(valid: pd.DataFrame) -> pd.DataFrame:
    """Simulations flagged by the strict or loose stop screen (section 14: all are read)."""
    return valid[valid[STRICT] | valid[LOOSE]]


def hand_counts(valid: pd.DataFrame, labels_path: str, key_path: str) -> dict[str, Any]:
    """Aggregate the filled blinded sheet per cell."""
    try:
        key = json.loads(Path(key_path).read_text(encoding="utf-8"))
        with open(labels_path, encoding="utf-8", newline="") as fh:
            labels = {row["blind_id"].strip(): row for row in csv.DictReader(fh)}
    except (OSError, ValueError, KeyError) as exc:
        raise rq1.InputError(f"cannot read --hand-labels / --blind-key: {exc}") from exc
    unknown = sorted(set(labels) - set(key))
    if unknown:
        raise rq1.InputError(f"--hand-labels has blind ids not in --blind-key: {unknown}")
    per_cell = {
        c: {
            "flagged": 0,
            "labelled": 0,
            "expected_action_yes": 0,
            "deciding_yes": 0,
            "deciding_likely": 0,
        }
        for c in CELLS
    }
    unlabelled = []
    salt = next(iter(key.values()))["salt"] if key else ""
    expected = {
        blind_id(salt, r.role, str(r.task_id), int(r.trial)) for r in flagged(valid).itertuples()
    }
    for bid, k in key.items():
        c = per_cell[k["cell"]]
        c["flagged"] += 1
        row = {f: (labels.get(bid) or {}).get(f, "").strip().lower() for f in LABEL_VALUES}
        if not all(row.values()):
            unlabelled.append(bid)
            continue
        for f, allowed in LABEL_VALUES.items():
            if row[f] not in allowed:
                raise rq1.InputError(f"--hand-labels {bid}: {f} must be one of {allowed}")
        c["labelled"] += 1
        c["expected_action_yes"] += row["expected_action"] == "yes"
        c["deciding_yes"] += row["deciding"] == "yes"
        c["deciding_likely"] += row["deciding"] == "likely"
    return {
        "per_cell": per_cell,
        "unlabelled": sorted(unlabelled),
        "key_matches_current_flags": set(key) == expected,
        "labels": "expected_action: the agreed action is in the task's expected actions; "
        "deciding: the early stop was the only (yes) or likely (likely) deciding failure",
    }


def _transcript(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for m in messages:
        calls = ", ".join(
            f"{c.get('name')}({json.dumps(c.get('arguments'), ensure_ascii=False)})"
            for c in m.get("tool_calls") or []
        )
        text = (m.get("content") or "").strip()
        lines.append(f"- **{m.get('role')}**: {text}" + (f" -> {calls}" if calls else ""))
    return lines


def export_blind_sheet(valid: pd.DataFrame, out_dir: Path) -> int:
    """Write the reading sheet, an empty labels CSV and the key (version hidden in the sheet)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(16)
    cache: dict[str, dict[tuple[str, int], Mapping[str, Any]]] = {}
    items = []
    for r in flagged(valid).itertuples():
        if r.results_path not in cache:
            sims = read_results(r.results_path)["simulations"]
            cache[r.results_path] = {(str(s["task_id"]), int(s["trial"])): s for s in sims}
        sim = cache[r.results_path][(str(r.task_id), int(r.trial))]
        bid = blind_id(salt, r.role, str(r.task_id), int(r.trial))
        items.append((bid, r, sim))
    items.sort(key=lambda x: x[0])
    sheet = [
        "# RQ3 flagged stops: blinded reading sheet",
        "",
        "Version (base / fix) is hidden. For each item fill `blind_labels.csv`: "
        "`expected_action` yes/no, `deciding` yes/likely/no. Do not open the key before "
        "every row is labelled.",
        "",
    ]
    for bid, r, sim in items:
        info = sim.get("reward_info") or {}
        checks = []
        for a in info.get("action_checks") or []:
            name = (a.get("action") or {}).get("name")
            checks.append(f"{name}: {'match' if a.get('action_match') else 'MISSING'}")
        sheet += [
            f"## {bid}",
            "",
            f"Task {r.task_id}, simulator {r.role.split('/')[1]}, "
            f"strict screen {bool(getattr(r, STRICT))}, reward {info.get('reward')}",
            "",
            "Expected actions: " + ("; ".join(checks) or "none recorded"),
            "",
            *_transcript(sim.get("messages") or []),
            "",
        ]
    (out_dir / "blind_sheet.md").write_text("\n".join(sheet), encoding="utf-8")
    with open(out_dir / "blind_labels.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["blind_id", "expected_action", "deciding", "notes"])
        writer.writerows([bid, "", "", ""] for bid, _, _ in items)
    key = {
        bid: {"cell": r.role, "task_id": str(r.task_id), "trial": int(r.trial), "salt": salt}
        for bid, r, _ in items
    }
    (out_dir / "KEY_open_after_labelling.json").write_text(
        json.dumps(key, indent=2) + "\n", encoding="utf-8"
    )
    return len(items)


# ---------------------------------------------------------------------------
# Run, report, CLI
# ---------------------------------------------------------------------------


def code_hashes() -> dict[str, str]:
    package = Path(variance.__file__).resolve().parent
    files = [Path(__file__).resolve(), Path(rq1.__file__).resolve()] + [
        package / f"{m}.py" for m in ("metrics", "variance", "tau2_results", "fidelity")
    ]
    return {p.relative_to(REPO_ROOT).as_posix(): rq1.sha256_file(p) for p in files}


def run_analysis(args: argparse.Namespace) -> dict[str, Any]:
    runs, inputs, warnings_, status = analysed(args)
    tasks = status["analysed_tasks"]
    valid, problems = analysis_set(runs, tasks)
    warnings_ += problems
    scope = [t for t in TASKS if t not in set(status["dropped_tasks"])]
    keep = ~valid[list(MERGE_SCREENS)].any(axis=1)
    succ = {c: per_task(valid, c) for c in CELLS}
    res: dict[str, Any] = {
        "schema": SCHEMA,
        "analysis_timestamp": args.analysis_timestamp or datetime.now().astimezone().isoformat(),
        "inputs": {
            inp.role: {
                "path": inp.path_arg,
                "run_name": inp.run_name,
                "sha256": inp.sha256,
                "git_commit": inp.info.get("git_commit"),
            }
            for inp in inputs
        },
        "settings": {"n_boot": args.n_boot, "seed": args.seed, "alpha": ALPHA, "reps": REPS},
        "prereg": rq1.read_git_info(REPO_ROOT, tag="prereg-v3"),
        "code_sha256": code_hashes(),
        "python": platform.python_version(),
        "protocol_warnings": warnings_,
        "section8": status,
        "confirmatory": contrasts(valid, args.n_boot, args.seed),
        "pass_k": {c: rq1.pass_k_block(succ[c], (1, 2, 3), args.n_boot, args.seed) for c in CELLS},
        "H3d": descriptives(runs, valid, scope),
        "H3e": decompositions(valid, args.n_boot, args.seed),
        "exploratory_without_merged": {
            "excluded_per_cell": {c: int((~keep & (valid["role"] == c)).sum()) for c in CELLS},
            **contrasts(valid[keep], args.n_boot, args.seed),
        },
        "hand_read": hand_counts(valid, args.hand_labels, args.blind_key)
        if args.hand_labels
        else None,
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


def _decision_row(h: str, d: Mapping[str, Any]) -> list[str]:
    verdict = "SUPPORTED" if d["supported"] else "NOT SUPPORTED"
    return [h, rq1.fmt_ci(d, 4), str(d["n_tasks"]), d["rule"], f"**{verdict}**"]


def _contrast_lines(c: Mapping[str, Any]) -> list[str]:
    a, b = c["H3a"], c["H3b"]
    lines = [
        rq1.md_table(
            ["Hypothesis", "Estimate [95% CI]", "Tasks", "Rule", "Verdict"],
            [
                _decision_row("H3a D_stop (fix - base)", a),
                _decision_row("H3b DiD (gap_base - gap_fix)", b),
            ],
        ),
        "",
        f"Strict premature-stop rate, gpt-oss-120b: base {rq1.fmt(a['rate_base'])}, fix "
        f"{rq1.fmt(a['rate_fix'])}. Gap pass^1(qwen3.8) - pass^1(gpt-oss): base "
        f"{rq1.fmt(b.get('gap_base'))}, fix {rq1.fmt(b.get('gap_fix'))}.",
        "",
        rq1.md_table(
            ["H3c: pass^1 fix - base", "base", "fix", "Difference [95% CI]", "Tasks"],
            [
                [
                    s,
                    rq1.fmt(c["H3c"][s]["pass1_base"]),
                    rq1.fmt(c["H3c"][s]["pass1_fix"]),
                    rq1.fmt_ci(c["H3c"][s]),
                    str(c["H3c"][s]["n_tasks"]),
                ]
                for s in SIMS
            ],
        ),
    ]
    return lines


def _count_ci(w: Mapping[str, Any]) -> str:
    return f"{w['count']} ({rq1.fmt(w['ci_low'])}-{rq1.fmt(w['ci_high'])})"


def render_report(res: Mapping[str, Any]) -> str:
    s8, d = res["section8"], res["H3d"]
    out = [
        "# RQ3 analysis: tau2-bench PR #702 with two user simulators",
        "",
        f"**{res['label']}**"
        + (": " + "; ".join(res["provisional_reasons"]) if res["provisional_reasons"] else ""),
        "",
        f"Analysis {res['analysis_timestamp']}; B = {res['settings']['n_boot']}, seed "
        f"{res['settings']['seed']}; pre-registration `prereg-v3` "
        f"({(res['prereg'] or {}).get('tag_commit') or 'tag not found'}).",
        "",
        "## Inputs",
        "",
        rq1.md_table(
            ["Cell", "Run", "tau2 commit", "sha256"],
            [
                [c, i["run_name"], str(i["git_commit"])[:7], i["sha256"][:16]]
                for c, i in res["inputs"].items()
            ],
        ),
        "",
        "## Section 8: data",
        "",
        f"Mode **{s8['mode']}**: {len(s8['analysed_tasks'])} of {s8['n_planned']} tasks analysed; "
        f"dropped {rq1.task_list(s8['dropped_tasks'])}; "
        f"pending {rq1.task_list(s8['pending_tasks'])}; "
        f"awaiting re-run {rq1.task_list(s8['awaiting_rerun_tasks'])}."
        + (f" {UNDERCOVER}." if len(s8["analysed_tasks"]) < 20 else ""),
        "",
        "## Confirmatory: H3a, H3b; secondary: H3c",
        "",
        *_contrast_lines(res["confirmatory"]),
        "",
        "H3b is weakly powered: a null is reported as not detected at this sample size.",
        "",
        "pass^k per cell (task-cluster bootstrap):",
        "",
        rq1.md_table(
            ["Cell", "pass^1", "pass^2", "pass^3"],
            [[c, *(rq1.fmt_ci(res["pass_k"][c][f"pass^{k}"]) for k in (1, 2, 3))] for c in CELLS],
        ),
        "",
        "## H3d: screens per cell (Wilson 95% CI)",
        "",
        rq1.md_table(
            [
                "Cell",
                "valid",
                "strict stop",
                "loose stop",
                "merged turns",
                "first-turn stop",
                "max_steps",
                "customer turns (mean)",
                "infra errors",
            ],
            [
                [
                    c,
                    str(d[c]["valid_simulations"]),
                    *(
                        _count_ci(d[c][k])
                        for k in (STRICT, LOOSE, "merged_turns", "first_turn_stop", "max_steps")
                    ),
                    rq1.fmt(d[c]["customer_turns_mean"], 1),
                    f"{d[c]['infra_errors_in_file']['count']}/{d[c]['infra_errors_in_file']['n']}",
                ]
                for c in CELLS
            ],
        ),
        "",
        "Infra errors are those left in the files after re-runs (tau2 --auto-resume deletes the "
        "ones it re-runs).",
        "",
    ]
    hand = res["hand_read"]
    out += ["Hand-read flagged stops (blind to version):", ""]
    if hand is None:
        out += [
            "Not yet: export the sheet with `--export-blind-sheet`, label it, then pass "
            "`--hand-labels` and `--blind-key`.",
            "",
        ]
    else:
        out += [
            rq1.md_table(
                [
                    "Cell",
                    "flagged",
                    "labelled",
                    "on an expected action",
                    "stop deciding",
                    "stop likely deciding",
                ],
                [[c, *(str(v) for v in hand["per_cell"][c].values())] for c in CELLS],
            ),
            "",
            f"Unlabelled: {', '.join(hand['unlabelled']) or 'none'}. Key matches the "
            f"current flags: {hand['key_matches_current_flags']}.",
            "",
        ]
    e = res["H3e"]
    comps = variance.CROSSED_COMPONENTS
    out += [
        "## H3e (exploratory): Design A components within each version",
        "",
        rq1.md_table(
            ["Component", "base [95% CI]", "fix [95% CI]"],
            [
                [
                    n,
                    rq1.fmt_ci(e[BASE]["components"][n], 4, "raw"),
                    rq1.fmt_ci(e[FIX]["components"][n], 4, "raw"),
                ]
                for n in comps
            ],
        ),
        "",
        rq1.md_table(
            ["Statistic", "fix - base [95% CI]"],
            [
                [n, rq1.fmt_ci(v, 4, "difference")]
                for n, v in e["difference_fix_minus_base"].items()
            ],
        ),
        "",
        "The simulator component is not interpreted (S = 2, section 7).",
        "",
        "## Exploratory (deviation 2026-10-31): without merged-turn / first-turn-stop simulations",
        "",
        "Excluded per cell: "
        + ", ".join(
            f"{c} {n}" for c, n in res["exploratory_without_merged"]["excluded_per_cell"].items()
        ),
        "",
        *_contrast_lines(res["exploratory_without_merged"]),
        "",
    ]
    if res["protocol_warnings"]:
        out += ["## Protocol warnings", "", *(f"- {w}" for w in res["protocol_warnings"]), ""]
    out += ["## Code", "", *(f"- `{k}` {v[:16]}" for k, v in res["code_sha256"].items()), ""]
    return "\n".join(out)


def write_outputs(res: Mapping[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(rq1.to_jsonable(res), indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(render_report(res), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for c in CELLS:
        p.add_argument(INPUT_FLAGS[c], required=True, help=f"results.json of cell {c}")
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "results" / "rq3_analysis")
    p.add_argument("--n-boot", type=int, default=N_BOOT)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument(
        "--retries-exhausted",
        nargs="*",
        default=[],
        metavar="TASK",
        help="tasks whose infra-error cells have had their 3 extra attempts",
    )
    p.add_argument("--hand-labels", help="filled blind_labels.csv")
    p.add_argument("--blind-key", help="KEY_open_after_labelling.json from --export-blind-sheet")
    p.add_argument(
        "--export-blind-sheet",
        type=Path,
        metavar="DIR",
        help="write the blinded reading sheet of the flagged stops and stop",
    )
    p.add_argument("--analysis-timestamp", help="pin the timestamp recorded in the outputs")
    p.add_argument(
        "--allow-partial",
        action="store_true",
        help="analyse before collection is complete (an interim look; never confirmatory)",
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.hand_labels) != bool(args.blind_key):
        print("error: --hand-labels and --blind-key go together", file=sys.stderr)
        return 2
    try:
        if args.export_blind_sheet:
            runs, _, _, status = analysed(args)
            valid = analysis_set(runs, status["analysed_tasks"])[0]
            n = export_blind_sheet(valid, args.export_blind_sheet)
            print(f"wrote {n} flagged simulation(s) to {args.export_blind_sheet}")
            return 0
        res = run_analysis(args)
    except rq1.InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    write_outputs(res, args.out_dir)
    print(f"{res['label']}: wrote {args.out_dir / 'report.md'} and results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
