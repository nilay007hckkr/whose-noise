"""Command-line summary of one or more tau2-bench runs.

Usage::

    python -m whose_noise.analyze RESULTS_JSON [RESULTS_JSON ...] [--csv out.csv] [--k 1 2 3]

For each run it prints:

* a summary: simulations, infra errors (excluded), success with a Wilson 95% CI,
  pass^k for each requested k that every task has enough valid trials for, the
  across-trial verdict flip rate, mean tokens per simulation for agent and user,
  and mean duration;
* a per-task table (valid trials, successes, whether the verdict flipped);
* the failed simulations with their first failed expected action;
* a Groq free-tier budget line (how many tasks/day the user simulator can afford).

Statistics come from ``whose_noise.metrics`` when it is importable and its functions
accept the expected arguments; otherwise small local fallbacks are used, so this
tool keeps working while ``metrics.py`` is being developed.

Two success numbers are printed. ``pooled success`` = successes / valid simulations,
with a Wilson 95% CI that treats every simulation as an independent Bernoulli draw;
with several trials per task the draws are clustered by task, so that interval is too
narrow. ``pass^k`` is the tau-bench per-task estimator averaged over tasks, with a
task-cluster bootstrap 95% CI (resampling tasks) when ``metrics.py`` provides it.
The two agree when every task has the same number of valid trials.

The percentile task bootstrap under-covers with few tasks (Monte Carlo, 3 trials per
task, bimodal task difficulty: pass^1 coverage 0.86 at 5 tasks, 0.93 at 10, 0.955 at
40; pass^3 coverage 0.68 at 5 tasks). Below ``MIN_TASKS_FOR_RELIABLE_BOOTSTRAP`` tasks
the interval is therefore labelled "approximate, under-covers"; below
``MIN_TASKS_FOR_BOOTSTRAP`` it is not shown at all. With so few tasks, read the
per-task table instead.

Results files that repeat a ``(task_id, trial)`` pair or mix base seeds are refused
(see ``tau2_results.trial_integrity_problems``); ``--allow-duplicates`` loads them anyway
and prints a WARNING in the summary.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pandas as pd

from whose_noise.tau2_results import (
    DuplicateTrialError,
    load_runs,
    outcomes_by_task,
    trial_integrity_problems,
)

GROQ_TOKENS_PER_DAY = 200_000
GROQ_REQUESTS_PER_DAY = 1_000
Z_95 = 1.959963984540054
# A percentile bootstrap over very few tasks is degenerate (e.g. 2 tasks with
# per-task pass^1 of 0.5 each give the interval [0.5, 0.5]); suppress it below this.
MIN_TASKS_FOR_BOOTSTRAP = 5
# Below this many tasks the percentile task bootstrap under-covers noticeably (see the
# module docstring); intervals are printed but labelled approximate.
MIN_TASKS_FOR_RELIABLE_BOOTSTRAP = 20


# --------------------------------------------------------------------------- metrics


def _metrics_module() -> ModuleType | None:
    """Import ``whose_noise.metrics`` lazily; None if it does not exist yet."""
    try:
        from whose_noise import metrics
    except ImportError:
        return None
    return metrics


def _wilson_fallback(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pass_hat_k_fallback(outcomes: Mapping[str, Sequence[bool]], k: int) -> float:
    """tau-bench pass^k: mean over tasks of C(c, k) / C(n, k) (unbiased estimator)."""
    vals = []
    for trials in outcomes.values():
        n, c = len(trials), sum(bool(x) for x in trials)
        if n < k:
            raise ValueError(f"task has {n} trials < k={k}")
        vals.append(math.comb(c, k) / math.comb(n, k))
    return float(sum(vals) / len(vals)) if vals else float("nan")


def _is_interval(value: Any) -> bool:
    try:
        lo, hi = value
        return 0.0 <= float(lo) <= float(hi) <= 1.0
    except (TypeError, ValueError):
        return False


def wilson_interval(successes: int, n: int) -> tuple[float, float]:
    """95% Wilson CI, via ``metrics.wilson_ci`` if available, else the local fallback."""
    fn = getattr(_metrics_module(), "wilson_ci", None)
    if fn is not None and n > 0:
        try:
            result = fn(successes, n)
            if _is_interval(result):
                lo, hi = result
                return (float(lo), float(hi))
        except (TypeError, ValueError):
            pass
    return _wilson_fallback(successes, n)


def pass_hat_k(outcomes: Mapping[str, Sequence[bool]], k: int) -> float:
    """pass^k via ``metrics.pass_hat_k`` if available, else the local fallback."""
    fn = getattr(_metrics_module(), "pass_hat_k", None)
    if fn is not None:
        try:
            value = float(fn(outcomes, k))
            if 0.0 <= value <= 1.0:
                return value
        except (TypeError, ValueError):
            pass
    return _pass_hat_k_fallback(outcomes, k)


def cluster_interval(
    outcomes: Mapping[str, Sequence[bool]],
    k: int = 1,
    n_boot: int = 2000,
    seed: int = 0,
    min_tasks: int = MIN_TASKS_FOR_BOOTSTRAP,
) -> tuple[float, float] | None:
    """Task-cluster bootstrap 95% CI of pass^k via ``metrics.cluster_bootstrap_ci``.

    Tasks (not simulations) are resampled, so correlation between trials of the same
    task is respected. Returns None when ``metrics`` is missing, when there are fewer
    than ``min_tasks`` tasks, or when its API does not match ``cluster_bootstrap_ci(outcomes,
    stat_fn, n_boot=, seed=) -> (point, lo, hi)``. There is deliberately no local
    fallback: the bootstrap belongs in ``metrics.py``. Seeded, hence deterministic.
    """
    metrics = _metrics_module()
    fn = getattr(metrics, "cluster_bootstrap_ci", None)
    if fn is None or len(outcomes) < max(2, min_tasks):
        return None
    try:
        result = fn(outcomes, lambda o: pass_hat_k(o, k), n_boot=n_boot, seed=seed)
        _, lo, hi = result
    except (TypeError, ValueError):
        return None
    return (float(lo), float(hi)) if _is_interval((lo, hi)) else None


# --------------------------------------------------------------------------- tables


@dataclass
class RunSummary:
    """Aggregate numbers for one tau2 run (one ``results.json``)."""

    run_name: str
    results_path: str
    agent_llm: str | None
    user_llm: str | None
    n_sims: int
    n_infra: int
    n_valid: int
    n_tasks: int
    max_trials: int
    min_valid_trials: int
    successes: int
    pooled_rate: float
    pooled_wilson: tuple[float, float]
    pass_k: dict[int, float | None] = field(default_factory=dict)
    pass_k_ci: dict[int, tuple[float, float] | None] = field(default_factory=dict)
    flip_rate: float | None = None
    n_flips: int = 0
    n_flip_eligible: int = 0
    mean_agent_tokens: float = float("nan")
    mean_user_tokens: float = float("nan")
    mean_user_requests: float = float("nan")
    mean_duration_s: float = float("nan")
    termination_counts: dict[str, int] = field(default_factory=dict)
    infra_breakdown: dict[str, int] = field(default_factory=dict)
    n_tasks_dropped: int = 0
    integrity_problems: list[str] = field(default_factory=list)


def _first_or_none(series: pd.Series) -> Any:
    vals = series.dropna()
    return vals.iloc[0] if len(vals) else None


def summarize_run(run_df: pd.DataFrame, ks: Sequence[int] | None = None) -> RunSummary:
    """Compute :class:`RunSummary` for the rows of a single run.

    Args:
        run_df: Rows from :func:`load_runs` belonging to one results file.
        ks: Values of k for pass^k. Default: 1..max trials per task. A k is reported
            as None when some task has fewer than k valid (non-infra) trials.
    """
    valid = run_df[~run_df["is_infra_error"]]
    outcomes = outcomes_by_task(run_df, exclude_infra=True)
    n_valid = len(valid)
    successes = int(valid["success"].astype(bool).sum()) if n_valid else 0
    max_trials = int(run_df.groupby("task_id").size().max()) if len(run_df) else 0
    min_valid = min((len(v) for v in outcomes.values()), default=0)

    if ks is None:
        ks = list(range(1, max_trials + 1))
    pass_k: dict[int, float | None] = {}
    pass_k_ci: dict[int, tuple[float, float] | None] = {}
    for k in ks:
        ok = bool(outcomes) and min_valid >= k
        pass_k[k] = pass_hat_k(outcomes, k) if ok else None
        pass_k_ci[k] = cluster_interval(outcomes, k) if ok else None

    eligible = [v for v in outcomes.values() if len(v) >= 2]
    flips = sum(1 for v in eligible if 0 < sum(v) < len(v))

    agent_tokens = valid["agent_prompt_tokens"] + valid["agent_completion_tokens"]
    user_tokens = valid["user_prompt_tokens"] + valid["user_completion_tokens"]

    return RunSummary(
        run_name=str(run_df["run_name"].iloc[0]),
        results_path=str(run_df["results_path"].iloc[0]),
        agent_llm=_first_or_none(run_df["agent_llm"]),
        user_llm=_first_or_none(run_df["user_llm"]),
        n_sims=len(run_df),
        n_infra=int(run_df["is_infra_error"].sum()),
        n_valid=n_valid,
        n_tasks=len(outcomes),
        max_trials=max_trials,
        min_valid_trials=min_valid,
        successes=successes,
        pooled_rate=successes / n_valid if n_valid else float("nan"),
        pooled_wilson=wilson_interval(successes, n_valid),
        pass_k=pass_k,
        pass_k_ci=pass_k_ci,
        flip_rate=flips / len(eligible) if eligible else None,
        n_flips=flips,
        n_flip_eligible=len(eligible),
        mean_agent_tokens=float(agent_tokens.mean()) if n_valid else float("nan"),
        mean_user_tokens=float(user_tokens.mean()) if n_valid else float("nan"),
        mean_user_requests=float(valid["n_user_turns"].mean()) if n_valid else float("nan"),
        mean_duration_s=float(valid["duration_s"].mean()) if n_valid else float("nan"),
        termination_counts={
            str(k): int(v) for k, v in run_df["termination_reason"].value_counts().items()
        },
        infra_breakdown=_infra_breakdown(run_df),
        n_tasks_dropped=run_df["task_id"].nunique() - len(outcomes),
        integrity_problems=trial_integrity_problems(run_df),
    )


def _infra_breakdown(run_df: pd.DataFrame) -> dict[str, int]:
    """Count infra errors by '<error_type> @ <error_stage>'."""
    infra = run_df[run_df["is_infra_error"]]
    labels = infra["error_type"].fillna("unknown") + " @ " + infra["error_stage"].fillna("?")
    return {str(k): int(v) for k, v in labels.value_counts().items()}


def task_table(run_df: pd.DataFrame) -> pd.DataFrame:
    """Per-task outcome table: valid trials, infra errors, successes, verdict flip.

    ``flip`` is "yes" when a task has both a success and a failure among its valid
    trials, "no" when all valid trials agree, and "n/a" with fewer than 2 valid trials.
    """
    rows = []
    for task_id, grp in run_df.groupby("task_id", sort=False):
        valid = grp[~grp["is_infra_error"]]
        n, s = len(valid), int(valid["success"].astype(bool).sum()) if len(valid) else 0
        flip = "n/a" if n < 2 else ("yes" if 0 < s < n else "no")
        rows.append(
            {
                "task": str(task_id),
                "trials": n,
                "infra": int(grp["is_infra_error"].sum()),
                "successes": s,
                "flip": flip,
            }
        )
    return pd.DataFrame(rows, columns=["task", "trials", "infra", "successes", "flip"])


def failed_table(run_df: pd.DataFrame) -> pd.DataFrame:
    """Valid simulations with reward < 1, with termination reason and failing actions."""
    failed = run_df[~run_df["is_infra_error"] & ~run_df["success"].fillna(True).astype(bool)]
    out = pd.DataFrame(
        {
            "task": failed["task_id"].astype(str),
            "trial": failed["trial"],
            "reward": failed["reward"],
            "termination": failed["termination_reason"],
            "db_match": failed["db_match"],
            "first_failed_action": failed["first_failed_action"].fillna("-"),
            "failed_writes": failed["failed_write_actions"].map(lambda t: ";".join(t) or "-"),
            "extra_calls": failed["extra_tool_calls"].map(lambda t: ";".join(t) or "-"),
        }
    )
    return out.reset_index(drop=True)


def groq_budget(
    mean_user_tokens: float,
    mean_user_requests: float,
    tokens_per_day: int = GROQ_TOKENS_PER_DAY,
    requests_per_day: int = GROQ_REQUESTS_PER_DAY,
) -> dict[str, float]:
    """Tasks/day affordable for the user simulator under Groq's daily caps.

    ``tasks_per_day`` = tokens_per_day / mean user tokens per task (the line requested
    for planning). ``tasks_per_day_by_requests`` uses the request cap, counting one
    LLM call per user turn (retries are not visible in results.json, so this is an
    upper bound). The binding limit is the smaller of the two.
    """
    by_tokens = tokens_per_day / mean_user_tokens if mean_user_tokens > 0 else float("nan")
    by_requests = requests_per_day / mean_user_requests if mean_user_requests > 0 else float("nan")
    return {
        "tasks_per_day": by_tokens,
        "tasks_per_day_by_requests": by_requests,
        "binding": float(min(by_tokens, by_requests)),
    }


# --------------------------------------------------------------------------- output


def _fmt(x: float | None, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def format_summary(s: RunSummary) -> str:
    """Render a :class:`RunSummary` as plain ASCII text."""
    lo, hi = s.pooled_wilson
    lines = [
        f"=== run: {s.run_name} ===",
        f"file           : {s.results_path}",
        f"agent / user   : {s.agent_llm} / {s.user_llm}",
        f"simulations    : {s.n_sims} total, {s.n_infra} infra errors excluded, "
        f"{s.n_valid} valid over {s.n_tasks} tasks",
        "terminations   : " + ", ".join(f"{k}={v}" for k, v in s.termination_counts.items()),
    ]
    for problem in s.integrity_problems:
        lines.append(f"WARNING        : duplicated trials, every rate below is suspect: {problem}")
    if s.infra_breakdown:
        lines.append(
            "infra errors   : " + ", ".join(f"{k}={v}" for k, v in s.infra_breakdown.items())
        )
    if s.n_tasks_dropped:
        lines.append(
            f"WARNING        : {s.n_tasks_dropped} task(s) have no valid trial and are absent "
            "from every rate below (selection bias if errors depend on the task)"
        )
    if any(k.endswith("@ evaluation") for k in s.infra_breakdown):
        lines.append(
            "WARNING        : some infra errors happened while *scoring* a finished "
            "conversation (tau2/evaluator); fix the evaluator setup and re-run"
        )
    lines.append(
        f"pooled success : {_fmt(s.pooled_rate)} ({s.successes}/{s.n_valid}) "
        f"Wilson95 [{_fmt(lo)}, {_fmt(hi)}] (treats sims as independent)"
    )
    for k, v in s.pass_k.items():
        label = f"pass^{k}"
        if v is None:
            reason = f"n/a (min valid trials/task = {s.min_valid_trials} < {k})"
            lines.append(f"{label:<15}: {reason}")
            continue
        ci = s.pass_k_ci.get(k)
        if ci is not None:
            ci_txt = f" task-bootstrap95 [{_fmt(ci[0])}, {_fmt(ci[1])}]"
            if s.n_tasks < MIN_TASKS_FOR_RELIABLE_BOOTSTRAP:
                ci_txt += (
                    f" (approximate, under-covers: {s.n_tasks} < "
                    f"{MIN_TASKS_FOR_RELIABLE_BOOTSTRAP} tasks)"
                )
        elif s.n_tasks < MIN_TASKS_FOR_BOOTSTRAP:
            ci_txt = f" (no bootstrap CI: {s.n_tasks} < {MIN_TASKS_FOR_BOOTSTRAP} tasks)"
        else:
            ci_txt = ""
        lines.append(f"{label:<15}: {_fmt(v)} (mean over tasks){ci_txt}")
    flip = (
        f"{_fmt(s.flip_rate)} ({s.n_flips}/{s.n_flip_eligible} tasks with >=2 valid trials)"
        if s.flip_rate is not None
        else "n/a (no task has >=2 valid trials)"
    )
    lines += [
        f"flip rate      : {flip}",
        f"mean tokens/sim: agent {_fmt(s.mean_agent_tokens, 0)}, "
        f"user {_fmt(s.mean_user_tokens, 0)}",
        f"mean duration  : {_fmt(s.mean_duration_s, 1)} s",
    ]
    budget = groq_budget(s.mean_user_tokens, s.mean_user_requests)
    lines.append(
        f"groq budget    : tasks_per_day = {GROQ_TOKENS_PER_DAY} / "
        f"{_fmt(s.mean_user_tokens, 0)} = {_fmt(budget['tasks_per_day'], 1)} "
        f"(request cap {GROQ_REQUESTS_PER_DAY}/day / {_fmt(s.mean_user_requests, 1)} user turns "
        f"= {_fmt(budget['tasks_per_day_by_requests'], 1)}; binding {_fmt(budget['binding'], 1)})"
    )
    return "\n".join(lines)


def _frame_text(df: pd.DataFrame, empty: str) -> str:
    return df.to_string(index=False) if len(df) else empty


def report(df: pd.DataFrame, ks: Sequence[int] | None = None) -> str:
    """Full text report for every run present in ``df``."""
    blocks = []
    for _, run_df in df.groupby("results_path", sort=False):
        s = summarize_run(run_df, ks)
        blocks.append(
            "\n".join(
                [
                    format_summary(s),
                    "",
                    "-- per task --",
                    _frame_text(task_table(run_df), "(no tasks)"),
                    "",
                    "-- failed simulations --",
                    _frame_text(failed_table(run_df), "(none)"),
                ]
            )
        )
    return "\n\n".join(blocks)


def to_csv_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``df`` with tuple-valued columns joined by ';' for CSV export."""
    out = df.copy()
    for col in ("failed_write_actions", "extra_tool_calls"):
        out[col] = out[col].map(";".join)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m whose_noise.analyze",
        description="Summarise tau2-bench results.json files (one block per run).",
    )
    parser.add_argument("results", nargs="+", help="results.json file(s) or run directories")
    parser.add_argument("--csv", help="write the per-simulation table to this CSV path")
    parser.add_argument(
        "--k", type=int, nargs="+", help="k values for pass^k (default: 1..max trials)"
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="load files with repeated (task, trial) pairs or mixed seeds (printed as WARNING)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.k is not None and any(k < 1 for k in args.k):
        build_parser().error("--k values must be >= 1")
    try:
        df = load_runs(args.results, on_duplicates="ignore" if args.allow_duplicates else "raise")
    except DuplicateTrialError as exc:
        print(f"error: {exc}")
        return 2
    if df.empty:
        print("no simulations found")
        return 1
    print(report(df, args.k))
    if args.csv:
        to_csv_frame(df).to_csv(args.csv, index=False)
        print(f"\nwrote {len(df)} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
