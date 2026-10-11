"""Reliability metrics for repeated-trial agent evaluations.

All functions are pure: they take per-task trial outcomes and return numbers.
Randomness (bootstrap) is driven only by an explicit ``seed``.

Outcome convention
------------------
``outcomes`` maps a task id to the sequence of binary trial outcomes for that
task (``True``/``1`` = success, ``False``/``0`` = failure). Infrastructure
errors must be removed (or re-run) *before* calling these functions; a NaN in
the input raises ``ValueError`` rather than being silently counted as failure.

The task is the unit of analysis. Bootstrap intervals resample *tasks* with
replacement (cluster bootstrap), never individual runs, because runs of the
same task are correlated (Miller 2024, "Adding Error Bars to Evals",
arXiv:2411.00640).

pass^k follows Yao et al. 2024 (tau-bench, arXiv:2406.12045, Sec. 3):
``pass^k_t = C(c_t, k) / C(n_t, k)``, the probability that a uniformly random
k-subset of the n_t observed trials is all successes. It is an unbiased
estimator of ``p_t**k``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence

import numpy as np
from scipy import stats

Outcomes = Mapping[Hashable, Sequence[bool | int]]
StatFn = Callable[[Outcomes], float]

__all__ = [
    "Outcomes",
    "cluster_bootstrap_ci",
    "mcnemar_exact",
    "paired_bootstrap_diff",
    "pass_hat_k",
    "pass_hat_k_curve",
    "pass_hat_k_per_task",
    "pass_hat_k_task",
    "wilson_ci",
]


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------


def _as_binary_array(task_id: Hashable, trials: Sequence[bool | int]) -> np.ndarray:
    """Convert one task's trial outcomes to a 0/1 int array, validating values."""
    arr = np.asarray(list(trials), dtype=float)
    if arr.ndim != 1:
        raise ValueError(
            f"Task {task_id!r}: outcomes must be a 1-D sequence, got shape {arr.shape}"
        )
    if np.isnan(arr).any():
        raise ValueError(
            f"Task {task_id!r}: outcomes contain NaN (infrastructure errors?). "
            "Drop or re-run them before computing metrics."
        )
    if not np.isin(arr, (0.0, 1.0)).all():
        raise ValueError(f"Task {task_id!r}: outcomes must be binary (0/1 or bool), got {arr}")
    return arr.astype(np.int64)


def _counts(outcomes: Outcomes) -> tuple[list[Hashable], np.ndarray, np.ndarray]:
    """Return (task_ids, n_trials, n_successes) for a non-empty outcomes mapping."""
    if len(outcomes) == 0:
        raise ValueError("outcomes is empty: need at least one task")
    task_ids = list(outcomes.keys())
    arrays = [_as_binary_array(t, outcomes[t]) for t in task_ids]
    n = np.array([a.size for a in arrays], dtype=np.int64)
    c = np.array([int(a.sum()) for a in arrays], dtype=np.int64)
    return task_ids, n, c


def _check_bootstrap_args(n_boot: int, alpha: float) -> None:
    if n_boot < 1:
        raise ValueError(f"n_boot must be >= 1, got {n_boot}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")


def _percentile_interval(samples: np.ndarray, alpha: float) -> tuple[float, float]:
    """Two-sided percentile interval of bootstrap replicates."""
    lo, hi = np.quantile(samples, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


# ---------------------------------------------------------------------------
# pass^k
# ---------------------------------------------------------------------------


def pass_hat_k_task(n: int, c: int, k: int) -> float:
    """Unbiased per-task pass^k estimate ``C(c, k) / C(n, k)``.

    Args:
        n: Number of valid trials of the task.
        c: Number of successful trials (0 <= c <= n).
        k: Number of trials that must all succeed (1 <= k <= n).

    Returns:
        Probability that a uniformly random k-subset of the n trials is all
        successes; 0.0 when ``c < k``.

    Raises:
        ValueError: if ``k < 1``, ``k > n``, ``c < 0`` or ``c > n``.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got k={k}")
    if k > n:
        raise ValueError(f"k={k} exceeds the number of trials n={n}")
    if c < 0 or c > n:
        raise ValueError(f"c must satisfy 0 <= c <= n, got c={c}, n={n}")
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def pass_hat_k_per_task(outcomes: Outcomes, k: int) -> dict[Hashable, float]:
    """Per-task pass^k values.

    Raises:
        ValueError: if ``outcomes`` is empty, contains non-binary values, or any
            task has fewer than ``k`` trials.
    """
    task_ids, n, c = _counts(outcomes)
    short = [(t, int(nt)) for t, nt in zip(task_ids, n, strict=True) if nt < k]
    if short:
        preview = ", ".join(f"{t!r}(n={nt})" for t, nt in short[:5])
        raise ValueError(
            f"pass^{k} needs at least {k} trials per task; {len(short)} task(s) have fewer: "
            f"{preview}{' ...' if len(short) > 5 else ''}"
        )
    return {
        t: pass_hat_k_task(int(nt), int(ct), k) for t, nt, ct in zip(task_ids, n, c, strict=True)
    }


def pass_hat_k(outcomes: Outcomes, k: int) -> float:
    """Mean of per-task pass^k over tasks (Yao et al. 2024).

    ``pass_hat_k(outcomes, 1)`` equals the mean of per-task success rates
    (which equals the pooled success rate when every task has the same number
    of trials).
    """
    per_task = pass_hat_k_per_task(outcomes, k)
    return float(np.mean(list(per_task.values())))


def pass_hat_k_curve(outcomes: Outcomes, ks: Iterable[int]) -> dict[int, float]:
    """pass^k for each k in ``ks``, as ``{k: value}`` in the order given."""
    return {int(k): pass_hat_k(outcomes, int(k)) for k in ks}


# ---------------------------------------------------------------------------
# Bootstrap intervals
# ---------------------------------------------------------------------------


def cluster_bootstrap_ci(
    outcomes: Outcomes,
    stat_fn: StatFn,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile CI for ``stat_fn`` by resampling tasks with replacement.

    Each bootstrap replicate draws T task ids with replacement from the T
    tasks in ``outcomes`` and keeps *all* trials of each drawn task. Because
    a task may be drawn more than once, the replicate mapping is keyed by
    position (0..T-1), so ``stat_fn`` must not rely on task-id semantics.

    Coverage warning: percentile intervals from a task bootstrap under-cover when
    there are few tasks, worst for pass^k with k close to the number of trials. A
    Monte Carlo check (3 trials/task, task difficulty ~ Beta(0.4, 0.6), nominal 0.95)
    gave coverage 0.86 (pass^1, T=5), 0.93 (pass^1, T=10), 0.955 (pass^1, T=40) and
    0.68 (pass^3, T=5). Treat intervals with T < ~20 as approximate and report the
    per-task counts alongside them.

    Args:
        outcomes: task id -> sequence of binary trial outcomes.
        stat_fn: maps an outcomes mapping to a scalar, e.g.
            ``lambda o: pass_hat_k(o, 2)``.
        n_boot: number of bootstrap replicates.
        alpha: 1 - confidence level (0.05 -> 95% CI).
        seed: seed for ``numpy.random.default_rng``.

    Returns:
        ``(point, lo, hi)`` where ``point = stat_fn(outcomes)``.
    """
    _check_bootstrap_args(n_boot, alpha)
    if len(outcomes) == 0:
        raise ValueError("outcomes is empty: need at least one task")
    point = float(stat_fn(outcomes))
    trials = [list(outcomes[t]) for t in outcomes]
    n_tasks = len(trials)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_tasks, size=(n_boot, n_tasks))
    reps = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        resampled = {pos: trials[i] for pos, i in enumerate(idx[b])}
        reps[b] = stat_fn(resampled)
    lo, hi = _percentile_interval(reps, alpha)
    return point, lo, hi


def paired_bootstrap_diff(
    outcomes_a: Outcomes,
    outcomes_b: Outcomes,
    k: int = 1,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Paired cluster-bootstrap CI for ``pass^k(A) - pass^k(B)``.

    Only tasks present in both mappings are used. Each replicate resamples
    those shared tasks with replacement and applies the *same* draw to A and
    B, so between-task variation common to both conditions cancels (paired
    design, Miller 2024 Sec. 4).

    Since pass^k is a mean over tasks, the statistic on a replicate equals the
    mean of the per-task differences ``d_t = pass^k_t(A) - pass^k_t(B)`` over
    the drawn tasks; this is computed in vectorised form.

    Returns:
        ``(diff, lo, hi)`` with ``diff`` computed on the shared tasks.

    Raises:
        ValueError: if no task is shared, or a shared task has < k trials.
    """
    _check_bootstrap_args(n_boot, alpha)
    shared = [t for t in outcomes_a if t in outcomes_b]
    if not shared:
        raise ValueError("outcomes_a and outcomes_b share no task ids")
    pa = pass_hat_k_per_task({t: outcomes_a[t] for t in shared}, k)
    pb = pass_hat_k_per_task({t: outcomes_b[t] for t in shared}, k)
    d = np.array([pa[t] - pb[t] for t in shared], dtype=float)
    diff = float(d.mean())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(n_boot, d.size))
    reps = d[idx].mean(axis=1)
    lo, hi = _percentile_interval(reps, alpha)
    return diff, lo, hi


# ---------------------------------------------------------------------------
# Paired test and simple proportions
# ---------------------------------------------------------------------------


def mcnemar_exact(
    a: Mapping[Hashable, bool | int], b: Mapping[Hashable, bool | int]
) -> tuple[int, int, float]:
    """Exact two-sided McNemar test on per-task binary verdicts.

    Uses the tasks present in both mappings. Discordant counts:
    ``b01`` = tasks where A failed and B succeeded,
    ``b10`` = tasks where A succeeded and B failed.
    Under H0 (no difference) ``b10 ~ Binomial(b01 + b10, 0.5)``; the p-value
    is the exact two-sided binomial test (``scipy.stats.binomtest``). With no
    discordant pairs the p-value is 1.0.

    Returns:
        ``(b01, b10, p_value)``.
    """
    shared = [t for t in a if t in b]
    if not shared:
        raise ValueError("a and b share no task ids")
    va = _as_binary_array("a", [a[t] for t in shared])
    vb = _as_binary_array("b", [b[t] for t in shared])
    b01 = int(np.sum((va == 0) & (vb == 1)))
    b10 = int(np.sum((va == 1) & (vb == 0)))
    n_disc = b01 + b10
    if n_disc == 0:
        return b01, b10, 1.0
    p_value = float(stats.binomtest(b10, n_disc, 0.5, alternative="two-sided").pvalue)
    return b01, b10, p_value


def wilson_ci(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Appropriate for independent Bernoulli trials only (e.g. one trial per
    task). For repeated trials per task use :func:`cluster_bootstrap_ci`.

    Returns:
        ``(lo, hi)``.

    Raises:
        ValueError: if ``n < 1``, ``successes`` outside ``[0, n]`` or alpha
            outside (0, 1).
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if successes < 0 or successes > n:
        raise ValueError(f"successes must satisfy 0 <= successes <= n, got {successes}, n={n}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    z = float(stats.norm.ppf(1.0 - alpha / 2.0))
    p = successes / n
    z2n = z * z / n
    denom = 1.0 + z2n
    center = (p + z2n / 2.0) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)
