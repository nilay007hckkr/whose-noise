"""Tests for whose_noise.metrics with hand-computed expected values."""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from whose_noise.metrics import (
    cluster_bootstrap_ci,
    mcnemar_exact,
    paired_bootstrap_diff,
    pass_hat_k,
    pass_hat_k_curve,
    pass_hat_k_per_task,
    pass_hat_k_task,
    wilson_ci,
)

# Two tasks with 4 trials each: c=3 and c=2.
TWO_TASKS = {"t0": [1, 1, 1, 0], "t1": [True, False, True, False]}


# ---------------------------------------------------------------------------
# pass_hat_k_task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (4, 4, 2, 1.0),
        (4, 2, 2, 1 / 6),  # C(2,2)/C(4,2) = 1/6
        (4, 1, 2, 0.0),  # c < k
        (4, 3, 2, 0.5),  # C(3,2)/C(4,2) = 3/6
        (4, 4, 4, 1.0),
        (4, 3, 1, 0.75),  # pass^1 = c/n
        (4, 0, 1, 0.0),
        (5, 3, 3, 0.1),  # C(3,3)/C(5,3) = 1/10
    ],
)
def test_pass_hat_k_task_hand_values(n: int, c: int, k: int, expected: float) -> None:
    assert pass_hat_k_task(n, c, k) == pytest.approx(expected)


def test_pass_hat_k_task_c_less_than_k_is_zero() -> None:
    for c in range(3):
        assert pass_hat_k_task(8, c, 3) == 0.0


@pytest.mark.parametrize("p", [0.0, 0.2, 0.5, 0.9, 1.0])
@pytest.mark.parametrize("k", [1, 2, 3, 5])
def test_pass_hat_k_task_is_unbiased_for_p_to_the_k(p: float, k: int) -> None:
    """E_c[C(c,k)/C(n,k)] with c ~ Binomial(n, p) equals p**k exactly."""
    n = 6
    expectation = sum(stats.binom.pmf(c, n, p) * pass_hat_k_task(n, c, k) for c in range(n + 1))
    assert expectation == pytest.approx(p**k, abs=1e-12)


@pytest.mark.parametrize(
    ("n", "c", "k"),
    [(4, 2, 5), (4, 2, 0), (4, 2, -1), (4, 5, 2), (4, -1, 2), (0, 0, 1)],
)
def test_pass_hat_k_task_invalid_args_raise(n: int, c: int, k: int) -> None:
    with pytest.raises(ValueError):
        pass_hat_k_task(n, c, k)


# ---------------------------------------------------------------------------
# pass_hat_k / curve
# ---------------------------------------------------------------------------


def test_pass_hat_k_two_tasks_hand_value() -> None:
    assert pass_hat_k(TWO_TASKS, 2) == pytest.approx((0.5 + 1 / 6) / 2)  # = 1/3


def test_pass_hat_k_per_task_values() -> None:
    per_task = pass_hat_k_per_task(TWO_TASKS, 2)
    assert per_task == {"t0": pytest.approx(0.5), "t1": pytest.approx(1 / 6)}


def test_pass_hat_1_equals_mean_success_rate() -> None:
    rng = np.random.default_rng(123)
    outcomes = {f"t{i}": rng.integers(0, 2, size=5).tolist() for i in range(30)}
    pooled = np.mean([v for trials in outcomes.values() for v in trials])
    assert pass_hat_k(outcomes, 1) == pytest.approx(pooled)


def test_pass_hat_1_with_unequal_trials_is_mean_of_task_rates() -> None:
    outcomes = {"a": [1, 0], "b": [1, 1, 1, 0]}  # rates 0.5 and 0.75
    assert pass_hat_k(outcomes, 1) == pytest.approx(0.625)


def test_pass_hat_k_curve_hand_values_and_monotone() -> None:
    curve = pass_hat_k_curve(TWO_TASKS, [1, 2, 3, 4])
    assert list(curve) == [1, 2, 3, 4]
    assert curve[1] == pytest.approx(0.625)  # (3/4 + 2/4) / 2
    assert curve[2] == pytest.approx(1 / 3)
    assert curve[3] == pytest.approx(0.125)  # (C(3,3)/C(4,3) + 0) / 2 = (1/4)/2
    assert curve[4] == pytest.approx(0.0)
    values = list(curve.values())
    assert all(x >= y for x, y in zip(values, values[1:], strict=False))


def test_pass_hat_k_raises_when_task_has_too_few_trials() -> None:
    outcomes = {"ok": [1, 1, 1], "short": [1]}
    with pytest.raises(ValueError, match=r"pass\^2 needs at least 2 trials.*'short'\(n=1\)"):
        pass_hat_k(outcomes, 2)


def test_pass_hat_k_rejects_empty_nan_and_nonbinary() -> None:
    with pytest.raises(ValueError, match="empty"):
        pass_hat_k({}, 1)
    with pytest.raises(ValueError, match="NaN"):
        pass_hat_k({"t": [1.0, float("nan")]}, 1)
    with pytest.raises(ValueError, match="binary"):
        pass_hat_k({"t": [1, 2]}, 1)


# ---------------------------------------------------------------------------
# cluster_bootstrap_ci
# ---------------------------------------------------------------------------


def _synthetic_outcomes(seed: int, n_tasks: int = 40, n_trials: int = 4) -> dict[str, list[int]]:
    """Tasks with p_t ~ Uniform(0, 1), n_trials Bernoulli(p_t) each."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.0, 1.0, size=n_tasks)
    y = rng.random((n_tasks, n_trials)) < p[:, None]
    return {f"t{i}": y[i].astype(int).tolist() for i in range(n_tasks)}


def _pass2(o):
    return pass_hat_k(o, 2)


def test_cluster_bootstrap_ci_contains_point_and_is_deterministic() -> None:
    outcomes = _synthetic_outcomes(seed=1)
    point, lo, hi = cluster_bootstrap_ci(outcomes, _pass2, n_boot=500, seed=7)
    assert point == pytest.approx(pass_hat_k(outcomes, 2))
    assert lo <= point <= hi
    assert lo < hi
    assert cluster_bootstrap_ci(outcomes, _pass2, n_boot=500, seed=7) == (point, lo, hi)
    assert cluster_bootstrap_ci(outcomes, _pass2, n_boot=500, seed=8) != (point, lo, hi)


def test_cluster_bootstrap_ci_degenerate_when_all_tasks_identical() -> None:
    outcomes = {f"t{i}": [1, 1, 0, 0] for i in range(10)}
    point, lo, hi = cluster_bootstrap_ci(outcomes, _pass2, n_boot=200)
    assert point == lo == hi == pytest.approx(1 / 6)


def test_cluster_bootstrap_ci_wider_at_higher_confidence() -> None:
    outcomes = _synthetic_outcomes(seed=2)
    _, lo95, hi95 = cluster_bootstrap_ci(outcomes, _pass2, n_boot=1000, alpha=0.05)
    _, lo50, hi50 = cluster_bootstrap_ci(outcomes, _pass2, n_boot=1000, alpha=0.5)
    assert lo95 <= lo50 and hi50 <= hi95


def test_cluster_bootstrap_ci_invalid_args() -> None:
    with pytest.raises(ValueError):
        cluster_bootstrap_ci(TWO_TASKS, _pass2, n_boot=0)
    with pytest.raises(ValueError):
        cluster_bootstrap_ci(TWO_TASKS, _pass2, alpha=1.0)
    with pytest.raises(ValueError):
        cluster_bootstrap_ci({}, _pass2)


# ---------------------------------------------------------------------------
# paired_bootstrap_diff
# ---------------------------------------------------------------------------


def test_paired_bootstrap_diff_hand_value_and_intersection() -> None:
    a = {"t0": [1, 1, 1, 1], "t1": [1, 0, 0, 0], "only_a": [1, 1, 1, 1]}
    b = {"t0": [1, 1, 0, 0], "t1": [0, 0, 0, 0], "only_b": [0, 0, 0, 0]}
    diff, lo, hi = paired_bootstrap_diff(a, b, k=1, n_boot=500)
    # per-task differences: t0 1.0 - 0.5 = 0.5, t1 0.25 - 0 = 0.25 -> mean 0.375
    assert diff == pytest.approx(0.375)
    assert 0.25 <= lo <= diff <= hi <= 0.5


def test_paired_bootstrap_diff_identical_conditions_is_zero() -> None:
    outcomes = _synthetic_outcomes(seed=3)
    assert paired_bootstrap_diff(outcomes, outcomes, k=2) == (0.0, 0.0, 0.0)


def test_paired_bootstrap_matches_cluster_bootstrap_against_zero_baseline() -> None:
    """With B all-failures, diff = pass^k(A); same seed -> same resampling -> same CI."""
    a = _synthetic_outcomes(seed=4)
    b = {t: [0] * len(v) for t, v in a.items()}
    paired = paired_bootstrap_diff(a, b, k=2, n_boot=300, seed=11)
    cluster = cluster_bootstrap_ci(a, _pass2, n_boot=300, seed=11)
    assert paired == pytest.approx(cluster)


def test_paired_bootstrap_is_tighter_than_unpaired_when_conditions_correlate() -> None:
    """B = A with a few flips: paired CI should be much narrower than CI of pass^1(A)."""
    a = _synthetic_outcomes(seed=5)
    b = {t: list(v) for t, v in a.items()}
    for t in list(b)[:5]:
        b[t][0] = 1 - b[t][0]
    _, lo_d, hi_d = paired_bootstrap_diff(a, b, k=1, n_boot=1000)
    _, lo_a, hi_a = cluster_bootstrap_ci(a, lambda o: pass_hat_k(o, 1), n_boot=1000)
    assert (hi_d - lo_d) < 0.5 * (hi_a - lo_a)


def test_paired_bootstrap_ci_coverage_on_synthetic_ground_truth() -> None:
    """Percentile CI of pass^2 should cover the true E[p^2] = 1/3 about 95% of the time.

    p_t ~ Uniform(0,1) so the population pass^2 is E[p^2] = 1/3. Uses a zero
    baseline B so diff == pass^2(A). Loose bounds: percentile bootstrap with
    T=40 tasks is known to under-cover slightly.
    """
    true_value = 1 / 3
    n_rep = 300
    hits = 0
    for r in range(n_rep):
        a = _synthetic_outcomes(seed=1000 + r)
        b = {t: [0] * len(v) for t, v in a.items()}
        _, lo, hi = paired_bootstrap_diff(a, b, k=2, n_boot=400, seed=r)
        hits += lo <= true_value <= hi
    coverage = hits / n_rep
    assert 0.88 <= coverage <= 0.99, coverage


def test_paired_bootstrap_diff_errors() -> None:
    with pytest.raises(ValueError, match="share no task"):
        paired_bootstrap_diff({"a": [1]}, {"b": [1]})
    with pytest.raises(ValueError, match="needs at least 3"):
        paired_bootstrap_diff({"a": [1, 1]}, {"a": [1, 1]}, k=3)


# ---------------------------------------------------------------------------
# mcnemar_exact
# ---------------------------------------------------------------------------


def _table(n11: int, n10: int, n01: int, n00: int) -> tuple[dict[str, int], dict[str, int]]:
    """Build paired verdicts with the given 2x2 counts (first digit = A, second = B)."""
    pairs = [(1, 1)] * n11 + [(1, 0)] * n10 + [(0, 1)] * n01 + [(0, 0)] * n00
    a = {f"t{i}": x for i, (x, _) in enumerate(pairs)}
    b = {f"t{i}": y for i, (_, y) in enumerate(pairs)}
    return a, b


def test_mcnemar_exact_hand_table() -> None:
    a, b = _table(n11=2, n10=6, n01=1, n00=1)
    b01, b10, p = mcnemar_exact(a, b)
    assert (b01, b10) == (1, 6)
    # Two-sided exact: 2 * P(X <= 1 | n=7, p=.5) = 2 * (1 + 7) / 128 = 0.125
    assert p == pytest.approx(0.125)


def test_mcnemar_exact_symmetric_and_other_hand_value() -> None:
    a, b = _table(n11=2, n10=6, n01=1, n00=1)
    assert mcnemar_exact(b, a) == (6, 1, pytest.approx(0.125))
    a, b = _table(n11=3, n10=5, n01=0, n00=4)
    assert mcnemar_exact(a, b) == (0, 5, pytest.approx(2 / 32))  # 2 * 0.5**5


def test_mcnemar_exact_no_discordant_pairs() -> None:
    a, b = _table(n11=4, n10=0, n01=0, n00=3)
    assert mcnemar_exact(a, b) == (0, 0, 1.0)


def test_mcnemar_exact_uses_shared_tasks_and_bools() -> None:
    a = {"x": True, "y": False, "z": True}
    b = {"x": False, "y": False, "w": True}
    b01, b10, p = mcnemar_exact(a, b)
    assert (b01, b10) == (0, 1)
    assert p == pytest.approx(1.0)
    with pytest.raises(ValueError, match="share no task"):
        mcnemar_exact({"x": 1}, {"y": 1})


# ---------------------------------------------------------------------------
# wilson_ci
# ---------------------------------------------------------------------------


def test_wilson_ci_hand_values() -> None:
    z = stats.norm.ppf(0.975)
    # 0/10: lo = 0, hi = (z^2/n) / (1 + z^2/n)
    lo, hi = wilson_ci(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-12)
    assert hi == pytest.approx((z * z / 10) / (1 + z * z / 10))
    assert hi == pytest.approx(0.2775, abs=1e-4)
    # 5/10: symmetric about 0.5, published value (0.2366, 0.7634)
    lo, hi = wilson_ci(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-4), pytest.approx(0.7634, abs=1e-4))


def test_wilson_ci_properties() -> None:
    lo, hi = wilson_ci(10, 10)
    assert hi == pytest.approx(1.0) and lo < 1.0
    lo95, hi95 = wilson_ci(7, 20, alpha=0.05)
    lo80, hi80 = wilson_ci(7, 20, alpha=0.20)
    assert lo95 < lo80 < 7 / 20 < hi80 < hi95
    assert math.isclose(sum(wilson_ci(3, 12)), 2 - sum(wilson_ci(9, 12)))


@pytest.mark.parametrize(
    ("s", "n", "alpha"), [(1, 0, 0.05), (-1, 5, 0.05), (6, 5, 0.05), (1, 5, 0.0)]
)
def test_wilson_ci_invalid_args(s: int, n: int, alpha: float) -> None:
    with pytest.raises(ValueError):
        wilson_ci(s, n, alpha)
