"""Variance decomposition of binary tau-bench outcomes: whose noise is it?

This module answers "how much of the run-to-run variation in tau-bench verdicts comes
from the task, from the user simulator, from their interaction, and from plain
re-run noise?" It works on a long table with one row per (valid) simulation.

Designs
-------
Design A, crossed (:func:`decompose_variance`)
    ``T`` tasks x ``S`` user-simulator configs x ``R`` identical repeats per cell.
    Model: ``y_tsr ~ Bernoulli(p_ts)`` with ``p_ts = mu + a_t + b_s + c_ts`` and
    independent zero-mean effects. Components (all on the probability scale)::

        task              Var(a_t)            how much tasks differ in difficulty
        simulator         Var(b_s)            how much simulators shift the mean
        task_x_simulator  Var(c_ts)           how much the simulator changes WHICH tasks pass
        residual          E[p_ts (1 - p_ts)]  re-run noise with everything held fixed

    By the law of total variance they add up to ``p_bar (1 - p_bar)``.

Design B, seed-crossed and nested in task (:func:`decompose_nested`)
    For each task, ``U`` user seeds x ``A`` agent seeds x ``D`` duplicate runs with
    identical seeds. Components: task, user, agent, user_x_agent and nondeterminism
    (disagreement between same-seed duplicates). With ``D == 1`` the last two cannot be
    separated and are reported together.

Estimators
----------
Balanced ANOVA / method-of-moments (Searle, Casella & McCulloch, *Variance
Components*, 1992). They are unbiased on the probability scale. Raw estimates can be
negative; they are returned raw (and kept raw inside bootstrap draws). Components are
also truncated at 0, and shares are ``max(c_k, 0) / sum_j max(c_j, 0)``.

Truncated shares are BIASED UPWARD for small components: truncation turns zero-mean
sampling noise into positive mass. In a Monte Carlo check (nested design, T=40,
U=A=3, D=2) a true ``user_x_agent`` noise share of 0.012 was estimated as 0.034 on
average (about 3x; about 4x at U=A=2). Report raw components with their CIs and
``frac_boot_nonpositive`` next to every share, show a share only when the raw
component's CI excludes 0 (:attr:`ComponentEstimate.ci_excludes_zero`), and consider
the untruncated ratios ``raw_k / sum_j raw_j`` (:func:`untruncated_shares`,
``ComponentEstimate.raw_share``), which are approximately unbiased but can fall
outside [0, 1].

Uncertainty
-----------
Cluster (task) bootstrap with percentile intervals: whole task slabs (all simulators,
seeds and repeats of a task) are resampled together, with indices drawn once from
``numpy.random.default_rng(seed)``, so results are reproducible. The task bootstrap
treats simulators as fixed. With few simulators (``S`` of 2 to 4) the ``simulator``
component cannot be estimated meaningfully (its interval under-covers, about 0.75
nominal-0.95 in a Monte Carlo check at ``S = 3``). Report per-simulator means and the
``task_x_simulator`` component instead.

Assumptions (all documented again in the public functions)
-----------------------------------------------------------
* Outcomes are binary (tau2's ``|reward - 1| <= 1e-6``). Infrastructure errors are removed
  BEFORE calling this module, because they are missing data, not failures.
* Repeats inside a cell are exchangeable and independent given the cell. Running the
  agent with ``--max-concurrency > 1`` on llama.cpp (batching, KV-cache reuse) or
  ordering runs in time can break this.
* Every component depends on ``p_bar``: near the floor (or ceiling) every component
  looks small. Always report ``p_bar`` next to shares.
* The design must be balanced. Incomplete tasks are dropped whole (never single runs),
  with a warning; extra repeats beyond the target count are truncated by trial order.
  Dropping tasks is missing-NOT-at-random when infrastructure errors depend on task
  difficulty (long conversations hit limits more often), so by default the target
  repeat count is the LARGEST one that keeps at least ``min_task_fraction`` (0.9) of
  the tasks, trading repeats for coverage (truncating repeats by trial order does not
  depend on outcomes). The warning reports the kept share and the success rate of kept
  versus dropped tasks. Best practice: re-run infrastructure errors before analysis.

Synthetic data with known components (:func:`generate_synthetic`,
:func:`generate_synthetic_nested`) is provided for validation.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
from scipy import special, stats

__all__ = [
    "CROSSED_COMPONENTS",
    "DEFAULT_BOOTSTRAP_SEED",
    "NESTED_COMPONENTS",
    "NESTED_COMPONENTS_NO_DUPLICATES",
    "BalancedArray",
    "BootstrapResult",
    "ComponentEstimate",
    "NestedDecomposition",
    "NoiseFloor",
    "Statistic",
    "SyntheticDataset",
    "UnbalancedDesignError",
    "UnbalancedDesignWarning",
    "VarianceDecomposition",
    "between_simulator_disagreement",
    "cell_flip_rate",
    "cluster_bootstrap",
    "compare_decompositions",
    "crossed_components",
    "crossed_mean_squares",
    "crossed_truth",
    "decompose_nested",
    "decompose_variance",
    "flip_rate_mixed_elsewhere",
    "generate_synthetic",
    "generate_synthetic_nested",
    "nested_components",
    "simulate_crossed_arrays",
    "noise_floor",
    "noise_floor_from_residual",
    "task_bootstrap_indices",
    "to_crossed_array",
    "to_nested_array",
    "truncated_shares",
    "untruncated_shares",
    "within_cell_disagreement",
]

DEFAULT_BOOTSTRAP_SEED = 20261011
"""Default seed for the task bootstrap (store it with every reported interval)."""

CROSSED_COMPONENTS: tuple[str, ...] = ("task", "simulator", "task_x_simulator", "residual")
NESTED_COMPONENTS: tuple[str, ...] = (
    "task",
    "user",
    "agent",
    "user_x_agent",
    "nondeterminism",
)
NESTED_COMPONENTS_NO_DUPLICATES: tuple[str, ...] = (
    "task",
    "user",
    "agent",
    "user_x_agent_plus_nondeterminism",
)

OnUnbalanced = Literal["drop", "raise"]
Scale = Literal["probability", "logit"]

_BOOT_CHUNK = 256  # bootstrap draws evaluated per vectorised batch (bounds memory use)

_EPS = 1e-12  # tolerance for floating-point sums of effect sizes

DEFAULT_MIN_TASK_FRACTION = 0.9
"""Default share of tasks the automatic repeat count must keep (see :func:`_target_reps`)."""


class UnbalancedDesignError(ValueError):
    """Raised when the design is unbalanced and ``on_unbalanced='raise'``."""


class UnbalancedDesignWarning(UserWarning):
    """Emitted when incomplete tasks are dropped or extra repeats are truncated."""


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statistic:
    """A point estimate with a percentile bootstrap confidence interval."""

    estimate: float
    ci_low: float
    ci_high: float


@dataclass(frozen=True)
class ComponentEstimate:
    """One variance component.

    Attributes:
        name: Component name (e.g. ``"task"``).
        raw: Unbiased method-of-moments estimate; may be negative.
        truncated: ``max(raw, 0)``.
        share: ``truncated / sum(truncated over all components)`` (NaN if that sum is 0
            or the component is not identifiable). Biased UPWARD for small components
            (see the module docstring); show it only when :attr:`ci_excludes_zero`.
        ci_low, ci_high: Percentile bootstrap interval of ``raw``.
        share_ci_low, share_ci_high: Percentile bootstrap interval of ``share``.
        frac_boot_nonpositive: Fraction of bootstrap draws with ``raw <= 0``. A large
            value means the data cannot distinguish the component from zero.
        raw_share: Untruncated ratio ``raw / sum(raw)`` (see :func:`untruncated_shares`);
            less biased than ``share`` but may be negative or above 1.
    """

    name: str
    raw: float
    truncated: float
    share: float
    ci_low: float
    ci_high: float
    share_ci_low: float
    share_ci_high: float
    frac_boot_nonpositive: float
    raw_share: float = math.nan

    @property
    def ci_excludes_zero(self) -> bool:
        """True iff the raw component's bootstrap interval lies strictly above 0."""
        return bool(self.ci_low > 0)


@dataclass(frozen=True)
class BootstrapResult:
    """Output of :func:`cluster_bootstrap`.

    Attributes:
        estimate: Statistic on the original data, shape ``(k,)``.
        draws: Statistic on each bootstrap sample, shape ``(n_boot, k)``.
        ci_low, ci_high: Percentile interval bounds per statistic, shape ``(k,)``.
        frac_nonpositive: Fraction of draws ``<= 0`` per statistic, shape ``(k,)``.
    """

    estimate: np.ndarray
    draws: np.ndarray
    ci_low: np.ndarray
    ci_high: np.ndarray
    frac_nonpositive: np.ndarray


@dataclass(frozen=True)
class BalancedArray:
    """A balanced outcome array built from a long table.

    Attributes:
        y: Float array of 0/1 outcomes. Crossed: ``(T, S, R)``; nested: ``(T, U, A, D)``.
        task_ids: Task id for each index of axis 0.
        levels: Level labels for each factor axis (simulators; or user and agent seeds).
        dropped_task_ids: Tasks dropped because at least one cell was incomplete.
        n_truncated_cells: Cells that had more repeats than the target and were cut.
        n_tasks_input: Tasks present in the input table (kept + dropped).
        kept_tasks_success_rate: Success rate over ALL input runs of the kept tasks
            (before truncation), for a like-for-like comparison with dropped tasks.
        dropped_tasks_success_rate: Success rate over all input runs of the dropped
            tasks (NaN when nothing was dropped). A gap to the kept rate signals that
            dropping is missing-not-at-random and every estimate is biased.
    """

    y: np.ndarray
    task_ids: tuple[Hashable, ...]
    levels: tuple[tuple[Hashable, ...], ...]
    dropped_task_ids: tuple[Hashable, ...] = ()
    n_truncated_cells: int = 0
    n_tasks_input: int = 0
    kept_tasks_success_rate: float = math.nan
    dropped_tasks_success_rate: float = math.nan

    @property
    def kept_task_fraction(self) -> float:
        """Share of input tasks that survived balancing."""
        return len(self.task_ids) / self.n_tasks_input if self.n_tasks_input else math.nan


@dataclass(frozen=True)
class VarianceDecomposition:
    """Result of :func:`decompose_variance` (Design A: tasks x simulators x repeats).

    Attributes:
        n_tasks, n_simulators, n_reps: Balanced design size actually analysed.
        task_ids, simulators: Labels in array order.
        dropped_task_ids: Tasks removed because a cell was incomplete.
        p_bar: Overall success rate (the scale on which every component lives).
        components: Variance components keyed by name (see :data:`CROSSED_COMPONENTS`).
            With one simulator, ``simulator`` and ``task_x_simulator`` are NaN and
            ``task`` absorbs the task x simulator interaction of that simulator.
        flip_rate: Fraction of (task, simulator) cells whose repeats are not all equal.
            Its expectation depends on ``n_reps``: ``1 - p^R - (1 - p)^R`` per cell.
        flip_rate_by_task: Per-task flip rate (mean over that task's cells).
        flip_rate_mixed_tasks: Flip rate of the cells ``(t, s)`` whose task is mixed
            (both a success and a failure) under the OTHER simulators (see
            :func:`flip_rate_mixed_elsewhere`). Selecting on the other simulators keeps
            the selection independent of the measured cell; selecting on the cell's own
            outcomes would be tautological (with one simulator it was always 1.0).
            NaN with one simulator. It is still conditional on observed mixing, so do
            NOT compare it with issue #540's flip rate; compare ``flip_rate`` instead.
        within_cell_disagreement: Probability that two repeats of the same cell
            disagree; unbiased for ``2 E[p(1-p)]`` and equal to ``2 * residual``.
        between_simulator_disagreement: Probability that two runs of the same task
            with different simulators disagree (NaN with one simulator).
        excess_between_disagreement: ``between - within``; its expectation is
            ``2 (simulator + task_x_simulator)``: extra disagreement caused purely by
            switching simulators.
        simulator_means: Success rate per simulator (treat simulators as fixed).
        n_boot, seed, alpha: Bootstrap settings, stored for reproducibility.
        n_tasks_input, kept_tasks_success_rate, dropped_tasks_success_rate: Balancing
            diagnostics copied from :class:`BalancedArray`.
    """

    n_tasks: int
    n_simulators: int
    n_reps: int
    task_ids: tuple[Hashable, ...]
    simulators: tuple[Hashable, ...]
    dropped_task_ids: tuple[Hashable, ...]
    p_bar: Statistic
    components: dict[str, ComponentEstimate]
    flip_rate: Statistic
    flip_rate_by_task: pd.Series
    flip_rate_mixed_tasks: float
    within_cell_disagreement: Statistic
    between_simulator_disagreement: Statistic
    excess_between_disagreement: Statistic
    simulator_means: pd.Series
    n_boot: int
    seed: int
    alpha: float
    n_tasks_input: int = 0
    kept_tasks_success_rate: float = math.nan
    dropped_tasks_success_rate: float = math.nan

    def to_frame(self) -> pd.DataFrame:
        """Components as a tidy table (one row per component)."""
        return _components_frame(self.components)


@dataclass(frozen=True)
class NestedDecomposition:
    """Result of :func:`decompose_nested` (Design B: user seeds x agent seeds per task).

    Attributes:
        n_tasks, n_user_seeds, n_agent_seeds, n_duplicates: Balanced design size.
        task_ids: Task labels in array order.
        dropped_task_ids: Tasks removed because a cell was incomplete.
        p_bar: Overall success rate.
        components: Components keyed by name. Names are :data:`NESTED_COMPONENTS`, or
            :data:`NESTED_COMPONENTS_NO_DUPLICATES` when ``n_duplicates == 1``.
            ``share`` is the share of TOTAL variance (task included).
        noise_shares: Shares of WITHIN-TASK noise (every component except ``task``),
            truncated as in :func:`truncated_shares`. ``noise_shares["user"]`` is the
            headline "user-noise share". BIASED UPWARD for small components (about 3x
            for a true share near 0.01 at T=40, U=A=3; see the module docstring):
            report it next to the raw component, its CI and ``frac_boot_nonpositive``,
            and only when the raw component's CI excludes 0.
        raw_noise_shares: Untruncated within-task ratios ``raw_k / sum raw`` (point
            estimates; approximately unbiased, may fall outside [0, 1]).
        n_boot, seed, alpha: Bootstrap settings.
        n_tasks_input, kept_tasks_success_rate, dropped_tasks_success_rate: Balancing
            diagnostics copied from :class:`BalancedArray`.
    """

    n_tasks: int
    n_user_seeds: int
    n_agent_seeds: int
    n_duplicates: int
    task_ids: tuple[Hashable, ...]
    dropped_task_ids: tuple[Hashable, ...]
    p_bar: Statistic
    components: dict[str, ComponentEstimate]
    noise_shares: dict[str, Statistic]
    n_boot: int
    seed: int
    alpha: float
    raw_noise_shares: dict[str, float] = field(default_factory=dict)
    n_tasks_input: int = 0
    kept_tasks_success_rate: float = math.nan
    dropped_tasks_success_rate: float = math.nan

    def to_frame(self) -> pd.DataFrame:
        """Components as a tidy table (one row per component)."""
        return _components_frame(self.components)


@dataclass(frozen=True)
class NoiseFloor:
    """The smallest pass^1 difference worth believing between two identical runs.

    Two runs with identical configuration on the same ``n_eval_tasks`` tasks
    (``trials_per_run`` trials each) differ in pass^1 by ``D``, a mean of independent
    zero-mean per-task differences with ``Var(D) = 2 * residual / (N * k)``.
    The normal approximation gives ``E|D| = sd * sqrt(2/pi)`` and the central
    ``level`` band ``|D| <= z * sd``.

    Attributes:
        residual: Within-cell variance ``E[p(1-p)]`` with bootstrap CI.
        sd_diff: Standard deviation of ``D``.
        expected_abs_diff: ``E|D|``: the typical gap between two identical runs.
        band: ``z_{(1+level)/2} * sd``: a fraction ``level`` of identical-run gaps
            are smaller than this. Differences below it are not evidence of anything.
        n_eval_tasks, trials_per_run, level: Settings used.
        n_boot, seed, alpha: Bootstrap settings.
    """

    residual: Statistic
    sd_diff: Statistic
    expected_abs_diff: Statistic
    band: Statistic
    n_eval_tasks: int
    trials_per_run: int
    level: float
    n_boot: int
    seed: int
    alpha: float


@dataclass(frozen=True)
class SyntheticDataset:
    """Synthetic outcomes with known variance components.

    Attributes:
        frame: Long table with the columns expected by :func:`decompose_variance`
            (or :func:`decompose_nested`) and a boolean ``success`` column.
        y: The same outcomes as a balanced array.
        p: True success probability of each cell.
        truth: True components on the probability scale, keyed like the estimator.
    """

    frame: pd.DataFrame
    y: np.ndarray
    p: np.ndarray
    truth: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Long table -> balanced array
# ---------------------------------------------------------------------------


def _sorted_labels(values: Sequence[Hashable]) -> list[Hashable]:
    """Sort labels naturally ('2' < '10' for numeric strings); fall back to str order."""
    unique = list(dict.fromkeys(values))
    numeric = pd.to_numeric(pd.Series(unique, dtype=object), errors="coerce")
    if not numeric.isna().any():
        return [u for _, u in sorted(zip(numeric.tolist(), unique, strict=True))]
    try:
        return sorted(unique)  # type: ignore[type-var]
    except TypeError:
        return sorted(unique, key=str)


def _target_reps(
    counts: pd.Series, n_reps: int | None, min_reps: int, min_task_fraction: float
) -> int:
    """Target repeats per cell.

    ``n_reps`` if given. Otherwise the LARGEST ``R >= min_reps`` such that at least
    ``min_task_fraction`` of the tasks have every cell with ``>= R`` repeats; if no such
    ``R`` exists, ``min_reps`` (which keeps the most tasks). If no task reaches
    ``min_reps`` at all, the best available count is returned so the caller can raise.

    Why not the most common cell count: a task is kept only if ALL its cells are full,
    so with per-run infrastructure-error probability ``q`` and ``S R`` runs per task the
    keep probability is ``(1 - q)^(S R)``. When errors are more common on hard tasks,
    the kept tasks are mostly easy ones (a Monte Carlo check with 5-20% errors kept
    13 of 40 tasks and inflated ``p_bar`` by +0.09). Cutting repeats instead is
    unrelated to outcomes, so it costs precision, not bias.

    Args:
        counts: Repeats per cell, indexed by (task, *factors), missing cells as 0.
        n_reps: Explicit target, or None for the automatic choice.
        min_reps: Smallest acceptable target.
        min_task_fraction: Share of tasks the automatic target must keep, in (0, 1].
    """
    if n_reps is not None:
        return int(n_reps)
    if not 0 < min_task_fraction <= 1:
        raise ValueError(f"min_task_fraction must be in (0, 1], got {min_task_fraction}")
    task_min = counts.groupby(level=0, sort=False).min()
    best = int(task_min.max())
    if best < min_reps:
        return best
    n_tasks = len(task_min)
    for reps in range(best, min_reps - 1, -1):
        if (task_min >= reps).sum() >= min_task_fraction * n_tasks - _EPS:
            return reps
    return min_reps


def _build_balanced(
    df: pd.DataFrame,
    *,
    task_col: str,
    factor_cols: Sequence[str],
    rep_col: str,
    outcome_col: str,
    n_reps: int | None,
    on_unbalanced: OnUnbalanced,
    min_reps: int,
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> BalancedArray:
    """Turn a long table into a balanced ``(T, *factor_levels, R)`` array.

    ``R`` comes from :func:`_target_reps`. Cells with more than ``R`` repeats keep the
    first ``R`` by ``rep_col`` order. Tasks with any cell holding fewer than ``R``
    repeats (including missing cells) are dropped whole. Both actions emit
    :class:`UnbalancedDesignWarning` (with the kept share of tasks and the success rate
    of kept vs dropped tasks), or raise :class:`UnbalancedDesignError` when
    ``on_unbalanced == 'raise'``.
    """
    if on_unbalanced not in ("drop", "raise"):
        raise ValueError(f"on_unbalanced must be 'drop' or 'raise', got {on_unbalanced!r}")
    cols = [task_col, *factor_cols, rep_col, outcome_col]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"missing columns {missing}; available: {list(df.columns)}")
    if df.empty:
        raise ValueError("empty input table")

    data = df[cols].copy()
    if data[outcome_col].isna().any():
        raise ValueError(
            f"{outcome_col!r} has missing values; remove infrastructure errors before "
            "calling (they are missing data, not failures)"
        )
    outcome = data[outcome_col].astype(float).to_numpy()
    if not np.isin(outcome, (0.0, 1.0)).all():
        raise ValueError(f"{outcome_col!r} must be binary (bool or 0/1)")
    data["_y"] = outcome

    keys = [task_col, *factor_cols]
    if data.duplicated([*keys, rep_col]).any():
        raise ValueError(f"duplicate rows for the same ({', '.join(keys)}, {rep_col})")

    data = data.sort_values([*keys, rep_col], kind="stable")
    data["_pos"] = data.groupby(keys, sort=False).cumcount()

    tasks = _sorted_labels(data[task_col].tolist())
    levels = [_sorted_labels(data[c].tolist()) for c in factor_cols]
    grid = pd.MultiIndex.from_product([tasks, *levels], names=keys)
    counts = data.groupby(keys).size().reindex(grid, fill_value=0)

    reps = _target_reps(counts, n_reps, min_reps, min_task_fraction)
    if reps < min_reps:
        raise ValueError(f"need at least {min_reps} repeat(s) per cell, got {reps}")

    n_truncated = int((counts > reps).sum())
    short = counts[counts < reps]
    bad_tasks = _sorted_labels(short.index.get_level_values(0).tolist()) if len(short) else []
    is_bad = data[task_col].isin(set(bad_tasks))
    kept_rate = float(data.loc[~is_bad, "_y"].mean()) if (~is_bad).any() else math.nan
    dropped_rate = float(data.loc[is_bad, "_y"].mean()) if is_bad.any() else math.nan
    if n_truncated or bad_tasks:
        n_kept = len(tasks) - len(bad_tasks)
        msg = (
            f"unbalanced design (target {reps} repeats per cell): "
            f"{len(bad_tasks)} task(s) with incomplete cells {bad_tasks[:10]}"
            f"{'...' if len(bad_tasks) > 10 else ''}, "
            f"{n_truncated} cell(s) with extra repeats; kept {n_kept}/{len(tasks)} tasks "
            f"({n_kept / len(tasks):.0%})"
        )
        if bad_tasks:
            msg += (
                f"; success rate over all input runs: kept tasks {kept_rate:.3f}, "
                f"dropped tasks {dropped_rate:.3f} (a gap means dropping is "
                "missing-not-at-random and biases every estimate; re-run the "
                "infrastructure errors instead)"
            )
        if on_unbalanced == "raise":
            raise UnbalancedDesignError(msg)
        warnings.warn(
            msg + "; dropping incomplete tasks whole and keeping the first repeats "
            f"(by {rep_col!r}) of over-full cells",
            UnbalancedDesignWarning,
            stacklevel=3,
        )

    bad = set(bad_tasks)
    kept_tasks = [t for t in tasks if t not in bad]
    if len(kept_tasks) < 2:
        raise ValueError(f"need at least 2 complete tasks, got {len(kept_tasks)}")

    keep = data[(data["_pos"] < reps) & ~data[task_col].isin(bad)]
    shape = (len(kept_tasks), *(len(lv) for lv in levels), reps)
    y = np.full(shape, np.nan)
    index = [pd.Categorical(keep[task_col], categories=kept_tasks).codes]
    index += [
        pd.Categorical(keep[c], categories=lv).codes
        for c, lv in zip(factor_cols, levels, strict=True)
    ]
    index.append(keep["_pos"].to_numpy())
    y[tuple(index)] = keep["_y"].to_numpy()
    assert not np.isnan(y).any(), "internal error: balanced array has holes"
    return BalancedArray(
        y=y,
        task_ids=tuple(kept_tasks),
        levels=tuple(tuple(lv) for lv in levels),
        dropped_task_ids=tuple(bad_tasks),
        n_truncated_cells=n_truncated,
        n_tasks_input=len(tasks),
        kept_tasks_success_rate=kept_rate,
        dropped_tasks_success_rate=dropped_rate,
    )


def to_crossed_array(
    df: pd.DataFrame,
    *,
    task_col: str = "task_id",
    sim_col: str | None = "simulator",
    rep_col: str = "trial",
    outcome_col: str = "success",
    n_reps: int | None = None,
    on_unbalanced: OnUnbalanced = "drop",
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> BalancedArray:
    """Build the ``(T, S, R)`` array for Design A from a long table.

    Args:
        df: One row per valid simulation (infrastructure errors already removed).
        task_col, sim_col, rep_col, outcome_col: Column names. ``sim_col=None`` means
            a single simulator (``S = 1``).
        n_reps: Target repeats per cell; default: the largest count that keeps at
            least ``min_task_fraction`` of the tasks (see :func:`_target_reps`).
        on_unbalanced: ``"drop"`` (warn, drop incomplete tasks whole, truncate extra
            repeats) or ``"raise"``.
        min_task_fraction: Used only when ``n_reps`` is None.

    Raises:
        ValueError: missing outcomes, non-binary outcomes, duplicate repeats, fewer
            than 2 repeats per cell, or fewer than 2 complete tasks.
    """
    if sim_col is None:
        df = df.assign(_simulator="all")
        sim_col = "_simulator"
    return _build_balanced(
        df,
        task_col=task_col,
        factor_cols=[sim_col],
        rep_col=rep_col,
        outcome_col=outcome_col,
        n_reps=n_reps,
        on_unbalanced=on_unbalanced,
        min_reps=2,
        min_task_fraction=min_task_fraction,
    )


def to_nested_array(
    df: pd.DataFrame,
    *,
    task_col: str = "task_id",
    user_col: str = "user_seed",
    agent_col: str = "agent_seed",
    rep_col: str = "trial",
    outcome_col: str = "success",
    n_reps: int | None = None,
    on_unbalanced: OnUnbalanced = "drop",
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> BalancedArray:
    """Build the ``(T, U, A, D)`` array for Design B from a long table.

    Seed labels only align runs WITHIN a task: the model treats user and agent seeds
    as nested in task, so seed ``7`` in task 1 and seed ``7`` in task 2 are unrelated
    draws. Every task must use the same set of labels (relabel to ``0..U-1`` if not).
    ``D = 1`` (no duplicate runs) is allowed. The automatic ``D`` (``n_reps=None``)
    can fall to 1, which merges ``user_x_agent`` and ``nondeterminism``; pass
    ``n_reps=2`` to keep them separate at the cost of dropping tasks.
    """
    return _build_balanced(
        df,
        task_col=task_col,
        factor_cols=[user_col, agent_col],
        rep_col=rep_col,
        outcome_col=outcome_col,
        n_reps=n_reps,
        on_unbalanced=on_unbalanced,
        min_reps=1,
        min_task_fraction=min_task_fraction,
    )


# ---------------------------------------------------------------------------
# Estimators on arrays (all accept leading batch dimensions)
# ---------------------------------------------------------------------------


def crossed_mean_squares(y: np.ndarray) -> dict[str, np.ndarray]:
    """Mean squares of the balanced two-way random model with interaction.

    Args:
        y: Outcomes with shape ``(..., T, S, R)``; leading axes are batch axes.

    Returns:
        ``{"MS_T", "MS_S", "MS_TS", "MS_E"}`` with expectations::

            E[MS_T]  = res + R txs + S R task
            E[MS_S]  = res + R txs + T R sim
            E[MS_TS] = res + R txs
            E[MS_E]  = res

        ``MS_S`` and ``MS_TS`` are NaN when ``S == 1``.
    """
    y = np.asarray(y, dtype=float)
    n_t, n_s, n_r = y.shape[-3:]
    if n_t < 2 or n_r < 2:
        raise ValueError(f"need T >= 2 and R >= 2, got T={n_t}, R={n_r}")
    g = y.mean(axis=(-3, -2, -1))
    yt = y.mean(axis=(-2, -1))
    yc = y.mean(axis=-1)
    ms_t = n_s * n_r * ((yt - g[..., None]) ** 2).sum(axis=-1) / (n_t - 1)
    ms_e = ((y - yc[..., None]) ** 2).sum(axis=(-3, -2, -1)) / (n_t * n_s * (n_r - 1))
    if n_s < 2:
        nan = np.full_like(g, np.nan)
        return {"MS_T": ms_t, "MS_S": nan, "MS_TS": nan, "MS_E": ms_e}
    ys = y.mean(axis=(-3, -1))
    ms_s = n_t * n_r * ((ys - g[..., None]) ** 2).sum(axis=-1) / (n_s - 1)
    inter = yc - yt[..., :, None] - ys[..., None, :] + g[..., None, None]
    ms_ts = n_r * (inter**2).sum(axis=(-2, -1)) / ((n_t - 1) * (n_s - 1))
    return {"MS_T": ms_t, "MS_S": ms_s, "MS_TS": ms_ts, "MS_E": ms_e}


def crossed_components(y: np.ndarray) -> np.ndarray:
    """Raw (untruncated) Design A components, ordered as :data:`CROSSED_COMPONENTS`.

    ``task = (MS_T - MS_TS)/(S R)``, ``simulator = (MS_S - MS_TS)/(T R)``,
    ``task_x_simulator = (MS_TS - MS_E)/R``, ``residual = MS_E``.

    With ``S == 1`` the simulator and interaction components are not identifiable:
    they are NaN and ``task = (MS_T - MS_E)/R`` (which then includes the interaction).

    Args:
        y: Outcomes ``(..., T, S, R)``.

    Returns:
        Array ``(..., 4)``.
    """
    y = np.asarray(y, dtype=float)
    n_t, n_s, n_r = y.shape[-3:]
    ms = crossed_mean_squares(y)
    if n_s < 2:
        task = (ms["MS_T"] - ms["MS_E"]) / n_r
        nan = np.full_like(task, np.nan)
        return np.stack([task, nan, nan, ms["MS_E"]], axis=-1)
    task = (ms["MS_T"] - ms["MS_TS"]) / (n_s * n_r)
    sim = (ms["MS_S"] - ms["MS_TS"]) / (n_t * n_r)
    txs = (ms["MS_TS"] - ms["MS_E"]) / n_r
    return np.stack([task, sim, txs, ms["MS_E"]], axis=-1)


def nested_components(y: np.ndarray) -> np.ndarray:
    """Raw Design B components from a ``(..., T, U, A, D)`` array.

    Mean squares are pooled within task::

        E[MS_T]  = nd + D ua + A D user + U D agent + U A D task
        E[MS_U]  = nd + D ua + A D user
        E[MS_A]  = nd + D ua + U D agent
        E[MS_UA] = nd + D ua
        E[MS_E]  = nd

    Returns:
        ``(..., 5)`` ordered as :data:`NESTED_COMPONENTS` when ``D >= 2``; ``(..., 4)``
        ordered as :data:`NESTED_COMPONENTS_NO_DUPLICATES` when ``D == 1`` (then the
        last entry is ``MS_UA = ua + nd``).
    """
    y = np.asarray(y, dtype=float)
    n_t, n_u, n_a, n_d = y.shape[-4:]
    if n_t < 2 or n_u < 2 or n_a < 2:
        raise ValueError(f"need T, U, A >= 2, got T={n_t}, U={n_u}, A={n_a}")
    g = y.mean(axis=(-4, -3, -2, -1))
    yt = y.mean(axis=(-3, -2, -1))
    ytu = y.mean(axis=(-2, -1))
    yta = y.mean(axis=(-3, -1))
    ytua = y.mean(axis=-1)
    ms_t = n_u * n_a * n_d * ((yt - g[..., None]) ** 2).sum(axis=-1) / (n_t - 1)
    ms_u = n_a * n_d * ((ytu - yt[..., None]) ** 2).sum(axis=(-2, -1)) / (n_t * (n_u - 1))
    ms_a = n_u * n_d * ((yta - yt[..., None]) ** 2).sum(axis=(-2, -1)) / (n_t * (n_a - 1))
    inter = ytua - ytu[..., :, :, None] - yta[..., :, None, :] + yt[..., None, None]
    ms_ua = n_d * (inter**2).sum(axis=(-3, -2, -1)) / (n_t * (n_u - 1) * (n_a - 1))
    task = (ms_t - ms_u - ms_a + ms_ua) / (n_u * n_a * n_d)
    user = (ms_u - ms_ua) / (n_a * n_d)
    agent = (ms_a - ms_ua) / (n_u * n_d)
    if n_d < 2:
        return np.stack([task, user, agent, ms_ua], axis=-1)
    ms_e = ((y - ytua[..., None]) ** 2).sum(axis=(-4, -3, -2, -1)) / (n_t * n_u * n_a * (n_d - 1))
    ua = (ms_ua - ms_e) / n_d
    return np.stack([task, user, agent, ua, ms_e], axis=-1)


def truncated_shares(components: np.ndarray) -> np.ndarray:
    """Shares ``max(c_k, 0) / sum_j max(c_j, 0)`` along the last axis.

    NaN components (not identifiable) stay NaN and are left out of the denominator.
    Shares are NaN when the denominator is 0 (e.g. all outcomes identical).

    Bias: although the raw components are unbiased, these shares are not. Truncating at
    0 turns zero-mean sampling noise of a small component into positive mass, so small
    shares are inflated (about 3-4x for a true share near 0.01 with 40 tasks). Use
    them only next to the raw components' CIs; see :func:`untruncated_shares`.
    """
    comps = np.asarray(components, dtype=float)
    pos = np.where(np.isnan(comps), np.nan, np.maximum(comps, 0.0))
    total = np.nansum(pos, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, pos / np.where(total > 0, total, 1.0), np.nan)


def untruncated_shares(components: np.ndarray) -> np.ndarray:
    """Ratios ``c_k / sum_j c_j`` of RAW components along the last axis.

    Ratios of unbiased estimators are approximately unbiased (to first order), unlike
    :func:`truncated_shares`, but a ratio can be negative or above 1 when some raw
    component is negative. NaN components are left out of the denominator and stay
    NaN; every ratio is NaN when the denominator is ``<= 0``.
    """
    comps = np.asarray(components, dtype=float)
    total = np.nansum(comps, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, comps / np.where(total > 0, total, 1.0), np.nan)


def flip_rate_mixed_elsewhere(y: np.ndarray) -> float:
    """Flip rate of cells whose task is mixed under the OTHER simulators.

    For each cell ``(t, s)`` of a ``(T, S, R)`` array, the task counts as mixed when the
    pooled repeats of the other ``S - 1`` simulators contain both a success and a
    failure. Cells are independent given their probabilities, so this selection does
    not look at the measured cell (no selection on the outcome). Returns NaN when
    ``S < 2`` or when no cell qualifies.
    """
    y = np.asarray(y, dtype=float)
    n_s, n_r = y.shape[-2], y.shape[-1]
    if n_s < 2:
        return math.nan
    c = y.sum(axis=-1)
    flips = (c > 0) & (c < n_r)
    other = c.sum(axis=-1, keepdims=True) - c
    mixed_other = (other > 0) & (other < (n_s - 1) * n_r)
    return float(flips[mixed_other].mean()) if mixed_other.any() else math.nan


def cell_flip_rate(y: np.ndarray) -> np.ndarray:
    """Fraction of cells whose repeats are not all equal; ``y`` is ``(..., T, S, R)``.

    Expectation per cell is ``1 - p^R - (1-p)^R``, so it grows with ``R``: always report
    ``R`` next to it. Comparable to tau2-bench issue #540's "share of tasks flipping".
    """
    y = np.asarray(y, dtype=float)
    c = y.sum(axis=-1)
    flips = (c > 0) & (c < y.shape[-1])
    return flips.mean(axis=(-2, -1))


def within_cell_disagreement(y: np.ndarray) -> np.ndarray:
    """Probability that two different repeats of the same cell disagree.

    Per cell ``2 c (R - c) / (R (R - 1))`` (fraction of the ``R(R-1)/2`` pairs that
    differ), averaged over cells. Unbiased for ``2 E[p(1-p)]`` for any ``R >= 2`` and
    algebraically equal to ``2 * MS_E``.
    """
    y = np.asarray(y, dtype=float)
    n_r = y.shape[-1]
    c = y.sum(axis=-1)
    return (2.0 * c * (n_r - c) / (n_r * (n_r - 1))).mean(axis=(-2, -1))


def between_simulator_disagreement(y: np.ndarray) -> np.ndarray:
    """Probability that two runs of the same task with different simulators disagree.

    For task ``t``: the mean over ordered pairs ``s != s'`` of
    ``p_ts (1 - p_ts') + p_ts' (1 - p_ts)`` with ``p_ts = c_ts / R``; then averaged over
    tasks. Different cells are independent, so this is unbiased. NaN when ``S < 2``.
    ``E[between - within] = 2 (simulator + task_x_simulator)``.
    """
    y = np.asarray(y, dtype=float)
    n_s = y.shape[-2]
    p = y.mean(axis=-1)
    if n_s < 2:
        return np.full(p.shape[:-2], np.nan)
    total = p.sum(axis=-1)
    same = (p * (1.0 - p)).sum(axis=-1)
    per_task = 2.0 * (total * (n_s - total) - same) / (n_s * (n_s - 1))
    return per_task.mean(axis=-1)


# ---------------------------------------------------------------------------
# Cluster bootstrap over tasks
# ---------------------------------------------------------------------------


def task_bootstrap_indices(n_tasks: int, n_boot: int, seed: int) -> np.ndarray:
    """Draw ``(n_boot, n_tasks)`` task indices with replacement, once, from ``seed``."""
    if n_boot < 1:
        raise ValueError(f"n_boot must be >= 1, got {n_boot}")
    rng = np.random.default_rng(seed)
    return rng.integers(0, n_tasks, size=(n_boot, n_tasks))


def _percentile_ci(draws: np.ndarray, alpha: float) -> tuple[np.ndarray, np.ndarray]:
    """Column-wise percentile interval ignoring NaN draws (all-NaN column -> NaN)."""
    low = np.full(draws.shape[1], np.nan)
    high = np.full(draws.shape[1], np.nan)
    for j in range(draws.shape[1]):
        col = draws[:, j]
        col = col[~np.isnan(col)]
        if col.size:
            low[j], high[j] = np.quantile(col, [alpha / 2, 1 - alpha / 2])
    return low, high


def cluster_bootstrap(
    y: np.ndarray,
    stat: Callable[[np.ndarray], np.ndarray],
    *,
    n_boot: int = 2000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    indices: np.ndarray | None = None,
) -> BootstrapResult:
    """Percentile cluster bootstrap that resamples whole tasks (axis 0 of ``y``).

    Args:
        y: Balanced array with tasks on axis 0.
        stat: Vectorised statistic: maps ``(..., T, ...)`` to ``(..., k)``. It is called
            on batches of resampled arrays with a leading batch axis.
        n_boot: Number of bootstrap samples.
        seed: Seed for :func:`task_bootstrap_indices` (ignored if ``indices`` given).
        alpha: Two-sided level; the interval is ``[alpha/2, 1 - alpha/2]`` quantiles.
        indices: Precomputed ``(n_boot, T)`` indices; pass the SAME indices to two
            aligned arrays for a paired comparison.

    Returns:
        :class:`BootstrapResult`. Duplicated tasks in a draw count as distinct tasks.
    """
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    y = np.asarray(y, dtype=float)
    if indices is None:
        indices = task_bootstrap_indices(y.shape[0], n_boot, seed)
    estimate = np.atleast_1d(np.asarray(stat(y), dtype=float))
    draws = np.concatenate(
        [
            np.asarray(stat(y[indices[i : i + _BOOT_CHUNK]]), dtype=float).reshape(
                -1, estimate.size
            )
            for i in range(0, indices.shape[0], _BOOT_CHUNK)
        ],
        axis=0,
    )
    low, high = _percentile_ci(draws, alpha)
    return BootstrapResult(estimate, draws, low, high, _nan_safe_fraction(draws))


def _nan_safe_fraction(draws: np.ndarray) -> np.ndarray:
    """Fraction of non-NaN draws ``<= 0`` per column (NaN for all-NaN columns)."""
    out = np.full(draws.shape[1], np.nan)
    for j in range(draws.shape[1]):
        col = draws[:, j]
        col = col[~np.isnan(col)]
        if col.size:
            out[j] = float((col <= 0).mean())
    return out


# ---------------------------------------------------------------------------
# Statistic vectors used by the bootstrap
# ---------------------------------------------------------------------------

_CROSSED_EXTRA = (
    "flip_rate",
    "within_cell_disagreement",
    "between_simulator_disagreement",
    "excess_between_disagreement",
    "p_bar",
)


def _crossed_stat_names() -> tuple[str, ...]:
    return (
        *CROSSED_COMPONENTS,
        *(f"share_{c}" for c in CROSSED_COMPONENTS),
        *_CROSSED_EXTRA,
    )


def _crossed_stat_vector(y: np.ndarray) -> np.ndarray:
    """All Design A scalar statistics, stacked on the last axis (batch-aware)."""
    comps = crossed_components(y)
    shares = truncated_shares(comps)
    dw = within_cell_disagreement(y)
    db = between_simulator_disagreement(y)
    extras = np.stack([cell_flip_rate(y), dw, db, db - dw, y.mean(axis=(-3, -2, -1))], axis=-1)
    return np.concatenate([comps, shares, extras], axis=-1)


def _nested_stat_names(n_dup: int) -> tuple[str, ...]:
    names = NESTED_COMPONENTS if n_dup >= 2 else NESTED_COMPONENTS_NO_DUPLICATES
    return (
        *names,
        *(f"share_{c}" for c in names),
        *(f"noise_share_{c}" for c in names[1:]),
        "p_bar",
    )


def _nested_stat_vector(y: np.ndarray) -> np.ndarray:
    """All Design B scalar statistics, stacked on the last axis (batch-aware)."""
    comps = nested_components(y)
    shares = truncated_shares(comps)
    noise_shares = truncated_shares(comps[..., 1:])
    p_bar = y.mean(axis=(-4, -3, -2, -1))[..., None]
    return np.concatenate([comps, shares, noise_shares, p_bar], axis=-1)


def _component_estimates(
    names: Sequence[str], boot: BootstrapResult, stat_names: Sequence[str]
) -> dict[str, ComponentEstimate]:
    """Assemble :class:`ComponentEstimate` objects from a bootstrap over stat vectors."""
    pos = {n: i for i, n in enumerate(stat_names)}
    raw_shares = untruncated_shares(np.array([boot.estimate[pos[n]] for n in names]))
    out: dict[str, ComponentEstimate] = {}
    for k, name in enumerate(names):
        i, j = pos[name], pos[f"share_{name}"]
        raw = float(boot.estimate[i])
        out[name] = ComponentEstimate(
            name=name,
            raw=raw,
            truncated=max(raw, 0.0) if not math.isnan(raw) else math.nan,
            share=float(boot.estimate[j]),
            ci_low=float(boot.ci_low[i]),
            ci_high=float(boot.ci_high[i]),
            share_ci_low=float(boot.ci_low[j]),
            share_ci_high=float(boot.ci_high[j]),
            frac_boot_nonpositive=float(boot.frac_nonpositive[i]),
            raw_share=float(raw_shares[k]),
        )
    return out


def _statistic(boot: BootstrapResult, stat_names: Sequence[str], name: str) -> Statistic:
    i = list(stat_names).index(name)
    return Statistic(float(boot.estimate[i]), float(boot.ci_low[i]), float(boot.ci_high[i]))


def _components_frame(components: dict[str, ComponentEstimate]) -> pd.DataFrame:
    rows = [vars(c) for c in components.values()]
    return pd.DataFrame(rows).set_index("name")


# ---------------------------------------------------------------------------
# Public high-level API
# ---------------------------------------------------------------------------


def decompose_variance(
    df: pd.DataFrame,
    *,
    task_col: str = "task_id",
    sim_col: str | None = "simulator",
    rep_col: str = "trial",
    outcome_col: str = "success",
    n_reps: int | None = None,
    on_unbalanced: OnUnbalanced = "drop",
    n_boot: int = 2000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> VarianceDecomposition:
    """Design A decomposition: task / simulator / task x simulator / residual.

    Args:
        df: Long table, one row per VALID simulation, with columns ``task_id``,
            ``simulator``, ``trial`` and boolean ``success`` (names configurable).
            Infrastructure errors must already be removed.
        task_col, sim_col, rep_col, outcome_col: Column names; ``sim_col=None`` for a
            single simulator (then only task and residual are identifiable).
        n_reps: Target repeats per cell (default: the largest count that keeps at
            least ``min_task_fraction`` of the tasks).
        on_unbalanced: ``"drop"`` to warn and drop incomplete tasks whole, ``"raise"``
            to refuse unbalanced input.
        n_boot, seed, alpha: Task-cluster bootstrap settings (percentile intervals).
        min_task_fraction: See :func:`to_crossed_array`.

    Returns:
        :class:`VarianceDecomposition`.

    Assumptions:
        Binary outcomes; repeats within a cell exchangeable and independent; tasks a
        random sample from a larger pool (the CI is over tasks); simulators treated as
        fixed by the task bootstrap, so the ``simulator`` component's CI is unreliable
        unless there are about 10 or more simulators.
    """
    arr = to_crossed_array(
        df,
        task_col=task_col,
        sim_col=sim_col,
        rep_col=rep_col,
        outcome_col=outcome_col,
        n_reps=n_reps,
        on_unbalanced=on_unbalanced,
        min_task_fraction=min_task_fraction,
    )
    y = arr.y
    names = _crossed_stat_names()
    boot = cluster_bootstrap(y, _crossed_stat_vector, n_boot=n_boot, seed=seed, alpha=alpha)

    c = y.sum(axis=-1)
    cell_flip = (c > 0) & (c < y.shape[-1])
    task_ids = arr.task_ids
    flip_by_task = pd.Series(cell_flip.mean(axis=1), index=pd.Index(task_ids, name=task_col))
    flip_mixed = flip_rate_mixed_elsewhere(y)
    simulators = arr.levels[0]
    sim_means = pd.Series(
        y.mean(axis=(0, 2)), index=pd.Index(simulators, name=sim_col or "simulator")
    )
    return VarianceDecomposition(
        n_tasks=y.shape[0],
        n_simulators=y.shape[1],
        n_reps=y.shape[2],
        task_ids=task_ids,
        simulators=simulators,
        dropped_task_ids=arr.dropped_task_ids,
        p_bar=_statistic(boot, names, "p_bar"),
        components=_component_estimates(CROSSED_COMPONENTS, boot, names),
        flip_rate=_statistic(boot, names, "flip_rate"),
        flip_rate_by_task=flip_by_task,
        flip_rate_mixed_tasks=flip_mixed,
        within_cell_disagreement=_statistic(boot, names, "within_cell_disagreement"),
        between_simulator_disagreement=_statistic(boot, names, "between_simulator_disagreement"),
        excess_between_disagreement=_statistic(boot, names, "excess_between_disagreement"),
        simulator_means=sim_means,
        n_boot=n_boot,
        seed=seed,
        alpha=alpha,
        n_tasks_input=arr.n_tasks_input,
        kept_tasks_success_rate=arr.kept_tasks_success_rate,
        dropped_tasks_success_rate=arr.dropped_tasks_success_rate,
    )


def decompose_nested(
    df: pd.DataFrame,
    *,
    task_col: str = "task_id",
    user_col: str = "user_seed",
    agent_col: str = "agent_seed",
    rep_col: str = "trial",
    outcome_col: str = "success",
    n_reps: int | None = None,
    on_unbalanced: OnUnbalanced = "drop",
    n_boot: int = 2000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> NestedDecomposition:
    """Design B decomposition: task / user seed / agent seed / user x agent / nondeterminism.

    Requires that the agent and the user simulator receive DIFFERENT, separately
    controlled seeds (tau2 v1.0.1 passes one seed to both; a wrapper is needed) and a
    temperature > 0 on both sides, otherwise seeds have no effect.

    Validity check (pre-register it): if ``nondeterminism`` is comparable to ``user``
    or ``agent``, seeds do not control sampling and the user/agent attribution is not
    trustworthy.

    Args and assumptions are as in :func:`decompose_variance`; seed labels identify
    levels only within a task (see :func:`to_nested_array`).
    """
    arr = to_nested_array(
        df,
        task_col=task_col,
        user_col=user_col,
        agent_col=agent_col,
        rep_col=rep_col,
        outcome_col=outcome_col,
        n_reps=n_reps,
        on_unbalanced=on_unbalanced,
        min_task_fraction=min_task_fraction,
    )
    y = arr.y
    n_dup = y.shape[-1]
    names = _nested_stat_names(n_dup)
    comp_names = NESTED_COMPONENTS if n_dup >= 2 else NESTED_COMPONENTS_NO_DUPLICATES
    boot = cluster_bootstrap(y, _nested_stat_vector, n_boot=n_boot, seed=seed, alpha=alpha)
    return NestedDecomposition(
        n_tasks=y.shape[0],
        n_user_seeds=y.shape[1],
        n_agent_seeds=y.shape[2],
        n_duplicates=n_dup,
        task_ids=arr.task_ids,
        dropped_task_ids=arr.dropped_task_ids,
        p_bar=_statistic(boot, names, "p_bar"),
        components=_component_estimates(comp_names, boot, names),
        noise_shares={c: _statistic(boot, names, f"noise_share_{c}") for c in comp_names[1:]},
        n_boot=n_boot,
        seed=seed,
        alpha=alpha,
        raw_noise_shares=dict(
            zip(
                comp_names[1:],
                (float(x) for x in untruncated_shares(boot.estimate[1 : len(comp_names)])),
                strict=True,
            )
        ),
        n_tasks_input=arr.n_tasks_input,
        kept_tasks_success_rate=arr.kept_tasks_success_rate,
        dropped_tasks_success_rate=arr.dropped_tasks_success_rate,
    )


def noise_floor_from_residual(
    residual: float, n_eval_tasks: int, trials_per_run: int = 1, level: float = 0.95
) -> tuple[float, float, float]:
    """Map a within-cell variance to ``(sd_diff, expected_abs_diff, band)``.

    ``D`` = pass^1(run 1) - pass^1(run 2) over ``N`` tasks with ``k`` trials each:
    ``Var(D) = 2 * residual / (N k)`` (task difficulty cancels because both runs use
    the same tasks). Under a normal approximation ``E|D| = sd sqrt(2/pi)`` and a
    fraction ``level`` of gaps lie within ``z_{(1+level)/2} sd``. Negative residual
    estimates are truncated to 0.
    """
    if n_eval_tasks < 1 or trials_per_run < 1:
        raise ValueError("n_eval_tasks and trials_per_run must be >= 1")
    if not 0 < level < 1:
        raise ValueError(f"level must be in (0, 1), got {level}")
    sd = math.sqrt(2.0 * max(residual, 0.0) / (n_eval_tasks * trials_per_run))
    z = float(stats.norm.ppf(0.5 + level / 2))
    return sd, sd * math.sqrt(2.0 / math.pi), z * sd


def noise_floor(
    df: pd.DataFrame,
    *,
    n_eval_tasks: int | None = None,
    trials_per_run: int = 1,
    level: float = 0.95,
    task_col: str = "task_id",
    sim_col: str | None = "simulator",
    rep_col: str = "trial",
    outcome_col: str = "success",
    n_reps: int | None = None,
    on_unbalanced: OnUnbalanced = "drop",
    n_boot: int = 2000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    min_task_fraction: float = DEFAULT_MIN_TASK_FRACTION,
) -> NoiseFloor:
    """Expected |pass^1 difference| between two identical runs: the smallest gap worth believing.

    The within-cell variance ``E[p(1-p)]`` (pooled over simulators, ``MS_E``) is
    estimated from repeats and mapped through :func:`noise_floor_from_residual`.
    The bootstrap resamples tasks and maps each draw the same way.

    Args:
        df: Long table as in :func:`decompose_variance`; one simulator is fine.
        n_eval_tasks: Tasks per hypothetical run (default: number of tasks analysed).
        trials_per_run: Trials per task in each hypothetical run (pass^1 averaged).
        level: Coverage of the reported ``band``.

    Assumptions:
        Identical runs are independent draws of the same process (no shared cache or
        batching effects); per-task outcomes are independent across tasks; the normal
        approximation for ``D`` is adequate (it is crude for very few tasks or when
        almost all tasks are deterministic).
    """
    arr = to_crossed_array(
        df,
        task_col=task_col,
        sim_col=sim_col,
        rep_col=rep_col,
        outcome_col=outcome_col,
        n_reps=n_reps,
        on_unbalanced=on_unbalanced,
        min_task_fraction=min_task_fraction,
    )
    n_eval = arr.y.shape[0] if n_eval_tasks is None else int(n_eval_tasks)

    def residual_stat(y: np.ndarray) -> np.ndarray:
        return crossed_mean_squares(y)["MS_E"][..., None]

    boot = cluster_bootstrap(arr.y, residual_stat, n_boot=n_boot, seed=seed, alpha=alpha)
    res = (float(boot.estimate[0]), float(boot.ci_low[0]), float(boot.ci_high[0]))
    mapped = [noise_floor_from_residual(r, n_eval, trials_per_run, level) for r in res]
    return NoiseFloor(
        residual=Statistic(*res),
        sd_diff=Statistic(*(m[0] for m in mapped)),
        expected_abs_diff=Statistic(*(m[1] for m in mapped)),
        band=Statistic(*(m[2] for m in mapped)),
        n_eval_tasks=n_eval,
        trials_per_run=trials_per_run,
        level=level,
        n_boot=n_boot,
        seed=seed,
        alpha=alpha,
    )


def compare_decompositions(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    *,
    design: Literal["crossed", "nested"] = "crossed",
    labels: tuple[str, str] = ("a", "b"),
    n_boot: int = 2000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
    **array_kwargs: object,
) -> pd.DataFrame:
    """Paired comparison of two conditions (e.g. English vs Hindi) on the same tasks.

    Both tables are made balanced, restricted to their common task ids, and resampled
    with the SAME bootstrap task indices, so the interval of ``b - a`` accounts for the
    task pairing.

    Args:
        df_a, df_b: Long tables for the two conditions.
        design: ``"crossed"`` (Design A) or ``"nested"`` (Design B). Both conditions
            must have the same number of duplicates in the nested design.
        labels: Names used for the estimate columns.
        array_kwargs: Passed to :func:`to_crossed_array` / :func:`to_nested_array`.

    Returns:
        DataFrame indexed by statistic with columns ``estimate_<a>``, ``estimate_<b>``,
        ``difference`` (b - a), ``ci_low``, ``ci_high``, ``frac_boot_diff_nonpositive``.
    """
    if design == "crossed":
        build, stat = to_crossed_array, _crossed_stat_vector
    elif design == "nested":
        build, stat = to_nested_array, _nested_stat_vector
    else:
        raise ValueError(f"design must be 'crossed' or 'nested', got {design!r}")
    arr_a = build(df_a, **array_kwargs)  # type: ignore[arg-type]
    arr_b = build(df_b, **array_kwargs)  # type: ignore[arg-type]
    common = [t for t in arr_a.task_ids if t in set(arr_b.task_ids)]
    if len(common) < 2:
        raise ValueError(f"need at least 2 common tasks, got {len(common)}")
    if len(common) < max(len(arr_a.task_ids), len(arr_b.task_ids)):
        warnings.warn(
            f"comparing on {len(common)} common tasks "
            f"({len(arr_a.task_ids)} in {labels[0]}, {len(arr_b.task_ids)} in {labels[1]})",
            UnbalancedDesignWarning,
            stacklevel=2,
        )
    pos_a = {t: i for i, t in enumerate(arr_a.task_ids)}
    pos_b = {t: i for i, t in enumerate(arr_b.task_ids)}
    y_a = arr_a.y[[pos_a[t] for t in common]]
    y_b = arr_b.y[[pos_b[t] for t in common]]
    if design == "crossed":
        names = _crossed_stat_names()
        if y_a.shape[-1] != y_b.shape[-1]:
            warnings.warn(
                f"repeats per cell differ ({labels[0]}: {y_a.shape[-1]}, "
                f"{labels[1]}: {y_b.shape[-1]}); flip_rate depends on R, so its difference "
                "is not comparable (pass n_reps=... to fix R)",
                UnbalancedDesignWarning,
                stacklevel=2,
            )
    else:
        if y_a.shape[-1] != y_b.shape[-1] and min(y_a.shape[-1], y_b.shape[-1]) < 2:
            raise ValueError("nested comparison needs D >= 2 in both conditions or D == 1 in both")
        names = _nested_stat_names(y_a.shape[-1])

    idx = task_bootstrap_indices(len(common), n_boot, seed)
    boot_a = cluster_bootstrap(y_a, stat, indices=idx, alpha=alpha)
    boot_b = cluster_bootstrap(y_b, stat, indices=idx, alpha=alpha)
    diff_draws = boot_b.draws - boot_a.draws
    low, high = _percentile_ci(diff_draws, alpha)
    frac = _nan_safe_fraction(diff_draws)
    return pd.DataFrame(
        {
            f"estimate_{labels[0]}": boot_a.estimate,
            f"estimate_{labels[1]}": boot_b.estimate,
            "difference": boot_b.estimate - boot_a.estimate,
            "ci_low": low,
            "ci_high": high,
            "frac_boot_diff_nonpositive": frac,
        },
        index=pd.Index(names, name="statistic"),
    )


# ---------------------------------------------------------------------------
# Synthetic data with known components
# ---------------------------------------------------------------------------


def _rademacher(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    return rng.choice(np.array([-1.0, 1.0]), size=shape)


def _normal_expectation(nodes: int = 80) -> tuple[np.ndarray, np.ndarray]:
    """Gauss-Hermite nodes and weights for expectations under a standard normal."""
    x, w = np.polynomial.hermite_e.hermegauss(nodes)
    return x, w / math.sqrt(2.0 * math.pi)


def crossed_truth(
    mu: float, sd_task: float, sd_sim: float, sd_txs: float, scale: Scale = "probability"
) -> dict[str, float]:
    """True Design A components (probability scale) for :func:`generate_synthetic`.

    * ``scale="probability"``: ``p = mu + a + b + c`` with Rademacher effects of size
      ``sd``; components are ``sd_task^2, sd_sim^2, sd_txs^2`` and residual
      ``mu(1-mu) - (sum of the three)``.
    * ``scale="logit"``: ``p = expit(logit(mu) + a + b + c)`` with normal effects. The
      probability-scale components are the functional-ANOVA terms
      ``Var(E[p|a])``, ``Var(E[p|b])``, the rest of ``Var(p)``, and ``E[p(1-p)]``,
      computed by Gauss-Hermite quadrature. Note that the interaction is non-zero
      even with ``sd_txs = 0`` when both main effects are present: additivity on the
      logit scale is not additivity on the probability scale.
    """
    _check_effect_args(mu, sd_task, sd_sim, sd_txs, scale)
    if scale == "probability":
        explained = sd_task**2 + sd_sim**2 + sd_txs**2
        return {
            "task": sd_task**2,
            "simulator": sd_sim**2,
            "task_x_simulator": sd_txs**2,
            "residual": mu * (1 - mu) - explained,
        }
    x, w = _normal_expectation()
    m = special.logit(mu)
    sd_all = math.sqrt(sd_task**2 + sd_sim**2 + sd_txs**2)
    p_all = special.expit(m + sd_all * x)
    e_p = float(w @ p_all)
    e_p2 = float(w @ p_all**2)

    def main_effect_var(sd_main: float, sd_rest: float) -> float:
        if sd_main == 0:
            return 0.0
        grid = special.expit(m + sd_main * x[:, None] + sd_rest * x[None, :])
        cond = grid @ w  # E[p | main effect at node i]
        return float(w @ cond**2 - (w @ cond) ** 2)

    var_task = main_effect_var(sd_task, math.sqrt(sd_sim**2 + sd_txs**2))
    var_sim = main_effect_var(sd_sim, math.sqrt(sd_task**2 + sd_txs**2))
    var_p = e_p2 - e_p**2
    return {
        "task": var_task,
        "simulator": var_sim,
        "task_x_simulator": var_p - var_task - var_sim,
        "residual": e_p - e_p2,
    }


def _check_effect_args(mu: float, sd_task: float, sd_sim: float, sd_txs: float, scale: str) -> None:
    if scale not in ("probability", "logit"):
        raise ValueError(f"scale must be 'probability' or 'logit', got {scale!r}")
    if min(sd_task, sd_sim, sd_txs) < 0:
        raise ValueError("standard deviations must be >= 0")
    if not 0 < mu < 1:
        raise ValueError(f"mu must be in (0, 1), got {mu}")
    spread = sd_task + sd_sim + sd_txs
    if scale == "probability" and not (mu - spread >= -_EPS and mu + spread <= 1 + _EPS):
        # Clipping p would silently change the true components.
        raise ValueError(f"mu +/- (sd_task + sd_sim + sd_txs) = {mu} +/- {spread} leaves [0, 1]")


def _crossed_probabilities(
    rng: np.random.Generator,
    batch: tuple[int, ...],
    n_tasks: int,
    n_sims: int,
    mu: float,
    sd_task: float,
    sd_sim: float,
    sd_txs: float,
    scale: Scale,
) -> np.ndarray:
    """Cell probabilities ``(*batch, T, S)`` for the crossed synthetic model."""
    t_shape, s_shape, c_shape = (
        (*batch, n_tasks, 1),
        (*batch, 1, n_sims),
        (*batch, n_tasks, n_sims),
    )
    if scale == "probability":
        a = sd_task * _rademacher(rng, t_shape)
        b = sd_sim * _rademacher(rng, s_shape)
        c = sd_txs * _rademacher(rng, c_shape)
        return mu + a + b + c
    a = rng.normal(0.0, sd_task, t_shape)
    b = rng.normal(0.0, sd_sim, s_shape)
    c = rng.normal(0.0, sd_txs, c_shape)
    return special.expit(special.logit(mu) + a + b + c)


def simulate_crossed_arrays(
    n_datasets: int,
    n_tasks: int,
    n_sims: int,
    n_reps: int,
    sd_task: float,
    sd_sim: float,
    sd_txs: float,
    seed: int,
    *,
    mu: float = 0.5,
    scale: Scale = "probability",
) -> np.ndarray:
    """Many independent crossed datasets at once: array ``(n_datasets, T, S, R)``.

    Used for Monte Carlo checks of bias and coverage; see :func:`generate_synthetic`.
    """
    _check_effect_args(mu, sd_task, sd_sim, sd_txs, scale)
    rng = np.random.default_rng(seed)
    p = _crossed_probabilities(
        rng, (n_datasets,), n_tasks, n_sims, mu, sd_task, sd_sim, sd_txs, scale
    )
    return (rng.random((*p.shape, n_reps)) < p[..., None]).astype(float)


def generate_synthetic(
    n_tasks: int,
    n_sims: int,
    n_reps: int,
    sd_task: float,
    sd_sim: float,
    sd_txs: float,
    seed: int,
    *,
    mu: float = 0.5,
    scale: Scale = "probability",
) -> SyntheticDataset:
    """Simulate Design A data (tasks x simulators x repeats) with known components.

    Args:
        n_tasks, n_sims, n_reps: Design size.
        sd_task, sd_sim, sd_txs: Effect sizes. On the probability scale they are
            Rademacher magnitudes (so variances are exactly ``sd^2``); on the logit scale
            they are normal standard deviations around ``logit(mu)``.
        seed: Seed for ``numpy.random.default_rng``.
        mu: Baseline success probability.
        scale: ``"probability"`` (exact additive truth; requires ``mu +/- sum(sd)`` in
            ``[0, 1]``) or ``"logit"`` (realistic mean-variance coupling; truth from
            :func:`crossed_truth`).

    Returns:
        :class:`SyntheticDataset` whose ``frame`` has columns ``task_id`` (str),
        ``simulator`` (str), ``trial`` (int) and ``success`` (bool).
    """
    _check_effect_args(mu, sd_task, sd_sim, sd_txs, scale)
    rng = np.random.default_rng(seed)
    p = _crossed_probabilities(rng, (), n_tasks, n_sims, mu, sd_task, sd_sim, sd_txs, scale)
    y = (rng.random((n_tasks, n_sims, n_reps)) < p[..., None]).astype(float)
    t_idx, s_idx, r_idx = np.meshgrid(
        np.arange(n_tasks), np.arange(n_sims), np.arange(n_reps), indexing="ij"
    )
    frame = pd.DataFrame(
        {
            "task_id": t_idx.ravel().astype(str),
            "simulator": np.char.add("sim", s_idx.ravel().astype(str)),
            "trial": r_idx.ravel(),
            "success": y.ravel().astype(bool),
        }
    )
    return SyntheticDataset(frame, y, p, crossed_truth(mu, sd_task, sd_sim, sd_txs, scale))


def generate_synthetic_nested(
    n_tasks: int,
    n_user_seeds: int,
    n_agent_seeds: int,
    n_duplicates: int,
    sd_task: float,
    sd_user: float,
    sd_agent: float,
    sd_ua: float,
    seed: int,
    *,
    mu: float = 0.5,
) -> SyntheticDataset:
    """Simulate Design B data with Rademacher effects (probability scale).

    ``p_tua = mu + tau_t + u_tu + alpha_ta + x_tua``; truth: ``sd^2`` for each effect
    and ``nondeterminism = mu(1-mu) - sum(sd^2)``. The frame has columns ``task_id``,
    ``user_seed``, ``agent_seed``, ``trial`` and ``success``.
    """
    spread = sd_task + sd_user + sd_agent + sd_ua
    if min(sd_task, sd_user, sd_agent, sd_ua) < 0 or not 0 < mu < 1:
        raise ValueError("need sd >= 0 and 0 < mu < 1")
    if mu - spread < -_EPS or mu + spread > 1 + _EPS:
        raise ValueError(f"mu +/- {spread} leaves [0, 1]")
    rng = np.random.default_rng(seed)
    t, u, a, d = n_tasks, n_user_seeds, n_agent_seeds, n_duplicates
    p = (
        mu
        + sd_task * _rademacher(rng, (t, 1, 1))
        + sd_user * _rademacher(rng, (t, u, 1))
        + sd_agent * _rademacher(rng, (t, 1, a))
        + sd_ua * _rademacher(rng, (t, u, a))
    )
    y = (rng.random((t, u, a, d)) < p[..., None]).astype(float)
    ti, ui, ai, di = np.meshgrid(*(np.arange(n) for n in (t, u, a, d)), indexing="ij")
    frame = pd.DataFrame(
        {
            "task_id": ti.ravel().astype(str),
            "user_seed": ui.ravel(),
            "agent_seed": ai.ravel(),
            "trial": di.ravel(),
            "success": y.ravel().astype(bool),
        }
    )
    explained = sd_task**2 + sd_user**2 + sd_agent**2 + sd_ua**2
    truth = {
        "task": sd_task**2,
        "user": sd_user**2,
        "agent": sd_agent**2,
        "user_x_agent": sd_ua**2,
        "nondeterminism": mu * (1 - mu) - explained,
    }
    return SyntheticDataset(frame, y, p, truth)
