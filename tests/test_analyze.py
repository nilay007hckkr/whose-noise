"""Tests for whose_noise.analyze (summary numbers + CLI smoke tests)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from whose_noise import analyze
from whose_noise.tau2_results import load_runs

# ----------------------------------------------------------------- pure helpers


def test_wilson_fallback_known_values() -> None:
    # 3/6 at 95%: textbook Wilson interval [0.1876, 0.8124]
    lo, hi = analyze._wilson_fallback(3, 6)
    assert lo == pytest.approx(0.1876, abs=1e-4)
    assert hi == pytest.approx(0.8124, abs=1e-4)
    # 0/10 -> lower bound exactly 0, upper ~0.2775
    lo, hi = analyze._wilson_fallback(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-12) and hi == pytest.approx(0.2775, abs=1e-4)
    assert all(math.isnan(x) for x in analyze._wilson_fallback(0, 0))


def test_pass_hat_k_fallback_hand_computed() -> None:
    outcomes = {"a": [True, True, True], "b": [True, True, False], "c": [False] * 3}
    # pass^1 = (1 + 2/3 + 0)/3 ; pass^2 = (1 + C(2,2)/C(3,2) + 0)/3 = (1 + 1/3)/3
    assert analyze._pass_hat_k_fallback(outcomes, 1) == pytest.approx(5 / 9)
    assert analyze._pass_hat_k_fallback(outcomes, 2) == pytest.approx(4 / 9)
    assert analyze._pass_hat_k_fallback(outcomes, 3) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        analyze._pass_hat_k_fallback(outcomes, 4)


def test_groq_budget() -> None:
    b = analyze.groq_budget(mean_user_tokens=8000, mean_user_requests=5)
    assert b["tasks_per_day"] == pytest.approx(25.0)  # 200000 / 8000
    assert b["tasks_per_day_by_requests"] == pytest.approx(200.0)  # 1000 / 5
    assert b["binding"] == pytest.approx(25.0)
    assert math.isnan(analyze.groq_budget(0, 0)["tasks_per_day"])


# ----------------------------------------------------------------- run summary


@pytest.fixture(params=["with_metrics", "fallback"])
def metrics_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run summary tests both with metrics.py and with it 'missing'."""
    if request.param == "fallback":
        monkeypatch.setattr(analyze, "_metrics_module", lambda: None)
    elif analyze._metrics_module() is None:
        pytest.skip("whose_noise.metrics not available")
    return request.param


def test_summarize_synthetic(synthetic_results: Path, metrics_mode: str) -> None:
    s = analyze.summarize_run(load_runs(synthetic_results))
    assert (s.n_sims, s.n_infra, s.n_valid, s.n_tasks) == (7, 1, 6, 3)
    assert s.max_trials == 3  # task "x" has 3 sims incl. the infra error
    assert s.min_valid_trials == 2
    assert s.successes == 3 and s.pooled_rate == pytest.approx(0.5)
    assert s.pooled_wilson[0] == pytest.approx(0.1876, abs=1e-3)
    assert s.pass_k[1] == pytest.approx(0.5)
    assert s.pass_k[2] == pytest.approx(1 / 3)
    assert s.pass_k[3] is None  # every task would need 3 valid trials
    assert s.flip_rate == pytest.approx(1 / 3) and (s.n_flips, s.n_flip_eligible) == (1, 3)
    assert s.mean_agent_tokens == pytest.approx(330.0)
    assert s.mean_user_tokens == pytest.approx(121.0)
    assert s.mean_user_requests == pytest.approx(2.0)
    # durations: trial0 -> 10 s, trial1 -> 20 s, three of each among valid sims
    assert s.mean_duration_s == pytest.approx(15.0)
    assert s.termination_counts == {"user_stop": 5, "max_steps": 1, "infrastructure_error": 1}
    # 3 tasks < MIN_TASKS_FOR_BOOTSTRAP: no (degenerate) bootstrap interval either way
    assert s.pass_k_ci[1] is None
    assert s.infra_breakdown == {"AuthenticationError @ evaluation": 1}
    assert s.n_tasks_dropped == 0


def test_cluster_interval(metrics_mode: str) -> None:
    outcomes = {str(i): [i % 2 == 0, True] for i in range(6)}  # pass^1 = (3*1 + 3*0.5)/6
    ci = analyze.cluster_interval(outcomes, k=1)
    if metrics_mode == "fallback":
        assert ci is None
        return
    assert ci is not None
    lo, hi = ci
    assert 0.5 <= lo <= 0.75 <= hi <= 1.0
    assert analyze.cluster_interval(outcomes, k=1) == ci  # seeded -> deterministic
    assert analyze.cluster_interval(dict(list(outcomes.items())[:4]), k=1) is None


def test_task_and_failed_tables(synthetic_results: Path) -> None:
    df = load_runs(synthetic_results)
    tt = analyze.task_table(df).set_index("task")
    assert tt.loc["2"].tolist() == [2, 0, 2, "no"]
    assert tt.loc["10"].tolist() == [2, 0, 1, "yes"]
    assert tt.loc["x"].tolist() == [2, 1, 0, "no"]
    ft = analyze.failed_table(df)
    assert list(zip(ft["task"], ft["trial"], strict=True)) == [("10", 0), ("x", 0), ("x", 1)]
    assert ft["first_failed_action"].tolist() == ["cancel_pending_order", "-", "-"]
    assert ft["extra_calls"].tolist() == ["-", "-", "modify_user_address"]


# ----------------------------------------------------------------- CLI smoke tests


def test_cli_on_mini_fixture(mini_results: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert analyze.main([str(mini_results)]) == 0
    out = capsys.readouterr().out
    assert "=== run: fixtures ===" in out
    assert "3 total, 1 infra errors excluded, 2 valid over 2 tasks" in out
    assert "pooled success : 0.500 (1/2)" in out
    assert "pass^1" in out
    assert "exchange_delivered_order_items" in out
    # mean user tokens = ((8210+966) + (4797+1007)) / 2 = 7490 -> 200000/7490 = 26.7
    assert "tasks_per_day = 200000 / 7490 = 26.7" in out
    assert "flip rate      : n/a" in out
    assert "infra errors   : AuthenticationError @ evaluation=1" in out
    assert "1 task(s) have no valid trial" in out


def test_cli_k_and_csv(
    synthetic_results: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = tmp_path / "out.csv"
    assert analyze.main([str(synthetic_results), "--k", "1", "2", "3", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "pass^2         : 0.333 (mean over tasks)" in out
    assert "pass^3         : n/a (min valid trials/task = 2 < 3)" in out
    assert "flip rate      : 0.333 (1/3 tasks with >=2 valid trials)" in out
    written = pd.read_csv(csv_path, dtype={"task_id": str})
    assert len(written) == 7
    row = written[(written["task_id"] == "10") & (written["trial"] == 0)].iloc[0]
    assert row["failed_write_actions"] == "cancel_pending_order"
    extra = written[(written["task_id"] == "x") & (written["trial"] == 1)].iloc[0]
    assert extra["extra_tool_calls"] == "modify_user_address"


def test_cli_rejects_bad_k(mini_results: Path) -> None:
    with pytest.raises(SystemExit):
        analyze.main([str(mini_results), "--k", "0"])


# ----------------------------------------------------------------- integrity and CI labels


def test_cli_refuses_duplicated_trials_unless_allowed(
    synthetic_results: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(synthetic_results.read_text(encoding="utf-8"))
    data["simulations"].append(dict(data["simulations"][0], seed=999))  # ("10", 1) again
    run = tmp_path / "dup_run"
    run.mkdir()
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    assert analyze.main([str(run)]) == 2
    assert "occur more than once" in capsys.readouterr().out
    assert analyze.main([str(run), "--allow-duplicates"]) == 0
    out = capsys.readouterr().out
    assert "WARNING        : duplicated trials" in out
    assert "more than one seed" in out


def test_small_task_bootstrap_ci_is_labelled_approximate() -> None:
    # 6 tasks: CI is shown (>= MIN_TASKS_FOR_BOOTSTRAP) but flagged (< 20 tasks).
    s = analyze.RunSummary(
        run_name="r",
        results_path="p",
        agent_llm="a",
        user_llm="u",
        n_sims=12,
        n_infra=0,
        n_valid=12,
        n_tasks=6,
        max_trials=2,
        min_valid_trials=2,
        successes=9,
        pooled_rate=0.75,
        pooled_wilson=(0.47, 0.91),
        pass_k={1: 0.75},
        pass_k_ci={1: (0.5, 1.0)},
    )
    line = next(ln for ln in analyze.format_summary(s).splitlines() if ln.startswith("pass^1"))
    assert "task-bootstrap95 [0.500, 1.000] (approximate, under-covers: 6 < 20 tasks)" in line
    s.n_tasks = 20
    line = next(ln for ln in analyze.format_summary(s).splitlines() if ln.startswith("pass^1"))
    assert "approximate" not in line
