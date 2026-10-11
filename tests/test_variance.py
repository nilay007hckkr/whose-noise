"""Tests for whose_noise.variance.

Expected values are either hand-computed (written out in comments) or come from
synthetic data whose true components are known by construction.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest

from whose_noise import variance as v

# Hand vector from the methodology spec (T=2 tasks, S=2 simulators, R=2 repeats):
#   y[0,0]=[1,1], y[0,1]=[1,0], y[1,0]=[0,0], y[1,1]=[0,1]
HAND = np.array([[[1, 1], [1, 0]], [[0, 0], [0, 1]]], dtype=float)


def crossed_frame(y: np.ndarray) -> pd.DataFrame:
    """Long table (task_id, simulator, trial, success) from a (T, S, R) array."""
    rows = [
        {"task_id": str(t), "simulator": f"sim{s}", "trial": r, "success": bool(y[t, s, r])}
        for t in range(y.shape[0])
        for s in range(y.shape[1])
        for r in range(y.shape[2])
    ]
    return pd.DataFrame(rows)


def nested_frame(y: np.ndarray) -> pd.DataFrame:
    """Long table (task_id, user_seed, agent_seed, trial, success) from (T, U, A, D)."""
    rows = [
        {
            "task_id": str(t),
            "user_seed": u,
            "agent_seed": a,
            "trial": d,
            "success": bool(y[t, u, a, d]),
        }
        for t in range(y.shape[0])
        for u in range(y.shape[1])
        for a in range(y.shape[2])
        for d in range(y.shape[3])
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Hand-computed vectors
# ---------------------------------------------------------------------------


def test_hand_vector_mean_squares_and_components() -> None:
    # g = .5, yt = [.75, .25], ys = [.5, .5], cell means [[1, .5], [0, .5]]
    # MS_T = S R sum (yt-g)^2/(T-1) = 4 * (.0625 + .0625) = .5
    # MS_S = 0 (both simulators have mean .5)
    # interaction residuals are +-.25 -> MS_TS = R * 4 * .0625 / 1 = .5
    # MS_E = (0 + .5 + 0 + .5) / (T S (R-1)) = 1/4
    ms = v.crossed_mean_squares(HAND)
    assert ms["MS_T"] == pytest.approx(0.5)
    assert ms["MS_S"] == pytest.approx(0.0)
    assert ms["MS_TS"] == pytest.approx(0.5)
    assert ms["MS_E"] == pytest.approx(0.25)
    # task = (.5-.5)/4 = 0, sim = (0-.5)/4 = -.125, txs = (.5-.25)/2 = .125, res = .25
    np.testing.assert_allclose(v.crossed_components(HAND), [0.0, -0.125, 0.125, 0.25])


def test_hand_vector_flip_and_disagreement() -> None:
    # Cells [1,1] and [0,0] are stable; [1,0] and [0,1] flip -> flip rate 2/4.
    assert v.cell_flip_rate(HAND) == pytest.approx(0.5)
    # Dw per cell 2c(R-c)/(R(R-1)): 0, 1, 0, 1 -> mean .5 = 2 * MS_E.
    assert v.within_cell_disagreement(HAND) == pytest.approx(0.5)
    assert v.within_cell_disagreement(HAND) == pytest.approx(
        2 * v.crossed_mean_squares(HAND)["MS_E"]
    )
    # Task 0: p = (1, .5): 1*.5 + .5*0 = .5. Task 1: p = (0, .5): 0*.5 + .5*1 = .5.
    assert v.between_simulator_disagreement(HAND) == pytest.approx(0.5)


def test_single_cell_disagreement_four_repeats() -> None:
    # Cell [1,1,0,0]: flips; 4 of the 6 pairs differ -> 2*2*2/12 = 2/3.
    cell = np.array([[[1, 1, 0, 0]]], dtype=float)
    assert v.cell_flip_rate(cell) == pytest.approx(1.0)
    assert v.within_cell_disagreement(cell) == pytest.approx(2 / 3)


def test_truncated_shares() -> None:
    # Positive parts [.1, 0, .1, .3] sum to .5.
    np.testing.assert_allclose(
        v.truncated_shares(np.array([0.1, -0.05, 0.1, 0.3])), [0.2, 0.0, 0.2, 0.6]
    )
    shares = v.truncated_shares(np.array([0.1, np.nan, np.nan, 0.3]))
    np.testing.assert_allclose(shares, [0.25, np.nan, np.nan, 0.75])
    assert np.isnan(v.truncated_shares(np.zeros(4))).all()


def test_untruncated_shares() -> None:
    # Raw [.1, -.05, .1, .3] sum to .45: ratios keep the negative entry.
    np.testing.assert_allclose(
        v.untruncated_shares(np.array([0.1, -0.05, 0.1, 0.3])),
        [0.1 / 0.45, -0.05 / 0.45, 0.1 / 0.45, 0.3 / 0.45],
    )
    np.testing.assert_allclose(
        v.untruncated_shares(np.array([0.1, np.nan, np.nan, 0.3])), [0.25, np.nan, np.nan, 0.75]
    )
    assert np.isnan(v.untruncated_shares(np.array([0.1, -0.2]))).all()


def test_truncated_share_of_a_zero_component_is_biased_upward() -> None:
    # True simulator component is exactly 0 (sd_sim = 0). Its raw estimate is unbiased,
    # so truncation can only push the mean share above 0; the untruncated ratio stays
    # near 0 (Monte Carlo, 2000 datasets of T=40, S=3, R=3; values pinned by the seed:
    # truncated mean .0025, untruncated mean -.0004).
    comps = v.crossed_components(v.simulate_crossed_arrays(2000, 40, 3, 3, 0.2, 0.0, 0.1, 3))
    assert np.nanmean(v.truncated_shares(comps)[:, 1]) > 0.002
    assert abs(np.nanmean(v.untruncated_shares(comps)[:, 1])) < 0.001


def test_ci_excludes_zero() -> None:
    def comp(ci_low: float) -> v.ComponentEstimate:
        return v.ComponentEstimate("user", 0.01, 0.01, 0.1, ci_low, 0.02, 0.0, 0.2, 0.1)

    assert comp(0.001).ci_excludes_zero
    assert not comp(0.0).ci_excludes_zero
    assert not comp(-0.01).ci_excludes_zero


def test_decompose_variance_on_hand_frame() -> None:
    res = v.decompose_variance(crossed_frame(HAND), n_boot=50)
    assert (res.n_tasks, res.n_simulators, res.n_reps) == (2, 2, 2)
    raw = {k: c.raw for k, c in res.components.items()}
    assert raw == pytest.approx(
        {"task": 0.0, "simulator": -0.125, "task_x_simulator": 0.125, "residual": 0.25}
    )
    assert res.components["simulator"].truncated == 0.0
    # Untruncated ratios over raw sum .25: 0, -.5, .5, 1.
    raw_shares = [res.components[k].raw_share for k in v.CROSSED_COMPONENTS]
    np.testing.assert_allclose(raw_shares, [0.0, -0.5, 0.5, 1.0])
    # Positive parts: 0, 0, .125, .25 -> shares 0, 0, 1/3, 2/3.
    shares = [res.components[k].share for k in v.CROSSED_COMPONENTS]
    np.testing.assert_allclose(shares, [0.0, 0.0, 1 / 3, 2 / 3])
    assert res.p_bar.estimate == pytest.approx(0.5)
    assert res.flip_rate.estimate == pytest.approx(0.5)
    assert res.flip_rate_by_task.to_dict() == {"0": 0.5, "1": 0.5}
    # Mixed-under-the-other-simulator selection (never looks at the measured cell):
    # (0,sim0)=[1,1]: other [1,0] mixed -> selected, no flip.  (0,sim1): other [1,1] not.
    # (1,sim0)=[0,0]: other [0,1] mixed -> selected, no flip.  (1,sim1): other [0,0] not.
    # -> 0/2 = 0. Selecting on the task's own outcomes gave 0.5 (both tasks "mixed").
    assert res.flip_rate_mixed_tasks == pytest.approx(0.0)
    assert res.simulator_means.to_dict() == {"sim0": 0.5, "sim1": 0.5}
    # E[Db - Dw] = 2 (sim + txs) = 0; here it is exactly .5 - .5.
    assert res.excess_between_disagreement.estimate == pytest.approx(0.0)
    assert list(res.to_frame().index) == list(v.CROSSED_COMPONENTS)


def test_single_simulator_task_and_residual_only() -> None:
    # Task 0 = [1,1,1,0] (mean .75), task 1 = [0,0,0,1] (mean .25), R = 4.
    # MS_T = R sum (yt-g)^2/(T-1) = 4 * 2 * .0625 = .5
    # MS_E = (3*.0625 + .5625 + .5625 + 3*.0625)/(2*3) = 1.5/6 = .25
    # task = (MS_T - MS_E)/R = .0625
    y = np.array([[[1, 1, 1, 0]], [[0, 0, 0, 1]]], dtype=float)
    df = crossed_frame(y).drop(columns="simulator")
    res = v.decompose_variance(df, sim_col=None, n_boot=50)
    assert res.components["task"].raw == pytest.approx(0.0625)
    assert res.components["residual"].raw == pytest.approx(0.25)
    assert math.isnan(res.components["simulator"].raw)
    assert math.isnan(res.components["task_x_simulator"].raw)
    assert math.isnan(res.between_simulator_disagreement.estimate)
    # Shares only over identifiable components: .0625/.3125 = .2, .25/.3125 = .8.
    assert res.components["task"].share == pytest.approx(0.2)
    assert res.components["residual"].share == pytest.approx(0.8)
    # With one simulator "mixed" would be decided by the measured cell itself
    # (always 1.0), so the statistic is not reported.
    assert math.isnan(res.flip_rate_mixed_tasks)


def test_nested_hand_vector_agent_only_noise() -> None:
    # Task 0: success iff agent seed 0 (whatever the user seed); task 1: always fails.
    # yt = [.5, 0], g = .25; user means within task equal the task mean -> MS_U = 0.
    # yta = [[1, 0], [0, 0]] -> sum (yta - yt)^2 = .5; interaction residuals all 0.
    # D=1: MS_T = UAD * 2*.0625 / 1 = .5, MS_A = UD * .5 / (T(A-1)) = .5, MS_UA = 0.
    #   task = (.5 - 0 - .5 + 0)/4 = 0, user = 0, agent = .5/2 = .25, ua+nd = 0.
    base = np.array([[[1, 0], [1, 0]], [[0, 0], [0, 0]]], dtype=float)
    y1 = base[..., None]
    np.testing.assert_allclose(v.nested_components(y1), [0.0, 0.0, 0.25, 0.0])
    # D=2 identical duplicates: MS_T = 1, MS_A = 1 -> agent = 1/(U D) = .25, nd = 0.
    y2 = np.repeat(y1, 2, axis=-1)
    np.testing.assert_allclose(v.nested_components(y2), [0.0, 0.0, 0.25, 0.0, 0.0])
    res = v.decompose_nested(nested_frame(y2), n_boot=50)
    assert res.noise_shares["agent"].estimate == pytest.approx(1.0)
    assert res.noise_shares["user"].estimate == pytest.approx(0.0)
    assert res.raw_noise_shares == pytest.approx(
        {"user": 0.0, "agent": 1.0, "user_x_agent": 0.0, "nondeterminism": 0.0}
    )
    assert res.n_tasks_input == 2
    assert list(res.components) == list(v.NESTED_COMPONENTS)
    res1 = v.decompose_nested(nested_frame(y1), n_boot=50)
    assert list(res1.components) == list(v.NESTED_COMPONENTS_NO_DUPLICATES)


# ---------------------------------------------------------------------------
# Degenerate inputs and validation
# ---------------------------------------------------------------------------


def test_all_outcomes_equal_gives_zero_components() -> None:
    y = np.ones((5, 3, 4))
    res = v.decompose_variance(crossed_frame(y), n_boot=50)
    for comp in res.components.values():
        assert comp.raw == 0.0
        assert math.isnan(comp.share)
    assert res.flip_rate.estimate == 0.0
    assert math.isnan(res.flip_rate_mixed_tasks)
    nf = v.noise_floor(crossed_frame(y), n_boot=50)
    assert nf.expected_abs_diff.estimate == 0.0


def test_identical_probabilities_give_near_zero_structure() -> None:
    ds = v.generate_synthetic(400, 6, 4, 0.0, 0.0, 0.0, seed=11, mu=0.4)
    comps = v.crossed_components(ds.y)
    assert np.abs(comps[:3]).max() < 0.005
    assert comps[3] == pytest.approx(0.24, abs=0.01)


def test_single_repeat_raises() -> None:
    with pytest.raises(ValueError, match="repeat"):
        v.decompose_variance(crossed_frame(np.ones((3, 2, 1))))
    with pytest.raises(ValueError):
        v.crossed_components(np.ones((3, 2, 1)))


def test_single_task_raises() -> None:
    with pytest.raises(ValueError, match="2 complete tasks"):
        v.decompose_variance(crossed_frame(np.ones((1, 2, 3))))


def test_missing_and_invalid_outcomes_raise() -> None:
    df = crossed_frame(HAND)
    df["success"] = df["success"].astype("boolean")
    df.loc[0, "success"] = pd.NA
    with pytest.raises(ValueError, match="infrastructure"):
        v.decompose_variance(df)
    bad = crossed_frame(HAND).assign(success=0.5)
    with pytest.raises(ValueError, match="binary"):
        v.decompose_variance(bad)
    dup = pd.concat([crossed_frame(HAND), crossed_frame(HAND).iloc[:1]])
    with pytest.raises(ValueError, match="duplicate"):
        v.decompose_variance(dup)
    with pytest.raises(KeyError):
        v.decompose_variance(crossed_frame(HAND).drop(columns="trial"))


def test_unbalanced_incomplete_task_is_dropped_whole() -> None:
    y = np.array([[[1, 0, 1]] * 2, [[0, 0, 1]] * 2, [[1, 1, 1]] * 2], dtype=float)
    df = crossed_frame(y)
    # Remove one repeat of task "2" (as if it were an infrastructure error).
    df = df[~((df.task_id == "2") & (df.simulator == "sim1") & (df.trial == 2))]
    with pytest.warns(v.UnbalancedDesignWarning, match="kept 2/3 tasks"):
        arr = v.to_crossed_array(df, n_reps=3)
    assert arr.task_ids == ("0", "1")
    assert arr.dropped_task_ids == ("2",)
    np.testing.assert_array_equal(arr.y, y[:2])
    assert arr.n_tasks_input == 3
    assert arr.kept_task_fraction == pytest.approx(2 / 3)
    # Kept tasks: [1,0,1]x2 + [0,0,1]x2 -> 6/12 = .5. Dropped task "2": 5 runs, all 1.
    assert arr.kept_tasks_success_rate == pytest.approx(0.5)
    assert arr.dropped_tasks_success_rate == pytest.approx(1.0)
    with pytest.raises(v.UnbalancedDesignError):
        v.to_crossed_array(df, n_reps=3, on_unbalanced="raise")


def test_default_reps_cut_repeats_instead_of_dropping_tasks() -> None:
    # Same data: keeping R=3 would keep only 2/3 tasks (< 0.9), so the default target
    # is R=2: no task is dropped and the first two repeats (by trial) of every cell kept.
    y = np.array([[[1, 0, 1]] * 2, [[0, 0, 1]] * 2, [[1, 1, 1]] * 2], dtype=float)
    df = crossed_frame(y)
    df = df[~((df.task_id == "2") & (df.simulator == "sim1") & (df.trial == 2))]
    with pytest.warns(v.UnbalancedDesignWarning, match="5 cell"):
        arr = v.to_crossed_array(df)
    assert arr.task_ids == ("0", "1", "2") and arr.dropped_task_ids == ()
    np.testing.assert_array_equal(arr.y, y[:, :, :2])
    assert math.isnan(arr.dropped_tasks_success_rate)


def test_default_reps_keeps_largest_r_that_keeps_enough_tasks() -> None:
    # 10 tasks x 2 sims x 4 repeats. Tasks 0-3 are hard (always fail) and each lost one
    # run to an infrastructure error; tasks 4-9 always succeed.
    # The old modal rule (R=4) would drop the 4 hard tasks: p_bar 1.0 instead of 0.6.
    # R=4 keeps 6/10 < 0.9; R=3 keeps 10/10 -> R=3, nothing dropped, p_bar = 6/10.
    y = np.zeros((10, 2, 4))
    y[4:] = 1.0
    df = crossed_frame(y)
    lost = df.task_id.isin(["0", "1", "2", "3"]) & (df.simulator == "sim0") & (df.trial == 1)
    df = df[~lost]
    with pytest.warns(v.UnbalancedDesignWarning):
        res = v.decompose_variance(df, n_boot=20)
    assert (res.n_tasks, res.n_reps, res.dropped_task_ids) == (10, 3, ())
    assert res.p_bar.estimate == pytest.approx(0.6)
    # One lost task out of 10 still meets the 0.9 threshold: R=4, task "0" dropped.
    one = crossed_frame(y)
    one = one[~((one.task_id == "0") & (one.simulator == "sim0") & (one.trial == 1))]
    with pytest.warns(v.UnbalancedDesignWarning, match="dropped tasks 0.000"):
        arr = v.to_crossed_array(one)
    assert arr.y.shape == (9, 2, 4) and arr.dropped_task_ids == ("0",)
    # A stricter threshold forces R=3 and keeps every task.
    with pytest.warns(v.UnbalancedDesignWarning):
        assert v.to_crossed_array(one, min_task_fraction=1.0).y.shape == (10, 2, 3)
    with pytest.raises(ValueError, match="min_task_fraction"):
        v.to_crossed_array(one, min_task_fraction=0.0)


def test_flip_rate_mixed_elsewhere_is_not_selected_on_the_outcome() -> None:
    # Every cell has p = .5 and R = 3: P(flip) = 1 - 2/8 = .75 for every cell, and the
    # other-simulator selection is independent of the measured cell, so the conditional
    # flip rate is still .75. (Selecting on the task's own 6 runs would give
    # .75 / (1 - 2/64) = .774, and with one simulator always 1.0.)
    rng = np.random.default_rng(5)
    y = (rng.random((20000, 2, 3)) < 0.5).astype(float)
    assert v.flip_rate_mixed_elsewhere(y) == pytest.approx(0.75, abs=0.01)
    assert math.isnan(v.flip_rate_mixed_elsewhere(y[:, :1]))


def test_unbalanced_extra_repeats_are_truncated_by_trial_order() -> None:
    y = np.array([[[1, 0]] * 2, [[0, 1]] * 2, [[1, 1]] * 2], dtype=float)
    extra = pd.DataFrame([{"task_id": "0", "simulator": "sim0", "trial": 7, "success": False}])
    df = pd.concat([crossed_frame(y), extra], ignore_index=True)
    with pytest.warns(v.UnbalancedDesignWarning, match="extra"):
        arr = v.to_crossed_array(df)
    assert arr.n_truncated_cells == 1
    np.testing.assert_array_equal(arr.y, y)


def test_balanced_input_emits_no_warning_and_sorts_ids_naturally() -> None:
    y = np.zeros((11, 2, 2))
    y[:, :, 0] = 1
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        arr = v.to_crossed_array(crossed_frame(y))
    assert arr.task_ids[:3] == ("0", "1", "2")
    assert arr.task_ids[-1] == "10"


def test_synthetic_generator_rejects_clipping() -> None:
    with pytest.raises(ValueError, match="leaves"):
        v.generate_synthetic(5, 2, 2, 0.3, 0.2, 0.1, seed=0, mu=0.5)
    with pytest.raises(ValueError, match="leaves"):
        v.generate_synthetic_nested(5, 2, 2, 2, 0.3, 0.2, 0.1, 0.1, seed=0)


# ---------------------------------------------------------------------------
# Unbiasedness (Monte Carlo against known truth)
# ---------------------------------------------------------------------------


def _assert_unbiased(estimates: np.ndarray, truth: list[float]) -> None:
    mean = estimates.mean(axis=0)
    mc_se = estimates.std(axis=0, ddof=1) / math.sqrt(estimates.shape[0])
    assert np.all(np.abs(mean - np.asarray(truth)) < 4 * mc_se), (mean, truth, mc_se)


def test_crossed_estimator_unbiased_probability_scale() -> None:
    sds = (0.2, 0.1, 0.15)
    y = v.simulate_crossed_arrays(2000, 40, 6, 4, *sds, seed=101)
    truth = v.crossed_truth(0.5, *sds)
    assert list(truth.values()) == pytest.approx([0.04, 0.01, 0.0225, 0.1775])
    _assert_unbiased(v.crossed_components(y), list(truth.values()))


def test_crossed_estimator_unbiased_logit_scale_validates_quadrature_truth() -> None:
    sds = (1.0, 0.5, 0.7)
    y = v.simulate_crossed_arrays(3000, 30, 6, 3, *sds, seed=102, mu=0.3, scale="logit")
    truth = v.crossed_truth(0.3, *sds, scale="logit")
    _assert_unbiased(v.crossed_components(y), list(truth.values()))


def test_logit_truth_is_a_decomposition_of_total_variance() -> None:
    truth = v.crossed_truth(0.3, 1.0, 0.5, 0.7, scale="logit")
    assert min(truth.values()) > 0
    # Monte Carlo check of the total p_bar(1 - p_bar) with a large direct draw.
    rng = np.random.default_rng(0)
    p = 1 / (1 + np.exp(-(np.log(0.3 / 0.7) + rng.normal(0, math.sqrt(1.74), 2_000_000))))
    assert sum(truth.values()) == pytest.approx(p.mean() * (1 - p.mean()), abs=1e-3)
    # No simulator effect -> exactly zero simulator component.
    assert v.crossed_truth(0.5, 1.0, 0.0, 0.5, scale="logit")["simulator"] == 0.0


def test_nested_estimator_unbiased() -> None:
    rng_seeds = range(2000)
    sds = (0.2, 0.1, 0.15, 0.05)
    est = np.array(
        [
            v.nested_components(v.generate_synthetic_nested(30, 3, 3, 2, *sds, seed=s).y)
            for s in rng_seeds
        ]
    )
    truth = v.generate_synthetic_nested(2, 2, 2, 2, *sds, seed=0).truth
    assert list(truth.values()) == pytest.approx([0.04, 0.01, 0.0225, 0.0025, 0.175])
    _assert_unbiased(est, list(truth.values()))


def test_nested_without_duplicates_merges_interaction_and_nondeterminism() -> None:
    sds = (0.2, 0.1, 0.15, 0.05)
    est = np.array(
        [
            v.nested_components(v.generate_synthetic_nested(30, 3, 3, 1, *sds, seed=s).y)
            for s in range(2000)
        ]
    )
    # ua + nd = .0025 + .175
    _assert_unbiased(est, [0.04, 0.01, 0.0225, 0.1775])


# ---------------------------------------------------------------------------
# Recovery on large synthetic data, zero components, coverage
# ---------------------------------------------------------------------------


def test_large_synthetic_recovers_components_and_ordering() -> None:
    ds = v.generate_synthetic(400, 12, 4, 0.25, 0.1, 0.15, seed=7)
    res = v.decompose_variance(ds.frame, n_boot=300)
    raw = {k: c.raw for k, c in res.components.items()}
    # truth: task .0625, sim .01, txs .0225, residual .155
    for name, tol in [
        ("task", 0.008),
        ("simulator", 0.005),
        ("task_x_simulator", 0.004),
        ("residual", 0.005),
    ]:
        assert raw[name] == pytest.approx(ds.truth[name], abs=tol), name
    assert raw["residual"] > raw["task"] > raw["task_x_simulator"] > raw["simulator"]
    for name in ("task", "task_x_simulator", "residual"):
        comp = res.components[name]
        assert comp.ci_low <= ds.truth[name] <= comp.ci_high, name
    # E[Db - Dw] = 2 (sim + txs) = .065
    assert res.excess_between_disagreement.estimate == pytest.approx(0.065, abs=0.01)


def test_large_synthetic_logit_scale_recovers_components() -> None:
    ds = v.generate_synthetic(400, 12, 4, 1.0, 0.0, 0.5, seed=8, scale="logit")
    res = v.decompose_variance(ds.frame, n_boot=300)
    raw = {k: c.raw for k, c in res.components.items()}
    assert raw["simulator"] == pytest.approx(0.0, abs=0.003)
    assert raw["task"] == pytest.approx(ds.truth["task"], abs=0.008)
    assert raw["task_x_simulator"] == pytest.approx(ds.truth["task_x_simulator"], abs=0.005)
    assert raw["residual"] == pytest.approx(ds.truth["residual"], abs=0.006)


def test_zero_components_estimated_near_zero_with_ci_containing_zero() -> None:
    ds = v.generate_synthetic(400, 6, 4, 0.2, 0.0, 0.0, seed=9)
    res = v.decompose_variance(ds.frame, n_boot=400)
    for name in ("simulator", "task_x_simulator"):
        comp = res.components[name]
        assert abs(comp.raw) < 0.004, name
        assert comp.ci_low <= 0.0 <= comp.ci_high, name
        assert comp.frac_boot_nonpositive > 0.025, name
    assert res.components["task"].ci_low > 0.02


def test_bootstrap_coverage_small_seeded_check() -> None:
    """Task bootstrap covers task, txs and residual at roughly nominal 95% (T=40)."""
    sds = (0.2, 0.1, 0.15)
    truth = np.array(list(v.crossed_truth(0.5, *sds).values()))
    data = v.simulate_crossed_arrays(300, 40, 3, 4, *sds, seed=2026)
    covered = np.zeros(4)
    for i, y in enumerate(data):
        boot = v.cluster_bootstrap(y, v.crossed_components, n_boot=400, seed=i)
        covered += (boot.ci_low <= truth) & (truth <= boot.ci_high)
    coverage = covered / len(data)
    # Spec Monte Carlo: task .93, txs .90, resid .94 (simulator under-covers at S=3).
    assert 0.88 <= coverage[0] <= 0.99
    assert 0.84 <= coverage[2] <= 0.99
    assert 0.88 <= coverage[3] <= 0.99
    # Documented limitation: with S=3 simulators the task bootstrap under-covers the
    # simulator component (spec Monte Carlo: .75), so its CI must not be reported as 95%.
    assert coverage[1] < 0.85


def test_bootstrap_is_deterministic_given_seed() -> None:
    df = v.generate_synthetic(30, 3, 3, 0.2, 0.1, 0.1, seed=1).frame
    a = v.decompose_variance(df, n_boot=200, seed=5)
    b = v.decompose_variance(df, n_boot=200, seed=5)
    c = v.decompose_variance(df, n_boot=200, seed=6)
    assert a.to_frame().equals(b.to_frame())
    assert not a.to_frame().equals(c.to_frame())
    assert (a.seed, a.n_boot, a.alpha) == (5, 200, 0.05)


def test_task_bootstrap_resamples_whole_tasks() -> None:
    idx = v.task_bootstrap_indices(5, 3, seed=0)
    y = np.arange(5 * 2 * 2, dtype=float).reshape(5, 2, 2)
    boot = v.cluster_bootstrap(y, lambda a: a[..., 0, 0, 0][..., None], indices=idx)
    np.testing.assert_array_equal(boot.draws[:, 0], y[idx[:, 0], 0, 0])


# ---------------------------------------------------------------------------
# Noise floor
# ---------------------------------------------------------------------------


def test_noise_floor_from_residual_hand_values() -> None:
    # Var(D) = 2 * .25 / 2 = .25 -> sd .5; E|D| = .5 sqrt(2/pi); band = 1.959964 * .5
    sd, mad, band = v.noise_floor_from_residual(0.25, n_eval_tasks=2)
    assert sd == pytest.approx(0.5)
    assert mad == pytest.approx(0.5 * math.sqrt(2 / math.pi))
    assert band == pytest.approx(1.959964 * 0.5, rel=1e-6)
    # Four trials per task per run halve the sd.
    assert v.noise_floor_from_residual(0.25, 2, trials_per_run=4)[0] == pytest.approx(0.25)
    assert v.noise_floor_from_residual(-0.01, 2)[0] == 0.0


def test_noise_floor_on_hand_frame() -> None:
    nf = v.noise_floor(crossed_frame(HAND), n_boot=50)
    # residual = MS_E = .25, N = T = 2 -> as in the hand values above.
    assert nf.residual.estimate == pytest.approx(0.25)
    assert nf.sd_diff.estimate == pytest.approx(0.5)
    assert nf.n_eval_tasks == 2


def test_noise_floor_matches_simulated_identical_runs() -> None:
    """E|pass^1(run1) - pass^1(run2)| from direct simulation matches the estimate."""
    n_tasks = 200
    ds = v.generate_synthetic(n_tasks, 1, 6, 0.3, 0.0, 0.15, seed=31)
    p = ds.p[:, 0]
    rng = np.random.default_rng(32)
    runs = (rng.random((20000, 2, n_tasks)) < p).astype(float)
    empirical = np.abs(runs[:, 0].mean(axis=1) - runs[:, 1].mean(axis=1)).mean()
    exact = v.noise_floor_from_residual(float((p * (1 - p)).mean()), n_tasks)[1]
    assert exact == pytest.approx(empirical, rel=0.03)
    nf = v.noise_floor(ds.frame, n_boot=300)
    assert nf.expected_abs_diff.estimate == pytest.approx(empirical, rel=0.1)
    assert nf.expected_abs_diff.ci_low <= empirical <= nf.expected_abs_diff.ci_high


# ---------------------------------------------------------------------------
# Paired comparison of two conditions
# ---------------------------------------------------------------------------


def test_compare_identical_conditions_gives_zero_difference() -> None:
    df = v.generate_synthetic(30, 3, 3, 0.2, 0.1, 0.1, seed=3).frame
    out = v.compare_decompositions(df, df, labels=("en", "hi"), n_boot=200)
    assert {"estimate_en", "estimate_hi", "difference"} <= set(out.columns)
    finite = out.dropna()
    assert (finite["difference"] == 0).all()
    assert (finite["ci_low"] == 0).all() and (finite["ci_high"] == 0).all()


def test_compare_detects_known_difference_in_interaction() -> None:
    en = v.generate_synthetic(300, 3, 4, 0.15, 0.05, 0.0, seed=4).frame
    hi = v.generate_synthetic(300, 3, 4, 0.15, 0.05, 0.25, seed=5).frame
    out = v.compare_decompositions(en, hi, labels=("en", "hi"), n_boot=300)
    row = out.loc["task_x_simulator"]
    assert row["difference"] == pytest.approx(0.0625, abs=0.015)
    assert row["ci_low"] > 0


def test_compare_aligns_on_common_tasks_with_warning() -> None:
    a = v.generate_synthetic(10, 2, 3, 0.2, 0.0, 0.1, seed=1).frame
    b = a[a.task_id != "3"]
    with pytest.warns(v.UnbalancedDesignWarning, match="9 common tasks"):
        out = v.compare_decompositions(a, b, n_boot=50)
    assert (out.dropna()["difference"] == 0).all()


def test_compare_nested_design_runs() -> None:
    a = v.generate_synthetic_nested(20, 2, 2, 2, 0.2, 0.1, 0.1, 0.05, seed=1).frame
    b = v.generate_synthetic_nested(20, 2, 2, 2, 0.2, 0.0, 0.2, 0.05, seed=2).frame
    out = v.compare_decompositions(a, b, design="nested", n_boot=100)
    assert "noise_share_user" in out.index
    assert out.loc["noise_share_user", "ci_low"] <= out.loc["noise_share_user", "ci_high"]
